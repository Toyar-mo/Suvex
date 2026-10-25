"""对话引擎（v0.93）：一轮对话 = 调模型 → 执行工具 → 再调模型，直到模型不再调工具。

## 它复用了解题引擎的什么

`suvex-api/app/kernel/runner.py` 那套 `ACTION: {"tool": .., "args": {..}}` 文本协议
（`:3935` `parse_action` / `:3945` `parse_actions` / `:4185` 的 `_iter_json_blocks`），
以及 `:1825` 的 `LlmReply` / `:1836` 的 `LlmGateway.chat`。

**不**复用的是 `dispatch_tool`（`:3760`）—— 那个函数签名要求 `SolveSpec` +
`SandboxContainer` + `httpx.AsyncClient`，而对话页**没有沙箱**。对话工具是
平台操作（建扫描、改技能、加 MCP），它们走的是 `service` / `skill_store` /
`mcp` 这些**已有的业务层**，不经过沙箱。见 `prompts.py` 的模块 docstring。

## ★ 为什么不用 OpenAI function-calling

两条理由，第二条是决定性的：

1. **平台要支持任意 provider**（`ProviderAccount.kind` 有 cloud / local，
   base_url 可改）。function-calling 的 wire 形状在 OpenAI 与 Anthropic 之间
   不同，各家兼容层对它的支持程度也不同 —— 引入它等于给"接入点"加一层
   能力矩阵，而现在的 `LlmGateway` 只发 `messages` + `system`，任何
   provider 都能用。

2. **解题引擎已经用文本协议跑了 45 道真题**（`parse_actions` 的注释里
   写着 v0.42 的实测：271 轮 / 14.2% 一轮写多个 ACTION）。对话页若换一套
   协议，就是本项目反复踩的"同一件事两个实现" —— 而这次连**解析器**
   都不同，将来两边对"什么是合法动作"的判断会分叉。

代价是模型偶尔会写坏 JSON。这个代价解题引擎已经在付，答案也一致：
`parse_actions` 的容错是"跳过坏的、继续好的"，不是整轮作废。

## ★ 一轮最多几轮工具调用

`MAX_TOOL_ROUNDS = 6`。为什么不是无限：每一轮是一次真实的 LLM 调用
（金钱 + 延迟），而"模型陷在工具循环里出不来"是一个**真实的**失败模式
（解题侧见过：连续 40 轮重复同一条命令）。6 轮足够覆盖"查一下 → 建一个 →
确认结果"这种正常链路，超了就停并把已做的告诉用户。

⚠️ 停的方式是**明确告诉模型"到上限了，现在总结"**，而不是静默截断 ——
静默截断会让用户看到一条没有结论的回复，而不知道为什么。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.chat import service as chat_service
from app.chat.models import ChatSession
from app.chat.prompts import CHAT_TOOL_NAMES, build_system_prompt

_log = logging.getLogger(__name__)

#: 一轮用户消息里最多做几轮"模型→工具→模型"。
MAX_TOOL_ROUNDS = 6

#: 喂给模型的上下文里，最多带多少条历史消息。
#:
#: 为什么不是"全部"：一次长对话可能有几百条，而每条工具输出都可能有几万字
#: （虽然落库时截到 60000）。全带上会让第 50 轮的一次调用就撑爆上下文。
#:
#: 取 30 的理由：正常对话里 30 条 ≈ 10 轮问答，够模型理解"刚才在聊什么"。
#: 更早的内容对当前这一轮几乎无用（用户真需要时会重述）。
HISTORY_MESSAGE_LIMIT = 30

#: 上下文里单条工具输出最多带多少字。
#:
#: 与落库口径（`MAX_MESSAGE_CHARS` = 60000）不同，这里更严：**落库是留档，
#: 喂模型是花钱**。一条 6 万字的 nmap 输出塞进上下文，代价是后面每一轮
#: 都要重新付这 6 万 token，而模型真正需要的通常只是前几百行。
HISTORY_TOOL_CHARS = 4000


class ChatEngineError(Exception):
    """引擎层的失败。router 会翻成 5xx/4xx。

    ⚠️ 与 `KernelError` 分开：那个类的文案是给**解题**场景写的
    （"这道题"、"沙箱"），直接抛到对话里会牛头不对马嘴。
    """


@dataclass
class ChatTurnResult:
    """一轮对话的产出。`messages` 是本轮新落库的全部消息（按 seq 升序）。"""

    messages: list[Any] = field(default_factory=list)
    #: 这一轮是否因为达到工具轮数上限而提前收尾。
    hit_tool_limit: bool = False


# ══════════════════════════════════════════════════════════
# 上下文拼装
# ══════════════════════════════════════════════════════════


def _render_history(rows: list[Any]) -> list[dict[str, str]]:
    """把库里的消息渲染成 LLM 的 `messages` 数组。

    ## 三种角色怎么映射

    | 库里的 role | 送到模型时 |
    |---|---|
    | `user` | `user` |
    | `assistant` | `assistant` |
    | `tool` | **`user`**（内容前面加一段"工具 X 的结果"） |

    ★ 工具结果为什么伪装成 `user`：OpenAI / Anthropic 的 wire 格式里
      `role: "tool"` 需要配 `tool_call_id`，而我们的工具调用是**文本协议**
      （`ACTION: {...}` 就写在 assistant 的正文里），没有 id 可配。
      发一个没有 id 的 `tool` 角色，多数 provider 会 400。

      用一个显式的「工具结果」前缀 + `user` 角色，模型完全能理解 ——
      这也是解题引擎的做法（`runner.py` 里把工具输出拼回 user 消息）。

    ## ★ 为什么 reasoning 不进上下文

    `reasoning`（`reasoning_content` / Anthropic thinking）是**展示物**。
    把它塞回下一轮的 `messages` 有两个问题：
    1. 多数 provider 明确拒绝把 reasoning 当输入（Anthropic 会报错）；
    2. 它会显著增长上下文，而那些推理对下一轮几乎无用
       （上一轮的"我打算这么做"在拿到结果之后就作废了）。

    所以它落库、给界面看，但不回喂。
    """
    out: list[dict[str, str]] = []
    for row in rows:
        role = row.role
        content = row.content or ""
        if role == "tool":
            name = row.tool_name or "未知工具"
            body = content
            if len(body) > HISTORY_TOOL_CHARS:
                body = (
                    body[:HISTORY_TOOL_CHARS]
                    + f"\n\n…（另有 {len(content) - HISTORY_TOOL_CHARS} 字未展开）"
                )
            failed = "（失败）" if row.tool_failed else ""
            args = ""
            if row.tool_args:
                try:
                    args = json.dumps(row.tool_args, ensure_ascii=False)
                except (TypeError, ValueError):
                    args = "{}"
                if len(args) > 500:
                    args = args[:500] + "…"
            out.append(
                {
                    "role": "user",
                    "content": (
                        f"[工具结果]{failed} `{name}` 参数 {args}\n"
                        f"--------\n{body}"
                    ),
                }
            )
        elif role == "assistant":
            # 空正文的 assistant 消息（模型只调了工具、没说话）不送 ——
            # 一个空的 assistant turn 在部分 provider 上是 400。
            if content.strip():
                out.append({"role": "assistant", "content": content})
        else:
            out.append({"role": "user", "content": content})
    return out


# ══════════════════════════════════════════════════════════
# 工具执行
# ══════════════════════════════════════════════════════════


class _ToolContext:
    """执行工具需要的一切。打包成一个对象，免得每个 _tool_* 都收七个参数。"""

    __slots__ = ("db", "session", "identity")

    def __init__(self, *, db: Session, session: ChatSession, identity: Any) -> None:
        self.db = db
        self.session = session
        self.identity = identity


def _need_scope(ctx: _ToolContext, scope: str) -> None:
    """工具级权限复核。

    ## 为什么在引擎里再判一次（router 已经判过了）

    router 判的是"**这个端点**要不要 admin"（`/chat` 本身只要登录）。
    而对话里的**某一次工具调用**可能需要更高的权限 —— 用户可能只是
    `read` scope，却让模型去改技能库。到了 `save_skill` 这一层才发现
    没权限，比在 router 层拒绝更准确，也才能给模型一句它能理解的话。

    ⚠️ 不用 `require_admin` 那个 FastAPI 依赖：它抛 `ApiError`，
      而引擎里的失败要以**工具结果**的形式回到模型（让它知道"这条路
      走不通，换个说法"），不是让整个请求 403。
    """
    if ctx.identity is None:
        raise _ToolDenied("这次对话没有可用的身份信息，不能执行需要权限的操作。")
    can = getattr(ctx.identity, "can", None)
    if can is None or not can(scope):
        raise _ToolDenied(
            f"当前账号没有 `{scope}` 权限，做不了这件事。"
            "需要的话请让管理员给你相应的权限。"
        )


class _ToolDenied(Exception):
    """工具因权限不足被拒。会被 engine 翻成一条**失败的工具结果**。"""


def _err(message: str) -> str:
    """把失败渲染成工具结果文本。

    口径与 `runner.dispatch_tool` 一致：**返回一句人话，不抛异常**。
    抛异常会中断整轮，而"模型填错了一个参数"不该让用户的提问失败。
    """
    return f"失败：{message}"


async def _tool_list_scans(ctx: _ToolContext, args: dict[str, Any]) -> str:
    from app.scans import service as scans_service

    limit = _int_arg(args, "limit", default=10, low=1, high=50)
    rows = scans_service.list_scans(ctx.db, sort="recent")[:limit]
    if not rows:
        return "平台里还没有扫描任务。"
    lines = [f"最近 {len(rows)} 个扫描任务："]
    for scan in rows:
        # ⚠️ 不读 `scan.finding_count` —— `Scan` 表上没有这一列。结论数在
        #    `findings` 表里，而为列表页的一个数字去 COUNT 每条是 N+1。
        #    这里只报扫描自身有的字段；用户要看结论就点进去。
        lines.append(
            f"- `{scan.id}` {scan.target}（{scan.target_kind}）状态={scan.status}"
        )
    return "\n".join(lines)


async def _tool_create_scan(ctx: _ToolContext, args: dict[str, Any]) -> str:
    """建扫描。

    ## ★ 为什么不自己拼 ScanCreate 然后落库

    走 `scans_service.create_scan` 那一条完整路径，因为它带着三件这里
    绝不能绕过的东西：

    1. **授权留痕**（`check_authorization`）—— 建扫描会对真实目标发包；
    2. **目标归一化与校验**（`targets.normalize_and_validate`）；
    3. **provider 存在性校验**（`_require_provider`）。

    绕过它们就是放弃这些保护，而那正是本项目「配了却静默失效」那类
    缺陷的来源（设计文档 §2.2 写明了这条决策）。

    ## 授权声明怎么填

    用户在**对话里明确要求**建扫描，这就是一次授权动作。所以这里用
    `settings.scan_authorization_statement` 的原话填进去 —— 但**不是**
    "顺手勾一个默认值"：`create_scan` 的 docstring 说那句话是防止前端
    默认勾选，而对话场景下用户的原话（"帮我扫 192.168.1.5"）本身就是
    明确指令。留痕里记的是 `authorized_by=当前用户` + 服务端时刻，可复核。

    ⚠️ 但这意味着**模型不能替用户决定扫什么**。提示词里"有副作用的操作
       先确认"那一条正是为此。引擎这边能做的只有：目标为空/不像目标时
       直接拒，让模型回去问清楚。
    """
    from app.core.config import get_settings
    from app.scans import service as scans_service
    from app.scans.schemas import AuthorizationIn, ScanCreate

    _need_scope(ctx, "scan")

    target = str(args.get("target") or "").strip()
    if not target:
        return _err("没有给 `target`。我需要知道你让我扫什么 —— 地址、仓库还是路径？")

    kind = str(args.get("targetKind") or args.get("target_kind") or "url").strip()
    if kind not in {"url", "repo", "path", "service"}:
        return _err(
            f"`targetKind` 只能是 url / repo / path / service，收到的是 `{kind}`。"
            "（`artifact` 不行：那要附件，而对话里传不了文件。）"
        )

    skills = args.get("skills")
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(",") if s.strip()]
    if not isinstance(skills, list) or not skills:
        # 默认 web：绝大多数对话场景是"扫一个网站"。写在这里而不是让模型
        # 必须记住，是因为漏填 skills 时 ScanCreate 会 400，而那条报错
        # 对模型来说不够可行动（它不知道"该填哪一个"）。
        skills = ["web"]

    goal = str(args.get("goal") or "findVulns").strip()
    if goal not in {"findVulns", "captureFlag", "verifyFix"}:
        goal = "findVulns"

    provider_id = _default_provider_id(ctx.db)
    if provider_id is None:
        return _err("平台上还没有配置任何模型接入点，建不了扫描。请先在设置里加一个。")

    brief = str(args.get("brief") or "").strip() or None
    if brief and len(brief) > 8000:
        return _err(f"`brief` 太长（{len(brief)} 字，上限 8000）。请压缩一下再试。")

    payload = ScanCreate(
        name="",
        target=target,
        targetKind=kind,
        skills=skills,
        providerId=provider_id,
        brief=brief,
        goal=goal,
        authorization=AuthorizationIn(
            confirmed=True,
            statement=get_settings().scan_authorization_statement,
        ),
    )
    try:
        scan, reach = scans_service.create_scan(
            ctx.db,
            payload,
            identity=ctx.identity,
            # ★ 对话页拿不到真实 IP（它是服务端进程里的内部调用）。
            # 记 `chat` 而不是编一个 127.0.0.1 —— 留痕的意义是"能回答
            # 这是谁从哪授权的"，编一个假 IP 比承认拿不到更糟。
            ip="chat",
            user_agent="suvex-chat",
        )
    except Exception as exc:  # noqa: BLE001 - 任何业务拒绝都要变成可读的工具结果
        return _err(f"建扫描被拒：{exc}")

    ctx.db.commit()
    notice = getattr(reach, "notice", None)
    extra = f"\n（目标预检：{notice}）" if notice else ""
    return (
        f"已创建扫描任务 `{scan.id}`，目标 {scan.target}（{scan.target_kind}），"
        f"技能 {'、'.join(skills)}，判据 {goal}。它已排队，会在后台跑。{extra}"
    )


async def _tool_list_challenges(ctx: _ToolContext, args: dict[str, Any]) -> str:
    from app.kernel import service as kernel_service

    limit = _int_arg(args, "limit", default=10, low=1, high=50)
    status = str(args.get("status") or "").strip() or None
    rows = kernel_service.list_challenges(ctx.db, status=status)[:limit]
    if not rows:
        return "库里没有符合条件的题目。"
    lines = [f"共 {len(rows)} 道题："]
    for challenge in rows:
        lines.append(
            f"- `{challenge.id}` {challenge.title or '(无标题)'}"
            f" 状态={challenge.status}"
        )
    return "\n".join(lines)


async def _tool_start_ctf_solve(ctx: _ToolContext, args: dict[str, Any]) -> str:
    from app.kernel import service as kernel_service

    _need_scope(ctx, "scan")
    challenge_id = str(args.get("challengeId") or args.get("challenge_id") or "").strip()
    if not challenge_id:
        return _err("没有给 `challengeId`。先用 `list_challenges` 找到题目 id。")
    challenge = ctx.db.get(kernel_service.KernelChallenge, challenge_id)
    if challenge is None:
        return _err(f"没有 id 为 `{challenge_id}` 的题目。")

    mode = str(args.get("mode") or "").strip() or None
    if mode is not None and mode not in {"safe", "auto"}:
        return _err(f"`mode` 只能是 safe 或 auto，收到 `{mode}`。")
    brief = str(args.get("brief") or "").strip() or None

    try:
        kernel_service.start_solve(ctx.db, challenge, mode=mode, brief=brief)
    except Exception as exc:  # noqa: BLE001
        return _err(f"受理失败：{exc}")
    return (
        f"已受理题目 `{challenge.id}`（{challenge.title or '无标题'}）。"
        "它在后台跑，过程去「解题」页看。"
    )


async def _tool_list_skills(ctx: _ToolContext, args: dict[str, Any]) -> str:
    from app.kernel import skills as skills_mod

    category = str(args.get("category") or "").strip().lower()
    rows = skills_mod.discover_skills()
    if category:
        rows = [s for s in rows if s.category.lower() == category]
    if not rows:
        return "没有符合条件的技能包。"
    lines = [f"共 {len(rows)} 篇："]
    by_category: dict[str, list[Any]] = {}
    for skill in rows:
        by_category.setdefault(skill.category, []).append(skill)
    for cat in sorted(by_category):
        lines.append(f"\n## {cat}")
        for skill in sorted(by_category[cat], key=lambda s: s.name):
            tag = "内置" if skill.source == "builtin" else "自定义"
            files = len(skill.files) if skill.files else 0
            extra = f"，{files} 个附件" if files else ""
            lines.append(f"- `{skill.name}`（{tag}{extra}）：{skill.description}")
    return "\n".join(lines)


async def _tool_read_skill(ctx: _ToolContext, args: dict[str, Any]) -> str:
    from app.kernel import skills as skills_mod

    name = str(args.get("name") or "").strip()
    if not name:
        return _err("没有给 `name`。")
    return skills_mod.load_skill_text(name)


async def _tool_save_skill(ctx: _ToolContext, args: dict[str, Any]) -> str:
    from app.kernel import skill_store

    _need_scope(ctx, "admin")
    name = str(args.get("name") or "").strip()
    category = str(args.get("category") or "").strip().lower()
    description = str(args.get("description") or "").strip()
    body = str(args.get("body") or "")
    missing = [
        key
        for key, value in (
            ("name", name),
            ("category", category),
            ("description", description),
            ("body", body),
        )
        if not value
    ]
    if missing:
        return _err(f"缺了必填参数：{'、'.join(missing)}。")

    try:
        path = skill_store.create_or_update_skill(
            name=name,
            category=category,
            description=description,
            body=body,
        )
    except skill_store.SkillWriteError as exc:
        # ⚠️ 不再手动拼 `exc.detail` —— `SkillWriteError.__str__` 已经把
        #    detail 接在 message 后面了（见 skill_store.py 那段 docstring
        #    解释为什么），再拼一次会得到重复的两句话。
        return _err(str(exc))
    return f"已写入技能包 `{name}`（分类 {category}），落盘在 `{path.name}`。"


async def _tool_delete_skill(ctx: _ToolContext, args: dict[str, Any]) -> str:
    from app.kernel import skill_store

    _need_scope(ctx, "admin")
    name = str(args.get("name") or "").strip()
    category = str(args.get("category") or "").strip().lower()
    if not name or not category:
        return _err("`name` 与 `category` 都要给 —— 技能按分类存放，只给名字找不到。")
    try:
        skill_store.delete_skill(name=name, category=category)
    except skill_store.SkillWriteError as exc:
        # 同上：`str(exc)` 已含 detail。
        return _err(str(exc))
    return f"已删除技能 `{name}`（分类 {category}）。"


async def _tool_list_mcp_servers(ctx: _ToolContext, args: dict[str, Any]) -> str:
    from app.kernel import service as kernel_service

    rows = kernel_service.public_mcp_servers(ctx.db)
    if not rows:
        return "还没有配置任何 MCP 服务器。"
    lines = [f"已配置 {len(rows)} 个 MCP 服务器："]
    for item in rows:
        env_note = "，带凭据" if item.get("has_env") else ""
        tools = item.get("allowed_tools")
        allow = f"，白名单 {len(tools)} 个工具" if tools else "，不限制工具"
        lines.append(f"- `{item['name']}`：{item['command']} {' '.join(item['args'])}{env_note}{allow}")
    return "\n".join(lines)


async def _tool_add_mcp_server(ctx: _ToolContext, args: dict[str, Any]) -> str:
    """加一条 MCP 连接，并**真的试连一次**。

    ## ★ 为什么必须试连

    这是本项目反复踩的那个形状：配了却静默失效。用户填完 command 之后，
    在此之前唯一能验证的办法是"保存 → 跑一次真实任务 → 看模型能不能调"，
    而那条路上的失败是模型侧的一次工具报错。

    所以这里保存前后各做一件事：
    1. **保存前**试连（`kernel_service.test_mcp_server`）—— 拉不起来就
       不保存，直接告诉模型原因；
    2. 保存后把重名、数量上限这些由 `save_mcp_servers` 把关的错误原样转述。

    ⚠️ 顺序是"先试连、再保存"，不是反过来：保存了但连不上，会让配置库里
       留下一条坏记录，而用户看到的是"我加了但没生效"。
    """
    from app.kernel import service as kernel_service

    _need_scope(ctx, "admin")
    name = str(args.get("name") or "").strip()
    command = str(args.get("command") or "").strip()
    if not name or not command:
        return _err("`name` 与 `command` 都要给。")

    raw_args = args.get("args") or []
    if isinstance(raw_args, str):
        raw_args = [part for part in raw_args.split(" ") if part]
    if not isinstance(raw_args, list):
        return _err("`args` 应该是字符串列表。")
    arg_list = [str(a) for a in raw_args]

    env = args.get("env") or {}
    if not isinstance(env, dict):
        return _err("`env` 应该是一个对象（键值都是字符串）。")
    env_map = {str(k): str(v) for k, v in env.items()}

    allowed = args.get("allowedTools") or args.get("allowed_tools")
    if allowed is not None:
        if isinstance(allowed, str):
            allowed = [t.strip() for t in allowed.split(",") if t.strip()]
        if not isinstance(allowed, list):
            return _err("`allowedTools` 应该是字符串列表。")
        allowed = [str(t) for t in allowed]

    proposal = {
        "name": name,
        "command": command,
        "args": arg_list,
        "env": env_map,
        "notes": "由对话页添加",
        "allowed_tools": allowed,
    }

    verdict = await kernel_service.test_mcp_server(proposal)
    if not verdict.get("ok"):
        return _err(
            f"试连失败，没有保存。原因：{verdict.get('reason') or '未知'}"
        )

    tools = verdict.get("tools") or []
    existing = kernel_service.get_mcp_servers(ctx.db)
    if any(str(item.get("name") or "") == name for item in existing):
        return _err(f"已经有一个叫 `{name}` 的 MCP 服务器了。换个名字，或先删掉它。")

    try:
        kernel_service.save_mcp_servers(ctx.db, [*existing, proposal])
    except Exception as exc:  # noqa: BLE001
        return _err(f"保存被拒：{exc}")
    ctx.db.commit()

    tool_names = "、".join(str(t.get("name") or "") for t in tools[:10])
    more = f"（还有 {len(tools) - 10} 个）" if len(tools) > 10 else ""
    return (
        f"已添加 MCP 服务器 `{name}`，试连成功（{verdict.get('elapsed_ms')} ms），"
        f"它提供 {len(tools)} 个工具：{tool_names}{more}。"
        "下一次任务开始时模型就能用它们。"
    )


#: 工具名 → 实现。**这是唯一的注册表** —— `prompts.CHAT_TOOLS` 里列了但这里
#: 没有的名字，会在 `_execute_tool` 里被明确报出来（而不是静默变成"未知工具"）。
#: 反过来，这里有而清单里没有的名字，`test_chat_tools.py` 会红。
_TOOL_IMPLS: dict[str, Any] = {
    "list_scans": _tool_list_scans,
    "create_scan": _tool_create_scan,
    "list_challenges": _tool_list_challenges,
    "start_ctf_solve": _tool_start_ctf_solve,
    "list_skills": _tool_list_skills,
    "read_skill": _tool_read_skill,
    "save_skill": _tool_save_skill,
    "delete_skill": _tool_delete_skill,
    "list_mcp_servers": _tool_list_mcp_servers,
    "add_mcp_server": _tool_add_mcp_server,
}


def _narration_before_action(text: str) -> str:
    """把一轮输出里 `ACTION:` 之前的"人话"取出来。

    ## 为什么需要这个函数

    模型的一轮输出通常是这个形状：

        好的，我先查一下最近的扫描。

        ACTION: {"tool": "list_scans", "args": {}}

    我们**解析**的是整段（`parse_actions` 要看到 `ACTION:`），但**落库给
    用户看的**只能是前半段。把后半段落进 `chat_messages.content` 的后果
    是用户在对话气泡里看到一行裸 JSON —— 那是我实测撞到的：

        assistant | '好的，我先查一下最近的扫描。\\nACTION: {"tool": "list_scans", "args": {}}'

    界面上它看起来像"机器人卡住了、把内部协议漏出来了"，而实际上工具
    调用已经成功。这不是美观问题：**内部协议出现在用户视野里，会让人
    无法判断这句话是模型说的还是系统拼的** —— 复核证据时这条是致命的。

    ## 为什么不直接复用 `runner._narration_before_action`

    那一个是**解题**引擎的私有函数，语义是"喂给活动流的 reasoning"，
    而它有一个 20 字的下限：

        return head if len(head) >= 20 else ""

    那个下限对解题场景是对的（"太短的 reasoning 不值得单发一条事件"），
    但对话场景**不能丢**："好的。" 这种一句话就是模型这一轮说的全部内容，
    丢掉之后这条 assistant 消息会变成空串，界面上看起来像模型没说话。
    所以这里保留**任意长度**的非空正文。

    ⚠️ `marker >= 0` 而不是 `> 0`：模型有时一上来就写 `ACTION:`
      （没有任何铺垫），那时 `find` 返回 0 —— 用 `> 0` 会走 else 分支，
      把整段（含 `ACTION: {...}`）当成正文存下来，正是要修的那个 bug。

    ⚠️ 去掉代码围栏：模型常把 ACTION 包在 ``` 里，直接切会让正文尾部
      带一串 ```，界面上像是 Markdown 渲染坏了。
    """
    marker = text.find("ACTION:")
    head = text[:marker] if marker >= 0 else text
    return head.replace("```", "").strip()


def _int_arg(args: dict[str, Any], key: str, *, default: int, low: int, high: int) -> int:
    """取一个整数参数并夹到区间内。

    ⚠️ `low` 定为 1 而不是 0：`limit=0` 是个无意义的请求，而
       "返回零条"与"没查到"在界面上无法区分。
    """
    raw = args.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _default_provider_id(db: Session) -> str | None:
    """取默认接入点。规则与 `kernel/service._snapshot_provider_id` **必须一致**。"""
    from sqlalchemy import select

    from app.providers.models import ProviderAccount

    row = (
        db.execute(
            select(ProviderAccount).order_by(
                ProviderAccount.is_default.desc(), ProviderAccount.created_at
            )
        )
        .scalars()
        .first()
    )
    return row.id if row is not None else None


async def _execute_tool(ctx: _ToolContext, name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """执行一个工具，返回 `(结果文本, 是否失败)`。

    ★ 失败**不抛**，返回文本 + 布尔。原因见 `_err`。

    ⚠️ 每次工具调用后 `db.flush()` 但不 `commit()` —— commit 由 router 的
       `session_scope` 在请求末尾统一做。这样"工具做了一半、模型又调下一个
       工具失败了"时，整轮要么全成、要么全不成（除 `create_scan` 那条路，
       它内部自己 commit，因为扫描是**独立于对话**的实体 —— 建出来了就该
       存在，不该因为对话后面出错而被回滚）。
    """
    impl = _TOOL_IMPLS.get(name)
    if impl is None:
        return (
            f"失败：没有叫 `{name}` 的工具。可用工具：{', '.join(sorted(CHAT_TOOL_NAMES))}。",
            True,
        )
    try:
        text = await impl(ctx, args)
    except _ToolDenied as exc:
        return f"失败：{exc}", True
    except Exception as exc:  # noqa: BLE001 - 工具内部崩了不该毁掉整轮对话
        _log.warning("对话工具 %s 执行失败", name, exc_info=True)
        return f"失败：工具 `{name}` 执行时出错（{type(exc).__name__}）。", True

    ctx.db.flush()
    return text, text.startswith("失败：")


# ══════════════════════════════════════════════════════════
# 主循环
# ══════════════════════════════════════════════════════════


def _build_gateway(provider_id: str | None, db: Session):
    """按 provider 造一个 LLM 网关。

    ★ 唯一读凭据的地方是 `providers.service.decrypt_provider_key`
      （复用既有纪律："明文只在一处出现"）。
    """
    from sqlalchemy import select

    from app.kernel.runner import LlmGateway
    from app.providers.models import ProviderAccount
    from app.providers.service import decrypt_provider_key

    provider = None
    if provider_id:
        provider = db.get(ProviderAccount, provider_id)
    if provider is None:
        provider = (
            db.execute(
                select(ProviderAccount).order_by(
                    ProviderAccount.is_default.desc(), ProviderAccount.created_at
                )
            )
            .scalars()
            .first()
        )
    if provider is None:
        raise ChatEngineError(
            "平台上还没有配置模型接入点。请先在「设置 → 模型」里加一个，再回来对话。"
        )
    return LlmGateway(
        model=provider.model,
        api_key=decrypt_provider_key(provider),
        base_url=provider.base_url or "",
        label=provider.label,
        kind=provider.kind,
    )


async def run_turn(
    db: Session,
    *,
    session: ChatSession,
    content: str,
    identity: Any,
    provider_id: str | None = None,
    gateway: Any = None,
) -> ChatTurnResult:
    """跑一轮对话。

    ## 参数 `gateway`

    给测试用的注入点。传了就不造真网关（测试里不可能真调 LLM）。
    生产路径永远不传，由 `_build_gateway` 从库里造。

    ## 流程

    1. 落用户消息
    2. 拼上下文（最近 N 条） + 系统提示词
    3. 调模型 → 解析 `ACTION:`
    4. 有动作就执行、落 tool 消息、回到 3（最多 `MAX_TOOL_ROUNDS` 轮）
    5. 没有动作 = 模型说完了 → 落 assistant 消息，结束

    ⚠️ 每一轮模型的**文本正文**都落一条 assistant 消息（哪怕它同时带了
       动作）。这正是界面上"它说要做什么 → 工具卡 → 它说结果"的对话感来
       源；只保留最后一条会让中间过程消失。
    """
    from app.kernel.runner import parse_actions

    result = ChatTurnResult()

    user_message = chat_service.append_user_message(db, session=session, content=content)
    result.messages.append(user_message)
    db.commit()

    if gateway is None:
        gateway = _build_gateway(provider_id, db)

    system = build_system_prompt()
    ctx = _ToolContext(db=db, session=session, identity=identity)

    for round_index in range(MAX_TOOL_ROUNDS + 1):
        history = chat_service.list_messages(
            db, session=session, limit=HISTORY_MESSAGE_LIMIT
        )
        messages = _render_history(history)

        # 到上限那一轮，不再允许动作 —— 明确要求它收尾。
        at_limit = round_index >= MAX_TOOL_ROUNDS
        system_this_round = system
        if at_limit:
            result.hit_tool_limit = True
            system_this_round = (
                system
                + "\n\n## ⚠️ 工具调用已到本轮上限\n\n"
                "不要再写 ACTION。现在直接用一段话总结：你做了什么、"
                "拿到了什么结果、还有什么没做完。"
            )

        try:
            reply = await gateway.chat(messages, system=system_this_round)
        except Exception as exc:  # noqa: BLE001
            _log.warning("对话调模型失败", exc_info=True)
            raise ChatEngineError(f"调用模型失败：{type(exc).__name__}。") from exc

        # ★ 落库给用户看的是**剥掉动作之后**的正文，不是模型原始输出。
        #
        # 模型把 `ACTION: {...}` 和"人话"写在同一次回复里。原始的
        # `reply.text` 直接落库会让用户在气泡里看到一行裸 JSON
        # （实测形状：`好的，我先查一下。\nACTION: {"tool": "list_scans", "args": {}}`），
        # 而工具其实已经调成功了。**内部协议出现在用户视野里，会让人
        # 分不清这句话是模型说的还是系统拼的** —— 复核证据时这是致命的。
        #
        # ⚠️ 动作仍然从**原始** `reply.text` 解析：剥掉之后就找不到
        #   `ACTION:` 了。
        raw_text = reply.text or ""
        text = _narration_before_action(raw_text)
        actions = [] if at_limit else parse_actions(raw_text)
        reasoning = getattr(reply, "reasoning", None)
        if not reasoning:
            # `LlmGateway` 把推理段塞在 `raw` 里（不同 provider 键名不同），
            # 这里只取一个展示用；拿不到就不展示，绝不编。
            raw = getattr(reply, "raw", None) or {}
            if isinstance(raw, dict):
                candidate = raw.get("reasoning_content") or raw.get("thinking")
                reasoning = candidate if isinstance(candidate, str) and candidate else None

        # 模型既没说话也没动作：这是一次空转。
        #
        # ⚠️ 这里有两种**必须分开说**的情况，混在一起会让用户看到一句
        #    误导的话：
        #    a) 模型真的什么都没产出（空回复，常见于接入点异常）；
        #    b) 模型写了 ACTION，但**这一轮不允许动作**（到上限了），
        #       于是动作被丢弃 —— 它其实产出了东西，只是用不上。
        #    对 b 说"模型没有产出内容"是错的，而用户会据此去换接入点，
        #    换完还是一样。
        if not text and not actions:
            from app.kernel.runner import parse_actions as _parse

            discarded = _parse(raw_text) if at_limit else []
            # ★ 三种情况必须分开说 —— 混在一起会让用户按错误方向去修：
            #   a) 到上限了，模型还想调 X：说清"它还想调 X"，不是"没产出"；
            #   b) 什么都没写：才说"没有产出内容"并提示换接入点；
            #   c) 写了铺垫但没动作（`text` 非空）—— 根本不会走到这个分支。
            note = (
                "（模型还想继续调工具，但本轮工具轮数已到上限，"
                f"它最后想调的是 `{discarded[0][0]}`。"
                "可以让它接着做，或换个更具体的问法。）"
                if discarded
                else "（模型这次没有产出内容。可以再问一次，或换个接入点试试。）"
            )
            placeholder = chat_service.append_assistant_message(
                db, session=session, content=note
            )
            db.commit()
            result.messages.append(placeholder)
            return result

        assistant_message = chat_service.append_assistant_message(
            db, session=session, content=text, reasoning=reasoning
        )
        result.messages.append(assistant_message)
        db.commit()

        if not actions:
            return result

        for name, args in actions:
            output, failed = await _execute_tool(ctx, name, args)
            tool_message = chat_service.append_tool_message(
                db,
                session=session,
                tool_name=name,
                tool_args=args,
                content=output,
                failed=failed,
            )
            result.messages.append(tool_message)
            db.commit()

    return result


__all__ = [
    "HISTORY_MESSAGE_LIMIT",
    "HISTORY_TOOL_CHARS",
    "MAX_TOOL_ROUNDS",
    "ChatEngineError",
    "ChatTurnResult",
    "run_turn",
]
