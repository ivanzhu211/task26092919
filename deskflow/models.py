"""对外暴露的数据类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class Task:
    """一个任务在某一时刻的快照。"""

    task_id: str
    status: str
    due_date: Optional[date]
    labels: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class BatchChange:
    """一个批次对任务应用的同一组改动；None 表示该维度不改动。"""

    status: Optional[str] = None
    due_date_shift_days: Optional[int] = None
    add_labels: frozenset[str] = field(default_factory=frozenset)
    remove_labels: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Batch:
    """批次的元数据。"""

    batch_id: str
    seq: int
    committed_at_ms: int
    target_task_ids: tuple[str, ...]
    change: BatchChange
    undone: bool = False
    undone_at_ms: Optional[int] = None
