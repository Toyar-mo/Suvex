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
    _wall_clock_band,
    _wall_clock_note,
    _wall_clock_seconds_left,
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

# ★ v0.79：强制注入的「当前状态」硬上限（字符）。
#
# 为什么需要这个数：system prompt 已经 ~18KB，再无限长会挤掉真正的方法论。
# 1400 字符大约是一屏，够写 8 条事实 + 8 个已测面 + 4 条待推进方向。
#
# ⚠️ 超限时**优先保留 negative 事实**（已排除的方向）—— 那是防止重复劳动
#    最直接的东西，比"已确认"更值钱：已确认的模型多半记得，已排除的它一定会重试。
LIVE_STATE_MAX_CHARS = 1400

#: ★ v0.77：同一个"打点形状"撞到第几次时提醒模型"换参数不算新尝试"。
#:
#: 3 而不是 2：第二次打同一个点很可能是正常的分支验证（先探 401，再试
#: 未带凭据）；到第三次基本可以确认是在原地换参数。
#: 提醒**只发一次**（`==` 而不是 `>=`）：反复提醒会挤占上下文，而模型的
#: 行为已经被那句提醒影响过一次了，再刷只会让它分心。
SHAPE_REPEAT_WARN_AT = 3

#: ★ v0.92：连续几次「采集失败」升格成一条结构性事实。
#:
#: ## 这修的是什么（32231 事故，缺陷 B）
#:
#: 03:33:49 起目标进入持续死亡，之后 **25.5 分钟、9 次 run_shell** 全在重试。
#: 模型 03:34:04 **自己意识到过**一次：
#:
#: > "The last sweep was all `000` — collection failure (server saturated),
#: >  not evidence."
#:
#: 它甚至调了 `list_coverage` 检查覆盖度。**但这个认知只活了一轮**，下一轮
#: 又回去重试了。平台没有任何机制把「连续 N 次 000」升级成一条结构性事实 ——
#: 而一旦 `record_fact` 进黑板，后续所有轮都能看到（「目标已死」这个判断
#: 现在是每次重新推导、随轮次蒸发）。
#:
#: ## 为什么是 3
#:
#: 2 次可能是抖动（服务在重启、网络在抖）—— 这时说「目标已死」会让模型
#: 过早放弃一个其实还活着、只是刚好卡了两下的目标。3 次连续才够说明
#: 「这不是偶发」。
#:
#: ## 为什么和 `SHAPE_REPEAT_WARN_AT` 分开
#:
#: 那个判「打的是不是同一个点」，这个判「打出去了但没有回应」。两者正交：
#: 换着路径全 000（这个要报）和同一条路径打到 200（那个要报）是两件事。
COLLECTION_FAIL_STREAK = 3

#: 输出里出现这些词，算「这一发没拿到任何目标数据」。
#:
#: ⚠️ 判的是**输出**不是退出码：curl 连不上时退出码 7、`-w %{http_code}`
#: 打 `000`、ffuf 报 `0 results` —— 形态太多，逐个判会漏。而"输出里有没有
#: 一个 3 位状态码 / 有没有连接被拒"覆盖面够且误判代价低（最坏是多一句提醒）。
#:
#: ⚠️ 刻意**不**收空输出：`grep` 没匹配到、`ls` 空目录都是正常的空输出，
#: 把它们算成采集失败会让模型以为目标死了，而目标可能好好的。
_COLLECTION_FAIL_TOKENS = (
    "connection refused",
    "failed to connect",
    "connection timed out",
    "operation timed out",
    "no route to host",
    "network is unreachable",
    "could not resolve host",
    "temporary failure in name resolution",
    "connection reset by peer",
    "curl: (7)",
    "curl: (28)",
    "curl: (6)",
    "curl: (56)",
    # `curl -sS` 没拿到响应时的形态。与 `-w '%{http_code}'` 打出的 `000`
    # 是**同一现象**（TCP 连上了、一个响应字节都没回），只是命令写法不同。
    # 32231 那次模型恰好用了 `-w`，所以 `000` 被识别；同一份输出换成
    # `-sS` 就会静默漏判 —— 同一个靶子死法，只因为少写一个参数。
    #
    # ⚠️ 刻意**不**收这几条（都实测过，都不是"发了但没拿到字节"）：
    #   - `curl: (35)` TLS 握手失败 —— TCP 已连上，靶子活着，只是不说 TLS。
    #     判成不可达正是把活靶子宣布成死的。
    #   - `wget: unable to resolve host address` —— 本地 DNS 失败，
    #     根本没碰到目标（与已收的 `could not resolve host` 不是一回事：
    #     那条是 curl 的措辞，配 `--resolve` 失效时才出现）。
    #   - `ssh: connect ... timed out` —— `ssh` 不在 `_ACTING_PROGRAMS`，
    #     所以这条根本走不到判据里。**刻意不收** ssh：它是运维/传输工具，
    #     本地 ssh 到构建机、跳板机会被误判成"在打靶"，而这类命令失败
    #     与靶子可达性无关。真要用 ssh 打目标，模型通常配 curl/nmap。
    "curl: (52)",
)

#: `curl -w '%{http_code}'` 打出来的「没拿到响应」。单独判，因为它是个裸的
#: `000`，不做边界限定会把 `Content-Length: 1000` 之类的数字误伤。
_HTTP_000 = re.compile(r"(?<![\d.])000(?![\d.])")

#: ★ 刻意**不**把「零结果」算成采集失败 —— 这条是实测纠正过来的。
#:
#: 32231 的 trace 里有两处形态诱人但会误判：
#:
#: ```
#: 03:01:44  ffuf ... -> Encountered error(s): 1 errors occurred.
#:                        * open /home/solver/.config/ffuf/scraper: no such file or directory
#: 03:02:39  gobuster common.txt -> 0 results   （靶子当时 HTTP 200，活得好好的）
#: ```
#:
#: 第一处是**本地配置坏了**（ffuf 的 scraper 路径不存在），第二处是**靶子健康
#: 且确实没暴露路径** —— 后者正是 32231 那条唯一站得住的结论（「GET-only 路由
#: 枚举干净」）。若把「0 results」或「encountered error」判成采集失败，就会在
#: 靶子活得好好的时候宣布它死了，并往黑板里写一条假事实 —— 那**比不提醒更糟**：
#: 它会让模型丢掉一个已经验证过的正确结论。
#:
#: 所以这里只认「一个响应字节都没拿到」：连接层失败 + `000`。零结果不算。

