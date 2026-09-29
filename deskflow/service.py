"""High-level batch-editing operations (the library entry point)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .errors import (
    AlreadyInitializedError,
    BatchNotFoundError,
    InvalidChangeError,
    TaskNotFoundError,
    UndoWindowExpiredError,
)
from .models import Batch, Change, Task, TaskStatus
from .store import SQLiteStore


class BatchService:
    """Initialize tasks, submit batches, undo batches, and read tasks.

    Current state is derived by replaying every not-yet-undone batch in
    commit order on top of the initial snapshot. Batches are append-only
    rows; undo only flips an undo flag, so undoing batches out of order
    never revives an edit.
    """

    UNDO_WINDOW_MS = 300_000

    def __init__(self, store: SQLiteStore):
        self.store = store
        self.conn = store.connect()
        if store.schema_version(self.conn) is None:
            store.initialize_schema(self.conn)
            self.conn.commit()

    @classmethod
    def open(cls, db_path: str | Path) -> "BatchService":
        return cls(SQLiteStore(db_path))

    def close(self) -> None:
        if self.conn is not None:
            self.conn.commit()
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "BatchService":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------ init

    def initialize(self, tasks: Iterable[Task], now_ms: int) -> None:
        tasks = list(tasks)
        seen: set[str] = set()
        for task in tasks:
            if not task.id or not task.id.strip():
                raise InvalidChangeError("task id must be a non-empty string")
            if task.id in seen:
                raise InvalidChangeError(f"duplicate task id: {task.id}")
            seen.add(task.id)
            if not isinstance(task.status, TaskStatus):
                raise InvalidChangeError("task status must be a TaskStatus")
            if task.due_date is not None and not isinstance(
                task.due_date, date
            ):
                raise InvalidChangeError("due_date must be a date or None")

        try:
            with self.conn:
                if self.store.is_initialized(self.conn):
                    raise AlreadyInitializedError(
                        "task store is already initialized"
                    )
                for task in tasks:
                    due_ordinal = (
                        None
                        if task.due_date is None
                        else task.due_date.toordinal()
                    )
                    self.store.insert_task(
                        self.conn,
                        task.id,
                        task.status.value,
                        due_ordinal,
                        frozenset(task.labels),
                    )
                self.store.mark_initialized(self.conn)
        except sqlite3.IntegrityError as exc:
            raise InvalidChangeError(str(exc)) from exc

    # ---------------------------------------------------------------- submit

    def submit(
        self,
        target_ids: Sequence[str],
        change: Change,
        now_ms: int,
    ) -> Batch:
        self._validate_change(change)
        targets = tuple(dict.fromkeys(target_ids))
        if not targets:
            raise InvalidChangeError("a batch needs at least one target task")

        with self.conn:
            for task_id in targets:
                row = self.conn.execute(
                    "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise TaskNotFoundError(f"unknown task id: {task_id}")

            seq_row = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM batches"
            ).fetchone()
            seq = int(seq_row["max_seq"]) + 1

            batch_id = self.store.insert_batch(
                self.conn,
                seq,
                targets,
                None if change.status is None else change.status.value,
                change.shift_days,
                frozenset(change.add_labels),
                frozenset(change.remove_labels),
                now_ms,
            )
        return self.get_batch(batch_id)

    @staticmethod
    def _validate_change(change: Change) -> None:
        if not isinstance(change, Change):
            raise InvalidChangeError("change must be a Change instance")
        if change.is_empty():
            raise InvalidChangeError("a batch must apply at least one edit")
        if change.status is not None and not isinstance(
            change.status, TaskStatus
        ):
            raise InvalidChangeError("status must be a TaskStatus or None")
        overlap = set(change.add_labels) & set(change.remove_labels)
        if overlap:
            raise InvalidChangeError(
                f"label both added and removed in one batch: "
                f"{sorted(overlap)}"
            )
        for label in set(change.add_labels) | set(change.remove_labels):
            if not isinstance(label, str) or not label:
                raise InvalidChangeError("labels must be non-empty strings")

    # ------------------------------------------------------------------ undo

    def undo(self, batch_id: int, now_ms: int) -> Batch:
        with self.conn:
            row = self.conn.execute(
                "SELECT * FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                raise BatchNotFoundError(f"unknown batch id: {batch_id}")

            if row["undone"]:
                return self._row_to_batch(row)

            age_ms = now_ms - int(row["committed_at"])
            if age_ms >= self.UNDO_WINDOW_MS:
                raise UndoWindowExpiredError(
                    f"batch {batch_id} undo window expired"
                    f" ({age_ms} ms >= {self.UNDO_WINDOW_MS} ms)"
                )

            self.store.mark_batch_undone(self.conn, batch_id, now_ms)
        return self.get_batch(batch_id)

    # ------------------------------------------------------------------ read

    def get_task(self, task_id: str) -> Task:
        states = self._current_states()
        if task_id not in states:
            raise TaskNotFoundError(f"unknown task id: {task_id}")
        return states[task_id]

    def list_tasks(self) -> list[Task]:
        states = self._current_states()
        return [states[row["id"]] for row in self.conn
                .execute("SELECT id FROM tasks ORDER BY id")]

    def get_batch(self, batch_id: int) -> Batch:
        row = self.conn.execute(
            "SELECT * FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            raise BatchNotFoundError(f"unknown batch id: {batch_id}")
        return self._row_to_batch(row)

    # ------------------------------------------------------------- replay

    def _current_states(self) -> dict[str, Task]:
        task_rows = self.conn.execute(
            "SELECT id, status, due_ordinal, labels_json FROM tasks"
        ).fetchall()
        states: dict[str, Task] = {}
        for row in task_rows:
            states[row["id"]] = Task(
                id=row["id"],
                status=TaskStatus(row["status"]),
                due_date=(
                    None
                    if row["due_ordinal"] is None
                    else date.fromordinal(int(row["due_ordinal"]))
                ),
                labels=frozenset(json.loads(row["labels_json"])),
            )

        batch_rows = self.conn.execute(
            "SELECT b.id, b.status, b.shift_days, b.add_labels,"
            " b.remove_labels, t.task_id"
            " FROM batches b"
            " JOIN batch_targets t ON t.batch_id = b.id"
            " WHERE b.undone = 0"
            " ORDER BY b.seq, t.position"
        ).fetchall()
        for row in batch_rows:
            task_id = row["task_id"]
            current = states[task_id]
            status = current.status
            if row["status"] is not None:
                status = TaskStatus(row["status"])
            due_date = current.due_date
            if due_date is not None:
                due_date = date.fromordinal(
                    due_date.toordinal() + int(row["shift_days"])
                )
            labels = set(current.labels)
            labels.update(json.loads(row["add_labels"]))
            labels.difference_update(json.loads(row["remove_labels"]))
            states[task_id] = Task(
                id=task_id,
                status=status,
                due_date=due_date,
                labels=frozenset(labels),
            )
        return states

    def _row_to_batch(self, row: sqlite3.Row) -> Batch:
        targets = tuple(
            target_row["task_id"]
            for target_row in self.conn.execute(
                "SELECT task_id FROM batch_targets"
                " WHERE batch_id = ? ORDER BY position",
                (row["id"],),
            )
        )
        change = Change(
            status=(
                None
                if row["status"] is None
                else TaskStatus(row["status"])
            ),
            shift_days=int(row["shift_days"]),
            add_labels=frozenset(json.loads(row["add_labels"])),
            remove_labels=frozenset(json.loads(row["remove_labels"])),
        )
        return Batch(
            id=int(row["id"]),
            seq=int(row["seq"]),
            target_ids=targets,
            change=change,
            committed_at_ms=int(row["committed_at"]),
            undone=bool(row["undone"]),
            undone_at_ms=(
                None
                if row["undone_at"] is None
                else int(row["undone_at"])
            ),
        )
