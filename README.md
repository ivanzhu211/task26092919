# Deskflow 批量任务编辑与撤销库

纯 Python 标准库实现（`sqlite3`），无接口层，由上层代码直接调用。
数据持久化在本地 SQLite 文件；测试可用内存库。

## 能力

- **初始化任务** `initialize_tasks`：写入任务初始状态（标识、状态、截止日、标签集合），只能执行一次。
- **提交批量改动** `commit_batch`：一个批次对多个不同任务应用同一组改动，全部目标任务原子生效，返回带唯一标识和确定提交顺序（`seq`）的批次。
- **按批次撤销** `undo_batch`：可按任意顺序撤销；窗口为提交后严格小于 300 秒，恰满 300 秒即不可撤销；已撤销批次重复撤销幂等成功且不受期限限制；撤销本身不可撤销。
- **读取当前任务** `get_task` / `get_tasks`：当前状态 = 初始状态按 `seq` 顺序重放所有未撤销批次。

## 改动语义

- `status`：非 None 即覆盖当前状态（设成当前值也保留意图）。
- `due_date_shift_days`：整数天，可负；在“此前日期”上累加；按无时区日历日计算（`date` 序数加减）；任务没有截止日时保持 `None`；显式传 `0` 是有效意图。
- `add_labels` / `remove_labels`：只影响指定标签，其他标签不动；同一批次不允许对同一标签既加又删；对已有标签再次添加、对不存在标签移除均保留意图。
- `status` / `due_date_shift_days` 传 `None`，标签集合不传或为空，表示该维度无改动；批次至少要包含一种改动。
- 撤销被后续编辑遮蔽的批次当下可能无可见变化，但其编辑意图会从重放序列中永久移除；其他批次的日期平移与其他标签编辑不受影响。
- 过期撤销抛 `UndoWindowExpiredError`，不改变任何任务；批次效果保留。
- 时间由调用方提供（整数毫秒，Unix 纪元），库校验不倒退；持久化最新时间。
- 无任务删除操作。

## 快速开始

```python
from datetime import date
from deskflow import DeskflowStore, UndoWindowExpiredError

store = DeskflowStore("deskflow.db")           # 或 ":memory:"
store.initialize_tasks([
    ("t-1", "todo", date(2026, 10, 1), {"red"}),
    ("t-2", "todo", None, set()),
])

b1 = store.commit_batch(
    ["t-1", "t-2"],
    status="doing",
    due_date_shift_days=2,
    add_labels={"urgent"},
    remove_labels={"red"},
    now_ms=1_727_000_000_000,
)

store.get_task("t-1")
# Task(task_id='t-1', status='doing', due_date=date(2026,10,3),
#      labels=frozenset({'urgent'}))

store.undo_batch(b1.batch_id, now_ms=1_727_000_000_000 + 60_000) # 窗口内
# 300_000ms 及之后再撤销会抛 UndoWindowExpiredError，且任务不变

store.close()   # 正常关闭；重开同一文件后状态、批次顺序、撤销资格一致
```

## 异常

- `ValueError`：参数非法、重复初始化、时钟倒退等。
- `KeyError`：读取不存在的任务。
- `BatchNotFoundError`：批次标识不存在。
- `UndoWindowExpiredError`：未撤销批次已满 300 秒。

## 存储模型

- `tasks`：任务初始状态。
- `batches`：批次头（自增 `seq` 决定提交顺序、提交时间、状态/日期维度改动、撤销标记与时间）。
- `batch_targets`：批次 → 目标任务。
- `batch_tag_ops`：批次 → 标签添加/移除操作。
- 当前状态不物化，读取时重放未撤销批次，因此乱序撤销天然正确，重启后语义不变。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：题目指定的两个场景、遮蔽撤销与意图保留、标签/日期隔离、300 秒边界
（299999ms 可撤 / 300000ms 不可撤）、重复撤销幂等、原子性、时钟不倒退、
无截止日平移、关闭重开持久化等，共 21 个用例。
