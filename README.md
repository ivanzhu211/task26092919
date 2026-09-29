# Deskflow 批量任务编辑与撤销库

供上层直接调用的 Python 库：初始化任务、提交批量改动、按批次选择性撤销、
读取当前任务。无接口层，持久化仅使用本地 SQLite 文件（Python 标准库，
无第三方运行时依赖）。

## 安装与运行环境

- Python ≥ 3.10（开发验证使用 3.14），无需任何数据库、缓存或网络服务。
- 直接使用仓库：在仓库根目录运行代码即可（`deskflow/` 是普通包）。
- 或在有权限的环境中安装：`pip install -e .`（隔离安装需要网络拉取
  setuptools；离线环境可加 `--no-build-isolation`）。

## 数据模型

- `Task(id, status, due_date, labels)`
  - `status`：`TaskStatus` 枚举（`pending` / `in_progress` / `completed` /
    `cancelled`）。
  - `due_date`：`datetime.date` 或 `None`，按**无时区日历日**计算。
  - `labels`：`frozenset[str]`。
- `Change(status=None, shift_days=0, add_labels=frozenset(), remove_labels=frozenset())`
  - 四类改动可任意组合，但同一批次内同一标签不能既添加又移除；批次至少
    包含一项改动。
  - `shift_days` 为整数天，在每个任务当前截止日上累加（可为负）。任务
    截止日为 `None` 时平移不产生日期。
- `Batch`：提交后返回，含全局唯一 `id`、确定的提交顺序 `seq`、目标任务、
  改动内容、提交时间和撤销状态。

## 核心语义

- 一个批次把同一组 `Change` 应用到全部目标任务；提交与撤销对全部目标
  原子生效（任一目标不存在则整批拒绝，任何任务都不变）。
- 当前状态 = 初始快照 + 按 `seq` 顺序重放所有**未撤销**批次：
  - 状态取最后一个设置状态的批次（覆盖）。
  - 截止日 = 初始日期 + 所有未撤销批次平移量之和。
  - 标签按顺序执行 add/remove，只影响被指定的标签。
- 编辑意图总是落库，即使提交时可见值未变（重复 add 已有标签、remove
  不存在标签、状态设为当前值）。撤销更早批次后，这些意图仍按提交顺序
  生效；被撤销的编辑不会因撤销其他批次而复活。
- 撤销可乱序。撤销一个已被后续编辑遮蔽的批次时，当下可能没有可见变化，
  但之后撤销后续批次时它不会重新出现；其他批次对日期和其他标签的改动
  不受影响。
- 撤销窗口：自提交起 300,000 毫秒（300 秒），`now_ms - committed_at_ms
  >= 300000` 即不可撤销，抛 `UndoWindowExpiredError`，批次效果保留且
  任何任务都不改变。已撤销批次重复撤销视为成功，不受期限限制。撤销
  本身不可撤销。
- 时间由调用方以整数毫秒提供并保证不倒退；库不读取系统时钟。
- 关闭并重新打开同一数据库文件后，任务状态、批次顺序与撤销资格一致。

## API

```python
from datetime import date
from deskflow import BatchService, Change, Task, TaskStatus

svc = BatchService.open("deskflow.db")   # 自动建表；可重复打开

svc.initialize([                          # 每个数据库仅一次，重复抛
    Task("t1", TaskStatus.PENDING, date(2026, 10, 1), frozenset({"desk"})),
], now_ms=1_760_000_000_000)

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

svc.get_task("t1")        # -> Task，按未撤销批次重放后的当前状态
svc.list_tasks()          # -> list[Task]，按 id 排序
svc.get_batch(batch.id)   # -> Batch 元数据（含 undone/undone_at_ms）

svc.undo(batch.id, now_ms=1_760_000_002_000)  # 窗口内撤销；重复撤销幂等
svc.close()                          # 提交事务并关闭连接
# 也支持 with BatchService.open(...) as svc: ...
```

异常均继承 `DeskflowError`：

- `InvalidChangeError`：空改动、空目标、标签冲突、重复初始化的任务等。
- `TaskNotFoundError` / `BatchNotFoundError`：标识不存在。
- `AlreadyInitializedError`：重复初始化。
- `UndoWindowExpiredError`：超过撤销窗口（过期撤销不改任何任务）。

## 测试

```bash
python3 -m pytest tests/ -q
```

覆盖（共 11 个）：

- 需求场景一：两批次同标签/同状态/不同平移，先撤销前一个再撤销后一个。
- 需求场景二：三个批次连续覆盖状态，先撤中间再撤最后，显现第一个状态。
- 意图保留（重复 add、remove 缺失标签、状态设为当前值）。
- 遮蔽撤销与乱序撤销不复活；不相关标签与平移不被连带抵消。
- 撤销窗口 299.999s/恰 300s 边界、重复撤销幂等且不受期限限制。
- 批次对多目标原子生效；过期撤销不改任何任务。
- 关闭重开后状态、顺序与撤销资格保持。

可运行示例：`PYTHONPATH=. python3 examples/readme_demo.py`

## 项目结构

```
deskflow/
  __init__.py    # 公共导出
  models.py      # Task / Change / Batch / TaskStatus
  errors.py      # 异常类型
  store.py       # SQLite 建表与追加式批次/撤销台账
  service.py     # BatchService：四类能力 + 提交顺序重放
tests/           # pytest 自动化测试
examples/        # 最小可运行示例
```

实现采用事件溯源式重放：批次只追加，撤销仅置位，不删除历史，
因此乱序撤销与持久化恢复都等价于“按顺序过滤未撤销批次后重放”。
