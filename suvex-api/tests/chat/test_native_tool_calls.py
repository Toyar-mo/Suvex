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

import contextlib
import json
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from app.chat import engine
from app.chat import service as chat_service
from app.chat.prompts import build_tools_schema, render_native_tool_note
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



# ══════════════════════════════════════════════════════════
# 13. v1.64：真正地把 tools 定义发给接入点（而不只是会解析回来的调用）
# ══════════════════════════════════════════════════════════
#
# 上面 §1-12 证明的是"接入点回原生调用 → 我们合成 ACTION 文本 → 解析执行"
# 这条**接收侧**链路。v1.64 补的是**发送侧**：把工具清单作为 `tools=` 发给
# 接入点，让模型一开始就用结构化调用，而不是被提示词教着写 `ACTION:` 文本。
# 少了这一半，模型照样能写文本协议、照样能发明 `<result_note>` 这类新标签
# —— 那正是要根治的 bug 来源。


def test_build_tools_schema_covers_every_chat_tool_in_order() -> None:
    """`build_tools_schema()` 与 `CHAT_TOOLS` 一一对应、顺序一致、键名齐全。"""
    from app.chat.prompts import CHAT_TOOLS, build_tools_schema

    schema = build_tools_schema()
    assert [t["function"]["name"] for t in schema] == [t.name for t in CHAT_TOOLS]
    for entry in schema:
        assert entry["type"] == "function"
        fn = entry["function"]
        assert fn["name"] and fn["description"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)
        assert isinstance(params["required"], list)
        # required 里的键必须真的在 properties 里（否则模型永远填不对）。
        for key in params["required"]:
            assert key in params["properties"], f"{fn['name']}.{key} 不在 properties"


def _capture_body(handler_sink: dict[str, Any]):  # noqa: ANN202
    """构造一个 handler，把收到的 request body 记进 `handler_sink`。"""

    def handler(request: httpx.Request) -> httpx.Response:
        handler_sink["body"] = json.loads(request.content.decode())
        handler_sink["headers"] = dict(request.headers)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    return handler


async def test_openai_body_includes_tools_when_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink: dict[str, Any] = {}
    _install_transport(monkeypatch, _capture_body(sink))
    gateway = _gateway()  # OpenAI 形态（非 anthropic）

    await gateway.chat(
        [{"role": "user", "content": "go"}],
        system="sys",
        tools=[{"type": "function", "function": {"name": "run_shell", "description": "d", "parameters": {"type": "object", "properties": {}, "required": []}}}],
    )

    body = sink["body"]
    assert body["tools"][0]["function"]["name"] == "run_shell"
    assert body["tool_choice"] == "auto"


async def test_body_omits_tools_when_not_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink: dict[str, Any] = {}
    _install_transport(monkeypatch, _capture_body(sink))
    gateway = _gateway()

    await gateway.chat([{"role": "user", "content": "go"}], system="sys")

    assert "tools" not in sink["body"]
    assert "tool_choice" not in sink["body"]