#: 输出里出现这些，说明**这一发确实拿到了目标数据**（哪怕同时还夹杂失败）。
#:
#: 三条判据：
#:
#: 1. **HTTP 状态行**（`HTTP/1.1 200`）—— 收到 `500` 也算活：5xx 是目标**自己**
#:    在报错，它还在跑。要抓的是「一个字节都没回来」，所以不区分 2xx/3xx/5xx。
#: 2. **裸的成功状态码**（`curl -w '%{http_code}'` 只打三位数，没有状态行）。
#:    32231 的 trace 里 `for i in 1..10; curl -w '%{http_code}' ...; done` 打的
#:    正是 `200 000 200` 这种混合输出 —— 只认状态行会把它判成失败，而靶子活着。
#: 3. **flag** —— 拿到 flag 时显然不该说目标死了。
#:
#: ⚠️ 第 2 条的前后向断言收得比 `\b` 紧：`\b` 在 `15016`/`1200` 这类数字里会
#: 误命中（`Content-Length`、`ETag` 到处都是），冒号也排除掉是为了不吃
#: `03:33:49` 这种时间戳里的 `333`。
#:
#: ## ★ v0.94：点号也必须排除（否则本机制在真实目标上近乎永久失效）
#:
#: 真实扫描实测（`scan_01M5SMPQ3CDDDHY46F30EWMA3D`，目标 `10.255.255.1:9`
#: 复核：11 次以上 `000`/`curl:(35)` 采集失败，`record_coverage` 13 次、
#: `record_fact` 8 次，**但「连续采集失败」升格一次都没触发**，库里那条
#: 「目标当前不可达」事实 0 条）。根因就是这个正则：
#:
#: ```
#: 10.255.255.1        → 命中 '255'（IP 第二段）→ 判成「拿到了目标数据」
#: code=000 total=0.200 → 命中 '200'（浮点秒数）→ 同上
#: ```
#:
#: 于是 `_looks_like_collection_failure` 恒返回 False，`collection_fail_streak`
#: 永远是 0。**影响面不是边角**：`curl -w '... total=%{time_total}'` 是这套
#: 提示词里最常见的写法，而 `200`/`203`/`255` 在任意三位八位组里都会出现 ——
#: 12 个常见目标地址里 5 个直接中招（`10.255.255.1`、`192.168.200.10`、
#: `203.0.113.50`、`172.16.255.1`、`300.1.1.1`）。
#:
#: 为什么点号两侧都要排：
#: - 左侧排 `.` → 挡住 `total=0.200` 与 `10.255` 的后两段
#: - 右侧排 `.` → 挡住 `300.1.1.1` 这种落在字符串开头的版本号/IP 首段
#:
#: 代价是「`200.` 结尾（句号紧贴）」这种写法不再算成功。实测真实输出里
#: 状态码后面从来不紧跟句号（要么换行、要么空格、要么 `OK`），而误判成
#: 「活着」的代价是把死靶子宣布成活的 —— 两害相权取此，与下面「宁可漏判
#: 失败」的取舍方向一致。
#:
#: ## ★ v0.94：状态行前面可能有 `curl -v` 的 `< ` 前缀
#:
#: 同一次真实扫描里发现的**第二个**漏判（与上面那个点号问题互相独立）：
#:
#: ```
#: curl -sv https://target/xxx
#: < HTTP/1.1 404 Not Found        ← 原来的 `^\s*HTTP/` 匹配不到
#: ```
#:
#: `curl -v` / `-sv` 把收到的每一行都加上 `< ` 前缀。原锚点要求行首只有
#: 空白，于是 **404/401/403/5xx 这些「目标明明回了话」的响应全被判成
#: 「一个字节都没回来」**。而 2xx/3xx 还有裸码那条兜底（`< 200` 的 `< `
#: 不是数字也不是点，兜底能命中），所以坏得最彻底的反而是 4xx/5xx ——
#: 恰好包含「服务活着但路径不对」和「服务活着但崩了」这两类最常见的
#: 正常结论。
#:
#: 这正是下面「宁可漏判失败」那段说的最坏情况：连吃 3 次后系统会往黑板
#: 写一条「目标当前不可达」的**假事实**，并且每轮推给所有 agent —— 模型
#: 会因此丢掉「服务活着、只是这个路径不存在」这个已经验证过的正确结论。
#:
#: 修法：行首允许 `\s` 与 `<`（`[\s<]*`）。不收 `>`（请求方向，模型自己的
#: 输出）与 `*`（curl 的注释行，那不可能是状态行）。
#:
#: ## 为什么这里宁可漏判失败
#:
#: 漏判失败 = 少提醒一次「目标可能死了」；误判失败 = 把**活着的**靶子宣布成死的，
#: 并往黑板写一条假事实。后者会毁掉模型已经验证过的正确结论（32231 里那条
#: 「GET-only 路由枚举干净」正是靠靶子健康时才成立的），代价高得多。
_SUCCESS_EVIDENCE = re.compile(
    r"^[\s<]*HTTP/[\d.]+\s+\d{3}\b"   # 状态行（含 curl -v 的 `< ` 前缀）
    r"|\bflag\{"                        # 拿到 flag
    r"|(?<![\d:.])[23]\d\d(?![\d:.])",  # 裸状态码 2xx/3xx（点号两侧都排，见上）
    re.IGNORECASE | re.MULTILINE,
)


def _looks_like_collection_failure(*, tool_name: str, command: str, output: str) -> bool:
    """这一发工具调用是不是「发出去但没拿到目标数据」。

    ## 为什么必须先确认「它真的打了目标」

    本地命令失败也常有连接字样（`pip install` 连不上镜像源、`git clone`
    被墙）。若不加这层，模型在容器里装个包失败就会被判「目标已死」，
    那是**比不提醒更糟的误导**。所以先用 `_looks_like_acting` 确认这次
    调用确实朝向目标，才把失败计入。

    ## 返回 False 的三种情况

    1. 不算行动（本地分析、记账、思考）—— 与目标可达性无关
    2. 输出里没有失败特征 —— 这一发可能成功了（甚至拿到了 flag）
    3. 输出里**同时**有成功标志 —— 见下

    ## 为什么"同时有成功标志"要判 False

    一条 `run_shell` 里常有多次请求（`for i in 1..10; curl ...; done` 打五个
    路径，三个 200 两个 000）。若只要出现一个 `000` 就判失败，那模型**打得越
    勤越会被判"靶子死了"**，而事实正相反。

    所以只要输出里出现了 `200`/`30x` 这类成功状态码，或找到了 flag，就认定
    这一发**拿到了目标数据**（哪怕部分请求失败）。宁可漏报也不要把活靶子
    宣布成死的 —— 那会毁掉模型已经验证过的结论。
    """
    if not _looks_like_acting(tool_name=tool_name, command=command):
        return False
    # 成功标志优先：拿到过响应就说明目标活着。
    if _SUCCESS_EVIDENCE.search(output):
        return False
    low = output.lower()
    if any(tok in low for tok in _COLLECTION_FAIL_TOKENS):
        return True
    return bool(_HTTP_000.search(output))


def _collection_outcome(*, tool_name: str, command: str, output: str) -> bool | None:
    """这一发调用关于「靶子可达性」给出了什么证据 —— **三态**。

    返回值：
    - `True`  明确拿到目标响应（状态行 / 2xx / flag）—— 靶子活着
    - `False` 明确了「发了但没回来字节」（000 / 连接被拒 / 超时 / 空回复）
    - `None`  说不清（没打靶，或打了但输出里既无成功也无失败特征）

    ## 为什么需要第三态

    `_looks_like_collection_failure` 是二值的：`False` 同时表示「成功了」
    和「看不出所以然」。用它驱动计数器就会把后者当成前者，而实测表明
    **后者占绝大多数**：`scan_01M5SMPQ3CDDDHY46F30EWMA3D` 打一个零字节
    返回的地址，72 次调用里 63 次属于「打了但说不清」（`nmap -Pn` 全丢包、
    `nc -z` 超时、gobuster 连上即断），二值口径判成 64 次成功，连续失败
    永远到不了 3。

    正例不该被这条影响：拿到状态行就是拿到状态行，仍然立刻清零。

    与 `_looks_like_collection_failure` 的关系：本函数是它的超集，只在
    「它说不失败」的内部再分出「明确成功」与「说不清」。保留旧函数是因为
    别处（换向提醒）只需要二值判断，不需要操心计数口径。
    """
    if _looks_like_collection_failure(
        tool_name=tool_name, command=command, output=output
    ):
        return False
    if not _looks_like_acting(tool_name=tool_name, command=command):
        return None  # 没打靶，对「靶子可不可达」零信息
    if _SUCCESS_EVIDENCE.search(output):
        return True
    return None

#: 一次 `run_shell` 里以**程序名**出现这些词，算"真的向目标发了请求"。
#:
#: 用**动作词**而不是"是否包含 http"：`grep -oE 'http[A-Za-z]*' bundle.js`
#: 里有 http 字样但一次请求都没发。反过来 `nmap -p- 10.0.0.5` 没有 http
#: 字样但确实是打目标。所以这里收的是发请求/发包的工具名。
#:
#: ⚠️ 这些名字必须出现在**命令段的第一个词**（剥掉 `sudo`/`timeout`/
#: `xargs` 这类包装后）才算数，不能是"整条命令里出现过这个子串"。
#: 子串匹配会把 `cat nmap.log` / `grep curl notes.txt` / `vim curl.conf`
#: / `less nmap_scan.xml` 全算成"在打靶" —— 这些一条请求都没发，
#: 只是在翻上一次的日志。v0.92 之前这个误判只影响一句换向建议，
#: v0.92 之后它成了"写不写黑板事实"的前置，代价从"多句废话"变成
#: "往黑板写一条假事实"，所以口径必须收紧。
_ACTING_PROGRAMS = frozenset(
    {
        "curl",
        "wget",
        "nmap",
        "ffuf",
        "gobuster",
        "dirb",
        "nikto",
        "whatweb",
        "sqlmap",
        "nc",
        "ncat",
        "socat",
        "openssl",  # 仅 s_client 子命令算（下面单独判）
        "mqtt",
        "redis-cli",
        "mysql",
        "psql",
        "mongosh",
        "docker",
    }
)

#: 这些前缀不改变"谁在发请求"，剥掉后继续看第一个真正的词。
_ACTING_WRAPPERS = frozenset(
    {"sudo", "env", "timeout", "nohup", "xargs", "nice", "stdbuf", "command", "exec"}
)

#: 命令分段：一条 `run_shell` 里常有 `a && b | c; d`。
_COMMAND_SEGMENT = re.compile(r"\|\||&&|\||;|\n|`|\$\(")


