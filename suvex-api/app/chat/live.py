"""进行中对话的**可重放事件总线**（对齐 OMP 的 resumable stream）。

## 问题

`POST /messages/stream` 的 SSE 只属于发起它的那条 HTTP 请求。刷新页面、
切走再回来、或者从侧栏打开一个正在跑的会话时，前端握不到那条流，只能轮询
已落库的消息 —— 于是正文是「一段一段蹦出来」的，而不是 token 流。

## 做法

本轮所有 `emit` 帧在推给原 SSE 的同时，写入环形缓冲，并广播给
`GET /chat/sessions/{id}/stream` 的订阅者。新页面连上后先回放缓冲，再接实时。

默认是进程内实现（单 worker 足够）。配置 `SUPEX_REDIS_URL` 后切到
Redis 列表 + pubsub：缓冲、序号和结束标记都在 Redis 里，多个 worker 共享。

## 为什么 Redis 帧带序号

订阅者先订阅 pubsub，再读历史列表。两步之间可能有一帧同时进了列表和
pubsub；没有序号就会重复写出一个 token。序号由 Redis `INCR` 生成，
订阅者只接受比已回放最大序号更大的帧。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any

from app.core.config import get_settings

MAX_BUFFER_FRAMES = 4000
MAX_BUFFER_CHARS = 2_000_000
REDIS_TTL_SECONDS = 3600
REDIS_END = "__end__"

_log = logging.getLogger(__name__)


class LiveTurnHub:
    """进程内实现。保留它作为默认后端，也是测试用的确定性后端。"""

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


class RedisLiveTurnHub:
    """Redis 后端：列表存历史，pubsub 推实时帧。

    只按会话存一个列表，键都带 TTL。`begin` 会清掉同会话的旧缓冲，
    防止上一轮已完成的事件被新页面重放。
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._redis: Any = None
        self._lock = asyncio.Lock()

    async def _client(self):
        async with self._lock:
            if self._redis is None:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(self._url, decode_responses=True)
                await self._redis.ping()
            return self._redis

    @staticmethod
    def _keys(session_id: str) -> tuple[str, str, str]:
        base = f"suvex:live:{session_id}"
        return f"{base}:frames", f"{base}:seq", f"{base}:ended"

    async def begin(self, session_id: str) -> None:
        redis = await self._client()
        frames, seq, ended = self._keys(session_id)
        await redis.delete(frames, seq, ended)

    async def publish(self, session_id: str, event: str, payload: dict[str, Any]) -> None:
        redis = await self._client()
        frames, seq_key, _ = self._keys(session_id)
        seq = await redis.incr(seq_key)
        raw = json.dumps([seq, event, payload], ensure_ascii=False, separators=(",", ":"))
        pipe = redis.pipeline()
        pipe.rpush(frames, raw)
        pipe.ltrim(frames, -MAX_BUFFER_FRAMES, -1)
        pipe.expire(frames, REDIS_TTL_SECONDS)
        pipe.expire(seq_key, REDIS_TTL_SECONDS)
        await pipe.execute()
        await redis.publish(f"{frames}:events", raw)

    async def finish(self, session_id: str) -> None:
        redis = await self._client()
        frames, _, ended = self._keys(session_id)
        await redis.set(ended, "1", ex=REDIS_TTL_SECONDS)
        await redis.publish(f"{frames}:events", REDIS_END)

    async def subscribe(self, session_id: str):
        redis = await self._client()
        frames, _, ended_key = self._keys(session_id)
        pubsub = redis.pubsub()
        await pubsub.subscribe(f"{frames}:events")
        try:
            rows = await redis.lrange(frames, 0, -1)
            raw_rows = [json.loads(row) for row in rows]
            last_seq = max((row[0] for row in raw_rows), default=0)
            for _, event, payload in raw_rows:
                yield event, payload
            if await redis.get(ended_key):
                return
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                data = message.get("data")
                if data == REDIS_END:
                    return
                seq, event, payload = json.loads(data)
                if seq <= last_seq:
                    continue
                last_seq = seq
                yield event, payload
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()


def _make_hub() -> LiveTurnHub | RedisLiveTurnHub:
    url = (get_settings().redis_url or "").strip()
    if not url:
        return LiveTurnHub()
    _log.info("对话事件总线使用 Redis：%s", url)
    return RedisLiveTurnHub(url)


hub = _make_hub()


__all__ = [
    "MAX_BUFFER_CHARS",
    "MAX_BUFFER_FRAMES",
    "LiveTurnHub",
    "RedisLiveTurnHub",
    "hub",
]