async def test_anthropic_body_converts_tools_to_input_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink: dict[str, Any] = {}
    _install_transport(monkeypatch, _capture_body(sink))
    gateway = _gateway(base_url="https://api.anthropic.com", model="claude")
    assert gateway.anthropic is True

    await gateway.chat(
        [{"role": "user", "content": "go"}],
        system="sys",
        tools=[{"type": "function", "function": {"name": "run_shell", "description": "d", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}],
    )

    tools = sink["body"]["tools"]
    # Anthropic 形态：没有 `function` 外壳，参数键叫 input_schema。
    assert tools[0]["name"] == "run_shell"
    assert "input_schema" in tools[0]
    assert "function" not in tools[0]
    assert tools[0]["input_schema"]["required"] == ["command"]


async def test_call_model_forwards_tools_only_when_gateway_supports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧网关（chat 不收 tools）不该被硬塞 kwargs 而 TypeError。"""
    received: dict[str, Any] = {}

    class _OldGateway:
        async def chat(self, messages, *, system):  # noqa: ANN001, ANN201, ARG002
            received["called"] = True
            return LlmReply(text="ok", source="content", raw={}, usage=None, finish_reason="stop")

    # _gateway_chat 探测 chat() 签名里没有 tools → 不传，正常返回。
    reply = await engine._gateway_chat(
        _OldGateway(),
        [{"role": "user", "content": "go"}],
        system="sys",
        tools=[{"type": "function", "function": {"name": "x"}}],
    )
    assert received.get("called") is True
    assert reply.text == "ok"


def test_supports_param_reads_signature_and_kwargs() -> None:
    class _Has:
        async def chat(self, messages, *, system, tools=None): ...  # noqa: ANN001

    class _Kw:
        async def chat(self, messages, *, system, **kwargs): ...  # noqa: ANN001

    class _None:
        async def chat(self, messages, *, system): ...  # noqa: ANN001

    assert engine._supports_param(_Has().chat, "tools") is True
    assert engine._supports_param(_Kw().chat, "tools") is True
    assert engine._supports_param(_None().chat, "tools") is False
    assert engine._supports_param(object(), "tools") is False


# ══════════════════════════════════════════════════════════
# 14. v1.64 stage2：系统提示词按「接入点是否支持原生调用」切换协议
# ══════════════════════════════════════════════════════════


def test_prompt_text_mode_teaches_action_protocol() -> None:
    from app.chat.prompts import build_system_prompt

    prompt = build_system_prompt()  # 默认文本协议
    assert 'ACTION: {"tool"' in prompt and "FINAL:" in prompt
    # 内部替换用的 `####` 分隔标记绝不能漏给模型。
    assert "####" not in prompt


def test_prompt_native_mode_drops_action_protocol_and_uses_function_calling() -> None:
    from app.chat.prompts import build_system_prompt

    prompt = build_system_prompt(native_tools=True)
    assert "####" not in prompt
    # 不再教它手写 ACTION JSON，而是说工具已作为原生函数提供。
    assert 'ACTION: {"tool"' not in prompt
    assert "原生函数" in prompt or "function calling" in prompt
    # 工具清单仍在（两种模式都要能看到有哪些工具）。
    assert "## 你的工具清单" in prompt


def test_gateway_supports_tools_detects_stream_and_chat() -> None:
    class _StreamOnly:
        async def chat_stream(self, messages, *, system, tools=None): ...  # noqa: ANN001

    class _ChatOnly:
        async def chat(self, messages, *, system, tools=None): ...  # noqa: ANN001

    class _None:
        async def chat(self, messages, *, system): ...  # noqa: ANN001

        async def chat_stream(self, messages, *, system): ...  # noqa: ANN001

    assert engine._gateway_supports_tools(_StreamOnly()) is True
    assert engine._gateway_supports_tools(_ChatOnly()) is True
    assert engine._gateway_supports_tools(_None()) is False


async def test_run_turn_uses_native_prompt_and_passes_tools_when_gateway_supports_it(
    db_session: Session,
) -> None:
    """支持 tools 的网关：系统提示走原生版、请求体带 tools。"""
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()

    seen: dict[str, Any] = {}

    class _NativeGateway:
        async def chat(self, messages, *, system, tools=None):  # noqa: ANN001, ANN201
            seen["has_native_prompt"] = "原生函数" in system or "function calling" in system
            seen["tools_len"] = len(tools or [])
            return LlmReply(text="好的，已完成。", source="content", raw={}, usage=None, finish_reason="stop")

    await engine.run_turn(
        db_session,
        session=session,
        content="你好",
        identity=ADMIN,
        gateway=_NativeGateway(),
    )

    assert seen.get("has_native_prompt") is True
    assert seen.get("tools_len", 0) == len(build_tools_schema())


async def test_run_turn_keeps_text_protocol_for_legacy_gateway(
    db_session: Session,
) -> None:
    """旧网关（不收 tools）：系统提示仍是文本协议版、且不会被塞 tools。"""
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()

    seen: dict[str, Any] = {}

    class _LegacyGateway:
        async def chat(self, messages, *, system):  # noqa: ANN001, ANN201
            seen["has_text_protocol"] = 'ACTION: {"tool"' in system
            return LlmReply(text="FINAL: 完成。", source="content", raw={}, usage=None, finish_reason="stop")

    with contextlib.suppress(Exception):  # noqa: BLE001 - 只关心提示词构建，不关心收尾
        await engine.run_turn(
            db_session, session=session, content="你好", identity=ADMIN, gateway=_LegacyGateway()
        )

    assert seen.get("has_text_protocol") is True


# ══════════════════════════════════════════════════════════
# 15. v1.64.1：模型干完活只写正文（不调工具）时不再无限推
# ══════════════════════════════════════════════════════════


def test_should_nudge_stops_when_no_tool_for_many_rounds() -> None:
    """跑过工具、但连续 5 轮只写正文不动手 → 认定作答，不再推。"""
    from app.chat import nudge as np

    base = dict(
        at_limit=False,
        said_final=False,
        actions=[],
        truncated=False,
        tools_used=2,
        text="两步都完成了：hello.txt 已创建（30 字节），cat 读回内容原样是 原生工具调用测试成功。",
        nudges_used=0,
        max_nudges=2,
        tool_nudges_used=3,  # 远未到 300 上限
    )
    # 连续没动工具 5 轮 → 不推
    assert np.should_nudge(**base, no_tool_rounds=5, max_no_tool_rounds=5) is False
    # 才第 1 轮没动工具 → 仍然推（给"工具返回后想再确认一下"留余地）
    assert np.should_nudge(**base, no_tool_rounds=1, max_no_tool_rounds=5) is True
    # 第 4 轮 → 还没到上限，仍推
    assert np.should_nudge(**base, no_tool_rounds=4, max_no_tool_rounds=5) is True


def test_should_nudge_still_nudges_unactioned_command_even_past_no_tool_cap() -> None:
    """正文里夹着要跑的 shell 命令 → 无论连续多少轮没动手都要推（还想动手）。"""
    from app.chat import nudge as np

    assert (
        np.should_nudge(
            at_limit=False,
            said_final=False,
            actions=[],
            truncated=False,
            tools_used=2,
            text="接下来我跑一下：\n```\ncat hello.txt\n```",
            nudges_used=0,
            max_nudges=2,
            tool_nudges_used=3,
            no_tool_rounds=99,
            max_no_tool_rounds=5,
        )
        is True
    )


def test_should_nudge_no_tool_cap_does_not_affect_when_no_tools_used() -> None:
    """从没跑过工具（纯问答）→ 走 narration 通道，不受 no_tool 计数影响。"""
    from app.chat import nudge as np

    # 纯问答、完整回答、没有意图词 → looks_unfinished=False → 不推
    assert (
        np.should_nudge(
            at_limit=False,
            said_final=False,
            actions=[],
            truncated=False,
            tools_used=0,
            text="当前工作目录是 /work。",
            nudges_used=0,
            max_nudges=2,
            no_tool_rounds=99,
            max_no_tool_rounds=5,
        )
        is False
    )


# ---------------------------------------------------------------------------
# v1.64.2：历史污染清洗 —— 回喂历史前剥掉旧文本协议的「写 ACTION：/ 执行：」残渣
# ---------------------------------------------------------------------------
def test_strip_legacy_action_narration_removes_stub_lines() -> None:
    """整行纯占位的「执行：/ 写 ACTION：/ 执行。」要被剥掉，实义句子保留。"""
    from app.chat.engine import _strip_legacy_action_narration as s

    sample = (
        "上传成功后的路径是 ./uplo4d/x.gif，先确认 .gif 后缀能否执行。\n\n\n"
        "执行：\n\n\n写 ACTION：\n\n\n执行。\n\n"
        "现在只写 ACTION JSON 行：\n\n\n执行：\n\n\n写 ACTION：\n\n\n执行。\n\n"
        "我现在测试 .user.ini 与 gif 后缀执行。"
    )
    out = s(sample)
    assert "写 ACTION：" not in out
    assert "现在只写 ACTION JSON 行" not in out
    # 实义句子保留
    assert "上传成功后的路径" in out
    assert "我现在测试" in out
    # 连续空行被压掉
    assert "\n\n\n" not in out


def test_strip_legacy_action_narration_keeps_real_action_json() -> None:
    """真正的 `ACTION: {...}`（含全角冒号）不能被当占位碎句删掉。"""
    from app.chat.engine import _strip_legacy_action_narration as s

    t = '好的。\nACTION：{"tool": "run_shell", "args": {"command": "ls"}}'
    out = s(t)
    assert "run_shell" in out
    assert "好的。" in out


def test_strip_legacy_action_narration_keeps_body_with_execute_word() -> None:
    """句子里出现了"执行"这个字、但不是整行占位 → 原样保留，不误删。"""
    from app.chat.engine import _strip_legacy_action_narration as s

    t = "执行这个命令：ls -la"
    assert s(t) == t


def test_render_history_applies_narration_cleanup() -> None:
    """`_render_history` 回喂 assistant 历史时确实清洗过（不把残渣喂给模型）。"""
    from app.chat import engine

    class _Row:
        def __init__(self, role, content, **kw):
            self.role = role
            self.content = content
            self.tool_name = kw.get("tool_name")
            self.tool_args = kw.get("tool_args")
            self.tool_failed = kw.get("tool_failed")
            self.model_extra = kw.get("model_extra")

    rows = [
        _Row("user", "继续"),
        _Row(
            "assistant",
            "我先看下目录。\n\n执行：\n\n写 ACTION：\n\n执行。\n\n现在读一下。",
        ),
    ]
    msgs = engine._render_history(rows)
    joined = "\n".join(m["content"] for m in msgs)
    assert "写 ACTION" not in joined
    assert "我先看下目录" in joined
    assert "现在读一下" in joined


# ---------------------------------------------------------------------------
# v1.67：模型流空闲超时 —— 上游只发 keepalive 不吐正文时必须超时，不许无限阻塞
# ---------------------------------------------------------------------------


async def test_stream_idle_timeout_raises_when_upstream_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复现线上卡死：上游挂着连接、只发 keepalive（无正文增量）。

    旧代码 `async for delta in gateway.chat_stream(...)` 会**永远**等下去，
    心跳冻结、租约被僵尸协程占死。v1.67 给每帧加空闲超时：`STREAM_IDLE_SECONDS`
    毫秒级无正文就抛 `ChatEngineError`（可重试），交回上层记 interrupted。
    """
    import asyncio

    # 把空闲阈值压到很小，让测试快速触发（生产默认 180s）
    monkeypatch.setattr(engine, "STREAM_IDLE_SECONDS", 0.05)

    class _HangingGateway:
        async def chat_stream(self, messages, *, system, finish_reason=None):  # noqa: ANN001, ANN201
            # 先吐一点点正文（pushed=True），然后永远阻塞（模拟 keepalive-only
            # 的死连接）。已有正文时超时不再退回非流式，而是如实抛出。
            yield "正在处理"
            await asyncio.sleep(3600)
            yield "永远不会到这里"

    emit = Recorder()
    with pytest.raises(engine.ChatEngineError) as excinfo:
        await engine._call_model(
            _HangingGateway(), [{"role": "user", "content": "go"}], system="sys", emit=emit
        )
    assert "无正文增量" in str(excinfo.value)
    # 可重试 → 交给 resumer，不落 failed
    assert getattr(excinfo.value, "retryable", True) is True


async def test_stream_idle_timeout_does_not_trip_on_slow_but_progressing_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真在吐字的慢流不该被误杀：每个正文增量都会重置计时。"""
    import asyncio

    monkeypatch.setattr(engine, "STREAM_IDLE_SECONDS", 0.2)

    class _SlowGateway:
        async def chat_stream(self, messages, *, system, finish_reason=None):  # noqa: ANN001, ANN201
            for piece in ["你", "好", "，", "世", "界"]:
                await asyncio.sleep(0.05)  # 每帧 50ms < 200ms 阈值，绝不触发
                yield piece

    emit = Recorder()
    reply = await engine._call_model(
        _SlowGateway(), [{"role": "user", "content": "go"}], system="sys", emit=emit
    )
    assert reply.text == "你好，世界"


async def test_stream_idle_timeout_falls_back_to_nonstream_when_nothing_pushed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """还没吐过正文就超时 → 退回非流式（而非整轮失败），与既有兜底一致。"""
    import asyncio

    monkeypatch.setattr(engine, "STREAM_IDLE_SECONDS", 0.05)

    class _HangingThenNonStreamGateway:
        async def chat_stream(self, messages, *, system, finish_reason=None):  # noqa: ANN001, ANN201
            await asyncio.sleep(3600)
            yield "x"

        async def chat(self, messages, *, system):  # noqa: ANN001, ANN201
            return LlmReply(
                text="兜底成功", source="content", raw={}, usage=None, finish_reason="stop"
            )

    emit = Recorder()
    reply = await engine._call_model(
        _HangingThenNonStreamGateway(),
        [{"role": "user", "content": "go"}],
        system="sys",
        emit=emit,
    )
    assert reply.text == "兜底成功"


# ══════════════════════════════════════════════════════════
# 15. 单轮墙钟预算（v1.68）：合法长任务也在到点优雅收尾，不是无限烧
# ══════════════════════════════════════════════════════════


def test_render_turn_wall_note_mentions_minutes_and_continue() -> None:
    """到点提示必须说清"跑了多久"和"点继续可接着干"，不是装成结论。"""
    note = engine.render_turn_wall_note(3600)
    assert "60 分钟" in note
    assert "继续" in note
    # 至少 1 分钟（防止四舍五入成 0 分钟这种怪话）。
    assert "1 分钟" in engine.render_turn_wall_note(10)


async def test_run_turn_stops_gracefully_when_wall_budget_exceeded(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单轮连续跑超过墙钟预算 → 落一句实话、needs_continue=True、不再继续烧。

    复刻 ctf 那个"每步都是不同真实命令、连续跑 6 小时"的形态：网关每轮都发一个
    可执行的 ACTION，tools_used 持续增长，MAX_REPEATED_ACTIONS 不触发；靠墙钟预算
    兜底收尾。
    """
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()

    monkeypatch.setattr(engine, "TURN_WALL_SECONDS", 1)

    # monotonic 每次调用 +10 秒 → 第一轮结束前就超过 1 秒预算。
    clock = {"t": 0.0}

    def _fake_monotonic() -> float:
        clock["t"] += 10.0
        return clock["t"]

    monkeypatch.setattr(engine.time, "monotonic", _fake_monotonic)

    async def fake_execute(ctx, name, args):  # noqa: ANN001, ANN202, ARG001
        return "ok", False

    monkeypatch.setattr(engine, "_execute_tool", fake_execute)

    # 动作由文本协议合成：直接用 ACTION 文本，保证 parse_actions 能解析出动作。
    class _ActionTextGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, *, system, tools=None):  # noqa: ANN001, ANN201
            self.calls += 1
            return LlmReply(
                text=f'继续。\nACTION: {{"tool": "run_shell", "args": {{"command": "echo step-{self.calls}"}}}}',
                source="content",
                raw={},
                usage=None,
                finish_reason="stop",
            )

    gateway = _ActionTextGateway()
    result = await engine.run_turn(
        db_session,
        session=session,
        content="开始长任务",
        identity=ADMIN,
        gateway=gateway,
    )

    assert result.needs_continue is True
    assert result.hit_tool_limit is True
    last = result.messages[-1]
    assert last.role == "assistant"
    assert "分钟" in last.content
    assert "继续" in last.content
    # 关键：并非无限跑；墙钟闸在某次工具执行后立刻停手（不会跑满 300 轮）。
    assert gateway.calls <= 3


