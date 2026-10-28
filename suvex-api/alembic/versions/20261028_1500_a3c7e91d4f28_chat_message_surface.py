"""chat: `chat_messages.surface` —— 区分「对话内容」与「过程证据」（v1.25）

## 这个迁移在加什么

一列 `surface`：
- `answer`（默认）：用户可见的对话内容 —— 用户说的话、模型的最终答复。
- `process`：过程证据 —— 「我先把附件解出来」这类边干边说的旁白、工具结果。

## 为什么要分开

用户原话：「又中断了，就不能一次性把 flag 给我解出来吗」，并贴出截图：
一串小气泡夹在工具行之间（「我先看看附件里是什么。」「解出来的文件叫 pwn…」
「这是 CTF 的 pwn 二进制，我拆开看逻辑。」）。

那是**工作过程**，不是回答。它把真正的结论挤出视野，而且任何一句被截断
都长得像"它停在半路"。CyberStrikeAI 的做法（`docs/zh-CN/
agent-finalization-best-practices.md`）叫「过程面 / 交付面隔离」：只有过
final gate 的内容才写 `messages.content`，其余进 `process_details`。

## 兼容性

`server_default='answer'`：存量行（含用户自己说的话、历史最终答复）全部
是 `answer` —— 历史里的旁白会被当答案显示，但这不影响正确性，且**不猜测**
哪条曾经是旁白（猜错就把用户的话藏起来了）。

Revision ID: a3c7e91d4f28
Revises: f7a2d5c91b36
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3c7e91d4f28"
down_revision = "f7a2d5c91b36"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column(
            "surface", sa.String(length=16), nullable=False, server_default="answer"
        ),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "surface")
