"""Required end-to-end scenarios from the specification."""

from __future__ import annotations

from datetime import date

from deskflow import Change, TaskStatus


def test_same_label_status_and_daily_shifts_undo_in_order(service):
    """Two batches: same label, same status, different shifts.

    Undo the first batch: the later status/label intent survives and only
    the later shift remains. Undo the second: back to the initial state.
    """
    initial = service.get_task("t1")
    assert initial.status is TaskStatus.PENDING
    assert initial.due_date == date(2026, 10, 1)
    assert initial.labels == frozenset({"base"})

    b1 = service.submit(
        ["t1"],
        Change(
            status=TaskStatus.COMPLETED,
            shift_days=1,
            add_labels=frozenset({"x"}),
        ),
        now_ms=1_000,
    )
    b2 = service.submit(
        ["t1"],
        Change(
            status=TaskStatus.COMPLETED,
            shift_days=2,
            add_labels=frozenset({"x"}),
        ),
        now_ms=2_000,
    )
    assert b1.seq == 1 and b2.seq == 2

    current = service.get_task("t1")
    assert current.status is TaskStatus.COMPLETED
    assert current.labels == frozenset({"base", "x"})
    assert current.due_date == date(2026, 10, 4)

    service.undo(b1.id, now_ms=3_000)
    current = service.get_task("t1")
    assert current.status is TaskStatus.COMPLETED
    assert current.labels == frozenset({"base", "x"})
    assert current.due_date == date(2026, 10, 3)

    service.undo(b2.id, now_ms=4_000)
    current = service.get_task("t1")
    assert current.status is TaskStatus.PENDING
    assert current.labels == frozenset({"base"})
    assert current.due_date == date(2026, 10, 1)


def test_three_status_overrides_undo_middle_then_last(service):
    """Status overrides replay by order; an undone middle never revives."""
    b1 = service.submit(
        ["t1"], Change(status=TaskStatus.IN_PROGRESS), now_ms=1_000
    )
    b2 = service.submit(
        ["t1"], Change(status=TaskStatus.COMPLETED), now_ms=2_000
    )
    b3 = service.submit(
        ["t1"], Change(status=TaskStatus.CANCELLED), now_ms=3_000
    )

    assert service.get_task("t1").status is TaskStatus.CANCELLED

    service.undo(b2.id, now_ms=4_000)
    assert service.get_task("t1").status is TaskStatus.CANCELLED

    service.undo(b3.id, now_ms=5_000)
    assert service.get_task("t1").status is TaskStatus.IN_PROGRESS

    # The first batch is still active; out-of-order undos never revived b2.
    assert service.get_batch(b2.id).undone is True
