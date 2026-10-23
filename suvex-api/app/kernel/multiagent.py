"""完整多智能体编排 —— root 调度、子 agent 并行执行（v0.58）。

## 为什么要有这个文件

v0.57 之前，`solve_in_sandbox` 是**一条** ReAct 循环：一个模型、一份上下文、
一条 `recent` 列表从头跑到尾。这在"一道题只需要一条思路"时够用，但有两类
结构性失败它修不了：

1. **上下文被一件事撑满**。侦察阶段抓回来的 HTML、扫出的一堆路径、跑失败的
   几屏输出，全堆在同一条 `recent` 里。到真正要深挖某个漏洞时，前面几十轮
   的噪声已经把窗口占满（`MAX_HISTORY_TURNS` 不停往前删 —— 删掉的就是它
   自己的记忆）。实测原话：「沙箱无外网…只能靠 http_request 逐个 URL 请求」，
   那种探索会吃满上下文。
2. **一次只能想一件事**。侦察、验证 A、验证 B 是**天然可并行**的，但一条循环
   只能串行。总轮次是 `spec.max_turns`，串行意味着每次切方向都在消耗同一份预算。

这不是"多加几个工具"能解决的 —— 要的是**多个独立的上下文、能并发、能互相
传结论**。这正是 strix（`strix/agents/factory.py`）做的事，也是它比我们强的地方。

## 与 strix 的关系（学它什么、不学它什么）

**学**：root agent 只调度不亲自动手（`root_agent_directive`）；每个漏洞类型/
资产开一个专职子 agent；子 agent 完成后用 `agent_finish` 交一份结构化报告；
共享 `notes`（跨 agent 草稿纸）与 `coverage`（谁测过什么，包括"干净"的也记）。

**不学**：它把整套 LangGraph + 35KB YAML 塞进一个运行时。我们用**自己的文本
协议**（`ACTION:` / `FINAL:`，见 `runner.py` 模块 docstring 第 2 条），因为它
已经被证明能跑通所有 provider，换成原生 tools 等于把测过的路径丢掉重来。

## 结构

```
run_multi_agent(spec, sandbox, gateway, system_prompt, ...)
  └─ AgentCoordinator            # 图 + 状态 + 信箱 + 共享状态，全在内存
       ├─ AgentNode(root)        # 只调度；工具集含 create_agent / wait / finish_scan
       │    └─ _agent_loop(...)  # 一条独立的 ReAct 循环（复用 host_tools 那套）
       ├─ AgentNode(child A)     # 每轮各自 gateway.chat()
       └─ AgentNode(child B)
```

## 三条纪律（与 runner.py 一致）

1. **不碰数据库**。这个模块是 async，而项目的 Session 是同步的。所有落库在
   `_run_worker` 的同步段做 —— 这里只通过 `on_progress` 回调把过程送出去。
2. **回调不许抛**。事件写失败不该让一场已经跑了二十轮的编排停摆。
3. **沙箱是共享的**。所有 agent 复用**同一个** `SandboxContainer` —— 它们并发
   执行命令，靠 shell 会话隔离（每条命令一个独立 `docker exec`）+ 各自的
   `/work/agents/<agent_id>` 工作目录避免踩踏。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.kernel import termination
from app.kernel.client import KernelError
from app.kernel.runner import (
    TOOL_NAMES,
    LlmGateway,
    SandboxContainer,
    SolveOutcome,
    SolveSpec,
    _final_stretch_note,
    _format_action_results,
    _format_reminder,
    _human_hint_note,
    _initial_prompt,
    _narration_before_action,
    _output_fingerprint,
    _sink_findings,
    _truncation_reminder,
    dispatch_tool,
    extract_candidates_with_reasons,
    extract_final_analysis,
    parse_actions,
    parse_final,
)

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════

#: root 一次最多能开多少个子 agent。**这是硬上限，不是建议**。
#:
#: ## 为什么要封顶
#:
#: 每个子 agent 都是一条独立循环，都各自消耗 token。不开顶的话，一个"帮我看看
#: 有哪些漏洞"的 instruction 就能让模型开出二十个 agent，而每一次 `gateway.chat`
#: 都是真金白银。20 个已经远超"一道渗透题值得并行"的量级。
#:
#: ⚠️ 触顶时 `create_agent` **返回错误信息**而不是静默拒绝 —— 模型得知道
#:    "我开不动了"，否则它会以为 spawn 成功、然后空等一个永远不启动的子 agent。
MAX_CHILDREN = 6

#: 整场编排的**总轮次预算**（所有 agent 加起来的 LLM 调用上限）。
#:
#: ## 为什么是总量而不是每 agent
#:
#: `spec.max_turns` 是给**一条**循环的数（默认 80）。这里开多条循环，但用户的
#: 成本预期是"一道题花这么多"。所以编排的总预算是 `max_turns` 的倍数，各 agent 分。
#: 一个子 agent 跑满自己的预算就把大半个总量吃了 —— 这正是我们要让它**省着开**的
#: 理由（也是 `create_agent` 提示词里强调"只在大/可并行时才开"的动机）。
#:
#: ## ★ 为什么从 1.0 改成 3.0（v0.67 真跑暴露）
#:
#: 原来 `TOTAL_TURN_RATIO = 1.0` —— 总预算 = `max_turns`（80）。但那是给
#: **一条**循环定的数。多智能体是 root + 最多 6 个子 agent 共 7 条循环来分这 80：
#: 实测一次真实渗透（`scan_01M5HEJ0…`，6 个智能体）**恰好跑满 80 轮预算就断**，
#: 4 个专职子 agent（auth / static-map / sqli / ssrf-rce）每条只分到十几二十轮，
#: 全卡在侦察/探口令阶段、一条 confirmed 都没收敛到就没了。
#:
#: 这不是"模型不行"，是**预算把并行的好处吃掉了**：开 4 个子 agent 本是为了
#: 并行覆盖更多面，结果总预算没跟着涨，等于把 80 轮切成 4 份，比单循环还浅。
#:
#: 所以总预算改按**预期并行宽度**放大：`max_turns * 3`（root + 约 3 个典型
#: 子 agent 的量级）。定 3 不是 7 是因为：①`child_turn_budget` 已把子 agent
#: 单个上限压到十几二十轮，多数子 agent 用不满；②真开到 6 个是少数，且多开
#: 会有 `remaining_budget` 的硬闸兜底；③3 倍在"够收敛"与"不烧钱"之间。
#: **这是一条成本/覆盖的权衡，不是免费的** —— 单次多智能体扫描的 LLM 成本
#: 会随之上浮，用户可在设置里把回合上限（= base）调小来对冲。
TOTAL_TURN_RATIO = 3.0  # 总预算 = int(spec.max_turns * TOTAL_TURN_RATIO)

#: 一个子 agent 最多跑多少轮（单 agent 上限）。
#:
#: 开太多会导致"一个子 agent 吃光预算"，开太少则子 agent 还没摸到东西就没了。
#: 取 `max(12, total // (MAX_CHILDREN // 2))` —— 让 3 个典型子 agent 各能跑
#: 十几到二十轮，够做完一次聚焦的验证。
def child_turn_budget(spec: SolveSpec) -> int:
    total = max(12, int(spec.max_turns * TOTAL_TURN_RATIO))
    return max(12, total // max(2, MAX_CHILDREN // 2))


#: 编排工具名。**不要**加进 `TOOL_NAMES` —— 那个元组是"所有 agent 都能调的基础
#: 工具"，而编排工具只有 root（部分只有子 agent）能调，见 `_root_tools` /
#: `_child_tools`。
CREATE_AGENT = "create_agent"
SEND_MESSAGE = "send_message_to_agent"
WAIT_FOR_AGENTS = "wait_for_agents"
VIEW_GRAPH = "view_agent_graph"
AGENT_FINISH = "agent_finish"
FINISH_SCAN = "finish_scan"
#: 共享草稿纸（root 与子 agent 都能读写）。
NOTE_WRITE = "write_note"
NOTE_LIST = "list_notes"
#: 覆盖度账本（"谁测过什么"，含"干净"的也记）。
COVERAGE_RECORD = "record_coverage"
COVERAGE_LIST = "list_coverage"
#: ★ v0.70 从 strix 学来的三个小工具。
#:
#: - `STOP_AGENT`：root 主动叫停某个跑偏/已无用的子 agent —— 没有它，一个方向
#:   错的子 agent 会一直烧预算到自己的上限（strix 有 stop_agent，我们之前只能等）。
#: - `THINK`：给模型一个**不调工具、只写推理**的出口。strix 有 think；我们的协议
#:   里纯文本回合被视为浪费（要求几乎每轮一次工具调用），但"动手前先把话说清"
#:   恰恰是减少瞎试的关键。THINK 让它记一段推理进上下文，不算白板轮。
#: - `TODO_*`：私有待办清单。root 用它把大目标拆成可跟踪的步骤，避免"走一步看一步"。
STOP_AGENT = "stop_agent"
THINK = "think"
TODO_CREATE = "create_todo"
TODO_LIST = "list_todos"
TODO_UPDATE = "update_todo"
#: ★ 共享威胁模型（学 strix）。root 建全局攻击面，子 agent 可读可补充。
THREAT_GET = "get_threat_model"
THREAT_AMEND = "amend_threat_model"

#: ★ v0.76：判定"原地打转"的滑窗参数。
#:
#: `ACTION_WINDOW = 20` 而非更大：门的判定发生在模型想收口的那一刻，
#: 而它想收口通常正是因为"最近这一段没什么进展"——看 20 条已能覆盖
#: 一次完整的探测试验；看 40+ 会把早期有效的侦察也混进来，稀释信号。
#: `ACTION_WINDOW_MIN_SAMPLES = 10`：样本太少时比例没有意义，
#: 宁可判"不知道"（`acting_ratio=None`）也不要误伤刚开始的扫描。
ACTION_WINDOW = 20
ACTION_WINDOW_MIN_SAMPLES = 10

#: ★ v0.77：同一个"打点形状"撞到第几次时提醒模型"换参数不算新尝试"。
#:
#: 3 而不是 2：第二次打同一个点很可能是正常的分支验证（先探 401，再试
#: 未带凭据）；到第三次基本可以确认是在原地换参数。
#: 提醒**只发一次**（`==` 而不是 `>=`）：反复提醒会挤占上下文，而模型的
#: 行为已经被那句提醒影响过一次了，再刷只会让它分心。
SHAPE_REPEAT_WARN_AT = 3

#: 一次 `run_shell` 里出现这些词，算"真的向目标发了请求"。
#:
#: 用**动作词**而不是"是否包含 http"：`grep -oE 'http[A-Za-z]*' bundle.js`
#: 里有 http 字样但一次请求都没发。反过来 `nmap -p- 10.0.0.5` 没有 http
#: 字样但确实是打目标。所以这里收的是发请求/发包的工具名。
_ACTING_TOKENS = (
    "curl",
    "wget",
    "nmap",
    "ffuf",
    "gobuster",
    "dirb",
    "nikto",
    "whatweb",
    "sqlmap",
    "nc ",
    "ncat",
    "socat",
    "openssl s_client",
    "mqtt",
    "redis-cli",
    "mysql",
    "psql",
    "mongosh",
    "docker ",
)

def _looks_like_acting(*, tool_name: str, command: str) -> bool:
    """这一轮有没有真的向目标发起交互。

    判定顺序（2026-10-23 P2 真跑后定下的）：

    1. **`http_request`** → 一定是行动（它除了发请求没别的用途）。
    2. **其他非 `run_shell` 工具**（`record_coverage` / `write_note` /
       `think` / `create_agent` …）→ 未行动：记账与思考不碰目标。
    3. **`run_shell` 的命令里有活体请求词**（`curl`/`nmap`/`ffuf`…）→ 行动。
       哪怕同一条命令里也顺手 `grep` 了 —— 有一发打出去了就算打了。
    4. 其余（`grep 本地文件`、`python3` 解析、写脚本、`ls`）→ 未行动。

    ⚠️ 这是**启发式**，不是精确判定。它只回答"最近是不是在原地打转"，
    误判的代价是"门多给或少给一句换向建议"，不影响结论对错。
    用固定词表而不是解析命令行语法，是因为后者的复杂度远超收益。
    """
    if tool_name == "http_request":
        return True
    if tool_name != "run_shell":
        return False
    low = command.lower()
    return any(tok in low for tok in _ACTING_TOKENS)

#: ★ v0.77：把一次请求抽象成"打的是哪个点"，用于判定"换参数算不算重复"。
#:
#: ## 为什么需要它（学自腾讯"持续攻击模式"队的 ctf.md「已排除路径」）
#:
#: 那支队伍（2026 智能渗透黑客松第 7 名）在 ctf.md 里维护一个
#: 「已排除路径（禁止重复）」清单，硬约束写得极狠：
#:
#: > ✗ 禁止重复已在「已排除路径」中的命令/URL/payload（**换参数也算重复**）
#: > ✗ 禁止重复已在 session 日志中跑过的命令
#: > ✗ 禁止反复验证已确认的事实
#:
#: 我们的项目**已有**的重复检测（`seen_calls`）只认"命令 + 输出逐字节相同"
#: —— 那太窄了。P2 那轮实证：`/api/v1/scans` 被探 **13 次**、
#: `/api/v1/auth/login` **12 次**、`/api/v1/auth/state` **9 次**，
#: 每次参数都略有不同（换 payload、换 header、换方法），
#: 所以 `seen_calls` 一次都没命中，模型在同一个点上原地耗尽 80 轮。
#:
#: 这里要做的是**粗粒度**的"同一个点"判定：抽掉参数值、留下动作形状。
#: 它判的不是"完全一样"，而是"只是换了参数"——正好补上那个缺口。
def target_shape(*, tool_name: str, command: str, args: dict[str, Any] | None = None) -> str:
    """把一次动作归约成"打的是哪个点"的形状串。

    返回空串 = 判不出（调用方跳过这条，不参与重复统计）——
    宁可漏判也不要错判：把两个不同的点误判成同一个，会阻止模型做该做的事。

    ## 归约规则

    - `http_request`：`method + 路径模板`（路径里的数字段归一成 `{id}`）
    - `run_shell` 里有 URL 的：所有 URL 的**路径部分**去参数、去 query
    - `run_shell` 里没有 URL 的：不对应"打点"，返回空串
      （本地分析类动作由 `_looks_like_acting` 那套管，不该混进这里）

    ## 刻意不做的事

    不归一化 payload、不归一化 header、不猜 `-d` 里的语义。
    判据只到"同一个路径被反复打"这一层 —— 再细就要解析命令行语义，
    复杂度和误判率都会上去，而收益（多提醒一句"换条路"）并不值得。
    """
    if tool_name == "http_request":
        a = args or {}
        method = str(a.get("method") or "GET").upper()
        url = str(a.get("url") or "")
        path = _url_path(url)
        return f"http_request {method} {_normalize_path(path)}" if path else ""

    if tool_name != "run_shell":
        return ""

    # `run_shell`：抽出命令里所有 URL 的路径，按出现顺序去重。
    paths: list[str] = []
    for m in _URL_IN_COMMAND.finditer(command):
        p = _normalize_path(_url_path(m.group(0)))
        if p and p not in paths:
            paths.append(p)
    if not paths:
        return ""
    return "run_shell " + " ".join(sorted(paths))


#: 命令里的 http(s) URL（到空白或引号为止）。
_URL_IN_COMMAND = re.compile(r"https?://[^\s'\"`)]+", re.IGNORECASE)

#: 路径里的纯数字段 → `{id}`。
#:
#: 这是"换参数也算重复"的关键一步：`/scans/1` 与 `/scans/2` 打的是同一个点。
#: 只对**整段都是数字**的段生效 —— `/v1` 这种带字母的不动。
_ID_SEGMENT = re.compile(r"/\d+(?=/|$)")


def _url_path(url: str) -> str:
    """从 URL 里取出路径部分（丢掉 scheme/host/query/fragment）。"""
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return ""
    return parsed.path or ""


def _normalize_path(path: str) -> str:
    """路径归一：数字段换成 `{id}`，去掉尾斜杠。"""
    if not path or path == "/":
        return "/"
    cleaned = _ID_SEGMENT.sub("/{id}", path)
    return cleaned.rstrip("/") or "/"

#: ★ v0.75：Idea / Memory 二分（学 BreachWeave）。
#:
#: `idea` 记"往哪走"，`fact` 记"已确认什么"。两者分开存、分开读 ——
#: 详见 `SharedState.ideas` 那段注释里为什么不能用现成的 `notes`。
IDEA_ADD = "add_idea"
IDEA_RESOLVE = "resolve_idea"
MEMORY_ADD = "add_fact"
MEMORY_GET = "get_memory"

#: Idea 的合法状态。`open` 待做 / `doing` 在做 / `done` 做成了 / `dropped` 走不通。
IDEA_STATUS = frozenset({"open", "doing", "done", "dropped"})

#: 事实的合法类型。`negative`（已排除）与 `positive` 同等重要 —— 它防止重走死路。
FACT_KINDS = frozenset({"positive", "negative", "constraint"})

_ROOT_ONLY = frozenset({
    CREATE_AGENT,
    SEND_MESSAGE,
    WAIT_FOR_AGENTS,
    VIEW_GRAPH,
    FINISH_SCAN,
    STOP_AGENT,
    TODO_CREATE,
    TODO_LIST,
    TODO_UPDATE,
})
_CHILD_ONLY = frozenset({AGENT_FINISH})
#: 威胁模型**双方都能读**（子 agent 要知道全局假设才知道自己那块的意义）；
#: 但 `amend` 的**写**权限 root 与子 agent 都开 —— 子 agent 在一线，最可能
#: 补充真实边界（"这个 /admin 其实不鉴权"），只让 root 写会丢信息。
_SHARED = frozenset({
    NOTE_WRITE,
    NOTE_LIST,
    COVERAGE_RECORD,
    COVERAGE_LIST,
    THINK,
    THREAT_GET,
    THREAT_AMEND,
    IDEA_ADD,
    IDEA_RESOLVE,
    MEMORY_ADD,
    MEMORY_GET,
})


def root_tool_names() -> tuple[str, ...]:
    return (*TOOL_NAMES, *_ROOT_ONLY, *_SHARED)


def child_tool_names() -> tuple[str, ...]:
    return (*TOOL_NAMES, *_CHILD_ONLY, *_SHARED)


# ══════════════════════════════════════════════════════════
# 状态：图 / 信箱 / 共享状态
# ══════════════════════════════════════════════════════════

#: agent 的四种状态。与 strix 的 `Status` 同名子集。
#:
#: ⚠️ **没有** "waiting/parked" —— 我们的子 agent 不是可被消息唤醒的常驻任务，
#: 而是"跑完就结束"的一次性协程。root 用 `wait_for_agents` 等它们全部结束，
#: 而不是等一条消息。这个差异很重要：它让"死锁"不可能发生（没有互相等待）。
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"


@dataclass
class AgentNode:
    """多智能体图上的一个节点。"""

    agent_id: str
    name: str
    task: str
    parent_id: str | None
    #: 这个 agent 自己那条循环的 outcome（子 agent 的候选/发现汇到 root 之前先放这）。
    outcome: SolveOutcome = field(default_factory=SolveOutcome)
    status: str = STATUS_RUNNING
    #: 子 agent 交回来的完成报告（纯文本，进 root 的下一轮上下文）。
    completion_report: str = ""
    #: ★ v0.70：root 用 `stop_agent` 叫停本节点时置 True。子 agent 循环每轮开头
    #: 检查它，看到就体面收尾（协作式，不 cancel task —— 见 `_do_stop_agent`）。
    stop_requested: bool = False

    def is_terminal(self) -> bool:
        return self.status in (STATUS_COMPLETED, STATUS_FAILED, STATUS_STOPPED)


@dataclass
class SharedState:
    """跨 agent 的共享草稿纸与覆盖度账本。

    ## 为什么是内存而不是 DB

    它们只在这场编排里活着 —— 编排结束，结论已经汇进 `SolveOutcome`。
    落库的是**结论**（findings / candidates / summary），不是中间草稿。
    把草稿也落库会让"这条笔记是谁在哪个扫描里写的"变成一个需要清理的
    历史包袱，而它没有复盘价值（过程已经在 events.jsonl 里）。
    """

    #: 共享草稿：标题 → 正文。**去重靠标题**（同一标题覆盖写）。
    notes: dict[str, str] = field(default_factory=dict)
    #: 覆盖度：surface → {"risk": str, "outcome": str, "by": str}。
    coverage: dict[str, dict[str, str]] = field(default_factory=dict)
    #: ★ v0.70：共享威胁模型（学 strix 的 get_threat_model / amend_threat_model）。
    #:
    #: 与 `notes` 的区别：notes 是**平铺的事实**（"发现 X 路径没鉴权"），
    #: threat_model 是**结构化的攻击假设**：资产 → 信任边界 → 可能的弱点 →
    #: 优先级。它回答"我们**打算**怎么打、为什么这样排"，notes 回答"我们**看到**了什么"。
    #: 混淆两者会让 root 在派活时没有全局的攻击面视图 —— 子 agent 各打各的，
    #: 重复覆盖同一块、漏掉整片死角。
    #:
    #: 结构：assets / trust_boundaries / hypotheses / priorities（全部是字符串列表）。
    threat_model: dict[str, list[str]] = field(
        default_factory=lambda: {
            "assets": [],
            "trust_boundaries": [],
            "hypotheses": [],
            "priorities": [],
        }
    )

    def amend_threat_model(self, field_name: str, items: list[str], *, by: str) -> list[str]:
        """往威胁模型的某一栏追加条目（去重），返回该栏当前全量。

        `field_name` 必须是四栏之一；认不出就抛 ValueError（调用方转成
        `_json_err`，不让一个错拼的栏名悄悄写进一个没人读的键）。
        """
        if field_name not in self.threat_model:
            raise ValueError(f"未知的威胁模型栏位 `{field_name}`")
        bucket = self.threat_model[field_name]
        seen = {s.strip().lower() for s in bucket}
        for raw in items:
            text = str(raw).strip()
            if text and text.lower() not in seen:
                bucket.append(f"{text}（by {by}）")
                seen.add(text.lower())
        return list(bucket)

    def render_threat_model(self) -> str:
        labels = {
            "assets": "资产",
            "trust_boundaries": "信任边界",
            "hypotheses": "攻击假设",
            "priorities": "优先级",
        }
        parts = []
        for key, label in labels.items():
            items = self.threat_model.get(key) or []
            body = "\n".join(f"  - {s}" for s in items) if items else "  （空）"
            parts.append(f"### {label}\n{body}")
        return "\n".join(parts)

    # ── ★ v0.75：Idea / Memory 二分（学 BreachWeave 的"状态分层维护"）──
    #
    # ## 为什么要把"往哪走"和"已确认什么"拆开
    #
    # 现在两者混在同一处：`notes`（平铺事实）+ `threat_model.hypotheses`
    # （攻击假设）+ `coverage`（测过什么）。混着放的后果实测能看见 ——
    # P1 那轮 agent 的共享笔记里同时躺着"确认了 CORS 反射"和"也许可以试试
    # 前端 chunk"，而 root 每轮都要整块读一遍，读到的信噪比随着轮次下降，
    # 到最后它自己也分不清哪条是事实、哪条是还没验的猜想。
    #
    # BreachWeave 的拆法：
    #   · Idea   = "当前值得继续推进的方向"（假设，**可废弃**）
    #   · Memory = "可复用的事实、证据与约束"（事实，**不可废**）
    #
    # 关键差别不在存储，在**生命周期**：
    #   · Idea 有状态：open → doing → done / **dropped**（走不通就废弃，
    #     并且要写为什么）。废弃的 Idea 会进 facts 当负面事实用。
    #   · Memory 只增不减，每条都带证据来源。
    #
    # ## 为什么不用现成的 notes
    #
    # `notes` 是无结构的 标题→正文，谁都能写、谁都能改、没有状态。
    # 一个"我打算试试 SPA 里那个 chunk"和一个"确认 `/api/v1/users` 无鉴权"
    # 在 notes 里长得一样。Idea 需要状态机，Memory 需要证据字段，两者都不该
    # 塞进一个裸 dict。

    #: 待推进方向：idea_id → {text, status, by, why(废弃原因), evidence}
    #:
    #: status 取值见 `IDEA_STATUS`。**不是 open/doing 的 Idea 不再进 root 的
    #: 提示**（做完了和走不通的都不该再占上下文）。
    ideas: dict[str, dict[str, str]] = field(default_factory=dict)
    #: 已确认事实：fact 文本 → {evidence, by, kind}
    #:
    #: `kind` 是 `positive`/`negative`/`constraint` —— 阴性事实（"这条路
    #: 走不通，因为…"）**同样重要**，它防止下一个 agent 重走一遍。
    facts: dict[str, dict[str, str]] = field(default_factory=dict)

    def add_idea(self, text: str, *, by: str) -> dict[str, str]:
        """登记一个待验证方向。返回该 Idea 的完整记录。"""
        body = text.strip()
        if not body:
            raise ValueError("Idea 正文不能为空")
        # 用正文前 60 字做 id：人可读，且同一句话不会重复登记。
        idea_id = body[:60]
        if idea_id in self.ideas:
            return self.ideas[idea_id]
        self.ideas[idea_id] = {
            "text": body[:400],
            "status": "open",
            "by": by,
            "why": "",
            "evidence": "",
        }
        return self.ideas[idea_id]

    def resolve_idea(
        self, idea_id: str, status: str, *, by: str, why: str = "", evidence: str = ""
    ) -> dict[str, str]:
        """更新一个方向的状态。

        `status="dropped"` 时**必须**给 `why` —— 这是本类唯一一条硬约束。
        理由：没有原因的废弃等于没记，"我们试过了不行"是最容易失真的记忆
        （下次没人知道到底试了什么）。`done` 同理要求 `evidence`。
        """
        rec = self.ideas.get(idea_id)
        if rec is None:
            # 允许用前缀模糊匹配，但必须唯一 —— 模型常常只记得前半句。
            hits = [k for k in self.ideas if k.startswith(idea_id)]
            if len(hits) != 1:
                raise ValueError(f"没有匹配到唯一的 Idea：`{idea_id}`（命中 {len(hits)} 条）")
            idea_id = hits[0]
            rec = self.ideas[idea_id]
        if status not in IDEA_STATUS:
            raise ValueError(f"未知的 Idea 状态 `{status}`，只能是 {sorted(IDEA_STATUS)}")
        if status == "dropped" and not why.strip():
            raise ValueError("标记 dropped 必须给 `why`（试了什么、为什么走不通）")
        if status == "done" and not evidence.strip():
            raise ValueError("标记 done 必须给 `evidence`（凭什么说做完了）")
        rec["status"] = status
        rec["why"] = why.strip()
        rec["evidence"] = evidence.strip()
        rec["by"] = f"{rec.get('by', '?')} → {by}"
        return rec

    def add_fact(self, text: str, *, by: str, evidence: str = "", kind: str = "positive") -> None:
        """记一条已确认事实。同文本覆盖（后者带新证据）。"""
        body = text.strip()
        if not body:
            raise ValueError("事实正文不能为空")
        if kind not in FACT_KINDS:
            raise ValueError(f"未知的事实类型 `{kind}`，只能是 {sorted(FACT_KINDS)}")
        self.facts[body[:300]] = {
            "evidence": evidence.strip(),
            "by": by,
            "kind": kind,
        }

    def render_ideas(self) -> str:
        """只列**还活着**的方向（open / doing）。

        已完成与已废弃的不进来 —— 它们的存在价值已经转移到 facts 里，
        再占 root 的上下文只会稀释它。这是 Idea/Memory 二分的真正收益。
        """
        live = {
            k: v for k, v in self.ideas.items() if v.get("status") in ("open", "doing")
        }
        if not live:
            return "（没有待推进的方向）"
        lines = []
        for k, v in live.items():
            lines.append(f"- [{v.get('status')}] {v.get('text', k)}（{v.get('by', '?')}）")
        return "\n".join(lines)

    def render_facts(self) -> str:
        if not self.facts:
            return "（还没有确认的事实）"
        labels = {"positive": "成立", "negative": "已排除", "constraint": "约束"}
        lines = []
        for text, info in self.facts.items():
            tag = labels.get(info.get("kind", "positive"), "?")
            ev = info.get("evidence") or ""
            suffix = f"｜证据：{ev[:120]}" if ev else ""
            lines.append(f"- [{tag}] {text}{suffix}")
        return "\n".join(lines)

    def write_note(self, title: str, body: str, *, by: str) -> None:
        self.notes[title.strip()[:120]] = f"{body.strip()}\n\n— by {by}"

    def record_coverage(self, surface: str, risk: str, outcome: str, *, by: str) -> None:
        key = surface.strip()[:200]
        prev = self.coverage.get(key)
        # 后写的覆盖前写的，但把"谁先测过"记在 outcome 里 —— 一个 surface
        # 被两个 agent 都测过，第二个的结论更可信（它可能带着第一个的笔记）。
        self.coverage[key] = {"risk": risk.strip(), "outcome": outcome.strip(), "by": by}
        if prev and prev.get("by") != by:
            self.coverage[key]["by"] = f"{prev.get('by')} → {by}"

    def render_notes(self) -> str:
        if not self.notes:
            return "（还没有任何笔记）"
        lines: list[str] = []
        for title, body in self.notes.items():
            lines.append(f"### {title}\n{body}")
        return "\n\n".join(lines)

    def render_coverage(self) -> str:
        if not self.coverage:
            return "（还没有任何覆盖记录）"
        lines: list[str] = []
        for surface, info in self.coverage.items():
            lines.append(
                f"- {surface} ｜风险：{info.get('risk', '?')}｜结论：{info.get('outcome', '?')}"
                f"（{info.get('by', '?')}）"
            )
        return "\n".join(lines)


@dataclass
class AgentCoordinator:
    """整场多智能体的图、状态、共享状态。**内存态，不碰 DB。**"""

    shared: SharedState = field(default_factory=SharedState)
    nodes: dict[str, AgentNode] = field(default_factory=dict)
    #: 本场总共已经花掉的 LLM 轮次（所有 agent 累计），受 `total_budget` 约束。
    turns_used: int = 0
    total_budget: int = 80
    #: 每个子 agent 的单场上限。
    child_budget: int = 20
    #: 整场编排是否被外部要求停止（用户终止 / 超时）。
    stop_requested: bool = False
    #: 私有待办清单（root 用它拆步骤）。key=id, value={text, status}。
    #: status: pending / doing / done。**不复用 coverage** —— 那个记"测过什么"，
    #: 这个记"打算做什么"，混淆会让两者都失真。
    todos: dict[str, dict[str, str]] = field(default_factory=dict)
    #: ★ v0.73：活发现收集器。与 `SolveSpec.finding_sink` 是**同一个 list 对象**
    #: （由 `run_multi_agent` 从 spec 上取来挂这里）。子智能体 `agent_finish` 的
    #: 结构化发现直接登记进来，超时取消时由 `_timeout_outcome` 收割。
    #: 为 None = 未开启（CTF 解题路径），所有登记点都是 no-op。
    finding_sink: list[Any] | None = None
    #: ★ v0.76：最近的动作类型滑窗，**按 agent 分开记**。
    #:
    #: 记的是"这一轮到底碰没碰目标"。`run_shell` 里可能是 `curl`（碰了目标），
    #: 也可能是 `grep 本地 bundle`（没碰）—— 分工在 `_looks_like_acting` 里。
    #:
    #: ⚠️ **必须按 agent 分开。** 第一版是 coordinator 上的一条全局列表，
    #: 2026-10-23 的 P2 重跑当场抓出问题：门只拦 root，却读全局比例；
    #: 子 agent a1 把 98 个动作里的 68 个花在 `cd /work/assets && grep/python`
    #: （该 agent 只有 19% 在打目标），把 root 自己的 47% 稀释走，
    #: 于是门在 root 明明打转时仍判"没打转"——**判定对象与判定依据必须是
    #: 同一个 agent**，否则这个信号没有意义。
    recent_actions: dict[str, list[bool]] = field(default_factory=dict)

    def note_action(self, *, acted: bool, agent_id: str = "root") -> None:
        """登记一条动作（`acted` = 这一轮是否真的向目标发了请求）。"""
        buf = self.recent_actions.setdefault(agent_id, [])
        buf.append(bool(acted))
        if len(buf) > ACTION_WINDOW:
            del buf[: len(buf) - ACTION_WINDOW]

    def acting_ratio(self, agent_id: str = "root") -> float | None:
        """该 agent 最近滑窗里"真打目标"的比例。样本不足时返回 None（不参与判定）。"""
        buf = self.recent_actions.get(agent_id) or []
        if len(buf) < ACTION_WINDOW_MIN_SAMPLES:
            return None
        return sum(1 for x in buf if x) / len(buf)

    _todo_seq: int = 0
    _id_seq: int = 0

    def new_agent_id(self) -> str:
        """生成本场唯一的短 id（`a1`、`a2`…）。短是刻意的 —— 它会出现在
        提示词与工具返回里，长 id 纯粹浪费 token。"""
        self._id_seq += 1
        return f"a{self._id_seq}"

    def add_root(self, name: str = "root") -> AgentNode:
        node = AgentNode(agent_id="root", name=name, task="", parent_id=None)
        self.nodes["root"] = node
        return node

    def add_child(self, *, name: str, task: str, parent_id: str) -> AgentNode:
        node = AgentNode(
            agent_id=self.new_agent_id(),
            name=name,
            task=task,
            parent_id=parent_id,
        )
        self.nodes[node.agent_id] = node
        return node

    def children_of(self, agent_id: str) -> list[AgentNode]:
        return [n for n in self.nodes.values() if n.parent_id == agent_id]

    def running_children(self, agent_id: str) -> list[AgentNode]:
        return [n for n in self.children_of(agent_id) if n.status == STATUS_RUNNING]

    def remaining_budget(self) -> int:
        return max(0, self.total_budget - self.turns_used)

    def can_spawn(self, parent_id: str) -> bool:
        return (
            len(self.children_of(parent_id)) < MAX_CHILDREN
            and self.remaining_budget() > 0
            and not self.stop_requested
        )

    # ── 私有待办清单 ──────────────────────────────────────
    def add_todo(self, text: str) -> dict[str, str]:
        text = text.strip()
        self._todo_seq += 1
        todo_id = f"t{self._todo_seq}"
        self.todos[todo_id] = {"text": text, "status": "pending"}
        return {"id": todo_id, **self.todos[todo_id]}

    def render_todos(self) -> str:
        if not self.todos:
            return "（还没有待办。用 `create_todo` 把大目标拆成可跟踪的步骤。）"
        mark = {"pending": "[ ]", "doing": "[~]", "done": "[x]"}
        return "\n".join(
            f"{mark.get(v['status'], '[?]')} {tid} {v['text']}"
            for tid, v in self.todos.items()
        )

    def render_graph(self, *, me: str) -> str:
        """把图渲染成缩进列表，`← you` 标出调用者。"""
        lines: list[str] = []

        def render(aid: str, depth: int) -> None:
            node = self.nodes.get(aid)
            if node is None:
                return
            marker = "  ← you" if aid == me else ""
            lines.append(f"{'  ' * depth}- {node.name} ({aid}) [{node.status}]{marker}")
            for child in self.children_of(aid):
                render(child.agent_id, depth + 1)

        roots = [n for n in self.nodes.values() if n.parent_id is None]
        for root in roots:
            render(root.agent_id, 0)
        counts: dict[str, int] = {STATUS_RUNNING: 0, STATUS_COMPLETED: 0, STATUS_FAILED: 0, STATUS_STOPPED: 0}
        for node in self.nodes.values():
            counts[node.status] = counts.get(node.status, 0) + 1
        summary = " ".join(f"{k}={v}" for k, v in counts.items() if v)
        return "\n".join(lines) + f"\n（{summary}；总预算 {self.turns_used}/{self.total_budget} 轮）"


# ══════════════════════════════════════════════════════════
# 编排工具的调用（root / child 各自的工具集）
# ══════════════════════════════════════════════════════════


def _json_ok(**kwargs: Any) -> str:
    return json.dumps({"success": True, **kwargs}, ensure_ascii=False)


def _json_err(error: str, **kwargs: Any) -> str:
    return json.dumps({"success": False, "error": error, **kwargs}, ensure_ascii=False)


async def dispatch_orchestration_tool(
    name: str,
    args: dict[str, Any],
    *,
    coord: AgentCoordinator,
    me: AgentNode,
    is_root: bool,
    spawner: Callable[..., Any] | None,
    sandbox: SandboxContainer,
    http: Any,
    spec: SolveSpec,
) -> str:
    """编排工具分发。**未知/越权工具返回一句话，不抛。**

    ⚠️ 与 `dispatch_tool` 同一纪律：不抛。模型用错工具名不该终止整场编排。
    """
    # 越权拦截：root 调 agent_finish、子 agent 调 finish_scan 都算错用。
    if name in _ROOT_ONLY and not is_root:
        return _json_err(f"`{name}` 只有 root 主智能体能调用；子智能体请用 `agent_finish` 结束。")
    if name in _CHILD_ONLY and is_root:
        return _json_err(f"`{name}` 只有子智能体能调用；root 主智能体请用 `finish_scan` 结束。")

    if name == CREATE_AGENT:
        return await _do_create_agent(args, coord=coord, me=me, spawner=spawner)
    if name == SEND_MESSAGE:
        return _do_send_message(args, coord=coord, me=me)
    if name == WAIT_FOR_AGENTS:
        return await _do_wait_for_agents(args, coord=coord, me=me)
    if name == VIEW_GRAPH:
        return _json_ok(graph_structure=coord.render_graph(me=me.agent_id))
    if name == NOTE_WRITE:
        return _do_write_note(args, coord=coord, me=me)
    if name == NOTE_LIST:
        return _json_ok(notes=coord.shared.render_notes())
    if name == COVERAGE_RECORD:
        return _do_record_coverage(args, coord=coord, me=me)
    if name == COVERAGE_LIST:
        return _json_ok(coverage=coord.shared.render_coverage())
    if name == AGENT_FINISH:
        return _do_agent_finish(args, coord=coord, me=me)
    if name == STOP_AGENT:
        return _do_stop_agent(args, coord=coord, me=me)
    if name == THINK:
        return _do_think(args, me=me)
    if name == TODO_CREATE:
        return _do_create_todo(args, coord=coord)
    if name == TODO_LIST:
        return _json_ok(todos=coord.render_todos())
    if name == TODO_UPDATE:
        return _do_update_todo(args, coord=coord)
    if name == THREAT_GET:
        return _json_ok(threat_model=coord.shared.render_threat_model())
    if name == IDEA_ADD:
        return _do_add_idea(args, coord=coord, me=me)
    if name == IDEA_RESOLVE:
        return _do_resolve_idea(args, coord=coord, me=me)
    if name == MEMORY_ADD:
        return _do_add_fact(args, coord=coord, me=me)
    if name == MEMORY_GET:
        return _do_get_memory(args, coord=coord)
    if name == THREAT_AMEND:
        return _do_amend_threat_model(args, coord=coord, me=me)
    if name == FINISH_SCAN:
        # finish_scan 的实际"终止"由循环在解析到 FINAL 时处理；这里只回一句确认，
        # 让模型知道它该用 FINAL 交结论（我们的文本协议里，"结束"就是写 FINAL）。
        return _json_ok(
            note="好，请用 `FINAL: {...}` 给出总结论（summary / tried / ruled_out / findings），"
            "然后本次编排结束。"
        )
    return _json_err(
        f"未知编排工具 `{name}`。可用：{', '.join(sorted(set(_ROOT_ONLY | _CHILD_ONLY | _SHARED)))}。"
    )


async def _do_create_agent(
    args: dict[str, Any],
    *,
    coord: AgentCoordinator,
    me: AgentNode,
    spawner: Callable[..., Any] | None,
) -> str:
    name = str(args.get("name") or "").strip()
    task = str(args.get("task") or "").strip()
    if not name:
        return _json_err("需要 `name`（给子智能体起个能看出职责的名字，如「SQLi 验证」）。")
    if not task:
        return _json_err("需要 `task`（具体要它做什么，越具体越好）。")
    if not coord.can_spawn(me.agent_id):
        if coord.remaining_budget() <= 0:
            return _json_err("总轮次预算已用尽，不能再开子智能体。请收口。")
        return _json_err(
            f"子智能体数量已达上限（{MAX_CHILDREN} 个）。请先等现有子智能体回报，"
            "或用 `send_message_to_agent` 给它们补充指令。"
        )
    if spawner is None:
        return _json_err("运行器没有提供子智能体启动器（spawner）。")
    try:
        child = await spawner(name=name, task=task, parent_id=me.agent_id)
    except Exception as exc:  # noqa: BLE001 - spawn 失败要变成模型能读的一句话
        logger.exception("create_agent: 启动子智能体失败")
        return _json_err(f"子智能体启动失败：{exc}")
    return _json_ok(
        agent_id=child.agent_id,
        name=child.name,
        note="子智能体已在后台并行运行。你可以继续自己的分析，"
        "稍后用 `wait_for_agents` 收它的报告，或用 `view_agent_graph` 看进度。"
        "**不要在它跑完前重复它的工作。**",
    )


def _do_send_message(args: dict[str, Any], *, coord: AgentCoordinator, me: AgentNode) -> str:
    target = str(args.get("target_agent_id") or "").strip()
    if not target:
        return _json_err("需要 `target_agent_id`。")
    if target == me.agent_id:
        return _json_err("不能给自己发消息；用 `write_note` 记私人笔记。")
    node = coord.nodes.get(target)
    if node is None:
        return _json_err(f"找不到子智能体 `{target}`。")
    if node.status != STATUS_RUNNING:
        return _json_err(
            f"`{target}` 已经是 {node.status}，收不到消息。"
            "（我们的子智能体跑完就结束，不能唤醒。要它继续做，开一个新的。）"
        )
    return _json_err(
        "当前编排是一次性协程模型：子智能体之间不通过消息互相打断（那样会引入死锁）。"
        "请用共享笔记 `write_note` 传递中间结论，或用 `wait_for_agents` 等它跑完读报告。"
    )


async def _do_wait_for_agents(args: dict[str, Any], *, coord: AgentCoordinator, me: AgentNode) -> str:
    """等自己所有仍在跑的子 agent 结束，汇总它们的完成报告。"""
    running = coord.running_children(me.agent_id)
    if not running:
        # 已经没有在跑的子 agent —— 把已结束的报告汇总给它（避免它空等）。
        done = [n for n in coord.children_of(me.agent_id) if n.completion_report]
        if not done:
            return _json_ok(
                wait_outcome="no_active_agents",
                note="没有在跑的子智能体，也没有已完成的报告。继续自己的工作或收口。",
            )
        return _json_ok(
            wait_outcome="all_done",
            reports=[{"name": n.name, "agent_id": n.agent_id, "report": n.completion_report} for n in done],
            note="所有子智能体都已结束，报告见上。",
        )
    # 真的等：asyncio 里每个子 agent 的任务在 `_spawn` 里被登记到 coord，
    # 这里 await 它们的 task 直到 gather 完成。用超时防止无限等。
    timeout = float(args.get("timeout_seconds") or 600)
    tasks = [n for n in running]
    # 每个 node 上的 future 由 spawner 放在 node.completion_report 里，等它非空。
    deadline = asyncio.get_running_loop().time() + min(timeout, 1800)
    while any(n.status == STATUS_RUNNING for n in tasks):
        if asyncio.get_running_loop().time() > deadline:
            break
        if coord.stop_requested:
            break
        await asyncio.sleep(0.5)
    reports = [
        {"name": n.name, "agent_id": n.agent_id, "status": n.status, "report": n.completion_report}
        for n in coord.children_of(me.agent_id)
        if n.completion_report
    ]
    return _json_ok(
        wait_outcome="all_done" if reports else "timeout",
        reports=reports,
        note=(
            "子智能体已完成，报告见上。请据此决定下一步（继续深挖 / 开新子 agent / 收口）。"
            if reports
            else "等待超时，子智能体仍在跑。你可以继续自己的工作，稍后再等一次。"
        ),
    )


def _do_write_note(args: dict[str, Any], *, coord: AgentCoordinator, me: AgentNode) -> str:
    title = str(args.get("title") or "").strip()
    body = str(args.get("body") or "").strip()
    if not title or not body:
        return _json_err("需要 `title` 与 `body`。")
    coord.shared.write_note(title, body, by=me.name)
    return _json_ok(note=f"已记录笔记「{title}」。其他智能体用 `list_notes` 能看到它。")


def _do_record_coverage(args: dict[str, Any], *, coord: AgentCoordinator, me: AgentNode) -> str:
    surface = str(args.get("surface") or "").strip()
    risk = str(args.get("risk") or "").strip()
    outcome = str(args.get("outcome") or "").strip()
    if not surface:
        return _json_err("需要 `surface`（测了什么：URL / 参数 / 端点）。")
    coord.shared.record_coverage(surface, risk or "未标", outcome or "未标", by=me.name)
    return _json_ok(note=f"已记录覆盖：{surface}。含「干净」的结果也要记 —— 那是别人不再重测的依据。")


def _do_agent_finish(args: dict[str, Any], *, coord: AgentCoordinator, me: AgentNode) -> str:
    """子 agent 收尾：把完成报告写进 node，由 spawner 的 task 结束时读走。

    ## ★ v0.73：`findings` 同时登记到活收集器

    `agent_finish` 是子智能体**显式结构化交出来**的发现 —— 它比 `FINAL` 更早、
    更可靠（子智能体干完自己那一块就交，而 `FINAL` 要等 root 收口，root 常常
    等不到）。旧版只把它拼成一段纯文本塞进共享笔记，于是：
    - 扫描正常结束时，走 `_render_completion_text` → root 的 FINAL → 能收到；
    - 扫描被超时取消时，这段文本还在笔记里，但**没有任何代码把它变成 findings**。

    所以这里补一次登记。只登记 `isinstance(findings, list)` 的元素，
    且只挑**看起来是结构化发现**的（dict/字符串都收 —— `agent_finish` 的
    schema 本来就允许字符串数组，那种情况按标题包一层）。
    """
    summary = str(args.get("result_summary") or "").strip()
    findings = args.get("findings") if isinstance(args.get("findings"), list) else []
    open_items = args.get("open_items") if isinstance(args.get("open_items"), list) else []

    # ★ v0.73：这一批是模型自己用 `agent_finish(findings=[...])` 明确交出来的
    # 结构化发现，必须进活收集器 —— 否则超时取消时它们只存在于笔记文本里。
    _sink_agent_findings(coord, findings, by=me.name)

    coord.shared.write_note(
        f"完成报告 · {me.name}",
        "\n".join(
            [summary or "(无摘要)"]
            + (["", "发现："] + [f"- {f}" for f in findings] if findings else [])
            + (["", "未决："] + [f"- {o}" for o in open_items] if open_items else [])
        ),
        by=me.name,
    )
    return _json_ok(
        note="完成报告已记录。请用 `FINAL: {...}` 给出你的结论（子智能体的 findings 会在 FINAL 里被父级收集）。"
    )


def _sink_agent_findings(
    coord: AgentCoordinator, findings: list[Any], *, by: str
) -> None:
    """把 `agent_finish(findings=[...])` 里的条目登记进 `finding_sink`。

    条目可能是 dict（`{"title":..., "severity":...}`）也可能是纯字符串
    （`"GET /api/v1/users 越权"`）。字符串按 `{"title": s}` 包一层 —— 宁可少
    字段，也不能把一条真实发现丢了：丢掉的是"我们漏了"，包成 title 至少
    人能在报告里看到它。

    绝不猜：非 list 一律忽略（`_do_agent_finish` 已经保证是 list）。
    异常只 warning —— 收集器是观测附件，不该让子智能体收尾失败。
    """
    sink = getattr(coord, "finding_sink", None)
    if sink is None:
        return
    try:
        for item in findings:
            if isinstance(item, dict):
                sink.append(item)
            elif isinstance(item, str) and item.strip():
                sink.append({"title": item.strip(), "reported_by": by})
            # 其他类型（None/int/list）静默跳过：没有可用语义，包进去只是噪音。
    except Exception:  # noqa: BLE001
        logger.warning("agent_finish：登记发现到活收集器失败（不影响收尾）", exc_info=True)


def _do_stop_agent(args: dict[str, Any], *, coord: AgentCoordinator, me: AgentNode) -> str:
    """root 主动叫停一个子 agent。

    实现是**协作式**的：把一个 `stop=True` 标在节点上，子 agent 的循环每轮
    开头检查（`node.stop_requested`），看到就收尾。**不直接 cancel 那个 task** ——
    cancel 会让它来不及把已拿到的中间结论写成笔记/覆盖记录，那些是后面收口
    要用的。让它在下一轮自己"体面退出"，比强杀有价值。
    """
    target = str(args.get("target_agent_id") or "").strip()
    reason = str(args.get("reason") or "").strip()
    if not target:
        return _json_err("需要 `target_agent_id`（要叫停哪个子智能体，用 view_agent_graph 看 id）。")
    node = coord.nodes.get(target)
    if node is None:
        return _json_err(f"找不到子智能体 `{target}`。")
    if node.agent_id == me.agent_id:
        return _json_err("不能叫停自己；你自己用 FINAL 收尾。")
    if not node.is_terminal():
        node.stop_requested = True
        if reason:
            # 把原因写进共享笔记，让被停的子 agent 与复盘的人都能看到"为什么停"。
            coord.shared.write_note(
                f"叫停 · {node.name}",
                reason,
                by=me.name,
            )
    return _json_ok(
        agent_id=target,
        note=(
            f"已请求 `{node.name}`({target}) 在**下一轮开始时**收尾退出。"
            "它会把自己已拿到的东西写成笔记再结束。"
            "你可以继续推进主线，之后用 `wait_for_agents` 收它留下的报告。"
        ),
    )


def _do_add_idea(args: dict[str, Any], *, coord: AgentCoordinator, me: AgentNode) -> str:
    """登记一个待推进方向。"""
    try:
        rec = coord.shared.add_idea(str(args.get("text") or args.get("idea") or ""), by=me.name)
    except ValueError as exc:
        return _json_err(str(exc))
    return _json_ok(
        **rec,
        note="已登记。做完或走不通时用 `resolve_idea` 收掉它 —— 走不通必须给 why。",
    )


def _do_resolve_idea(args: dict[str, Any], *, coord: AgentCoordinator, me: AgentNode) -> str:
    """把一个方向收成 done / dropped。"""
    try:
        rec = coord.shared.resolve_idea(
            str(args.get("idea_id") or args.get("idea") or ""),
            str(args.get("status") or ""),
            by=me.name,
            why=str(args.get("why") or ""),
            evidence=str(args.get("evidence") or ""),
        )
    except ValueError as exc:
        return _json_err(str(exc))
    return _json_ok(**rec, note="已更新。剩下的待推进方向：" + coord.shared.render_ideas())


def _do_add_fact(args: dict[str, Any], *, coord: AgentCoordinator, me: AgentNode) -> str:
    """记一条已确认事实（含"已排除"这种阴性事实）。"""
    try:
        coord.shared.add_fact(
            str(args.get("text") or args.get("fact") or ""),
            by=me.name,
            evidence=str(args.get("evidence") or ""),
            kind=str(args.get("kind") or "positive"),
        )
    except ValueError as exc:
        return _json_err(str(exc))
    return _json_ok(note="已记入共享事实。")


def _do_get_memory(args: dict[str, Any], *, coord: AgentCoordinator) -> str:
    """一次读回"往哪走"与"已确认什么"两栏（分开呈现，不混在一起）。"""
    return _json_ok(
        ideas=coord.shared.render_ideas(),
        facts=coord.shared.render_facts(),
    )


def _do_think(args: dict[str, Any], *, me: AgentNode) -> str:
    """给模型一个"只推理不动手"的出口。

    strix 的 think 工具 = 让模型把当前判断写下来、进上下文、然后继续。我们
    的协议里**纯文本回合**被视为空转（要求几乎每轮一次工具调用），但"动手前
    先把话说清"恰恰能减少瞎试。这个工具让推理**成为一次合法行动** ——
    模型写的话会原样回到下一轮上下文里（经由 tool 结果）。
    """
    thought = str(args.get("thought") or args.get("text") or "").strip()
    if not thought:
        return _json_err("需要 `thought`（你想理清什么：当前证据、下一步假设、为什么选它）。")
    # 把推理本身作为工具结果返回 —— 于是它进了 transcript，下一轮能读到。
    return _json_ok(thought=thought[:2000], note="已记录。基于这段推理继续行动。")


def _do_create_todo(args: dict[str, Any], *, coord: AgentCoordinator) -> str:
    text = str(args.get("text") or args.get("task") or "").strip()
    if not text:
        return _json_err("需要 `text`（要做的这一步，写成一句可判完成的话）。")
    item = coord.add_todo(text)
    return _json_ok(**item, note=f"已加入待办。当前清单：\n{coord.render_todos()}")


def _do_update_todo(args: dict[str, Any], *, coord: AgentCoordinator) -> str:
    todo_id = str(args.get("id") or "").strip()
    status = str(args.get("status") or "").strip().lower()
    if not todo_id:
        return _json_err("需要 `id`（用 `list_todos` 看有哪些）。")
    if todo_id not in coord.todos:
        return _json_err(f"找不到待办 `{todo_id}`。")
    if status:
        if status not in ("pending", "doing", "done"):
            return _json_err("`status` 只能是 pending / doing / done。")
        coord.todos[todo_id]["status"] = status
    return _json_ok(
        id=todo_id,
        status=coord.todos[todo_id]["status"],
        note=f"已更新。当前清单：\n{coord.render_todos()}",
    )


def _do_amend_threat_model(
    args: dict[str, Any], *, coord: AgentCoordinator, me: AgentNode
) -> str:
    """往共享威胁模型的某一栏追加条目。

    四栏：`assets`（有哪些资产）/ `trust_boundaries`（信任边界在哪）/
    `hypotheses`（可能的攻击假设）/ `priorities`（先打哪、为什么）。
    接受 `field` + `items`（列表）或 `item`（单条）。
    """
    field_name = str(args.get("field") or "").strip()
    if not field_name:
        return _json_err(
            "需要 `field`：assets / trust_boundaries / hypotheses / priorities 之一。"
        )
    raw_items = args.get("items")
    if isinstance(raw_items, str):
        items = [raw_items]
    elif isinstance(raw_items, list):
        items = [str(x) for x in raw_items]
    elif args.get("item"):
        items = [str(args["item"])]
    else:
        return _json_err("需要 `items`（要补充的条目，列表）或 `item`（单条）。")
    try:
        bucket = coord.shared.amend_threat_model(field_name, items, by=me.name)
    except ValueError as exc:
        return _json_err(str(exc))
    return _json_ok(
        field=field_name,
        items=bucket,
        note=f"已更新威胁模型。全量：\n{coord.shared.render_threat_model()}",
    )


# ══════════════════════════════════════════════════════════
# 单条 agent 循环（root 与 child 共用）
# ══════════════════════════════════════════════════════════


@dataclass
class AgentLoopResult:
    """一条 agent 循环跑完的结果。"""

    outcome: SolveOutcome
    finished: bool  # 是否主动用 FINAL 收了尾（vs 轮次用尽/停止）
    final_payload: dict[str, Any] | None = None


async def run_agent_loop(
    *,
    node: AgentNode,
    coord: AgentCoordinator,
    spec: SolveSpec,
    sandbox: SandboxContainer,
    gateway: LlmGateway,
    system_prompt: str,
    base_message: dict[str, str],
    is_root: bool,
    max_turns: int,
    on_progress: Callable[[str, str, dict[str, str]], None] | None,
    spawner: Callable[..., Any] | None,
    http: Any,
    hint_provider: Callable[[], str | None] | None = None,
) -> AgentLoopResult:
    """跑一条独立的 ReAct 循环，直到 FINAL / 轮次用尽 / 外部停止。

    这是 `solve_in_sandbox` 单循环逻辑的**多 agent 版**：同样的文本协议
    （`ACTION:` / `FINAL:`）、同样的消息分段（base/recent）、同样的重复检测，
    区别只有两点：
      1. 工具集按 `is_root` 分（root 有编排工具，child 有 agent_finish）；
      2. 轮次预算是**从总量里扣**（`coord.turns_used`），不是各自的固定数。
    """
    from app.kernel.runner import MAX_ACTIONS_PER_TURN, MAX_HISTORY_TURNS

    log: list[str] = []
    recent: list[dict[str, str]] = []

    def _messages() -> list[dict[str, str]]:
        return [base_message, *recent]

    def notify(kind: str, summary: str, payload: dict[str, str]) -> None:
        if on_progress is None:
            return
        # ★ 每条过程事件都打上"谁做的" —— 否则多智能体跑起来后，事件流里
        #   root 与各子 agent 的 tool_call/reasoning 混成一条河，复盘时无法
        #   分辨这条命令是 root 下的还是哪个子 agent 下的。`log` 里本来就带
        #   `[{node.agent_id} tN]`，这里把同一份归属也放进结构化 payload，
        #   导入器据此按 agent 分名册（见 `app/scans/importer.py`）。
        enriched = dict(payload)
        enriched["agent"] = node.agent_id
        enriched["agentName"] = node.name
        enriched["turn"] = str(turn)
        try:
            on_progress(kind, summary, enriched)
        except Exception:  # noqa: BLE001 - 过程不该影响结果
            logger.warning("多智能体过程事件发送失败（不影响编排）", exc_info=True)

    outcome = SolveOutcome()
    seen_calls: dict[tuple[str, str, str], int] = {}
    #: ★ v0.77：同一个"打点形状"被撞了几次（见 `target_shape`）。
    shape_hits: dict[str, int] = {}

    for turn in range(1, max_turns + 1):
        if coord.stop_requested:
            outcome.error = "编排被外部要求停止。"
            break
        if node.stop_requested:
            # root 用 stop_agent 叫停了我 —— 体面收尾，不是崩。
            outcome.error = "已被主智能体叫停（stop_agent），提前收尾。"
            log.append(f"[{node.agent_id} t{turn}] 收到 stop_agent 叫停，退出循环")
            break
        if coord.remaining_budget() <= 0:
            outcome.error = f"总轮次预算已用尽（{coord.total_budget} 轮）。"
            break
        outcome.turns = turn
        coord.turns_used += 1

        # 收尾提醒（只剩最后一轮时）
        if max_turns - turn == 0:
            recent.append({"role": "user", "content": _final_stretch_note(0)})

        # 人给的线索（只在 root 上生效 —— 子 agent 有明确 task，不掺人类线索）
        if is_root and hint_provider is not None:
            try:
                hint = hint_provider()
            except Exception:  # noqa: BLE001
                logger.warning("读取人给线索失败", exc_info=True)
                hint = None
            if hint:
                recent.append({"role": "user", "content": _human_hint_note(hint)})
                log.append(f"[{node.agent_id} t{turn}] 已注入人给线索：{hint[:80]}")
                # ★ v0.69：发一条过程事件 —— 单循环路径（runner.py:4214）早就发了，
                #   但多智能体这里以前**只塞 recent、不发事件**：人给了线索，活动流
                #   里却看不到"已接收"，只能干等、怀疑输入框没生效（2026-10-22 真跑
                #   暴露：用户发了「扫描端口…把 8080 关了」，约 87 秒后 root 确实把
                #   它并入推理了，但界面上没有任何回执）。
                #   注意口径：线索是**方向性建议**，不覆盖只读/写操作纪律 —— root 有权
                #   把它判为"不该执行"（那次它就正确拒绝了"关服务"这个破坏性动作）。
                #   所以文案不写"已执行"，只写"已并入推理"。
                notify(
                    "reasoning",
                    f"已把人工线索并入本轮推理：{hint[:60]}",
                    {"text": hint[:200], "hint": "1"},
                )

        try:
            reply = await gateway.chat(_messages(), system=system_prompt)
        except KernelError as exc:
            outcome.error = exc.message
            log.append(f"[{node.agent_id} t{turn}] LLM 失败：{exc.message}")
            break

        outcome.llm_calls += 1
        outcome.usages.append((turn, gateway.model, gateway.label, reply.usage))
        text = reply.text or ""
        log.append(f"[{node.agent_id} t{turn}] {text[:2000]}")
        recent.append({"role": "assistant", "content": text})
        truncated = reply.finish_reason in ("length", "max_tokens")

        # ① 结论？
        final = parse_final(text)
        if final is not None:
            # ★ v0.75：结束条件外置（学 BreachWeave）。
            #
            # root 说"我扫完了"不算数 —— 由编排层按可数的覆盖率/结论条数
            # 判定。这里**只拦 root**：子 agent 的 FINAL 是它的交差，
            # 拦它会让 root 永远等不到回报（那正是 v0.73 修的那个死法）。
            if is_root:
                # `spec.min_coverage = 0` = 显式关掉这道门（测试用，见字段注释）。
                _min_cov = getattr(spec, "min_coverage", None)
                if _min_cov != 0:
                    # ⚠️ 先有鸡还是先有蛋：`finding_sink` 只在 FINAL **被放行之后**
                    #    才登记，所以直接拿 sink 长度当结论条数，会永远判 0 条、
                    #    永远拒绝收口 —— 实测连拒 5 轮直到轮次用尽（这一步
                    #    `test_root_final_passes_after_recording_enough_coverage`
                    #    第一次跑就抓到了）。
                    #    正确口径：**这次 FINAL 里交上来的发现也算**。所以取
                    #    "已登记的 + 本次这批"的并集大小。
                    _batch = final.get("findings")
                    _batch_n = len([x for x in _batch if x]) if isinstance(_batch, list) else 0
                    gate = termination.decide(
                        termination.Observation(
                            coverage_count=len(coord.shared.coverage),
                            finding_count=len(coord.finding_sink or []) + _batch_n,
                            turns_used=coord.turns_used,
                            total_budget=coord.total_budget,
                            model_claims_done=True,
                            acting_ratio=coord.acting_ratio(node.agent_id),
                        ),
                        min_coverage=(
                            termination.MIN_COVERAGE if _min_cov is None else int(_min_cov)
                        ),
                    )
                    if not gate.allow:
                        # 不结束，把话塞回上下文继续跑。**不发 FINAL 事件** ——
                        # 它没有发生；发一条 reasoning 让界面能看见"被退回了"。
                        recent.append({"role": "user", "content": gate.nudge})
                        log.append(f"[{node.agent_id} t{turn}] 收口被拒：{gate.reason}")
                        notify("reasoning", f"还不能收口：{gate.reason}", {"text": gate.reason})
                        continue

            outcome.candidates, rejected = extract_candidates_with_reasons(final)
            outcome.summary, outcome.tried, outcome.ruled_out = extract_final_analysis(final)
            outcome.workorders = final.get("workorders")
            outcome.findings = final.get("findings")
            # ★ v0.73：同时登记到活收集器 —— 见 `SolveSpec.finding_sink`。
            # 子 agent 是最常写出真实发现的那个角色，而它写完之后 root 往往
            # 还在等其他子 agent 回报；超时一旦发生在那个窗口里，这一批
            # 发现会随协程一起消失（实测：6 条已确认发现入库 0 条）。
            _sink_findings(spec, outcome.findings)
            outcome.transcript = "\n\n".join(log)
            if not outcome.candidates and rejected and is_root:
                outcome.error = (
                    "模型给出了结论，但候选 flag 被本地格式校验拒绝：" + "；".join(rejected[:3])
                )
            return AgentLoopResult(outcome=outcome, finished=True, final_payload=final)

        # ② 动作？
        actions = parse_actions(text)
        if not actions:
            if len(text.strip()) >= 20:
                notify("reasoning", text.strip()[:200], {"text": text.strip()})
            reminder = _truncation_reminder(len(text)) if truncated else _format_reminder()
            recent.append({"role": "user", "content": reminder})
            continue

        batch = actions[:MAX_ACTIONS_PER_TURN]
        dropped = len(actions) - len(batch)
        completed: list[tuple[str, str, str, str]] = []
        narration = _narration_before_action(text)
        if narration:
            notify("reasoning", narration[:200], {"text": narration})

        for tool_name, tool_args in batch:
            command = str(tool_args.get("command") or json.dumps(tool_args, ensure_ascii=False))
            log.append(f"[{node.agent_id} t{turn}] tool {tool_name} {json.dumps(tool_args, ensure_ascii=False)[:400]}")

            # ★ v0.76：登记这一轮有没有真的向目标发请求。门的"原地打转"
            #   判定靠这个滑窗（见 `_looks_like_acting` 的顺序说明）。
            coord.note_action(
                acted=_looks_like_acting(tool_name=tool_name, command=command),
                agent_id=node.agent_id,
            )

            # 编排工具 vs 基础工具，分派到不同入口。
            orchestr_tools = _ROOT_ONLY | _CHILD_ONLY | _SHARED
            if tool_name in orchestr_tools:
                result = await dispatch_orchestration_tool(
                    tool_name,
                    tool_args,
                    coord=coord,
                    me=node,
                    is_root=is_root,
                    spawner=spawner,
                    sandbox=sandbox,
                    http=http,
                    spec=spec,
                )
            else:
                result = await dispatch_tool(tool_name, tool_args, spec=spec, sandbox=sandbox, http=http)

            log.append(f"[{node.agent_id} t{turn}] result: {result[:1500]}")
            outcome.tool_outputs.append(result)
            outcome.tool_calls += 1

            call_key = (
                tool_name,
                json.dumps(tool_args, ensure_ascii=False, sort_keys=True),
                _output_fingerprint(result),
            )
            repeated_note = ""
            first_turn = seen_calls.get(call_key)
            if first_turn is not None:
                repeated_note = (
                    f"⚠️ 这条命令你在第 {first_turn} 轮跑过，输出完全一样 —— 换一条路。\n\n"
                )
            else:
                seen_calls[call_key] = turn

            # ★ v0.77：粗粒度"同一个点"判定（学腾讯队的「已排除路径」）。
            #
            # 上面那个 `seen_calls` 要求**命令 + 输出逐字节相同**，太窄：
            # P2 那轮 `/api/v1/scans` 被探 13 次、`/auth/login` 12 次，
            # 每次都换了参数，所以一次都没命中，模型在同一个点上耗尽预算。
            # 这里只判"打的是不是同一个路径"（数字段归一成 {id}），
            # 命中就明确告诉它"换参数不算新尝试"。
            shape = target_shape(tool_name=tool_name, command=command, args=tool_args)
            if shape:
                shape_hits[shape] = shape_hits.get(shape, 0) + 1
                if shape_hits[shape] == SHAPE_REPEAT_WARN_AT:
                    n = shape_hits[shape]
                    _tip = (
                        f"⚠️ 这是第 {n} 次打 `{shape}` 这个点，前面每一次都换了参数 —— "
                        "**换参数不算新尝试**。同一个点上再试也得不到新结论："
                        "换一个路径、换一个身份、或者换一层（换个协议/入口）再看。"
                    )
                    repeated_note += _tip + chr(10) + chr(10)


            completed.append((tool_name, command, result[:3000], repeated_note))
            notify(
                "tool_call",
                f"{tool_name} {command}"[:200],
                {"tool": tool_name, "command": command, "output": result},
            )

        recent.append({"role": "user", "content": _format_action_results(completed, dropped) + (
            "\n\n（提醒：你上一次回复被长度上限切断。）" if truncated else ""
        )})
        outcome.saw_tool_output = True
        if len(recent) > MAX_HISTORY_TURNS * 2:
            del recent[: len(recent) - MAX_HISTORY_TURNS * 2]

    outcome.transcript = "\n\n".join(log)
    if not outcome.error:
        outcome.error = f"回合用尽（{max_turns} 轮）仍未给出结论。"
    return AgentLoopResult(outcome=outcome, finished=False)


# ══════════════════════════════════════════════════════════
# 顶层入口：驱动 root + 子 agent
# ══════════════════════════════════════════════════════════


async def run_multi_agent(
    spec: SolveSpec,
    *,
    sandbox: SandboxContainer,
    gateway: LlmGateway,
    root_system_prompt: str,
    child_system_prompt: str,
    on_progress: Callable[[str, str, dict[str, str]], None] | None = None,
    hint_provider: Callable[[], str | None] | None = None,
) -> SolveOutcome:
    """完整多智能体编排：root 调度、子 agent 并行、结论汇总。

    ## 汇总语义（这是与单循环最大的不同）

    单循环只有一个 `outcome`。多智能体里 root 也可能直接产出候选（它自己也
    用基础工具），子 agent 也可能产出候选/发现。**汇总规则**：

      - **候选 flag**：root 的优先；root 没有则取第一个子 agent 有的。
        （一道题只有一个 flag，多个 agent 报不同候选时以 root 的收口为准。）
      - **findings / workorders**：**合并**所有 agent 的（去重靠标题）——
        渗透评估允许多条发现，合并才对。
      - **transcript**：拼接（每段带 agent 名），因为复盘要看全过程。

    `run_multi_agent` 是"root + N 个 child 各跑一条循环"，它的控制流与单循环
    **结构不同**（多协程、有 spawner、有汇总） —— 见 `runner.solve_in_sandbox`
    里分流的说明。
    """
    coord = AgentCoordinator(
        total_budget=max(12, int(spec.max_turns * TOTAL_TURN_RATIO)),
        child_budget=child_turn_budget(spec),
        # ★ v0.73：把 spec 上的活收集器**同一个对象**挂到协调器上 ——
        # 子智能体 `agent_finish` 登记的东西必须与 `_timeout_outcome` 收割的
        # 是同一个 list，否则超时路径依然拿不到子智能体交出来的发现。
        finding_sink=getattr(spec, "finding_sink", None),
    )
    root = coord.add_root()
    root_outcome = SolveOutcome()

    # 子 agent 的 task 句柄，用于 wait_for_agents 收敛。
    child_tasks: dict[str, asyncio.Task[AgentLoopResult]] = {}

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0), trust_env=False) as http:

        async def spawner(*, name: str, task: str, parent_id: str) -> AgentNode:
            """root 调 create_agent 时由 dispatch 调进来：建 node + 起协程。"""
            child = coord.add_child(name=name, task=task, parent_id=parent_id)
            child_base = {
                "role": "user",
                "content": _child_task_prompt(spec, child=child, coord=coord),
            }
            task_obj = asyncio.create_task(
                run_agent_loop(
                    node=child,
                    coord=coord,
                    spec=spec,
                    sandbox=sandbox,
                    gateway=gateway,
                    system_prompt=child_system_prompt,
                    base_message=child_base,
                    is_root=False,
                    max_turns=coord.child_budget,
                    on_progress=on_progress,
                    spawner=None,  # 子 agent 不能再开子 agent（单层，防爆）
                    http=http,
                    hint_provider=None,
                )
            )
            child_tasks[child.agent_id] = task_obj

            async def _finalize(cid: str = child.agent_id, t=task_obj) -> None:
                try:
                    res = await t
                except Exception as exc:  # noqa: BLE001
                    logger.exception("子智能体 %s 崩溃", cid)
                    node = coord.nodes.get(cid)
                    if node:
                        node.status = STATUS_FAILED
                        node.outcome.error = f"子智能体崩溃：{exc}"
                    return
                node = coord.nodes.get(cid)
                if node is None:
                    return
                node.outcome = res.outcome
                node.status = STATUS_COMPLETED if (res.finished or not res.outcome.error) else STATUS_FAILED
                node.completion_report = _render_completion_text(node, res)

            asyncio.create_task(_finalize())
            return child

        root_loop = await run_agent_loop(
            node=root,
            coord=coord,
            spec=spec,
            sandbox=sandbox,
            gateway=gateway,
            system_prompt=root_system_prompt,
            base_message={"role": "user", "content": _initial_prompt(spec)},
            is_root=True,
            max_turns=spec.max_turns,
            on_progress=on_progress,
            spawner=spawner,
            http=http,
            hint_provider=hint_provider,
        )
        root_outcome = root_loop.outcome
        root.outcome = root_outcome
        root.status = STATUS_COMPLETED if root_loop.finished else STATUS_FAILED

        # root 收尾后，等所有还在跑的子 agent 结束（避免协程泄漏 / 丢结果）。
        coord.stop_requested = False
        pending = [t for t in child_tasks.values() if not t.done()]
        if pending:
            # 给它们一个宽限窗口把手上这轮跑完；超时则标记停止。
            done, not_done = await asyncio.wait(pending, timeout=float(coord.child_budget) * 90)
            for t in not_done:
                t.cancel()
            # 让 _finalize 跑完
            await asyncio.sleep(0)

    # ── 汇总 ──────────────────────────────────────────────
    merged = _merge_outcomes(root_outcome, coord, child_tasks)
    return merged


def _child_task_prompt(spec: SolveSpec, *, child: AgentNode, coord: AgentCoordinator) -> str:
    """给子 agent 的首轮用户消息：题目信息 + 它的具体任务 + 已有共享状态。"""
    parts = [_initial_prompt(spec), "", "## 你的具体任务", child.task]
    notes = coord.shared.render_notes()
    if notes and notes != "（还没有任何笔记）":
        parts += ["", "## 其他智能体已记录的共享笔记（先读，别重复别人的工作）", notes]
    coverage = coord.shared.render_coverage()
    if coverage and coverage != "（还没有任何覆盖记录）":
        parts += ["", "## 已经有人测过的范围（这些别重测）", coverage]
    parts += [
        "",
        "## 交付方式",
        "完成后用 `FINAL: {...}` 给出你的结论（summary / tried / ruled_out / findings）。",
        "如果你的任务是验证某个具体问题，用 `agent_finish` 把发现与未决项记下来，再用 FINAL 收口。",
        "**只做分给你的这一件事**，不要重复其他智能体已经覆盖的范围。",
    ]
    return "\n".join(parts)


def _render_completion_text(node: AgentNode, res: AgentLoopResult) -> str:
    """把子 agent 的结果渲染成一段给 root 读的完成报告。"""
    o = res.outcome
    lines = [f"== 子智能体「{node.name}」({node.agent_id}) 完成报告 =="]
    lines.append(f"状态：{'成功' if node.status == STATUS_COMPLETED else '失败'}")
    if node.task:
        lines.append(f"任务：{node.task}")
    lines.append("")
    lines.append("摘要：" + (o.summary or "(无)"))
    if o.candidates:
        lines.append("候选 flag：" + ", ".join(c.value for c in o.candidates))
    if isinstance(o.findings, list) and o.findings:
        lines.append("发现：")
        lines += [f"- {_finding_title(f)}" for f in o.findings]
    if o.tried:
        lines.append("试过：" + "；".join(o.tried[:6]))
    if o.ruled_out:
        lines.append("已排除：" + "；".join(o.ruled_out[:6]))
    if o.error:
        lines.append(f"问题：{o.error}")
    return "\n".join(lines)


def _finding_title(finding: Any) -> str:
    if isinstance(finding, dict):
        return str(finding.get("title") or finding.get("name") or json.dumps(finding, ensure_ascii=False)[:120])
    return str(finding)[:120]


def _merge_outcomes(
    root_outcome: SolveOutcome,
    coord: AgentCoordinator,
    child_tasks: dict[str, asyncio.Task[AgentLoopResult]],
) -> SolveOutcome:
    """把 root 与所有子 agent 的 outcome 合并成一个。见 `run_multi_agent` docstring。"""
    merged = root_outcome

    # 候选 flag：root 优先；没有则用第一个有候选的子 agent。
    if not merged.candidates:
        for node in coord.nodes.values():
            if node.agent_id == "root":
                continue
            if node.outcome and node.outcome.candidates:
                merged.candidates = list(node.outcome.candidates)
                break

    # findings / workorders：合并去重（按标题）。
    seen_finding: set[str] = set()
    merged_findings: list[Any] = []
    merged_workorders: list[Any] = []
    seen_wo: set[str] = set()
    all_outcomes = [root_outcome] + [n.outcome for n in coord.nodes.values() if n.agent_id != "root"]

    def _add_findings(src: object) -> None:
        if not isinstance(src, list):
            return
        for f in src:
            key = _finding_title(f)
            if key not in seen_finding:
                seen_finding.add(key)
                merged_findings.append(f)

    def _add_workorders(src: object) -> None:
        if not isinstance(src, list):
            return
        for w in src:
            key = (
                str(w.get("title") or w.get("action") or "")
                if isinstance(w, dict)
                else str(w)
            )[:120]
            if key and key not in seen_wo:
                seen_wo.add(key)
                merged_workorders.append(w)

    for o in all_outcomes:
        if o is None:
            continue
        _add_findings(o.findings)
        _add_workorders(o.workorders)

    if merged_findings:
        merged.findings = merged_findings
    if merged_workorders:
        merged.workorders = merged_workorders

    # 汇总统计
    merged.turns = coord.turns_used
    merged.llm_calls = sum(o.llm_calls for o in all_outcomes if o)
    merged.tool_calls = sum(o.tool_calls for o in all_outcomes if o)
    merged.usages = [u for o in all_outcomes if o for u in o.usages]
    merged.tool_outputs = [t for o in all_outcomes if o for t in o.tool_outputs]
    merged.saw_tool_output = any(o.saw_tool_output for o in all_outcomes if o)

    # transcript：拼接（带 agent 名）
    parts: list[str] = []
    if root_outcome.transcript:
        parts.append("【root】\n" + root_outcome.transcript)
    for node in coord.nodes.values():
        if node.agent_id != "root" and node.outcome and node.outcome.transcript:
            parts.append(f"【{node.name}】\n" + node.outcome.transcript)
    merged.transcript = "\n\n".join(parts)

    return merged


__all__ = [
    "AgentCoordinator",
    "AgentNode",
    "AgentLoopResult",
    "SharedState",
    "MAX_CHILDREN",
    "child_turn_budget",
    "dispatch_orchestration_tool",
    "root_tool_names",
    "child_tool_names",
    "run_agent_loop",
    "run_multi_agent",
]
