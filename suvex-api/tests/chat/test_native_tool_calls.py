"""原生工具调用（`tool_calls` / `tool_use`）落到对话页的测试（v1.33）。

## 这个文件守的是什么缺陷

线上会话 `chat_01M6556ES13EGH8NN712252SPB`：本地 OpenAI 兼容网关回的是
**原生工具调用**（`choices[0].delta.tool_calls` + `finish_reason="tool_calls"`，
正文为空）。`stream_delta` 只取 `delta.content`，整段工具调用被丢弃；引擎
拿到 `text='' actions=[] finish='tool_calls'`，进空回复分支后额度耗尽，
在 `already_spoke and not at_limit` 处**静默 return** —— turn_run 正常
`done`，界面不动。用户看到的就是「对话跑到一半就结束」。

这一组用例钉三件事：

1. 两套 wire 形状都能归一化成 `{"index","id","name","arguments"}`；
2. 对话页把归一化结果合成成 `ACTION:` 文本（且**不**泄漏给前端 delta）；
3. 上游只回原生调用、正文为空时，整轮结束时必须有**一条可见消息**，
   不能静默收尾。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from app.chat import engine, service as chat_service
from app.chat.prompts import render_native_tool_note
from app.kernel import runner
from app.kernel.runner import LlmReply, parse_actions
from tests.test_chat import ADMIN


# ══════════════════════════════════════════════════════════
# 夹具：把网络层换成 MockTransport，走**真** LlmGateway
# ══════════════════════════════════════════════════════════


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:  # noqa: ANN001
    """和 `tests/kernel/test_runner.py` 里既有的写法一致。"""
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN001, ANN202
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(runner.httpx, "AsyncClient", factory)


def _sse(payloads: list[dict[str, Any] | str]) -> bytes:
    """把若干 `data:{...}` 拼成一条 SSE 响应体（空行断帧）。"""
    blocks: list[str] = []
    for payload in payloads:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        blocks.append(f"data: {text}\n\n")
    return "".join(blocks).encode()


def _stream_handler(payloads: list[dict[str, Any] | str]):  # noqa: ANN202
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            200,
            content=_sse(payloads),
            headers={"content-type": "text/event-stream"},
        )

    return handler


def _json_handler(body: dict[str, Any]):  # noqa: ANN202
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json=body)

    return handler


def _gateway(*, base_url: str = "https://api.example.com", model: str = "m"):
    return runner.LlmGateway(model=model, api_key="k", base_url=base_url)


class Recorder:
    """收 `emit` 推出去的每一帧。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, event: str, data: dict[str, Any]) -> None:
        self.events.append((event, dict(data)))

    @property
    def deltas(self) -> list[str]:
        return [data.get("text", "") for event, data in self.events if event == "delta"]


#: 真实线上帧形状：正文为空，工具调用分片，最后 `finish_reason="tool_calls"`。
_OPENAI_ONLY_CALL = [
    {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "run_shell", "arguments": '{"comm'},
                        }
                    ]
                }
            }
        ]
    },
    {
        "choices": [
            {
                "delta": {
                    "tool_calls": [{"index": 0, "function": {"arguments": 'and":"id"}'}}]
                }
            }
        ]
    },
    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    "[DONE]",
]


# ══════════════════════════════════════════════════════════
# 1. 流式：只有 delta.tool_calls
# ══════════════════════════════════════════════════════════


async def test_streaming_tool_calls_only_become_parsable_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """线上那条帧：只有 `delta.tool_calls` → 合成 `ACTION:`，可被 parse_actions 解出。"""
    _install_transport(monkeypatch, _stream_handler(_OPENAI_ONLY_CALL))
    emit = Recorder()

    reply = await engine._call_model(
        _gateway(), [{"role": "user", "content": "go"}], system="sys", emit=emit
    )

    actions = parse_actions(reply.text)
    assert actions == [("run_shell", {"command": "id"})]
    # 契约：每一行 ACTION 都以换行开头（HoldbackBuffer 靠行首识别协议）。
    assert reply.text.startswith("\nACTION: ")
    assert reply.finish_reason == "tool_calls"
    assert reply.tool_calls and reply.tool_calls[0]["name"] == "run_shell"


