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
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.chat import sandbox as chat_sandbox
from app.chat import service as chat_service
from app.chat.models import ChatSession
from app.chat.prompts import (
    CHAT_TOOL_NAMES,
    CONSEQUENTIAL_TOOL_NAMES,
    TOOL_BY_NAME,
    build_system_prompt,
    may_call_tool,
    render_format_reminder,
    render_truncation_reminder,
)
from app.chat.stream import EVENT_AWAITING, EVENT_DELTA, EVENT_MESSAGE, EVENT_PHASE, Emit

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

#: 挂载技能正文的**总**预算（所有挂载技能加起来）。
#:
#: ★ 为什么需要这个而 `skills.MAX_SKILL_CHARS`（24000）不够：那一个是
#:   **单篇**上限。用户挂五篇，总量就是 12 万字 —— 而它每一轮都要重发。
#:   这里按总量卡，超出时按挂载顺序截：先挂的优先，因为界面上勾选按钮
#:   的排列顺序就是用户心里的优先级。
MAX_MOUNTED_SKILL_CHARS = 40_000

#: 上下文里单条工具输出最多带多少字。
#:
#: 与落库口径（`MAX_MESSAGE_CHARS` = 60000）不同，这里更严：**落库是留档，
#: 喂模型是花钱**。一条 6 万字的 nmap 输出塞进上下文，代价是后面每一轮
#: 都要重新付这 6 万 token，而模型真正需要的通常只是前几百行。
HISTORY_TOOL_CHARS = 4000

#: 一轮里最多"提醒"模型几次让它别停半路（v1.13）。
#:
#: ## 它修的是什么（真实缺陷）
#:
#: 改造前：模型回一句「文件到了。先判型：看包概况、协议分层、流量最大的会话。」
#: —— 正确，但停在半路，没有 `ACTION:`。主循环看到 `actions` 为空就地
#: `return`，整轮结束。用户看到的就是"输出一次就停了"，只能自己打
#: 「继续」「解题啊」把它推一下。
#:
#: ## 为什么是 2 而不是无限
#:
#: 每一次提醒都是一次额外的模型调用（用户等的时间、平台的 token 钱）。
#: 取 2 的理由：模型被推一次就够了；推第二次还不写动作，说明它不是
#: "忘了写"，而是"本来就想这么答"——再推只是烧钱。
#:
#: 上界还必须与 `MAX_TOOL_ROUNDS` 分开算：那个管的是**工具往返**预算，
#: 这个管的是**纠偏**预算。混在一起会让"提醒了两次"吃掉两个工具轮次，
#: 于是真正需要跑工具的一轮反而没预算了。
MAX_NARRATION_NUDGES = 2

#: 「只叙述、没动作」的正文要**同时**满足这两条，才值得推它一下（v1.13）。
#:
#: ## 为什么必须两条都满足（否则每次纯问答都要多花一次调用）
#:
#: 绝大多数对话轮次**本来就以"没有 ACTION"结束** —— 用户问「你能做什么」，
#: 模型答一段话，这就是完整的一轮。如果见到 `actions` 为空就去提醒，
#: 每一次普通问答都会被强行多调 1-2 次模型（延迟翻倍、费用翻倍），
#: 而绝大多数情况下它答得挺好。
#:
#: ## 判据一：有"接下来要动手"的意图词
#:
#: 长度是最先想到的判据，但**它是错的** —— 实测一跑就发现
#: 「你好，我能帮你建扫描、改技能库、加 MCP 工具。」（22 字）是完整回答，
#: 而它会被长度判据推两次，界面上于是出现三条一模一样的助手气泡。
#:
#: 真正区分"说完"与"停在半路"的，是正文里有没有**指向下一步动作**的词。
#: 实测样本（会话「你能做什么」30 条里的原文）逐条对照：
#:
#: | 正文 | 意图词 | 实际是什么 |
#: |---|---|---|
#: | 「文件到了。**先判**型：看包概况、协议分层…」 | `先判` ✓ | **停在半路**，该推 |
#: | 「能，而且比"起引擎"更可控…**我先看**沙箱里有没有附件」 | `我先` ✓ | **停在半路**，该推 |
#: | 「你好，我能帮你建扫描、改技能库、加 MCP 工具。」 | 无 | 完整回答，不该推 |
#: | 「目前平台上没有扫描任务。」 | 无 | 完整回答，不该推 |
#: | 「能。区别在于两种解题方式…」（225 字） | 无 | 完整回答，不该推 |
#:
#: ⚠️ 注意 `我能` **不在**词表里（`我(先|这就|马上|现在)`），它就是被
#:   长度判据误伤的那一条。词表要跟着实测样本改，不要凭感觉加。
_NARRATION_INTENT = re.compile(
    r"(我(先|这就|马上|现在)|先(看|判|查|跑|扫|试|起|摸|确|列|读|拉|执行)"
    r"|接下来|下一步|下面我|让我|我来(看|查|跑|试|扫))"
)

#: 判据二：正文还不能太长。
#:
#: 意图词是弱信号 —— 一段 600 字的完整结论里完全可能顺带写一句「我先看了
#: 一下」，那不是"停在半路"，那是在叙述**已经做过**的事。长文一律当作
#: 说完：真正的"停在半路"形态是**短短一句打算**（实测两条分别是 27 字和
#: 66 字），它没有篇幅去铺开结论。
#:
#: ## 已知的代价（刻意接受）
#:
#: 一个"确实完整但很短、而且碰巧用了意图词"的回答（例如「解出来了：flag{…}，
#: 我先把过程写一下」）会被推一次。模型会再说一遍（这次照样没有动作），
#: 之后 `MAX_NARRATION_NUDGES` 用完，我们照常收尾。用户多等一次调用，
#: 但**不会丢内容** —— 这个方向的错误比"停在半路没人管"轻得多。
MAX_NARRATION_ONLY_CHARS = 200


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
    #: 若这一轮停在等待人工确认上，这里是被挂起那条消息的 id。
    #:
    #: ⚠️ 它是**消息 id** 而不是布尔量：界面上要能定位到具体哪一条、
    #:   用户点确认时也要把它带回来。用布尔量的话前端还得自己从
    #:   `messages` 里再筛一遍。
    awaiting_approval: str | None = None