def _command_programs(command: str) -> list[str]:
    """把一条 shell 命令拆成"每个段实际在跑的程序名"。

    只做执行位置判定，不解析 shell 语法 —— 目标是区分"在打靶"和
    "在翻本地文件"，不是精确复现 shell 语义。

    ⚠️ 已知不精确之处，两处都**刻意不修**，因为修错的代价更大：

    1. **heredoc 正文里的行会被当成独立段**（`cat > x.sh <<EOF` 里写的
       `curl ...` 会被算成在打靶）。剥 heredoc 正文要处理 `<<EOF` /
       `<<-EOF` / `<<'EOF'` / 引号变体 / 正文缩进 / 同一行还有 `> file`
       重定向，剥错了就是漏判（真打靶被判成没打）。
    2. **包装器带参数的选项**（`env -u FOO curl ...` / `sudo -u www-data
       curl ...` / `timeout -s KILL 30 curl ...`）会让 `-u` 后面的那个词
       被当成程序名。要修得知道每个包装器的哪些选项吃参数，而 `sudo -u`
       吃、`curl -s` 不吃 —— 维护一张这样的表，出错时又是漏判。

    两处都落在**漏判**一侧（少记一次采集失败），不会凭空多记一条。
    """
    programs: list[str] = []
    for segment in _COMMAND_SEGMENT.split(command):
        words = segment.split()
        i = 0
        # 交替前进，直到既不剩赋值也不剩包装器。
        #
        # ⚠️ 这两种前缀会**互相嵌套**，所以不能顺序跑两遍：`env X=1 curl ...`
        # 的形态是先剥 `env`、才露出 `X=1`，而 `X=1 env curl ...` 是先跳过
        # 赋值才露出 `env`。只按一个固定顺序走，总有一种形态漏掉 —— 那种漏
        # 判的后果是「真打靶了却被判成没打靶」，采集失败不计数，模型会把
        # 「打不通」当成「扫完了没东西」。实测见 `_tmp/er/probe_acting_firstword.py`。
        while i < len(words):
            start = i
            # 跳过前置 VAR=value 赋值（`FOO=1 curl ...`）
            while i < len(words) and "=" in words[i] and words[i].split("=", 1)[0].isidentifier():
                i += 1
            # 剥掉包装器与它们自己的选项/时长参数
            while i < len(words) and _basename(words[i]) in _ACTING_WRAPPERS:
                i += 1
                while i < len(words) and (
                    words[i].startswith("-") or re.fullmatch(r"\d+[smhd]?", words[i])
                ):
                    i += 1
            if i == start:  # 这一轮什么都没吃掉，收敛
                break
        if i < len(words):
            programs.append(_basename(words[i]))
        # openssl 要看到 s_client 才算发请求
        if i < len(words) and _basename(words[i]) == "openssl" and "s_client" not in words:
            programs.pop()
    return programs


def _basename(word: str) -> str:
    """`/usr/bin/curl` → `curl`。"""
    return word.rsplit("/", 1)[-1]


def _looks_like_acting(*, tool_name: str, command: str) -> bool:
    """这一轮有没有真的向目标发起交互。

    判定顺序（2026-10-23 P2 真跑后定下的）：

    1. **`http_request`** → 一定是行动（它除了发请求没别的用途）。
    2. **其他非 `run_shell` 工具**（`record_coverage` / `write_note` /
       `think` / `create_agent` …）→ 未行动：记账与思考不碰目标。
    3. **`run_shell` 的某个命令段以活体请求程序开头**（`curl`/`nmap`/`ffuf`…）
       → 行动。哪怕同一条命令里也顺手 `grep` 了 —— 有一发打出去了就算打了。
    4. 其余（`grep 本地文件`、`python3` 解析、写脚本、`ls`）→ 未行动。

    ⚠️ 这是**启发式**，不是精确判定。它只回答"最近是不是在原地打转"，
    误判的代价是"门多给或少给一句换向建议"。

    ⚠️ v0.92 起它多了一个调用方：`_looks_like_collection_failure` 用它
    判断"这次失败是不是朝向目标的"。那个场景下误判的代价是**往黑板写
    一条假事实**（"目标不可达"），比"多句废话"重得多 —— 所以第 3 条从
    "命令里出现过这个子串"收紧成"命令段的第一个词是这个程序"。
    """
    if tool_name == "http_request":
        return True
    if tool_name != "run_shell":
        return False
    return any(prog in _ACTING_PROGRAMS for prog in _command_programs(command))


def collection_fail_note(*, streak: int) -> str:
    """连续 N 次采集失败时发给模型的那句提醒（v0.92，缺陷 B）。

    ## 与 `_wall_clock_note` 的分工

    那条管「你还有多少时间」，这条管「你的靶子还在不在」。32231 那次两个
    同时缺：模型既不知道只剩 12 秒，也不知道打了 25 分钟全是 000。

    ## 措辞上的三个刻意选择

    1. **点明"这不是'没漏洞'的证据"** —— 这是最关键的一句。模型把
       "采不到" 当成 "干净" 是这类事故的共同形态。报告里模型自己写
       "The last sweep was all `000` — collection failure, not evidence"，
       说明它知道这个区分，只是这个认知活不过一轮。
    2. **给出"现在该做什么"而不是只说"目标死了"** —— 只说事实会让模型
       停下来等（它没法修服务器），必须把它导向「把已有证据收口」。
    3. **明说这条已经写进黑板** —— 否则模型会把这条提醒当成一次性提示，
       下一轮继续重试（正是 32231 的形态）。
    """
    return (
        f"⚠️ **连续 {streak} 次采集失败**：目标当前不可达，这不是「没有漏洞」的证据。\n"
        "- 这更像是目标被打饱和/挂掉了（你和你自己的子智能体都在打它）；"
        "继续重试**只会加重它**，不会得到新结论；\n"
        "- 这条已经记进「已确认事实」，后续轮次都会看到，不需要你重新推导；\n"
        "- **现在该做的**：把已经拿到的证据收口成结论（写清哪几项确认了、"
        "哪几项因为目标不可达而未能验证），而不是再发一条重试命令。\n"
    )


def _collection_fact_text(*, streak: int) -> str:
    """写进黑板的那条事实正文。

    ⚠️ 与提醒分开写：提醒是**给人看的行动指引**，事实是**给后续轮次的
    状态记录**。事实必须能独立读懂（它会在别的 agent、别的轮次的 prompt 里
    单独出现），所以带上"连续 N 次"这个量化前提与"不代表没漏洞"这个判读边界。
    """
    return (
        f"目标当前不可达（连续 {streak} 次采集失败，多为 000 / 连接被拒 / 超时）。"
        "这不是「没有漏洞」的证据 —— 采集能力本身失效了。"
        "恢复可达前，任何「没找到」的结论都不成立。"
    )


#: v0.83：哪些工具调用算"干了一轮活"（→ 记账欠费 +1）。
#:
#: ## 为什么不是"每个工具调用都算"
#:
#: 记账类工具（`record_fact` / `add_fact` / `record_coverage` / `write_note` …）
#: 本身就是**在产出**，把它们算成欠账会形成一个荒谬的不动点：
#: 越记账欠账越多。所以它们必须排除，且 `record_fact` 还要**清零**。
#:
#: ## 为什么"读黑板"也不算欠账
#:
#: `get_scan_fact` / `get_memory` / `list_notes` 这些是**消费**而不是生产。
#: 一个模型可能连着读十个事实而不产出一条新的 —— 那不是欠账，
#: 那是它在用黑板（正是我们要的）。只有**探查**（碰了目标或处理了产物）
#: 才可能产生新结论，也才谈得上"该记没记"。
#:
#: ⚠️ 定义必须放在 `FACT_RECORD` 等常量**之后**（Python 模块级名字在
#:    执行到定义语句时才求值，放到常量前会 `NameError` 导入失败）。
_DEBT_FREE_TOOL_NAMES = frozenset({
    "record_fact",
    "get_scan_fact",
    "add_fact",
    "get_memory",
    "record_coverage",
    "list_coverage",
    "write_note",
    "list_notes",
    "think",
    "get_threat_model",
    "amend_threat_model",
    "add_idea",
    "resolve_idea",
    "create_todo",
    "list_todos",
    "update_todo",
    "view_agent_graph",
    "wait_for_agents",
    "send_message_to_agent",
})


