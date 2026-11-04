# 对话功能原生工具调用迁移（v1.64 / v1.64.1）— 2026-11-04

> 用户请求（m00184）：「对齐 CyberStrikeAI 设计对话功能，别再出 bug 了」。
> 本文记录根因、方案、改动、验证与遗留。

## 1. 为什么要动架构，而不是再打一个补丁

对话引擎原来是一套**手写 ReAct 文本协议**：模型必须在正文里写 `ACTION: {"tool": ...}`
和 `FINAL:`，引擎用正则把它解析出来执行。这套协议的结构性代价，在 `app/kernel/runner.py`
的原始设计注释里被明确写成一个词——**「解析脆弱」**：

- 每个模型供应商的「自创写法」都要单独加一条正则去兜。历史补丁：`<result>`（v1.44）、
  `<ds_safety>`（v1.52）、`<system*>`（v1.56/v1.57）、`<parameter>`（v1.48/v1.52）、
  `<result_note>`（v1.63）……每次都是「模型自创新标签 → 泄漏进正文 / 被当成动作 → 加正则」。
- `engine.py` 最终累积了 **51 处** `_XML_*` / normalize / looks_like_plan 之类的正则补丁。

**CyberStrikeAI 的做法**是反面参照：它**直接删掉了手写 ReAct 路径**，改用
CloudWeGo Eino ADK 的原生 tool-calling + 一组中间件（`patch_tool_calls`、
`agentic_tool_pair_reconciler_middleware`、`continuation_user_dedup`、`context_budget` 等）。
**Agent 侧只有一种 tool call 范式**，正是来根治这一类问题的。

于是本次迁移的核心决定：**让模型走 provider 原生 `tool_calls`，不再让它手写文本协议。**

## 2. 方案：Approach A —— 只在「产出侧」换成原生，解析侧不动

难点在于「不动大梁」。关键探测（生产 provider `deepseek-v4.1-flash`，
base `http://10.39.200.13/v1`）证明了：

- 单选 / 纯答 / 多轮 round-trip / 一轮并发多个 tool_calls —— 全部正常；
- **Case Z**：历史里把 `assistant.tool_calls` 整条丢弃、只留 user 前缀
  `[工具结果]`，模型**仍能正确续跑**。⇒ **无需改 DB schema、无需重写 `_render_history`**。

所以采用「Approach A」：

```
模型发原生 tool_calls
        │
        ▼
engine._with_native_actions  ← 把原生调用**合成**成既有的 `ACTION: {...}` 文本
        │
        ▼
parse_actions / _narration_before_action / holdback / plan / nudge  ← 全部原样复用
```

**唯一变化在「模型怎么产出调用」**：它不再手写 ACTION 文本，而是发结构化 tool_calls；
引擎把它合回 ACTION 文本后，下面的解析/执行/落库链路一行没改。这样整类「文本标签泄漏」
被**从源头绕过**（模型根本不写标签了），而既有经过大量线上踩坑的解析逻辑不受影响。

对**不支持原生 tools 的网关**（如自建/中转网关）保留**文本协议降级**：系统提示词照旧教
它写 ACTION，且不发送 `tools=`。两种模式由同一个 `_gateway_supports_tools(gateway)`
谓词**同时**决定「发不发 tools」和「用哪版提示词」，避免自相矛盾。

## 3. 改动清单

### 后端 `suvex-api`

| 文件 | 改动 |
|---|---|
| `app/chat/prompts.py` | 新增 `_TOOL_PARAM_SCHEMAS`（16 工具逐字 JSON schema，键名与 `_execute_tool` 读取的 arg 键同源）+ `build_tools_schema()`（渲染 OpenAI `{"type":"function",...}`）；`build_system_prompt(native_tools=False)` 参数：native 模式把「ACTION 教学」整段（用一对 `####` 内部标记包住）替换成 `_NATIVE_TOOLS_SECTION`，text 模式清掉 `####` |
| `app/kernel/runner.py` | `LlmGateway._body(..., tools=None)`：OpenAI 分支注入 `tools`+`tool_choice="auto"`，Anthropic 分支转 `input_schema`；`chat()` / `chat_stream()` 加 `tools` 参数透传。**旧调用（不传 tools）body 逐字节不变** |
| `app/chat/engine.py` | `_supports_param`（签名探测含 VAR_KEYWORD 兜底）、`_gateway_chat`、`_gateway_supports_tools`、`_system_prompt_for(native_tools=)`；`_call_model(...,tools=)`；`run_turn` / `resume_approved` 两处传 `tools=None if at_limit else build_tools_schema()` + `native_tools=_gateway_supports_tools(gateway)` |
| `app/chat/nudge.py` | `should_nudge` 新增 `no_tool_rounds` / `max_no_tool_rounds`（见 §4） |

