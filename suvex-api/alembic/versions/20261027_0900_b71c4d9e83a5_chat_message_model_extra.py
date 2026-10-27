"""chat: 消息加 `model_extra` —— 把「给模型看的材料」和「用户说的话」分开存（v0.112）

## 这个迁移在修什么

`engine.run_turn` 原来这么写：

    stored_content = content + _render_attachments(files, handed=handed)

也就是把附件正文 + 交接路径说明**拼进用户那句话**，然后整条落库。
而界面渲染用户消息用的就是 `content`，于是用户看到的是自己"说过"一大段
`## 附件：xxx`、`原始文件（只读）也在沙箱里，路径如下：…`。

这不是审美问题。`app/chat/engine.py` 里为同一个形状早写过判例
（`_narration_before_action` 的 docstring）：

> 内部协议出现在用户视野里，会让人无法判断这句话是模型说的还是系统拼的
> —— 复核证据时这条是致命的。

那段材料还带着附件 id、容器内路径、以及"用 binwalk / tcpdump 分析"这类
**我们写给模型的指令**。用户看到会合理地以为"这是我打的吗？我什么时候
说过这些"。

## 为什么加列，而不是在渲染时用分隔符切掉

切分隔符要在**两个**地方各切一次（API 序列化、以及 `_render_history`
反向拼回），而且用户正文里真出现那个分隔符时会切错 —— 附件正文是任意
字节的文本，什么序列都可能出现。用列表达"这是两个东西"是结构化的，
切字符串不是。

## 为什么不干脆不存、每轮重算

重算需要知道"这条消息当时带了哪些附件"，而附件文件在交接之后已经搬进
会话目录、`attachments` 没有建表记录 id 与消息的对应关系。补那张表比加
一列大得多，而且历史消息反正是要重放的 —— 存下来就是最直接的重放材料。

## 兼容性

`model_extra` 可空、默认 NULL。**NULL 与空串语义不同**：
  · `NULL`   —— 这条消息没有注入材料（绝大多数消息）；
  · `''`     —— 理论上不写这个值，写入方用 `None`。
所以读侧一律 `(row.model_extra or "")`，不做 `is NULL` 的特殊分支。

存量那几条被污染的消息**不在这里洗**。理由：正文已经落库，切回去要靠
猜"用户原话到哪儿结束"，而那段材料里有 `---` 分隔符、用户正文里也可能
有。猜错的后果是把用户的真实提问截掉一半 —— 比留着一段多余材料糟。

## 回滚

直接 drop 列。回滚后模型上下文会丢掉附件材料（因为 `_render_history`
已经改为读这一列），但**用户可见的正文不会变干净** —— 那些材料本来就
写在 `content` 里。这是单向的语义改进，回滚只还原结构。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b71c4d9e83a5"
down_revision = "f3a1c8d52e07"
branch_labels = None
depends_on = None

TABLE = "chat_messages"
COLUMN = "model_extra"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column(COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column(TABLE, COLUMN)
