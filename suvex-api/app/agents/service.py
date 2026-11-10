"""Agent 配置服务（v1.83）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.models import AgentProfile
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.timeutil import now_utc

#: 内置角色（名称/描述）——只用于展示与后续编排，不自动改变执行器行为。
BUILTINS: tuple[tuple[str, str, str], ...] = (
    ("goals", "目标拆解", "把渗透任务目标拆解成若干独立、可验证的子目标。"),
    ("planner", "规划", "读取态势、判定目标，只在确有未覆盖的新方向时补充探索意图。"),
    ("mainagent", "主", "人机接口：观察进展，把人的意图落成 hint 或更高优先级意图。"),
    ("worker", "执行", "领取一条意图执行，把发现的事实/漏洞写回知识图谱后停止。"),
    ("worker.intranet", "执行 · 内网", "内网阶段变体：有立足点/隧道时替代 worker 正文（需执行器接入）。"),
    ("auto", "Auto", "平台操作助手：管理任务、资产、技能、工具与 MCP。"),
    ("pentest", "渗透测试", "独立渗透：侦察→找攻击面→深入利用→验证→收尾，形成完整链条。"),
    ("reporter", "报告撰写", "根据发现与执行过程撰写并回写 Markdown 报告。"),
    ("retester", "漏洞复测", "对已修复漏洞复测，保留独立复测结论。"),
    ("verifier", "漏洞验证", "上报后自动复核 PoC 证据并保存独立验证结论。"),
)


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
    existing = {row.key for row in db.execute(select(AgentProfile)).scalars()}
    changed = False
    for key, name, description in BUILTINS:
        if key in existing:
            continue
        db.add(
            AgentProfile(
                id=new_id("agp"),
                key=key,
                name=name,
                description=description,
                builtin=True,
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
