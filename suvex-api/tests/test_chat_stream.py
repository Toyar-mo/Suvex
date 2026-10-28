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
    """端点把话写进库，并返回那条消息（带 id / seq）。

    ★ v1.24：必须先有一轮在跑 —— 插话的语义就是"塞给正在跑的那一轮"。
      没有在跑的轮次时端点会 409 `NO_RUNNING_TURN`（见下一条用例）。
    """
    from app.chat import turn_runs
    from app.chat.router import interject
    from app.chat.schemas import ChatInterjectIn

    run = turn_runs.start(db_session, session_id=chat_session.id)
    db_session.commit()

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

    turn_runs.finish(db_session, run, status="done")
    db_session.commit()


def test_interject_without_a_running_turn_is_rejected(db_session, chat_session):
    """★ v1.24：没有在跑的轮次时必须拒绝，而不是默默写一条没人回答的消息。

    实测事故：会话 A 的长任务在跑，用户切到新建的会话 B 打「你好」按 Enter
    → 旧逻辑落了一条 user 消息，而 B 里没有轮次会去读历史 → 那句话永远
    没有回复，界面停在「插话 · 模型下一轮会看到」+ 永不消失的「模型在推理…」。
    """
    from app.chat.router import interject
    from app.chat.schemas import ChatInterjectIn
    from app.core.errors import ApiError

    with pytest.raises(ApiError) as caught:
        interject(chat_session.id, ChatInterjectIn(content="你好"), db_session, ADMIN)

    assert getattr(caught.value, "code", "") == "NO_RUNNING_TURN"
    # 关键：不要留下一条无人回答的消息
    rows = chat_service.list_messages(db_session, session=chat_session)
    assert not any(r.content == "你好" for r in rows)


def test_interject_rejects_blank(db_session, chat_session):
    """空白插话要被拒 —— 一条只有空格的消息会让模型不知道在回什么。"""
    from app.chat.router import interject
    from app.chat.schemas import ChatInterjectIn
    from app.core.errors import ApiError

    with pytest.raises(ApiError):
        interject(chat_session.id, ChatInterjectIn(content="   "), db_session, ADMIN)


# ══════════════════════════════════════════════════════════════════════
# v1.13：流式路径同样要把"被切断"带出来
# ══════════════════════════════════════════════════════════════════════
#
# ## 为什么这条必须单独有
#
# `engine._call_model` 有两条分支：`emit is None` 走 `gateway.chat()`，
# 否则走 `gateway.chat_stream()`。**对话页永远走后者**（它是流式的），
# 而 `chat_stream` 原来只 `yield` 正文 —— 收尾帧里那句
# `finish_reason: "length"` 被丢掉了，于是线上那条路径**永远**收不到
# 截断信号。`FakeGateway` 只实现 `chat()`，所以上面 `test_chat.py`
# 那一组全绿也证明不了流式路径是对的 —— 它走的根本不是那条路径。
#
# 这两条测试因此改成走 `emit=Recorder()`，逼 `_call_model` 进流式分支。


class _StreamingGateway:
    """只实现 `chat_stream` 的假模型（对话页真实走的那条路径）。

    ⚠️ **故意不实现 `chat()`**：`_call_model` 的 `except` 里有一句
      "流式失败就退回非流式"。如果这个假网关两个都实现了，一次流式
      断言失败会被那句兜底悄悄掩盖成"非流式成功了"，测试变成假绿。
      让它只有 `chat_stream`，兜底路径一走就 AttributeError，立刻暴露。
    """

    def __init__(self, chunks: list[list[str]], *, finish_reason: str = "") -> None:
        self.chunks = list(chunks)
        self.finish_reason = finish_reason
        self.seen: list[list[dict[str, str]]] = []

    async def chat_stream(self, messages, *, system, finish_reason=None):  # noqa: ANN001
        self.seen.append([dict(m) for m in messages])
        pieces = self.chunks.pop(0) if self.chunks else []
        for piece in pieces:
            yield piece
        # 与真网关同形：收尾帧不产正文，只把取值 append 进调用方给的 list。
        # ⚠️ 必须**只在** finish_reason 非空时 append —— 镜像
        #   `stream_finish_reason` 取不到返回空串的纪律（空串不 append，
        #   否则 `_call_model` 的 `finish_reasons[-1]` 会拿到一个空串，
        #   而那会把"最后一帧的真实取值"冲掉）。
        if finish_reason is not None and self.finish_reason:
            finish_reason.append(self.finish_reason)


