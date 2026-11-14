import json

from app.providers.llm import stream_reasoning_delta


def test_openai_reasoning_delta_is_separate_from_content() -> None:
    data = json.dumps(
        {"choices": [{"delta": {"reasoning_content": "先看端口", "content": "答案"}}]}
    )
    assert stream_reasoning_delta(False, None, data) == "先看端口"


def test_anthropic_thinking_delta() -> None:
    data = json.dumps({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "想一下"}})
    assert stream_reasoning_delta(True, "content_block_delta", data) == "想一下"


def test_content_only_has_no_reasoning() -> None:
    data = json.dumps({"choices": [{"delta": {"content": "答案"}}]})
    assert stream_reasoning_delta(False, None, data) == ""
