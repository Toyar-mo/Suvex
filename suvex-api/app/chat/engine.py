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

import asyncio
import contextlib
import inspect
import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import finding_tools, graph_tools, platform_tools
from app.chat import failure as failure_kind
from app.chat import interrupts as interrupt_bus
from app.chat import mcp_tools
from app.chat import nudge as nudge_policy
from app.chat import sandbox as chat_sandbox
from app.chat import service as chat_service
from app.chat import todos as todo_store
from app.chat import turn_runs as turn_run_store
from app.chat.models import ChatMessage, ChatSession
from app.chat.prompts import (
    BRUTE_HARD_LIMIT,
    CHAT_TOOL_NAMES,
    TOOL_BY_NAME,
    approval_gated_names,
    build_system_prompt,
    build_tools_schema,
    may_call_tool,
    render_auth_attempt_blocked_note,
    render_brute_stop_reminder,
    render_final_confirm_reminder,
    render_format_reminder,
    render_native_tool_note,
    render_plan_exhausted_note,
    render_unfinished_plan_reminder,
    render_repeated_conclusion_note,
    render_result_continue_reminder,
    render_stall_reminder,
    render_strategy_pivot_reminder,
    render_truncation_reminder,
    render_turn_wall_note,
)
from app.chat.stream import (
    EVENT_AWAITING,
    EVENT_DELTA,
    EVENT_MESSAGE,
    EVENT_PHASE,
    EVENT_TOOL_OUTPUT,
    EVENT_TOOL_PROGRESS,
    Emit,
)
from app.core.errors import ApiError

_log = logging.getLogger(__name__)

#: 一轮用户消息里最多做几轮"模型→工具→模型"。
def _env_int(name: str, default: int) -> int:
    """读一个正整数环境变量（读不到/非法就用默认值）。

    ★ 为什么预算要能改：不同任务的"够用"差一个数量级 —— 一道 pwn 题
      十几到几十轮工具往返，而一个"查一下扫描结果"两轮就完。写死在代码
      里意味着要么把简单任务拖慢，要么把难题截断。默认值给难题留足，
      运维再用环境变量收紧。
    """
    import os

    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default



def _turn_elapsed_seconds(turn_run: Any, fallback_start: float) -> float:
    """这一轮已经跑了多少秒（跨 resume / 重启累计）。

    `time.monotonic()` 的起点在每次接手时重置 —— 2026-11-09 的线上会话
    因此跑了 5 小时（每次接手都 <1h，合计远超 1h 墙钟）。`turn_run.started_at`
    是落库的，接手沿用同一个起点；没有 turn_run（普通问答）时才退回单调时钟。
    """
    started = getattr(turn_run, "started_at", None) if turn_run is not None else None
    if started is not None:
        try:
            from app.core.timeutil import now_utc

            return max(0.0, (now_utc() - started).total_seconds())
        except TypeError:
            pass
    return time.monotonic() - fallback_start


#: 一轮里最多几次**工具往返**。
#:
#: ★ v1.26：6 → 40 → **200** → **800**（与 CyberStrikeAI 的
#:   `max_iterations: 800` 对齐 —— 用户明确要求"人家 800 我们也 800"）。
#:
#: 为什么必须这么大：参考实现 CyberStrikeAI 的默认就是 `max_iterations: 800`。
#: 而实测一条真实的 CTF pwn 链（侦察 → 反汇编 → 找 gadget → 构造 ROP →
#: 打远程 → 拿 flag）动辄几十上百次工具调用。上限 40 的失败形态很具体：
#: 不是"报错"，而是**任务刚好做到一半，循环用尽，模型被迫收尾** ——
#: 用户看到的就是「又中断了，不能一次性解出来吗」。
#:
#: 每次往返都是真金白银，所以默认可调（`SUPEX_CHAT_MAX_TOOL_ROUNDS`）。
MAX_TOOL_ROUNDS = _env_int("SUPEX_CHAT_MAX_TOOL_ROUNDS", 800)

#: 喂给模型的上下文里，最多带多少条历史消息。
#:
#: 为什么不是"全部"：一次长对话可能有几百条，而每条工具输出都可能有几万字
#: （虽然落库时截到 60000）。全带上会让第 50 轮的一次调用就撑爆上下文。
#:
#: ★ v1.26：30 → **200** → **400**（随轮次上限 800 同比放大）。
#:
#: 取 30 的失败形态和轮次上限一样：**难题做到一半，早期证据被挤掉了**。
#: 一条工具结果就是一条消息，30 条只够 10 轮左右；而 200 轮的预算下，
#: 第 60 轮时模型已经看不到第 5 轮拿到的关键输出 —— 于是它开始重复
#: 已经做过的事，看起来"笨"、"绕圈"。
#:
#: 模型窗口是 1M token（`deepseek-v4.1-flash`），200 条 × 每条最多
#: `HISTORY_TOOL_CHARS` 字仍在安全范围内。真正的兜底是下面的
#: `MAX_HISTORY_CHARS`：超了就按"先丢最老的**工具结果**"降级，
#: 保住用户的话与模型的结论。
HISTORY_MESSAGE_LIMIT = _env_int("SUPEX_CHAT_HISTORY_MESSAGE_LIMIT", 400)

#: 送进模型的**历史总字符**上限（约 1 char ≈ 0.6 token，40 万字符 ≈ 24 万 token）。
#:
#: ★ 为什么既要条数上限又要总字符上限：条数管"记得多久"，字符管"一次调用
#:   多贵"。一条 8000 字的工具输出和一条 3 字的消息在条数上是等价的，
#:   在成本上差 2000 倍。只卡条数等于把成本交给运气。
# v1.85：400k → 150k。实测 232 条 / 228k 字符的会话每轮整包重发，
# 首字延迟明显高于扫描 agent（每个 agent 只带自己的短上下文）。
MAX_HISTORY_CHARS = _env_int("SUPEX_CHAT_MAX_HISTORY_CHARS", 150_000)

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

#: 回复被 `max_tokens` 切断时，允许多少次「接着写」（v1.21）。
#:
#: ★ 必须与 `MAX_NARRATION_NUDGES` 分开。实测线上会话
#:   `chat_01M60YNRTZ0Z8S7BF85NNWMRNN` 的三条 turn_run 全部是
#:   `nudges_used=2` 就收尾 —— 模型在正文里用英文长篇推理，单条回复反复
#:   撞 `max_tokens`；两次纠偏额度用完之后，第三次被切断时我们什么都不做，
#:   界面上就停在「第二次 read 则」这种半句话上（用户说的「解一半又停下」）。
#:
#:   切断是**我们的上限**造成的，不是模型不听话，所以给大额度；"忘了写
#:   ACTION"才是模型的问题，那个保持 2 次。6 次 × 每轮几千 token 仍是
#:   可控成本，而用户不用再手打「继续」。
MAX_TRUNCATION_CONTINUES = _env_int("SUPEX_CHAT_MAX_TRUNCATION_CONTINUES", 6)

#: 「本轮跑过工具」的轮次最多被推几次（v1.23）。
#:
#: ★ 独立于 `MAX_NARRATION_NUDGES`(2)。那 2 次是给**纯问答**的成本闸，
#:   而任务型轮次（`tools_used > 0`）的语义是"任务进行中，突然只写一句
#:   人话就收尾几乎总是没做完"。共用 2 次会让一次空回复（模型抽风写了个
#:   空串）就吃掉一半预算，于是后面真正的"没写完"反而推不动 —— 实测线上
#:   会话 `chat_01M61DWC71W8989RRMW6DZS9SP` 就是 `nudges_used=2 /
#:   truncations_used=0 / tools_used=2`，界面停在一句英文上。
#:
#: 8 次而不是无限：确定性失败（例如工具一直报同一个错）必须能收尾，
#: 否则就是每轮烧一次模型调用而用户只看到同一个卡点。
MAX_TOOL_NUDGES = _env_int("SUPEX_CHAT_MAX_TOOL_NUDGES", 8)

#: ★ v1.49：同一条命令（工具+参数完全相同）连续执行多少次就判定"原地打转"。
#:
#: 实测（会话 chat_01M6BWQ69MZJNBAB7C7QMVXPV2 的 turn_01M6E99VZ…）：
#: round_index 75、tools_used 101，模型在同一个死胡同里反复写「我意识到一个
#: 从未做过的测试…现在真正运行」，跑的却是**同一条**命令、拿到**一模一样**的
#: 结果。判据只认这个最硬的信号（完全相同 = 不可能有新信息），不掺任何长度/
#: 语义启发式 —— 那些在技术性长文里误判率太高。
#:
#: 取 5：容许偶发的"重跑一次确认"（模型有时确实会重跑同一命令对比），
#: 但连跑 5 次完全相同的命令而还在期待不同结果，只可能是策略走死了。
MAX_REPEATED_ACTIONS = _env_int("SUPEX_CHAT_MAX_REPEATED_ACTIONS", 5)

#: ★ v1.64.1：跑过工具、但**连续多少轮没再调任何工具**（只写了正文）就认定
#: 它已经干完了，不再推。
#:
#: 为什么要有这条：`should_nudge` 的 `tools_used > 0` 分支是**无条件**推
#: （"跑过工具就推"的安全网，见 nudge.py）。原生工具调用（v1.64）之后，模型
#: 干完活就**不再调工具、只写一段结论**——没有 `FINAL:`/`ACTION:` 这种终止
#: 信号，于是"跑过工具就推"会把一段完整的结论当成"还没说完"反复推，模型只好
#: 反复复述完成情况，一路烧到 `MAX_TOOL_NUDGES=300`。
#: 实测（chat_01M6KEQ7VJTN9A5QER9KQJEV0B，2026-11-04）：一个两步小任务，
#: round_index 已到 38、tool_nudges_used 33，其中真工具调用只有 2 次，其余
#: 全是模型在复述"两步都完成了"。用户说的"别再出 bug"就是这类。
#:
#: 判据只认最硬的信号：**这一轮有没有真的执行工具**。执行了就清零重来
#: （说明它还在推进）；连续 N 轮没执行又没被别的通道（计划句打回/截断/
#: 正文带命令）接住 → 认定它是在作答，收尾。
#:
#: 取 5：与 `MAX_REPEATED_ACTIONS` 同口径 —— 容许"工具返回后想再确认一下、只
#: 写一句不调工具"的偶发情况，但连 5 轮都不调工具还在被推，只可能是它其实
#: 已经答完了。必须 ≤ `MAX_NARRATION_NUDGES`(2) 之上的合理值、且远小于
#: `MAX_TOOL_NUDGES`(300)，否则等于没设。
MAX_NO_TOOL_NUDGES = _env_int("SUPEX_CHAT_MAX_NO_TOOL_NUDGES", 5)

#: 跑过工具的轮次里，模型回了**空回复**时最多再推几次（v1.31）。
#:
#: ★ 空回复与「写了人话没写 ACTION」是两种病：前者是模型抽风（实测线上
#:   复跑 `chat_01M652CRXMJ4H9P1XABAJ4VGSH`，计划句之后连回两次空），
#:   后者是它真在描述进度。旧代码把空回复挤在 `MAX_NARRATION_NUDGES`(2)
#:   里，两个空就把额度吃光，于是「已经说过话」直接收尾 —— 界面停在半句
#:   计划上。
#:
#: 6 次而不是 300（`MAX_TOOL_NUDGES`）：空回复没有进展可言，连回 6 次
#: 就是确定性的模型/接入点故障，再推只是烧钱；而 6 次足够跨过偶发的
#: 两次抽风。计数用 `nudges_used`，不新增持久字段。
MAX_EMPTY_NUDGES = _env_int("SUPEX_CHAT_MAX_EMPTY_NUDGES", 6)

#: ★ v1.67：模型流式响应的**空闲超时**（秒）。超过这么久没有新的"正文增量"，
#:   就判定上游把连接挂死了，抛可重试超时 → 本轮回退为 `interrupted`、交给
#:   resumer 接手，而不是让协程永远等下去。
#:
#: ## 为什么要这一条（线上事故）
#:
#: 会话 `ctf`（turn_01M6N2JEY1FZ9HERVADHSQHVZ3）和"加一个 MCP 工具连接"
#: 两个轮次都 `status=running` 但 `heartbeat_at` 冻在原地 49~53 分钟 —— 它们
#: **不是**在跑工具（工具 `DEFAULT_SHELL_TIMEOUT=300` 会超时），而是卡在
#: `_call_model` 的 `async for delta in gateway.chat_stream(...)`：上游只发
#: SSE 注释行 `: keepalive`（`runner._iter_sse` 会跳过，不 yield 正文），没有
#: 任何**正文**增量，`async for` 就永远阻塞。更糟的是这个卡死协程仍占着进程内
#: `_ACTIVE` 租约（`turn_runs.is_active` 恒 True），resumer 的 `stale_runs` /
#: `claim` 都因 `is_active()` 跳过它 —— 被自己的安全网挡在门外，永不接手。
#:
#: 这一条对应 CyberStrikeAI 的「bounded agent waits」（每一次阻塞等待都必须有
#: 界，绝不 no-limit）。取 180s：远大于正常"上游吐字间隔"（正常几秒一个字），
#: 又小于单个合法工具上限 300s —— 只要真的在吐正文就会不断刷新，不会误杀。
#: 模型正在长思考但**最终会吐字**时，也仍在 180s 内（思考完会出首字）；真正
#: 卡死的是"连接还在、但永远没有正文"，那正是要抓的。
#: 设 0 关闭（仅供测试/特殊部署），生产绝不置 0。
STREAM_IDLE_SECONDS = _env_int("SUPEX_CHAT_STREAM_IDLE_SECONDS", 180)

#: ★ v1.68：单轮**墙钟预算**（秒）。一次 `run_turn` / `resume_approved` 调用从
#:   开始干活算起，连续跑超过这么久就**优雅收尾**：落一句"本轮已运行超过 N 分钟，
#:   先停下，确认需要可再发「继续」"，落 `interrupted`（可接手），释放沙箱。
#:
#: ## 为什么需要（线上事故）
#:
#: 会话 `ctf` 的 turn_01M6N2JEY1FZ9HERVADHSQHVZ3 单轮连续跑了 **22732s（≈6.3h）**、
#: 214 轮 / 629 次工具 —— 每一步都是**不同的**真实 `run_shell`（在真做一道堆 pwn），
#: 所以 `MAX_REPEATED_ACTIONS`(5) 永不触发、`MAX_TOOL_NUDGES`(300) 管的是"纠偏"不是
#: "动作"。也就是说：**一个合法但极难的活会在没有任何总上限的情况下一直烧下去**，
#: 用户点「中断并继续」只是让它接着无限跑，观感就是"一直不停"。
#:
#: 这正是 CyberStrikeAI 的 `agent.tool_timeout_minutes`（默认 10，"avoid 0 (no limit)"）
#: —— **一次任务必须有总时长上界**，到点停并释放资源，而不是让"动作"不受限。
#: 取 3600s（1h）：远大于任何正常单轮（正常的活几分钟内收）；真需要更久的大任务，
#: 用户点「继续」即可再拿一整段预算，既不失控、也不一刀切死长任务。
#: 设 0 关闭（仅供测试/特殊部署），生产绝不置 0。
TURN_WALL_SECONDS = _env_int("SUPEX_CHAT_TURN_WALL_SECONDS", 3600)

#: 「同一结论复读闸」（v1.69）的两个上下文门槛。
#:
#: 只判「最近几条 assistant 正文是否在复读同一句结论」，但**必须先干够活**：
#: 会话 chat_01M6P1TAYTASS6P64P85PED8MB 卡死时是 round 127 / tools 437；正常
#: 长任务远低于此。门槛设在十几轮/二十次工具，既能拦住真正的死循环，又不会
#: 误伤「刚开始干活时正常地重复描述现状」（那会误杀正常会话与测试）。
REPEATED_CONCLUSION_MIN_TOOLS = _env_int("SUPEX_CHAT_REPEATED_CONCLUSION_MIN_TOOLS", 20)
REPEATED_CONCLUSION_MIN_ROUNDS = _env_int("SUPEX_CHAT_REPEATED_CONCLUSION_MIN_ROUNDS", 12)

#: 任务型轮次里，模型写了 `FINAL:` 之后我们先**打回**几次（v1.29）。
#:
#: ## 为什么需要打回
#:
#: 线上会话 `chat_01M62X9DDC52RM95NKQW8P5DY8` 里，模型的 FINAL 写的是
#: 「现在查 libc 版本。」—— 那是**进度播报**，不是结论。而 `should_nudge`
#: 的第一条判据是 `said_final -> 不推`，于是这一轮在跑完一个工具后就结束了。
#: 用户看到「又是中途停下，继续都没用」：每点一次「继续」只推进一步。
#:
#: ## 为什么只问一次
#:
#: 打回要花一次模型调用。给 1 次：真做完的轮次多花一次就能收尾；而把
#: FINAL 当口头禅的模型第二次也会被接受（不能无限问下去）。
MAX_FINAL_CONFIRMATIONS = _env_int("SUPEX_CHAT_MAX_FINAL_CONFIRMATIONS", 1)

#: 计划句（"接下来要看主逻辑"）最多打回几次（v1.30）。
#:
#: ## 为什么不和 `MAX_FINAL_CONFIRMATIONS` 共用
#:
#: 线上会话 `chat_01M640EY2P54QC1E2QS54R0R3Z`（用户原话「解题到一半就结束了」）：
#: seq 8 先写了一句「checksec 没输出，我用 readelf 和 pwn 自己看防护」——
#: 那句是计划，被打回一次；seq 10 又写「典型栈溢出。看主逻辑。」—— 同样是
#: 计划，但 `final_confirmations` 已经到 1，闸门直接放行，落成 `answer`，
#: 整轮 `done`。用户看到的仍是停一半。
#:
#: 计划句和"真的做完没有"是两件事：前者只说明**还没动手**，它的额度就该
#: 按"还要推几步"算，而不是按"问没问过"算。这里数的是**连续**计划句：
#: 模型真的执行了工具就清零（见 `run_turn`），只有连续 4 次只写计划不执行
#: 才判定为绕圈。否则 100+ 步的长任务里正常出现的 4 句“接下来…”会把额度
#: 吃光，最后落成「停在了下一步上」。
MAX_PLAN_BOUNCES = _env_int("SUPEX_CHAT_MAX_PLAN_BOUNCES", 4)

#: 对话侧一次模型调用的输出上限（v1.21）。
#:
#: `LlmGateway` 的默认是 4096，那对"一句话回答"够、对"看完反汇编再讲
#: 结论"不够 —— 实测模型会把推理过程写成正文，然后在半句话处被切掉。
#: 8192 不改变协议，只是把天花板抬高；真正的兜底仍是
#: `MAX_TRUNCATION_CONTINUES` 的续跑（抬上限不能消除切断，只能减少它）。
CHAT_MAX_TOKENS = 8192

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

    #: 再跑一遍有没有意义。默认 True —— "多试一次"的代价是几秒，
    #: 而"该重试却没重试"的代价是任务停在半路。
    retryable: bool = True


class ChatProviderAuthError(ChatEngineError):
    """模型接入点**拒绝了我们**（密钥无效/过期/无权限）。

    ★ 为什么要单独一个类：这不是"模型这次答坏了"，而是**配置错了**。
      自动接手再跑一百次都是同一个 401，而每一轮要白等 90 秒心跳窗口，
      用户只会看到同一条错误反复出现三遍（实测 `resume_attempts=3`）。
      所以 `retryable = False`，`resumer` 见到它直接放弃并留下可操作说明。
    """

    retryable = False


def _wrap_model_failure(exc: BaseException) -> ChatEngineError:
    """把底层异常翻译成用户能照着做的失败。

    ## 为什么不能只写类名

    改造前这里是 `f"调用模型失败：{type(exc).__name__}。"` —— 于是界面上
    只有「请求失败，请稍后再试。」，而真实原因是
    `HTTP 401 Invalid token`。用户完全无从判断该改密钥还是该等一等。

    ## 为什么两种文案

    凭证错要给出**下一步动作**（去哪改、改完点什么）；其余错误保留原始
    细节（截断到 300 字），因为那些细节恰恰是排查线索（502/超时/上下文超限）。
    """
    detail = failure_kind.describe(exc)
    if failure_kind.classify(detail) == failure_kind.PROVIDER_AUTH:
        return ChatProviderAuthError(
            "模型接入点拒绝了这次请求（API key 无效或已过期）。"
            "请到「设置 → 模型」里更新这个接入点的密钥，保存后点下面的「重试」，"
            "这一轮会从断点接着跑，不用重新发问。"
            f"（原始信息：{detail}）"
        )
    return ChatEngineError(f"调用模型失败：{detail}")


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
    #: 这一轮是**用户主动停掉**的（v1.25）。`_run_guarded` 据此把执行记录落成
    #: `cancelled` 而不是 `done` —— 后者会被后台当成"跑完了"，而用户的意思
    #: 恰恰是"别再跑了"。
    stopped: bool = False
    #: 这一轮**还没给出可交付结论**（v1.35）：最后停在计划句上、额度已经用尽。
    #:
    #: `_run_guarded` 据此把执行记录落成 `interrupted` 而不是 `done`：
    #: `done` 会被界面和后台都当成"这一问已经答完"，而实际上下一步还没执行。
    #: 不落 `cancelled` —— 那是用户主动停，与"模型自己停在半路"是两件事。
    needs_continue: bool = False


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


def _phase_args_preview(args: dict[str, Any] | None) -> dict[str, str]:
    """phase 帧里带一份**浅层**参数摘要（v1.92）。

    只取标量并截断到 200 字符：完整 args 可能是整份文件内容或附件正文，
    每 1.5 秒推一次会把 SSE 变成传输大对象。嵌套结构一律跳过 —— 界面
    只需要"这条命令大概是什么"，详情在落库的工具消息里。
    """
    if not args:
        return {}
    preview: dict[str, str] = {}
    for key, value in list(args.items())[:12]:
        if isinstance(value, (str, int, float, bool)):
            preview[str(key)] = str(value)[:200]
    return preview


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


#: 原生工具调用合成文本时最多保留几条。
#:
#: ★ 与解题侧 `app/kernel/runner.py` 的 `MAX_ACTIONS_PER_TURN = 5` 对齐：
#:   一轮里真要调 6 个以上工具，说明模型在发散，而不是在执行计划。
MAX_SYNTH_ACTIONS = 5