async def test_the_streaming_path_carries_the_truncation_signal(
    db_session: Session, chat_session
) -> None:
    """流式分支必须把 `finish_reason` 带进 `LlmReply`（v1.13）。

    ★ 改造前 `_call_model` 的流式收尾是硬编码 `finish_reason=""`，
      于是**线上那条路径永远判不出截断**。`FakeGateway`（只实现 `chat`）
      测不到这一点 —— 它那条路是非流式的。
    """
    gateway = _StreamingGateway(
        [["我先看一下抓包的整体情况，按协议分层过一遍"]], finish_reason="length"
    )
    recorder = Recorder()

    reply = await engine._call_model(
        gateway, [{"role": "user", "content": "看看这个包"}], system="", emit=recorder
    )

    assert reply.text == "我先看一下抓包的整体情况，按协议分层过一遍"
    assert reply.finish_reason == "length", "流式路径又丢掉了截断信号"


async def test_a_streaming_truncated_reply_gets_the_truncation_reminder(
    db_session: Session, chat_session
) -> None:
    """端到端：流式被切断 → 喂的是截断提醒，不是格式提醒。

    这条把"信号带出来了"与"信号真的被用来选提醒语"串在一起 ——
    只测前者的话，`_is_truncated` 那侧写错了也发现不了。
    """
    gateway = _StreamingGateway(
        [
            ["我先看一下情况"],
            ["结论：这个包是 USB 抓包。"],
        ],
        finish_reason="length",
    )
    recorder = Recorder()

    await engine.run_turn(
        db_session,
        session=chat_session,
        content="分析这个包",
        identity=ADMIN,
        gateway=gateway,
        emit=recorder,
    )

    # ★ v1.19：`truncated` 是硬信号，被切断的回复每次都被推 —— 这个夹具
    #   每条回复都声称被切断，所以轮数 >= 2。要守的是第一次纠偏喂的是
    #   **截断**那句（不是格式那句）。
    assert len(gateway.seen) >= 2, "被切断的回复没有被推"
    fed = [m.get("content") or "" for m in gateway.seen[1]]
    assert any("达到输出长度上限被切断" in c for c in fed)


# ══════════════════════════════════════════════════════════════════════
# v1.14：自动纠偏续跑要推一帧 `phase: resume`
# ══════════════════════════════════════════════════════════════════════
#
# ## 为什么这条不是"顺手加个提示"就够
#
# 纠偏逻辑（v1.13）本身是**静默**的：模型这一轮没写动作，服务端悄悄
# 再问它一次，界面上的「正在想…」就那么停着，然后凭空蹦出下一段。
# 用户无法区分两件事：
#
#   · 它在自动重试 —— 该等；
#   · 它卡死了 —— 该刷新重发。
#
# 这两件事的动作**相反**，所以"看不出来"本身就是一个缺陷。
# CyberStrikeAI 在同一位置推 `finalization_auto_continue` 事件
# （"最终回复检查尚未收敛，正在基于已有轨迹继续执行…"）。
#
# ## 边界：它是 phase，不是 message
#
# 它**不能**走 `message` 帧。`message` 帧推的都是已落库的行，刷新还在；
# 而一次成功的纠偏在库里不留任何痕迹。用 message 帧会造出一条刷新就
# 消失的幽灵消息 —— 那比不推更糟。下面 `test_the_resume_frame_is_not_
# a_persisted_message` 专门钉这一点。


def _resume_phases(rec: "Recorder") -> list[dict[str, Any]]:
    return [p for p in rec.of(EVENT_PHASE) if p.get("phase") == "resume"]


