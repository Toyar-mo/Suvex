"""对话流式推送（v0.100）：`emit` 回调 + SSE 帧 + 插话。

## 这一组在钉什么

改造前的体验（用户原话）：「正在想的时候不能插话，没有思考过程、
工具调用、调用结果的显示」。根因是 `run_turn` **整轮一次回包** ——
它其实每一步都落库并 commit，只是没人把数据送出去。

这组测试要钉的**不是"流能不能开"**（那是框架的事），而是三件会
静默失效的事：

1. **帧真的发出去了**，而且顺序与时间线一致（thinking → 消息 → tool → …）。
   漏发一帧不会报错，界面只是"少一块"，而那看起来像模型没做那一步。
2. **推的是已落库的消息**（有 id / seq）。推临时拼的对象会让界面在
   结束时"跳一下"，更糟时与服务端正文不一致（服务端会做
   `_narration_before_action` 裁剪）。
3. **非流式路径逐字节不变**。`emit=None` 时一个 await 都不该做 ——
   那条路径被审批、重放和大量既有测试用着。

## 反面同样重要

只测"传了 emit 会推帧"是不够的：还要测**不传 emit 时不会因为
少了个订阅者而挂起或报错**。那是这类改造最容易引入的回归。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.chat import engine, service as chat_service, stream
from app.chat.stream import (
    EVENT_AWAITING,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_MESSAGE,
    EVENT_PHASE,
    encode_frame,
    encode_keepalive,
    stream_turn,
)
from tests.test_chat import ADMIN, FakeGateway, _FakeReply


class Recorder:
    """收帧的替身。`emit` 就是这个对象的方法。"""

    def __init__(self) -> None:
        self.frames: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, event: str, payload: dict[str, Any]) -> None:
        # 深拷贝一份：engine 不保证不再改那个 dict
        self.frames.append((event, json.loads(json.dumps(payload, default=str))))

    def of(self, event: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.frames if name == event]

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.frames]


@pytest.fixture()
def chat_session(db_session: Session):
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()
    return session


# ══════════════════════════════════════════════════════════
# 1. 帧编码
# ══════════════════════════════════════════════════════════


def test_encode_frame_shape() -> None:
    """一帧是 `event:` + `data:` 两行，以空行结尾（SSE 规范）。"""
    frame = encode_frame("phase", {"phase": "thinking"})
    assert frame.startswith("event: phase\n")
    assert frame.endswith("\n\n")
    assert '"phase":"thinking"' in frame


def test_encode_frame_keeps_chinese_readable() -> None:
    """★ 中文不能被转成 `\\uXXXX`。

    帧里大量是中文正文。转义会让体积涨约 3 倍，而且抓包/日志里
    完全不可读 —— 排查"界面为什么没显示那句话"时，你只能看到
    一屏 `\\u4f60\\u597d`。
    """
    frame = encode_frame("message", {"content": "正在读取目标端口"})
    assert "正在读取目标端口" in frame
    assert "\\u" not in frame


def test_encode_frame_data_is_single_line() -> None:
    """★ `data:` 里不能出现裸换行 —— 多行正文会把一帧拆成多帧。

    这是 SSE 最经典的坑：正文里一个 `\\n` 就让解析器断帧，
    而症状是"消息后半段凭空消失"。
    """
    frame = encode_frame("message", {"content": "第一行\n第二行\n第三行"})
    data_lines = [ln for ln in frame.split("\n") if ln.startswith("data: ")]
    assert len(data_lines) == 1, f"一帧里出现了 {len(data_lines)} 行 data：{frame!r}"


def test_encode_keepalive_is_a_comment() -> None:
    """心跳必须是注释行（`:` 开头），不能是空帧。

    空帧在部分代理上会被当成"无内容"而继续缓冲 —— 那正是活动流
    `encode_open` 那个 22 秒事故的同一种成因。
    """
    frame = encode_keepalive()
    assert frame.startswith(":")
    assert "\n\n" in frame


# ══════════════════════════════════════════════════════════
# 2. `run_turn` 的 emit 接线
# ══════════════════════════════════════════════════════════


async def test_turn_emits_thinking_phase_before_the_model_call(db_session, chat_session):
    """★ 调模型**之前**要有 thinking 帧。

    这一帧就是界面上「正在想…」那一行能显示内容的原因。它在
    `gateway.chat(...)` **之前**发出 —— 否则模型想的那几十秒里
    界面仍然什么都没有，改造就等于没做。
    """
    rec = Recorder()
    gateway = FakeGateway(["你好，我是助手。"])
    await engine.run_turn(
        db_session,
        session=chat_session,
        content="你好",
        identity=ADMIN,
        gateway=gateway,
        emit=rec,
    )

    phases = rec.of(EVENT_PHASE)
    assert phases, "没有任何 phase 帧 —— 界面上仍是一片空白"
    assert phases[0]["phase"] == "thinking", f"第一帧不是 thinking：{phases[0]}"
    assert phases[0]["round"] == 0


async def test_turn_emits_every_persisted_message(db_session, chat_session):
    """★ 每一条落库的消息都要推一帧，且**带 id / seq**。

    带 id 是关键：前端要按 id 去重与定位（否则乐观气泡与真消息会打架）。
    推临时拼的对象通常就没有 id，这条能抓住那种写法。
    """
    rec = Recorder()
    gateway = FakeGateway(["我先看看。"])
    result = await engine.run_turn(
        db_session,
        session=chat_session,
        content="帮我看看",
        identity=ADMIN,
        gateway=gateway,
        emit=rec,
    )

    pushed = rec.of(EVENT_MESSAGE)
    # user + assistant 至少两条
    assert len(pushed) >= 2, f"只推了 {len(pushed)} 条消息帧"
    for payload in pushed:
        assert payload.get("id"), f"消息帧没有 id：{payload}"
        assert isinstance(payload.get("seq"), int), f"消息帧没有 seq：{payload}"
    # 与 run_turn 的返回值应逐条对应
    assert len(pushed) == len(result.messages)


async def test_message_frames_use_camel_case(db_session, chat_session):
    """★ 消息帧必须是 camelCase（与 `ChatMessageOut` 的序列化口径一致）。

    前端按 camelCase 解析。推 snake_case 会让字段全部读不到，
    而表现是「工具卡没有名字」这类奇怪的空值 —— 不像一个协议错误，
    像界面 bug。

    ## ★ 这条断言为什么最后写成了「黑名单检查」而不是断言 `by_alias=True`

    第一版是这么写的，然后变异验证把它证伪了：把 `model_dump(by_alias=True)`
    改成 `model_dump()` 之后**这条测试照样绿**。原因是 `CamelModel` 的
    **字段名本身已经是 camelCase**（`createdAt` 就是字段名，不是 alias），
    所以 `by_alias` 对输出毫无影响 —— 那个变异是空变异，不是测试漏了。

    但这件事暴露了真正的风险形状：**有人不用 schema、手拼一个 dict**。
    那时才会真的冒出 `tool_name` / `created_at` 这类键。

    所以现在断言的是「payload 里不许出现任何 snake_case 形态的键」，
    它对两种写法都会红：手拼 snake_case dict、或换成一个 snake_case 字段的 schema。
    """
    rec = Recorder()
    gateway = FakeGateway(['我调个工具。\nACTION: {"tool": "list_skills", "args": {}}', "好。"])
    await engine.run_turn(
        db_session,
        session=chat_session,
        content="hi",
        identity=ADMIN,
        gateway=gateway,
        emit=rec,
    )
    pushed = rec.of(EVENT_MESSAGE)
    assert pushed, "没有消息帧"

    for payload in pushed:
        snake_keys = [k for k in payload if "_" in k]
        assert not snake_keys, (
            f"消息帧里出现了 snake_case 键 {snake_keys} —— "
            "前端按 camelCase 解析，这些字段会全部读不到"
        )
        assert "createdAt" in payload, f"没有 createdAt：{payload}"


async def test_tool_call_emits_start_phase_then_result(db_session, chat_session):
    """★ 工具**开始执行前**要有一帧，结果落库后还要有一帧。

    只在结束时推结果的话，一个跑几十秒的工具在界面上那段时间是空的 ——
    与改造前的体验一样。
    """
    rec = Recorder()
    gateway = FakeGateway(
        [
            '我先列一下能力。\nACTION: {"tool": "list_skills", "args": {}}',
            "好了，就这些。",
        ]
    )
    await engine.run_turn(
        db_session,
        session=chat_session,
        content="有什么技能",
        identity=ADMIN,
        gateway=gateway,
        emit=rec,
    )
    phases = rec.of(EVENT_PHASE)
    tool_phases = [p for p in phases if p.get("phase") == "tool"]
    assert tool_phases, f"没有任何 tool phase 帧：{phases}"
    assert tool_phases[0].get("tool") == "list_skills"

    # 工具结果也要作为一条消息帧推出（带 toolName）
    pushed = rec.of(EVENT_MESSAGE)
    tool_msgs = [m for m in pushed if m.get("role") == "tool"]
    assert tool_msgs, "工具结果没有作为消息帧推出"
    assert tool_msgs[0].get("toolName") == "list_skills"


async def test_no_emit_means_no_emit_calls_but_still_works(db_session, chat_session):
    """★★ 不传 `emit` 时行为完全照旧 —— 这是最容易被这次改造弄坏的地方。

    非流式路径（`POST /messages`）被审批、重放和大量既有测试用着。
    如果它开始依赖"必须有人订阅"，就会造出一类只在某种调用方式下
    才出现的挂起。
    """
    gateway = FakeGateway(["照旧回答。"])
    result = await engine.run_turn(
        db_session,
        session=chat_session,
        content="你好",
        identity=ADMIN,
        gateway=gateway,
        # 刻意不传 emit
    )
    assert result.messages, "非流式路径没有产出消息"
    assert any(m.role == "assistant" for m in result.messages)


# ══════════════════════════════════════════════════════════
# 3. `stream_turn` 生成器
# ══════════════════════════════════════════════════════════


async def _collect(gen) -> list[str]:
    return [frame async for frame in gen]


async def test_stream_turn_forwards_frames_and_ends_with_done() -> None:
    """生成器要把 emit 的帧原样送出，并以 `done` 收尾。"""

    async def runner(emit):
        await emit(EVENT_PHASE, {"phase": "thinking", "round": 0})
        await emit(EVENT_MESSAGE, {"id": "m1", "seq": 1})
        return {"n": 1}

    frames = await _collect(stream_turn(runner, to_payload=lambda r: {"messageCount": r["n"]}))
    body = "".join(frames)
    assert "event: phase" in body
    assert "event: message" in body
    assert "event: done" in body
    assert '"messageCount":1' in body


async def test_stream_turn_turns_exceptions_into_error_frames() -> None:
    """★ 异常必须变成 `error` 帧，不能抛出去。

    响应头在这一轮跑起来之前就发出去了。此时再抛异常，客户端只会看到
    「连接被重置」而**没有任何原因** —— 它会把网络抖动和「模型接入点
    失效」当成同一件事，然后无脑重发（再烧一次 token）。
    """
    async def runner(emit):
        await emit(EVENT_PHASE, {"phase": "thinking", "round": 0})
        raise RuntimeError("模型接入点 401")

    frames = await _collect(stream_turn(runner, to_payload=lambda r: {}))
    body = "".join(frames)
    assert "event: error" in body
    assert "模型接入点 401" in body
    # 出错时不该再发 done（前端据此判"这一轮没成功"）
    assert "event: done" not in body


async def test_stream_turn_prints_error_type_name() -> None:
    """error 帧要带异常**类型名** —— 只有 message 时看不出是哪种失败。"""

    async def runner(emit):
        raise ValueError("参数不对")

    body = "".join(await _collect(stream_turn(runner, to_payload=lambda r: {})))
    assert "ValueError" in body


# ══════════════════════════════════════════════════════════
# 4. 插话
# ══════════════════════════════════════════════════════════


async def test_interjection_is_seen_by_the_next_round(db_session, chat_session):
    """★ 插话的核心机制：每一轮开头都重读历史，所以写进库就会被看到。

    ## 这条测的是**机制**，不是端点

    端点是薄薄一层（校验 + `append_user_message`）。真正要证明的是
    「在 run_turn 跑的过程中写一条 user 消息，下一轮模型能看到它」——
    因为整个"插话"功能就建立在这个性质上。

    所以这里手工模拟：第一轮模型调用时写一条消息进去，
    然后检查**第二次** `gateway.chat` 收到的 messages 里有没有它。
    """
    gateway = FakeGateway(
        [
            '我先查一下。\nACTION: {"tool": "list_skills", "args": {}}',
            "好的，我看到你补的话了。",
        ]
    )

    class InterjectingGateway:
        """第一轮时模拟"用户插话"，然后转发给真剧本。"""

        def __init__(self, inner: FakeGateway) -> None:
            self.inner = inner
            self.calls = 0
            self.seen: list[list[dict[str, str]]] = []

        async def chat(self, messages, *, system):  # noqa: ANN001, ANN201
            self.calls += 1
            self.seen.append([dict(m) for m in messages])
            if self.calls == 1:
                # 模拟用户在模型跑的时候插了一句
                chat_service.append_user_message(
                    db_session, session=chat_session, content="顺便看一下 nmap"
                )
                db_session.commit()
            return await self.inner.chat(messages, system=system)

    gw = InterjectingGateway(gateway)
    await engine.run_turn(
        db_session,
        session=chat_session,
        content="有什么技能",
        identity=ADMIN,
        gateway=gw,
    )

    assert gw.calls >= 2, "只调了一次模型，测不到'下一轮'"
    second = gw.seen[1]
    joined = "\n".join(m.get("content", "") for m in second)
    assert "顺便看一下 nmap" in joined, (
        "插话没有出现在下一轮的上下文里 —— "
        "「插话」这个功能就建立在这个性质上，它坏了用户的话等于白说"
    )


def test_interject_endpoint_persists_a_user_message(db_session, chat_session):
    """端点把话写进库，并返回那条消息（带 id / seq）。"""
    from app.chat.router import interject
    from app.chat.schemas import ChatInterjectIn

    out = interject(
        chat_session.id,
        ChatInterjectIn(content="插一句"),
        db_session,
        ADMIN,
    )
    assert out.role == "user"
    assert out.content == "插一句"
    assert out.id and out.seq

    rows = chat_service.list_messages(db_session, session=chat_session)
    assert any(r.content == "插一句" for r in rows)


def test_interject_rejects_blank(db_session, chat_session):
    """空白插话要被拒 —— 一条只有空格的消息会让模型不知道在回什么。"""
    from app.chat.router import interject
    from app.chat.schemas import ChatInterjectIn
    from app.core.errors import ApiError

    with pytest.raises(ApiError):
        interject(chat_session.id, ChatInterjectIn(content="   "), db_session, ADMIN)
