"""Invariants: intent retention, shadowing, window, atomicity, persistence."""

from __future__ import annotations

from datetime import date

import pytest

from deskflow import (
    BatchService,
    Change,
    InvalidChangeError,
    Task,
    TaskNotFoundError,
    TaskStatus,
    UndoWindowExpiredError,
)


def test_redo_add_and_remove_intents_are_kept(service):
    """Re-adding an existing label and removing a missing label still count."""
    b1 = service.submit(
        ["t1"],
        Change(
            add_labels=frozenset({"base"}),
            remove_labels=frozenset({"missing"}),
        ),
        now_ms=1_000,
    )
    # No visible change at commit time.
    assert service.get_task("t1").labels == frozenset({"base"})

    # An intermediate batch makes the recorded intents meaningful.
    b2 = service.submit(
        ["t1"],
        Change(
            add_labels=frozenset({"missing"}),
            remove_labels=frozenset({"base"}),
        ),
        now_ms=2_000,
    )
    assert service.get_task("t1").labels == frozenset({"missing"})

    # Undo b2: b1 is still active, but its intent applied before b2 only.
    service.undo(b2.id, now_ms=3_000)
    assert service.get_task("t1").labels == frozenset({"base"})

    # Undo b1: state returns to the initial snapshot.
    service.undo(b1.id, now_ms=4_000)
    assert service.get_task("t1").labels == frozenset({"base"})


def test_setting_status_to_current_value_still_records_intent(service):
    b1 = service.submit(
        ["t1"], Change(status=TaskStatus.PENDING), now_ms=1_000
    )
    b2 = service.submit(
        ["t1"], Change(status=TaskStatus.COMPLETED), now_ms=2_000
    )
    assert service.get_task("t1").status is TaskStatus.COMPLETED

    service.undo(b2.id, now_ms=3_000)
    # b1 set PENDING (the then-current value) but its intent survives.
    assert service.get_task("t1").status is TaskStatus.PENDING

    service.undo(b1.id, now_ms=4_000)
    assert service.get_task("t1").status is TaskStatus.PENDING


def test_undo_shadowed_then_later_batch_keeps_undone(service):
    """Undo an older, shadowed edit first; it must not return later."""
    b1 = service.submit(
        ["t1"],
        Change(shift_days=1, add_labels=frozenset({"a"})),
        now_ms=1_000,
    )
    b2 = service.submit(
        ["t1"],
        Change(shift_days=10, add_labels=frozenset({"a", "b"})),
        now_ms=2_000,
    )

    service.undo(b1.id, now_ms=3_000)
    current = service.get_task("t1")
    assert current.due_date == date(2026, 10, 11)
    assert current.labels == frozenset({"base", "a", "b"})

    service.undo(b2.id, now_ms=4_000)
    current = service.get_task("t1")
    assert current.due_date == date(2026, 10, 1)
    assert current.labels == frozenset({"base"})


def test_unrelated_labels_and_shifts_are_not_undone(service):
    b1 = service.submit(
        ["t1"], Change(shift_days=5, add_labels=frozenset({"a"})),
        now_ms=1_000,
    )
    b2 = service.submit(
        ["t1"], Change(shift_days=7, add_labels=frozenset({"b"})),
        now_ms=2_000,
    )
    service.undo(b2.id, now_ms=3_000)
    current = service.get_task("t1")
    assert current.due_date == date(2026, 10, 6)
    assert current.labels == frozenset({"base", "a"})
    assert b1.id != b2.id


def test_window_boundary_and_idempotent_undo(service):
    committed_at = 10_000
    b1 = service.submit(
        ["t1"], Change(status=TaskStatus.COMPLETED), now_ms=committed_at
    )

    # 299.999s: still undoable.
    service.undo(b1.id, now_ms=committed_at + 299_999)
    assert service.get_task("t1").status is TaskStatus.PENDING

    # Re-undoing an undone batch always succeeds, even far past the window.
    again = service.undo(b1.id, now_ms=committed_at + 10_000_000)
    assert again.undone is True

    # A fresh batch at exactly 300s is not undoable and keeps its effect.
    b2 = service.submit(
        ["t1"], Change(status=TaskStatus.CANCELLED), now_ms=committed_at
    )
    with pytest.raises(UndoWindowExpiredError):
        service.undo(b2.id, now_ms=committed_at + 300_000)
    assert service.get_task("t1").status is TaskStatus.CANCELLED