async def test_a_format_nudge_emits_a_visible_resume_phase(
    db_session: Session, chat_session
) -> None:
    """叙述了要动手但没写 ACTION → 续跑，且中途推了一帧 `resume`。

    没有这一帧，用户看到的就是"正在想…"停住几秒后突然出现下一段。
    """
    rec = Recorder()
    gateway = FakeGateway(
        [
            "我先看一下最近的扫描。",
            "看完了：一共三条扫描，都在运行。",
        ]
    )

    await engine.run_turn(
        db_session,
        session=chat_session,
        content="看看有哪些扫描",
        identity=ADMIN,
        gateway=gateway,
        emit=rec,
    )

    # 模型被调了两次 = 被纠偏了一次。⚠️ 不能只断言"有 resume 帧"：
    # 如果判据误伤成"每轮都推"，seen 会是 3（推满 MAX_NARRATION_NUDGES）,
    # 那一轮 resume 帧也会存在，测试却绿了。
    assert len(gateway.seen) == 2, f"纠偏次数不对：{len(gateway.seen)}"
    resumes = _resume_phases(rec)
    assert len(resumes) == 1, f"没有推 resume 帧：{rec.of(EVENT_PHASE)}"
    assert resumes[0]["reason"] == "format"
    assert resumes[0]["attempt"] == 1
    assert resumes[0]["maxAttempts"] == engine.MAX_NARRATION_NUDGES
    # round 必须是**被纠偏那一轮**的索引，不是下一轮 —— 前端拿它算「第几轮」。
    assert isinstance(resumes[0]["round"], int)


async def test_a_truncated_streaming_nudge_says_truncation_not_format(
    db_session: Session, chat_session
) -> None:
    """被输出上限切断时，`resume` 帧的 `reason` 必须是 `truncation`。

    ★ 这一条与上一条**不能合并**：两种病因喂给模型的提醒语不同，界面文案
      也不同（"接着写" vs "把话说完"）。`reason` 传错了，模型拿到的提醒
      语也就选错了 —— 那正是 v1.13 修的那个坑的翻版。
    """
    gateway = _StreamingGateway(
        [
            ["我先看一下抓包的整体情况，按协议分层过一遍"],
            ["结论：这是一个 USB 抓包。"],
        ],
        finish_reason="length",
    )
    rec = Recorder()

    await engine.run_turn(
        db_session,
        session=chat_session,
        content="分析这个包",
        identity=ADMIN,
        gateway=gateway,
        emit=rec,
    )

    resumes = _resume_phases(rec)
    # ★ v1.19：被切断的回复**每次**都会被推（`nudge.should_nudge` 里
    #   `truncated` 是硬信号），所以这个夹具"每条回复都声称被切断"时会推
    #   两次而不是一次。这里断言的不变量是**原因标签**：只要推了，就必须
    #   报 truncation，绝不能报成 format —— 界面文案据此区分
    #   「在让它把话说完」与「在让它按格式重写」。
    assert resumes, f"没有推 resume 帧：{rec.of(EVENT_PHASE)}"
    assert {r["reason"] for r in resumes} == {"truncation"}, resumes
    assert resumes[0]["reason"] == "truncation", (
        "被切断却报了 format —— 界面会说「正在让它把话说完」而不是「被截断」"
    )


async def test_the_resume_frame_is_not_a_persisted_message(
    db_session: Session, chat_session
) -> None:
    """纠偏帧**不落库**，也不能凭空多出一条消息。

    ★ 这是 `phase` 与 `message` 的分界线。如果哪天有人把纠偏改成落一条
      "正在重试…"的 assistant 消息，用户刷新页面就会看到一条他从未见过、
      也永远不该存在的消息。这条测试钉住：纠偏前后库里只有两条用户
      能看到的消息（用户问 + 模型答案）。
    """
    rec = Recorder()
    gateway = FakeGateway(
        [
            "我先看一下最近的扫描。",
            "看完了：一共三条扫描，都在运行。",
        ]
    )

    await engine.run_turn(
        db_session,
        session=chat_session,
        content="看看有哪些扫描",
        identity=ADMIN,
        gateway=gateway,
        emit=rec,
    )

    rows = chat_service.list_messages(db_session, session=chat_session)
    contents = [r.content for r in rows]
    assert "看看有哪些扫描" in contents
    # 纠偏产生的中间轮**没有正文**，所以不该落任何行；落了的只能是
    # 那条 final 答案（这里 FakeGateway 第二轮的正文被 ACTION 裁剪掉，
    # 所以答案正文可能为空 —— 但绝不该出现"正在重试"这类文案）。
    assert not any("重试" in c or "续跑" in c for c in contents), (
        f"纠偏帧被落库成了消息：{contents}"
    )
    # 消息帧的数量必须等于库里的行数（推了什么就是库里有什么）。
    assert len(rec.of(EVENT_MESSAGE)) == len(rows)
