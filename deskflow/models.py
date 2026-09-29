"""Domain types for Deskflow batch task editing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import AbstractSet, Optional


class TaskStatus(str, Enum):
    """Lifecycle state of a task."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Task:
    """A task as seen by callers: identity, status, due date, labels."""

    id: str
    status: TaskStatus
    due_date: Optional[date]
    labels: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Change:
    """One group of edits applied by a batch to every target task."""

    status: Optional[TaskStatus] = None
    shift_days: int = 0
    add_labels: frozenset[str] = field(default_factory=frozenset)
    remove_labels: frozenset[str] = field(default_factory=frozenset)

    def is_empty(self) -> bool:
        """True when the change applies no edit at all."""
        return (
            self.status is None
            and self.shift_days == 0
            and not self.add_labels
            and not self.remove_labels
        )


@dataclass(frozen=True)
class Batch:
    """Metadata of a committed batch."""

    id: int
    seq: int
    target_ids: tuple[str, ...]
    change: Change
    committed_at_ms: int
    undone: bool
    undone_at_ms: Optional[int]