# ══════════════════════════════════════════════════════════
# 流式推送的辅助（v0.100）
# ══════════════════════════════════════════════════════════
#
# 这三个都是**空转安全**的：`emit is None` 时立刻返回，一个 await 都不做。
# 非流式路径（`POST /messages`）因此逐字节保持旧行为 —— 这条很重要，
# 那条路径被审批、重放与大量测试用着。


async def _emit(emit: Emit | None, event: str, payload: dict[str, Any]) -> None:
    """推一帧（`emit` 为空就什么都不做）。"""
    if emit is not None:
        await emit(event, payload)


async def _emit_message(emit: Emit | None, message: Any) -> None:
    """推一条**已落库**的消息。

    ⚠️ 用 `ChatMessageOut` 的序列化口径（camelCase），不是 ORM 对象直接 dump。
       前端按 camelCase 解析，推 snake_case 会让字段全部读不到 ——
       而那种错在界面上表现为「工具卡没有名字」这类奇怪的空值，
       不像一个协议错误。
    """
    if emit is None:
        return
    from app.chat.schemas import ChatMessageOut

    await emit(
        EVENT_MESSAGE,
        ChatMessageOut.model_validate(message).model_dump(mode="json", by_alias=True),
    )


async def _emit_phase(
    emit: Emit | None, phase: str, round_index: int, **extra: Any
) -> None:
    """推一条「现在在做什么」的 phase 帧。

    `phase` 取值：`thinking`（模型在推理）/ `tool`（正在执行某个工具）/
    `resume`（模型这一轮没写动作或写了空回复，我们正在自动纠偏续跑）。
    额外字段原样带上（工具名、`reason`/`attempt`/`maxAttempts` 等），
    前端只认它认识的。

    ★ `resume` 的语义边界：**它不是一条消息，不落库**。它表示"这一轮
      还没有结束，服务端在替用户多推一次模型"。所以它必须走 phase 帧，
      不能走 message 帧 —— message 帧推的都是已落库的行，用户刷新还在；
      而这次纠偏如果成功，库里根本不会留下痕迹（只有模型最终那句）。
    """
    if emit is None:
        return
    await emit(EVENT_PHASE, {"phase": phase, "round": round_index, **extra})


async def _emit_delta(emit: Emit | None, text: str) -> None:
    """推一段**确认安全**的正文增量（v0.102）。

    `text` 必须是 `HoldbackBuffer` 放行过的前缀 —— 它已经过了
    「不含 `ACTION:` / `FINAL:` 协议起头」这道闸。调用方**不要**把
    模型原始 token 直接喂进来，那样内部协议会出现在用户视野里。
    """
    if emit is None or not text:
        return
    await emit(EVENT_DELTA, {"text": text})


async def _call_model(
    gateway: Any,
    messages: list[dict[str, str]],
    *,
    system: str,
    emit: Emit | None,
) -> Any:
    """调一次模型：**能流式就流式，否则退回非流式**（v0.102）。

    ## 返回什么

    始终是一个 `LlmReply`（`text` 是**完整原文**，含 `ACTION:` 部分）——
    调用方（主循环）走的那条路与非流式时代**完全一样**：它仍然
    `parse_actions(raw_text)`、仍然 `_narration_before_action(raw_text)` 落库。
    流式只影响「中途推了什么给界面」，不影响「拿到手的回复长什么样」。

    这是刻意的：如果流式路径返回拼起来的增量，那它必然缺 ACTION 段
    （增量通道把协议扣掉了），`parse_actions` 就永远解析不出工具调用 ——
    对话会退化成纯聊天，而且**没有任何报错**。

    ## 什么时候不流式

    `emit is None` —— 也就是非流式入口（`POST /messages`）。那条路径没有
    人在看流，多一次 SSE 解析只是白花 CPU，且被审批/重放/大量测试用着，
    让它走一条新的 HTTP 路径没有收益。

    ## ★ 流式失败必须能退回非流式，不能整轮失败

    流式比我方其它 HTTP 路径**新**，而接入点生态参差：某些自建网关对
    `stream: true` 直接 400，某些代理会把 SSE 缓冲到整段才吐。
    这两种情况下重发一次非流式请求是**便宜且正确**的兜底 —— 用户最坏
    拿到的就是改造前的体验，而不是"这个模型用不了"。

    但兜底只在**还没推出去任何字**时成立：已经推了半句再重来一遍，
    界面上会出现"这句话写了两遍"。所以用 `pushed` 记着。
    """
    if emit is None:
        return await gateway.chat(messages, system=system)

    from app.chat.holdback import HoldbackBuffer
    from app.kernel.runner import LlmReply, parse_usage  # 循环 import 规避

    buffer = HoldbackBuffer()
    collected: list[str] = []
    #: `chat_stream` 把收尾帧里的 finish_reason `append` 到这里（v1.13）。
    #: 用 list 是因为生成器只能 yield 正文，没有第二个返回值 —— 见
    #: `LlmGateway.chat_stream` 的 docstring。
    finish_reasons: list[str] = []
    pushed = False
    try:
        async for delta in gateway.chat_stream(
            messages, system=system, finish_reason=finish_reasons
        ):
            collected.append(delta)
            safe = buffer.feed(delta)
            if safe:
                pushed = True
                await _emit_delta(emit, safe)
        tail = buffer.flush()
        if tail:
            pushed = True
            await _emit_delta(emit, tail)
    except Exception:  # noqa: BLE001
        if pushed:
            # 已经吐出字了 —— 不能重来，只能如实失败。
            raise
        _log.info("流式调模型失败，退回非流式重试一次", exc_info=True)
        return await gateway.chat(messages, system=system)

    raw_text = "".join(collected)
    return LlmReply(
        text=raw_text,
        source="content",
        raw={},
        usage=parse_usage({}),
        # ★ 取最后一个取值：正常只有一帧带它，但代理/网关吐多帧时，
        #   以最后那个为准（更接近真实收尾原因）。
        finish_reason=finish_reasons[-1] if finish_reasons else "",
    )


# ══════════════════════════════════════════════════════════
# 截断判据（v1.13）
# ══════════════════════════════════════════════════════════