def _counts_toward_fact_debt(*, tool_name: str) -> bool:
    """这次调用是否属于"干了活但没记账"里的那个活。

    判定只看**工具种类**，不看命令内容 —— 与 `_looks_like_acting` 不同：
    那边判的是"碰没碰目标"（要扫命令里的 curl/nmap），这边判的是
    "有没有可能产生新结论"（`run_shell` 里就算只是 grep 本地 bundle，
    也可能读出了端点清单，那同样是该记的东西）。

    ⚠️ 启发式，不是精确判定。误判的代价只是催早或催晚一次 ——
    而"不催"的代价已经由 v0.80 那轮实测给出：42 次调用、0 条事实。
    """
    return tool_name not in _DEBT_FREE_TOOL_NAMES

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
# ★ v0.79：项目黑板（落库版，学 CyberStrikeAI 的 project_facts）。
#
# ⚠️ 与 `add_fact`（`MEMORY_ADD`）的区别要说清，否则模型会两个都记或都不记：
#   · `add_fact`   = **本次运行的**内存态事实，进程结束就没了，跨扫描不继承；
#   · `record_fact` = **落库的**事实，跨轮次、跨扫描都还在（同目标下次能继承）。
#
# 既然两个都在，为什么不做成一个？因为内存态那个是 v0.75 就有的、已被
# 实测验证过（v0.77 那轮出了 5 条真发现，其中 2 条主动压级到 info），
# 而落库这个是新加的、依赖部署侧一次迁移。合成一个会让"迁移没跑"变成
# "记账功能整体失效"。分开之后，黑板挂了不影响原有记账。
FACT_RECORD = "record_fact"
FACT_GET = "get_scan_fact"

#: 黑板非空时，每轮追加给 root 的「怎么用黑板」引导（v0.80）。
#:
#: 为什么是一句常量而不是写进 `kernel_pentest.md`：
#: 提示词里已经有两节讲清楚了（6.2.1.2 / 6.2.1.3），而 v0.77 的实测
#: 是 `add_fact` **0 次** —— 说明"讲清楚"不等于"会被做"。这一句紧跟在
#: 黑板索引后面、每轮都在，位置就是它的全部价值。
#:
#: ⚠️ 只有索引块非空时才追加（见 `render_live_state`）：空黑板多一个字
#:    都会让"没有可推的状态就一个字节都不加"那条承诺失效。
FACT_INDEX_NUDGE = (
    "> 这块黑板是**系统替你看的账**：上面写着「已排除」的，别再用换参数的方式重试"
    "（换 payload / 换 header / 换大小写算同一件事）；本轮若要试，先在代码里验证。\n"
    "> 反过来，本轮**新得到**的结论（尤其是「试过走不通」）要用 `record_fact` 落库，"
    "否则下次扫同一个目标它不会在这里出现。要看某条全文（证据 / POC / 攻击链），"
    "调 `get_scan_fact(fact_key=...)`，**不要凭摘要臆造细节**。"
)

#: v0.83：连续多少个**动作轮**没 `record_fact` 就该催一次。
#:
#: ## 为什么需要这个（v0.80 实测证伪了「软提醒够了」）
#:
#: v0.80 上线后的第一轮真实扫描（`scan_01M5MTK3GA5EQ7BJ4CWYHC8YES`）
#: 跑了 42 次工具调用（41 次 `run_shell` + 1 次 `list_mcps`），
#: `record_fact` **0 次**、`record_coverage` **0 次**。
#:
#: 而那一轮里：工具注册在 `_SHARED` 里（模型看得见）、提示词 §6.2.1.2
#: 专门写了「什么时候该用 record_fact 而不是 add_fact」、`FACT_INDEX_NUDGE`
#: 也每轮跟着黑板推给它 —— **全都到位了，模型依然一次都不写**。
#:
#: 结论：和 v0.77 的 `add_fact=0` 是同一个病 ——
#: 「把规矩讲清楚」和「行为真的发生」之间没有因果关系。
#: 静态提示词会退化成背景音；而**软提醒**（"记得记"）同样退化成背景音，
#: 因为它没有代价、也不阻断任何事。
#:
#: ## 所以改成计数阈值 + 附带一次强制动作
#:
#: 不是每轮都催（那会变成噪音，模型学会无视它），而是**攒够 N 轮才催一次**，
#: 且催的时候给出**照抄即可的参数骨架** —— 把"要不要记"变成"填 payload"，
#: 前者需要判断力，后者只需要执行。
#:
#: 值选 6 而不是 3：探查阶段常有 3-4 轮纯读（下载 bundle、解包、grep），
#: 那些轮次本来就没有可记的结论；6 轮之后还没有任何一条结论才是异常信号。
FACT_DEBT_WINDOW = 6

#: `record_fact` 的参数骨架（v0.84）。
#:
#: ## 为什么单独抽成一个常量
#:
#: 这个骨架要在**两个地方**逐字出现：
#:   1. `FACT_DEBT_NUDGE`（欠账到阈值时插进状态段）
#:   2. `_do_record_fact` 的空参数分支（模型发了 `{}` 时的回包）
#:
#: 两处手写同一段 JSON 迟早会漂。抽出来之后改一处即可。
#:
#: ## 为什么必须带 `"args"`
#:
#: v0.83 的旧骨架把 `fact_key` / `summary` 放在**顶层**，而
#: `runner.parse_actions` 只认 `payload["args"]` —— 模型照抄旧骨架会得到
#: `args={}`，也就是**又一个空参数**。这正是 v0.83 那 3 次 `{}` 的候选成因
#: 之一，所以 v0.84 把包装层写死进来，不再留给人手补。
#:
#: ## 为什么用 `+` 拼而不是写进 f-string / `.format()`
#:
#: ⚠️ **拼接发生在定义时**：`+` 拼出来的字符串里就是真花括号，所以
#: 任何拿这个常量去 `.format()` 的地方都会把 `{"tool"` 当成替换字段。
#:
#: v0.84 第一次改写时就踩了：把 `FACT_DEBT_NUDGE` 定义成
#: `"...{n}...".format` 拼骨架，结果调用点 `.format(n=6)` 直接抛
#: `KeyError: '"tool"'` —— 那会让**每一个欠账到阈值的扫描崩**。
#: 所以拆成三段，只 format 真正含 `{n}` 的那段，骨架原样接上。
_RECORD_FACT_SKELETON = (
    '{"tool": "record_fact", "args": {'
    '"fact_key": "<category>/<slug>", '
    '"summary": "<一句话结论，≤300 字>", '
    '"confidence": "confirmed|tentative|deprecated", '
    '"evidence": "<你凭什么这么判断，一句话>"}}'
)

#: `record_coverage` 的参数骨架（v0.84 #4）。
#:
#: ## 为什么需要它 —— 一个实地观察到的半个轮次浪费
#:
#: 实测 `scan_01M5PGMX9ADDWV0V42B1W48ZF9` 第 652 行，模型写的是：
#:
#:     record_coverage {"area": "api-v1-unauthenticated-endpoint-fuzz", ...}
#:
#: **字段名猜成了 `area`**。拿到回包后它自己纠正了（第 654 行：
#: "The record_coverage needs `surface` field"），然后第 655 行重发成功 ——
#: 但那一轮白花了。
#:
#: 而同一份代码里 `record_fact` 早就有骨架了（`_RECORD_FACT_SKELETON`），
#: 所以它 v0.84 那轮一次空参数都没有。**两个工具的待遇应该一致。**
#:
#: 与 `_RECORD_FACT_SKELETON` 同样必须带 `"args"`：`parse_actions` 只认
#: `payload["args"]`，没有这个包装模型照抄会得到 `args={}`。
_COVERAGE_SKELETON = (
    '{"tool": "record_coverage", "args": {'
    '"surface": "<方法 + 路径 + 关键条件>", '
    '"risk": "<这个点理论上可能出什么问题>", '
    '"outcome": "<成立：… | 已排除：… | 未测完：…>"}}'
)

