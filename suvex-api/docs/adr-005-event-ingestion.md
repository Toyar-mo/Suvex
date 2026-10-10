# ADR-005：事件接入 —— 只补解析层，传输层保持 SSE

- **状态**：已采纳（后端任务 4）
- **相关**：契约 §0.1、§2.5、§2.6、§4、§4.3.1；任务 4 的四条要求

## 背景

任务 4 要求把外部工具（strix）产出的实时事件推给前端，替换掉 Activity 页的
mock 流。四条要求：① 捕获并**结构化**外部输出；② WebSocket 端点按 scan_id 订阅、
支持多客户端；③ 先落库再推（可回放）；④ 断线重连补齐（`last_event_id` 增量拉取）。

## 决策

### 1. 传输层**不动**，仍然是 SSE

任务两次写"WebSocket"，而契约 §0.1 已裁决用 SSE，且任务 1 已实现完整端点。
逐条对照任务 4 的四条要求：

| 要求 | SSE 侧现状 |
| --- | --- |
| ② 按 scan_id 订阅、多客户端、即时推送 | ✅ 已有：`GET /scans/{id}/events`，SSE 天然支持任意多个独立连接 |
| ③ 先落库再推、可回放 | ✅ 已有：事件先写 `activity_events`，`/events/recent` 做 seed |
| ④ 断线重连补齐 | ✅ 已有：`Last-Event-ID` 头 + `?lastEventId=` 参数 + `resync` 帧 |
| ① 捕获并结构化外部事件 | ❌ **缺的就是这一条** |

也就是说：四条要求里三条已经满足，换成 WebSocket **不会带来任何新增能力**，
却要丢掉已经能用的补发/心跳/多客户端机制，再实现一遍。

WebSocket 相对 SSE 的两个实质优势是**双向**与**二进制帧** ——
在这个场景（服务端单向推文本事件）都用不上。契约 §0.1 已经论证过这一点，
本次实测没有推翻它的理由。

> 这是任务与契约的**第二次冲突**（第一次是任务 1 的 JWT，见 ADR-001）。
> 两次的处理方式一致：按契约实现，把偏离写进 ADR，并说清为什么不按任务字面做。

### 2. 事件格式**照源码写，不照文档写**

★ 任务原话：「看一下 strix 是否有现成的结构化日志/事件输出方式
（**先去查它的文档/源码，不要凭空假设格式**）」。

查证结果：**官方文档写错了。** 文档说事件在
`strix_runs/<run_name>/events.jsonl`，而我在本机装好的 `strix-agent` 1.6.2
源码里全包搜索 `events.jsonl` —— **零命中**。那个文件不存在。

> 这条正好印证了"先查再写"的价值：照着文档写，我会实现一个永远读不到数据的
> 解析器，而它不会报错 —— 只会在真实扫描时安静地什么都不产出。

真实产物与依据（详见契约 §4.3.1 的表）：

| 文件 | 依据 |
| --- | --- |
| `run.json` | `core/paths.py: RUN_RECORD_FILENAME` |
| `.state/agents.json` | `interface/tui/live_view.py: hydrate_from_run_dir` |
| `.state/agents.db` | `interface/viewer/transcript.py` + `interface/tui/history.py` |
| `strix.log` | `telemetry/logging.py: _FORMAT`（纯文本，没有结构） |

**结构化事件流在 `agents.db` 里**，strix 自己的 TUI 与 Web viewer 都读它。
所以我复刻的是**它的**解析逻辑（`TuiLiveView`），不是自己发明一套。

### 3. 不去 tail `strix.log`

那是纯文本（`时间 级别 scan_id agent_id logger: 消息`）。从文本里正则抠
"这是一次工具调用"必然脆，而结构化数据就在同一个目录里躺着。
用得着正则的地方一处都没有。

## 语义映射：诚实标出哪些是确证的

契约定义了 **8 种**事件类型，而 strix 的结构化流只有 **2 种**
（`chat` / `tool`）。所以这不是改字段名，是一次真实的语义翻译。
每个映射的依据强度都写进了契约 §4.3.1 的表：

* ✅ **确证**：`tool_call`（直接对应）、`failed`（`result.success is False`）、
  `reasoning` ← `think` 工具（该工具签名就是 `think(thought: str)`，
  docstring 写明是内心推理）；
* ⚠️ **语义近似**：`reasoning` ← assistant 发言（契约的 8 种里没有"对外发言"
  这一类，用 `detail.intent` 区分 `plan` / `narration`）；