# ══════════════════════════════════════════════════════════
# 16. 同一结论复读闸（v1.69）：模型反复给同一句"做不下去"就收尾，别再换个算法穷尽
# ══════════════════════════════════════════════════════════


def test_is_repeated_conclusion_detects_repeated_blocked_verdict() -> None:
    """同一句结论（措辞小幅变化、结论不变）出现 ≥3 次 → 判 True。"""
    from app.chat import nudge

    verdicts = [
        "没拿到 flag。这道题需要动态分析，而本沙箱没有 KVM、虚拟化缺失，硬件虚拟化不可用，静态已全空间穷尽。",
        "没拿到 flag。这道题需要动态分析，本沙箱没有 KVM、虚拟化缺失，硬件虚拟化不可用，静态已全空间穷尽。",
        "没拿到 flag。这道题需要动态分析，而本沙箱没有 KVM、虚拟化缺失，静态已全空间穷尽。",
    ]
    assert nudge.is_repeated_conclusion(verdicts) is True


def test_is_repeated_conclusion_false_for_distinct_progress() -> None:
    """不同的实质进展（不是复读结论）不该误判。"""
    from app.chat import nudge

    progress = [
        "创建了 a.txt 并写入 hello，验证通过。",
        "读取 b.txt 得到 world，内容正确。",
        "已经遍历 3 个目录，找到目标文件并开始解析。",
    ]
    assert nudge.is_repeated_conclusion(progress) is False