#: 欠账达到 `FACT_DEBT_WINDOW` 时追加的强制提醒（v0.83）。
#:
#: ⚠️ 只在**欠账触发**时出现，不是每轮。区别：
#:    · `FACT_INDEX_NUDGE` = 常驻引导（"怎么用黑板"）→ 每轮都在
#:    · 这一条 = 异常信号（"你已经 N 轮没记了"）→ 只在真的欠账时出现
#:
#: 两者都要，因为它们回答不同问题。只有前者时模型会读到麻木（v0.80 实证）；
#: 只有后者时它不知道平时该怎么用它。
#:
#: ## 为什么把参数骨架写进来
#:
#: 实测那 42 次调用里有大量"我已经确认了 X"的事实（比如 `/health` 不鉴权、
#: API 面是 `/kernel/*`），这些停在模型的推理里，一步都没落库。
#: 让模型从零构造 `record_fact` 的参数是一次额外的认知负担 ——
#: 而它现在正处在"忙着干活"的状态，任何额外负担都会被推迟。
#: 给一个只需替换内容的骨架，把成本降到接近零。
#:
#: ## ★ 拼接顺序：**先 format 前半段，再拼骨架**
#:
#: 换成"整条写完再 `.format(n=...)`"会崩：
#:
#:     FACT_DEBT_NUDGE.format(n=6)
#:     KeyError: '"tool"'
#:
#: 因为骨架里是**真**花括号。所以拆三段：`_FACT_DEBT_NUDGE_HEAD`
#: （唯一含 `{n}`，要 format）+ `_RECORD_FACT_SKELETON`（原样）
#: + `_FACT_DEBT_NUDGE_TAIL`（原样）。生产走 `fact_debt_nudge(n)`。
_FACT_DEBT_NUDGE_HEAD = (
    "⚠️ **你已经 {n} 轮没有把结论写进项目黑板了。**\n"
    "\n"
    "这一轮结束前，**先调一次 `record_fact`**，再继续你的探查。\n"
    "\n"
    "从上面 {n} 轮里挑**最有价值的一条**（通常是「试过走不通」或「确认了某个形状」），"
    "按这个骨架填：\n"
    "\n"
    "```\n"
    "ACTION: "
)

_FACT_DEBT_NUDGE_TAIL = (
    "\n"
    "```\n"
    "\n"
    "**为什么不能跳过**：这轮扫描结束、对话历史被裁掉之后，"
    "你上面那些结论就没了 —— 下次扫同一个目标要从头再来一遍。"
)


def fact_debt_nudge(n: int) -> str:
    """生成欠账提醒 —— **唯一正确的调用方式**。

    不要写 `FACT_DEBT_NUDGE.format(n=n)`：骨架里的花括号会让它抛
    `KeyError: '"tool"'`。见 `_FACT_DEBT_NUDGE_HEAD` 的说明。
    """
    return _FACT_DEBT_NUDGE_HEAD.format(n=n) + _RECORD_FACT_SKELETON + _FACT_DEBT_NUDGE_TAIL