* ⚠️ **载荷启发式**：`discovery` 的三个字段按"名字最可能对得上"取，
  **需要一次真实扫描对着 `vulnerabilities.json` 核对之后才算坐实**。

**8 种里有 4 种在 strix 侧没有对应来源**，如实记：`poc_generated` /
`verified` / `blocked` / `completed`。其中 `completed` 由扫描生命周期发出
（ADR-004），另三种外部工具根本没有对应的结构化信号。

### 4. 幂等靠数据库索引，不靠"先查后写"

导入器每轮调度都跑一次（扫描还在写外部库）。去重靠
`activity_events` 上的 `(scan_id, source_row_id)` **部分唯一索引**，
撞了就是一个 `IntegrityError`，跳过即可。

不用"先 SELECT 看有没有再 INSERT"：那在并发下是有洞的
（两个导入器同时查到"没有"），而唯一索引是原子的。

### 5. 游标来自数据库，不从内存记

`max(source_row_id) WHERE scan_id = ?`。内存游标在服务重启后就丢了，
那意味着每次重启都要重读整个外部库 —— 而重复读的代价不只是慢：
去重逻辑一旦有洞，重复事件就真的落库了。

## 实现时的两个发现

1. **我自己的注释踩了转义坑。** 在 docstring 里写 `\uXXXX`（想举例说明
   不使用 `ensure_ascii=False` 的后果）时，Python 把它当成真的 Unicode 转义
   去解，后面不是合法十六进制 → **整个模块 import 失败**。
   一个纯注释的改动让模块起不来，报错还指向一个看不懂的 Unicode 位置。
   已修，并在那里留了说明。

2. **测试暴露了 `failed` 载荷的形状差异。** 我按 `detail["tool"]` 取工具名，
   而契约 §2.6 里 `failed` 的载荷是 `{kind, attempted, error}` ——
   **`tool` 只属于 `tool_call`**。测试当场报 `KeyError`。
   这是"照契约写"而不是"照顺手写"的价值：前台渲染时才发现的话，
   表现会是活动流里某类事件不显示工具名。

## 代价与边界（如实记）

1. **一条工具调用只产出一条事件（在它结束时）。**
   strix 自己是"先建调用事件、结果回来再 `version+1` 更新同一条"，
   而契约的 `ActivityEvent` **没有版本号字段**。要在契约里表达"同一条被更新"
   就得加字段（前端类型变更），所以代价是：**活动流里看不到"正在执行"的那一行**，
   只在工具结束时出现。要那个效果得给契约加版本号，属于后续工作。

2. **`tool_call` 的 `exitCode` 与 `durationMs` 恒为 `null`。**
   外部工具只给 `result.success` 布尔，没有单次调用的退出码与耗时。
   编一个 `0` 会让人以为那是真的进程退出码。

3. **★ 格式来自源码，未与真实产物对账。**
   测试用的是**按源码 schema 造出来的库**（`sqlite3` 建表语句逐字照抄 SDK）。
   它能证明"解析逻辑与源码描述的格式一致"，**不能**证明
   "真实 strix 写出来的库逐字段就是这个样子" —— 那需要一次真实运行，
   而本机没有 Docker（见 README「已知未验证项」）。
   装好 Docker 之后第一件该做的事就是：跑一次真实扫描，
   对着 `vulnerabilities.json` 与 `agents.db` 核对 `discovery` 的字段名。

4. **实时性的粒度是一个调度轮询周期**（默认 2 秒）。
   事件在"外部库落一行"到"我们导进来"之间最多差 2 秒。
   对"看 agent 在做什么"够用；要更低就调小 `SUPEX_SCAN_POLL_SECONDS`。

## 测试

新增 37 个用例（`tests/test_strix_events.py` + `tests/test_event_import.py`），
每一条断言都能追到 strix 源码的某一行（文件头有对照表）。
其中最关键的两条：

* `test_reimporting_is_idempotent` —— 反复导入三次，只落一条。
  没有它，活动流里每条事件会重复几十次，而那看起来像"agent 疯了"。
* `test_progressive_import_matches_single_pass` —— **边跑边导的结果
  与最后一次性导的结果必须一致**。不一致的话，"实时看到的"与
  "刷新后回放的"就是两份不同的历史，而任务 4 的验收标准同时要求这两件事。

替身后端（`FakeRunner`）现在产出**真实结构**的 strix 产物，
所以端到端验收走的是完整链路：外部库 → 解析 → 落库 → API 读回。
