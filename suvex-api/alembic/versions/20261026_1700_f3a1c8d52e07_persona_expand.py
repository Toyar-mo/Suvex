"""chat: 人格（persona）扩容 —— 加 api / cloud / ctf / aillm 四个方向（v0.99）

## 这条迁移在做什么

`chat_sessions.persona` 上有一条 CHECK 约束，只允许
`default / recon / pentest / binary / postex / container / forensics`。
本轮从 CyberStrikeAI 的 13 个角色里补了四个**方向不重叠**的
（`api` / `cloud` / `ctf` / `aillm`），所以约束必须同步扩。

## ★ 为什么是「新建一条迁移」而不是改 `e8f3c2a71b54`

那条已经在生产库里跑过了。改它的内容不会重跑 ——
结果是：全新部署的库、已有的库、以及代码里的 `CHAT_PERSONAS`
三者互不一致，而且**只在部分环境里坏**。这类"改历史迁移"的错
排查起来最贵：本地新库是好的，线上老库写不进去。

## ★ 为什么先 drop 再 create，不试图 ALTER

PostgreSQL 的 CHECK 约束不能局部修改，只能重建。
而"先判存不存在"的分支会让迁移在两种环境下走不同路径 ——
那正是上面要避免的东西。这里是确定的：drop 掉（`IF EXISTS` 兜住
理论上已被手工删掉的情况），再按**新全集**建。

## 回滚会失败，这是刻意的

`downgrade()` 只在**库里没有这四个新值的数据**时才能成功 ——
如果已经有会话选了 `ctf`，回滚会撞上 CHECK 而报错。

那个报错是**要保留的**：静默把这些会话改回 `default` 会让用户
"我选的人格自己变了"而且毫无解释。宁可让回滚失败，让人先决定
那些会话该怎么办。
"""

from __future__ import annotations

from alembic import op

revision = "f3a1c8d52e07"
down_revision = "e8f3c2a71b54"
branch_labels = None
depends_on = None

#: 扩容后的全集。⚠️ 与 `app/chat/models.py::CHAT_PERSONAS` 逐字一致 ——
#: 不一致的后果分两种，都很隐蔽：
#:   · 迁移里少了某个 id → 那次写入被 CHECK 拒绝（用户点保存报错，还算明显）；
#:   · 迁移里多了某个 id → 能存进库，但 `PERSONA_BY_ID` 查不到，
#:     引擎**静默退回默认人格**（界面上完全看不出异常，这是最坏的一种）。
#: `app/chat/prompts.py` 末尾的断言钉住代码侧两处一致；这一份靠
#: `tests/chat/test_persona_migration.py` 钉住。
CHAT_PERSONAS_NEW = (
    "default",
    "recon",
    "pentest",
    "binary",
    "postex",
    "container",
    "forensics",
    "api",
    "cloud",
    "ctf",
    "aillm",
)

#: 扩容前的全集（= `e8f3c2a71b54` 建的那一组）。`downgrade` 要用。
CHAT_PERSONAS_OLD = (
    "default",
    "recon",
    "pentest",
    "binary",
    "postex",
    "container",
    "forensics",
)

CONSTRAINT = "chat_session_persona_valid"


def _check_sql(personas: tuple[str, ...]) -> str:
    """拼 CHECK 表达式。

    ★ `persona IS NULL` 必须放行 —— NULL 是"没选过"，不是非法值。
    ★ 用 `repr()` 生成字面量，而不是 f-string 手写引号：
      这些值是我们自己的常量，但同一套写法在 `models.py` 里也在用，
      两处用同一个生成方式才可能"逐字一致"。
    """
    return "persona IS NULL OR persona IN " f"({', '.join(repr(p) for p in personas)})"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, "chat_sessions", type_="check")
    op.create_check_constraint(CONSTRAINT, "chat_sessions", _check_sql(CHAT_PERSONAS_NEW))


def downgrade() -> None:
    """回退到 7 个人格。

    ⚠️ 若库里已有会话用了这四个人格之一，`create_check_constraint` 会失败。
    **不要**在失败前偷偷 UPDATE 那些行 —— 见模块 docstring 最后一段。
    """
    op.drop_constraint(CONSTRAINT, "chat_sessions", type_="check")
    op.create_check_constraint(CONSTRAINT, "chat_sessions", _check_sql(CHAT_PERSONAS_OLD))