#: 上游"这一轮只回了工具调用、正文为空"的收尾原因集合。
#:
#: 线上会话 `chat_01M6556ES13EGH8NN712252SPB` 就是 `tool_calls`：网关
#: 回 `delta.tool_calls` + `finish_reason="tool_calls"`，正文为空。
#: 另外两个是 Anthropic / 旧 OpenAI 的同义写法，一起收。
_NATIVE_FINISH_REASONS = frozenset({"tool_calls", "tool_use", "function_call"})


def _has_protocol_marker(text: str) -> bool:
    """正文里已经写了 `ACTION:` / `FINAL:` 吗（即模型自己走了文本协议）。"""
    from app.kernel.runner import _MARKER  # 循环 import 规避

    return bool(_MARKER.search(text or ""))


def _native_action_lines(calls: object) -> list[str]:
    """把归一化的原生工具调用转成 `ACTION:` 行（每行以换行开头）。

    ★ 换行开头是契约：`HoldbackBuffer` 靠行首识别协议起头，行中出现的
      `ACTION:` 会照常推给前端（那就是泄漏）。

    `arguments` 是接入点给的 JSON 字符串（可能不合法 —— 模型写坏 JSON
    是已知形态，解题侧 `parse_actions` 同一容忍度）；解析失败按 `{}` 走，
    其它调用照常产出，并留一条日志给排障。
    """
    items = [call for call in (calls or ()) if isinstance(call, dict)]
    if len(items) > MAX_SYNTH_ACTIONS:
        _log.warning(
            "chat.native_tool_calls 一轮回了 %s 个工具调用，只保留前 %s 个",
            len(items),
            MAX_SYNTH_ACTIONS,
        )
        items = items[:MAX_SYNTH_ACTIONS]

    lines: list[str] = []
    for call in items:
        name = str(call.get("name") or "")
        arguments = call.get("arguments")
        args: dict = {}
        if isinstance(arguments, str) and arguments.strip():
            try:
                parsed = json.loads(arguments)
            except ValueError:
                _log.warning(
                    "chat.native_tool_calls 参数不是合法 JSON，按空对象处理 name=%s",
                    name,
                )
            else:
                if isinstance(parsed, dict):
                    args = parsed
        # ★ 有的网关把整个动作对象塞进 `arguments`（`{"tool": ..., "args": ...}`），
        #   而不是只塞参数 —— 取内层，否则会变成 `tool=name` 而参数套一层壳。
        if "tool" in args and "args" in args:
            name = str(args.get("tool") or name)
            inner = args.get("args")
            args = inner if isinstance(inner, dict) else {}
        if not name:
            _log.warning("chat.native_tool_calls 工具调用没有名字，已丢弃")
            continue
        lines.append(
            "\nACTION: " + json.dumps({"tool": name, "args": args}, ensure_ascii=False)
        )
    return lines


def _with_native_actions(reply: Any) -> Any:
    """上游只回了原生工具调用时，补合成 `ACTION:` 文本（v1.33）。

    ★ **不**改 `LlmReply.tool_calls`：那是给排障/审计看的"网关原样"，
      合成只发生在这里 —— 对话页唯一需要文本协议的地方。
    ★ 正文里已经有 `ACTION:`/`FINAL:` 时**不**合成：模型自己走了文本协议，
      再加一行就是重复调用同一个工具。
    """
    calls = tuple(getattr(reply, "tool_calls", ()) or ())
    if not calls:
        return reply
    text = getattr(reply, "text", "") or ""
    if _has_protocol_marker_or_xml(text):
        return reply
    lines = _native_action_lines(calls)
    if not lines:
        return reply
    return replace(reply, text=text + "".join(lines))


def _native_names(reply: Any) -> list[str]:
    """回复里原生工具调用的名字（给用户看的提示语用，去重保序）。"""
    names: list[str] = []
    for call in getattr(reply, "tool_calls", ()) or ():
        name = str((call or {}).get("name") or "") if isinstance(call, dict) else ""
        if name and name not in names:
            names.append(name)
    return names


def _supports_tool_calls(stream_fn: Any) -> bool:
    """这个 `chat_stream` 认不认 `tool_calls=` 这个可选出口。

    ★ 为什么不用"先传再捕获 TypeError"：`async for` 里的 `TypeError`
      分不清是"参数不认识"还是"流内容坏了"，而后者的语义是"退回非流式
      重试"。签名探测把两件事分开。
    """
    return _supports_param(stream_fn, "tool_calls")


def _supports_param(fn: Any, name: str) -> bool:
    """`fn` 的签名里有没有名为 `name` 的参数（含 `**kwargs` 兜底）。"""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


async def _gateway_chat(
    gateway: Any,
    messages: list[dict[str, str]],
    *,
    system: str,
    tools: list[dict] | None = None,
) -> Any:
    """非流式调模型，`tools` 只在网关认这个参数时才传。

    ★ 与 `_supports_param` 同一理由：旧网关 / 测试假网关的 `chat()` 可能
      根本没收 `tools` 形参，硬传会 `TypeError`，而那是"每个非流式对话
      都炸"的级别。认才传，不认就走改造前那条路（纯文本协议）。
    """
    if tools and _supports_param(getattr(gateway, "chat", None), "tools"):
        return await gateway.chat(messages, system=system, tools=tools)
    return await gateway.chat(messages, system=system)