async def test_finish_reason_in_the_same_frame_as_content_is_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一帧既带正文又带 `finish_reason`：收尾原因不能被吞掉。

    部分 OpenAI 兼容网关（以及某些代理）会把 `delta.content` 与
    `finish_reason` 放进同一帧。若按 `delta` 是否为空来门控收尾帧解析，
    这类网关的 `finish_reason` 会静默丢失，然后 `finish_reason == ""`，
    使 v1.13 的截断检测失效（被切断的回复会被当成正常结束）。
    """
    frames = [
        {"choices": [{"delta": {"content": "hello"}, "finish_reason": "length"}]},
        "[DONE]",
    ]
    _install_transport(monkeypatch, _stream_handler(frames))
    emit = Recorder()

    reply = await engine._call_model(
        _gateway(), [{"role": "user", "content": "go"}], system="sys", emit=emit
    )

    assert reply.finish_reason == "length"
    assert reply.text == "hello"


# ══════════════════════════════════════════════════════════
# 2. 非流式：message.content=null + message.tool_calls
# ══════════════════════════════════════════════════════════


async def test_non_streaming_tool_calls_are_carried_and_synthesized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "list_scans",
                                "arguments": '{"limit": 3}',
                            },
                        }
                    ],
                },
            }
        ]
    }
    _install_transport(monkeypatch, _json_handler(body))
    gateway = _gateway()

    direct = await gateway.chat([{"role": "user", "content": "go"}], system="sys")
    assert direct.tool_calls and direct.tool_calls[0]["name"] == "list_scans"
    assert direct.text == ""

    reply = await engine._call_model(
        gateway, [{"role": "user", "content": "go"}], system="sys", emit=None
    )
    assert parse_actions(reply.text) == [("list_scans", {"limit": 3})]


# ══════════════════════════════════════════════════════════
# 3. 正文 + tool_calls 混合
# ══════════════════════════════════════════════════════════


async def test_mixed_text_and_tool_calls_keep_text_then_append_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = [
        {"choices": [{"delta": {"content": "我先看一下。"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_3",
                                "function": {
                                    "name": "list_scans",
                                    "arguments": "{}",
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    ]
    _install_transport(monkeypatch, _stream_handler(frames))

    reply = await engine._call_model(
        _gateway(), [{"role": "user", "content": "go"}], system="sys", emit=Recorder()
    )

    assert reply.text.startswith("我先看一下。\nACTION: ")
    assert parse_actions(reply.text) == [("list_scans", {})]


# ══════════════════════════════════════════════════════════
# 4. 两条并行调用：顺序按 index
# ══════════════════════════════════════════════════════════


async def test_parallel_tool_calls_are_ordered_by_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 帧里故意把 index=1 放在前面：输出必须是 index 升序，不是到达顺序。
    frames = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call_b",
                                "function": {"name": "get_scan", "arguments": "{}"},
                            },
                            {
                                "index": 0,
                                "id": "call_a",
                                "function": {"name": "list_scans", "arguments": "{}"},
                            },
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    ]
    _install_transport(monkeypatch, _stream_handler(frames))

    reply = await engine._call_model(
        _gateway(), [{"role": "user", "content": "go"}], system="sys", emit=Recorder()
    )

    assert parse_actions(reply.text) == [("list_scans", {}), ("get_scan", {})]
    assert [call["index"] for call in reply.tool_calls] == [0, 1]


# ══════════════════════════════════════════════════════════
# 5. arguments 不是合法 JSON
# ══════════════════════════════════════════════════════════


async def test_invalid_arguments_json_falls_back_to_empty_dict_but_keeps_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_bad",
                                "function": {"name": "run_shell", "arguments": "{not json"},
                            },
                            {
                                "index": 1,
                                "id": "call_ok",
                                "function": {"name": "list_scans", "arguments": "{}"},
                            },
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    ]
    _install_transport(monkeypatch, _stream_handler(frames))

    reply = await engine._call_model(
        _gateway(), [{"role": "user", "content": "go"}], system="sys", emit=Recorder()
    )

    assert parse_actions(reply.text) == [("run_shell", {}), ("list_scans", {})]


# ══════════════════════════════════════════════════════════
# 6. Anthropic：content_block_start + 两段 input_json_delta
# ══════════════════════════════════════════════════════════


async def test_anthropic_tool_use_arguments_are_stitched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "run_shell",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"command":'},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": ' "id"}'},
        },
        {"type": "content_block_stop", "index": 0},
    ]
    _install_transport(monkeypatch, _stream_handler(frames))
    gateway = _gateway(base_url="https://api.anthropic.com", model="claude")
    assert gateway.anthropic is True

    reply = await engine._call_model(
        gateway, [{"role": "user", "content": "go"}], system="sys", emit=Recorder()
    )

    assert reply.tool_calls[0]["arguments"] == '{"command": "id"}'
    assert parse_actions(reply.text) == [("run_shell", {"command": "id"})]


# ══════════════════════════════════════════════════════════
# 7. 引擎级：只回原生调用、正文为空 → 必须有可见消息
# ══════════════════════════════════════════════════════════


class _NativeOnlyGateway:
    """每轮都只回「原生工具调用」且正文为空（线上那个形态）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, *, system):  # noqa: ANN001, ANN201, ARG002
        self.calls += 1
        return LlmReply(
            text="",
            source="content",
            raw={},
            usage=None,
            finish_reason="tool_calls",
        )