#: 兼容用的常量（0 填充的示例文本），不是生产用法。
#:
#: `tests/kernel/test_fact_debt_nudge.py` 有多处 `from ... import FACT_DEBT_NUDGE`
#: 的引用，而那些测试验的是"提醒文案该有什么"，与那个数字无关 ——
#: 保留这个名字免得连带改一堆断言。生产一律调 `fact_debt_nudge(n)`。
FACT_DEBT_NUDGE = fact_debt_nudge(0)

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
    FACT_RECORD,
    FACT_GET,
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

    def render_live_state(
        self,
        *,
        turn: int = 0,
        max_turns: int = 0,
        fact_index: str = "",
        fact_debt: int = 0,
    ) -> str:
        """把共享状态渲染成**注入 system prompt** 的短状态（v0.79）。

        ## 为什么需要它（这不是 `render_*` 的重复品）

        v0.77 验证轮实测：`run_shell` 238 次，`record_coverage` 只有 **2 次**，
        `add_idea` / `add_fact` 各 **0 次**。工具都在、提示词也写了，模型不用。

        原因不是"看不见"——它每轮都看得到对话历史。真正的原因是**没有理由看**：
        只要历史还在，它凭记忆就够了；等到历史被裁掉（`MAX_HISTORY_TURNS`），
        它已经走远，而它并不知道自己忘了什么。

        `get_memory` 这类「模型主动调用」的机制在这个前提下**必然失效**——
        不觉得自己需要，就不会去取。所以改成**无条件推送**：拼进 system prompt，
        每轮都在，模型想不看都不行。

        ## 与 `_child_task_prompt` 的分工

        子 agent 拿到的是**它自己的 task + 全场覆盖**（见 `_child_task_prompt`），
        不注入全场事实——它只需要知道自己那块。全场状态只给 root，因为只有
        root 在做全局决策。

        ## 空状态返回空串

        首轮一个字节都不加。否则 system prompt 里挂着「（还没有…）」四行占位，
        既占预算又教模型"这些段可以空着"。

        ## ⚠️ 回合位置**不参与**"有没有内容"的判定

        「第 3/120 轮」这种话首轮就有、且永远有 —— 如果让它进 `parts`，
        `if not parts` 永远为假，空状态就会返回一段只有回合数的壳子，
        上面这段"一个字节都不加"的承诺当场失效（第一版就是这么写的，
        `check_live_state.py` 测出来空状态 len=48 才抓到）。

        所以回合位置单独判断：**只有当确实有状态可推时才附上它**。

        ## `fact_index` 由调用方传入，不在这里读 DB

        落库黑板（`record_fact` 写的那份）的索引块由 `DbFactStore.index_block()`
        渲染好传进来。**`SharedState` 是纯内存的**，它 import 不到 SQLAlchemy
        也不该 import —— 这条边界一破，`tests/kernel/` 里那些不碰数据库的
        用例就全跑不起来了。

        ⚠️ `fact_index` 非空时**即使 `self.facts` 为空也要出内容** ——
        那是别的轮次/别的扫描留下的事实，正是最该被看见的一类。

        ## 记账欠费（v0.83）由调用方传入，不在这里算

        `fact_debt` 状态挂在 `AgentCoordinator` 上（那里才知道"谁在干活"），
        `SharedState` 不认识 coordinator。所以用 `fact_debt` 参数注入 ——
        缺省 0 = 不提醒，CTF 路径与所有不传它的老调用方行为完全不变。
        """
        parts: list[str] = []

        # ② 待推进方向（只列 open/doing，死掉的已经转移进 facts）
        if any(v.get("status") in ("open", "doing") for v in self.ideas.values()):
            parts.append("## 当前方向\n" + self.render_ideas())

        # ③ 已确认事实 —— **negative 排前面**（见 `LIVE_STATE_MAX_CHARS` 注释）
        if self.facts:
            neg = [t for t, i in self.facts.items() if i.get("kind") == "negative"]
            rest = [t for t, i in self.facts.items() if i.get("kind") != "negative"]
            lines: list[str] = []
            for t in neg:
                ev = (self.facts[t].get("evidence") or "")[:80]
                lines.append(f"- [已排除] {t}" + (f"｜{ev}" if ev else ""))
            for t in rest:
                info = self.facts[t]
                tag = {"positive": "成立", "constraint": "约束"}.get(info.get("kind", "positive"), "?")
                ev = (info.get("evidence") or "")[:80]
                lines.append(f"- [{tag}] {t}" + (f"｜{ev}" if ev else ""))
            parts.append("## 已确认事实\n" + "\n".join(lines))

        # ④ 已测覆盖 —— 这是"我打过哪些点"的权威清单
        if self.coverage:
            lines = [f"- {s}：{i.get('outcome', '?')}" for s, i in self.coverage.items()]
            parts.append("## 已测位置\n" + "\n".join(lines))

        # ⑤ 落库黑板索引（跨轮次、跨扫描的事实，由调用方渲染好传进来）
        if fact_index:
            parts.append("## 项目黑板（持久，含上次扫描的结论）\n" + fact_index)

        # ⑤.5 v0.83：记账欠费 —— 攒够 `FACT_DEBT_WINDOW` 轮没记才催。
        #
        # ## 为什么必须**在 `if not parts` 之前** `append`
        #
        # 这一条本身就是"非空"的来源：模型可能一次 `record_coverage` /
        # `write_note` 都没写过，但已经 `run_shell` 了六轮 —— 此时
        # `parts` 里只有 `fact_debt` 这一项，如果把它放在 `if not parts`
        # 之后，恰好就是**最该被催的那一刻**被判成"空状态"直接 return 掉。
        # 这个 bug 在写测试时当场抓住（`render_live_state(fact_debt=6) == ""`）。
        #
        # ⚠️ 它不破坏"空黑板一个字节都不多"的约束：`fact_debt=0` 时
        #    什么都不 append，空状态仍然返回 ""（`test_empty_state_stays_empty`
        #    与 `test_live_state.py` 都钉着）。
        #
        # ## 为什么这条必须是"条件触发"而不是"每轮都在"
        #
        # `FACT_INDEX_NUDGE` 是常驻引导，v0.80 实测它单独存在时
        # `record_fact` 仍是 0 次 —— 因为在模型眼里它属于"背景说明"，
        # 和 system prompt 里其它几十条规矩没有区别。
        #
        # 一条**只在异常时出现**的话才有信号价值：它的出现本身就在说
        # "你现在做的事和规矩不一致"。这是它和常驻引导唯一的差别，
        # 也是它值得多占这几百字节的全部理由。
        if fact_debt >= FACT_DEBT_WINDOW:
            # ⚠️ 用 `fact_debt_nudge(n)`，**不是** `FACT_DEBT_NUDGE.format(n=...)` ——
            # 后者会抛 `KeyError: '"tool"'`（骨架里的真花括号被当成替换字段）。
            parts.append(fact_debt_nudge(fact_debt))

        if not parts:
            return ""
        # ① 回合位置 —— 放在最前，让"还剩多少"成为可感知的数字，而不是抽象的"要努力"。
        #    ⚠️ 在 `if not parts` **之后**才插，见上面 docstring 的说明。
        if max_turns > 0:
            left = max(0, max_turns - turn)
            parts.insert(0, f"（第 {turn}/{max_turns} 轮，剩 {left} 轮）")
        # ⑥ v0.80：黑板非空时，每轮给一句「怎么用」的引导。
        #
        # ## 为什么不能只靠提示词里那两节（6.2.1.2 / 6.2.1.3）
        #
        # v0.77 实测：`add_fact` 被调 **0 次**、`record_coverage` **2 次**。
        # 模型每轮都看得到对话历史，所以**不觉得自己需要记账** —— 直到历史
        # 被裁掉已经走远。这条实测推翻了"把规则写进提示词就够了"的假设：
        # 静态提示词里的规矩会退化成背景音，而**每轮都在、且紧挨着黑板
        # 内容**的一句话才有行为约束力。
        #
        # ## 为什么要同时提"别重试"和"记下来"
        #
        # 只说前者，下一轮的黑板永远是空的（没人往里写，这条提醒就没内容可读）；
        # 只说后者，模型会为了交差刷无意义条目。两个动作是一条链：
        # **记 → 推给它 → 不重试**。
        #
        # ⚠️ 放在 `if not parts` 之后：**空黑板时一个字节都不能多**。
        #    这条被 `tests/kernel/test_live_state.py` 的空状态断言钉着；
        #    它同时保证 prompt 前缀只在"空→非空"这一个方向变，
        #    已在跑的扫描不会因为这段文本而缓存全失效。
        if fact_index:
            parts.append(FACT_INDEX_NUDGE)

        body = "\n\n".join(parts)

        # ⑤ 硬上限：超了从**末尾**截（前面的方向与已排除事实最值钱），
        #    并显式标注被截断 —— 不静默丢，否则模型以为清单就这些。
        if len(body) > LIVE_STATE_MAX_CHARS:
            keep = LIVE_STATE_MAX_CHARS - 40
            body = body[:keep] + "\n…（更多记录请调用 get_memory 查看）"
        return "\n\n—— 当前状态（系统每轮推送，不是你的历史记忆）——\n" + body

    def render_global_constraints(self) -> str:
        """只渲染 `kind == "constraint"` 的事实 —— **给子 agent** 用（v0.92）。

        ## 为什么子 agent 需要这一条，而其他事实不需要

        `render_live_state` 只给 root，理由是"全场方向与事实对子 agent 是噪音，
        它会以为那些是自己的活儿"。那个理由对 `positive` / `negative` 成立，
        对 `constraint` **不成立**：

        | kind | 说的是什么 | 对子 agent |
        | --- | --- | --- |
        | `positive` | 某个策略有效 | 噪音（不是它的活儿） |
        | `negative` | 某条路已排除 | 噪音（它可能本来就该走那条） |
        | `constraint` | **靶子本身现在打不通** | ★ 物理前提 |

        靶子死了的时候，子 agent 枚举路径得到的每一条"没找到"都是**假阴性**。
        它不需要知道别人在干什么，但必须知道"你现在打的这个东西没回字节"。

        ## 为什么必须单独一个方法，而不是给 `render_live_state` 加参数

        那个方法的 `is_root` 守卫被 `tests/kernel/test_live_state.py:test_only_root_gets_it`
        按源码结构钉着（注入点前后不许重开 `if is_root:`）。加参数会让守卫
        变成"取决于参数"的软约定，那条测试就钉不住了。两个方法、两个调用点，
        各自的守卫都是硬结构。

        ## 为什么按 kind 过滤而不是按 `by`（谁写的）过滤

        写这条事实的 agent 可能是任意一个子 agent，而它要警示的是**所有**
        agent。按 `by` 过滤会让"root 写的只给 root 看"，正好漏掉最需要它的那些。

        ## 空时返回空串

        与 `render_live_state` 同一条纪律：没有约束时一个字节都不加，
        否则每轮都挂着一段占位、并且污染 system prompt 前缀（缓存全失效）。
        """
        constraints = [t for t, i in self.facts.items() if i.get("kind") == "constraint"]
        if not constraints:
            return ""
        lines = [f"- {t}" for t in constraints]
        return (
            "## 全局约束（靶子层面，与你的具体任务无关，但会否掉你的结论）\n"
            + "\n".join(lines)
        )


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

    #: ★ v0.92：**连续**几次工具调用「发出去但没拿到目标数据」。
    #:
    #: ## 与 `recent_actions` 为什么一个按 agent 分、一个不分
    #:
    #: `recent_actions` 判的是「**这个 agent** 是不是在原地打转」—— 那是关于
    #: 某个智能体的行为，所以必须按 agent 分（混着算会把 root 的 47% 稀释成
    #: 30%，门就不拦了）。
    #:
    #: 「目标可不可达」是关于**靶子**的事实，全场所有智能体打的是同一个靶子。
    #: 所以这里**故意全局**：a1 的三次 000 + root 的三次 000 就是六次，不该因为
    #: 分属两个 agent 就各算三次。任何一次成功采集都会把计数清零 —— 因此
    #: 一个 agent 在本地瞎折腾（DNS 写错）造成的失败，会被其它 agent 的
    #: 成功立刻抵消，不会误判整个靶子已死。
    collection_fail_streak: int = 0

    #: 这一轮「目标不可达」的升级是否已经发过。
    #:
    #: 发过就不再重复往上下文里塞 —— 提醒的**持久化**靠写进 `shared.facts`
    #: （每轮随「已确认事实」推送），而不是靠每轮重现一遍。
    #: 只在采集成功（靶子活过来）时复位，这样"死了→活了→又死了"能再报一次。
    collection_fail_escalated: bool = False

    def note_collection(self, *, ok: bool | None) -> None:
        """登记一次采集结果，维护连续失败计数与升级标志。

        只做计数，**不发提醒** —— 提醒由调用方在用得上它的地方拼
        （那一处才知道要拼进哪条消息、给哪个 agent 看）。

        ## ★ v0.95：`ok` 是**三态**，`None` = 无证据

        原先这里是二态 `bool`，调用方写 `ok=not collection_failed`。那个写法
        把「这次输出里没看到失败特征」等同于「这次成功了」—— 而绝大多数
        工具调用其实是**第三种情况**：打了目标，但输出里既没有成功标志
        （状态行 / 2xx / flag），也没有失败标志（000 / 连接被拒 / 超时），
        比如 `nmap -Pn` 打一个全丢包的地址、`nc -z` 超时、gobuster 撞上
        「连上就被关」的目标。

        后果是**任何一次无法归类的打靶调用都会把计数清零**，于是

            「不可达目标上永远攒不到连续 3 次失败」

        这不是理论：`scan_01M5SMPQ3CDDDHY46F30EWMA3D`（10.255.255.1:9，全程
        零字节返回）72 次调用里，二态模型判出 **63 次「成功」**、最长连续
        失败 2 次、升格 0 条；三态模型判出 1 次真成功、9 次明确失败、62 次
        无证据、最长连续失败 6 次、升格 2 条。
        同一个目标、同一批输出，差别只在「无证据」算不算成功。

        三态的口径与文件开头「宁可漏判失败」那条一致：
        - `True`  明确拿到目标响应 → 清零（靶子活着，这是最可靠的信号）
        - `False` 明确没拿到字节     → 计数 +1
        - `None`  说不清             → **两个计数都不动**（既不加也不清零）

        为什么 `None` 不清零：清零等于宣称「靶子活着」，而它只是想表达
        「这次没说清」。宁可让计数停在原处等下一次明确信号。
        """
        if ok is None:
            return
        if ok:
            self.collection_fail_streak = 0
            self.collection_fail_escalated = False
        else:
            self.collection_fail_streak += 1

    def take_escalation(self) -> int | None:
        """跨过阈值的那一次返回连续失败次数并置位；否则 `None`。

        ## 为什么阈值判断放在这里，不放在调用方

        原先这段条件是手写在 `_execute_tool_batch` 里的：

            if (coord.collection_fail_streak >= COLLECTION_FAIL_STREAK
                    and not coord.collection_fail_escalated):

        它读的两个字段和 `note_collection` 写的是同一对，却分散在两个地方 ——
        于是「计数怎么涨」和「涨到多少算数」可以各自漂移。2026-10-23 那次
        事故里，调用方写的是 `ok=not collection_failed`（二态），阈值照样是 3，
        两处都"没错"，但合起来永远达不到阈值。把判据收进同一个类，
        改口径时就不可能只改一半。

        调用方拿到返回值后只做**副作用**（写黑板、拼提醒），不碰状态机。
        返回次数而不是 `bool`，是因为事实正文里要写「连续 N 次」。
        """
        if (
            self.collection_fail_streak >= COLLECTION_FAIL_STREAK
            and not self.collection_fail_escalated
        ):
            self.collection_fail_escalated = True
            return self.collection_fail_streak
        return None

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

    # ── v0.83：记账欠费 ──────────────────────────────
    #
    # 与 `recent_actions` 分开记的理由：那个滑窗的语义是"最近 20 个动作里
    # 有几个真打目标"，用来判**打转**；这个计数器回答的是另一件事 ——
    # "我有多久没把结论记下来了"，用来判**欠账**。两者要的更新时机不同
    # （`recent_actions` 每个工具调用都记；这个只在"产生新信息"时涨、
    # 在 `record_fact` **写库成功**时清零），混进同一个列表会让两个判定
    # 互相污染。
    #
    # ⚠️ 同样**按 agent 分开**：理由与 `recent_actions` 完全相同 ——
    # 子 agent 的记账不能替代 root 的记账，反之亦然。
    #
    # ⚠️ 它挂在 coordinator 而不是 `SharedState` 上：`SharedState` 是**共享**
    # 内容（黑板），欠费是**每个 agent 自己的**记账状态，和
    # `recent_actions` / `todos` 同类。放错层会让"谁欠的"这个问题失去答案。
    #: **自上次 `record_fact` 以来**，该 agent 花了多少个"干活轮"。
    fact_debt: dict[str, int] = field(default_factory=dict)

    def note_fact_recorded(self, agent_id: str = "root") -> None:
        """记了一条 → 该 agent 的欠账清零。"""
        self.fact_debt[agent_id] = 0

    def note_investigation(self, agent_id: str = "root") -> None:
        """干了一轮活（且没记账）→ 欠账 +1。"""
        self.fact_debt[agent_id] = self.fact_debt.get(agent_id, 0) + 1

    def fact_debt_of(self, agent_id: str = "root") -> int:
        """该 agent 当前欠了多少轮没记账。"""
        return self.fact_debt.get(agent_id, 0)

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
    if name == FACT_RECORD:
        return _do_record_fact(args, coord=coord, me=me, spec=spec)
    if name == FACT_GET:
        return _do_get_scan_fact(args, coord=coord, spec=spec)
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
        # ★ v0.84 #4：空/错字段**不**只回一句"要 surface"。
        #
        # 实测（`scan_01M5PGMX9ADDWV0V42B1W48ZF9` 第 652 行）：模型第一次写的是
        #
        #     record_coverage {"area": "api-v1-unauthenticated-endpoint-fuzz", ...}
        #
        # —— 字段名猜成 `area`。它拿到报错后自己纠正了（第 654 行：
        # "The record_coverage needs `surface` field"），但那白花了半轮。
        #
        # 与 `_do_record_fact` 同一个判断：**报错回包是模型一定会读的**
        # （它在等结果），骨架放这里比放几万字提示词里命中率高。
        # 骨架形状与提示词 §6.2.1.1 的两个示例保持一致。
        return _json_err(
            "record_coverage 需要 `surface`（测了什么：方法 + 路径 + 关键条件）。"
            "按这个骨架填：\n"
            "ACTION: "
            + _COVERAGE_SKELETON
            + "\n"
            "`risk` 写这个点**理论上**可能出什么问题，`outcome` 写实际结果 —— "
            "开头用「成立：」/「已排除：」/「未测完：」标清楚。"
        )
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