async def _call_model(
    gateway: Any,
    messages: list[dict[str, str]],
    *,
    system: str,
    emit: Emit | None,
    session_id: str | None = None,
    tools: list[dict] | None = None,
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
        # v1.83：空响应重试。接入点回了 2xx 但没有任何内容时，重发一次
        # 比让这一轮直接结束更划算；次数来自 `llm_retry_config`。
        policy = getattr(gateway, "retry_policy", None)
        empty_retries = int(getattr(policy, "empty_retries", 0) or 0)
        interval = int(getattr(policy, "empty_interval_ms", 0) or 0)
        reply = None
        for attempt in range(empty_retries + 1):
            reply = _with_native_actions(
                await _gateway_chat(gateway, messages, system=system, tools=tools)
            )
            if reply.text.strip() or getattr(reply, "tool_calls", ()):
                return reply
            if attempt < empty_retries and interval > 0:
                from app.providers.retry import backoff_ms

                await asyncio.sleep(backoff_ms(attempt, interval) / 1000)
        assert reply is not None
        return reply

    from app.chat.holdback import HoldbackBuffer
    from app.kernel.runner import LlmReply, parse_usage  # 循环 import 规避

    buffer = HoldbackBuffer()
    collected: list[str] = []
    #: `chat_stream` 把收尾帧里的 finish_reason `append` 到这里（v1.13）。
    #: 用 list 是因为生成器只能 yield 正文，没有第二个返回值 —— 见
    #: `LlmGateway.chat_stream` 的 docstring。
    finish_reasons: list[str] = []
    #: ★ 2026-10-30：`chat_stream` 按 `index` 累积原生工具调用，收流后写进来。
    #:   这里不合成文本 —— 合成在 `_with_native_actions` 里做（只此一处）。
    tool_calls_out: list[dict] = []
    #: ★ v1.92：收尾帧里的 usage（可能多帧，取最后一个）。
    usage_frames: list[dict] = []
    pushed = False
    stream_kwargs: dict[str, Any] = {
        "system": system,
        "finish_reason": finish_reasons,
    }
    # ★ `tool_calls` 是新增的可选输出通道。旧实现（以及大量测试里的
    #   假网关）只接受 `system` / `finish_reason` —— 不看签名就传 kwarg
    #   会直接 `TypeError`，而那会被外层 `except` 误判成"流式坏了"，
    #   于是每次对话都白跑一次非流式重试。
    stream_fn = getattr(gateway, "chat_stream", None)
    if stream_fn is None:
        # 旧网关 / 测试里的假网关没有 `chat_stream`：退回非流式。
        # 少了这一支，任何"只实现 chat()"的接入点在 emit 非空时都会直接
        # AttributeError —— 而 docstring 里承诺的正是"最坏退化为非流式"。
        return _with_native_actions(
            await _gateway_chat(gateway, messages, system=system, tools=tools)
        )
    if stream_fn is not None and _supports_tool_calls(stream_fn):
        stream_kwargs["tool_calls"] = tool_calls_out
    # ★ v1.64：原生工具调用。把 tools 定义透传给 chat_stream（若它认这个参数）。
    #   `_supports_param` 判断签名里有没有 `tools`，避免旧网关/假网关 TypeError。
    if tools and stream_fn is not None and _supports_param(stream_fn, "tools"):
        stream_kwargs["tools"] = tools
    stream_iter = gateway.chat_stream(messages, **stream_kwargs).__aiter__()
    try:
        # ★ v1.67：逐帧"空闲超时"。不再直接 `async for delta in ...`，而是手动
        #   驱动迭代器、给每次 `__anext__` 套一个 `asyncio.wait_for(..., 空闲秒数)`。
        #   这样"上游挂着连接却永远不吐正文"（只发 `: keepalive` 注释，被
        #   `runner._iter_sse` 跳过）会在 `STREAM_IDLE_SECONDS` 后抛
        #   `asyncio.TimeoutError`，走下面 `except` 的"没吐过字就退回非流式"；
        #   而非流式也有 httpx 的 `DEFAULT_LLM_TIMEOUT=180` 兜底，最终会失败并被
        #   `_run_guarded_active` 记成 `interrupted`、交给 resumer 接手 ——
        #   绝不无限阻塞（对应 CyberStrikeAI 的 bounded agent waits）。
        #   每个**正文**增量都会重新计时，所以真在吐字的慢模型不会误伤。
        while True:
            try:
                delta = await asyncio.wait_for(
                    stream_iter.__anext__(),
                    timeout=STREAM_IDLE_SECONDS or None,
                )
            except StopAsyncIteration:
                break
            except TimeoutError:
                raise ChatEngineError(
                    f"模型流 {STREAM_IDLE_SECONDS}s 无正文增量（上游卡死）"
                ) from None
            # ★ v1.25：每个增量查一次"用户有没有按中断"。按停得**立刻**
            #   生效 —— 这是"中断并继续"与"插话"的根本区别（后者要等下一轮）。
            if session_id and interrupt_bus.should_break(session_id):
                break
            collected.append(delta)
            safe = buffer.feed(delta)
            if safe:
                pushed = True
                await _emit_delta(emit, safe)
        tail = buffer.flush()
        if tail and not (session_id and interrupt_bus.should_break(session_id)):
            pushed = True
            await _emit_delta(emit, tail)
    except Exception:  # noqa: BLE001
        if pushed:
            # 已经吐出字了 —— 不能重来，只能如实失败。
            raise
        _log.info("流式调模型失败，退回非流式重试一次", exc_info=True)
        return _with_native_actions(
            await _gateway_chat(gateway, messages, system=system, tools=tools)
        )
    finally:
        # ★ v1.67：无论正常结束、break 还是超时/异常，都必须关掉底层生成器，
        #   否则 httpx 那条 SSE 连接会泄漏（超时场景尤其关键 —— 上游还挂着连接）。
        aclose = getattr(stream_iter, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001 - 关闭失败不该盖过真正的结果/异常
                _log.debug("关闭模型流迭代器失败", exc_info=True)

    raw_text = "".join(collected)
    return _with_native_actions(
        LlmReply(
            text=raw_text,
            source="content",
            raw={},
            usage=parse_usage(usage_frames[-1] if usage_frames else {}),
            # ★ 取最后一个取值：正常只有一帧带它，但代理/网关吐多帧时，
            #   以最后那个为准（更接近真实收尾原因）。
            finish_reason=finish_reasons[-1] if finish_reasons else "",
            tool_calls=tuple(tool_calls_out),
        )
    )


# ══════════════════════════════════════════════════════════
# 截断判据（v1.13）
# ══════════════════════════════════════════════════════════


def _last_user_asks_continue(history: list[Any]) -> bool:
    """最近一条用户消息是不是明确要求继续。"""
    for message in reversed(history):
        if getattr(message, "role", None) != "user":
            continue
        content = getattr(message, "content", "") or ""
        return any(word in content for word in ("继续", "接着", "往下", "渗透"))
    return False


#: 只认**真正的登录/口令尝试**，不认 `Authorization: Bearer ...`。
#:
#: ★ v1.87.1：旧正则包含 `auth`，于是任何带认证头的已授权请求（例如
#:   `curl -H "Authorization: Bearer svx_..." /settings`）都会被算作口令尝试；
#:   10 次后就触发硬上限，把认证后的 API 探测全部拦死。这不是目标行为，
#:   是分类器误判。这里保留登录/爆破词，忽略 Authorization/credentials 这类
#:   表示“已认证访问”的词。
_AUTH_TOOL_PATTERN = re.compile(
    r"(?i)(\blogin\b|\bsignin\b|\bpassw(?:or)?d\b|brute|candidate|\bhydra\b|\bmedusa\b|\bpatator\b)"
)


_CODE_WRITE_RE = re.compile(
    r"(?is)(<<\s*['\"]?[A-Za-z_][\w-]*|(?:^|[;&|]\s*)(?:cat|tee|printf|echo)\s+[^\n|]*>)"
)


def _looks_like_auth_attempt(name: str, args: dict[str, Any]) -> bool:
    """粗略判断这次工具调用是不是登录/口令尝试。

    只用于**爆破停损**，不作为安全判据。误判的代价是把认证后的正常 API
    请求拦死（v1.87.1 实测：Authorization 头被 `auth` 命中，10 次就误停）；
    漏判的代价是继续烧钱爆破。因此只认登录/口令词，不认认证头。

    ★ 2026-11-13：**写脚本不算尝试登录**。线上把
      `cat > dev.py <<'PY' ... login/password ... PY` 连续算成 10 次口令
      尝试，随后把所有 run_shell 都拦死。命令正文里出现 login 只是代码
      内容，不是这次动作在发认证请求。heredoc / 重定向写文件先排除。
    """
    if name not in {"run_shell", "verify_http"}:
        return False
    command = str(args.get("command") or "")
    if name == "run_shell" and _CODE_WRITE_RE.search(command):
        return False
    return bool(_AUTH_TOOL_PATTERN.search(f"{name} {args}"))


#: 会真正改变“测过什么”的工具。它们才算一次独立探测；
#: `record_coverage` / `list_*` / `load_*` 是记账或读资料，不算。
_PROBE_TOOL_NAMES = frozenset({"run_shell", "browser", "verify_http"})


def _is_probe_action(name: str) -> bool:
    return name in _PROBE_TOOL_NAMES


def _action_signature(name: str, args: Any) -> str:
    """一个动作的稳定签名，用于"原地打转"检测（v1.49）。

    同一工具 + **参数完全相同** = 同一个动作。用 `json.dumps(sort_keys=True)`
    保证字典顺序不影响判等：`{"a":1,"b":2}` 与 `{"b":2,"a":1}` 是同一个命令，
    必须得到同一个签名，否则"模型每次把参数换个顺序写"就能绕过检测。

    ★ 参数里的**文本内容**要逐字参与判等 —— 「同一条命令连跑 N 次」的定义就是
      命令文本一模一样；只要命令改了一个字节（换个路径、加个参数），
      就是"尝试了不同策略"，不该算打转。所以不做任何归一化/截断。
    """
    try:
        rendered = json.dumps(args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        # 参数不是可 JSON 化的（理论上不该发生，动作都来自 JSON 解析），
        # 退化成 repr —— 宁可保守也算出一个签名，不要让检测因为一个怪参数崩掉。
        rendered = repr(args)
    return f"{name}\x00{rendered}"


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


def _strip_legacy_action_narration(text: str) -> str:
    """剥掉旧文本协议时代模型自己写下的「写 ACTION：/ 执行：」叙述残渣（v1.64.2）。

    ## 为什么要这个

    迁移到原生 tool-calling 之后，模型不再需要手写 `ACTION: {...}`。但**老会话**
    的 assistant 历史是文本协议时代留下的 —— 那时模型常常在正文里刷出一片纯占位
    碎句，例如线上会话 `chat_01M6H1JNW3S2010NB0474PN369`（「ctf」）实测：

        上传成功后的路径是 … 先确认 … 测试 `.user.ini` 上传。

        执行：

        写 ACTION：

        执行。

        现在只写 ACTION JSON 行：

        执行：

        写 ACTION：

        执行。

        我现在测试 `.user.ini` 与 gif 后缀执行。…

    那些「执行：」「写 ACTION：」「执行。」「现在只写 ACTION JSON 行：」是**当年**模型
    想发动作却没真发出去、只把"打算"写成正文的碎渣。它们不含任何 JSON、不含命令，
    是纯噪声。

    ## 为什么要在**回喂历史**时剥

    模型会**照抄它自己的历史**。即使这轮的系统提示词已经改成原生调用、当前工具调用
    也是干干净净的原生 `tool_calls`（实测 `tool_args` 是 `{"command": "…"}`），模型
    看到历史里一屏「写 ACTION：」，就会继续在正文里写「写 ACTION：」——历史污染，
    界面上看着像"又坏了"，其实工具一直在正常执行。把历史里的残渣剥掉，模型就没有
    坏模板可抄了。

    ## 剥什么 / 不剥什么（保守）

    只删**整行就是占位碎句**的行（去空白和常见尾标点后，等于 "执行" / "写ACTION" /
    "现在只写ACTIONJSON行" 之类，且**不含 `{`、不含命令字符**）。理由：
    · 一行里但凡有 `{`（真的 JSON）就不删 —— 那是有效动作；
    · 一行里但凡有其它实义文字就不删 —— 只处理"整行纯占位"这一种。
    删完把连续空行压成一个，避免留一堆空行。

    ★ 只作用于**喂给模型的 messages**（`_render_history`）。落库的 `content` 原样保留，
      界面上历史气泡显示的还是当时的原文 —— 不改写用户看到的历史。
    """
    lines = text.split("\n")
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        # 归一：去掉常见尾标点/空白再做比对。
        core = stripped.rstrip("：:。. 　")
        core_nospace = core.replace(" ", "").replace("　", "")
        # 仅当「整行是占位碎句」且不含 JSON/命令迹象时才丢。
        is_stub = (
            "{" not in stripped
            and core_nospace in _LEGACY_NARRATION_STUBS
        )
        if is_stub:
            continue
        kept.append(line)
    # 压连续空行：两次以上空行折成一次。
    out: list[str] = []
    blank = False
    for line in kept:
        if line.strip() == "":
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(line)
    return "\n".join(out).strip()


#: 「整行纯占位」的旧叙述碎句全集（去空白、去尾标点后比对）。v1.64.2。
_LEGACY_NARRATION_STUBS = frozenset(
    {
        "执行",
        "执行下面",
        "写ACTION",
        "写ACTIONJSON行",
        "现在只写ACTION",
        "现在只写ACTIONJSON行",
        "我现在只写ACTIONJSON行",
        "现在写ACTION",
        "接下来写ACTION",
        "ACTION",
    }
)


def _recent_assistant_texts(rows: list[Any], *, window: int = 12) -> list[str]:
    """从历史消息里取最近 `window` 条 assistant 正文（旧→新），供「同一结论复读」判据用。

    ## 为什么**不**只看 `surface=="answer"`（v1.69 实测教训）

    带动作的 assistant 消息落库时 `surface="process"`（见 `run_turn` 的
    surface 赋值）—— 而"反复给出同一句结论"恰恰发生在这些**叙述**里（每轮都带
    一个不同的真实 ACTION，正文却是同一句"没拿到 flag、需要 KVM"）。
    只看 `answer` 会漏掉全部复读样本、判据永不触发（集成测试当场挂死就是这个）。
    所以这里取所有 assistant 正文；`content` 已是 `_narration_before_action` 后的
    人类叙述（动作被剥掉），正是要比对的东西。
    """
    texts = [
        (getattr(r, "content", "") or "")
        for r in rows
        if getattr(r, "role", "") == "assistant"
    ]
    return [t for t in texts if t.strip()][-window:]


def _render_history(rows: list[Any]) -> list[dict[str, str]]:
    """把库里的消息渲染成 LLM 的 `messages` 数组。

    ## 三种角色怎么映射
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
            # ★ v1.64.2：回喂前剥掉**旧文本协议时代的叙述残渣**。
            #   见 `_strip_legacy_action_narration` 的长注释 —— 老会话里
            #   模型自己写过成片的「执行：/ 写 ACTION：/ 执行。」占位碎句，
            #   迁移到原生调用后这些碎句仍是**历史**，模型会照抄自己的旧
            #   写法（历史污染），界面上继续刷「写 ACTION：」。只清洗**喂给
            #   模型的历史**，不动落库正文、不动界面显示。
            clean = _strip_legacy_action_narration(content)
            # 空正文的 assistant 消息（模型只调了工具、没说话）不送 ——
            # 一个空的 assistant turn 在部分 provider 上是 400。
            if clean.strip():
                out.append({"role": "assistant", "content": clean})
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
    # ★ v1.53：滑窗可能从会话中间切开，让首条是 assistant（残句）或 tool。
    #   Anthropic 风格 Messages API **要求首条是 user**（否则 400），而平台
    #   明说"国内所有大模型均可用"；即便 provider 宽容，模型也会凭空接话
    #   （看不到用户最早的问题）。丢掉开头的非 user 消息（都是半截上下文，
    #   丢了无害）。
    while out and out[0]["role"] != "user":
        out.pop(0)
    return _trim_history_to_budget(out)


def _trim_history_to_budget(out: list[dict[str, str]]) -> list[dict[str, str]]:
    """按**总字符**再收一道，超了就丢最老的工具结果（v1.26）。

    ## 为什么条数上限不够

    一条 8000 字的工具输出和一条 3 字的消息在"条数"上等价，在 token 上
    差两千倍。只卡条数 = 把一次调用的成本交给运气，而运气差的那次就是
    `context_length_exceeded`（整轮直接失败，用户看到"又没反应"）。

    ## 丢的顺序（有讲究）

    1. **先丢最老的 `[工具结果]`** —— 它们最占地方，而且最新一轮的模型
       调用通常只需要"最近几步 + 结论"，早期的原始输出价值递减。
    2. 工具都丢完了还超，再从最老的普通消息丢。
    3. **永远保留最后一条**（模型要回的就是它）和第一条 user（用户的问题）。

    丢过东西时在**第一条**消息上挂一句说明，让模型知道"上面被省略了"，
    而不是以为自己漏看了什么。
    """
    total = sum(len(m["content"]) for m in out)
    if total <= MAX_HISTORY_CHARS or len(out) <= 1:
        return out

    keep = [True] * len(out)
    last = len(out) - 1
    first_user = 0 if out and out[0]["role"] == "user" else None

    def droppable(index: int) -> bool:
        if index == last:
            return False
        # 第一条 user 消息永远保留（它是这轮问题的锚点）。
        return first_user is None or index != first_user

    # 第一轮：只丢工具结果，从最老的开始。
    for index in range(len(out)):
        if total <= MAX_HISTORY_CHARS:
            break
        if droppable(index) and keep[index] and out[index]["content"].startswith("[工具结果]"):
            total -= len(out[index]["content"])
            keep[index] = False

    # 第二轮：还不够就从最老的开始丢其它消息。
    for index in range(len(out)):
        if total <= MAX_HISTORY_CHARS:
            break
        if droppable(index) and keep[index]:
            total -= len(out[index]["content"])
            keep[index] = False

    dropped = keep.count(False)
    if dropped == 0:
        return out

    trimmed = [m for m, k in zip(out, keep, strict=False) if k]
    note = "（上下文已省略较早的 " + str(dropped) + " 段内容以控制长度。如需要，可用工具重新读取。）"
    trimmed[0] = {
        "role": trimmed[0]["role"],
        "content": note + chr(10) + chr(10) + trimmed[0]["content"],
    }
    return trimmed


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

    __slots__ = ("db", "session", "identity", "mcp", "allowed_tools", "gateway")

    def __init__(
        self,
        *,
        db: Session,
        session: ChatSession,
        identity: Any,
        mcp: mcp_tools.McpToolCatalog | None = None,
        allowed_tools: set[str] | None = None,
        gateway: Any = None,
    ) -> None:
        self.db = db
        self.session = session
        self.identity = identity
        #: 本轮从会话挂载的 MCP server 发现出来的工具目录。
        #:
        #: ★ 放在 ctx 上而不是全局缓存：用户随时可能在另一个标签页改了挂载，
        #:   这一轮就应当按最新的来；而同一个会话的两轮之间也可能不同的
        #:   server 掉线。每轮重新发现是"所见即所得"最便宜的实现。
        self.mcp = mcp or mcp_tools.McpToolCatalog()
        #: None = 不限制；集合 = 只允许这些内置工具（v1.83）。
        self.allowed_tools = allowed_tools
        #: 本轮使用的网关。`spawn_subagents` 要拿它启动子代理；
        #: 其余工具不需要。
        self.gateway = gateway


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
            agent_task_id=getattr(ctx.session, "agent_task_id", None),
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
    except ApiError as exc:
        if exc.code == "CTF2_NOT_BOUND":
            # ★ 明确告诉模型"重试无用"：这条错误来自**配置**，不是瞬时故障。
            #   2026-11-08 实测：账号解绑后模型先重试了一次，接着从沙箱里
            #   探测 fake-ip 端口想绕开平台 —— 整轮白跑。
            return _err(
                "受理失败：尚未绑定 CTF2 账号。**这不是重试能解决的** —— "
                "请让用户在「CTF」页绑定 PAT，绑定后重新发起。"
                "不要改用直连靶机或内网地址的方式绕过。"
            )
        return _err(f"受理失败：{exc}")
    except Exception as exc:  # noqa: BLE001
        return _err(f"受理失败：{exc}")
    return (
        f"已受理题目 `{challenge.id}`（{challenge.title or '无标题'}）。"
        "它在后台跑，过程去「解题」页看。"
    )


def _brief_text(text: str | None, limit: int = 320) -> str:
    """把题面压成一行：折叠换行、截断。

    题面常常是多段 markdown（含代码块）。列表里逐题铺原文会把 30 道题
    撑成几千行；模型需要的是"这题在问什么"，完整内容可以在真要动手时
    再从平台详情/本地同步快照里取。
    """
    if not text:
        return ""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


async def _tool_fetch_competition_challenges(
    ctx: _ToolContext, args: dict[str, Any]
) -> str:
    """列出**比赛平台**（羊城杯）的题，可选择带上某道题的靶机连接信息。

    ## 和 `list_challenges` 的区别（最容易被混淆的一处）

    * `list_challenges` —— 本平台 `KernelChallenge` 题库（`/ctf2` 看板
      那条链路，我们自己的题）。
    * **本工具** —— 比赛平台（`ycb-test.dasctf.com`）的题，就是比赛当天
      真正要刷的那批。

    用户说"看看有哪些题""第几题开放了没"时，比赛场景要用**这个**。

    ## 参数

    * `remoteId`（可选）—— 给了就顺带查这道题的靶机环境（IP/端口/账号
      密码/是否就绪），省去模型再调一次工具。
    """
    from app.chat import competition

    try:
        tasks = await competition.list_tasks(ctx.db)
    except LookupError:
        return _err(
            "当前账号没有指向比赛平台。请先在「设置 → 解题 / 比赛平台」里"
            "绑定比赛平台的 AccessKey（或启用一个平台配置），再用这个工具。"
        )
    except Exception as exc:  # noqa: BLE001 - 网络/协议错误都变成人话
        return _err(competition.friendly_driver_error(exc, "拉比赛平台题库"))

    if not tasks:
        return "比赛平台这边还没有题（可能还没放题，或者全部已结束）。"

    solved = sum(1 for t in tasks if t.solved)
    open_count = sum(1 for t in tasks if t.is_open)
    lines = [f"比赛平台共 {len(tasks)} 道题：已解 {solved} 道，已开放 {open_count} 道。"]
    # ★ v1.76：题面与附件地址从**本地同步快照**补齐（零网络成本）。
    #   列表接口只有 id/标题/分值，模型以前根本看不到题目在问什么；
    #   而库里 24/30 道题的附件 URL 其实都有。
    from app.kernel.service import get_challenge_by_remote_id

    for task in tasks:
        flags = []
        if task.solved:
            flags.append("✅已解")
        if not task.is_open:
            flags.append("🔒未开放")
        score = f" {task.score}分" if task.score else ""
        cat = f"[{task.category}] " if task.category else ""
        suffix = f"（{' / '.join(flags)}）" if flags else ""
        lines.append(f"- `{task.remote_id}` {cat}{task.title}{score}{suffix}")

        local = get_challenge_by_remote_id(ctx.db, task.remote_id)
        if local is None:
            continue
        brief = _brief_text(local.description)
        if brief:
            lines.append(f"  题面：{brief}")
        for item in local.attachments or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            name = str(item.get("name") or "附件").strip() or "附件"
            lines.append(f"  - 附件：{name} {url}")

    remote_id = str(args.get("remoteId") or args.get("remote_id") or "").strip()
    if remote_id:
        lines.append("")
        try:
            env, prompt_text = await competition.read_env(ctx.db, remote_id)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"（查题目 `{remote_id}` 的靶机环境失败：{exc}）")
        else:
            if prompt_text:
                lines.append(f"题目 `{remote_id}` 的靶机：")
                lines.append(prompt_text)
            elif env.needs_init:
                lines.append(f"题目 `{remote_id}` 的靶机还没起，需要先启动环境。")
            else:
                lines.append(f"题目 `{remote_id}` 没有靶机环境（多半是附件/静态题）。")
    return "\n".join(lines)


async def _tool_submit_flag(ctx: _ToolContext, args: dict[str, Any]) -> str:
    """把 flag 提交到**比赛平台**（羊城杯）。

    ## 两件必须做的事（都在这里做，不指望模型记得）

    1. **剥花括号外壳**：赛事要求"只交花括号内的内容"。模型交
       `DASCTF{xxx}` 会被判错、白扣一次额度。这里统一过
       `competition.strip_flag_shell`。
    2. **区分「没受理」和「判错」**：`SubmitOutcome.accepted` 为 False 时
       平台根本没受理（限流/格式/额度用完），**不消耗错误次数** —— 这时
       要让模型知道"可以重试"，而不是"答案错了"。

    ## 参数

    * `remoteId`（必填）—— 题目 id（`fetch_competition_challenges` 列出来的）
    * `flag`（必填）—— 答案。带不带 `DASCTF{}` 外壳都行，这里会剥。
    """
    from app.chat import competition

    remote_id = str(args.get("remoteId") or args.get("remote_id") or "").strip()
    if not remote_id:
        return _err("没有给 `remoteId`。先用 `fetch_competition_challenges` 看题目 id。")
    raw = str(args.get("flag") or args.get("answer") or "").strip()
    if not raw:
        return _err("没有给 `flag`（答案）。")

    answer = competition.strip_flag_shell(raw)
    if not answer:
        return _err("剥掉花括号后答案是空的，检查一下你交的是什么。")

    try:
        limit = competition.answer_max_length(ctx.db)
    except LookupError:
        return _err(
            "当前账号没有指向比赛平台，交不了 flag。请先在设置里绑定比赛平台。"
        )
    except Exception:  # noqa: BLE001 - 读上限失败不该挡住提交
        limit = 256
    if len(answer) > limit:
        return _err(
            f"答案太长（{len(answer)} 字符，平台上限 {limit}）。"
            "检查是不是多带了提示语或换行 —— 只交答案本身。"
        )

    try:
        outcome = await competition.submit(ctx.db, remote_id, answer)
    except LookupError:
        return _err("当前账号没有指向比赛平台，交不了 flag。请先在设置里绑定比赛平台。")
    except Exception as exc:  # noqa: BLE001
        return _err(competition.friendly_driver_error(exc, "提交到比赛平台"))

    shown = answer if len(answer) <= 80 else answer[:77] + "…"
    if outcome.is_solved:
        return (
            f"✅ 正确！题目 `{remote_id}` 已解出（提交内容 `{shown}`）。"
            "这是最终答案，不要再重复提交同一道题。"
        )
    if outcome.already_solved:
        return f"题目 `{remote_id}` 在这之前就已经解出了（提交内容 `{shown}`），不用再交。"
    if not outcome.accepted:
        # 平台没受理 —— 不消耗错误次数，可以重试。
        return (
            f"⚠️ 平台没受理这次提交（题目 `{remote_id}`），**没有消耗错误次数**，"
            f"可以稍后重试。平台原话：{outcome.message or '（无）'}"
        )
    # 受理了但判错：真的错了一次。
    return (
        f"❌ 平台判定为错误答案（题目 `{remote_id}`，提交内容 `{shown}`）。"
        f"答案不对，别原样再交 —— 回去再核一遍。{('平台原话：' + outcome.message) if outcome.message else ''}"
    )


# ══════════════════════════════════════════════════════════
# v1.71：赛事自动托管（多模型赛跑）—— 对话里下指令就能起整场
# ══════════════════════════════════════════════════════════
#
# 这三个工具复用 `app/ctf_race/manager.py` 的**进程内单例** —— 与 CTF 页
# 的 `/ctf-race/*` 面板是**同一场**。所以对话里起的那场，打开 CTF 页能
# 看到同样的进度；反过来面板上起的，对话里 `competition_race_status`
# 也查得到。同一时刻只允许一场（多场=多倍容器+多倍额度，且面板只显示一场）。
#
# ⚠️ 起赛跑是**真活**（连平台、起容器、烧模型额度）：`start` 是
#   consequential（走审批闸，见 CONSEQUENTIAL_TOOL_NAMES）。`status` 只读。


def _race_snapshot_brief(snap: dict) -> str:
    """把 manager 的完整快照压成一段给人看的中文摘要（工具回执）。"""
    if not snap.get("running"):
        report = snap.get("report")
        if report:
            return _race_report_brief(report)
        return "当前没有在跑的赛事托管。"
    lines = ["赛事托管进行中："]
    challenges = snap.get("challenges") or {}
    # coordinator.snapshot() 形状：{remote_id: {stage, winner_model, ...}} 或 {rounds,...}
    rounds = challenges.get("rounds") if isinstance(challenges, dict) else None
    if rounds is not None:
        lines.append(f"- 第 {rounds} 轮")
    per = challenges.get("challenges") if isinstance(challenges, dict) else None
    if isinstance(per, dict):
        solved = sum(1 for c in per.values() if (c or {}).get("stage") == "solved")
        total = len(per)
        lines.append(f"- 已解 {solved}/{total}")
        for rid, c in per.items():
            c = c or {}
            stage = c.get("stage", "?")
            title = c.get("title") or rid
            line = f"  · {title}（{stage}）"
            if c.get("flag"):
                line += f" flag={c['flag']}"
            if c.get("last_error"):
                line += f" 备注：{c['last_error']}"
            lines.append(line)
    cost = snap.get("cost") or {}
    if isinstance(cost, dict) and cost.get("totalCostUsd") is not None:
        lines.append(f"- 累计成本 ${cost.get('totalCostUsd'):.4f}")
    return "\n".join(lines)


def _race_report_brief(report: dict) -> str:
    lines = [
        "赛事托管已结束：",
        f"- 共 {report.get('total', 0)} 道，解出 {report.get('solved', 0)}，"
        f"失败 {report.get('failed', 0)}，跳过 {report.get('skipped', 0)}",
    ]
    if report.get("stopped_reason"):
        lines.append(f"- 结束原因：{report['stopped_reason']}")
    by = report.get("by_challenge") or {}
    for rid, c in by.items():
        c = c or {}
        if c.get("flag"):
            lines.append(f"  · {c.get('title') or rid}：已解出 flag={c['flag']}")
    return "\n".join(lines)


async def _tool_start_competition_race(ctx: _ToolContext, args: dict[str, Any]) -> str:
    from app.ctf_race import manager as race_manager

    _need_scope(ctx, "start_competition_race")

    models_arg = args.get("models")
    if isinstance(models_arg, str):
        models_list = [m.strip() for m in models_arg.split(",") if m.strip()]
    elif isinstance(models_arg, (list, tuple)):
        models_list = [str(m).strip() for m in models_arg if str(m).strip()]
    else:
        models_list = []

    max_concurrent = _int_arg(args, "maxConcurrent", default=2, low=1, high=8)
    max_bumps = _int_arg(args, "maxBumps", default=3, low=0, high=10)
    no_submit = bool(args.get("noSubmit") or args.get("no_submit") or False)
    raw_budget = args.get("maxBudgetUsd", args.get("max_budget_usd"))
    max_budget: float | None = None
    if raw_budget not in (None, ""):
        try:
            max_budget = float(raw_budget)
            if max_budget < 0:
                max_budget = None
        except (TypeError, ValueError):
            max_budget = None

    try:
        snap = await race_manager.start(
            models=tuple(models_list),
            max_concurrent=max_concurrent,
            max_budget_usd=max_budget,
            no_submit=no_submit,
            max_bumps=max_bumps,
        )
    except race_manager.RaceAlreadyRunning:
        return "已经有一场赛事托管在跑，先停掉再来（用 `stop_competition_race`），或者用 `competition_race_status` 看进度。"
    except LookupError:
        return _err(
            "当前账号没有指向比赛平台，起不了赛事托管。"
            "请先在「设置 → 解题 / 比赛平台」里绑定比赛平台的 AccessKey。"
        )
    except Exception as exc:  # noqa: BLE001
        from app.chat import competition as competition_mod

        return _err(competition_mod.friendly_driver_error(exc, "启动赛事托管"))

    return (
        "已启动赛事自动托管（多模型赛跑，整场）。它在后台跑，可能几十分钟。\n"
        "用 `competition_race_status` 查进度，`stop_competition_race` 停。\n\n"
        + _race_snapshot_brief(snap)
    )


async def _tool_competition_race_status(ctx: _ToolContext, args: dict[str, Any]) -> str:
    from app.ctf_race import manager as race_manager

    return _race_snapshot_brief(race_manager.snapshot())


async def _tool_stop_competition_race(ctx: _ToolContext, args: dict[str, Any]) -> str:
    from app.ctf_race import manager as race_manager

    _need_scope(ctx, "stop_competition_race")
    snap = race_manager.stop("对话里手动停止")
    return "已请求停止赛事托管（当前正在跑的工具返回后这一轮就结束）。\n\n" + _race_snapshot_brief(snap)


async def _tool_list_skills(ctx: _ToolContext, args: dict[str, Any]) -> str:
    from app.kernel import skills as skills_mod

    category = str(args.get("category") or "").strip().lower()
    rows = skills_mod.discover_skills()
    if category:
        rows = [s for s in rows if s.category.lower() == category]
    if not rows:
        return "没有符合条件的技能包。"

    # ★ v1.79：可选带出「被加载过几次」。
    #
    # 用户问「哪几篇没被用过」时，原来 `list_skills` 答不了（它只有清单），
    # 模型只好用 `run_shell` 去翻沙箱目录、扫事件表 —— 实测一个这样的问题
    # 跑了 70 轮 / 53 次 shell。这个参数把答案放进一次调用里。
    #
    # 默认关闭：清单本身是"我有哪些技能"，而统计要扫最多 2 万条事件；
    # 每次列清单都顺带扫一遍是纯浪费。
    include_usage = bool(args.get("includeUsage") or args.get("include_usage"))
    usage_by_name: dict[str, Any] = {}
    usage_note = ""
    if include_usage:
        from app.kernel.skill_usage import collect_skill_usage

        report = collect_skill_usage(ctx.db)
        usage_by_name = {row.name: row for row in report.rows}
        loaded = sum(1 for skill in rows if usage_by_name.get(skill.name))
        usage_note = (
            f"\n统计口径：模型调用 `load_skill` 的次数（共扫描 {report.sampled} 条事件）。"
            f"它证明「请过这一篇」，不证明「读进去了」或「照做了」。"
            f"\n本清单里 {len(rows) - loaded} 篇从未被加载，{loaded} 篇至少被加载过一次。"
        )
        if report.truncated:
            usage_note += f"\n⚠️ 只统计了最近 {report.limit} 条事件，更早的没算进来。"

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
            loads = ""
            if include_usage:
                row = usage_by_name.get(skill.name)
                loads = f"，被加载 {row.loads if row else 0} 次"
            lines.append(f"- `{skill.name}`（{tag}{extra}{loads}）：{skill.description}")
    if usage_note:
        lines.append(usage_note)
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


def _request_spec_from_args(data: dict[str, Any]) -> Any:
    """把模型给的 `{url,method,headers,body}` 变成 `HttpRequestSpec`。

    只做形状校验，不判断 URL 是否在授权范围内 —— 那是 scope guard 的事。
    """
    from app.scans import validators

    url = str(data.get("url") or "").strip()
    if not url:
        raise ValueError("url 必填")
    headers = data.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError("headers 必须是对象")
    body = data.get("body")
    if body is not None and not isinstance(body, str):
        raise ValueError("body 必须是字符串")
    return validators.HttpRequestSpec(
        method=str(data.get("method") or "GET").upper(),
        url=url,
        headers={str(key): str(value) for key, value in headers.items()},
        body=body.encode("utf-8") if body else None,
    )


async def _tool_verify_http(ctx: _ToolContext, args: dict[str, Any]) -> str:
    """`verify_http`：对授权目标重放请求，给发现一份可复核的证据（v1.81）。

    ## 为什么放在对话工具里，而不是让模型自己写 curl

    模型写 curl 也能发请求，但它**看不到"稳不稳"**：一次 200 不代表复现，
    一次回显也不代表差异。这个工具把两类确定性判据做在代码里：

    * `replay`：同一请求 N 次，响应指纹（状态/长度/正文哈希/标记）一致；
    * `differential`：对照组 vs 实验组，只认状态码/新增标记/长度差 ≥20%。

    ## 安全

    * 出站走 `GuardedHTTPTransport`（SSRF：DNS 重绑定/内网/metadata）；
    * `trust_env=False`，不读环境代理；
    * `scope="scan"`：只有具备 scan 权限的身份能调用。
    """
    import asyncio

    import httpx

    from app.core.netguard import GuardedHTTPTransport, ensure_guarded
    from app.scans import validators

    def factory() -> httpx.Client:
        client = httpx.Client(
            transport=GuardedHTTPTransport(),
            trust_env=False,
            follow_redirects=False,
            timeout=validators.DEFAULT_TIMEOUT_SECONDS,
        )
        # 与所有出站客户端同一条纪律：装完 transport 再确认 guard 真在。
        ensure_guarded(client)
        return client

    markers = tuple(str(marker) for marker in (args.get("markers") or ()))
    kind = str(args.get("kind") or "replay").strip().lower()
    try:
        if kind == "replay":
            spec = _request_spec_from_args(args)
            outcome = await asyncio.to_thread(
                validators.replay,
                spec,
                attempts=max(2, int(args.get("attempts") or 2)),
                markers=markers,
                client_factory=factory,
            )
        elif kind == "differential":
            control = args.get("control") or {}
            experiment = args.get("experiment") or args
            if not isinstance(control, dict) or not isinstance(experiment, dict):
                raise ValueError("control / experiment 必须是对象")
            outcome = await asyncio.to_thread(
                validators.differential,
                _request_spec_from_args(control),
                _request_spec_from_args(experiment),
                markers=markers,
                client_factory=factory,
            )
        else:
            raise ValueError("kind 只支持 replay / differential")
    except (ValueError, TypeError) as exc:
        return _err(f"参数不对（{exc}）。kind=replay 需要 url；kind=differential 需要 control 与 experiment。")

    return json.dumps(
        {
            "kind": outcome.kind,
            "passed": outcome.passed,
            "confidence": outcome.confidence,
            "summary": outcome.summary,
            "evidence": outcome.evidence,
            "attempts": outcome.attempts,
        },
        ensure_ascii=False,
    )


#: 对话侧写黑板时记在 `recorded_by` 上的作者前缀。
#:
#: ⚠️ 不能写 `human`：这句话是**模型**写的，不是人写的。资产页要能分开
#:   「模型在对话里记的」与「扫描过程中 agent 记的」—— 两者吵起来时，
#:   `recorded_by` 是唯一能判断"该信哪一条"的字段。
_FACT_AUTHOR_PREFIX = "chat:"


def _fact_author(ctx: _ToolContext) -> str:
    """写黑板的作者。列宽 64，会话 id 是 `chat_<ULID>`，加前缀仍在界内。"""
    return f"{_FACT_AUTHOR_PREFIX}{ctx.session.id}"[:64]


def _coverage_slug(surface: str) -> str:
    """把"方法 + 路径 + 关键条件"压成合法的 `fact_key` slug。

    ## 为什么在本地压，而不是让 `normalize_fact_key` 报错

    模型写的是人话（`POST /api/v1/login (form)`），而 `fact_key` 只允许
    `[a-z0-9-/]`。把报错丢回去让它改写，是一次必然发生的额外回合；
    而这里的压缩是**无损的选择**：原话仍留在 `summary` / `detail` 里，
    key 只是归档用的地址。真正需要模型重新决定的只有"分类"，
    而分类由 `outcome` 的前缀决定，不是它猜的。
    """
    return re.sub(r"[^a-z0-9]+", "-", surface.lower()).strip("-")[:120]


async def _tool_record_coverage(ctx: _ToolContext, args: dict[str, Any]) -> str:
    """`record_coverage`：把"测过什么、结论是什么"写进黑板（v1.88）。

    ## 为什么对话侧需要这个工具

    扫描侧的 `record_coverage` 一直存在，渗透提示词也一直要求模型
    "每次失败先记账"—— 但 `CHAT_TOOLS` 里没有这个名字，模型照做只会
    拿到「没有叫 record_coverage 的工具」。于是那条纪律在对话里从未
    真正执行过（2026-11-11 评估列为必修缺口：闭环断在"写"这一半）。

    ## 落库形状

    与扫描侧**同一张表**（`scan_facts`），`scan_id=None`：对话没有扫描，
    但资产页按 `target` 查覆盖度（`assets.service.coverage_for_target`），
    所以只要有 target 就能被看见。`已排除：…` 落 `ruled-out` 类，
    其余落 `surface` 类 —— 正是那个读接口的分组依据。

    ⚠️ 这条记录**不会**在同一轮里回喂给模型（对话没有黑板索引）。
      它的读者是资产页与下一次对同一目标的扫描；想当场复核请用
      `verify_http`。写清楚这一点，比让模型以为"我记了它就会记得"强。
    """
    from app.facts import service as facts_service

    _need_scope(ctx, "record_coverage")

    target = facts_service.normalize_target(str(args.get("target") or ""))
    if not target:
        return _err(
            "`target` 不能为空 —— 黑板按目标归档，没有目标，这条记录谁也查不到。"
        )

    surface = str(args.get("surface") or "").strip()
    outcome = str(args.get("outcome") or "").strip()
    risk = str(args.get("risk") or "").strip()
    if not surface:
        return _err("`surface` 不能为空 —— 写清楚方法 + 路径 + 关键条件。")
    if not outcome:
        return _err("`outcome` 不能为空 —— 形状是 `成立：…` / `已排除：…` / `未测完：…`。")

    slug = _coverage_slug(surface)
    if not slug:
        return _err("`surface` 里没有可用的字母/数字 —— 换个写法（例如 `GET /api/v1/health`）。")

    # ★ 分类只看前缀，不让模型另填一个 category：两处判据必然漂移，
    #   而这里的唯一目的是让"排除了什么"与"测过什么"能分开统计。
    category = "ruled-out" if outcome.startswith("已排除") else "surface"
    try:
        fields = facts_service.validate_fact(
            fact_key=f"{category}/{slug}",
            summary=outcome[:300],
            confidence="confirmed",
        )
    except ValueError as exc:
        return _err(str(exc))

    try:
        fact, created = facts_service.upsert_fact(
            ctx.db,
            scan_id=None,
            target=target,
            recorded_by=_fact_author(ctx),
            fields=fields,
            detail=f"风险：{risk}" if risk else None,
            evidence_excerpt=outcome[:1000],
        )
        ctx.db.commit()
    except Exception as exc:  # noqa: BLE001 - 写入失败要以工具结果回到模型
        ctx.db.rollback()
        return _err(f"写黑板失败：{exc}")

    state = "新建" if created else f"覆盖（第 {fact.version} 版）"
    return (
        f"已记账：`{fact.fact_key}` → 目标 {target}（{category}，{state}）。"
        "资产页的「覆盖度」能看到这条；同一目标的后续扫描会继承它。"
    )


async def _tool_record_fact(ctx: _ToolContext, args: dict[str, Any]) -> str:
    """`record_fact`：把一条认知写进黑板（v1.88）。

    ## 与 `record_coverage` 的分工

    `record_coverage` 回答"这个面测没测过"，`record_fact` 回答"我知道什么"。
    两者落同一张表、靠 `category` 分开：`surface` / `ruled-out` 属于覆盖度，
    `endpoint` / `credential` / `constraint` / `note` 属于事实。这不是
    两个存储，而是**一个黑板的两种问法**。

    ## 为什么要带 target

    跨扫描继承（`facts.load_inherited_facts`）按 `normalize_target` 的键
    比对 —— 没有 target 的那条记录永远继承不到。所以这里宁可报错，
    也不写一条"看着成功、实际孤儿"的行。
    """
    from app.facts import service as facts_service

    _need_scope(ctx, "record_fact")

    target = facts_service.normalize_target(str(args.get("target") or ""))
    if not target:
        return _err("`target` 不能为空 —— 没有目标的事实继承不到，等于没记。")

    fact_key = str(args.get("fact_key") or "").strip()
    summary = str(args.get("summary") or "").strip()
    if not fact_key:
        return _err("`fact_key` 不能为空，形状是 `category/slug`，例如 `ruled-out/settings-token-is-mock`。")
    if not summary:
        return _err("`summary` 不能为空 —— 一句话结论。")

    try:
        fields = facts_service.validate_fact(
            fact_key=fact_key,
            summary=summary,
            confidence=str(args.get("confidence") or "tentative"),
            trust_level=args.get("trustLevel") or args.get("trust_level"),
        )
    except ValueError as exc:
        return _err(str(exc))

    evidence = str(args.get("evidence") or "").strip()
    detail = str(args.get("detail") or "").strip()
    try:
        fact, created = facts_service.upsert_fact(
            ctx.db,
            scan_id=None,
            target=target,
            recorded_by=_fact_author(ctx),
            fields=fields,
            detail=detail or None,
            evidence_excerpt=evidence[:1000] or None,
        )
        ctx.db.commit()
    except Exception as exc:  # noqa: BLE001 - 写入失败要以工具结果回到模型
        ctx.db.rollback()
        return _err(f"写黑板失败：{exc}")

    state = "新建" if created else f"覆盖（第 {fact.version} 版）"
    return (
        f"已记账：`{fact.fact_key}` → 目标 {target}"
        f"（{fact.category} / {fact.confidence}，{state}）。"
    )


async def _tool_list_facts(ctx: _ToolContext, args: dict[str, Any]) -> str:
    """`list_facts`：读同一目标的黑板（v1.89）。

    ## 为什么写和读必须成对

    v1.88 先补了 `record_coverage` / `record_fact`（提示词一直要求调用，
    工具表里却没有）。但只写不读的黑板是个单向垃圾桶：模型每轮自己记
    一遍，下一轮又不知道记过什么，还会把已排除的方向重试一遍 ——
    而"防重复劳动"正是黑板存在的全部理由。

    ## 输出口径

    一行一条：`[confidence] fact_key：summary（谁写的）`，按 `updated_at`
    倒序（最新的才是现状）。这里**不**照搬 `render_fact_index` 的
    "ruled-out 置顶 + 40 条预算" —— 那是给 system prompt 的索引块
    （每轮都要渲染，必须便宜且稳定）；工具结果是模型主动要看的一次性
    输出，按时间倒序更符合"现在是什么情况"。
    """
    from app.facts import service as facts_service
    from app.facts.models import ScanFact

    target = facts_service.normalize_target(str(args.get("target") or ""))
    if not target:
        return _err("`target` 不能为空 —— 黑板按目标归档。")

    limit = _int_arg(args, "limit", default=30, low=1, high=100)
    rows = list(
        ctx.db.execute(
            select(ScanFact)
            .where(ScanFact.target == target)
            .order_by(ScanFact.updated_at.desc())
            .limit(limit)
        ).scalars()
    )
    if not rows:
        return f"目标 {target} 的黑板还是空的：没有已记录的事实或覆盖度。"

    lines = [
        f"- [{row.confidence}] {row.fact_key}：{row.summary}"
        + (f"（{row.recorded_by}）" if row.recorded_by else "")
        for row in rows
    ]
    return f"目标 {target} 的黑板（最近 {len(rows)} 条）：\n" + "\n".join(lines)


async def _tool_create_todo(ctx: _ToolContext, args: dict[str, Any]) -> str:
    """`create_todo`：给这次对话加一步计划（v1.89）。

    ## 它和扫描侧的 `create_todo` 不是一回事

    那个是**单次扫描**里的私有待办（内存态、进程重启就没了）；
    这里落 `chat_todos` 表，跨轮、跨重启都在，而且每轮提示词里重新渲染。
    合并成一套会让"计划"同时指两种生命周期，那是概念污染。
    """
    text = str(args.get("text") or "").strip()
    if not text:
        return _err("`text` 不能为空 —— 一句话写一步。")

    row = todo_store.create_todo(ctx.db, session_id=ctx.session.id, text=text)
    if row is None:
        return _err(
            f"这次对话的计划已经有 {todo_store.MAX_TODOS} 条了。"
            "计划表不是日志：先用 `update_todo` 把已完成的收尾，或换一个新会话。"
        )
    return f"已加入计划 #{row.seq}：{row.text}"


async def _tool_list_todos(ctx: _ToolContext, args: dict[str, Any]) -> str:
    """`list_todos`：读回当前计划（提示词里也有一份，这里用于确认）。"""
    rows = todo_store.list_todos(ctx.db, ctx.session.id)
    if not rows:
        return "这次对话还没有计划。长任务可以先用 `create_todo` 写 3~7 步。"
    return "当前计划：\n" + "\n".join(
        f"- #{row.seq} [{row.status}] {row.text}" for row in rows
    )


async def _tool_update_todo(ctx: _ToolContext, args: dict[str, Any]) -> str:
    """`update_todo`：按序号改状态/文字。

    ## 为什么用序号而不是 id

    模型在提示词里看到的是 `#3`，不是 `todo_01J...`。让它去引用一个它
    从没见过的 ULID，就多了一次猜错的机会 —— 而序号是人也能读的。
    """
    try:
        seq = int(args.get("seq"))
    except (TypeError, ValueError):
        return _err("`seq` 要填计划里的序号（`#3` 就填 3）。")

    status = args.get("status")
    text = args.get("text")
    try:
        row = todo_store.update_todo(
            ctx.db,
            session_id=ctx.session.id,
            seq=seq,
            status=str(status).strip() if status else None,
            text=str(text) if text else None,
        )
    except ValueError as exc:
        return _err(str(exc))

    if row is None:
        return _err(f"没有 #{seq} 这条计划。用 `list_todos` 看当前有哪些。")
    return f"#{row.seq} → {row.status}：{row.text}"


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
        # ★ v1.62：http 连接展示 url（没有 command/args）；stdio 展示命令行。
        if str(item.get("type") or "stdio") == "http":
            where = f"http {item.get('url') or '(缺 url)'}"
            hdr = "，带请求头" if item.get("has_headers") else ""
            lines.append(f"- `{item['name']}`：{where}{hdr}{env_note}{allow}")
        else:
            lines.append(
                f"- `{item['name']}`：{item.get('command', '')} "
                f"{' '.join(item.get('args') or [])}{env_note}{allow}"
            )
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
    # ★ v1.62：支持 http 类型（url + headers，不需要 command）。
    conn_type = str(args.get("type") or "stdio").strip().lower() or "stdio"
    url = str(args.get("url") or "").strip()
    if not name:
        return _err("`name` 要给（模型用它来调这条连接）。")
    if conn_type == "http":
        if not url:
            return _err("http 类型的连接要给 `url`（如 https://host/mcp）。")
    elif not command:
        return _err("stdio 连接要给 `command`（拉起 server 的可执行文件，如 npx / python3）。")

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

    headers = args.get("headers") or {}
    if not isinstance(headers, dict):
        return _err("`headers` 应该是一个对象（键值都是字符串）。")
    headers_map = {str(k): str(v) for k, v in headers.items()}

    allowed = args.get("allowedTools") or args.get("allowed_tools")
    if allowed is not None:
        if isinstance(allowed, str):
            allowed = [t.strip() for t in allowed.split(",") if t.strip()]
        if not isinstance(allowed, list):
            return _err("`allowedTools` 应该是字符串列表。")
        allowed = [str(t) for t in allowed]

    proposal = {
        "name": name,
        "type": conn_type,
        "command": command,
        "args": arg_list,
        "env": env_map,
        "url": url,
        "headers": headers_map,
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

    # ★ v1.62：`test_mcp_server` 回的 `tools` 是**工具名字符串列表**（不是 dict）。
    #   这里原写成 `t.get("name")` —— 那是 dict 的用法，对字符串会 AttributeError。
    #   之前没暴露是因为这条工具在测试里从未真跑到"试连成功、有工具"的分支。
    tool_names = "、".join(str(t) for t in tools[:10])
    more = f"（还有 {len(tools) - 10} 个）" if len(tools) > 10 else ""
    return (
        f"已添加 MCP 服务器 `{name}`，试连成功（{verdict.get('elapsed_ms')} ms），"
        f"它提供 {len(tools)} 个工具：{tool_names}{more}。"
        "下一次任务开始时模型就能用它们。"
    )


# ── 子 agent 扇出（v1.86）──────────────────────────────────
#
# 与 CyberStrikeAI 的 `task` 工具同一个边界：主代理把**自包含**的独立任务
# 交给子代理并行做，子代理只返回证据/结论，最终由主代理综合。这里不搬
# 它的角色文件与三种编排，只做最小可用的 fan-out。

#: 子代理最多同时跑几个。超过 4 个时，模型供应商的并发限制/熔断反而会
#: 把整轮拖慢；4 是"能明显提速、又不会把上游打爆"的折中。
SUBAGENT_MAX_TASKS = 4

#: 单个子代理的墙钟上限。到点就取消并如实返回超时。
SUBAGENT_TIMEOUT_SECONDS = 600

#: 子代理允许的工具：只读/侦察为主，避免一次扇出产生四份不受控的写操作。
#: `spawn_subagents` 自己不在里面 —— 禁止嵌套委派（同 CyberStrikeAI 的
#: no_nested_task 中间件）。
#: ⚠️ 只列**真实存在的聊天工具**（见 `prompts.CHAT_TOOL_NAMES`）。
#: `http_request`/`read_attachment` 是解题引擎的工具，对话里没有；把不存在的
#: 名字放进来，子代理会看到"工具不可用"并退回 shell —— 2026-11-11 实测。
SUBAGENT_SAFE_TOOLS: frozenset[str] = frozenset(
    {
        "run_shell",
        "browser",
        "verify_http",
        "list_scans",
        "list_facts",
        "load_skill",
        "load_recipe",
        "list_skills",
    }
)

#: 单个子代理结果回传给主代理的最大字符数。它是证据，不是转储。
SUBAGENT_RESULT_CHARS = 6000


async def _tool_spawn_subagents(ctx: _ToolContext, args: dict[str, Any]) -> str:
    """并行派发独立调查任务给子代理，汇总它们的结论。"""
    if ctx.gateway is None:
        return "失败：当前没有可用的模型网关，不能派子代理。"
    if (ctx.session.title or "").startswith("[子代理]"):
        return "失败：子代理不能继续派子代理（禁止嵌套委派）。"

    raw_tasks = args.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return "失败：tasks 必须是非空数组，每项含 name 与 prompt。"
    if len(raw_tasks) > SUBAGENT_MAX_TASKS:
        return f"失败：一次最多派 {SUBAGENT_MAX_TASKS} 个子代理。"

    prepared: list[tuple[str, str, set[str]]] = []
    for index, item in enumerate(raw_tasks, start=1):
        if not isinstance(item, dict):
            return f"失败：第 {index} 项不是对象。"
        name = str(item.get("name") or f"task-{index}").strip()[:80]
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            return f"失败：子代理 `{name}` 没有 prompt。"
        requested = item.get("tools")
        if isinstance(requested, list) and requested:
            allowed = {str(t) for t in requested} & SUBAGENT_SAFE_TOOLS
            if not allowed:
                return f"失败：子代理 `{name}` 请求的工具都不在允许列表里。"
        else:
            allowed = set(SUBAGENT_SAFE_TOOLS)
        prepared.append((name, prompt, allowed))

    from app.chat import sandbox as chat_sandbox
    from app.core.db import get_session_factory

    async def run_one(name: str, prompt: str, allowed: set[str]) -> tuple[str, str | None]:
        subdb = get_session_factory()()
        sub = None
        try:
            sub = chat_service.create_session(
                subdb,
                created_by=ctx.session.created_by,
                title=f"[子代理] {name}"[:120],
            )
            sub.provider_id = ctx.session.provider_id
            sub.persona = ctx.session.persona
            sub.require_approval = False
            sub.parent_session_id = ctx.session.id
            subdb.commit()

            task_prompt = (
                "你是主对话派出的子代理，只完成下面这项独立任务。\n"
                "不要委派其他子代理；直接使用工具调查/验证。\n"
                "完成后严格按以下格式给结论（主代理会综合你的结果）：\n"
                "## 结论\n## 证据\n## 未完成/限制\n\n"
                f"主对话背景：{(ctx.session.title or '').strip()}\n"
                f"任务：{prompt}"
            )
            await asyncio.wait_for(
                run_turn_durable(
                    subdb,
                    session=sub,
                    content=task_prompt,
                    identity=ctx.identity,
                    gateway=ctx.gateway,
                    explicit_tools=sorted(allowed),
                    allowed_tools=allowed,
                    emit=None,
                ),
                timeout=SUBAGENT_TIMEOUT_SECONDS,
            )
            messages = chat_service.list_messages(subdb, session=sub)
            answers = [
                m
                for m in messages
                if getattr(m, "role", None) == "assistant"
                and getattr(m, "surface", None) != "process"
                and (getattr(m, "content", None) or "").strip()
            ]
            content = (answers[-1].content if answers else "").strip()
            if not content:
                return name, "子代理没有给出结论。"
            return name, content[:SUBAGENT_RESULT_CHARS]
        except TimeoutError:
            return name, f"子代理超时（>{SUBAGENT_TIMEOUT_SECONDS}s）。"
        except Exception as exc:  # noqa: BLE001 - 一个子代理失败不影响其他
            _log.warning("子代理 %s 执行失败", name, exc_info=True)
            return name, f"子代理失败：{type(exc).__name__}: {exc}"
        finally:
            if sub is not None:
                # 子代理会话**保留**：它挂在父会话下面，供之后查看分支过程。
                # 只回收容器；会话/消息是这次扇出的证据。
                try:
                    chat_sandbox.drop_sandbox(sub.id)
                except Exception:  # noqa: BLE001
                    _log.debug("回收子代理沙箱失败", exc_info=True)
            subdb.close()

    results = await asyncio.gather(
        *(run_one(name, prompt, allowed) for name, prompt, allowed in prepared)
    )
    sections = []
    for index, (name, content) in enumerate(results, start=1):
        sections.append(f"## 子代理 {index}：{name}\n{content}")
    return "\n\n".join(sections)


#: 工具名 → 实现。**这是唯一的注册表** —— `prompts.CHAT_TOOLS` 里列了但这里
#: 没有的名字，会在 `_execute_tool` 里被明确报出来（而不是静默变成"未知工具"）。
#: 反过来，这里有而清单里没有的名字也会红
#: （`tests/test_chat.py::test_every_declared_tool_has_an_implementation_and_vice_versa`）。
_TOOL_IMPLS: dict[str, Any] = {
    "list_scans": _tool_list_scans,
    "create_scan": _tool_create_scan,
    "list_challenges": _tool_list_challenges,
    "start_ctf_solve": _tool_start_ctf_solve,
    # ★ v1.46：比赛平台（羊城杯）原生工具 —— 对话页直接刷题/交 flag。
    #   与上面两个的区别：这两个走**比赛平台 API**（slabmatch 驱动），
    #   不是本平台 KernelChallenge 题库。见 `app/chat/competition.py`。
    "fetch_competition_challenges": _tool_fetch_competition_challenges,
    "submit_flag": _tool_submit_flag,
    # ★ v1.71：赛事自动托管（多模型赛跑，整场）—— 对话里下指令起赛跑。
    #   与 CTF 页 `/ctf-race/*` 面板共用 `app/ctf_race/manager.py` 的同一场单例。
    "start_competition_race": _tool_start_competition_race,
    "competition_race_status": _tool_competition_race_status,
    "stop_competition_race": _tool_stop_competition_race,
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
    # ★ v1.88：黑板（覆盖度 / 事实）—— 提示词一直要求模型记账，
    #   工具表里却没有，于是"记账"这条纪律在对话里从未执行过。
    "record_coverage": _tool_record_coverage,
    "record_fact": _tool_record_fact,
    "list_facts": _tool_list_facts,
    # ★ v1.89：会话级计划（`chat_todos`）。与扫描侧的同名工具不是一回事。
    "create_todo": _tool_create_todo,
    "list_todos": _tool_list_todos,
    "update_todo": _tool_update_todo,
    # ★ v1.81：可重放验证。对授权目标发真实请求，但只读、不改平台状态；
    #   安全边界是 GuardedHTTPTransport（SSRF）+ scope guard，不是审批。
    "verify_http": _tool_verify_http,
    "spawn_subagents": _tool_spawn_subagents,
    # ★ P1 执行图：角色通过这组工具真正创建/领取/写回任务。
    "create_agent_task": graph_tools.create_agent_task,
    "add_agent_goal": graph_tools.add_agent_goal,
    "add_agent_intent": graph_tools.add_agent_intent,
    "list_agent_graph": graph_tools.list_agent_graph,
    "claim_agent_intent": graph_tools.claim_agent_intent,
    "finish_agent_intent": graph_tools.finish_agent_intent,
    "prove_agent_goal": graph_tools.prove_agent_goal,
    # ★ P2c：复测/验证角色的漏洞上下文与结论写回。
    "get_finding_context": finding_tools.get_finding_context,
    "record_finding_result": finding_tools.record_finding_result,
    "list_findings": platform_tools.list_findings,
    "list_assets": platform_tools.list_assets,
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


#: 模型偶尔不写 `ACTION:` 协议，而是自创 XML 风格：`<action>{...}</action>`
#: 包一个动作、`<analysis>...</analysis>` 包一段内心推理。
#:
#: ★ 为什么必须处理它（v1.41，线上实测 2026-11-01 会话
#:   `chat_01M690GSFE4SSP0DB5BHR0BNDK`）：
#:   那个模型（deepseek-v4.1-flash）在长链解题里一轮写了 576 个 `<action>`
#:   标签，但只有 54 条工具结果 —— 也就是说**绝大多数动作既没被执行、也没被
#:   隐藏**，原样落进 process 面消息给用户看（界面上是一堆裸 `<action>` /
#:   `<analysis>` 和 JSON）。同时走原生 `tool_calls` 的那少数步执行了，
#:   于是 turn_run 记成 `nudges_used=95 / tools_used=45` 一直烧到纠偏额度用尽，
#:   仍停在计划句 —— 用户看到的是"停在下一步的计划上"。
#:
#: 修法：在解析/落库/推送三处统一把 XML 风格**改写成**文本协议
#: （`ACTION: {...}`），并删掉 `<analysis>` 块（那是推理，不该进正文）。
#: 这样 `parse_actions` 原样能执行、`_narration_before_action` 原样能剥离、
#: `HoldbackBuffer` 原样能拦。
_XML_ACTION_RE = re.compile(r"<action>\s*(\{.*?\})\s*</action>", re.DOTALL)
# ★ v1.62：`result` / `analysis` 都改成**同族前缀**（`result[a-zA-Z_]*` 等）。
#   线上真实漏网（会话 chat_01M6H1JNW3S2010NB0474PN369 seq976，2026-11-04）：
#   模型自创了新的 `<result_note>` 标签（不是 `<result>`），一句
#   `<result_note> 执行：… <result_note> 写 ACTION：…` 连成一长串还**没有闭合**。
#   旧正则只认精确 `<result>` → 既没删掉、也没锁流，最终把这一坨塞进了 `surface=answer`
#   的正文里，用户看到满屏 `<result_note> 执行：写 ACTION：`，还以为"干一轮就停了"
#   （其实模型一直在自说自话地"写 ACTION"却从没真的发出 `ACTION: {...}`）。
#   与 v1.56/v1.57 处理 `system*` 家族是同一类"模型自创协议"坑：按族收口。
#: ★ v1.63：改成同族前缀（`analysis[a-zA-Z_]*`，用 `(?![A-Za-z_])` 收边界，不用 `\b`
#:   —— `\b` 卡在 `_` 上是词字符，`<result_note>` 这类带下划线的同族标签根本匹配不到，
#:   与 v1.57 处理 `system*` 家族是同一坑）。
_XML_ANALYSIS_RE = re.compile(
    r"<analysis[a-zA-Z_]*(?![A-Za-z_])[^>]*>.*?</analysis[a-zA-Z_]*(?![A-Za-z_])>",
    re.DOTALL,
)
# ★ v1.44：模型伪造「工具结果」—— `<result>ok</result>`。工具结果只能由平台
#   真实返回，模型自己在正文里写出来的 `<result>` 是**幻觉**，必须删掉：
#   一来它不该出现在用户视野里（界面上一堆 `<result>ok</result>`），二来更
#   要命的是它会**随历史回灌给模型**，让模型以为自己"已经执行过了"，于是
#   一直写"执行。/<result>ok</result>"空转（线上实测会话
#   `chat_01M6BWQ69MZJNBAB7C7QMVXPV2`：单轮 40 回合、77 工具、37 次纠偏）。
#: ★ v1.63：同 `_XML_ANALYSIS_RE`，`result` 改同族前缀（`result[a-zA-Z_]*`）。线上
#:   模型自创 `<result_note>`（会话 chat_01M6H1JNW3S2010NB0474PN369 seq976）——
#:   旧 `\b` 边界匹配不到 `_`，于是「一直在自说自话写 ACTION 却不真发」的一长串
#:   `<result_note> 执行：…` 原样漏进 `surface=answer`。改成前缀后整族一起删。
_XML_RESULT_RE = re.compile(
    r"<result[a-zA-Z_]*(?![A-Za-z_])[^>]*>.*?</result[a-zA-Z_]*(?![A-Za-z_])>",
    re.DOTALL,
)
# ★ v1.52：模型自创的"安全自检"块 `<ds_safety>…</ds_safety>`（线上见
#   `<ds_safety>[用户未成年]否 [分类]其他 [判定]…[规则]无</ds_safety>`）。
#   与 `<analysis>` 同类：推理/自检不进正文，也不该随历史回灌。
_XML_DS_SAFETY_RE = re.compile(r"<ds_safety\b[^>]*>.*?</ds_safety>", re.DOTALL)
# ★ v1.48：模型改用 Anthropic 工具调用风格——`<parameter name="command">…</parameter>`
#   + `<parameter name="tool">run_shell</parameter>`，外面常常**没有** `<invoke>` 开标签、
#   只有 `</invoke>` 收尾。参数体是**纯文本**（可含换行/heredoc/引号/花括号），
#   不是 JSON —— 所以不能用 JSON 解析，只能按 `<parameter name="X">…</parameter>`
#   逐个抓值，再合成 `ACTION: {"tool":..., "args":{...}}`。
#   线上实测（2026-11-02，会话 chat_01M6BWQ69MZJNBAB7C7QMVXPV2 的下一轮
#   turn_01M6E99VZ6G3CYW5XY5GAX80WZ）：不认这套时该轮 101 回合、纠偏 45 次，
#   反复出现「参数错误：需要 `command`」——工具被调起但参数没送到。
_XML_PARAM_RE = re.compile(
    r'<parameter\s+name\s*=\s*"([^"]+)"\s*>(.*?)</parameter>', re.DOTALL
)
_XML_INVOKE_TAG_RE = re.compile(r"</?invoke\b[^>]*>", re.DOTALL)
#: `<invoke name="run_shell">` 这种"工具名在开标签属性里"的写法（真 Claude 格式）。
_XML_INVOKE_NAME_RE = re.compile(r'<invoke\s+name\s*=\s*"([^"]+)"[^>]*>', re.DOTALL)
#: 收尾兜底：删掉还没闭合/落单的协议标签本身（增量截断时可能只剩半个标签）。
_XML_STRAY_TAG_RE = re.compile(
    r"</?(?:action|analysis|result|invoke|parameter|ds_safety|system)"
    r"[a-zA-Z_]*(?![A-Za-z_])[^>]*>",
    re.DOTALL,
)
#: ★ v1.52：判断"要不要走规范化"的**同一套**标签集合（含收尾 `/` 形态）。
#:   与 `_XML_STRAY_TAG_RE` 保持一致——历史上正因为两处集合不同（v1.48 守卫漏了
#:   `</parameter>`）导致裸收尾标记原样漏出、甚至被拼进要执行的 shell 命令。
_XML_ANY_PROTOCOL_TAG_RE = re.compile(
    r"</?(?:action|analysis|result|invoke|parameter|ds_safety|system)"
    r"[a-zA-Z_]*(?![A-Za-z_])[^>]*>",
    re.DOTALL,
)

# ★ v1.56：`<system>…</system>` —— 模型自创的第四套 XML 包装。
#
#   线上实测（会话「Server Host: https://pro.dasctl.c…」，2026-11-03）：模型把
#   一堆**意图碎片**包进 `<system>` 标签刷屏：
#       <system>ACTION.</system> <system>执行.</system> <system>先.</system> ……
#       <system>ACTION: {"tool":"run_shell","args":{"command":"…"}}</system>
#   这些既不是合法 `ACTION:` 行、也不在任何已知标签集合里 → 不执行也不隐藏，
#   正文刷屏停不下来（与 v1.41/v1.44/v1.49/v1.52 同一类"自创协议"坑）。
#
#   处理策略（两段）：
#   1. `<system>…</system>` 里若含真正的 `ACTION: {...}`，**只保留那段 ACTION 行**
#      （这是模型唯一想让它执行的形态），其余包裹文字丢掉；
#   2. 其余 `<system>…</system>`（`ACTION.` / `执行.` / `先.` 这种纯碎片）整块删掉，
#      绝不把里面的碎词当命令执行（与 v1.53"认不出的参数组整组丢弃"同一原则）。
#
# ★ v1.57：整族 `system*` 标签一起处理。线上审计（会话
#   chat_01M6BWQ69MZJNBAB7C7QMVXPV2，2026-11-03）发现同族还有三种变体：
#     · `<system_warning>你没写 ACTION，请补上。</system_warning>` —— 纠偏提示，纯噪声；
#     · `<system_welcome/>` —— 空自闭合；
#     · `<system_call>\n{"tool": "run_shell", "args": {…}}` —— **只有开标签、没有闭合**，
#       后跟一段 `{"tool":…, "args":…}` 的 JSON（这其实是模型想发的真动作，只是用错了
#       标签名，引擎认不出 → 那轮"无动作" → 平台又回灌 `<system_warning>你没写 ACTION`）。
#   v1.56 的正则用 `system\b`，而 `\b` 卡在 `system_warning` 的 `_`（`_` 是词字符），
#   **一个都没匹配到** —— 31/33 条历史消息照样原样回放给前端。
#   现在标签名扩成 `system[a-zA-Z_]*`（用 `(?![A-Za-z_])` 收边界，不用 `\b`），
#   并单独处理"只有开标签的 `<system_call>{json}`"。
_XML_SYSTEM_BLOCK_RE = re.compile(
    r"<system[a-zA-Z_]*(?![A-Za-z_])[^>]*>(.*?)</system[a-zA-Z_]*(?![A-Za-z_])>",
    re.DOTALL,
)
#: `<system>` 块里真正可执行的行：`ACTION: {…}`（JSON 到行尾）。
_XML_SYSTEM_ACTION_RE = re.compile(r"ACTION\s*[:：]\s*(\{.*\})\s*$", re.DOTALL)
#: 只有开标签、后面直接跟 JSON 的形态：`<system_call>\n{"tool":…, "args":…}`（无闭合）。
#: 抓"开标签 + 其后到文末（或下一个裸标签）的 JSON 对象"。
_XML_SYSTEM_OPEN_CALL_RE = re.compile(
    r"<system[a-zA-Z_]*(?![A-Za-z_])[^>]*>\s*(\{.*?\})\s*(?=<|$)",
    re.DOTALL,
)
#: 判定一段括号文本是不是"可当动作"的 JSON：必须能解析且含 `"tool"` 键。
_XML_BARE_TOOL_JSON_RE = re.compile(r"\{.*\"tool\".*\}", re.DOTALL)

# ★ v1.49：DeepSeek 的 DSML 风格 —— `<｜DSML｜ parameter>` / `<｜DSML｜ invoke>`
#   / `<｜DSML｜ calls>`，分隔符是**全角竖线 U+FF5C**（`｜`，UTF-8 ef bd 9c），
#   竖线与词之间常有空格。线上实测（会话 chat_01M6BWQ69MZJNBAB7C7QMVXPV2
#   的 turn_01M6E99VZ…，2026-11-02）：模型只写**收尾标记**（`</｜DSML｜ parameter>`
#   等），命令体直接内联在正文里、前面**没有**开标签、没有 `name=`、没有
#   `tool` 标记 —— 于是 v1.41/v1.44/v1.48 都认不出，这一轮被判「无动作」，
#   反复「我意识到一个从未做过的测试…现在真正运行」却执行不了，空转 75 轮。
#
#   两种形态分别处理：
#   1. 有真 `<parameter name="X">body</parameter>`（开标签在，可能是全角也可能是
#      半角）→ 交给 `_parameter_blocks_to_actions` 合成动作（先把它规范成半角标签）。
#   2. 只有收尾标记、没有开标签 → 这些标记**只是正文噪声**，直接删掉（不猜命令，
#      因为无从得知哪段文本是命令、传给哪个工具）。
_DSML_TAG_RE = re.compile(r"</?\s*[｜|]\s*DSML\s*[｜|]\s*[A-Za-z_]+[^>]*>", re.DOTALL)
#: 带 `name="X"` 的 DSML 开标签（`<｜DSML｜ parameter name="command">`）。
_DSML_NAMED_INVOKE_RE = re.compile(
    r"<\s*[｜|]\s*DSML\s*[｜|]\s*invoke\s+name\s*=\s*\"([^\"]+)\"\s*>", re.DOTALL
)


def _is_dsml(text: str) -> bool:
    return bool(text) and bool(_DSML_TAG_RE.search(text))


def _dsml_tag_to_halfwidth(match: re.Match[str]) -> str:
    """把一个 DSML 标签改写成对应的半角标签。

    只做"翻译"，不做判断：`</｜DSML｜ parameter>` → `</parameter>`；
    `<｜DSML｜ invoke name="run_shell">` → `<invoke name="run_shell">`；
    `<｜DSML｜ parameter name="command">` → `<parameter name="command">`；
    `<｜DSML｜ calls>` / `</｜DSML｜ calls>` → 空串（`calls` 只是外层包裹）。
    翻译后交给现有的半角正则链路（`_parameter_blocks_to_actions` /
    `_XML_INVOKE_TAG_RE` / `_XML_STRAY_TAG_RE`）统一处理。
    """
    raw = match.group(0)
    closing = raw.lstrip().startswith("</")
    # 去掉前后 `<`/`</` `>` 与分隔符，取出"标签名 + 属性"
    body = raw[2:] if closing else raw[1:]
    body = body.rsplit(">", 1)[0]
    body = re.sub(r"^\s*[｜|]\s*DSML\s*[｜|]\s*", "", body).strip()
    name = re.match(r"([A-Za-z_]+)", body)
    tag = name.group(1).lower() if name else ""
    if tag == "calls":
        return ""  # 外层包裹，无意义
    attrs = body[name.end() :] if name else ""
    if closing:
        return f"</{tag}>"
    return f"<{tag}{attrs}>" if attrs else f"<{tag}>"


def _parameter_blocks_to_actions(text: str) -> str:
    """把 Anthropic 风格 `<parameter>` 组改写成 `ACTION:` 行（v1.48）。

    逐组（一次 `<invoke>` 内的一组 `<parameter>`）合成一个动作：取
    `name="tool"` 作工具名、`name="args"` 若存在则整体当 args，其余
    `<parameter name="X">body</parameter>` 里 X 作参数名、body 原文（**不 strip
    内部换行**，只去首尾空白）作参数值 —— 参数体是 bash 脚本，逐字节保留才对。

    认不出（没有 `name="tool"`）的 `<parameter>` 组**不猜**，原样留着交给
    兜底清理（宁可少执行一个，也不执行一个参数错的动作）。
    """
    out: list[str] = []
    last = 0
    # 连续出现的 <parameter> 归为同一组（中间只允许空白/换行）。
    matches = list(_XML_PARAM_RE.finditer(text))
    if not matches:
        return text
    i = 0
    while i < len(matches):
        group = [matches[i]]
        j = i + 1
        while j < len(matches) and text[group[-1].end() : matches[j].start()].strip() == "":
            group.append(matches[j])
            j += 1
        params: dict[str, Any] = {}
        for m in group:
            params[m.group(1).strip()] = m.group(2).strip()
        tool = params.pop("tool", "")
        if not tool:
            # 工具名可能写在 `<invoke name="run_shell">` 开标签上（真 Claude 格式）。
            # 在组之前最近的文本里找它。
            preceding = text[last : group[0].start()]
            inv = None
            for m in _XML_INVOKE_NAME_RE.finditer(preceding):
                inv = m.group(1).strip()
            if inv:
                tool = inv
        if not tool:
            # 这一组认不出工具名：**整组丢掉**（连同 body），不保留。
            #
            # ★ v1.53：历史坑 —— 早先这里 `continue` 跳过、把 body 原样留成正文，
            #   于是 `<parameter name="command">rm -rf /tmp/x</parameter>`（缺 tool）
            #   被删掉标签后 body 变成人话里的一段裸命令，紧接着 v1.51 的
            #   `extract_trailing_command` 会把它**当成要执行的命令直接跑掉**。
            #   一个"认不出的协议碎片"绝不该变成一条被执行的真实命令 ——
            #   认不出就整组丢弃（丢字节比跑错命令安全得多）。
            out.append(text[last : group[0].start()])
            last = group[-1].end()
            i = j
            continue
        args: dict[str, Any] = {}
        raw_args = params.pop("args", None)
        if raw_args is not None:
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    args = parsed
            except ValueError:
                pass
        args.update(params)
        # 把这一整段（组内所有 parameter 标签 + 之间空白）替换成一行 ACTION。
        out.append(text[last : group[0].start()])
        out.append("\nACTION: " + json.dumps({"tool": tool, "args": args}, ensure_ascii=False) + "\n")
        last = group[-1].end()
        i = j
    out.append(text[last:])
    return "".join(out)


def _is_tool_json(raw: str) -> bool:
    """`raw` 是否是一段可当动作的 JSON（能解析成 dict 且含非空 `"tool"`）。

    v1.57：`<system_call>\n{"tool":…, "args":…}` 这种只有开标签的形态，其 JSON
    是模型想发的真动作 —— 只有确认它结构合法（dict + 有 tool 名）才展开成
    ACTION，绝不把任意一坨花括号当命令。解析失败一律 False（随后整块丢弃）。
    """
    import json as _json

    try:
        obj = _json.loads(raw)
    except (ValueError, TypeError):
        return False
    return isinstance(obj, dict) and bool(obj.get("tool"))


def _normalize_xml_actions(text: str) -> str:
    """把自创的 XML 块规范化（找不到就是原文）。

    - `<action>{...}</action>` → `\\nACTION: {...}\\n`（每块一行，行首起协议）；
    - `<analysis>...</analysis>` → 整块删掉（推理不进正文，也不该执行）；
    - `<result>...</result>`（v1.44）→ 整块删掉（**伪造的工具结果**，工具结果
      只能由平台返回；留着还会随历史回灌把模型带进"我已执行"的空转）。

    只在**检测到** XML 协议标签时才动；普通回复（无 XML）逐字节不变。
    """
    # ★ v1.52：这个"要不要动手"的守卫**必须**和 `_XML_STRAY_TAG_RE` 认的标签集合
    #   一致。历史教训：v1.48 只把 `"<parameter"` 放进守卫、漏了 `"</parameter>"`
    #   —— 于是「只有收尾 `</parameter>`、没有开标签」的正文**直接原样返回**，
    #   残留标记既没被 strip、还会随历史回灌（线上 seq794/818：`</parameter>`
    #   甚至被拼进了 shell 命令，bash 报 `/parameter: No such file or directory`）。
    #   现在改用同一个正则判断，杜绝两处集合漂移。
    if not text or not (_XML_ANY_PROTOCOL_TAG_RE.search(text) or _is_dsml(text)):
        return text
    # ★ v1.49：先把 DSML 全角标签规范成半角，让下游正则统一处理。
    #
    #   `<｜DSML｜ parameter>`（收尾，无开标签）→ `</parameter>`。这类只有收尾
    #   标记的形态，命令体内联在正文里、无处可还原，删掉标记即可（下面的
    #   `_XML_STRAY_TAG_RE` 会清掉裸 `</parameter>`）；**不猜**它是哪个工具。
    #   `<｜DSML｜ parameter name="command">`（有开标签）→ `<parameter name="command">`，
    #   交给 `_parameter_blocks_to_actions` 正常合成动作。
    if _is_dsml(text):
        text = _DSML_TAG_RE.sub(_dsml_tag_to_halfwidth, text)
    # ★ v1.56：先处理 `<system>…</system>`。分两种：
    #   · 块内含真 `ACTION: {...}` → 只保留那段 ACTION 行（这是模型唯一想执行的
    #     形态），其余包裹文字丢掉；
    #   · 其余（`ACTION.`/`执行.`/`先.` 这种碎片）→ 整块删掉，绝不把碎词当命令执行。
    #   必须在删 `<analysis>/<result>` 之前做：`<system>` 里的 ACTION 要能被下面
    #   的 `_XML_ACTION_RE`/`parse_actions` 正常接住。
    if "<system" in text:
        def _unpack_system(m: re.Match[str]) -> str:
            inner = m.group(1)
            am = _XML_SYSTEM_ACTION_RE.search(inner)
            if am:
                return "\nACTION: " + am.group(1) + "\n"
            # v1.57：块内是一个裸 `{"tool":…}` JSON（`<system_call>{…}</system_call>`）→
            # 它其实是想发动作，展开成 ACTION 行。
            bm = _XML_BARE_TOOL_JSON_RE.search(inner)
            if bm and _is_tool_json(bm.group(0)):
                return "\nACTION: " + bm.group(0).strip() + "\n"
            return ""  # 纯碎片，整块丢弃

        text = _XML_SYSTEM_BLOCK_RE.sub(_unpack_system, text)
        # v1.57：只有开标签、后跟裸 JSON 的 `<system_call>{json}`（无闭合）。
        def _unpack_open_call(m: re.Match[str]) -> str:
            body = m.group(1)
            if _is_tool_json(body):
                return "\nACTION: " + body.strip() + "\n"
            return ""

        text = _XML_SYSTEM_OPEN_CALL_RE.sub(_unpack_open_call, text)
    # 先删 <analysis> / <result> / <ds_safety>（都不该进正文），再改写 <action>。
    text = _XML_ANALYSIS_RE.sub("", text)
    text = _XML_RESULT_RE.sub("", text)
    # ★ v1.52：`<ds_safety>...</ds_safety>` 是模型的另一套"安全自检"块（线上见
    #   `<ds_safety>[用户未成年]否 [分类]其他 [判定]…[规则]无</ds_safety>`）。
    #   它与 `<analysis>` 同类：既不该给用户看，也不该随历史回灌（和 v1.44 的
    #   `<result>` 一样会自我强化）。整块删掉。只删成对的；落单的收尾标记由下面
    #   `_XML_STRAY_TAG_RE` 兜。
    text = _XML_DS_SAFETY_RE.sub("", text)
    # v1.48：Anthropic 风格 <parameter> 组 → ACTION:（在删 <action> 之前做，
    # 两者不会同时出现；分开放是为了各自可读、可单独测）。
    if "<parameter" in text:
        text = _parameter_blocks_to_actions(text)
        text = _XML_INVOKE_TAG_RE.sub("", text)  # 去掉 <invoke>/</invoke> 外壳
    text = _XML_ACTION_RE.sub(lambda m: "\nACTION: " + m.group(1) + "\n", text)
    # 兜底：清掉落单/未闭合的标签残留（如半截 `<result>`、`</invoke>`、`</parameter>`）。
    return _XML_STRAY_TAG_RE.sub("", text)


def _has_protocol_marker_or_xml(text: str) -> bool:
    """正文里已经有文本协议（`ACTION:`/`FINAL:`）**或** XML 块吗。"""
    if _XML_ANY_PROTOCOL_TAG_RE.search(text or ""):
        return True
    if _is_dsml(text):
        return True
    return _has_protocol_marker(text)



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


async def _build_tool_context(
    db: Session,
    session: ChatSession,
    identity: Any,
    *,
    gateway: Any = None,
    allowed_tools: set[str] | None = None,
) -> _ToolContext:
    """建本轮的工具上下文，并发现会话挂载的 MCP 工具。

    ★ 发现失败**不抛**：挂载的某个 server 连不上，只是这一轮少几个工具；
      整轮对话必须照常开始。`discover_mounted` 已经把每台 server 的失败
      收成状态（会进提示词，告诉模型"别调这些"）。
    """
    try:
        catalog = await mcp_tools.discover_mounted(db, session)
    except Exception:  # noqa: BLE001 - 连接发现不该毁掉整轮对话
        _log.warning("发现会话挂载的 MCP 工具失败，本轮按未挂载处理", exc_info=True)
        catalog = mcp_tools.McpToolCatalog()
    from app.agents import service as agents_service

    allowed = agents_service.allowed_tools_for_persona(db, session.persona)
    if allowed_tools is not None:
        allowed = set(allowed_tools) if allowed is None else allowed & set(allowed_tools)
    return _ToolContext(
        db=db,
        session=session,
        identity=identity,
        mcp=catalog,
        allowed_tools=allowed,
        gateway=gateway,
    )


def _tools_schema(ctx: _ToolContext) -> list[dict[str, object]]:
    """本轮交给原生 tool-calling 的完整工具表：内置工具 + 挂载的 MCP 工具。"""
    schema: list[dict[str, object]] = [
        item
        for item in build_tools_schema()
        if ctx.allowed_tools is None
        or item["function"]["name"] in ctx.allowed_tools
    ]
    schema.extend(ctx.mcp.tool_schema())
    return schema


def _auto_approve_active(session: Any) -> bool:
    """会话是否在自动放行窗口内（v1.83）。"""
    from app.core.timeutil import now_utc

    until = getattr(session, "auto_approve_until", None)
    return bool(until is not None and until > now_utc())


def _needs_approval(name: str) -> bool:
    """这个工具在开了「人工审批」时要不要先挂起。

    ★ MCP 工具一律算"有副作用"：它们连接的是外部 server，模型能读什么、
      写什么由 server 自己决定，平台无法像内置工具那样逐个标
      `consequential`。默认拦住是唯一安全的方向 —— 漏拦一次就是一次
      未经确认的外部动作。
    """
    return name in approval_gated_names() or mcp_tools.is_mcp_tool_name(name)


def _tool_progress_emitter(emit: Emit | None, name: str) -> Any:
    """每 5 秒报一次"还在跑"的心跳（没有 emit 时为 None）。"""
    if emit is None:
        return None

    async def _send(payload: dict[str, Any]) -> None:
        await emit(EVENT_TOOL_PROGRESS, {"tool": name, **payload})

    return _send


async def _with_heartbeat(
    awaitable: Any,
    on_progress: Any,
) -> Any:
    """等待一个工具结果，同时每 5 秒推一次心跳。

    ★ 只对**可能慢**且没有可读输出的工具（MCP / verify_http / 建扫描…）
      挂在外面；`run_shell` / `browser` 自己会推输出，不再叠加心跳。
      心跳不写进工具输出，避免把"等了多少秒"伪装成命令结果。
    """
    import asyncio
    import time

    if on_progress is None:
        return await awaitable

    async def _tick() -> None:
        started = time.monotonic()
        while True:
            await asyncio.sleep(5)
            try:
                await on_progress({'seconds': int(time.monotonic() - started)})
            except Exception:  # noqa: BLE001 - 心跳失败不该影响工具
                _log.debug("工具心跳发送失败", exc_info=True)

    task = asyncio.create_task(_tick())
    try:
        return await awaitable
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _tool_output_emitter(emit: Emit | None, name: str) -> Any:
    """把工具输出块转成 SSE 帧（没有 emit 时返回 None，走非流式路径）。"""
    if emit is None:
        return None

    async def _send(chunk: str) -> None:
        await emit(EVENT_TOOL_OUTPUT, {"tool": name, "chunk": chunk})

    return _send


async def _execute_tool(
    ctx: _ToolContext,
    name: str,
    args: dict[str, Any],
    *,
    on_output: Any = None,
    on_progress: Any = None,
) -> tuple[str, bool]:
    """执行一个工具，返回 `(结果文本, 是否失败)`。

    ★ 失败**不抛**，返回文本 + 布尔。原因见 `_err`。

    ⚠️ 每次工具调用后 `db.flush()` 但不 `commit()` —— commit 由 router 的
       `session_scope` 在请求末尾统一做。这样"工具做了一半、模型又调下一个
       工具失败了"时，整轮要么全成、要么全不成（除 `create_scan` 那条路，
       它内部自己 commit，因为扫描是**独立于对话**的实体 —— 建出来了就该
       存在，不该因为对话后面出错而被回滚）。
    """
    if (
        ctx.allowed_tools is not None
        and name not in ctx.allowed_tools
        and not mcp_tools.is_mcp_tool_name(name)
    ):
        return (f"该 Agent 未授权工具 `{name}`。", True)

    impl = _TOOL_IMPLS.get(name)
    if impl is None:
        # ★ v1.80：动态 MCP 工具。它们不在 `_TOOL_IMPLS` 里（名字来自本会话
        #   挂载的 server），执行走 `McpToolCatalog` —— 失败只影响这一个工具。
        binding = ctx.mcp.get(name)
        if binding is None:
            available = sorted(CHAT_TOOL_NAMES)
            if ctx.mcp:
                available += sorted(ctx.mcp.bindings)
            return (
                f"失败：没有叫 `{name}` 的工具。可用工具：{', '.join(available)}。",
                True,
            )
        try:
            # ★ v1.92：MCP 的 `notifications/progress` 转成 tool_progress
            #   帧；与心跳同一个事件，界面只需要一条状态行。
            async def _mcp_progress(params: dict[str, Any]) -> None:
                if on_progress is None:
                    return
                payload: dict[str, Any] = {}
                for key in ("progress", "total", "message"):
                    if params.get(key) is not None:
                        payload[key] = params[key]
                if payload:
                    await on_progress(payload)

            text = await ctx.mcp.call(binding, args, on_progress=_mcp_progress)
        except mcp_tools.McpToolError as exc:
            return f"失败：{exc}", True
        # MCP 的 `isError=true` 由内核渲染成「MCP 工具返回错误：…」，
        # 不是我们约定的「失败：」前缀。两者都算失败，界面上才会标红。
        return text, text.startswith(("失败：", "MCP 工具返回错误："))
    try:
        # ★ v1.92：`run_shell` 走流式实现 —— 边跑边把 stdout 推到 SSE，
        #   界面上的运行卡才有"输出在涨"可看。其余工具仍是整块返回。
        #   最终文本与内核版逐字一致（同一套截断/超时说明）。
        if name == "run_shell" and on_output is not None:
            text = await chat_sandbox.stream_run_shell(ctx, args, on_output)
        elif name == "browser" and on_output is not None:
            text = await chat_sandbox.tool_browser(ctx, args, on_output)
        else:
            text = await _with_heartbeat(impl(ctx, args), on_progress)
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
    from app.providers import retry

    return LlmGateway(
        model=provider.model,
        api_key=decrypt_provider_key(provider),
        base_url=provider.base_url or "",
        label=provider.label,
        kind=provider.kind,
        max_tokens=CHAT_MAX_TOKENS,
        retry_policy=retry.load_policy(db),
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


def _gateway_supports_tools(gateway: Any) -> bool:
    """这个网关能不能接收 `tools=`（原生工具调用）。

    ★ `chat_stream`（流式主路径）或 `chat`（非流式/审批续跑）任一认这个参数，
      就算"支持"。用来同时决定两件事，且必须一致：
        ① 要不要把 `tools=` 发出去；
        ② 系统提示词用「原生调用」版还是「文本 ACTION」版。
      两者错配会自相矛盾（教它写 ACTION 却又给 tools，或反之）。
    """
    for attr in ("chat_stream", "chat"):
        fn = getattr(gateway, attr, None)
        if fn is not None and _supports_param(fn, "tools"):
            return True
    return False


def _challenge_category(db: Session, session: ChatSession) -> str | None:
    """绑定题目的会话 → 题目分类；普通对话返回 None。

    只用于把题型手册正文注入对话提示（见 `build_system_prompt`）。
    题被删/分类为空都返回 None，提示词退回原样，不抛。
    """
    if not session.challenge_id:
        return None
    from app.kernel.models import KernelChallenge

    challenge = db.get(KernelChallenge, session.challenge_id)
    return challenge.category if challenge is not None else None


def _system_prompt_for(
    session: ChatSession,
    hinted_tools: list[str] | None = None,
    *,
    native_tools: bool = False,
    challenge_category: str | None = None,
    mcp_catalog: mcp_tools.McpToolCatalog | None = None,
    db: Session | None = None,
    locale: str | None = None,
) -> str:
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

    prompt = build_system_prompt(
        persona=session.persona,
        require_approval=bool(session.require_approval),
        mounted_skills=mounted,
        hinted_tools=hinted_tools,
        native_tools=native_tools,
        challenge_category=challenge_category,
        # ★ v1.89：计划是**库里的状态**，不是上一轮说了什么。每轮重读，
        #   所以窗口外（被截断）的旧计划依然在提示词里。
        todos=todo_store.list_todos(db, session.id) if db is not None else None,
        locale=locale,
    )
    # ★ v1.80：会话挂载的 MCP 工具。放在最后 —— 它比骨架更"这个对话专属"。
    #   原生模式下 schema 已在 `tools=` 里，这一段补的是"哪台连不上"；
    #   文本协议模式下它还是模型唯一能看到的工具清单。
    if mcp_catalog is not None:
        prompt += mcp_catalog.prompt_section()
    return prompt


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
    turn_run: Any = None,
    resume_from: Any = None,
    allowed_tools: set[str] | None = None,
    locale: str | None = None,
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
    if attachment_ids and resume_from is None:
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
    # ★ v1.19：从断点接手时**不再追加**一条用户消息 —— 那一轮的用户提问
    #   早就在历史里了（它在第一轮就落库了）。再追加一条会变成"用户又说
    #   了一遍"，把上下文搞成两份。
    if resume_from is None:
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

    # ★ v1.80：先发现本会话挂载的 MCP 工具，再渲染提示词 —— 提示词要把
    #   "已挂载但连不上"的 server 告诉模型（它会据此避免反复重试）。
    ctx = await _build_tool_context(
        db, session, identity, gateway=gateway, allowed_tools=allowed_tools
    )
    native_tools = _gateway_supports_tools(gateway)
    challenge_category = _challenge_category(db, session)

    def system_now() -> str:
        # 每轮重读计划：模型在同一轮里会 create/update todo；只在轮次开始时
        # 渲染一次的话，“3/3 完成”要等下一轮才被看到，而它已经接着干别的了。
        return _system_prompt_for(
            session,
            explicit_tools,
            native_tools=native_tools,
            challenge_category=challenge_category,
            mcp_catalog=ctx.mcp,
            db=db,
            locale=locale,
        )

    system = system_now()

    #: 打开审批时，这些工具不执行、只挂起。
    gate_on = bool(session.require_approval) and not _auto_approve_active(session)

    #: 已经"提醒"过模型几次（见 `MAX_NARRATION_NUDGES`）。
    #:
    #: ★ 它单独计数、不从 `MAX_TOOL_ROUNDS` 里扣 —— 两笔预算管的是不同的
    #:   东西（纠偏 vs 工具往返）。混在一起会让"提醒了两次"吃掉两个工具
    #:   轮次，于是真正需要跑工具的那一轮反而没预算了。
    nudges_used = 0
    #: 上面两个数里，有多少次是「本轮跑过工具、只写了一句人话」引起的（v1.23）。
    #:
    #: 单独一个计数器，因为它有自己的额度 `MAX_TOOL_NUDGES`。见那个常量的注释。
    tool_nudges_used = 0

    #: 上面那个数里，有多少次是**被切断**引起的（v1.21）。
    #:
    #: 两个数分开记的理由见 `MAX_TRUNCATION_CONTINUES`：`nudges_used` 管
    #: 「总共多花了几轮」（循环上界与工具预算要用它），`truncations_used`
    #: 只是它的子集，用来判断截断额度还剩多少。格式错的次数 = 前者减后者。
    truncations_used = 0

    #: 这一轮**真正执行过**几次工具（v1.19）。
    #:
    #: 它是纠偏判据里最重要的一条结构信号：跑过工具的轮次突然"只写一句
    #: 人话"就收尾，绝大多数是没做完；而整轮零工具的纯问答不该被推。
    #: 见 `app/chat/nudge.py`。
    #:
    #: ★ v1.29：**必须在 `if resume_from` 之前初始化。** 之前它被写在恢复块
    #:   后面，于是任何一次"自动接手"都会把刚从库里恢复的工具计数清零 ——
    #:   接手后的那一轮以为"这轮没跑过工具"，最重要的结构信号失效。
    tools_used = 0

    #: ★ v1.64.1：连续多少轮"跑过工具但这一轮没再调任何工具"（只写了正文）。
    #:   见 `MAX_NO_TOOL_NUDGES`。执行工具时清零。
    no_tool_rounds = 0

    #: 本轮 FINAL 被"打回"过几次（v1.29）。见 `MAX_FINAL_CONFIRMATIONS`。
    final_confirmations = 0

    #: 本轮因为"这句是计划不是结论"被打回几次（v1.30）。见 `MAX_PLAN_BOUNCES`。
    #:
    #: ★ 与 `final_confirmations` 分开：后者是"问过没有"，前者是"推了几步"。
    #:   共用一个计数就是上面那场线上事故的成因。
    plan_bounces = 0

    #: 本轮「模型一个字没写」被推了几次（v1.32，见 `MAX_EMPTY_NUDGES`）。
    #:
    #: ★ 与 `nudges_used` 分开：共用一个数时，前面几次叙述纠偏会把空回复
    #:   的额度一起吃掉，于是计划句之后的第一个空回复就被判「额度用完」。
    empty_nudges_used = 0

    #: ★ v1.49：最近执行过的动作签名（工具+参数），用于"原地打转"检测。
    #:   只留最近 `MAX_REPEATED_ACTIONS` 条，够判连续重复即可。
    recent_action_sigs: list[str] = []
    #: ★ v1.49：这一轮是否已经因为"原地打转"提醒过一次 —— 只提醒一次，
    #:   不能每轮都喂同一句（那就成了新的死循环）。
    stall_warned = False
    #: ★ v1.84：这一轮累计的登录/口令尝试次数。同一条命令检测抓不到
    #:   "每次换个候选"的爆破，这里按策略族计数。
    brute_attempts = 0
    brute_warned = False
    #: ★ v1.87：认证之外真正执行过的探测动作签名。交付前要求至少 3 个
    #:   不同方向，防止“只试了登录就交卷”。
    non_auth_probe_sigs: set[str] = set()
    #: 把工作委派给子代理/扫描后，父代理可以直接汇总，不要求它自己再跑
    #: 3 个探测 —— 子代理那边有同样的交付闸门。
    delegated_work = False
    #: 交付前因为“只有认证一条策略族”被打回的次数。只打回一次，避免死循环。
    strategy_bounces = 0
    #: ★ v1.84：用户说「继续」但模型只重贴结果的次数。允许打回一次，
    #:   第二次才接受，避免无限循环。
    result_bounces = 0

    if resume_from is not None:
        nudges_used = int(getattr(resume_from, "nudges_used", 0) or 0)
        truncations_used = int(getattr(resume_from, "truncations_used", 0) or 0)
        tool_nudges_used = int(getattr(resume_from, "tool_nudges_used", 0) or 0)
        tools_used = int(getattr(resume_from, "tools_used", 0) or 0)

    #: 下一轮要往上下文里补的一句提醒（一次性）。
    #:
    #: ⚠️ 不能直接 `messages.append(...)` —— `messages` 每轮都由
    #:   `_render_history(history)` **从库里重新拼**，append 上去的东西
    #:   在下一轮就被丢掉了。所以用一个变量把它带到下一轮，在那里补。
    pending_reminder: str | None = None

    #: ★ v1.68：本调用内"墙钟预算"的起点。见 `TURN_WALL_SECONDS`。
    turn_wall_start = time.monotonic()

    #: 循环上界 = 工具轮数 + 1（最后那轮用来总结）+ 纠偏轮数。
    for round_index in range(
        MAX_TOOL_ROUNDS
        + 1
        + MAX_NARRATION_NUDGES
        + MAX_TRUNCATION_CONTINUES
        + MAX_TOOL_NUDGES
        + MAX_FINAL_CONFIRMATIONS
    ):
        # ★ v1.68：墙钟预算闸。跑到超过 `TURN_WALL_SECONDS` 就地优雅收尾 ——
        #   不是让它接着烧，也不是硬砍，而是落一句"到点了、要继续再发继续"，
        #   本轮 marked interrupted（可接手/可续跑），沙箱释放。
        #   只在"真跑过工具"（tools_used>0）时判，纯问答不受影响；设 0 关闭。
        if (
            TURN_WALL_SECONDS
            and tools_used > 0
            and _turn_elapsed_seconds(turn_run, turn_wall_start) >= TURN_WALL_SECONDS
        ):
            exhausted = chat_service.append_assistant_message(
                db,
                session=session,
                content=render_turn_wall_note(TURN_WALL_SECONDS),
            )
            db.commit()
            result.messages.append(exhausted)
            result.hit_tool_limit = True
            result.needs_continue = True
            return result

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
        # ★ v1.69：同一结论复读闸。模型连续多次给出几乎同一句结论（"没拿到、
        #   做不下去"）时，判定它已经到底，收尾并讲清是阻塞、不是没试够 ——
        #   避免像 chat_01M6P1TAYTASS6P64P85PED8MB 那样换个算法反复穷尽 12 小时。
        #   ★ 只在**已经干了足够多轮/足够多次工具**时才判：反复复述"我看到的是
        #     一个 ELF…"这种正常叙述不算"结论复读"（否则会误杀正常长任务/测试）。
        #     真正卡死的会话是几百次工具、上百轮；这个门槛远高于任何正常节奏。
        if (
            tools_used >= REPEATED_CONCLUSION_MIN_TOOLS
            and round_index - nudges_used >= REPEATED_CONCLUSION_MIN_ROUNDS
            and nudge_policy.is_repeated_conclusion(_recent_assistant_texts(history))
        ):
            exhausted = chat_service.append_assistant_message(
                db,
                session=session,
                content=render_repeated_conclusion_note(),
            )
            db.commit()
            result.messages.append(exhausted)
            return result
        messages = _render_history(history)
        # ★ v1.53 兜底：`_render_history` 会把开头的非 user 消息丢掉（保证首条
        #   是 user，见其文档）。极端情况下（小窗口里全是 assistant/tool 残句）
        #   可能把窗口清空 —— 直接拿空数组调模型必然 400。给一条最小 user 占位，
        #   让模型至少有话可接，而不是整轮失败。
        if not messages:
            messages = [{"role": "user", "content": "（继续之前的工作。）"}]
        if pending_reminder is not None:
            # ★ 一次性：补进去之后立刻清掉。留着的话它会出现在之后每一轮
            #   的上下文里，模型会以为用户反复在催它。
            messages.append({"role": "user", "content": pending_reminder})
            pending_reminder = None

        # ★ v1.13：`round_index - nudges_used` —— 纠偏轮不消耗工具预算。
        at_limit = (round_index - nudges_used) >= MAX_TOOL_ROUNDS
        system_this_round = system_now()
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
                gateway,
                messages,
                system=system_this_round,
                emit=emit,
                session_id=session.id,
                # ★ v1.64：原生工具调用。到本轮工具上限时**不再给 tools**
                #   —— 那时系统提示已要求"直接用一段话总结"，再给它 tools
                #   会让模型继续调工具，把「收尾」变成下一轮工具调用。
                tools=None if at_limit else _tools_schema(ctx),
            )
            # ★ v1.92：把这一轮的 token 累加进执行记录（流式与非流式都带
            #   usage；接入点没给时 add_usage 自己不写，不制造假 0）。
            if turn_run is not None:
                turn_run_store.add_usage(db, turn_run, reply.usage)
        except Exception as exc:  # noqa: BLE001
            _log.warning("对话调模型失败", exc_info=True)
            raise _wrap_model_failure(exc) from exc

        if turn_run is not None:
            turn_run_store.heartbeat(
                db,
                turn_run,
                round_index=round_index,
                nudges_used=nudges_used,
                truncations_used=truncations_used,
                tool_nudges_used=tool_nudges_used,
                tools_used=tools_used,
            )
            db.commit()

        # ★ 落库给用户看的是**剥掉动作之后**的正文。理由见 `_narration_before_action`。
        raw_text = reply.text or ""
        # ★ v1.41：先把自创的 XML 动作写法（`<action>{...}</action>` /
        #   `<analysis>...</analysis>`）统一改写成文本协议，再做后面所有
        #   "剥离 / 解析 / 落库"。否则这些块既不被执行也不被隐藏，整段
        #   漏进用户气泡（见 `_normalize_xml_actions` 的线上实测记录）。
        raw_text = _normalize_xml_actions(raw_text)
        # ★ v1.19：先摘掉开头的 `FINAL:`。必须在 `_narration_before_action`
        #   之前 —— 那个函数复用内核的 `ACTION|FINAL` 正则，会把
        #   `FINAL: 解出来了` 整条截成空串，再落成"模型没有产出内容"的占位语。
        raw_text, said_final = nudge_policy.split_final(raw_text)
        text = _narration_before_action(raw_text)
        actions = [] if at_limit else parse_actions(raw_text)
        # ★ v1.51：见下 —— 贴了命令没写 ACTION 时直接当 run_shell 跑。
        if not actions and not at_limit and not said_final:
            _synth_cmd = nudge_policy.extract_trailing_command(text)
            if _synth_cmd:
                actions = [("run_shell", {"command": _synth_cmd})]
        # ★ v1.32：一行一帧的诊断日志。这类故障的失败模式是**静默的**：
        #   界面停在半句话上，没有异常、没有报错、turn_run 也是 done，
        #   只能靠自己猜「它到底回了什么」。只记长度与标志，不记正文。
        _log.info(
            "chat.round round=%s raw_len=%s text_len=%s actions=%s said_final=%s "
            "truncated=%s tools=%s nudges=%s tool_nudges=%s empty_nudges=%s "
            "reasoning_len=%s finish=%r raw_keys=%s",
            round_index,
            len(raw_text),
            len(text),
            len(actions),
            said_final,
            _is_truncated(reply),
            tools_used,
            nudges_used,
            tool_nudges_used,
            empty_nudges_used,
            len(getattr(reply, "reasoning", None) or ""),
            getattr(reply, "finish_reason", ""),
            sorted((getattr(reply, "raw", None) or {}).keys())
            if isinstance(getattr(reply, "raw", None), dict)
            else type(getattr(reply, "raw", None)).__name__,
        )
        reasoning = getattr(reply, "reasoning", None)
        if not reasoning:
            raw = getattr(reply, "raw", None) or {}
            if isinstance(raw, dict):
                candidate = raw.get("reasoning_content") or raw.get("thinking")
                reasoning = candidate if isinstance(candidate, str) and candidate else None

        # ══════════════════════════════════════════════════════════
        # ★ v1.25：用户按了「中断」/「彻底停止」
        # ══════════════════════════════════════════════════════════
        #
        # 检查点放在**模型回复之后**：流式循环里已经按 `should_break`
        # 提前停了收字，所以此刻 `raw_text` 就是"被打断时说到哪儿"。
        #
        # 为什么不抛异常：抛异常会被 `_run_guarded` 当成"中途断了"记成
        # `interrupted`，而 `interrupted` 会被后台 `resumer` 自动接手 ——
        # 与"用户明确要停"完全相反。所以这里正常返回 + `stopped` 标志。
        _stop = interrupt_bus.stop_requested(session.id)
        _note = None if _stop else interrupt_bus.take_note(session.id)
        if _stop or _note is not None:
            if text:
                _m = chat_service.append_assistant_message(
                    db, session=session, content=text, reasoning=reasoning,
                    surface="answer" if _stop else "process",
                )
                result.messages.append(_m)
                db.commit()
                await _emit_message(emit, _m)
            if _stop:
                result.stopped = True
                interrupt_bus.clear(session.id)
                return result
            # 中断并继续：把用户补充作为一条 user 消息注入，然后接着跑。
            _m2 = chat_service.append_user_message(
                db,
                session=session,
                content=INTERRUPT_NOTE.format(note=_note or "(无)"),
                # ★ 过程面：这句是我们**替用户写的注入模板**，不是他打的字。
                #   画成用户气泡会让人以为是自己说的话（v1.25）。
                surface="process",
            )
            result.messages.append(_m2)
            db.commit()
            await _emit_message(emit, _m2)
            await _emit_phase(emit, "resume", round_index, reason="user")
            continue

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
            #
            # ★ v1.31：跑过工具的轮次用**空回复额度**（见 `MAX_EMPTY_NUDGES`）。
            #   复跑实测：计划句之后模型连回两次空，2 次纠偏额度当场见底，
            #   于是「已经说过话」就 `return` —— 界面上最后一句仍是半句计划。
            # ★ v1.32：单独计数。与 `nudges_used` 共用时，前面几次叙述纠偏
            #   会把空回复的额度一起吃掉 —— 实跑 round 15 就是 6 次 format
            #   纠偏之后，第一个空回复当场判「用完」，又停在半句计划上。
            nudge_truncated = _is_truncated(reply)
            if nudge_truncated:
                nudge_allowed = truncations_used < MAX_TRUNCATION_CONTINUES
            elif tools_used > 0:
                nudge_allowed = empty_nudges_used < MAX_EMPTY_NUDGES
            else:
                nudge_allowed = (
                    nudges_used - truncations_used
                ) < MAX_NARRATION_NUDGES
            if not at_limit and nudge_allowed:
                nudges_used += 1
                if nudge_truncated:
                    truncations_used += 1
                elif tools_used > 0:
                    tool_nudges_used += 1
                    empty_nudges_used += 1
                truncated = nudge_truncated
                pending_reminder = (
                    render_truncation_reminder(len(raw_text))
                    if truncated
                    else render_format_reminder(
                        code_block_only=nudge_policy.has_unactioned_command(text)
                    )
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

            # ★ v1.33：上游只回了原生工具调用、正文为空（线上
            #   `chat_01M6556ES13EGH8NN712252SPB`）。纠偏额度用尽后**不许**
            #   沿 `already_spoke` 静默 return —— 那正是用户看到的
            #   「对话跑到一半就结束」。改为追加一句可见的实话。
            if reply.finish_reason in _NATIVE_FINISH_REASONS:
                note = chat_service.append_assistant_message(
                    db,
                    session=session,
                    content=render_native_tool_note(_native_names(reply)),
                )
                db.commit()
                result.messages.append(note)
                await _emit_message(emit, note)
                return result

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
                # ★ v1.31：额度见底、模型又不吽声时，最后一句话要是**计划**，
                #   就补一句实话再收尾 —— 否则用户看到的就是「话停在半句上」。
                last = next(
                    (
                        m
                        for m in reversed(result.messages)
                        if getattr(m, "role", None) == "assistant"
                    ),
                    None,
                )
                if last is not None and nudge_policy.looks_like_plan(
                    getattr(last, "content", "") or ""
                ):
                    exhausted = chat_service.append_assistant_message(
                        db, session=session, content=render_plan_exhausted_note()
                    )
                    db.commit()
                    result.messages.append(exhausted)
                    result.needs_continue = True
                    await _emit_message(emit, exhausted)
                return result

            _log.warning(
                "chat.giveup empty_reply nudges=%s tool_nudges=%s empty_nudges=%s "
                "truncations=%s at_limit=%s already_spoke=%s",
                nudges_used,
                tool_nudges_used,
                empty_nudges_used,
                truncations_used,
                at_limit,
                already_spoke,
            )

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

        # ★ v1.29：这次 FINAL 是不是**提前宣言**（跑过工具、还没打回过）。
        #   是的话它就不是结论，落 process —— 否则界面会先闪一条"结论"气泡，
        #   随后又被真正的结论顶掉（v1.25 修过同一形态）。
        #
        # ★ v1.30：额度改用 `plan_bounces`。计划句与"问过没有"无关 ——
        #   见 `MAX_PLAN_BOUNCES` 的长注释（线上会话 chat_01M640EY2…）。
        #
        # ★ v1.31：**不再要求模型自己写 `FINAL:`**。线上复跑
        #   chat_01M652CRXMJ4H9P1XABAJ4VGSH 的停点是：模型写
        #   「保护很弱：没有 PIE，也没有 canary。接下来查看 main、read_input
        #   等函数。」—— 它没写 FINAL，于是 `said_final=False`，这句话按
        #   `answer` 落库；紧接着两次空回复吃光纠偏额度，整轮 `done`，
        #   界面就停在半句计划上（用户：「输出一半就停了」）。
        #   计划句的判据是**句子本身**，与模型有没有自称 FINAL 无关。
        # ★ v1.35：同样不再要求 `tools_used > 0` —— 零工具轮次的计划句也
        #   是计划句（见 `nudge.looks_like_plan` 的长正文窗口）。
        # ★ v1.35：**被输出上限切断的回复不算计划句** —— 它并非"说完就停了
        #   "，而是话没说完；截断提醒（"接着写完"）比计划提醒更准，
        #   而且截断有自己的额度（`MAX_TRUNCATION_CONTINUES`）。
        plan_like_final = bool(
            not actions
            and not at_limit
            and not _is_truncated(reply)
            and nudge_policy.looks_like_plan(text)
        )
        result_block = bool(not actions and nudge_policy.looks_like_result(text))
        todos_now = todo_store.list_todos(db, session.id)
        open_steps = [row.text for row in todos_now if row.status != "done"]
        unfinished_plan = bool(
            not actions and not result_block and open_steps and tools_used > 0
        )
        # ★ v1.87：渗透任务只做过很少的真实探测就交结果块，不接受。
        #   旧实现只在认证达到 6 次时才会触发；但“SQL 试一下、XSS 试一下”
        #   同样可能只有 1~2 个方向。这里按**实际执行过的非认证动作**算，
        #   至少 3 个不同方向才允许收口，打回一次。
        is_pentest_task = (
            (session.persona or "") == "pentest" or brute_attempts > 0
        ) and tools_used > 0
        thin_pentest_final = bool(
            result_block
            and is_pentest_task
            and not delegated_work
            and len(non_auth_probe_sigs) < 3
            and strategy_bounces < 2
        )
        user_continue = _last_user_asks_continue(history)
        # 用户刚说继续、模型却只重贴结果：把它当过程，逼它执行下一步；
        # 只打回一次，第二次接受，避免死循环。
        force_continue_result = bool(
            result_block and user_continue and result_bounces < 1
        )
        premature_final = (
            (plan_like_final or unfinished_plan)
            and tools_used > 0
            and plan_bounces < MAX_PLAN_BOUNCES
        )
        assistant_message = chat_service.append_assistant_message(
            db,
            session=session,
            content=text,
            reasoning=reasoning,
            # ★ v1.25：**带动作的叙述是过程面**（"我先把附件解出来"），不画气泡。
            #   只有这一轮真的给出结论（没有 ACTION）时才算 `answer`。
            #   这样主对话流只剩：用户的话 + 最终答复 + 一个折叠的「过程详情」。
            # ★ v1.30：额度用完的计划句也不能当 `answer` —— 它是计划，不是结果。
            surface=(
                "answer"
                if (
                    not actions
                    and not premature_final
                    and not plan_like_final
                    and not force_continue_result
                    and not thin_pentest_final
                )
                else "process"
            ),
        )
        result.messages.append(assistant_message)
        db.commit()
        await _emit_message(emit, assistant_message)

        if not actions:
            # ★ v1.84：已经按交付格式给出「结果」块 = 本轮交付完成。
            #   结果块里的「未完成/限制」是边界说明，不是"还要继续"的计划；
            #   不在这里截断，`should_nudge` 会把它当半句话反复打回，
            #   模型于是重写同一份结论，界面上出现多条重复 answer。
            if result_block:
                if force_continue_result:
                    result_bounces += 1
                    pending_reminder = render_result_continue_reminder()
                    await _emit_phase(
                        emit,
                        "resume",
                        round_index,
                        reason="result-continue",
                        attempt=result_bounces,
                        maxAttempts=1,
                    )
                    continue
                if thin_pentest_final:
                    strategy_bounces += 1
                    pending_reminder = render_strategy_pivot_reminder(
                        brute_attempts,
                        final_attempt=True,
                        probes=len(non_auth_probe_sigs),
                    )
                    await _emit_phase(
                        emit,
                        "resume",
                        round_index,
                        reason="strategy-pivot",
                        attempt=strategy_bounces,
                        maxAttempts=2,
                    )
                    continue
                return result

            # ★ v1.64.1：这一轮模型没调任何工具（只写了正文）。累计"连续没调
            #   工具"的轮数 —— 一旦它跑过工具又连续 N 轮不再动手，就认定它是在
            #   作答而非中停（见 `MAX_NO_TOOL_NUDGES`）。执行工具的分支会把它
            #   清零。
            no_tool_rounds += 1
            # ★ v1.29：`FINAL:` 是模型自己宣告"做完了"。实测它会把这句话用在
            #   **进度播报**上（线上原文：`FINAL: 现在查 libc 版本。`），于是
            #   这一轮在跑完一个工具后就结束了。所以任务型轮次第一次收到
            #   FINAL 时先打回，问它一句"真的交付了吗"。
            if premature_final:
                plan_bounces += 1
                final_confirmations += 1
                pending_reminder = (
                    render_unfinished_plan_reminder(open_steps)
                    if unfinished_plan
                    else (
                        render_final_confirm_reminder(text)
                        if said_final
                        else render_format_reminder()
                    )
                )
                await _emit_phase(
                    emit,
                    "resume",
                    round_index,
                    reason="final-check",
                    attempt=plan_bounces,
                    maxAttempts=MAX_PLAN_BOUNCES,
                )
                continue

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
            #
            # ★ v1.32：计划句的打回额度用尽时**先**收尾，与
            #   `resume_approved` 的顺序对齐 —— 否则它会先走 tool nudge
            #   一路推到 300 次工具轮数，用户等的还是半句话。
            # ★ v1.35：只限**跑过工具**的轮次。零工具的计划句本质是
            #   "说要去做却没写 ACTION"，走下面的纠偏通道（`format`，
            #   额度 `MAX_NARRATION_NUDGES`）更准：它既不该当成答复，
            #   也不该占用"提前宣告结论"的预算。
            if unfinished_plan:
                # 计划还有未完成步骤，但这句话既不像计划、也没结果块：
                # 仍然不能当结论交出去；给用户一句明确的可继续说明。
                exhausted = chat_service.append_assistant_message(
                    db,
                    session=session,
                    content=render_plan_exhausted_note(),
                )
                db.commit()
                result.messages.append(exhausted)
                result.needs_continue = True
                await _emit_message(emit, exhausted)
                return result

            if plan_like_final and tools_used > 0:
                # ★ v1.30：打回额度用完，但这句话仍然是计划 —— **不能当结论
                #   交出去**。补一句实话，用户才知道该说「继续」。
                exhausted = chat_service.append_assistant_message(
                    db, session=session, content=render_plan_exhausted_note()
                )
                db.commit()
                result.messages.append(exhausted)
                # ★ v1.35：停在计划句上 = 还没给出结论，不能记成 `done`。
                result.needs_continue = True
                await _emit_message(emit, exhausted)
                return result

            if nudge_policy.should_nudge(
                at_limit=at_limit,
                said_final=said_final,
                actions=actions,
                truncated=_is_truncated(reply),
                tools_used=tools_used,
                text=text,
                nudges_used=nudges_used - truncations_used,
                max_nudges=MAX_NARRATION_NUDGES,
                truncations_used=truncations_used,
                max_truncations=MAX_TRUNCATION_CONTINUES,
                tool_nudges_used=tool_nudges_used,
                max_tool_nudges=MAX_TOOL_NUDGES,
                no_tool_rounds=no_tool_rounds,
                max_no_tool_rounds=MAX_NO_TOOL_NUDGES,
            ):
                nudges_used += 1
                if _is_truncated(reply):
                    truncations_used += 1
                elif tools_used > 0 or nudge_policy.has_unactioned_command(text):
                    # ★ v1.50：\"正文里写了 shell 命令却没走 ACTION\"同样走
                    #   `tool_nudges` 额度 —— 否则它既不在 `tools_used>0` 分支
                    #   累加计数、又绕过了 narration 额度（`has_unactioned_command`
                    #   分支直接返回 True），`tool_nudges_used < max_tool_nudges`
                    #   永远为真 → 死循环。
                    tool_nudges_used += 1
                # ★ v1.25：这句是**被纠偏推掉的提前结论**，不是最终答复。
                #   降级成过程面 —— 否则用户会看到两条几乎一样的结论气泡
                #   （实测：「`pwd` 输出是 `/work`。」+「当前工作目录是 `/work`。」）。
                truncated = _is_truncated(reply)
                pending_reminder = (
                    render_truncation_reminder(len(raw_text))
                    if truncated
                    else render_format_reminder(
                        code_block_only=nudge_policy.has_unactioned_command(text)
                    )
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
                continue

            # ★ v1.35：纠偏额度也用完了，但这句仍然是计划 —— 同样不能
            #   当结论交出去（零工具轮次走的就是这条通道）。补一句实话，
            #   并记下"这一轮还没答完"，由 `_run_guarded_active` 落
            #   `interrupted`。
            if plan_like_final:
                exhausted = chat_service.append_assistant_message(
                    db, session=session, content=render_plan_exhausted_note()
                )
                db.commit()
                result.messages.append(exhausted)
                result.needs_continue = True
                await _emit_message(emit, exhausted)
                return result

            _log.warning(
                "chat.giveup no_action said_final=%s plan_like=%s plan_bounces=%s "
                "nudges=%s tool_nudges=%s truncations=%s at_limit=%s text_len=%s",
                said_final,
                plan_like_final,
                plan_bounces,
                nudges_used,
                tool_nudges_used,
                truncations_used,
                at_limit,
                len(text),
            )
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
            if gate_on and _needs_approval(name):
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
            await _emit_phase(
                emit, "tool", round_index, tool=name, args=_phase_args_preview(args)
            )

            # ★ v1.87：认证/口令尝试到上限后**不结束整轮**。旧实现直接落
            #   “已停止”并 return，用户看到的是一次失败就交卷。现在只拒绝
            #   这一类工具，把“换策略族”的指令喂回去，让模型继续做别的面。
            if _looks_like_auth_attempt(name, args) and brute_attempts >= BRUTE_HARD_LIMIT:
                output = render_auth_attempt_blocked_note(brute_attempts)
                failed = True
                tools_used += 1
                no_tool_rounds = 0
                tool_message = chat_service.append_tool_message(
                    db,
                    session=session,
                    tool_name=name,
                    tool_args=args,
                    content=output,
                    failed=True,
                    duration_ms=0,
                )
                result.messages.append(tool_message)
                db.commit()
                await _emit_message(emit, tool_message)
                if pending_reminder is None:
                    pending_reminder = render_strategy_pivot_reminder(brute_attempts)
                _log.warning(
                    "chat.auth_blocked attempts=%s tool=%s session=%s",
                    brute_attempts,
                    name,
                    session.id,
                )
                continue

            tool_started = time.perf_counter()
            output, failed = await _execute_tool(
                ctx, name, args, on_output=_tool_output_emitter(emit, name),
                on_progress=_tool_progress_emitter(emit, name),
            )
            tool_elapsed_ms = int((time.perf_counter() - tool_started) * 1000)
            tools_used += 1
            no_tool_rounds = 0  # v1.64.1：真的动了手，连续没动手计数清零。
            # ★ v1.87：计划句的打回额度是**连续**额度，不是整轮总额。
            #   101 步 / 48 次工具的长任务里，模型每隔几个工具就会说一句
            #   “接下来查 X”；旧代码从不复位，4 句正常的计划就把额度吃光，
            #   最后落成「停在了下一步上」。执行了真实工具 = 还在推进，
            #   计划打回额度应当重新开始；连续 4 次只写计划不执行才是异常。
            plan_bounces = 0
            tool_message = chat_service.append_tool_message(
                db,
                session=session,
                tool_name=name,
                tool_args=args,
                content=output,
                failed=failed,
                duration_ms=tool_elapsed_ms,
            )
            result.messages.append(tool_message)
            db.commit()
            await _emit_message(emit, tool_message)

            # ★ v1.49：原地打转检测 —— 同一条命令（工具+参数完全相同）
            #   连续执行 MAX_REPEATED_ACTIONS 次 = 只可能在空转。
            #   判据只认这个最硬的信号（完全相同 = 不可能有新信息），不掺
            #   长度/语义启发式（技术性长文里误判率太高）。
            sig = _action_signature(name, args)
            recent_action_sigs.append(sig)
            if len(recent_action_sigs) > MAX_REPEATED_ACTIONS:
                del recent_action_sigs[0]
            if (
                not stall_warned
                and len(recent_action_sigs) >= MAX_REPEATED_ACTIONS
                and len(set(recent_action_sigs)) == 1
            ):
                # 只提醒一次：喂回去之后本轮不再重复同一句（否则又成死循环）。
                pending_reminder = render_stall_reminder(
                    name, len(recent_action_sigs)
                )
                stall_warned = True
                _log.warning(
                    "chat.stall tool=%s repeated=%s", name, len(recent_action_sigs)
                )

            # ★ v1.84：登录/口令尝试的停损。与"完全相同命令"是两条判据：
            #   爆破每次候选都不同，命令签名永远不重复，必须按策略族计数。
            # ★ v1.87：到上限只锁这一族，不再结束整轮；同时记录认证之外的
            #   真实探测，交付闸门据此拒绝“只试了登录”的结果块。
            if _looks_like_auth_attempt(name, args):
                brute_attempts += 1
                if brute_attempts >= BRUTE_HARD_LIMIT:
                    pending_reminder = render_strategy_pivot_reminder(brute_attempts)
                    _log.warning(
                        "chat.auth_limit attempts=%s session=%s", brute_attempts, session.id
                    )
                elif brute_attempts >= 6 and not brute_warned:
                    if pending_reminder is None:
                        pending_reminder = render_brute_stop_reminder(brute_attempts)
                    brute_warned = True
                    _log.warning(
                        "chat.brute_stop attempts=%s session=%s", brute_attempts, session.id
                    )
            elif _is_probe_action(name):
                non_auth_probe_sigs.add(sig)
            if name in {"spawn_subagents", "create_scan", "start_ctf_solve"}:
                delegated_work = True

    return result


async def resume_approved(
    db: Session,
    *,
    session: ChatSession,
    message: Any,
    identity: Any,
    gateway: Any = None,
    turn_run: Any = None,
    emit: Emit | None = None,
    locale: str | None = None,
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

    # ★ v1.80：审批后续跑也要重建 MCP 目录 —— 被批准的可能正是一个
    #   `mcp__…` 动态工具，它不在 `_TOOL_IMPLS` 里，只能从目录执行。
    #   后面那轮循环复用同一个 ctx，避免再试连一遍。
    ctx = await _build_tool_context(db, session, identity)

    output, failed = await _execute_tool(
        ctx,
        message.tool_name or "",
        message.tool_args or {},
        on_output=_tool_output_emitter(emit, message.tool_name or ""),
        on_progress=_tool_progress_emitter(emit, message.tool_name or ""),
    )
    chat_service.finish_approved_tool(
        db, session=session, message=message, output=output, failed=failed
    )
    result.messages.append(message)
    db.commit()

    if gateway is None:
        gateway = _build_gateway(_resolve_provider(session, None), db)

    system = _system_prompt_for(
        session,
        native_tools=_gateway_supports_tools(gateway),
        challenge_category=_challenge_category(db, session),
        mcp_catalog=ctx.mcp,
        db=db,
        locale=locale,
    )
    gate_on = bool(session.require_approval) and not _auto_approve_active(session)

    # ★ v1.13：与 `run_turn` 同一套纠偏预算。
    #
    # 不同步这条路径的后果是**只在审批之后**复现"输出一次就停了"：
    # 用户点完"确认"，模型说一句"好的，我来跑"就没了 —— 而这个形态
    # 恰恰更容易出现，因为刚批准的工具结果正要喂回去，它常常先播报一句。
    nudges_used = 0
    truncations_used = 0
    tool_nudges_used = 0
    #: 「模型一个字没写」被推了几次（v1.32，见 `MAX_EMPTY_NUDGES`）。
    empty_nudges_used = 0
    tools_used = 0
    #: ★ v1.64.1：见 `MAX_NO_TOOL_NUDGES`（run_turn 同名计数）。
    no_tool_rounds = 0
    final_confirmations = 0
    plan_bounces = 0
    pending_reminder: str | None = None

    #: ★ v1.68：本调用内"墙钟预算"的起点。见 `TURN_WALL_SECONDS`（同 run_turn）。
    turn_wall_start = time.monotonic()

    for round_index in range(
        MAX_TOOL_ROUNDS
        + MAX_NARRATION_NUDGES
        + MAX_TRUNCATION_CONTINUES
        + MAX_TOOL_NUDGES
        + MAX_FINAL_CONFIRMATIONS
    ):
        # ★ v1.68：墙钟预算闸（同 run_turn）。
        if (
            TURN_WALL_SECONDS
            and tools_used > 0
            and _turn_elapsed_seconds(turn_run, turn_wall_start) >= TURN_WALL_SECONDS
        ):
            exhausted = chat_service.append_assistant_message(
                db,
                session=session,
                content=render_turn_wall_note(TURN_WALL_SECONDS),
            )
            db.commit()
            result.messages.append(exhausted)
            result.hit_tool_limit = True
            result.needs_continue = True
            return result

        history = chat_service.list_messages(
            db, session=session, limit=HISTORY_MESSAGE_LIMIT
        )
        # ★ v1.69：同一结论复读闸（与 run_turn 同一判据/门槛，见那里的长注释）。
        if (
            tools_used >= REPEATED_CONCLUSION_MIN_TOOLS
            and round_index - nudges_used >= REPEATED_CONCLUSION_MIN_ROUNDS
            and nudge_policy.is_repeated_conclusion(_recent_assistant_texts(history))
        ):
            exhausted = chat_service.append_assistant_message(
                db,
                session=session,
                content=render_repeated_conclusion_note(),
            )
            db.commit()
            result.messages.append(exhausted)
            return result
        messages = _render_history(history)
        if not messages:
            messages = [{"role": "user", "content": "（继续之前的工作。）"}]
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
            # ★ v1.64：与主循环同款 —— 原生工具调用（到上限时不给 tools），
            #   并把回复过一遍 `_with_native_actions` 合成回 `ACTION:` 文本，
            #   这样下面 `parse_actions`/`_narration_before_action` 那段
            #   （本就为文本协议写的）原样能用，不出现"审批后续跑不会执行工具"。
            reply = _with_native_actions(
                await _gateway_chat(
                    gateway,
                    messages,
                    system=system_this_round,
                    tools=None if at_limit else _tools_schema(ctx),
                )
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("对话调模型失败（审批后）", exc_info=True)
            raise _wrap_model_failure(exc) from exc

        if turn_run is not None:
            turn_run_store.heartbeat(
                db,
                turn_run,
                round_index=round_index,
                nudges_used=nudges_used,
                truncations_used=truncations_used,
                tool_nudges_used=tool_nudges_used,
                tools_used=tools_used,
            )
            db.commit()

        raw_text = reply.text or ""
        # ★ v1.62：与 `run_turn` 主循环同一处规范化。历史遗漏 —— 这个
        #   "批准后继续"（resume_approved / tool-approval）分支原先**没有**
        #   调用 `_normalize_xml_actions`，于是模型自创的 XML 块（`<action>` /
        #   `<result_note>` / `<parameter>` 等）在这条路上原样漏进正文、也不
        #   参与动作解析。补齐后再做 `split_final` / `_narration_before_action`
        #   / `parse_actions`，与主循环保持完全一致。
        raw_text = _normalize_xml_actions(raw_text)
        # ★ v1.19：先摘掉开头的 `FINAL:`。必须在 `_narration_before_action`
        #   之前 —— 那个函数复用内核的 `ACTION|FINAL` 正则，会把
        #   `FINAL: 解出来了` 整条截成空串，再落成"模型没有产出内容"的占位语。
        raw_text, said_final = nudge_policy.split_final(raw_text)
        text = _narration_before_action(raw_text)
        actions = [] if at_limit else parse_actions(raw_text)
        # ★ v1.51：见主循环同一处注释 —— 贴了命令没写 ACTION 时直接当 run_shell 跑。
        if not actions and not at_limit and not said_final:
            _synth_cmd = nudge_policy.extract_trailing_command(text)
            if _synth_cmd:
                actions = [("run_shell", {"command": _synth_cmd})]
        reasoning = getattr(reply, "reasoning", None)
        if not reasoning:
            raw = getattr(reply, "raw", None) or {}
            if isinstance(raw, dict):
                candidate = raw.get("reasoning_content") or raw.get("thinking")
                reasoning = candidate if isinstance(candidate, str) and candidate else None

        # ══════════════════════════════════════════════════════════
        # ★ v1.25：用户按了「中断」/「彻底停止」
        # ══════════════════════════════════════════════════════════
        #
        # 检查点放在**模型回复之后**：流式循环里已经按 `should_break`
        # 提前停了收字，所以此刻 `raw_text` 就是"被打断时说到哪儿"。
        #
        # 为什么不抛异常：抛异常会被 `_run_guarded` 当成"中途断了"记成
        # `interrupted`，而 `interrupted` 会被后台 `resumer` 自动接手 ——
        # 与"用户明确要停"完全相反。所以这里正常返回 + `stopped` 标志。
        _stop = interrupt_bus.stop_requested(session.id)
        _note = None if _stop else interrupt_bus.take_note(session.id)
        if _stop or _note is not None:
            if text:
                _m = chat_service.append_assistant_message(
                    db, session=session, content=text, reasoning=reasoning,
                    surface="answer" if _stop else "process",
                )
                result.messages.append(_m)
                db.commit()
                await _emit_message(emit, _m)
            if _stop:
                result.stopped = True
                interrupt_bus.clear(session.id)
                return result
            # 中断并继续：把用户补充作为一条 user 消息注入，然后接着跑。
            _m2 = chat_service.append_user_message(
                db,
                session=session,
                content=INTERRUPT_NOTE.format(note=_note or "(无)"),
                # ★ 过程面：这句是我们**替用户写的注入模板**，不是他打的字。
                #   画成用户气泡会让人以为是自己说的话（v1.25）。
                surface="process",
            )
            result.messages.append(_m2)
            db.commit()
            await _emit_message(emit, _m2)
            await _emit_phase(emit, "resume", round_index, reason="user")
            continue

        if not text and not actions:
            nudge_truncated = _is_truncated(reply)
            # ★ v1.31：与 `run_turn` 同一条额度分级（空回复走 `MAX_EMPTY_NUDGES`）。
            # ★ v1.32：空回复单独计数，不被叙述纠偏吃额度。
            if nudge_truncated:
                nudge_allowed = truncations_used < MAX_TRUNCATION_CONTINUES
            elif tools_used > 0:
                nudge_allowed = empty_nudges_used < MAX_EMPTY_NUDGES
            else:
                nudge_allowed = (
                    nudges_used - truncations_used
                ) < MAX_NARRATION_NUDGES
            if not at_limit and nudge_allowed:
                nudges_used += 1
                if nudge_truncated:
                    truncations_used += 1
                elif tools_used > 0:
                    tool_nudges_used += 1
                    empty_nudges_used += 1
                pending_reminder = (
                    render_truncation_reminder(len(raw_text))
                    if nudge_truncated
                    else render_format_reminder(
                        code_block_only=nudge_policy.has_unactioned_command(text)
                    )
                )
                continue

            # ★ v1.33：与 `run_turn` 同一条原生工具调用兜底 —— 批准后上游
            #   只回工具调用时，不能落一句「批准后模型没有产出内容」了事。
            if reply.finish_reason in _NATIVE_FINISH_REASONS:
                note = chat_service.append_assistant_message(
                    db,
                    session=session,
                    content=render_native_tool_note(_native_names(reply)),
                )
                db.commit()
                result.messages.append(note)
                return result

            placeholder = chat_service.append_assistant_message(
                db,
                session=session,
                content="（批准后模型没有产出内容。可以再问一次，或换个接入点试试。）",
            )
            db.commit()
            result.messages.append(placeholder)
            return result

        # ★ v1.29：这次 FINAL 是不是**提前宣言**（跑过工具、还没打回过）。
        #   是的话它就不是结论，落 process —— 否则界面会先闪一条"结论"气泡，
        #   随后又被真正的结论顶掉（v1.25 修过同一形态）。
        # ★ v1.30：与 `run_turn` 同一条计划句闸（见 `MAX_PLAN_BOUNCES`）。
        # ★ v1.31：同样不再要求 `FINAL:` —— 计划句就是计划句（见 run_turn）。
        # ★ v1.35：也不再要求 `tools_used > 0`（与 run_turn 同步），同样
        #   不把被切断的回复当计划句（见 run_turn 那里的注释）。
        plan_like_final = bool(
            not actions
            and not at_limit
            and not _is_truncated(reply)
            and nudge_policy.looks_like_plan(text)
        )
        premature_final = (
            plan_like_final and tools_used > 0 and plan_bounces < MAX_PLAN_BOUNCES
        )
        assistant_message = chat_service.append_assistant_message(
            db,
            session=session,
            content=text,
            reasoning=reasoning,
            # ★ v1.25：**带动作的叙述是过程面**（"我先把附件解出来"），不画气泡。
            #   只有这一轮真的给出结论（没有 ACTION）时才算 `answer`。
            #   这样主对话流只剩：用户的话 + 最终答复 + 一个折叠的「过程详情」。
            surface=(
                "answer"
                if (not actions and not premature_final and not plan_like_final)
                else "process"
            ),
        )
        result.messages.append(assistant_message)
        db.commit()

        if not actions:
            # ★ v1.29：与 `run_turn` 同一条 FINAL 打回闸（见那边的长注释）。
            if premature_final:
                plan_bounces += 1
                final_confirmations += 1
                pending_reminder = (
                    render_final_confirm_reminder(text)
                    if said_final
                    else render_format_reminder()
                )
                continue

            # ★ v1.35：同 `run_turn` —— 只有跑过工具的计划句才走"提前宣告
            #   结论"的额度；零工具的计划句走下面的纠偏通道。
            if plan_like_final and tools_used > 0:
                # ★ v1.30：额度用尽的计划句不当结论，补一句实话（同 `run_turn`）。
                # ★ v1.35：并记下"这一轮还没答完"，由 `_run_guarded_active`
                #   落 `interrupted`。
                exhausted = chat_service.append_assistant_message(
                    db, session=session, content=render_plan_exhausted_note()
                )
                db.commit()
                result.messages.append(exhausted)
                result.needs_continue = True
                return result

            # ★ v1.13：与 `run_turn` 同一条判据顺序（见那边的长注释）：
            #   到上限 / 用尽纠偏 / `_looks_unfinished` —— 三者都过了才推。
            #
            # ★ v1.31：这段原本被一行无条件的 `return result` 挡在后面，
            #   永远不执行 —— 批准后的续跑从来没有纠偏过，模型写完一句
            #   计划就直接收尾（与 run_turn 的 v1.13 缺陷同源）。
            if nudge_policy.should_nudge(
                at_limit=at_limit,
                said_final=said_final,
                actions=actions,
                truncated=_is_truncated(reply),
                tools_used=tools_used,
                text=text,
                nudges_used=nudges_used - truncations_used,
                max_nudges=MAX_NARRATION_NUDGES,
                truncations_used=truncations_used,
                max_truncations=MAX_TRUNCATION_CONTINUES,
                tool_nudges_used=tool_nudges_used,
                max_tool_nudges=MAX_TOOL_NUDGES,
                no_tool_rounds=no_tool_rounds,
                max_no_tool_rounds=MAX_NO_TOOL_NUDGES,
            ):
                nudges_used += 1
                if _is_truncated(reply):
                    truncations_used += 1
                elif tools_used > 0 or nudge_policy.has_unactioned_command(text):
                    # ★ v1.50：\"正文里写了 shell 命令却没走 ACTION\"同样走
                    #   `tool_nudges` 额度 —— 否则它既不在 `tools_used>0` 分支
                    #   累加计数、又绕过了 narration 额度（`has_unactioned_command`
                    #   分支直接返回 True），`tool_nudges_used < max_tool_nudges`
                    #   永远为真 → 死循环。
                    tool_nudges_used += 1
                # ★ v1.25：这句是**被纠偏推掉的提前结论**，不是最终答复。
                #   降级成过程面 —— 否则用户会看到两条几乎一样的结论气泡
                #   （实测：「`pwd` 输出是 `/work`。」+「当前工作目录是 `/work`。」）。
                pending_reminder = (
                    render_truncation_reminder(len(raw_text))
                    if _is_truncated(reply)
                    else render_format_reminder(
                        code_block_only=nudge_policy.has_unactioned_command(text)
                    )
                )
                continue

            # ★ v1.35：纠偏额度用完而句子仍是计划 —— 同 `run_turn`，
            #   不当结论，记下"还没答完"。
            if plan_like_final:
                exhausted = chat_service.append_assistant_message(
                    db, session=session, content=render_plan_exhausted_note()
                )
                db.commit()
                result.messages.append(exhausted)
                result.needs_continue = True
                return result
            return result

        for name, args in actions:
            if gate_on and _needs_approval(name):
                pending = chat_service.mark_awaiting_approval(
                    db, session=session, tool_name=name, tool_args=args
                )
                result.messages.append(pending)
                result.awaiting_approval = pending.id
                db.commit()
                return result

            tool_started = time.perf_counter()
            tool_output, tool_failed = await _execute_tool(ctx, name, args)
            tool_elapsed_ms = int((time.perf_counter() - tool_started) * 1000)
            tools_used += 1
            no_tool_rounds = 0  # v1.64.1：真的动了手，连续没动手计数清零。
            tool_message = chat_service.append_tool_message(
                db,
                session=session,
                tool_name=name,
                tool_args=args,
                content=tool_output,
                failed=tool_failed,
                duration_ms=tool_elapsed_ms,
            )
            result.messages.append(tool_message)
            db.commit()

    return result


# ── v1.19：把"一轮"包成可持久化、可接手的东西 ─────────────────────


#: 中断时写进对话里的那句话。
#:
#: 为什么必须**落一条消息**而不是只写日志：改造前用户看到的是"半句话 +
#: 什么都不发生"，无从判断是卡了、断了、还是模型不想说了。一条说明至少
#: 告诉他"这一轮断了、我在后台接着做、也可以催我一句"。
INTERRUPTED_NOTE = "（这一轮中途断了：{reason}。我在后台接着做；也可以发「继续」催我一下。）"

#: ★ v1.25：「中断并继续」时注入给模型的那条 user 消息。
#:
#: 用户按了「中断」——他要的是**立刻别按原计划走**，并把自己的补充纳入。
#: 所以这句话必须同时交代两件事：① 上面那段输出被打断了；② 优先照新说明来。
INTERRUPT_NOTE = chr(10).join([
    "【用户中断并继续】",
    "你上一条输出被用户主动打断了。请把下面的补充纳入考虑，然后**直接继续做事**"
    "（写 ACTION），不要重复已经完成的步骤。",
    "",
    "用户补充：{note}",
    "（若用户没写补充，就基于已有结果继续推进，并在这一轮给出结论。）",
])

#: ★ v1.22：**不值得重试**的失败（密钥/权限）。说清"修好之后点重试"，
#: 因为用户唯一能做的事在设置页，而重试入口就在错误条上。
FAILED_NOTE = (
    "（这一轮没能跑起来：{reason}"
    "这类问题重试通常也一样 —— 请先按上面的提示处理（比如更新模型接入点的密钥），"
    "再点错误条上的「重试」，这一轮会从断点接着跑，不用重新发问。）"
)


def _collapse_turn_answers(db: Session, result: ChatTurnResult) -> None:
    """一轮里**只保留最后一条** assistant 消息作为 `answer`（v1.25）。

    ## 为什么

    用户实测：一次任务里出现两条几乎一样的结论气泡
    （「`pwd` 输出是 `/work`。」+「当前工作目录是 `/work`。」）。原因是
    纠偏机制会为了"可能没说完"再问一次模型，而模型常常把同一句话再说一遍。

    ## 为什么在**轮末**统一收拾，而不是在推之前逐句判断

    "这句像不像半句"是不可靠的：`pwd` 输出是 `/work`。这种 18 字的完整
    回答，任何长度/标点启发式都会误判；而误判成"说完了"就等于放行一次
    半路停（更贵的错）。所以判据保持宽松（宁可多问一次），但在轮末把
    **中间的** assistant 消息降级成过程面 —— 界面上永远只有一句结论。
    """
    # ★ v1.33：按**会话里最后一条用户消息之后**的全部 assistant 消息折叠，
    #   而不是只看 `result.messages`。后台接手会另起一个执行体，它拿不到
    #   原执行体那条结论；只折叠自己的列表，界面上就会留下两条 answer。
    #   以用户消息为界不需要给消息加 turn_id：同一轮的所有消息都排在它后面。
    if not result.messages:
        return
    session_id = getattr(result.messages[0], "session_id", None)
    if not session_id:
        return
    last_user_seq = db.scalar(
        select(ChatMessage.seq)
        .where(ChatMessage.session_id == session_id, ChatMessage.role == "user")
        .order_by(ChatMessage.seq.desc())
        .limit(1)
    )
    assistants = [
        m
        for m in db.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.session_id == session_id,
                ChatMessage.role == "assistant",
                ChatMessage.seq > (last_user_seq or 0),
            )
            .order_by(ChatMessage.seq)
        )
        if (m.content or "").strip()
    ]
    changed = False
    for earlier in assistants[:-1]:
        if getattr(earlier, "surface", "answer") != "process":
            earlier.surface = "process"
            changed = True
    if changed:
        db.commit()


async def _run_guarded(
    db: Session,
    *,
    session: ChatSession,
    kind: str,
    call,
) -> ChatTurnResult:
    """跑一轮并把执行状态落库；失败时留下可接手的状态与一句说明。

    `kind` 只用于日志（"turn" / "resume"），不参与判据。
    """
    run = turn_run_store.start(db, session_id=session.id)
    db.commit()
    # ★ v1.33：本进程正在跑的轮次不能被本进程的后台 resumer 抢走。
    #   心跳是 90 秒粒度，长工具期间会显得过期；集合在 finally 里清掉，
    #   进程崩溃后集合自然消失，跨进程接手仍由数据库心跳决定。
    turn_run_store.mark_active(run.id)
    try:
        return await _run_guarded_active(
            db, session=session, kind=kind, run=run, call=call
        )
    finally:
        turn_run_store.unmark_active(run.id)


async def _run_guarded_active(
    db: Session,
    *,
    session: ChatSession,
    kind: str,
    run: Any,
    call,
) -> ChatTurnResult:
    """`_run_guarded` 的主体：此时 `run` 已经登记为本进程活跃轮次。"""
    try:
        result = await call(run)
    except BaseException as exc:  # noqa: BLE001 - 见下
        # ⚠️ 接 `BaseException` 是刻意的：客户端断开时 `stream_turn` 会
        #   `task.cancel()`，抛的是 `CancelledError` —— 那恰恰是**最常
        #   见**的"又没反应"，如果只接 `Exception` 就把它漏成"没人管的
        #   running 行"，后台永远不接手。
        reason = turn_run_store.redact(f"{type(exc).__name__}: {exc}")
        # ★ v1.22：不可重试的失败（凭证错）直接落 `failed`，别再伪装成
        #   "中途断了、我在后台接着做" —— 后台接手会一路重试到上限，
        #   用户会看到同一条错误反复出现三遍。`failed` 的语义正是"自动别再来"。
        retryable = bool(getattr(exc, "retryable", True))
        try:
            db.rollback()
            if retryable:
                turn_run_store.finish_from_error(db, run, reason=reason)
                chat_service.append_assistant_message(
                    db, session=session, content=INTERRUPTED_NOTE.format(reason=reason)
                )
            else:
                turn_run_store.finish(db, run, status="failed", error=reason)
                chat_service.append_assistant_message(
                    db, session=session, content=FAILED_NOTE.format(reason=reason)
                )
            db.commit()
        except Exception:  # noqa: BLE001 - 连记账都失败就别再抛新的
            _log.warning("记录中断状态失败（%s）", kind, exc_info=True)
        raise
    # ★ v1.25：一轮只留最后一条 assistant 作为对话内容（见 `_collapse_turn_answers`）。
    _collapse_turn_answers(db, result)

    # ★ v1.25：用户主动停掉的这一轮落 `cancelled` —— 不能落 `done`（会被当成
    #   跑完了），也不能落 `interrupted`（后台 resumer 会自动接手，与"别再跑"相反）。
    # ★ v1.35：停在计划句上的轮次落 `interrupted` —— 它还没给出结论，落 `done`
    #   会让界面和后台都以为"答完了"。
    if result.stopped:
        turn_run_store.finish(db, run, status="cancelled")
    elif result.needs_continue:
        turn_run_store.finish_incomplete(db, run)
    else:
        turn_run_store.finish(db, run, status="done")
    db.commit()
    return result


async def run_turn_durable(
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
    resume_from: Any = None,
    allowed_tools: set[str] | None = None,
    locale: str | None = None,
) -> ChatTurnResult:
    """`run_turn` 的持久化外壳（生产路径都走这个）。"""

    async def call(run: Any) -> ChatTurnResult:
        return await run_turn(
            db,
            session=session,
            content=content,
            identity=identity,
            provider_id=provider_id,
            gateway=gateway,
            attachment_ids=attachment_ids,
            explicit_tools=explicit_tools,
            emit=emit,
            turn_run=run,
            resume_from=resume_from,
            allowed_tools=allowed_tools,
            locale=locale,
        )

    return await _run_guarded(db, session=session, kind="turn", call=call)


async def resume_approved_durable(
    db: Session,
    *,
    session: ChatSession,
    message: Any,
    identity: Any,
    gateway: Any = None,
    locale: str | None = None,
) -> ChatTurnResult:
    """`resume_approved` 的持久化外壳。"""

    async def call(run: Any) -> ChatTurnResult:
        return await resume_approved(
            db,
            session=session,
            message=message,
            identity=identity,
            gateway=gateway,
            turn_run=run,
            locale=locale,
        )

    return await _run_guarded(db, session=session, kind="resume", call=call)


__all__ = [
    "HISTORY_MESSAGE_LIMIT",
    "HISTORY_TOOL_CHARS",
    "INTERRUPTED_NOTE",
    "MAX_MOUNTED_SKILL_CHARS",
    "MAX_TOOL_ROUNDS",
    "ChatEngineError",
    "ChatTurnResult",
    "resume_approved",
    "resume_approved_durable",
    "run_turn",
    "run_turn_durable",
]
