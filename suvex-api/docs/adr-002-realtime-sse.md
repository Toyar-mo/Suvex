# ADR-002：实时通道用 SSE，模块命名为 `events`

- **状态**：已采纳（后端任务 1）
- **相关**：契约 §0.1、§4；任务说明里的模块清单 `agents_ws`

## 背景

后端任务 1 的模块清单写的是：

> FastAPI 项目结构：按领域拆分模块（auth / providers / scans / findings /
> reports / **agents_ws**）

`_ws` 后缀指向 WebSocket。而契约 §0.1 已经把「实时通道」这一项裁决掉了：

|          |                                                    |
| -------- | -------------------------------------------------- |
| 假设说   | 实时数据用 **WebSocket**                           |
| 代码实际 | 前端接入点注释与 `ActivityPage` 的 TODO 都写的是 **SSE** |
| **决策** | **SSE**                                            |

## 决策

1. **传输用 SSE**（契约已裁）。
2. **包名叫 `app/events`，不叫 `agents_ws`。**

## 理由

### 传输：契约的三条理由都成立，实现时又验证了一遍

1. **事件流是单向的**（服务端 → 客户端）。WebSocket 的双向能力用不上，
   却要多维护一层连接状态机 —— 而这一层在 SSE 里由浏览器负责。
2. **断线补发是内建的**。SSE 用 `Last-Event-ID` + 服务端重放，
   浏览器 `EventSource` **重连时自动带上这个头**。
   WebSocket 要在应用层自己实现补发游标 ——
   而"补发漏掉的事件"正是 `activity-mock.ts` 明确列出的三个复杂度之一。
   实现时这一点体现得很具体：服务端的补发逻辑就是一个
   `WHERE seq > ?`，游标由协议本身负责送达。
3. **前端零改动**。`useActivityStream` 的消费端已按"重连 + 按 `at` 排序 +
   120ms 批量合并"设计，形状就是 `EventSource` 的。

### 命名：`agents_ws` 这个名字会误导

包名是给人看的契约。留一个 `agents_ws` 包，后来者第一件事是去找
WebSocket 握手在哪 —— 而它不存在。这属于"文档与实现漂移"里最难查的一类：
**名字说的是一件没发生的事**。

`app/events` 描述的是它的领域（事件流），而不是它碰巧用的传输方式。
真要说传输，SSE 与 WebSocket 都只是 `stream.py` 里的一个函数。

## 实现要点（供后续任务参考）

| 项 | 做法 | 为什么 |
| --- | --- | --- |
| 补发游标 | `seq` 自增列 + `WHERE seq > ?` | ULID 在同一毫秒内无序，做游标要退化成字符串比较；自增列走主键索引 |
| 首次连接 | 支持 `?lastEventId=` 查询参数 | `EventSource` **只在重连时**带 `Last-Event-ID`；首次连接正是唯一有丢事件风险的时刻（seed 与订阅之间的缝） |
| 无游标 | 从当前末尾开始 | 从头发的话，跑了三小时的扫描每次打开页面都推几千帧 |
| 补发上限 | 超出 `SSE_REPLAY_MAX_EVENTS` 发 `resync` | 「积压十万条」与「已被清理」对客户端是同一件事 |
| 推送方式 | **每秒查一次表**，不用进程内广播 | 跨进程正确（多 worker 或独立写入进程都能看到）；补发逻辑只有一套。代价是延迟上限约 1 秒 |
| 生成器 | 同步生成器 + `StreamingResponse` | Starlette 会把它放进线程池，`time.sleep` 不阻塞事件循环。代价：每条连接占一个线程 |
| 心跳 | 注释行 `: keepalive` | 空帧在部分代理上会被当作"无内容"而触发缓冲 |
| 响应头 | `X-Accel-Buffering: no` | 不加它，nginx 会缓冲到连接结束才发 —— 表现是"活动流一直空白，直到扫描结束才一次性出现" |

## 代价（如实记）

1. **HTTP/1.1 下每域名 6 连接**（契约 §0.1 已记）。
   本平台同时只开一条流（Activity 页），不构成问题；要同时订阅多条走 HTTP/2。
2. **每个 SSE 连接占一个线程**（同步生成器跑在线程池里）。
   几个人用没问题；要开很多条就换成 async 生成器 + `asyncio.sleep`，
   帧编码部分（`encode_event`）不用改。
3. **延迟上限约 1 秒**（轮询间隔）。对"看 agent 在做什么"够用；
   要更低就调小 `POLL_SECONDS`，但空转查询会成比例增加。
4. **`resync` 帧需要前端配合**：收到它之后要回退到
   `GET /scans/{id}/events/recent` 重新拉历史。这条目前**只写进了契约**，
   前端还没接（`useActivityStream` 仅处理 `activity` 事件）。

## 测试

- `test_stream_replays_only_events_after_cursor` —— 补发语义，一条不多一条不少
- `test_stale_cursor_asks_for_resync` —— 游标失效时必须出声，不能静默继续
- `test_cursor_without_id_starts_at_tail` —— 无游标不重放历史
- `test_event_frame_format` —— `id:` 必须给（补发的唯一依据）、中文不转义
- `test_keepalive_is_a_comment_line`
- `test_sse_endpoint_wires_streaming_response_correctly` —— HTTP 接线
  （生成器在这里被换成有限生成器；真实长连接由验收脚本覆盖）

> ⚠️ **不用 `TestClient` 打真实 SSE 端点**：真实的 `stream_events` 是永不结束的，
> 会让整轮 pytest 挂住（实测卡了 7 分钟无输出）。给它加"只跑 3 秒"的开关
> 是更坏的做法（生产代码里多一个只为测试存在的分支）。
> 真正的端到端覆盖在 `scripts/verify_acceptance.py`：起真 uvicorn、
> 开真 socket、连上之后写一条新事件、等它推过来、再关掉。
