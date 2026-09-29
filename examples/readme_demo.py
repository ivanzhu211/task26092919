"""Minimal end-to-end demo of the Deskflow batch library.

Run from the repository root:
    python3 examples/readme_demo.py
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

from deskflow import BatchService, Change, Task, TaskStatus


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="deskflow-demo-"))
    db_path = tmp / "deskflow.db"

    svc = BatchService.open(db_path)
    svc.initialize(
        [
            Task(
                id="t1",
                status=TaskStatus.PENDING,
                due_date=date(2026, 10, 1),
                labels=frozenset({"desk"}),
            )
        ],
        now_ms=1_760_000_000_000,
    )

    batch = svc.submit(
        target_ids=["t1"],
        change=Change(
            status=TaskStatus.COMPLETED,
            shift_days=3,
            add_labels=frozenset({"urgent"}),
            remove_labels=frozenset({"desk"}),
        ),
        now_ms=1_760_000_001_000,
    )
    print("after submit:", svc.get_task("t1"))

    svc.undo(batch.id, now_ms=1_760_000_002_000)
    print("after undo:  ", svc.get_task("t1"))
    svc.close()


if __name__ == "__main__":
    main()