def _looks_unfinished(text: str) -> bool:
    """这段"只叙述、没动作"的正文，是不是**停在半路**（打算接着做但没写 ACTION）。

    ## 为什么要它，而不是"见到没有 ACTION 就提醒"

    绝大多数对话轮次**本来就以"没有 ACTION"结束** —— 用户问「你能做什么」，
    模型答一段话，那就是完整的一轮。见到 `actions` 为空就提醒，会让每一次
    普通问答都多花 1-2 次模型调用（延迟翻倍、费用翻倍）。

    ## 判据：意图词 + 不算太长，**两条都要**

    先试过"只看长度"，一跑测试就发现是错的：
    「你好，我能帮你建扫描、改技能库、加 MCP 工具。」（22 字）是完整回答，
    却会被推两次 —— 界面上出现三条一模一样的助手气泡，比原来的缺陷更糟。

    真正区分两者的是正文里有没有**指向下一步动作**的词，配合长度上限把
    「叙述已经做过的事」排除掉。逐条依据见 `_NARRATION_INTENT` 上方那张表。

    ## 与 `_is_truncated` 的分工

    被切断时**必然**没有 ACTION，但那不是"忘写格式"，是"话没说完"——
    两种病因要喂不同的提醒语，见 `render_format_reminder` /
    `render_truncation_reminder`。这里只判"要不要推"，推的时候才去问
    "为什么没写动作"。
    """
    body = text.strip()
    if not body or len(body) >= MAX_NARRATION_ONLY_CHARS:
        return False
    return bool(_NARRATION_INTENT.search(body))


def _is_truncated(reply: Any) -> bool:
    """这次回复是不是**被输出长度上限切断**的。

    ## 为什么需要它（v1.13 修的真实缺陷）

    被切断和"格式写错"看起来都是"回复里没有 ACTION"，但病因完全不同，
    药也不同 —— 见 `render_format_reminder` / `render_truncation_reminder`
    的对照表。用同一句话的后果是：被切断的模型被要求"写下一步动作"，
    它会试图把断掉的那句话写完，于是**又超一次**。

    ## 判据与内核侧保持同一口径

    `runner.py` 里是 `reply.finish_reason in ("length", "max_tokens")`，
    取值集合来自 `app.providers.llm.TRUNCATED_FINISH_REASONS` ——
    两家 provider 各叫一个名字（OpenAI `length` / Anthropic `max_tokens`），
    **只认一个就会漏掉另一家**，而漏掉的后果是静默的（界面把截断当成答完）。

    ## 取不到就当作没被截断

    `finish_reason` 为空串是常态：非流式入口走 `gateway.chat()`，而
    `LlmReply.finish_reason` 的默认值就是 `""`；自建网关也常常不给这个字段。
    这种情况下退回"格式错"那句提醒是**正确的**——那是更常见也更安全的猜测
    （它至少不会让模型试图把断掉的话补完）。
    """
    from app.providers.llm import TRUNCATED_FINISH_REASONS

    reason = getattr(reply, "finish_reason", "") or ""
    return reason in TRUNCATED_FINISH_REASONS


# ══════════════════════════════════════════════════════════
# 上下文拼装
# ══════════════════════════════════════════════════════════


def _render_attachments(
    files: list[tuple[str, str]],
    *,
    handed: list[tuple[str, str]] | None = None,
) -> str:
    """把附件的正文拼成一段，接在用户那句话**之后**。

    ## 为什么接在用户消息里，而不是作为独立的一条

    OpenAI/Anthropic 的 wire 格式里没有"附件"这个东西 —— 它要么是一条
    user 消息的一部分，要么是一条独立的 user 消息。做成独立消息的后果：
    `HISTORY_TOOL_CHARS` 的截断口径、`_render_history` 的三角色映射、
    以及"这一轮到底问了什么"的语义都会多出一种形状要处理。

    接在同一条里则完全不动现有的消息模型：模型看到的是一句提问 + 紧跟着
    的材料，正是人类贴一段日志再问问题的样子。

    ## 格式

    ```
    用户说的话

    ---
    ## 附件：`access.log`
    <正文>

    ## 附件：`nginx.conf`
    <正文>
    ---
    原始文件（只读）：
    - `/work/attachments/<id>-access.log`
    ```

    ★ 附件放在**后面**：先问题、后材料。反过来的话模型要读完几万字材料
      才看到"到底要干什么"，而工具输出/长上下文里的注意力衰减是实测存在的。

    ## `handed` 为什么单独列一段

    二进制附件（pcapng/zip/ELF）在正文里只有一句"读不了"。光有那句话，
    模型会以为**没救了** —— 而文件其实就在沙箱里。所以必须显式给出
    容器内路径，并且说清"路径带 id 前缀"（`a8f7-` 那种），否则模型会
    按文件名 `ls` 然后报找不到。

    这一段只在有交接文件时出现。纯文本附件也照样交接了（路径在），
    但正文已经够用，不需要再引导；不过**给了也没坏处**——所以文案统一
    说明"原始文件在这些路径"，让模型自己决定要不要 `xxd`。
    """
    handed = handed or []
    if not files and not handed:
        return ""
    blocks: list[str] = ["", "---"]
    for name, body in files:
        blocks.append(f"## 附件：`{name}`")
        blocks.append(body.rstrip())
        blocks.append("")
    if handed:
        blocks.append("原始文件（只读）也在沙箱里，路径如下：")
        for name, container_path in handed:
            blocks.append(f"- `{name}` → `{container_path}`")
        blocks.append(
            "文本附件的正文已经贴在上面了；但被截断过的、或者你想 grep/awk "
            "全文的，直接读这些路径。二进制附件（pcapng/zip/ELF）在上面只有"
            "一句「读不了」，真正的内容在这条路径上 —— 用 `run_shell` 分析："
            "`file` 看类型、`xxd` 看字节、`binwalk` 拆包、`tcpdump -r` 读抓包、"
            "`strings` 捞可打印串。"
            "路径里的数字前缀是附件 id，用完整路径，别只按文件名 `ls`。"
        )
    blocks.append("---")
    return "\n".join(blocks)


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
            # ★ v0.112：`model_extra` 是**只给模型看**的材料（附件正文、
            #   容器内路径、分析指路）。用户那句话在 `content` 里，材料
            #   单独一列，拼回来之后模型看到的东西与改动前逐字节一致。
            #
            #   为什么在这里拼、而不是在写入时拼好：界面上 `content` 就是
            #   用户气泡里的字。写入时拼进去，用户会看到自己"说过"一大段
            #   `## 附件：…` 的内部说明（v0.112 修的就是这个）。
            extra = getattr(row, "model_extra", None) or ""
            out.append({"role": "user", "content": content + extra})
    return out


