"""基于 SQLite 的批量任务编辑与撤销存储。

设计要点：
- 任务初始状态单独存于 ``tasks`` 表；每个批次存于 ``batches`` /
  ``batch_targets`` / ``batch_tag_ops`` 三张表，带有确定的提交顺序 ``seq``。
- 当前状态不做物化，读取时从初始状态出发，按 ``seq`` 顺序重放所有未撤销
  批次得到。因此撤销可以按任意顺序发生，且被撤销批次的编辑意图不会因
  遮蔽批次先被撤销而重新出现。
- 撤销窗口由调用方提供的整数毫秒时间判定：``now - committed_at < 300_000``
  才可撤销；恰满 300 秒即不可撤销。过期只关闭窗口，不消除批次效果。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date
from pathlib import Path
from typing import Optional

from .errors import BatchNotFoundError, UndoWindowExpiredError
from .models import Batch, BatchChange, Task

UNDO_WINDOW_MS = 300_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id   TEXT PRIMARY KEY,
    status    TEXT NOT NULL,
    due_date  TEXT,
    labels    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS batches (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         TEXT NOT NULL UNIQUE,
    committed_at_ms  INTEGER NOT NULL,
    status_value     TEXT,
    due_shift_days   INTEGER,
    undone           INTEGER NOT NULL DEFAULT 0,
    undone_at_ms     INTEGER
);
CREATE TABLE IF NOT EXISTS batch_targets (
    batch_seq INTEGER NOT NULL,
    task_id   TEXT NOT NULL,
    PRIMARY KEY (batch_seq, task_id),
    FOREIGN KEY (batch_seq) REFERENCES batches(seq),
    FOREIGN KEY (task_id)   REFERENCES tasks(task_id)
);
CREATE TABLE IF NOT EXISTS batch_tag_ops (
    batch_seq INTEGER NOT NULL,
    tag       TEXT NOT NULL,
    op        TEXT NOT NULL CHECK (op IN ('add', 'remove')),
    PRIMARY KEY (batch_seq, tag),
    FOREIGN KEY (batch_seq) REFERENCES batches(seq)
);
"""


def _encode_labels(labels: set[str] | frozenset[str]) -> str:
    return ",".join(sorted(labels))


def _decode_labels(raw: str) -> frozenset[str]:
    return frozenset(raw.split(",")) if raw else frozenset()


