"""进行中轮次的事件总线：回放 + 实时订阅。"""

from __future__ import annotations

import asyncio

from app.chat.live import LiveTurnHub


async def test_subscriber_gets_replay_then_live() -> None:
    hub = LiveTurnHub()
    await hub.begin("chat_1")
    await hub.publish("chat_1", "delta", {"text": "a"})

    iterator = hub.subscribe("chat_1").__aiter__()
    assert await iterator.__anext__() == ("delta", {"text": "a"})

    await hub.publish("chat_1", "delta", {"text": "b"})
    assert await asyncio.wait_for(iterator.__anext__(), 1) == ("delta", {"text": "b"})
    await hub.finish("chat_1")
    try:
        await asyncio.wait_for(iterator.__anext__(), 1)
    except StopAsyncIteration:
        pass
    else:
        raise AssertionError("finish 后订阅必须结束")
