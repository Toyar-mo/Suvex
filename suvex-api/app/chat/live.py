"""进行中对话的**可重放事件总线**（对齐 OMP 的 resumable stream）。

## 问题

`POST /messages/stream` 的 SSE 只属于发起它的那条 HTTP 请求。刷新页面、
切走再回来、或者从侧栏打开一个正在跑的会话时，前端握不到那条流，只能轮询
已落库的消息 —— 于是正文是「一段一段蹦出来」的，而不是 token 流。

## 做法

本轮所有 `emit` 帧在推给原 SSE 的同时，写入本进程的环形缓冲，并广播给
`GET /chat/sessions/{id}/stream` 的订阅者。新页面连上后先回放缓冲，再接实时。

单进程、单 worker 部署足够；多 worker 需要 Redis/pubsub，本实现不假装支持。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

MAX_BUFFER_FRAMES = 4000
MAX_BUFFER_CHARS = 2_000_000


class LiveTurnHub:
    def __init__(self) -> None:
        self._buffer: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._ended: set[str] = set()
        self._lock = asyncio.Lock()

    async def begin(self, session_id: str) -> None:
        async with self._lock:
            self._buffer[session_id] = []
            self._ended.discard(session_id)

    async def publish(self, session_id: str, event: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            buf = self._buffer[session_id]
            buf.append((event, payload))
            while len(buf) > MAX_BUFFER_FRAMES:
                buf.pop(0)
            while sum(len(str(p)) for _, p in buf) > MAX_BUFFER_CHARS and len(buf) > 1:
                buf.pop(0)
            subscribers = list(self._subscribers.get(session_id, ()))
        for queue in subscribers:
            queue.put_nowait((event, payload))

    async def finish(self, session_id: str) -> None:
        async with self._lock:
            self._ended.add(session_id)
            subscribers = list(self._subscribers.get(session_id, ()))
        for queue in subscribers:
            queue.put_nowait(None)

    async def subscribe(self, session_id: str):
        """先回放当前缓冲，再持续产出实时帧；结束/断开时退出。"""
        async with self._lock:
            snapshot = list(self._buffer.get(session_id, ()))
            ended = session_id in self._ended
            queue: asyncio.Queue = asyncio.Queue()
            if not ended:
                self._subscribers[session_id].add(queue)
        try:
            for item in snapshot:
                yield item
            if ended:
                return
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item
        finally:
            async with self._lock:
                self._subscribers[session_id].discard(queue)


hub = LiveTurnHub()


__all__ = ["MAX_BUFFER_CHARS", "MAX_BUFFER_FRAMES", "LiveTurnHub", "hub"]
