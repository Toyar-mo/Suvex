"""scans.flag —— CTF 夺旗的结果单独一列（任务 18）

Revision ID: d8e3a1f60c47
Revises: c4d1e8b70a52
Create Date: 2026-09-27 13:10:00.000000+00:00

## 为什么要有这一列

一次 `captureFlag` 扫描跑完，flag 此前只活在两个地方：报告文件里的一个代码块，
以及 `finish_scan` 事件 `detail` 的 JSON 里 —— 而那条事件的 `summary` 是被
`_clip` 截断的 JSON 原文，**flag 正好落在截断点之后**。

于是实测撞到：任务跑完、agent 明确说"flag 已验证"，而用户在界面上看不到它。
用户原话是「任务结束了，没看到 flag」。

## 为什么不是塞进 `findings`

★ **flag 不是漏洞。** 那次扫描 `findings` 是空的 —— 而这是**正确的**：
一道逆向题本来就没有"存在什么问题"可言。硬塞一条 critical 会让
「漏洞发现」页出现一条不是漏洞的记录，而那一页的语义是"发现了什么缺陷"。

flag 是**这次任务达成的判据**，不是被发现的缺陷。两个维度，不合并
（与 `finding.verification` / `finding.status` 同一类判断）。

## 可空、无默认值、不回填

`NULL` 表示"没跑夺旗，或抽取不唯一"（见 `scans/flag.py`：宁可留空也不猜）。
存量行全部留 NULL 是**正确**的 —— 它们当初就没把 flag 抽出来，
迁移不该替它们编一个。已经跑完的那些要补，得用抽取器重新跑一遍
（运维动作，见部署说明），而不是在这里猜。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "d8e3a1f60c47"
down_revision: str | None = "c4d1e8b70a52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("scans", sa.Column("flag", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("scans", "flag")
