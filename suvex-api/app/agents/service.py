"""Agent 配置服务（v1.83）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.models import AgentProfile
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.timeutil import now_utc

#: 内置角色（名称/描述）——只用于展示与后续编排，不自动改变执行器行为。
PERSONAS: tuple[tuple[str, str, str], ...] = (
    ("default", "通用助手", "不偏向任何方向，按你说的做。"),
    ("recon", "信息收集", "先把面摸清：资产、端口、指纹、可见的服务。"),
    ("pentest", "渗透测试", "按攻击路径推进：入口 → 提权 → 影响。"),
    ("binary", "二进制分析", "逆向、漏洞点定位、利用原语。"),
    ("postex", "后渗透", "拿到立足点之后：横向、持久化、凭据。"),
    ("container", "容器安全", "镜像、编排、逃逸面、供应链。"),
    ("forensics", "数字取证", "还原发生了什么：时间线、截图、证据链。"),
    ("api", "API 安全", "接口契约里哪里缺了检查：鉴权、越权、注入、批量赋值。"),
    ("cloud", "云与控制面", "元数据、IAM、编排 RBAC —— 身份能走多远。"),
    ("ctf", "CTF 解题", "先判题型、先拿稳的分、时间盒。"),
    ("aillm", "AI / LLM 应用", "提示注入、工具滥用、RAG 投毒、系统提示词提取。"),
)

BUILTINS: tuple[tuple[str, str, str], ...] = (
    ("goals", "目标拆解", "把渗透任务目标拆解成若干独立、可验证的子目标。"),
    ("planner", "规划", "读取态势、判定目标，只在确有未覆盖的新方向时补充探索意图。"),
    ("mainagent", "主", "人机接口：观察进展，把人的意图落成 hint 或更高优先级意图。"),
    ("worker", "执行", "领取一条意图执行，把发现的事实/漏洞写回知识图谱后停止。"),
    ("worker.intranet", "执行 · 内网", "内网阶段变体：有立足点/隧道时替代 worker 正文（需执行器接入）。"),
    ("auto", "Auto", "平台操作助手：管理任务、资产、技能、工具与 MCP。"),
    ("reporter", "报告撰写", "根据发现与执行过程撰写并回写 Markdown 报告。"),
    ("retester", "漏洞复测", "对已修复漏洞复测，保留独立复测结论。"),
    ("verifier", "漏洞验证", "上报后自动复核 PoC 证据并保存独立验证结论。"),
)


#: 角色模板（“选一个角色”对话框的选项）。只用于预填新建表单，
#: 不自动改变执行器行为。
ROLE_TEMPLATES: tuple[dict[str, str], ...] = (
    {"key": "general", "name": "通用助手", "description": "不偏向任何方向，按你说的做。"},
    {"key": "recon", "name": "信息收集", "description": "先把面摸清：资产、端口、指纹、可见的服务。"},
    {"key": "pentest", "name": "渗透测试", "description": "按攻击路径推进：入口→授权→影响。"},
    {"key": "reverse", "name": "二进制分析", "description": "逆向、漏洞点定位、利用原语。"},
    {"key": "postexp", "name": "后渗透", "description": "拿到立足点之后：横向、持久化、凭据。"},
    {"key": "container", "name": "容器安全", "description": "镜像、编排、逃逸面、供应链。"},
    {"key": "forensics", "name": "数字取证", "description": "还原发生了什么：时间线、截图、证据链。"},
    {"key": "api", "name": "API 安全", "description": "接口契约里哪里缺了检查：鉴权、越权、注入、批量赋值。"},
    {"key": "cloud", "name": "云与控制面", "description": "元数据、IAM、编排 RBAC —— 身份能走多远。"},
    {"key": "ctf", "name": "CTF 解题", "description": "先判题型、先拿稳的分、时间盒。"},
    {"key": "ai", "name": "AI / LLM 应用", "description": "提示注入、工具滥用、RAG 投毒、系统提示词提取。"},
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
    existing = {row.key for row in db.execute(select(AgentProfile)).scalars()}
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
