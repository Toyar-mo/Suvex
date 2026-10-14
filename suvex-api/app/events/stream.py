"""SSE 帧的编码与事件流生成。

## 帧格式（契约 §4.2）

```
id: evt_09
event: activity
data: {"id":"evt_09", ...}

```

`id:` **必须给** —— 它是 `Last-Event-ID` 补发的依据，而浏览器
`EventSource` 会自动在重连时带上这个头。不给的话用户网络抖动一次就丢一段历史。

## 补发游标的三种来源

| 来源                     | 场景                       | 行为                       |
| ------------------------ | -------------------------- | -------------------------- |
| `Last-Event-ID` 请求头   | 浏览器自动重连             | 重放该事件之后的所有事件   |
| `?lastEventId=` 查询参数 | **首次**连接（见下）       | 同上                       |
| 都没有                   | 裸 `EventSource` 连接      | 从当前末尾开始，不重放     |

**为什么需要查询参数**：`EventSource` 只能在**重连**时自动带 `Last-Event-ID`，
首次连接带不了 —— 而首次连接恰恰是唯一有丢事件风险的时刻。契约 §4.4 要求
前端「先拉 `events/recent` 做 seed、再订阅」，这两步之间有一个几百毫秒的缝：
缝里产生的事件，seed 里没有、订阅也收不到。让前端把 seed 最后一条的 id
用 `?lastEventId=` 带上来，这条缝就关掉了。

不带游标时**从末尾开始**而不是从头重放：从头发的话，一个跑了三小时的扫描
会让每次打开页面都推几千帧，而前端会全部渲染一遍。

## `resync` 帧

契约 §4.4：若客户端 `Last-Event-ID` 之后的事件已被清理，服务端应发一个
`event: resync` 帧告知「需要重新拉取」。

我们把这个判断放宽了一点：**游标落后超过 `sse_replay_max_events` 时也发 resync**。
理由是"事件还在但积压了十万条"和"事件被清理了"对客户端是同一件事 ——
都该回退到 `GET /events/recent` 重新拉，而不是让服务端慢慢推。

## 轮询而不是进程内广播

事件流靠**每秒查一次 `activity_events`**，不用进程内的发布订阅。

代价是延迟上限约 1 秒（可调），换来的是：

* **跨进程正确**。将来若用多个 worker，或事件由独立进程写入，
  轮询照样能看到它们；进程内广播会静默漏掉 —— 而"漏事件"是不报错的失败。
* **补发逻辑只有一套**。广播 + 补发意味着两条路径，两条路径必然在边界上不一致。
* 一次带索引的 `WHERE seq > ?` 是微秒级；一个连接一秒一次，
  相比它占用的那个线程完全不是瓶颈。

`StreamingResponse` 接同步生成器时 Starlette 会把它放进线程池，
所以这里的 `time.sleep` 不会阻塞事件循环。代价是**每个连接占一个线程** ——
本平台同时只有 Activity 页一条流，够用。若将来要开很多条，把它们换成
async 生成器 + `asyncio.sleep` 即可，帧编码部分不用改。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from app.core.config import get_settings
from app.core.db import get_session_factory
from app.events import service
from app.events.models import ActivityEvent
from app.events.schemas import ActivityEventOut

SSE_MEDIA_TYPE = "text/event-stream"

# 一帧最多带多少条事件。太大的批次会让客户端在单帧里卡住渲染，
# 而且 TCP 层的分片会让"一帧一批"的边界变得不可预期。
BATCH_LIMIT = 200

# 轮询间隔（秒）。0.5 会让空转的查询翻倍，2 秒会让活动流明显发顿。
POLL_SECONDS = 1.0

# SSE 必须禁掉的中间层行为。
# `X-Accel-Buffering: no` 是给 nginx 的 —— 不加它，nginx 会把响应缓冲到
# 连接结束才发出去，表现是"活动流一直空白，直到扫描结束才一次性出现"。
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def encode_event(event: ActivityEvent) -> str:
    """一条完整事件 → 一个 SSE 帧。"""
    payload = ActivityEventOut.model_validate(event).model_dump(mode="json")
    # `ensure_ascii=False`：evidence / output 里大量是中文与原始响应片段。
    # 转成 \\uXXXX 会让帧体积涨 3 倍，而且日志里完全不可读。
    # SSE 规范本身规定流是 UTF-8，不需要转义。
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.id}\nevent: activity\ndata: {data}\n\n"


def encode_keepalive() -> str:
    """注释行心跳。

    契约 §4.2：每 15–30 秒发一个注释行，防止中间代理断连。
    用注释（以 `:` 开头）而不是空帧 —— 空帧在部分代理上会被当作
    "无内容"而触发缓冲，注释行是明确有内容但不触发任何事件的。
    """
    return ": keepalive\n\n"


def encode_open() -> str:
    """连接建立后**立刻**发的第一帧（2026-10-10 实测事故）。

    ## 它解决的是什么

    实测：直连后端 `:8000` 的 TTFB 是 **0.005 秒**，而经 Vite 代理 `:5173`
    却是 **22.4 秒** —— 并且那 22.4 秒后才出现的首个字节正是
    `: keepalive`（心跳是 20 秒）。

    根因不在我们这边，也不在 Vite：**Node 的 `res.writeHead()` 只把响应头
    记在内部状态里，真正写进 socket 要等第一次 `write()` 正文**
    （这正是 `res.flushHeaders()` 存在的原因）。代理收到我们的响应头后转给
    下游时调的就是 `writeHead()`，而 `stream_events` 在没有新事件时
    **一个字节都不产出**，于是响应头一直攒着，直到 20 秒后第一次心跳。

    ## 症状为什么特别难查

    浏览器 `EventSource` 的 `onopen` 要等响应头到达才触发。头被攒住 →
    `onopen` 迟迟不来 → 前端状态停在 `connecting` → 界面一直显示**「连接中」**。

    而**事件照常显示**（它们来自订阅前那次 `/events/recent` 的 seed），
    所以看起来像"流是通的，只是状态标签坏了"。实测时我先后怀疑过
    状态订阅、`onopen` 没绑上、CORS、Cookie，全都不是。

    ## 为什么修在这里，而不是去配代理

    在代理那侧 flush 也能让本机好起来，但那样只治一个代理。
    让**服务端连上就吐一个字节**是通用的：nginx、cloudflared、CDN
    任何会缓冲响应头的中间层都因此立刻放行。这与 `X-Accel-Buffering: no`
    是同一条思路（那条是让 nginx 别缓冲**响应体**，这条是别攒**响应头**）。

    用注释行而不是空帧，理由与 `encode_keepalive` 相同。
    与心跳区分成两种文字（`: open` / `: keepalive`）是为了排查时
    一眼能从 `curl -N` 的输出里看出"连上了"还是"又跳了一次心跳"。
    """
    return ": open\n\n"


def encode_resync(reason: str) -> str:
    payload = json.dumps({"reason": reason}, ensure_ascii=False)
    return f"event: resync\ndata: {payload}\n\n"


def resolve_cursor(
    scan_id: str | None, last_event_id: str | None
) -> tuple[int, str | None]:
    """算出起始游标，以及是否需要先发一帧 `resync`。返回 `(游标, resync 原因)`。"""
    cfg = get_settings()
    factory = get_session_factory()

    with factory() as db:
        latest = service.latest_seq(db, scan_id)

        if not last_event_id:
            # 没有游标 = 客户端没做 seed。从末尾开始，不重放历史。
            return latest, None

        seq = service.seq_for_event_id(db, last_event_id)
        if seq is None:
            return latest, "请求的事件已被清理，请重新拉取历史后再订阅。"
        if latest - seq > cfg.sse_replay_max_events:
            return latest, (
                f"待补发的事件超过上限（{cfg.sse_replay_max_events} 条），"
                "请重新拉取历史后再订阅。"
            )
        return seq, None


def stream_events(
    *,
    scan_id: str | None,
    last_event_id: str | None,
) -> Iterator[str]:
    """SSE 生成器。客户端断开时 Starlette 关闭它，`GeneratorExit` 在 `yield` 处抛出。"""
    cfg = get_settings()
    factory = get_session_factory()

    # ★ 连上就先吐一个字节 —— 见 `encode_open` 的说明。
    #   少了这一句，经代理时响应头会被攒到第一次心跳（默认 20 秒），
    #   而前端的 `onopen` 要等响应头，界面就一直停在「连接中」。
    yield encode_open()

    cursor, resync_reason = resolve_cursor(scan_id, last_event_id)
    if resync_reason:
        yield encode_resync(resync_reason)

    last_keepalive = time.monotonic()

    while True:
        # 每次轮询开一个**短命会话**，而不是全程占着一个。
        # 全程占用的话，几条 SSE 连接就能把连接池（默认 5+10）吃干，
        # 表现是「打开活动页之后，其余接口全部超时」——
        # 而这两件事看起来毫无关系，排查方向会完全跑偏。
        with factory() as db:
            events = service.events_after(
                db, cursor=cursor, scan_id=scan_id, limit=BATCH_LIMIT
            )
            # 在会话关闭前把要用的值取出来：Session 关闭后 ORM 对象会过期，
            # 再访问属性会触发一次对已关闭会话的刷新，抛 DetachedInstanceError。
            frames = [encode_event(event) for event in events]
            if events:
                cursor = events[-1].seq

        yield from frames

        if frames:
            last_keepalive = time.monotonic()
            # 攒满一批说明可能还有积压，立刻再查一次，不睡。
            if len(frames) >= BATCH_LIMIT:
                continue

        now = time.monotonic()
        if now - last_keepalive >= cfg.sse_heartbeat_seconds:
            yield encode_keepalive()
            last_keepalive = now

        time.sleep(POLL_SECONDS)