async def test_engine_never_returns_silently_on_native_only_replies(
    db_session: Session,
) -> None:
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()
    gateway = _NativeOnlyGateway()

    result = await engine.run_turn(
        db_session,
        session=session,
        content="继续解题",
        identity=ADMIN,
        gateway=gateway,
    )

    last = result.messages[-1]
    assert last.role == "assistant"
    assert "原生工具调用" in last.content
    assert render_native_tool_note() in last.content
    assert gateway.calls > 1, "额度内仍应尝试纠偏，而不是第一次空回复就收尾"


# ══════════════════════════════════════════════════════════
# 8. 泄漏守卫：合成的 ACTION 不进前端 delta
# ══════════════════════════════════════════════════════════


async def test_synthesized_action_text_never_leaks_into_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_transport(monkeypatch, _stream_handler(_OPENAI_ONLY_CALL))
    emit = Recorder()

    reply = await engine._call_model(
        _gateway(), [{"role": "user", "content": "go"}], system="sys", emit=emit
    )

    assert "ACTION:" in reply.text
    assert all("ACTION:" not in piece for piece in emit.deltas)

# ══════════════════════════════════════════════════════════
# 9. 纯函数：认不出的形状一律返回 []，绝不抛
# ══════════════════════════════════════════════════════════


def test_tool_call_delta_parsers_never_throw_on_unknown_shapes() -> None:
    from app.providers import llm

    assert llm._json_text(None) == ""
    assert llm._json_text("x") == "x"
    assert llm._json_text({}) == ""
    assert json.loads(llm._json_text({"a": 1})) == {"a": 1}

    openai = False
    assert llm.tool_calls_delta(openai, None, "") == []
    assert llm.tool_calls_delta(openai, None, "[DONE]") == []
    assert llm.tool_calls_delta(openai, None, "{not json") == []
    assert llm.tool_calls_delta(openai, None, "[]") == []
    assert llm.tool_calls_delta(openai, None, '{"choices": []}') == []
    assert llm.tool_calls_delta(openai, None, '{"choices": ["x"]}') == []
    assert llm.tool_calls_delta(openai, None, '{"choices": [{"delta": "x"}]}') == []
    assert (
        llm.tool_calls_delta(
            openai, None, '{"choices": [{"delta": {"tool_calls": "x"}}]}'
        )
        == []
    )
    # 非 dict 项跳过；全空的调用项也跳过。
    assert (
        llm.tool_calls_delta(
            openai,
            None,
            json.dumps({"choices": [{"delta": {"tool_calls": [1, {}]}}]}),
        )
        == []
    )

    anthropic = True
    # content_block_start 但不是 tool_use；content_block_delta 但不是参数片段。
    assert (
        llm.tool_calls_delta(
            anthropic, None, '{"type": "content_block_start", "content_block": {"type": "text"}}'
        )
        == []
    )
    assert (
        llm.tool_calls_delta(
            anthropic,
            None,
            '{"type": "content_block_delta", "delta": {"type": "text_delta"}}',
        )
        == []
    )
    # 既没有 type 也没有 event → 认不出。
    assert llm.tool_calls_delta(anthropic, None, "{}") == []


def test_tool_calls_from_message_never_throws_and_reads_anthropic() -> None:
    from app.providers import llm

    assert llm.tool_calls_from_message(False, None) == []
    assert llm.tool_calls_from_message(True, {"content": "x"}) == []
    assert llm.tool_calls_from_message(False, {"choices": []}) == []
    assert llm.tool_calls_from_message(False, {"choices": ["x"]}) == []
    assert llm.tool_calls_from_message(False, {"choices": [{"message": None}]}) == []
    assert (
        llm.tool_calls_from_message(
            False, {"choices": [{"message": {"tool_calls": "x"}}]}
        )
        == []
    )
    # 非 dict 项跳过，后面的照常产出。
    assert llm.tool_calls_from_message(
        False,
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            None,
                            {"function": {"name": "run_shell", "arguments": "{}"}},
                        ]
                    }
                }
            ]
        },
    ) == [{"index": 1, "id": "", "name": "run_shell", "arguments": "{}"}]

    # Anthropic 非流式：tool_use 块 → arguments 是 input 的 JSON。
    assert llm.tool_calls_from_message(
        True,
        {
            "content": [
                {"type": "text", "text": "..."},
                {
                    "type": "tool_use",
                    "id": "toolu_9",
                    "name": "run_shell",
                    "input": {"command": "id"},
                },
            ]
        },
    ) == [
        {
            "index": 1,
            "id": "toolu_9",
            "name": "run_shell",
            "arguments": '{"command": "id"}',
        }
    ]


