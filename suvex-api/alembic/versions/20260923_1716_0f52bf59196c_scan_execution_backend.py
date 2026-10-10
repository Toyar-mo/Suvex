"""scan execution backend

给 `scans` 加六列，记录「这次扫描在**外面**是怎么跑的」。

| 列          | 对外可见 | 用途                                          |
| ----------- | -------- | --------------------------------------------- |
| `run_id`    | ✅       | strix 侧的运行标识（`strix_runs/<run_id>/`）  |
| `runner`    | ❌       | 用的哪个执行后端 —— 让历史记录能被正确解读     |
| `run_dir`   | ❌       | 产物目录（`events.jsonl`、标准输出日志）      |
| `pid`       | ❌       | 子进程 pid —— **终止与孤儿回收全靠它**        |
| `exit_code` | ❌       | 进程退出码 —— 判"完成"还是"失败"的依据         |
| `log_path`  | ❌       | stdout/stderr 的落盘位置                      |

`run_id` 对外可见是任务 3 的明确要求：「记录 strix 侧生成的 run 标识，
关联到本地的 scan 记录」—— 而"能关联"意味着人要能看见它、拿去 strix 那边对账。
其余五列是服务端内部信息：路径与 pid 对前端没用，只是多余的信息面。

## 全部可空，而且必须可空

* 还没启动的扫描（`queued`）这六列都是 NULL；
* 启动失败时 `pid` 可能已经写入而 `exit_code` 永远没有值；
* `runner` 也是可空的 —— 用"空"表示"没跑到能确定后端的那一步"，
  比默认填一个 `'strix'` 更诚实（后者会让一次从未启动的扫描
  看起来像是被 strix 跑过）。

不加 `server_default` 是有意的：给历史行填一个默认值等于**编造**
"这些扫描是用 strix 跑的"，而它们其实是在这些列存在之前创建的。

## 这一版没有加的东西

**没有加 `queued_at`。** `startedAt` 在 `queued` 时已经是受理时刻
（契约 §2.1 的说明），再加一列会是同一个信息的第二个来源，
而"哪个才是排队开始时间"会立刻变成每次查数据都要重新判断的问题。

代价如实记：一次扫描**在队列里等了多久**，事后再也查不出来。
要排查"为什么这次扫描等了 5 分钟"，只能看 `startedAt`（真实开跑时刻）
与创建时间的差 —— 而后者在这一版里没有落库。
真要这个数据，应该在调度器认领时写一条 `activity_events`，
而不是加一列（事件本来就有时间戳，而且能记下"等了多久"这个**事件**）。

Revision ID: 0f52bf59196c
Revises: 698537531e16
Create Date: 2026-09-23 17:16:46.465745+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 迁移版本标识，由 alembic 分配，不要手改
revision: str = '0f52bf59196c'
down_revision: str | None = '698537531e16'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('scans', sa.Column('run_id', sa.String(length=200), nullable=True))
    op.add_column('scans', sa.Column('runner', sa.String(length=32), nullable=True))
    op.add_column('scans', sa.Column('run_dir', sa.Text(), nullable=True))
    op.add_column('scans', sa.Column('pid', sa.Integer(), nullable=True))
    op.add_column('scans', sa.Column('exit_code', sa.Integer(), nullable=True))
    op.add_column('scans', sa.Column('log_path', sa.Text(), nullable=True))

    # 孤儿回收与调度器认领都要按状态查，而 `ix_scans_status_created_at`
    # 已经覆盖了 `status = 'queued' ORDER BY created_at` 这个形状 ——
    # 所以这里刻意不再加索引。多一个同样的索引只会让写入慢一点。


def downgrade() -> None:
    op.drop_column('scans', 'log_path')
    op.drop_column('scans', 'exit_code')
    op.drop_column('scans', 'pid')
    op.drop_column('scans', 'run_dir')
    op.drop_column('scans', 'runner')
    op.drop_column('scans', 'run_id')
