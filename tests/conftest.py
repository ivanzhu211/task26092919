"""Shared pytest fixtures."""

from __future__ import annotations

from datetime import date

import pytest

from deskflow import BatchService, Task, TaskStatus


@pytest.fixture()
def service(tmp_path):
    db_path = tmp_path / "deskflow.db"
    svc = BatchService.open(db_path)
    svc.initialize(
        [
            Task(
                id="t1",
                status=TaskStatus.PENDING,
                due_date=date(2026, 10, 1),
                labels=frozenset({"base"}),
            ),
            Task(
                id="t2",
                status=TaskStatus.IN_PROGRESS,
                due_date=date(2026, 11, 1),
                labels=frozenset(),
            ),
        ],
        now_ms=0,
    )
    yield svc
    svc.close()