class DeskflowStore:
    """库的入口，由上层直接实例化并调用。"""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        """打开（必要时创建）数据库。``:memory:`` 仅用于测试。"""
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- 初始化 -------------------------------------------------------------

    def initialize_tasks(
        self,
        tasks: list[tuple[str, str, Optional[date], set[str] | frozenset[str]]],
    ) -> None:
        """写入任务初始状态。

        每个元素为 ``(task_id, status, due_date, labels)``；仅允许在尚无任务
        （且尚无批次）时初始化一次，重复初始化抛 ``ValueError``。
        """
        if not tasks:
            raise ValueError("initialize_tasks 需要至少一个任务")
        seen: set[str] = set()
        rows: list[tuple[str, str, Optional[str], str]] = []
        for task_id, status, due, labels in tasks:
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("任务标识必须是非空字符串")
            if not isinstance(status, str) or not status:
                raise ValueError("任务状态必须是非空字符串")
            if due is not None and not isinstance(due, date):
                raise ValueError("截止日必须是 datetime.date 或 None")
            if task_id in seen:
                raise ValueError(f"任务标识重复: {task_id}")
            seen.add(task_id)
            rows.append((task_id, status, due.isoformat() if due else None,
                         _encode_labels(frozenset(labels))))

        cur = self._conn.execute("SELECT COUNT(*) FROM tasks")
        if cur.fetchone()[0] > 0:
            raise ValueError("任务已初始化，不能重复初始化")

        self._conn.executemany(
            "INSERT INTO tasks (task_id, status, due_date, labels) VALUES (?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    # -- 提交批次 -----------------------------------------------------------

    def commit_batch(
        self,
        target_task_ids: list[str],
        status: Optional[str] = None,
        due_date_shift_days: Optional[int] = None,
        add_labels: Optional[set[str]] = None,
        remove_labels: Optional[set[str]] = None,
        *,
        now_ms: int,
    ) -> Batch:
        """提交一个批次，对全部目标任务原子生效。

        ``status`` / ``due_date_shift_days`` 为 None 表示该维度不改动；
        平移 0 天是有效意图。标签集合为 None 或空表示不操作该方向。
        """
        self._check_clock(now_ms)

        targets = list(target_task_ids)
        if not targets:
            raise ValueError("批次必须至少包含一个目标任务")
        if len(set(targets)) != len(targets):
            raise ValueError("批次目标任务不能重复")
        if status is not None and (not isinstance(status, str) or not status):
            raise ValueError("状态必须是非空字符串或 None")
        if due_date_shift_days is not None:
            if isinstance(due_date_shift_days, bool) or not isinstance(
                due_date_shift_days, int
            ):
                raise ValueError("截止日平移必须是整数天或 None")
        adds = frozenset(add_labels or ())
        removes = frozenset(remove_labels or ())
        if adds & removes:
            raise ValueError("同一批次不能同时添加和移除同一标签")
        if any(
            not isinstance(tag, str) or not tag or "," in tag
            for tag in adds | removes
        ):
            raise ValueError("标签必须是不含逗号的非空字符串")
        change = BatchChange(status, due_date_shift_days, adds, removes)
        if (
            change.status is None
            and change.due_date_shift_days is None
            and not change.add_labels
            and not change.remove_labels
        ):
            raise ValueError("批次不包含任何改动意图")

        batch_id = uuid.uuid4().hex
        try:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO batches (batch_id, committed_at_ms, status_value, "
                    "due_shift_days) VALUES (?, ?, ?, ?)",
                    (batch_id, now_ms, status, due_date_shift_days),
                )
                seq = cur.lastrowid
                self._conn.executemany(
                    "INSERT INTO batch_targets (batch_seq, task_id) VALUES (?, ?)",
                    [(seq, task_id) for task_id in targets],
                )
                self._conn.executemany(
                    "INSERT INTO batch_tag_ops (batch_seq, tag, op) VALUES (?, ?, ?)",
                    [(seq, tag, "add") for tag in sorted(adds)]
                    + [(seq, tag, "remove") for tag in sorted(removes)],
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"批次包含不存在的目标任务: {exc}") from exc

        self._set_last_clock(now_ms)
        return self.get_batch(batch_id)

    # -- 撤销批次 -----------------------------------------------------------

    def undo_batch(self, batch_id: str, *, now_ms: int) -> Batch:
        """撤销一个批次。

        - 未撤销且 ``now - committed_at_ms < 300_000``：标记撤销，整体生效。
        - 未撤销且已满 300 秒：抛 ``UndoWindowExpiredError``，不改变任何任务。
        - 已撤销：直接返回（幂等成功），不受期限限制。
        """
        self._check_clock(now_ms)

        cur = self._conn.execute(
            "SELECT seq, committed_at_ms, undone FROM batches WHERE batch_id = ?",
            (batch_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise BatchNotFoundError(f"批次不存在: {batch_id}")
        seq, committed_at, undone = row

        if not undone:
            if now_ms - committed_at >= UNDO_WINDOW_MS:
                raise UndoWindowExpiredError(
                    f"批次 {batch_id} 的撤销窗口已过"
                )
            with self._conn:
                self._conn.execute(
                    "UPDATE batches SET undone = 1, undone_at_ms = ? WHERE seq = ?",
                    (now_ms, seq),
                )
            self._set_last_clock(now_ms)
        return self.get_batch(batch_id)

    # -- 读取 ---------------------------------------------------------------

    def get_task(self, task_id: str) -> Task:
        """读取单个任务的当前状态（初始状态 + 未撤销批次按序重放）。"""
        cur = self._conn.execute(
            "SELECT status, due_date, labels FROM tasks WHERE task_id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"任务不存在: {task_id}")
        return self._snapshot(task_id, row)

    def get_tasks(self) -> list[Task]:
        """读取全部任务的当前状态，按任务标识排序。"""
        rows = self._conn.execute(
            "SELECT task_id, status, due_date, labels FROM tasks ORDER BY task_id"
        ).fetchall()
        return [
            self._snapshot(task_id, (status, due, labels))
            for task_id, status, due, labels in rows
        ]

    def get_batch(self, batch_id: str) -> Batch:
        """读取批次元数据（含撤销状态）。"""
        cur = self._conn.execute(
            "SELECT seq, batch_id, committed_at_ms, status_value, "
            "due_shift_days, undone, undone_at_ms FROM batches WHERE batch_id = ?",
            (batch_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise BatchNotFoundError(f"批次不存在: {batch_id}")
        seq, bid, committed_at, status_value, shift, undone, undone_at = row
        targets = tuple(
            r[0]
            for r in self._conn.execute(
                "SELECT task_id FROM batch_targets WHERE batch_seq = ? ORDER BY task_id",
                (seq,),
            )
        )
        tag_rows = self._conn.execute(
            "SELECT tag, op FROM batch_tag_ops WHERE batch_seq = ?", (seq,)
        ).fetchall()
        adds = frozenset(tag for tag, op in tag_rows if op == "add")
        removes = frozenset(tag for tag, op in tag_rows if op == "remove")
        return Batch(
            batch_id=bid,
            seq=seq,
            committed_at_ms=committed_at,
            target_task_ids=targets,
            change=BatchChange(status_value, shift, adds, removes),
            undone=bool(undone),
            undone_at_ms=undone_at,
        )

    def list_batches(self) -> list[Batch]:
        """按提交顺序列出全部批次。"""
        ids = [
            r[0]
            for r in self._conn.execute("SELECT batch_id FROM batches ORDER BY seq")
        ]
        return [self.get_batch(bid) for bid in ids]

    def close(self) -> None:
        """正常关闭数据库连接。"""
        self._conn.close()

    # -- 内部 ---------------------------------------------------------------

    def _snapshot(
        self,
        task_id: str,
        initial: tuple[str, Optional[str], str],
    ) -> Task:
        status, due_raw, labels_raw = initial
        due: Optional[date] = date.fromisoformat(due_raw) if due_raw else None
        labels: set[str] = set(_decode_labels(labels_raw))

        active = self._conn.execute(
            "SELECT b.seq, b.status_value, b.due_shift_days FROM batches b "
            "JOIN batch_targets t ON t.batch_seq = b.seq "
            "WHERE b.undone = 0 AND t.task_id = ? ORDER BY b.seq",
            (task_id,),
        ).fetchall()
        active_seqs = [seq for seq, _, _ in active]

        tag_ops: list[tuple[int, str, str]] = []
        if active_seqs:
            placeholders = ",".join("?" * len(active_seqs))
            tag_ops = self._conn.execute(
                f"SELECT batch_seq, tag, op FROM batch_tag_ops "
                f"WHERE batch_seq IN ({placeholders}) ORDER BY batch_seq",
                active_seqs,
            ).fetchall()
        tag_by_seq: dict[int, list[tuple[str, str]]] = {}
        for seq, tag, op in tag_ops:
            tag_by_seq.setdefault(seq, []).append((op, tag))

        for seq, status_value, shift in active:
            if status_value is not None:
                status = status_value
            if shift is not None and due is not None:
                due = date.fromordinal(due.toordinal() + shift)
            for op, tag in tag_by_seq.get(seq, []):
                if op == "add":
                    labels.add(tag)
                else:
                    labels.discard(tag)

        return Task(task_id, status, due, frozenset(labels))

    def _check_clock(self, now_ms: int) -> None:
        if isinstance(now_ms, bool) or not isinstance(now_ms, int):
            raise ValueError("时间必须是整数毫秒")
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'last_clock_ms'"
        ).fetchone()
        if row is not None and now_ms < int(row[0]):
            raise ValueError("操作时间不能倒退")

    def _set_last_clock(self, now_ms: int) -> None:
        self._conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('last_clock_ms', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(now_ms),),
        )
        self._conn.commit()