### v1.64.1 —— 迁移后暴露的第二个 bug（线上实测抓到，已修）

原生 tool-calling 之后，模型干完活就**不再调工具、只写一段结论**（没有 FINAL/ACTION 终止
信号）。而 `should_nudge` 的 `tools_used > 0` 分支是**无条件推**（"跑过工具就推"的安全网），
于是把一段完整结论当成"还没说完"反复推，模型只好反复复述完成情况——

线上实测（会话 `chat_01M6KEQ7VJTN9A5QER9KQJEV0B`，2026-11-04）：一个两步小任务，
`round_index` 已到 **38**、`tool_nudges_used` **33**，其中真工具调用只有 **2** 次，
其余全是模型在复述"两步都完成了"。这会把轮次一路烧到 `MAX_TOOL_NUDGES=300`。

**修法（对齐 `MAX_REPEATED_ACTIONS` 的思路）**：新增 `MAX_NO_TOOL_NUDGES=5`。
- `engine` 每轮统计 `no_tool_rounds`（这一轮有没有真的执行工具）；**执行工具即清零**。
- `should_nudge`：跑过工具、但**连续 `no_tool_rounds >= 5` 轮没再动手**、且正文没夹着
  要跑的 shell 命令 → 认定它在作答，**不再推**，落成答案收尾。
- 不误伤：真实任务里叙述后紧跟工具调用 → 计数清零，永远到不了 5；只有**卡住反复复述**
  的模型才会命中——正是要停的那种。

## 4. 验证

### 单元测试（全绿）
- `tests/chat/test_native_tool_calls.py`：37 passed。覆盖 `build_tools_schema`（16 工具顺序
  / required⊆properties）、OpenAI/Anthropic 两种 body 形态、`_supports_param`、两种 prompt、
  `_gateway_supports_tools` 三态、`run_turn` native&legacy 两路、以及 `should_nudge` 的
  `no_tool_rounds` 三条（到 5 停 / 带命令仍推 / 纯问答不受影响）。
- `tests/chat/` 全量：**457 passed, 1 skipped**。
- 调整了一条既有测试 `test_narration_nudges_do_not_eat_the_empty_reply_budget`：叙述轮数
  6→3（必须 < 新的 5 上限），验证「叙述纠偏不吃空回复额度」的本意不变。

### 真模型 E2E（手动）
- round-by-round：`finish=tool_calls`、`native=['run_shell']`、`parse_actions` 正确解出
  结构化参数、**leak=False**（无 `result_note`/`<result>`/`</parameter>` 泄漏）。

### 线上（Kali prod `http://192.168.44.128:8080`，viewer `zzviewer88`）
- 部署 `engine.py`/`prompts.py`/`runner.py`/`nudge.py`，重启 `Application startup complete`、health 200。
- **两步任务**（创建 hello.txt + 读回）：3 次真实 `run_shell`，**单条干净答案**
  「两步都已完成…读回内容原样为 原生工具调用测试成功。与要求写入的字符串完全一致，结束。」，
  **零重复、零泄漏**，`chat_turn_runs` 终态 **done / round 8 / tools 3**（修复前是 38+ 轮）。
  截图 `artifacts/v164-fixed-clean.png`（已视觉复核）。
- **中断/继续不回归**：`sleep 90` 长任务中断 → 弹「中断并继续 / 彻底停止」；点「彻底停止」→
  红色「已请求停止（第 1 轮）：当前正在跑的工具没法中途打断，等它返回后这一轮就会结束。
  不用再点中断。」；`sleep` 返回后 `chat_turn_runs` 终态 **cancelled**。

## 5. 遗留 / 后续

- 前端数据契约未变（工具卡片、过程详情折叠、中断继续、进度轮询均照常），无需改前端。
- 文本协议降级路径保留（旧/中转网关用），未删除，仅在不支持 tools 的接入点启用。
- `MAX_NO_TOOL_NUDGES` 默认 5，可经 `SUPEX_CHAT_MAX_NO_TOOL_NUDGES` 调。