def _do_record_fact(
    args: dict[str, Any], *, coord: AgentCoordinator, me: AgentNode, spec: SolveSpec
) -> str:
    """写一条**落库**的事实（同 fact_key 覆盖）。

    ## 为什么要落库而不是只进内存（`add_fact` 已经能做这件事了）

    `add_fact` 记的事实活不过这次运行：进程重启就没了，扫描结束就没了，
    下次对同一目标扫描时**什么也继承不到**。而"上次我试过 `/api/v1/scans`
    走不通"这种认知，恰恰是最该跨扫描保留的 —— 它的失效速度最慢，
    重复验证的成本却最高。

    ## 校验在 `app/facts/service.validate_fact`

    这里只做"把参数搬过去 + 把 ValueError 变成模型看得懂的 JSON"。
    fact_key 形状、confidence 三态、summary 长度上限都在 service 里 ——
    那些规则的唯一读者是模型，不该散落在 kernel。
    """
    store = getattr(spec, "fact_store", None)
    if store is None:
        # ⚠️ 不抛。模型调了一个合法存在的工具但部署侧没开 ——
        # 要让它知道"这次没有黑板"，然后继续干活，而不是卡在这一步。
        return _json_ok(
            note="本次扫描未启用项目黑板（record_fact 需要落库）。"
            "请改用 `add_fact` 记内存态事实，或直接继续。",
            stored=False,
        )
    if not args:
        # ★ v0.84：空参数**不**只回一句"要 fact_key"。
        #
        # v0.83 的真实扫描里 `record_fact` 被调了 11 次，其中 **3 次是空参数
        # `{}`**，全在最早期 —— 模型"想记账"但那一刻不知道骨架长什么样，
        # 先发个空对象试探（后面被 `_json_err` 教会了格式，所以后面 8 次都对）。
        #
        # 提示词 §6.2.1.2 现在给了骨架，但提示词是几万字的，模型在赶进度时
        # 会跳过细节。**报错回包是它一定会读的**（因为它在等结果），所以把
        # 骨架放在这里比放在提示词里命中率更高。两处都写不嫌多 —— 这段文字
        # 与 `FACT_DEBT_NUDGE` 里的骨架保持同一形状，改一处就要同步另一处。
        return _json_err(
            "record_fact 需要参数。按这个骨架填：\n"
            "\n"
            "ACTION: " + _RECORD_FACT_SKELETON + "\n"
            "\n"
            "`fact_key` 形状是 `category/slug`（全小写、只用 `-`），category 取 "
            "endpoint / credential / surface / ruled-out / constraint / note 之一；"
            "`summary` ≤300 字；`confidence` 取 confirmed / tentative / deprecated。"
        )
    try:
        res = store.record(
            fact_key=str(args.get("fact_key") or ""),
            summary=str(args.get("summary") or ""),
            category=(str(args["category"]) if args.get("category") else None),
            confidence=str(args.get("confidence") or "tentative"),
            trust_level=(str(args["trust_level"]) if args.get("trust_level") else None),
            detail=(str(args["detail"]) if args.get("detail") else None),
            evidence_excerpt=(
                str(args["evidence"]) if args.get("evidence") else None
            ),
            edges=args.get("edges") if isinstance(args.get("edges"), list) else None,
            recorded_by=me.agent_id,
        )
    except ValueError as exc:
        return _json_err(str(exc))
    except Exception:  # noqa: BLE001 - DB 抖一下不该让整场编排停摆
        logger.warning("写项目黑板失败", exc_info=True)
        return _json_ok(
            note="写黑板时出错（已记录日志）。继续用 `add_fact` 记内存态事实，不要重试。",
            stored=False,
        )
    # ★ v0.83：真的存进去了才销账。放在 `store.record` 成功之后、
    #   `stored=False` 那两条路径**不销账** —— 否则"调了但没存进去"
    #   会把欠费清掉，而模型手上依然什么都没有。这正是要防的假完成。
    coord.note_fact_recorded(me.agent_id)
    return _json_ok(
        fact_key=res["fact_key"],
        created=res["created"],
        version=res["version"],
        note=(
            "已写入黑板（新条目）。"
            if res["created"]
            else f"已更新黑板（第 {res['version']} 版）。"
        ),
    )