def test_batch_applies_to_all_targets_atomically(service):
    # One unknown target rejects the whole batch: nothing changes.
    with pytest.raises(TaskNotFoundError):
        service.submit(
            ["t1", "ghost"],
            Change(status=TaskStatus.COMPLETED),
            now_ms=1_000,
        )
    assert service.get_task("t1").status is TaskStatus.PENDING

    b1 = service.submit(
        ["t1", "t2"],
        Change(status=TaskStatus.COMPLETED, shift_days=-2,
               add_labels=frozenset({"shared"}),
               remove_labels=frozenset({"base"})),
        now_ms=2_000,
    )
    assert service.get_task("t1").status is TaskStatus.COMPLETED
    assert service.get_task("t2").status is TaskStatus.COMPLETED
    assert service.get_task("t1").due_date == date(2026, 9, 29)
    assert service.get_task("t2").due_date == date(2026, 10, 30)
    assert service.get_task("t1").labels == frozenset({"shared"})
    assert service.get_task("t2").labels == frozenset({"shared"})

    # Expired undo changes neither task.
    with pytest.raises(UndoWindowExpiredError):
        service.undo(b1.id, now_ms=2_000 + 300_000)
    assert service.get_task("t1").status is TaskStatus.COMPLETED
    assert service.get_task("t2").status is TaskStatus.COMPLETED


def test_state_survives_reopen(tmp_path):
    db_path = tmp_path / "deskflow.db"
    svc = BatchService.open(db_path)
    svc.initialize(
        [Task("t1", TaskStatus.PENDING, date(2026, 10, 1), frozenset())],
        now_ms=0,
    )
    b1 = svc.submit(
        ["t1"],
        Change(status=TaskStatus.IN_PROGRESS, shift_days=3,
               add_labels=frozenset({"a"})),
        now_ms=1_000,
    )
    b2 = svc.submit(
        ["t1"], Change(status=TaskStatus.COMPLETED, shift_days=2),
        now_ms=2_000,
    )
    svc.undo(b1.id, now_ms=3_000)
    svc.close()

    reopened = BatchService.open(db_path)
    task = reopened.get_task("t1")
    assert task.status is TaskStatus.COMPLETED
    assert task.due_date == date(2026, 10, 3)
    assert task.labels == frozenset()  # b1's label intent was undone

    # Commit order and undo eligibility persist.
    assert reopened.get_batch(b1.id).undone is True
    assert reopened.get_batch(b2.id).seq == 2
    with pytest.raises(UndoWindowExpiredError):
        reopened.undo(b2.id, now_ms=2_000 + 300_000)
    reopened.close()


def test_invalid_changes_are_rejected(service):
    with pytest.raises(InvalidChangeError):
        service.submit(["t1"], Change(), now_ms=1_000)
    with pytest.raises(InvalidChangeError):
        service.submit([], Change(status=TaskStatus.COMPLETED), now_ms=1_000)
    with pytest.raises(InvalidChangeError):
        service.submit(
            ["t1"],
            Change(
                add_labels=frozenset({"x"}),
                remove_labels=frozenset({"x"}),
            ),
            now_ms=1_000,
        )


def test_list_tasks_returns_all(service):
    ids = {task.id for task in service.list_tasks()}
    assert ids == {"t1", "t2"}


def test_never_touch_deleted_or_uninitialized_reads(tmp_path):
    """Reading an unknown task fails; tasks are never removed by the API."""
    svc = BatchService.open(tmp_path / "fresh.db")
    svc.initialize(
        [Task("t1", TaskStatus.PENDING, None, frozenset())], now_ms=0
    )
    with pytest.raises(TaskNotFoundError):
        svc.get_task("nope")
    # Undoing every batch leaves every initialized task readable.
    b1 = svc.submit(
        ["t1"], Change(status=TaskStatus.COMPLETED), now_ms=1_000
    )
    svc.undo(b1.id, now_ms=2_000)
    assert {t.id for t in svc.list_tasks()} == {"t1"}
    svc.close()
