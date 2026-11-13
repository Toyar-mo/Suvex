"""对话上下文压缩（v2.0，对齐 OMP 的 compaction）。

## 它解决什么

长任务跑到几百轮后，历史里大量是工具输出。现在的 `_trim_history_to_budget`
只能**从最老的开始丢**，丢掉的可能是"已经验证过哪些方向、flag 在哪张表"
这类结论；模型下一轮会重复劳动，或者因为看不到关键证据而做出错误判断。

这里改成 OMP 的做法：把旧历史压成一条**持久化摘要**，只保留最近一段原文。
原始消息一条不删，`chat_compactions.first_kept_seq` 就是"从哪条开始还看原文"
的边界。后续每轮从摘要 + 最近消息重建上下文，不重复调用摘要模型。

## 触发

- 渲染后的上下文字符数超过 `COMPACT_TRIGGER_CHARS`（默认 70% 的
  `MAX_HISTORY_CHARS`）；
- 调用方显式 `force=True`：输出被截断且无内容、或单段墙钟到点、或上游报
  context overflow。

`compact()` 返回 `None` 表示"没有可压缩的旧消息 / 摘要调用失败"。调用方
必须把 `None` 当成"压缩没成功"，不能自动继续 —— 否则下一次调用还是同一份
超长上下文。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chat.models import ChatCompaction, ChatMessage
from app.chat.service import list_messages
from app.core.ids import new_id

logger = logging.getLogger("suvex.chat.compaction")

#: 触发压缩的字符数。与 engine 的 `MAX_HISTORY_CHARS` 保持同一量级：
#: 那个是"硬上限，再超就开始丢"，这里是"提前压，别丢"。
COMPACT_TRIGGER_CHARS = int(
    os.getenv("SUPEX_CHAT_COMPACT_TRIGGER_CHARS", "105000")
)

#: 压缩后保留最近多少字符的原文。太小会丢当前步骤，太大压不动。
COMPACT_KEEP_CHARS = 40_000

#: 送给摘要模型的旧历史最多多少字符（每轮都重发，摘要本身也要花钱）。
COMPACT_INPUT_CHARS = 80_000

#: 单条消息在摘要输入里的截断长度。
COMPACT_MESSAGE_CHARS = 4_000

#: 摘要本身的上限；超出截断，避免摘要变成新的上下文炸弹。
COMPACT_SUMMARY_CHARS = 12_000

#: 少于这么多条旧消息时不压 —— 收益抵不过一次摘要调用。
COMPACT_MIN_OLD_MESSAGES = 4

_SUMMARY_SYSTEM = """你是安全测试对话的上下文压缩器。

把下面这段较早的对话压缩成一份供后续 agent 继续工作的摘要。必须保留：
- 用户的目标、约束、授权范围；
- 已经确认的事实（目标、版本、路径、账号、flag 位置、已验证/已排除的方向）；
- 已经执行过的关键命令与结果（只保留能改变判断的部分）；
- 尚未完成的步骤和下一步。

