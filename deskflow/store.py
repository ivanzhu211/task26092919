"""SQLite persistence for tasks, batches, and the undo ledger."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional


SCHEMA_SQL = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE tasks (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    due_ordinal INTEGER,
    labels_json TEXT NOT NULL
);

CREATE TABLE batches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    seq            INTEGER NOT NULL UNIQUE,
    status         TEXT,
    shift_days     INTEGER NOT NULL,
    add_labels     TEXT NOT NULL,
    remove_labels  TEXT NOT NULL,
    committed_at   INTEGER NOT NULL,
    undone         INTEGER NOT NULL DEFAULT 0,
    undone_at      INTEGER
);

CREATE TABLE batch_targets (
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    task_id  TEXT NOT NULL REFERENCES tasks(id),
    position INTEGER NOT NULL,
    PRIMARY KEY (batch_id, task_id)
);

CREATE INDEX idx_batch_targets_task ON batch_targets(task_id);
"""


class SQLiteStore:
    """Owns the SQLite database file and its schema lifecycle."""

    def __init__(self, path: str | Path):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', '1')"
        )

    def schema_version(self, conn: sqlite3.Connection) -> Optional[int]:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
        ).fetchone()
        if exists is None:
            return None
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        return None if row is None else int(row["value"])

    def mark_initialized(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('initialized', '1')"
        )

    def is_initialized(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT 1 FROM meta WHERE key = 'initialized'"
        ).fetchone()
        return row is not None

    def insert_task(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        status: str,
        due_ordinal: Optional[int],
        labels: frozenset[str],
    ) -> None:
        conn.execute(
            "INSERT INTO tasks(id, status, due_ordinal, labels_json)"
            " VALUES (?, ?, ?, ?)",
            (
                task_id,
                status,
                due_ordinal,
                json.dumps(sorted(labels), ensure_ascii=False),
            ),
        )

    def insert_batch(
        self,
        conn: sqlite3.Connection,
        seq: int,
        target_ids: tuple[str, ...],
        status: Optional[str],
        shift_days: int,
        add_labels: frozenset[str],
        remove_labels: frozenset[str],
        committed_at_ms: int,
    ) -> int:
        cur = conn.execute(
            "INSERT INTO batches(seq, status, shift_days, add_labels,"
            " remove_labels, committed_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                seq,
                status,
                shift_days,
                json.dumps(sorted(add_labels), ensure_ascii=False),
                json.dumps(sorted(remove_labels), ensure_ascii=False),
                committed_at_ms,
            ),
        )
        batch_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO batch_targets(batch_id, task_id, position)"
            " VALUES (?, ?, ?)",
            [
                (batch_id, task_id, position)
                for position, task_id in enumerate(target_ids)
            ],
        )
        return batch_id

    def mark_batch_undone(
        self, conn: sqlite3.Connection, batch_id: int, undone_at_ms: int
    ) -> None:
        conn.execute(
            "UPDATE batches SET undone = 1, undone_at = ? WHERE id = ?",
            (undone_at_ms, batch_id),
        )