def test_is_repeated_conclusion_detects_oscillation() -> None:
    """结论与独特推理交替出现（振荡）也要抓到（v1.69 冒烟教训）。"""
    from app.chat import nudge

    verdict = "任务已完成，文件已就位，无需再做验证，内容为 hi 三字节。"
    seq = [
        verdict,
        "The UUID result loop keeps going; let me look at stderr differently.",
        verdict,
        "Interesting, this has a stderr block with uuidgen not found in path.",
        verdict,
        "我按原生工具接口真正执行一次，不再重复解释这一点了。",
    ]
    assert nudge.is_repeated_conclusion(seq) is True


def test_is_repeated_conclusion_false_when_fewer_than_threshold() -> None:
    from app.chat import nudge

    two = ["没拿到 flag，需要 KVM。", "没拿到 flag，需要 KVM。"]
    assert nudge.is_repeated_conclusion(two) is False


def test_render_repeated_conclusion_note_is_a_blocked_statement() -> None:
    note = engine.render_repeated_conclusion_note()
    # 必须讲清"这是阻塞、不是没试够"，并指出要补条件。
    assert "结论" in note
    assert "环境" in note or "信息" in note
    assert "继续" in note


async def test_run_turn_stops_when_conclusion_repeats(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型每轮都发出动作但结论完全一样 → 复读闸收尾（不再无限换个命令试）。

    复刻 chat_01M6P1TAYTASS6P64P85PED8MB 的形态：每步都是不同真实命令（重复动作
    检测不触发），但每次给的结论都是同一句"没拿到 flag、需要 KVM"。第 3 次之后
    应被复读闸收尾。
    """
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()

    async def fake_execute(ctx, name, args):  # noqa: ANN001, ANN202, ARG001
        return "ok", False

    monkeypatch.setattr(engine, "_execute_tool", fake_execute)
    # 把上下文门槛降到很小，让测试几轮内就能验证"复读即收尾"这件事本身
    # （生产门槛是 20 工具/12 轮，见 engine 常量注释）。
    monkeypatch.setattr(engine, "REPEATED_CONCLUSION_MIN_TOOLS", 2)
    monkeypatch.setattr(engine, "REPEATED_CONCLUSION_MIN_ROUNDS", 2)

    class _RepeatingConclusionGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, *, system, tools=None):  # noqa: ANN001, ANN201
            self.calls += 1
            # 每个命令都不同（避免 MAX_REPEATED_ACTIONS），但正文结论是同一句。
            verdict = (
                "没拿到 flag。这道题需要动态分析，而本沙箱没有 KVM、虚拟化缺失，"
                "硬件虚拟化不可用，静态已全空间穷尽。"
            )
            return LlmReply(
                text=(
                    f"{verdict}\n"
                    f'ACTION: {{"tool": "run_shell", "args": {{"command": "echo probe-{self.calls}"}}}}'
                ),
                source="content",
                raw={},
                usage=None,
                finish_reason="stop",
            )

    gateway = _RepeatingConclusionGateway()
    result = await engine.run_turn(
        db_session,
        session=session,
        content="解这道 rev 题",
        identity=ADMIN,
        gateway=gateway,
    )

    last = result.messages[-1]
    assert last.role == "assistant"
    assert "结论" in last.content
    # 不该无限跑：复读闸在少数几轮内收尾。
    assert gateway.calls <= 5