不要编造、不要给建议、不要写客套话。用简洁的 Markdown 分节输出。"""


def latest(db: Session, *, session_id: str) -> ChatCompaction | None:
    """这个会话最近一次压缩边界。"""
    return db.scalars(
        select(ChatCompaction)
        .where(ChatCompaction.session_id == session_id)
        .order_by(ChatCompaction.created_at.desc(), ChatCompaction.id.desc())
        .limit(1)
    ).first()


def list_for_session(db: Session, *, session_id: str) -> list[ChatCompaction]:
    """按时间正序返回所有压缩边界（界面画分界线用）。"""
    return list(
        db.scalars(
            select(ChatCompaction)
            .where(ChatCompaction.session_id == session_id)
            .order_by(ChatCompaction.created_at, ChatCompaction.id)
        )
    )


def load_context(db: Session, *, session: Any) -> tuple[ChatCompaction | None, list[ChatMessage]]:
    """返回 `(最近压缩, 需要保留原文的消息)`。

    没有压缩时就是普通的历史窗口；有压缩时只取 `seq > first_kept_seq`，
    更早的消息由摘要代表。
    """
    comp = latest(db, session_id=session.id)
    if comp is None:
        return None, list_messages(db, session=session, limit=400)
    return comp, list_messages(
        # 语义是「从 first_kept_seq 这条开始保留」，而 `after_seq` 是严格
        # 大于 —— 不减 1 会把边界那条消息自己漏掉（边界若是 tool，就是
        # 刚执行完的那条结果）。
        db, session=session, after_seq=comp.first_kept_seq - 1, limit=400
    )


DEFAULT_CONTEXT_WINDOW_TOKENS = int(
    os.getenv("SUPEX_CHAT_CONTEXT_WINDOW_TOKENS", "128000")
)

#: 常见模型的上下文窗口（token）。只是兜底；未知模型用环境变量。
_MODEL_WINDOWS = (
    ("deepseek", 1_000_000),
    ("gemini", 1_000_000),
    ("claude", 200_000),
    ("qwen", 128_000),
    ("gpt-4.1", 1_000_000),
    ("gpt-4o", 128_000),
    ("gpt-3.5", 16_385),
)


def context_window_tokens(model: str | None) -> int:
    name = (model or "").lower()
    for needle, window in _MODEL_WINDOWS:
        if needle in name:
            return window
    return DEFAULT_CONTEXT_WINDOW_TOKENS


def trigger_chars(model: str | None = None, context_window: int | None = None) -> int:
    """按模型窗口取 70%，但不超过本地硬预算 `COMPACT_TRIGGER_CHARS`。

    小窗口模型（32k/16k）会因此更早压缩，避免等 105k 字符时已经被
    provider 直接拒绝；大窗口模型仍受本地成本上限约束。
    """
    window = int(context_window or context_window_tokens(model))
    return min(COMPACT_TRIGGER_CHARS, max(8_000, int(window * 0.7 * 4)))


def estimate_context_tokens(db: Session, *, session: Any) -> int:
    """估算当前会喂给模型的上下文 token 数（字符/4）。

    这是给界面显示“上下文百分比”的近似值：按压缩边界后的摘要 + 最近原文
    计算，不包含系统提示词与工具 schema。真实计费以 provider usage 为准。
    """
    comp, rows = load_context(db, session=session)
    chars = len(comp.summary) if comp is not None else 0
    chars += sum(
        len(m.content or "") + len(m.model_extra or "") for m in rows
    )
    return max(1, chars // 4)


def needs_compaction(
    rendered_chars: int,
    model: str | None = None,
    context_window: int | None = None,
) -> bool:
    return rendered_chars >= trigger_chars(model, context_window)


def _serialize(rows: list[ChatMessage]) -> str:
    parts: list[str] = []
    for row in rows:
        content = (row.content or "").strip()
        if not content:
            continue
        if row.model_extra:
            content += "\n\n" + row.model_extra
        if row.role == "tool":
            args = json.dumps(row.tool_args or {}, ensure_ascii=False, default=str)
            content = (
                f"工具：{row.tool_name or '?'}  参数：{args}  "
                f"失败：{bool(row.tool_failed)}\n{content}"
            )
        if len(content) > COMPACT_MESSAGE_CHARS:
            content = content[:COMPACT_MESSAGE_CHARS] + "\n…（截断）"
        parts.append(f"### {row.role}\n{content}")
    text = "\n\n".join(parts)
    if len(text) <= COMPACT_INPUT_CHARS:
        return text
    # 头和尾都要：头是用户最初的目标/授权，尾是紧挨着保留段的最近证据。
    # 只从最老的开始截会丢掉后者，模型压缩后反而忘掉刚做过什么。
    head = COMPACT_INPUT_CHARS // 4
    tail = COMPACT_INPUT_CHARS - head
    return text[:head] + "\n\n…（中间省略）…\n\n" + text[-tail:]


def _split_keep(rows: list[ChatMessage]) -> tuple[list[ChatMessage], list[ChatMessage]]:
    """按字符预算切出 (要压缩的旧消息, 保留原文的最近消息)。"""
    keep: list[ChatMessage] = []
    used = 0
    for row in reversed(rows):
        size = len(row.content or "") + len(row.model_extra or "")
        if keep and used + size > COMPACT_KEEP_CHARS and len(keep) >= 8:
            break
        keep.append(row)
        used += size
    keep.reverse()
    old = rows[: len(rows) - len(keep)]
    # 边界必须落在 user 消息上：`_render_history` 会丢掉首条非 user
    # （Anthropic 要求首条是 user）。切在 tool/assistant 上会让最近一条
    # 工具结果被直接扔掉 —— 模型压缩后反而看不到刚执行了什么。
    # user 和 tool 都能作为上下文起点（`_render_history` 把 tool 渲染成
    # user 的“工具结果”消息）。assistant 开头会被丢掉，所以把它移进摘要。
    while keep and keep[0].role == "assistant":
        old.append(keep.pop(0))
    return old, keep


def _mechanical_summary(rows: list[ChatMessage]) -> str:
    """摘要模型不可用时的确定性降级（OMP `shake` 的最小版本）。

    不编造结论，只按角色保留可核对的信息：用户目标、执行过的命令、
    最近的助手叙述。原文仍在库里，模型可以用工具重读。
    """
    lines = [
        "（摘要模型本轮不可用；以下是机械保留的关键信息，"
        "原始消息仍在库里，可用工具重新读取。）",
        "",
    ]
    used = 0
    for row in rows:
        content = (row.content or "").strip()
        if not content:
            continue
        if row.role == "user":
            part = "用户目标/约束：" + content[:800]
        elif row.role == "tool":
            part = (
                f"执行 {row.tool_name or '?'} "
                f"{json.dumps(row.tool_args or {}, ensure_ascii=False, default=str)[:500]}"
                f"（失败={bool(row.tool_failed)}）：{content[:500]}"
            )
        else:
            part = "助手结论：" + content[:500]
        if used + len(part) > COMPACT_SUMMARY_CHARS:
            break
        lines.append(part)
        used += len(part)
    return "\n".join(lines)


async def compact(
    db: Session,
    *,
    session: Any,
    gateway: Any,
    force: bool = False,
    turn_run: Any = None,
) -> ChatCompaction | None:
    """压缩旧历史并落库。失败返回 `None`，调用方据此决定是否停在原地。"""
    if gateway is None:
        return None
    comp, rows = load_context(db, session=session)
    old, keep = _split_keep(rows)
    if len(old) < COMPACT_MIN_OLD_MESSAGES or not keep:
        return None

    old_text = _serialize(old)
    if len(old_text) < 2_000 and not force:
        return None

    # 上一次摘要也要带给摘要器，否则第二次压缩会丢掉更早的结论。
    prompt = old_text
    if comp is not None:
        prompt = f"## 更早的摘要\n{comp.summary}\n\n## 本次要并入摘要的对话\n{old_text}"

    try:
        try:
            reply = await gateway.chat(
                [{"role": "user", "content": prompt}],
                system=_SUMMARY_SYSTEM,
            )
        except TypeError:
            # 老网关/测试假网关不收 `system` 形参；摘要仍能跑，只是没有角色约束。
            reply = await gateway.chat([{"role": "user", "content": prompt}])
    except Exception:  # noqa: BLE001 - 压缩失败不能毁掉这一轮
        logger.warning("上下文压缩调用失败，尝试机械降级", exc_info=True)
        reply = None

    if turn_run is not None and reply is not None and getattr(reply, "usage", None) is not None:
        from app.chat import turn_runs as turn_run_store

        turn_run_store.add_usage(db, turn_run, reply.usage, track_context=False)

    summary = str(
        getattr(reply, "text", "") or (reply if isinstance(reply, str) else "")
    ).strip()
    if not summary:
        if not force:
            logger.warning("上下文压缩返回空摘要，继续用原历史")
            return None
        summary = _mechanical_summary(old)
        logger.warning("上下文压缩降级为机械摘要（session=%s）", session.id)
    if len(summary) > COMPACT_SUMMARY_CHARS:
        summary = summary[:COMPACT_SUMMARY_CHARS] + "\n…（摘要截断）"

    before = sum(len(m.content or "") + len(m.model_extra or "") for m in rows)
    after = len(summary) + sum(len(m.content or "") + len(m.model_extra or "") for m in keep)
    # 压缩不能反而变大：小历史 + 摘要开销会出现 before=41k / after=44k。
    # 少于 10% 收益就不落边界（尤其是空截断的 force 重试）。
    if after >= before * 0.9:
        logger.info(
            "chat.compact skip session=%s before=%s after=%s（收益不足）",
            session.id,
            before,
            after,
        )
        return None
    row = ChatCompaction(
        id=new_id("cmp"),
        session_id=session.id,
        summary=summary,
        first_kept_seq=keep[0].seq,
        tokens_before=max(0, before // 4),
        tokens_after=max(0, after // 4),
    )
    db.add(row)
    db.commit()
    logger.info(
        "chat.compact session=%s old=%s keep=%s chars=%s->%s",
        session.id,
        len(old),
        len(keep),
        before,
        after,
    )
    return row
