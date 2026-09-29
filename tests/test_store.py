"""DeskflowStore 自动化测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date

from deskflow import (
    BatchNotFoundError,
    DeskflowStore,
    Task,
    UndoWindowExpiredError,
)


def fresh_store(path: str | None = None) -> DeskflowStore:
    store = DeskflowStore(path or ":memory:")
    store.initialize_tasks(
        [
            ("t1", "todo", date(2026, 10, 1), {"red"}),
            ("t2", "todo", date(2026, 10, 10), {"red", "blue"}),
            ("t3", "todo", None, set()),
        ]
    )
    return store


class InitializationTests(unittest.TestCase):
    def test_initial_state_visible(self) -> None:
        store = fresh_store()
        self.assertEqual(
            store.get_task("t1"),
            Task("t1", "todo", date(2026, 10, 1), frozenset({"red"})),
        )
        tasks = store.get_tasks()
        self.assertEqual([t.task_id for t in tasks], ["t1", "t2", "t3"])
        self.assertIsNone(store.get_task("t3").due_date)

    def test_initialize_once(self) -> None:
        store = fresh_store()
        with self.assertRaises(ValueError):
            store.initialize_tasks([("t9", "todo", None, set())])

    def test_unknown_task(self) -> None:
        store = fresh_store()
        with self.assertRaises(KeyError):
            store.get_task("nope")

    def test_invalid_args(self) -> None:
        store = DeskflowStore(":memory:")
        with self.assertRaises(ValueError):
            store.initialize_tasks([("a", "todo", None, set()), ("a", "x", None, set())])
        with self.assertRaises(ValueError):
            store.commit_batch([], now_ms=0)
        store.initialize_tasks([("a", "todo", None, set())])
        with self.assertRaises(ValueError):
            store.commit_batch(["a", "a"], now_ms=0)
        with self.assertRaises(ValueError):
            store.commit_batch(["ghost"], now_ms=0)
        with self.assertRaises(ValueError):
            store.commit_batch(["a"], add_labels={"x"}, remove_labels={"x"}, now_ms=0)
        with self.assertRaises(ValueError):
            store.commit_batch(["a"], now_ms=0)
        with self.assertRaises(ValueError):
            store.commit_batch(["a"], due_date_shift_days=1.5, now_ms=0)


class CommitAndReplayTests(unittest.TestCase):
    def test_combined_changes(self) -> None:
        store = fresh_store()
        store.commit_batch(
            ["t1"],
            status="doing",
            due_date_shift_days=3,
            add_labels={"green"},
            remove_labels={"red"},
            now_ms=0,
        )
        self.assertEqual(
            store.get_task("t1"),
            Task("t1", "doing", date(2026, 10, 4), frozenset({"green"})),
        )

    def test_negative_shift_and_other_tags_untouched(self) -> None:
        store = fresh_store()
        store.commit_batch(["t2"], due_date_shift_days=-4, remove_labels={"red"}, now_ms=0)
        snapshot = store.get_task("t2")
        self.assertEqual(snapshot.due_date, date(2026, 10, 6))
        self.assertEqual(snapshot.labels, frozenset({"blue"}))

    def test_batch_applies_to_all_targets_atomically(self) -> None:
        store = fresh_store()
        batch = store.commit_batch(
            ["t1", "t2"], status="done", add_labels={"z"}, now_ms=0
        )
        self.assertEqual(len(batch.target_task_ids), 2)
        for task_id in ("t1", "t2"):
            self.assertEqual(store.get_task(task_id).status, "done")
            self.assertIn("z", store.get_task(task_id).labels)

    def test_intent_kept_when_no_visible_change(self) -> None:
        store = fresh_store()
        # 状态设成当前值、重复添加已有标签、移除不存在的标签：全部保留意图。
        store.commit_batch(
            ["t1"],
            status="todo",
            add_labels={"red"},
            remove_labels={"ghost"},
            now_ms=0,
        )
        self.assertEqual(
            store.get_task("t1"),
            Task("t1", "todo", date(2026, 10, 1), frozenset({"red"})),
        )

    def test_shift_without_due_date_stays_none(self) -> None:
        store = fresh_store()
        store.commit_batch(["t3"], status="doing", due_date_shift_days=10, now_ms=0)
        self.assertIsNone(store.get_task("t3").due_date)

    def test_deterministic_order_and_ids(self) -> None:
        store = fresh_store()
        b1 = store.commit_batch(["t1"], status="a", now_ms=1)
        b2 = store.commit_batch(["t1"], status="b", now_ms=2)
        self.assertEqual([b.batch_id for b in store.list_batches()], [b1.batch_id, b2.batch_id])
        self.assertEqual((b1.seq, b2.seq), (1, 2))
        self.assertNotEqual(b1.batch_id, b2.batch_id)


class UndoTests(unittest.TestCase):
    def test_simple_undo_restores_initial(self) -> None:
        store = fresh_store()
        batch = store.commit_batch(["t1"], status="done", now_ms=0)
        store.undo_batch(batch.batch_id, now_ms=1)
        self.assertEqual(
            store.get_task("t1"),
            Task("t1", "todo", date(2026, 10, 1), frozenset({"red"})),
        )
        self.assertTrue(store.get_batch(batch.batch_id).undone)

    def test_undo_only_target_tags_and_days(self) -> None:
        store = fresh_store()
        b1 = store.commit_batch(["t2"], due_date_shift_days=2, add_labels={"x"}, now_ms=0)
        b2 = store.commit_batch(["t2"], due_date_shift_days=5, remove_labels={"blue"}, now_ms=1000)
        store.undo_batch(b1.batch_id, now_ms=2000)
        snapshot = store.get_task("t2")
        self.assertEqual(snapshot.due_date, date(2026, 10, 15))
        self.assertEqual(snapshot.labels, frozenset({"red"}))
        store.undo_batch(b2.batch_id, now_ms=3000)
        self.assertEqual(
            store.get_task("t2"),
            Task("t2", "todo", date(2026, 10, 10), frozenset({"red", "blue"})),
        )

    def test_expired_undo_changes_nothing(self) -> None:
        store = fresh_store()
        batch = store.commit_batch(["t1"], status="done", now_ms=0)
        with self.assertRaises(UndoWindowExpiredError):
            store.undo_batch(batch.batch_id, now_ms=300_000)
        self.assertFalse(store.get_batch(batch.batch_id).undone)
        self.assertEqual(store.get_task("t1").status, "done")
        with self.assertRaises(UndoWindowExpiredError):
            store.undo_batch(batch.batch_id, now_ms=299_999 + 1)
        store.undo_batch(batch.batch_id, now_ms=299_999)

    def test_expired_effect_retained_but_window_closed(self) -> None:
        store = fresh_store()
        batch = store.commit_batch(["t1"], due_date_shift_days=1, now_ms=10_000)
        with self.assertRaises(UndoWindowExpiredError):
            store.undo_batch(batch.batch_id, now_ms=10_000 + 300_000)
        self.assertEqual(store.get_task("t1").due_date, date(2026, 10, 2))

    def test_double_undo_is_idempotent_forever(self) -> None:
        store = fresh_store()
        batch = store.commit_batch(["t1"], status="done", now_ms=0)
        store.undo_batch(batch.batch_id, now_ms=1)
        store.undo_batch(batch.batch_id, now_ms=10**12)
        self.assertTrue(store.get_batch(batch.batch_id).undone)

    def test_undo_unknown_batch(self) -> None:
        store = fresh_store()
        with self.assertRaises(BatchNotFoundError):
            store.undo_batch("missing", now_ms=0)

    def test_clock_must_not_go_backwards(self) -> None:
        store = fresh_store()
        store.commit_batch(["t1"], status="a", now_ms=10_000)
        with self.assertRaises(ValueError):
            store.commit_batch(["t1"], status="b", now_ms=9_999)
        batch = store.commit_batch(["t1"], status="b", now_ms=10_001)
        with self.assertRaises(ValueError):
            store.undo_batch(batch.batch_id, now_ms=10_000)

    def test_required_scenario_two_status_overrides(self) -> None:
        """三个批次连续覆盖状态：先撤中间、再撤最后，显现第一个批次状态。"""
        store = fresh_store()
        first = store.commit_batch(["t1"], status="s1", now_ms=0)
        middle = store.commit_batch(["t1"], status="s2", now_ms=1_000)
        last = store.commit_batch(["t1"], status="s3", now_ms=2_000)
        self.assertEqual(store.get_task("t1").status, "s3")

        store.undo_batch(middle.batch_id, now_ms=3_000)
        # 中间批次被最后批次遮蔽，撤销当下无可见变化，但其意图已永久移除。
        self.assertEqual(store.get_task("t1").status, "s3")

        store.undo_batch(last.batch_id, now_ms=4_000)
        self.assertEqual(store.get_task("t1").status, "s1")

        store.undo_batch(first.batch_id, now_ms=5_000)
        self.assertEqual(store.get_task("t1").status, "todo")


class RequiredScenarioOneTests(unittest.TestCase):
    """题目指定场景一：两批次同标签/同状态 + 各自日期平移。"""

    def test_full_flow(self) -> None:
        store = fresh_store()
        earlier = store.commit_batch(
            ["t1"],
            status="doing",
            due_date_shift_days=2,
            add_labels={"shared"},
            now_ms=0,
        )
        later = store.commit_batch(
            ["t1"],
            status="doing",
            due_date_shift_days=3,
            add_labels={"shared"},
            now_ms=1_000,
        )
        self.assertEqual(
            store.get_task("t1"),
            Task(
                "t1",
                "doing",
                date(2026, 10, 6),
                frozenset({"red", "shared"}),
            ),
        )

        store.undo_batch(earlier.batch_id, now_ms=2_000)
        self.assertEqual(
            store.get_task("t1"),
            Task(
                "t1",
                "doing",
                date(2026, 10, 4),
                frozenset({"red", "shared"}),
            ),
        )

        store.undo_batch(later.batch_id, now_ms=3_000)
        self.assertEqual(
            store.get_task("t1"),
            Task("t1", "todo", date(2026, 10, 1), frozenset({"red"})),
        )

    def test_out_of_order_undo_with_tag_remove_intent(self) -> None:
        """后续批次移除标签，先撤后续批次，再撤添加批次：标签保持移除后不存在。"""
        store = fresh_store()
        add = store.commit_batch(["t2"], add_labels={"new"}, now_ms=0)
        remove = store.commit_batch(["t2"], remove_labels={"new"}, now_ms=1_000)
        self.assertNotIn("new", store.get_task("t2").labels)

        store.undo_batch(remove.batch_id, now_ms=2_000)
        self.assertIn("new", store.get_task("t2").labels)

        store.undo_batch(add.batch_id, now_ms=3_000)
        self.assertNotIn("new", store.get_task("t2").labels)
        # 其他标签未被连带抵消。
        self.assertEqual(store.get_task("t2").labels, frozenset({"red", "blue"}))


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "deskflow.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_state_order_and_undo_eligibility_survive_reopen(self) -> None:
        store = DeskflowStore(self.path)
        store.initialize_tasks([("t1", "todo", date(2026, 1, 1), {"a"})])
        b1 = store.commit_batch(["t1"], status="x", due_date_shift_days=2, now_ms=1_000)
        b2 = store.commit_batch(
            ["t1"], status="y", due_date_shift_days=-1, add_labels={"b"}, now_ms=2_000
        )
        store.undo_batch(b1.batch_id, now_ms=3_000)
        store.close()

        reopened = DeskflowStore(self.path)
        self.assertEqual(
            reopened.get_task("t1"),
            Task("t1", "y", date(2025, 12, 31), frozenset({"a", "b"})),
        )
        batches = reopened.list_batches()
        self.assertEqual([b.seq for b in batches], [1, 2])
        self.assertTrue(reopened.get_batch(b1.batch_id).undone)
        self.assertFalse(reopened.get_batch(b2.batch_id).undone)

        # b2 提交于 2000ms；恰满 300 秒的判定在重开后仍然成立。
        with self.assertRaises(UndoWindowExpiredError):
            reopened.undo_batch(b2.batch_id, now_ms=302_000)
        # 已撤销批次重开后再次撤销仍然幂等成功。
        reopened.undo_batch(b1.batch_id, now_ms=10**12)
        reopened.close()


if __name__ == "__main__":
    unittest.main()