def _do_get_scan_fact(
    args: dict[str, Any], *, coord: AgentCoordinator, spec: SolveSpec
) -> str:
    """按 fact_key 取一条事实的**全文**。

    ## 为什么需要这个工具（索引块已经给了摘要）

    索引块（`render_fact_index`）每条只给 80 字符摘要，因为它是每轮进
    system prompt 的东西。而"这条事实的完整证据是什么"是**按需**的：
    模型在准备复述或据此下结论时才需要。

    ⚠️ 页脚（`INDEX_FOOTER`）明确写了「禁止凭摘要臆造细节」——
    那个禁止要有对应动作可做，否则只是一句空话。这个工具就是那个动作。
    """
    key_raw = str(args.get("fact_key") or "").strip()
    if not key_raw:
        return _json_err("需要 `fact_key`，例如 `endpoint/put-auth-users-id`。索引块里每条开头那个就是。")
    store = getattr(spec, "fact_store", None)
    if store is None:
        return _json_ok(note="本次扫描未启用项目黑板，没有可查的落库事实。", found=False)
    try:
        got = store.get(key_raw)
    except ValueError as exc:
        return _json_err(str(exc))
    except Exception:  # noqa: BLE001
        logger.warning("读项目黑板失败", exc_info=True)
        return _json_ok(note="读黑板时出错（已记录日志）。", found=False)
    if got is None:
        return _json_ok(
            note=f"黑板里没有 {key_raw!r}。先用 `record_fact` 记一条，或核对 key 拼写。",
            found=False,
        )
    return _json_ok(found=True, **got)


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
    #: ★ v0.92：墙钟档位（-1 = 还没催过）。**每条循环各记各的** —— root 与
    #: 各 child 是独立的 `run_agent_loop` 调用，共享一个墙钟但各自的对话历史
    #: 独立。同一条催促要发给每一个在跑的智能体：32231 那次恰恰是 root 收工了
    #: 而子 agent 还在跑（或反之），只提醒一边等于没提醒。
    _wall_warn_stage: int = -1

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

        # ★★ v0.92：**墙钟催促**（学 32231 事故）。
        #
        # ## 为什么多智能体这条路径特别需要它
        #
        # 单循环路径（`runner.solve_in_sandbox`）v0.70 起有回合三档催促，
        # 但**这条多智能体路径连那个都没有** —— 只有上面那句"只剩最后一轮"。
        # 而 32231 那次跑的正是多智能体（root + 3 child），于是：
        #
        # ```
        # 墙钟 deadline           : 03:57:36
        # 最后一条 run_shell 发起 : 03:57:24   ← 只剩 12 秒
        # 该命令实际跑完          : 03:59:16   ← 逾期 100 秒，全白费
        # ```
        #
        # `coord.remaining_budget()` 是**总轮次**预算（`max_turns * 3` = 1500），
        # 这次只用了 78 次工具调用 —— 轮次池几乎没动，墙钟却到点了。轮次维度
        # 的催促在这种"少轮次长墙钟"的扫描场景里**天然失效**，必须单独看时间。
        #
        # ⚠️ 每个 agent 各记各的 `_wall_warn_stage`：同一条催促要发给每一个
        #    在跑的智能体。只提醒 root 的话，子 agent 会在 root 收工后继续
        #    对着将死的目标发命令。
        wall_stage = _wall_clock_band(spec)
        if wall_stage is not None and wall_stage > _wall_warn_stage:
            _wall_warn_stage = wall_stage
            wall_left = _wall_clock_seconds_left(spec)
            if wall_left is not None:
                recent.append(
                    {
                        "role": "user",
                        "content": _wall_clock_note(
                            wall_stage, wall_left, (spec.wall_clock_seconds or 0) / 60
                        ),
                    }
                )
                log.append(f"[{node.agent_id} t{turn}] 墙钟催促（第 {wall_stage + 1} 档）：剩 {wall_left}s")

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

        # ★ v0.79：root 的 system prompt 每轮**重建**，把当前状态推给它。
        #
        # 为什么不改 `system_prompt` 本身：它是入参、被多个子 agent 共享引用，
        # 就地拼接会污染下一条循环。这里每轮算一份新的局部变量。
        #
        # 为什么只给 root：子 agent 有自己的 task 与全场覆盖（`_child_task_prompt`），
        # 全场方向与事实对它只是噪音 —— 它会以为那些是自己的活儿。
        _sys = system_prompt
        if is_root:
            # 落库黑板的索引块（v0.79）。`fact_store` 是可选注入的
            # （见 `SolveSpec.fact_store`）：CTF 解题路径与纯逻辑测试都是 None，
            # 那时这里拿到空串，`render_live_state` 自然不会多吐一段。
            #
            # ⚠️ 用 try/except 包住：DB 抖一下不该让整轮 LLM 调用失败。
            # 拿不到索引块就当没有黑板，继续跑 —— 记账是增强，不是前置条件。
            _fact_index = ""
            _store = getattr(spec, "fact_store", None)
            if _store is not None:
                try:
                    _fact_index = _store.index_block() or ""
                except Exception:  # noqa: BLE001 - 见上面的说明
                    logger.warning("渲染黑板索引失败，本轮按无黑板处理", exc_info=True)
            _live = coord.shared.render_live_state(
                turn=turn,
                max_turns=max_turns,
                fact_index=_fact_index,
                # v0.83：欠费按 agent 分，必须传"谁在看这段 prompt"对应的值，
                # 否则子 agent 会读到 root 的欠费（反之亦然）。
                fact_debt=coord.fact_debt_of(node.agent_id),
            )
            if _live:
                _sys = system_prompt + "\n\n" + _live
        else:
            # ★ v0.92：子 agent 只拿**全局约束**（靶子层面的事实），不拿全场方向。
            #
            # 与上面 `is_root` 分支的分工写在 `render_global_constraints` 的
            # docstring 里：`positive`/`negative` 对子 agent 是噪音，
            # 但 `constraint`（"靶子现在打不通"）会把它所有的"没找到"变成假阴性。
            #
            # ⚠️ 这是**每轮**算的，不是只在首轮：升格可能发生在子 agent 已经
            #    在跑之后（它自己连续打不通，或者别的 agent 打不通而计数器是
            #    全局的）。只写进首轮消息的话，那时已经在跑的 agent 一辈子
            #    都读不到这条（首轮 prompt 由 `_child_task_prompt` 一次性生成）。
            _constraints = coord.shared.render_global_constraints()
            if _constraints:
                _sys = system_prompt + "\n\n" + _constraints
        try:
            reply = await gateway.chat(_messages(), system=_sys)
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

            # ★ v0.83：记账欠费 —— 干了一轮"可能产出结论"的活就 +1。
            #   清零在 `_do_record_fact` 的**写库成功**之后（不在这里按工具名
            #   清，因为"调了 record_fact"和"记进去了"不是一回事：
            #   DB 抖动时 `stored=False`，模型手上仍然什么都没有，欠费不该销）。
            if _counts_toward_fact_debt(tool_name=tool_name):
                coord.note_investigation(node.agent_id)

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

            # ★ v0.92：连续采集失败升格为结构性事实（修 32231 事故缺陷 B）。
            #
            # ## 为什么记在这里而不是在 `_wall_clock_band` 那一层
            #
            # 这一层才拿得到 `result`（工具真实输出）。上面所有提醒判的都是
            # 「你发了什么命令」，只有这里知道「发出去之后有没有回来东西」。
            #
            # ## 为什么必须写进 `shared.facts` 而不是只拼进 `repeated_note`
            #
            # `repeated_note` 只出现在**这一条**工具结果后面，下一轮就没了。
            # 32231 的模型 03:34:04 自己意识到"all 000 — not evidence"，但
            # **这个认知只活了一轮**，下一轮又回去重试。写进 facts 才会随
            # 「已确认事实」每轮推送给所有 agent（`render_live_state` ③）。
            #
            # ## 为什么只在跨过阈值那一次写
            #
            # `add_fact` 是同文本覆盖，每次失败都写一次会把 facts 当成日志刷，
            # 且每轮都往 prompt 里塞同一条没信息量的东西。`escalated` 标志
            # 保证一个"死亡周期"只写一次；靶子活过来（采集成功）时复位，
            # 于是"死了→活了→又死了"能再报一次。
            outcome_ = _collection_outcome(
                tool_name=tool_name, command=command, output=result
            )
            coord.note_collection(ok=outcome_)
            escalated_streak = coord.take_escalation()
            if escalated_streak is not None:
                _streak = escalated_streak
                # 写黑板：root 每轮从 `render_live_state` 读到它，
                # 子 agent 从 `render_global_constraints` 读到它
                # （两者都按"每轮现算"推送，不是只写进某一轮的消息）。
                try:
                    coord.shared.add_fact(
                        _collection_fact_text(streak=_streak),
                        by=node.agent_id,
                        evidence=f"连续 {_streak} 次工具输出无响应字节",
                        kind="constraint",
                    )
                except ValueError:  # 正文为空/kind 非法 —— 不该发生，不让它炸整轮
                    logger.warning("写入目标不可达事实失败", exc_info=True)
                repeated_note += collection_fail_note(streak=_streak) + chr(10)
                log.append(
                    f"[{node.agent_id} t{turn}] 连续 {_streak} 次采集失败，已升格为事实"
                )

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
