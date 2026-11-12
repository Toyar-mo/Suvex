"""Agent 配置服务（v1.83）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.artex import ROLES as ARTEX_ROLES
from app.agents.models import AgentProfile
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.timeutil import now_utc

#: 兼容旧的引用点：persona / workflow 两组的 (key, name, description)。
PERSONAS: tuple[tuple[str, str, str], ...] = tuple(
    (role.key, role.name, role.description) for role in ARTEX_ROLES if role.kind == "persona"
)
BUILTINS: tuple[tuple[str, str, str], ...] = tuple(
    (role.key, role.name, role.description) for role in ARTEX_ROLES if role.kind != "persona"
)

#: 角色模板（“选一个角色”对话框的选项）。
ROLE_TEMPLATES: tuple[dict[str, str], ...] = tuple(
    {"key": role.key, "name": role.name, "description": role.description}
    for role in ARTEX_ROLES
)


def allowed_tools_for_persona(db: Session, persona_id: str | None) -> set[str] | None:
    """该人格绑定的聊天工具子集；None = 不限制。

    绑定为空 = 不限制（兼容旧行为）；配置了但全是未知名字 = 空集
    （模型将没有任何内置工具，这是显式配置的后果）。
    """
    if not persona_id:
        return None
    row = db.execute(
        select(AgentProfile).where(AgentProfile.key == persona_id)
    ).scalar_one_or_none()
    if row is None:
        return None
    configured = [str(item).strip() for item in (row.tools or []) if str(item).strip()]
    if not configured:
        return None

    from app.chat.prompts import CHAT_TOOL_NAMES

    return {item for item in configured if item in CHAT_TOOL_NAMES}


def persona_catalog(db: Session) -> list[dict[str, str]]:
    """聊天页角色列表：DB 提供名称/描述，代码提供 prompt guidance。"""
    ensure_builtins(db)
    rows = {row.key: row for row in list_profiles(db)}

    from app.chat.prompts import CHAT_PERSONAS_SPEC

    return [
        {
            "id": spec.id,
            "label": rows[spec.id].name if spec.id in rows else spec.label,
            "summary": rows[spec.id].description if spec.id in rows else spec.summary,
        }
        for spec in CHAT_PERSONAS_SPEC
    ]


def list_templates() -> list[dict[str, str]]:
    return [dict(item) for item in ROLE_TEMPLATES]


def list_profiles(db: Session) -> list[AgentProfile]:
    return list(
        db.execute(
            select(AgentProfile).order_by(
                AgentProfile.builtin.desc(), AgentProfile.created_at.asc()
            )
        ).scalars()
    )


def get_profile(db: Session, profile_id: str) -> AgentProfile | None:
    return db.get(AgentProfile, profile_id)


def get_by_key(db: Session, key: str) -> AgentProfile:
    row = db.execute(select(AgentProfile).where(AgentProfile.key == key)).scalar_one_or_none()
    if row is None:
        raise ApiError.not_found(f"没有 key 为 `{key}` 的 Agent")
    return row


def ensure_builtins(db: Session) -> list[AgentProfile]:
    """幂等播种内置角色；已存在的 key 不覆盖用户对描述/启用的修改。"""
    rows = list(db.execute(select(AgentProfile)).scalars())
    valid = {role.key for role in ARTEX_ROLES}
    # ARTeX 角色替换旧的领域人格：删掉不再存在的内置行，用户自建行不动。
    for row in rows:
        if row.builtin and row.key not in valid:
            db.delete(row)
    existing = {row.key for row in rows if row.key in valid}
    changed = False
    for key, name, description in BUILTINS + PERSONAS:
        if key in existing:
            continue
        db.add(
            AgentProfile(
                id=new_id("agp"),
                key=key,
                name=name,
                description=description,
                builtin=True,
                kind="persona" if key in {item[0] for item in PERSONAS} else "workflow",
                enabled=True,
                mcp_servers=[],
                skills=[],
                tools=[],
            )
        )
        changed = True
    if changed:
        db.commit()
    return list_profiles(db)


def create(
    db: Session,
    *,
    key: str,
    name: str,
    description: str = "",
    mcp_servers: list[str] | None = None,
    skills: list[str] | None = None,
    tools: list[str] | None = None,
) -> AgentProfile:
    key = (key or "").strip()
    name = (name or "").strip()
    if not key or not name:
        raise ApiError.validation("key 与 name 不能为空")
    if db.execute(select(AgentProfile).where(AgentProfile.key == key)).scalar_one_or_none():
        raise ApiError.validation("这个 key 已存在")
    row = AgentProfile(
        id=new_id("agp"),
        key=key[:80],
        name=name[:120],
        description=(description or "").strip(),
        builtin=False,
        enabled=True,
        mcp_servers=list(mcp_servers or []),
        skills=list(skills or []),
        tools=list(tools or []),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update(db: Session, profile_id: str, **fields) -> AgentProfile:
    row = db.get(AgentProfile, profile_id)
    if row is None:
        raise ApiError.not_found("Agent 不存在")
    if row.builtin:
        raise ApiError.validation("内置 Agent 不可修改；请复制为自定义")
    for name in ("name", "description", "enabled", "mcp_servers", "skills", "tools"):
        if name in fields:
            setattr(row, name, fields[name])
    row.updated_at = now_utc()
    db.commit()
    db.refresh(row)
    return row


def delete(db: Session, profile_id: str) -> bool:
    row = db.get(AgentProfile, profile_id)
    if row is None:
        return False
    if row.builtin:
        raise ApiError.validation("内置 Agent 不可删除")
    db.delete(row)
    db.commit()
    return True