async def test_anthropic_complete_input_is_a_terminal_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`content_block_start` 直接给了完整 `input` 时，后续片段不能再拼。"""
    frames = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "run_shell",
                "input": {"command": "id"},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"command":"whoami"}'},
        },
        {"type": "content_block_stop", "index": 0},
    ]
    _install_transport(monkeypatch, _stream_handler(frames))
    gateway = _gateway(base_url="https://api.anthropic.com", model="claude")

    reply = await engine._call_model(
        gateway, [{"role": "user", "content": "go"}], system="sys", emit=Recorder()
    )

    assert parse_actions(reply.text) == [("run_shell", {"command": "id"})]


# ══════════════════════════════════════════════════════════
# 10. 合成器的边界：上限、内层包装、已有标记、无名调用
# ══════════════════════════════════════════════════════════


def _native_reply(text: str, calls: list[dict]) -> LlmReply:
    return LlmReply(
        text=text,
        source="content",
        raw={},
        usage=None,
        finish_reason="tool_calls",
        tool_calls=tuple(calls),
    )


def test_synthesis_caps_at_max_actions_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = [{"name": f"tool_{i}", "arguments": "{}"} for i in range(7)]

    with caplog.at_level("WARNING"):
        reply = engine._with_native_actions(_native_reply("", calls))

    assert reply.text.count("ACTION:") == engine.MAX_SYNTH_ACTIONS
    assert "只保留前" in caplog.text


def test_synthesis_unwraps_a_whole_action_object_in_arguments() -> None:
    reply = engine._with_native_actions(
        _native_reply(
            "",
            [{"name": "x", "arguments": json.dumps({"tool": "run_shell", "args": {"command": "id"}})}],
        )
    )

    assert parse_actions(reply.text) == [("run_shell", {"command": "id"})]


def test_synthesis_skips_unnamed_calls(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        reply = engine._with_native_actions(_native_reply("", [{"name": "", "arguments": "{}"}]))

    assert reply.text == ""
    assert "没有名字" in caplog.text


def test_synthesis_does_not_duplicate_text_the_model_already_wrote_as_protocol() -> None:
    reply = _native_reply(
        'FINAL: 好了。\nACTION: {"tool": "run_shell", "args": {}}',
        [{"name": "run_shell", "arguments": "{}"}],
    )

    assert engine._with_native_actions(reply) is reply


def test_native_names_dedupes_and_supports_tool_calls_detection() -> None:
    reply = _native_reply(
        "",
        [
            {"name": "run_shell"},
            {"name": "run_shell"},
            {"name": "list_scans"},
            {"name": ""},
            "not-a-dict",
        ],
    )

    assert engine._native_names(reply) == ["run_shell", "list_scans"]
    # 不可调用对象拿不到签名 → False（走旧网关的兜底路径）。
    assert engine._supports_tool_calls(object()) is False
    assert engine._supports_tool_calls(lambda **kwargs: None) is True


# ══════════════════════════════════════════════════════════
# 11. 批准后续跑：同样不能静默收尾
# ══════════════════════════════════════════════════════════


async def test_resume_approved_never_returns_silently_on_native_only_replies(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = chat_service.create_session(db_session, created_by="user_admin")
    message = chat_service.append_tool_message(
        db_session,
        session=session,
        tool_name="list_scans",
        tool_args={},
        content="",
    )
    message.approval_status = "approved"
    db_session.commit()

    async def fake_execute(ctx, name, args):  # noqa: ANN001, ANN202, ARG001
        return "scans: 0", False

    monkeypatch.setattr(engine, "_execute_tool", fake_execute)
    gateway = _NativeOnlyGateway()

    result = await engine.resume_approved(
        db_session,
        session=session,
        message=message,
        identity=ADMIN,
        gateway=gateway,
    )

    assert result.messages[-1].content == render_native_tool_note()
    assert gateway.calls > 1


# ══════════════════════════════════════════════════════════
# 12. 空回复 + at_limit 的最后一道日志（不是静默分支）
# ══════════════════════════════════════════════════════════


async def _plain_empty_chat(self, messages, *, system):  # noqa: ANN001, ARG001, ANN201
    return LlmReply(text="", source="content", raw={}, usage=None, finish_reason="")


async def test_giveup_path_still_records_a_placeholder(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()
    monkeypatch.setattr(engine, "MAX_TOOL_ROUNDS", 0)
    gateway = _NativeOnlyGateway()
    gateway.chat = _plain_empty_chat.__get__(gateway)  # type: ignore[method-assign]

    result = await engine.run_turn(
        db_session,
        session=session,
        content="继续",
        identity=ADMIN,
        gateway=gateway,
    )

    assert "没有产出内容" in result.messages[-1].content

