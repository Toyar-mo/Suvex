"""对话的流式推送（SSE）—— 让「正在想…」不再是一片空白。

## 这个模块补的是什么

v0.97 之前，`POST /chat/sessions/{id}/messages` 是**整轮一次回包**：
`engine.run_turn()` 里可能有 1 次模型调用 + 最多 6 轮工具往返，
而用户在这几十秒里看到的只有一个转圈。实测的用户原话是
「没有思考过程、工具调用、调用结果的显示」—— 而在同类工具
（claude code / cursor）里，那些是**边跑边出**的。

差别不是好看：`run_turn` 其实**每一步都已落库并 commit**
（user 消息 / assistant 正文 + reasoning / 每条工具卡），
数据一直在产生，只是没人把它送出去。本模块就是那根管子。

## 为什么不用 `app/events` 那套

那套是**活动流**：靠每秒轮询 `activity_events` 表、跨进程、带补发游标。
对话的形态完全不同：

| | 活动流 | 对话流 |
| --- | --- | --- |
| 生命周期 | 常驻订阅 | 与一次请求同生共死 |
| 数据源 | 数据库轮询 | 内存里正在跑的那个 `run_turn` |
| 补发 | 需要（断了要能追） | **不需要**（断了就重新发一句） |

硬塞进那一套会带来两个坏处：为了「补发」得把推理过程也写进
`activity_events`（那是给人看的事件表，会被扫描活动淹没），
以及 1 秒轮询粒度会让工具卡一顿一顿地出现。

所以这里只用它**同一个帧编码约定**（`id:` / `event:` / `data:` 三行），
生成器是自己的。

## ★ 帧里的 `event:` 名字就是界面上的时间线

```
event: phase       {"phase": "thinking", "round": 0}      模型在推理
event: message     {<一条已落库的消息>}                     每落一条推一条
event: awaiting    {"messageId": "..."}                    停下来等人工确认
event: done        {"messageCount": 3}                     本轮结束
event: error       {"message": "..."}                      这一轮失败了
```

★ **`message` 帧推的是「已落库」的消息，不是临时拼的假对象。**
理由：界面上最终显示的就是库里那条（刷新页面还在）。如果流里推一份
临时数据、结束时再换成库里的，就会出现「内容跳一下」；更糟的是
两者可能不一致（比如服务端对正文做了 `_narration_before_action`
裁剪），用户会看到字变了。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from app.events.stream import SSE_HEADERS, SSE_MEDIA_TYPE

#: 帧的类型（= 界面上的时间线节点）。
EVENT_PHASE = "phase"
EVENT_MESSAGE = "message"
EVENT_AWAITING = "awaiting"
EVENT_DONE = "done"
EVENT_ERROR = "error"

#: ★ v0.102：正文的**增量**。`{"text": "..."}` 是要追加到「正在生成那条」
#: 尾巴上的字。
#:
#: ## 为什么在已有 `message` 帧之外还要一个 `delta`
#:
#: `message` 帧推的是**已落库**的整条消息 —— 它是权威的、刷新页面还能看到。
#: 但它是**成块**的：模型想完一整段才有得推，界面上那块文字一瞬间铺开。
#: 用户的原话是「消息是一瞬间蹦出一大块」。
#:
#: 调研三个同类项目（LibreChat / Open WebUI / LobeChat）之后的结论：
#: 它们看起来「逐字长出来」**不是因为入场动画**，是因为正文本来就是
#: 逐 token 到达的。Open WebUI 的 `ResponseMessage.svelte` 里唯一的
#: `in:fade` 用在后继建议 chips 上，消息行本身零入场动画。
#:
#: 所以这里加一条增量通道：`delta` 负责**过程**，`message` 仍是**结果**。
#: 两者的关系是硬不变式：**所有 delta 拼起来（规范化后）是随后那条
#: `message` 帧 content 的前缀**。
#:
#: ## ★ 为什么是「前缀」而不是「相等」
#:
#: 引擎把 `ACTION: {...}` 之前的正文落库（`_narration_before_action`）。
#: delta 通道只推那一部分（`app/chat/holdback.py` 的暂扣缓冲负责），
#: 所以拼起来的 delta 必然落在落库正文之内。
#:
#: 但两处对**代码围栏**的处理无法逐字节相等：落库侧会
#: `.replace("```", "")`（拿到整段才做得对），而实时缓冲做不到 ——
#: 未闭合的 ` ``` ` 只有在流结束后才知道它是不是代码块的开头，而已经
#: emit 出去的帧撤不回来。这个差异用户看不到（围栏是 Markdown 的零宽
#: 结构标记）。所以不变式退到"去掉围栏与空白后为前缀"，由
#: `tests/chat/test_holdback.py` 钉住。
EVENT_DELTA = "delta"

#: 推一帧的回调：`(event_name, payload)`。
#:
#: `engine.run_turn` 拿到的就是它。**它必须是可选的** —— 非流式路径
#: （`POST /messages`）不传，行为逐字节不变。这不是洁癖：
#: 那条路径被审批、重放、测试大量使用，让它依赖一个「必须有人订阅」
#: 的机制会造出一类只在某种调用方式下才出现的挂起。
Emit = Callable[[str, dict[str, Any]], Awaitable[None]]

#: 一轮对话里模型可能想很久。心跳间隔取活动流同一个量级（它用 20 秒）。
#: 太短会在日志里刷屏，太长会让 nginx 把连接判成空闲。
KEEPALIVE_SECONDS = 15.0


def encode_frame(event: str, payload: dict[str, Any], *, event_id: str | None = None) -> str:
    """一帧 SSE。

    ## `ensure_ascii=False` 是必须的

    帧里大量是中文正文与原始工具输出。转成 `\\uXXXX` 会让体积涨约 3 倍，
    而且抓包/日志里完全不可读。SSE 规范本身规定流是 UTF-8。

    ## 为什么 `data` 里不放换行

    SSE 的 `data:` 行遇到换行就断帧 —— 一条多行正文会被解析成多个
    `data:` 行再拼回去，规范上可行，但任何一处手写的解析器都会在这里出错。
    JSON 编码天然没有裸换行（`\\n` 是转义序列），所以直接用 JSON。
    """
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    prefix = f"id: {event_id}\n" if event_id else ""
    return f"{prefix}event: {event}\ndata: {data}\n\n"


def encode_keepalive() -> str:
    """注释行心跳（与活动流同一套约定）。

    ⚠️ 用注释而不是空帧：空帧在部分代理上会被当成「无内容」而继续缓冲，
    注释行是明确有字节但不触发任何事件的。
    """
    return ": keepalive\n\n"


async def stream_turn(
    runner: Callable[[Emit], Awaitable[Any]],
    *,
    to_payload: Callable[[Any], dict[str, Any]],
) -> AsyncIterator[str]:
    """跑一轮对话，把它产出的每一帧实时送出去。

    ## 参数

    `runner` 收一个 `emit` 回调，自己跑完 `run_turn` 并返回结果。
    做成回调注入而不是在这里直接 import engine：这个模块只管「怎么把
    事件变成帧」，不该知道对话引擎长什么样（也就不会与 engine 互相 import
    成环 —— `engine` 要 import 本模块的 `Emit` 类型）。

    `to_payload` 把 `run_turn` 的返回值转成 `done` 帧的载荷。

    ## ★ 为什么要在**任务**里跑，而不是直接 await

    `yield` 必须发生在 `await run_turn(...)` 进行中的时候 —— 否则整轮跑完
    才开始吐帧，与一次回包没有区别。所以 `run_turn` 放进 `asyncio.Task`，
    本函数只负责从队列里取帧并 `yield`。

    ## ★ 异常必须变成 `error` 帧，不能让它抛出去

    响应头在这一轮跑起来之前就已经发出去了（`StreamingResponse` 的语义）。
    此时再抛异常，客户端只会看到「连接被重置」，而**没有任何原因** ——
    它会把网络抖动和「模型接入点失效」当成同一件事，然后无脑重发。
    所以这里捕获一切，翻成一条 `error` 帧。这是本项目一贯的判据：
    失败要带着原因到达人眼前。
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def emit(event: str, payload: dict[str, Any]) -> None:
        await queue.put(encode_frame(event, payload))

    async def run() -> Any:
        try:
            return await runner(emit)
        finally:
            # 无论正常结束还是抛异常，都要让下面的循环退出 ——
            # 少了这一步，异常路径会永远卡在 `queue.get()` 上。
            await queue.put(None)

    task = asyncio.create_task(run())
    try:
        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except TimeoutError:
                # 模型想得久（或某个工具跑得久）时靠心跳占住连接。
                yield encode_keepalive()
                continue
            if frame is None:
                break
            yield frame

        # 循环退出 = runner 已经结束。这里取结果：正常则发 done，异常则发 error。
        try:
            result = await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 见 docstring：必须变成帧
            yield encode_frame(EVENT_ERROR, {"message": f"{type(exc).__name__}: {exc}"})
            return
        yield encode_frame(EVENT_DONE, to_payload(result))
    finally:
        # 客户端断开（用户关页面 / 切会话）时，别让 run_turn 在后台继续烧模型。
        if not task.done():
            task.cancel()


__all__ = [
    "EVENT_AWAITING",
    "EVENT_DELTA",
    "EVENT_DONE",
    "EVENT_ERROR",
    "EVENT_MESSAGE",
    "EVENT_PHASE",
    "Emit",
    "KEEPALIVE_SECONDS",
    "SSE_HEADERS",
    "SSE_MEDIA_TYPE",
    "encode_frame",
    "encode_keepalive",
    "stream_turn",
]