# ══════════════════════════════════════════════════════════
# 工具执行
# ══════════════════════════════════════════════════════════


class _ToolContext:
    """执行工具需要的一切。打包成一个对象，免得每个 _tool_* 都收七个参数。

    ## ★ v0.107：沙箱不在这里

    执行类工具（`run_shell` / `browser`）需要 `SandboxContainer`，但它
    **不在 `__slots__` 里**，也不由引擎持有 —— 它住在
    `app/chat/sandbox.py` 的进程内注册表里，按 `session.id` 惰性创建、
    跨轮保留。

    为什么不做成 `ctx.sandbox`：`_ToolContext` 由 `run_turn` 每轮新建
    一次，而沙箱要**跨轮活着**。放进来的话每轮看起来都是一个新沙箱
    （类型上不假，语义上假），而真正持久的那个在别处 —— 同一件事两个
    表述，早晚有人用错那个。让执行工具自己去 `sandbox.get_sandbox(ctx.session)`
    取，只有一个来源。
    """

    __slots__ = ("db", "session", "identity")

    def __init__(self, *, db: Session, session: ChatSession, identity: Any) -> None:
        self.db = db
        self.session = session
        self.identity = identity


def _need_scope(ctx: _ToolContext, tool_name: str) -> None:
    """工具级权限复核。

    ## 为什么在引擎里再判一次（router 已经判过了）

    router 判的是"**这个端点**要不要 admin"（`/chat` 本身只要登录）。
    而对话里的**某一次工具调用**可能需要更高的权限 —— 用户可能只是
    `read` scope，却让模型去改技能库。到了 `save_skill` 这一层才发现
    没权限，比在 router 层拒绝更准确，也才能给模型一句它能理解的话。

    ## 为什么参数是工具名，不是 scope 字符串

    原先是 `_need_scope(ctx, "admin")` —— 判据（谁能做什么）被写在了
    **调用点**。同一个工具所需的权限因此在两处各存了一份：这里手写的
    字符串，和 `ChatToolSpec.scope`。两处不一致时的症状特别隐蔽：
    工具的声明是 `scan`，这里手写 `admin`，于是"有 scan 没 admin 的账号
    看着工具可用、点了却被拒"，而错误信息会说"缺少 admin 权限"——
    用户完全无从判断该找谁开权限。

    改成传工具名后，判据只有一个来源（`prompts.TOOL_BY_NAME` 里的
    `scope`），与 `router.decide_approval` 共用 `may_call_tool`。

    ⚠️ 不用 `require_admin` 那个 FastAPI 依赖：它抛 `ApiError`，
      而引擎里的失败要以**工具结果**的形式回到模型（让它知道"这条路
      走不通，换个说法"），不是让整个请求 403。
    """
    if may_call_tool(tool_name, ctx.identity):
        return
    spec = TOOL_BY_NAME.get(tool_name)
    if spec is None or spec.scope is None:
        raise _ToolDenied("这次对话没有可用的身份信息，不能执行需要权限的操作。")
    raise _ToolDenied(
        f"当前账号没有 `{spec.scope}` 权限，做不了这件事。"
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

    _need_scope(ctx, "create_scan")

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

    _need_scope(ctx, "start_ctf_solve")
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


async def _tool_load_skill(ctx: _ToolContext, args: dict[str, Any]) -> str:
    """`load_skill`：读一篇技能正文（可指定篇内的单个文件）。

    ## 为什么它和 `read_skill` 并存，而不是合并

    `read_skill` 是 v0.93 就有的（给平台操作对话读技能库用的），参数
    只有 `name`。`load_skill` 是内核那套工具的名字与形状
    （`name` + `file`），而**提示词里教模型用的是内核的用法** ——
    两处名字不同，模型会把解题时的习惯带过来然后收到"没有叫 load_skill
    的工具"。

    别名成同一个函数也不行：内核版支持 `file`（技能包里的附件，比如
    `payloads.md`），而 `read_skill` 的签名里没有它。保留两个名字、
    共用同一个底层调用，是最小代价 —— 参数面是并集，实现只有一份。
    """
    from app.kernel import skills as skills_mod

    name = str(args.get("name") or "").strip()
    if not name:
        return _err("没有给 `name`。")
    return skills_mod.load_skill_text(name, file=str(args.get("file") or ""))


async def _tool_load_recipe(ctx: _ToolContext, args: dict[str, Any]) -> str:
    """`load_recipe`：读一条命令行工具配方（完整命令行 + 参数 + 局限）。

    ## 与内核版的唯一区别：不探测

    内核版同样只是 `render_recipe(name)` 查表。但配方库里写着"这个工具
    的二进制**可能不存在**"（`recipes.py` 每条都有 `limitation`），
    而对话沙箱是**无限制档**，装的包和解题镜像**不完全一样**。

    这里仍不探测，理由与内核侧一致（见 `recipes.render_index` 的注释：
    提示词构建时沙箱还没起，谎报"装没装"比不报更糟）。配方正文里
    每条都带 `limitation`，模型读到就知道要先 `command -v` 确认。
    """
    from app.kernel import recipes as recipes_mod

    name = str(args.get("name") or "").strip()
    if not name:
        return _err("没有给 `name`。用 `load_recipe` 之前先看提示词里的配方清单。")
    return recipes_mod.render_recipe(name)


async def _tool_save_skill(ctx: _ToolContext, args: dict[str, Any]) -> str:
    from app.kernel import skill_store

    _need_scope(ctx, "save_skill")
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

    _need_scope(ctx, "delete_skill")
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

    _need_scope(ctx, "add_mcp_server")
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
#: 反过来，这里有而清单里没有的名字也会红
#: （`tests/test_chat.py::test_every_declared_tool_has_an_implementation_and_vice_versa`）。
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
    # ★ v0.107：执行类工具。实现在 `app/chat/sandbox.py` —— 那里复用
    #   内核的 `_tool_run_shell` / `run_browser`，不抄一份。
    "run_shell": chat_sandbox.tool_run_shell,
    "browser": chat_sandbox.tool_browser,
    "load_skill": _tool_load_skill,
    "load_recipe": _tool_load_recipe,
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

    ## ★★ v0.102：判据必须与 `parse_actions` 用同一个正则（修一个真实缺陷）

    原来这里用的是朴素的 `text.find("ACTION:")`，而解析器用的是
    `runner._MARKER = re.compile(r"^\\s*(ACTION|FINAL)\\s*[:：]", re.MULTILINE)`。
    两处判据不一致，产生了两个**方向相反**的缺陷：

    **① 全角冒号：解析器认、这里不认 → 裸 JSON 漏给用户**

            '好的。\\nACTION：{"tool": "run_shell", "args": {}}'

        `parse_actions` 用 `[:：]` 认出了动作并**真的执行了工具**；
        而 `find("ACTION:")` 找不到半角冒号，于是**整段（含裸 JSON）落库**。
        界面上表现为"机器人把内部协议漏出来了"，而同时它的工具调用成功了 ——
        这正是本函数 docstring 上半段在防的那个事故，只是换了个触发条件。

    **② 行首之外的出现：这里认、解析器不认 → 正文被腰斩**

            '我看到一段 ACTION: 开头的日志，那是模型写的。'

        这句话里的 `ACTION:` 不在行首，`_MARKER` 要求 `^\\s*` 所以**不匹配**,
        也就是这一轮**没有任何动作**。而 `find` 会命中，于是正文被切成
        "我看到一段" —— 用户看到一句没说完的话。

    两个都只在特定输入下出现，所以线上一直没炸；但都是**静默**的。
    修法不是各打各的补丁，而是让两处共用同一个判据：这里直接 import
    解析器的 `_MARKER`。

    ⚠️ 连带影响：`FINAL:` 现在也会被切开。对话页从不产出 FINAL
      （那是解题侧的收尾协议），模型偶尔会照猫画虎写出来 ——
      那种东西同样不该出现在用户气泡里，所以这是个改进而不是回归。
    """
    from app.kernel.runner import _MARKER  # noqa: PLC2701 - 见上：必须与解析器同一判据

    match = _MARKER.search(text)
    head = text[: match.start()] if match else text
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


def _resolve_provider(session: ChatSession, explicit: str | None) -> str | None:
    """决定这一轮用哪个接入点。

    顺序：**本条显式指定 > 会话设置 > 平台默认**（`None` 交给 `_build_gateway`）。

    ★ 顺序不能反过来。会话上的值是"这个对话的默认"，逐条指定是
      "这一次例外" —— 反过来会让用户改了会话设置却发现某条消息还是
      按旧的跑，而他找不到原因。
    """
    if explicit:
        return explicit
    return session.provider_id


def _system_prompt_for(session: ChatSession, hinted_tools: list[str] | None = None) -> str:
    """按会话设置渲染系统提示词。

    ★ 这里读的是**库里的当前值**，不是请求参数：用户可能在另一个标签页
      刚刚改了人格/审批开关，而本轮应该按最新的来。用请求参数会让两个
      标签页各按各的跑，且"改了没生效"无法复现。
    """
    mounted = chat_service.mounted_skill_texts(session)
    if mounted:
        # 总量截断：按挂载顺序累加，超出预算的**截断正文**而不是整篇丢弃
        # （丢一篇会让用户以为没勾上；截断至少前言还在，且下面会明确标注）。
        trimmed: list[tuple[str, str]] = []
        budget = MAX_MOUNTED_SKILL_CHARS
        for name, body in mounted:
            if budget <= 0:
                break
            text = body if len(body) <= budget else body[:budget] + "\n\n…（这篇超出本次对话的挂载预算，后面没展开）"
            trimmed.append((name, text))
            budget -= len(text)
        mounted = trimmed

    return build_system_prompt(
        persona=session.persona,
        require_approval=bool(session.require_approval),
        mounted_skills=mounted,
        hinted_tools=hinted_tools,
    )


async def run_turn(
    db: Session,
    *,
    session: ChatSession,
    content: str,
    identity: Any,
    provider_id: str | None = None,
    gateway: Any = None,
    attachment_ids: list[str] | None = None,
    explicit_tools: list[str] | None = None,
    emit: Emit | None = None,
) -> ChatTurnResult:
    """跑一轮对话。

    ## 参数 `gateway`

    给测试用的注入点。传了就不造真网关（测试里不可能真调 LLM）。
    生产路径永远不传，由 `_build_gateway` 从库里造。

    ## 参数 `attachment_ids`

    这一轮要带给模型看的附件 id。正文在**落用户消息之前**读出来并拼进
    这条消息的 `content` —— 见 `attachments.read_text` 的说明为什么
    "用完即删"。

    ## 参数 `emit`（v0.100）

    推一帧的回调。**不传就是老行为**（整轮跑完才回包），那条路径被审批、
    重放与大量测试用着，不能因为这次改造而变成"必须有订阅者"。

    传了之后，每一个「已经落库并 commit」的节点都会推一帧出去 ——
    界面上于是能实时看到：模型在推理 → 它说了什么 → 它要调哪个工具 →
    工具结果是什么 → 下一轮…

    ⚠️ 推的必须是**已落库**的那条消息（`ChatMessageOut` 的形态），
       不是临时拼的对象：界面上最终显示的是库里那条，
       推一份临时数据会在结束时"跳一下"，更糟时两者内容不一致。

    ## 流程

    1. 读附件正文，拼进用户消息，落库
    2. 拼上下文（最近 N 条） + 系统提示词（按会话设置渲染）
    3. 调模型 → 解析 `ACTION:`
    4. 有动作就执行、落 tool 消息、回到 3（最多 `MAX_TOOL_ROUNDS` 轮）
       - ⚠️ 打开审批时，**有副作用的工具不执行**，落一条 pending 消息并停
    5. 没有动作 = 模型说完了 → 落 assistant 消息，结束

    ⚠️ 每一轮模型的**文本正文**都落一条 assistant 消息（哪怕它同时带了
       动作）。这正是界面上"它说要做什么 → 工具卡 → 它说结果"的对话感来源。
    """
    from app.chat import attachments as chat_attachments
    from app.kernel.runner import parse_actions

    result = ChatTurnResult()

    # ── 附件：先读正文，再拼进这一轮的提问 ──────────────────
    #
    # ★ 拼接发生在**落库之前**：附件正文成为这条 user 消息的一部分，
    #   于是下一轮它自然出现在历史里，不需要"这一轮额外带哪些附件"的状态。
    #   代价是它每轮都重发（见 attachments 模块 docstring 的取舍说明）。
    files: list[tuple[str, str]] = []
    # 交接给沙箱的附件：`(显示名, 容器内路径)`。
    handed: list[tuple[str, str]] = []
    if attachment_ids:
        files = chat_attachments.read_text(session_id=session.id, attachment_ids=attachment_ids)
        # ★ v0.109：不再「用完即删」。
        #
        #   原先是 `discard(...)` —— 文本读进上下文之后文件就没用了。那在
        #   "附件只能当文本读"的时代是对的。
        #
        #   但现在对话有沙箱：一个 pcapng 的价值恰恰在于 `tcpdump`/`binwalk`
        #   能跑它，而 `read_text` 对二进制只回一句"读不了"。**先删掉再去
        #   告诉模型"你可以去 /work/attachments 看看"** 是自相矛盾的。
        #
        #   改成 `hand_over(...)`：文件从暂存区**搬**到会话交接目录，那个
        #   目录以只读方式挂进沙箱的 `/work/attachments`。搬走而不是复制，
        #   是为了让暂存区腾空 —— 否则一路撞 `MAX_STAGED_PER_SESSION`(30)。
        handed = chat_attachments.hand_over(
            session_id=session.id, attachment_ids=attachment_ids
        )

    # ★ v0.112：附件材料进 `model_extra`，**不**拼进 `content`。
    #
    #   原来这里是 `stored_content = content + _render_attachments(...)`，
    #   于是界面把那段材料当作用户自己打的字渲染出来 —— 里面有
    #   `## 附件：xxx`、容器内路径、以及"用 binwalk / tcpdump 分析"这类
    #   我们写给模型的指令。用户看到会合理地以为"我什么时候说过这些"。
    #
    #   这与 `_narration_before_action` 那条判例是同一个形状：内部材料进了
    #   用户视野之后，复核时无法判断某句话是模型说的还是系统拼的。
    #
    #   拆成两列之后：界面只渲染 `content`（用户原话），模型上下文由
    #   `_render_history` 拼回 `content + model_extra`，**逐字节不变**。
    attachments_extra = _render_attachments(files, handed=handed)
    user_message = chat_service.append_user_message(
        db,
        session=session,
        content=content,
        model_extra=attachments_extra or None,
    )
    result.messages.append(user_message)
    db.commit()
    await _emit_message(emit, user_message)

    if gateway is None:
        gateway = _build_gateway(_resolve_provider(session, provider_id), db)

    system = _system_prompt_for(session, explicit_tools)
    ctx = _ToolContext(db=db, session=session, identity=identity)

    #: 打开审批时，这些工具不执行、只挂起。
    gate_on = bool(session.require_approval)

    #: 已经"提醒"过模型几次（见 `MAX_NARRATION_NUDGES`）。
    #:
    #: ★ 它单独计数、不从 `MAX_TOOL_ROUNDS` 里扣 —— 两笔预算管的是不同的
    #:   东西（纠偏 vs 工具往返）。混在一起会让"提醒了两次"吃掉两个工具
    #:   轮次，于是真正需要跑工具的那一轮反而没预算了。
    nudges_used = 0

    #: 下一轮要往上下文里补的一句提醒（一次性）。
    #:
    #: ⚠️ 不能直接 `messages.append(...)` —— `messages` 每轮都由
    #:   `_render_history(history)` **从库里重新拼**，append 上去的东西
    #:   在下一轮就被丢掉了。所以用一个变量把它带到下一轮，在那里补。
    pending_reminder: str | None = None

    #: 循环上界 = 工具轮数 + 1（最后那轮用来总结）+ 纠偏轮数。
    for round_index in range(MAX_TOOL_ROUNDS + 1 + MAX_NARRATION_NUDGES):
        # ★ v0.100：在调模型**之前**推一条 phase 帧。
        #
        # 这一帧对应的正是界面上「正在想…」那一行 —— 它现在能显示
        # 「第 2 轮 · 模型在推理」而不是一个没有信息的转圈。
        # 工具往返越多，这个信息越有用：用户能看出它是**在推进**
        # 还是**卡住了**。
        #
        # v1.13：序号用**工具轮次**而不是循环变量 —— 纠偏轮不该让界面上
        # 的"第 N 轮"跳号（用户看到的是"怎么突然第 4 轮了"）。
        await _emit_phase(
            emit, "thinking", round_index - nudges_used, budget=MAX_TOOL_ROUNDS
        )

        history = chat_service.list_messages(
            db, session=session, limit=HISTORY_MESSAGE_LIMIT
        )
        messages = _render_history(history)
        if pending_reminder is not None:
            # ★ 一次性：补进去之后立刻清掉。留着的话它会出现在之后每一轮
            #   的上下文里，模型会以为用户反复在催它。
            messages.append({"role": "user", "content": pending_reminder})
            pending_reminder = None

        # ★ v1.13：`round_index - nudges_used` —— 纠偏轮不消耗工具预算。
        at_limit = (round_index - nudges_used) >= MAX_TOOL_ROUNDS
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
            reply = await _call_model(
                gateway, messages, system=system_this_round, emit=emit
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("对话调模型失败", exc_info=True)
            raise ChatEngineError(f"调用模型失败：{type(exc).__name__}。") from exc

        # ★ 落库给用户看的是**剥掉动作之后**的正文。理由见 `_narration_before_action`。
        raw_text = reply.text or ""
        text = _narration_before_action(raw_text)
        actions = [] if at_limit else parse_actions(raw_text)
        reasoning = getattr(reply, "reasoning", None)
        if not reasoning:
            raw = getattr(reply, "raw", None) or {}
            if isinstance(raw, dict):
                candidate = raw.get("reasoning_content") or raw.get("thinking")
                reasoning = candidate if isinstance(candidate, str) and candidate else None

        if not text and not actions:
            # ★ v1.13：空回复也要先试一次提醒，而不是当场给用户一句
            #   「（模型这次没有产出内容。可以再问一次，或换个接入点试试。）」
            #
            # 那句占位语把**平台的失败**推给了用户去处理。而实测里这个形态
            # （会话「你能做什么」seq 8 / seq 24）出现时，模型下一轮其实
            # 完全能正常产出 —— 它只是这一轮什么都没写。多调一次的成本，
            # 比让用户重打一遍问题低得多。
            #
            # 但 `at_limit` 那轮不能推：那一轮我们要的就是"用一段话总结"，
            # 模型交白卷时占位语是唯一能给用户的信息。
            if not at_limit and nudges_used < MAX_NARRATION_NUDGES:
                nudges_used += 1
                truncated = _is_truncated(reply)
                pending_reminder = (
                    render_truncation_reminder(len(raw_text))
                    if truncated
                    else render_format_reminder()
                )
                # ★ v1.14：纠偏续跑必须有个可见的 Phase 帧。
                #
                # 改造前这里什么都不推：界面上「正在想…」停住，然后凭空蹦出
                # 下一段。用户无法区分「它在自动重试」与「它卡死了」——
                # 而这两件事该做的动作完全相反（等 / 刷新重发）。
                # CyberStrikeAI 在同一位置推 `finalization_auto_continue`
                # （"最终回复检查尚未收敛，正在基于已有轨迹继续执行…"），
                # 我们是同一个语义，只是走 phase 帧而不是 message 帧：
                # 它不是答案，不该落库变成一条气泡。
                await _emit_phase(
                    emit,
                    "resume",
                    round_index,
                    reason="truncation" if truncated else "format",
                    attempt=nudges_used,
                    maxAttempts=MAX_NARRATION_NUDGES,
                )
                continue

            # ★ v1.13：**这一轮已经给用户看过话了，就不要再补一句占位语。**
            #
            # 典型形态：模型答了一句短的完整回答（"你好，我能帮你…"），
            # 我们按长度判据推了它一次，它这次什么都没写。此时它上一句
            # 就是答案，追加「模型这次没有产出内容」等于告诉用户
            # "刚才那句话不算数" —— 而那句话其实是对的。
            already_spoke = any(
                getattr(m, "role", None) == "assistant" for m in result.messages
            )
            if already_spoke and not at_limit:
                return result

            from app.kernel.runner import parse_actions as _parse

            discarded = _parse(raw_text) if at_limit else []
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
            await _emit_message(emit, placeholder)
            return result

        assistant_message = chat_service.append_assistant_message(
            db, session=session, content=text, reasoning=reasoning
        )
        result.messages.append(assistant_message)
        db.commit()
        await _emit_message(emit, assistant_message)

        if not actions:
            # ★ v1.13：**不立刻结束**，先分清它是"说完了"还是"停在半路"。
            #
            # 改造前这里就是一句 `return result` —— 于是模型回一句
            # 「文件到了。先判型：看包概况、协议分层、流量最大的会话。」
            # （27 字，没写 ACTION）整轮就结束了，用户只能自己打「继续」。
            # 实测那条会话里两次出现这个形态。
            #
            # 判据分三层，**顺序有意义**：
            #
            # 1. `at_limit` —— 已经到工具上限，这句总结是我们要的结局，
            #    推它继续只会让它去写"不要再写 ACTION"之外的 ACTION。
            # 2. `MAX_NARRATION_NUDGES` 用完 —— 推过两次还不写动作，
            #    说明它本来就想这么答，再推只是烧钱与拖时间。
            # 3. `_looks_unfinished` —— 正文里得有"接下来要动手"的意图词，
            #    且不算太长。纯问答（没有意图词）不该被推。
            #
            # 三层都过了才推，且把**为什么**告诉模型（被切断 vs 格式错），
            # 见 `render_format_reminder` / `render_truncation_reminder`。
            if (
                not at_limit
                and nudges_used < MAX_NARRATION_NUDGES
                and _looks_unfinished(text)
            ):
                nudges_used += 1
                truncated = _is_truncated(reply)
                pending_reminder = (
                    render_truncation_reminder(len(raw_text))
                    if truncated
                    else render_format_reminder()
                )
                # ★ v1.14：同上 —— 自动续跑要让用户看得见（见空回复分支的长注释）。
                #
                # 这条路的 `reason` 更有信息量：此时模型**写了**正文却没写
                # ACTION，所以除了「格式错 / 被切断」之外，正文本身也是
                # 用户已经看到的半句话。前端据此可以说「它停在半句话上，
                # 正在让它接着干」而不是笼统的「重试中」。
                await _emit_phase(
                    emit,
                    "resume",
                    round_index,
                    reason="truncation" if truncated else "format",
                    attempt=nudges_used,
                    maxAttempts=MAX_NARRATION_NUDGES,
                )
                # 这一轮没有动作，所以循环末尾的"工具预算 +1"不适用；
                # 用 `continue` 直接进下一轮模型调用。
                continue
            return result

        for name, args in actions:
            # ── 人工审批闸门 ──────────────────────────────────
            #
            # ★ 判据是 `CONSEQUENTIAL_TOOL_NAMES`（与提示词里那段用的是
            #   同一个集合），不是"凡是写操作"。两处用同一个集合是刻意的：
            #   提示词告诉模型"这些会等确认"，如果这里实际拦的集合不一样，
            #   模型会对某些工具毫无预警地卡住 —— 而它会以为是自己失败了。
            #
            # ★ 挂起之后**立刻停止这一轮**：用户还没确认，模型继续调下一个
            #   工具会让界面上同时挂起多个确认框，而且它们的顺序取决于
            #   模型而不是用户。停下来，等用户决定，由 `resume_approved`
            #   接着跑。
            if gate_on and name in CONSEQUENTIAL_TOOL_NAMES:
                pending = chat_service.mark_awaiting_approval(
                    db, session=session, tool_name=name, tool_args=args
                )
                result.messages.append(pending)
                result.awaiting_approval = pending.id
                db.commit()
                await _emit_message(emit, pending)
                # ★ 单独的 awaiting 帧：界面据此把输入框放出来并提示
                #   "先确认一下"，而不是继续转圈等一个永远不会来的结果。
                await _emit(emit, EVENT_AWAITING, {"messageId": pending.id})
                return result

            # ★ 工具**开始执行**之前先推一帧。
            #
            # 理由：`_execute_tool` 可能跑几十秒（一个扫描、一次模型调用的
            # 代理）。只在结束时推结果的话，界面上那段时间是空的 ——
            # 与改造前的体验一样。推一条"开始"帧，用户才知道它在干嘛。
            await _emit_phase(emit, "tool", round_index, tool=name)

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
            await _emit_message(emit, tool_message)

    return result


async def resume_approved(
    db: Session,
    *,
    session: ChatSession,
    message: Any,
    identity: Any,
    gateway: Any = None,
) -> ChatTurnResult:
    """执行一条刚刚被批准的工具调用，然后**接着跑这一轮**。

    ## 为什么是"接着跑"而不是"只执行"

    用户点确认的意思是"这一步可以做，继续"。只执行不继续的话，模型下一条
    消息会看到工具结果但不会主动说话 —— 用户还得再发一句"然后呢"。
    所以这里执行完直接进入下一轮模型调用。

    ## 与 `run_turn` 的关系

    共用 `_execute_tool` / `_system_prompt_for` / `_build_gateway`，
    但**不复用主循环**：这里已经有一条落好库的 user 消息了，再走一遍
    "落用户消息"会造出一条重复的提问。复制这段循环的代价是两处都要维护，
    而抽出一个"从当前历史继续跑 N 轮"的公共函数会让参数表长到读不懂
    （它要接收 gate_on、at_limit 语义、以及"这一轮的 user 消息已经存在"
    这个开关）。

    ⚠️ 这是一个**已知的重复**。如果第三处需要同样的循环，那时再抽 ——
       两处重复的代价小于现在就把抽象做错的代价。
    """
    from app.kernel.runner import parse_actions

    result = ChatTurnResult()

    output, failed = await _execute_tool(
        _ToolContext(db=db, session=session, identity=identity),
        message.tool_name or "",
        message.tool_args or {},
    )
    chat_service.finish_approved_tool(
        db, session=session, message=message, output=output, failed=failed
    )
    result.messages.append(message)
    db.commit()

    if gateway is None:
        gateway = _build_gateway(_resolve_provider(session, None), db)

    system = _system_prompt_for(session)
    gate_on = bool(session.require_approval)
    ctx = _ToolContext(db=db, session=session, identity=identity)

    # ★ v1.13：与 `run_turn` 同一套纠偏预算。
    #
    # 不同步这条路径的后果是**只在审批之后**复现"输出一次就停了"：
    # 用户点完"确认"，模型说一句"好的，我来跑"就没了 —— 而这个形态
    # 恰恰更容易出现，因为刚批准的工具结果正要喂回去，它常常先播报一句。
    nudges_used = 0
    pending_reminder: str | None = None

    for round_index in range(MAX_TOOL_ROUNDS + MAX_NARRATION_NUDGES):
        history = chat_service.list_messages(
            db, session=session, limit=HISTORY_MESSAGE_LIMIT
        )
        messages = _render_history(history)
        if pending_reminder is not None:
            messages.append({"role": "user", "content": pending_reminder})
            pending_reminder = None

        at_limit = (round_index - nudges_used) >= MAX_TOOL_ROUNDS - 1
        system_this_round = system
        if at_limit:
            result.hit_tool_limit = True
            system_this_round = (
                system
                + "\n\n## ⚠️ 工具调用已到本轮上限\n\n"
                "不要再写 ACTION。现在直接用一段话总结。"
            )

        try:
            reply = await gateway.chat(messages, system=system_this_round)
        except Exception as exc:  # noqa: BLE001
            _log.warning("对话调模型失败（审批后）", exc_info=True)
            raise ChatEngineError(f"调用模型失败：{type(exc).__name__}。") from exc

        raw_text = reply.text or ""
        text = _narration_before_action(raw_text)
        actions = [] if at_limit else parse_actions(raw_text)
        reasoning = getattr(reply, "reasoning", None)
        if not reasoning:
            raw = getattr(reply, "raw", None) or {}
            if isinstance(raw, dict):
                candidate = raw.get("reasoning_content") or raw.get("thinking")
                reasoning = candidate if isinstance(candidate, str) and candidate else None

        if not text and not actions:
            if not at_limit and nudges_used < MAX_NARRATION_NUDGES:
                nudges_used += 1
                pending_reminder = (
                    render_truncation_reminder(len(raw_text))
                    if _is_truncated(reply)
                    else render_format_reminder()
                )
                continue

            placeholder = chat_service.append_assistant_message(
                db,
                session=session,
                content="（批准后模型没有产出内容。可以再问一次，或换个接入点试试。）",
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
            # ★ v1.13：与 `run_turn` 同一条判据顺序（见那边的长注释）：
            #   到上限 / 用尽纠偏 / `_looks_unfinished` —— 三者都过了才推。
            if (
                not at_limit
                and nudges_used < MAX_NARRATION_NUDGES
                and _looks_unfinished(text)
            ):
                nudges_used += 1
                pending_reminder = (
                    render_truncation_reminder(len(raw_text))
                    if _is_truncated(reply)
                    else render_format_reminder()
                )
                continue
            return result

        for name, args in actions:
            if gate_on and name in CONSEQUENTIAL_TOOL_NAMES:
                pending = chat_service.mark_awaiting_approval(
                    db, session=session, tool_name=name, tool_args=args
                )
                result.messages.append(pending)
                result.awaiting_approval = pending.id
                db.commit()
                return result

            tool_output, tool_failed = await _execute_tool(ctx, name, args)
            tool_message = chat_service.append_tool_message(
                db,
                session=session,
                tool_name=name,
                tool_args=args,
                content=tool_output,
                failed=tool_failed,
            )
            result.messages.append(tool_message)
            db.commit()

    return result


__all__ = [
    "HISTORY_MESSAGE_LIMIT",
    "HISTORY_TOOL_CHARS",
    "MAX_MOUNTED_SKILL_CHARS",
    "MAX_TOOL_ROUNDS",
    "ChatEngineError",
    "ChatTurnResult",
    "resume_approved",
    "run_turn",
]
