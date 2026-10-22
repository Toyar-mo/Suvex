"""`adapters.builder`：生成 → 校验 → 试跑 → 回喂重试。

## 这组用例里最重要的两条

1. **错误回喂**（`test_build_feeds_the_error_back`）—— 闭环的核心。
   如果失败之后不把错误告诉模型，它只会重写一份一模一样的配置，
   而"重试三次"就退化成"浪费三次调用"。

2. **试跑不过就不算成功**（`test_build_does_not_trust_a_config_that_cannot_fetch`）
   —— 语法正确的配置完全可能是幻觉的。不试跑就存下来，等于把问题
   推迟到比赛当天提交答案的那一刻。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.kernel.adapters.builder import (
    build_spec,
    extract_json,
    parse_spec,
    verify_spec,
)
from app.kernel.adapters.probe import ProbeReport
from app.kernel.adapters.spec import PlatformSpec, SpecError
from tests.kernel.test_generic_driver import LIST_PAYLOAD, SLABMATCH_SPEC

BASE = "https://example.com/slab-match/api/v1/agent"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _report(**overrides: Any) -> ProbeReport:
    base: dict[str, Any] = {
        "base_url": BASE,
        "origin": "https://example.com",
        "auth_style": "header",
        "auth_name": "X-Agent-AccessKey",
        "challenge_source": "/ctf/exercise-list",
        "challenge_count": 2,
    }
    base.update(overrides)
    return ProbeReport(**base)


def _ok(data: Any) -> dict[str, Any]:
    return {"code": "00000", "message": "", "data": data}


def _routes(mapping: dict[str, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, payload in mapping.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"code": "404", "message": "no such route"})

    return handler


class FakeChat:
    """按顺序吐出预设回复，并记下每次收到的消息。"""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []
        self.systems: list[str] = []

    async def __call__(self, messages: list[dict[str, str]], *, system: str = "") -> str:
        self.calls.append([dict(m) for m in messages])
        self.systems.append(system)
        if not self._replies:
            raise AssertionError("FakeChat 被调用的次数超出了预设的回复数")
        return self._replies.pop(0)


# ══════════════════════════════════════════════════════════
# 输出解析
# ══════════════════════════════════════════════════════════


def test_extract_json_accepts_a_bare_object() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_strips_a_markdown_fence() -> None:
    """模型很爱包一层代码块 —— 那是排版习惯，不是错误，不该因此失败。"""
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('```jsonc\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_ignores_surrounding_chatter() -> None:
    """模型有时候还是要说一句"这是配置："。抠出来就好。"""
    assert extract_json('这是配置：\n{"a": 1}\n希望有帮助。') == {"a": 1}


def test_extract_json_raises_when_there_is_none() -> None:
    with pytest.raises(SpecError):
        extract_json("我没法完成这个任务。")


# ══════════════════════════════════════════════════════════
# base_url 必须钉死在用户填的主机上
# ══════════════════════════════════════════════════════════


def test_a_missing_base_url_is_filled_from_what_the_user_typed() -> None:
    data = {k: v for k, v in SLABMATCH_SPEC.items() if k != "base_url"}
    spec = parse_spec(json.dumps(data), base_url=BASE)
    assert spec.base_url == BASE


def test_the_model_may_add_a_path_prefix() -> None:
    """用户只填域名时，模型补出真实前缀是**对的**（文档里就是那样）。"""
    data = dict(SLABMATCH_SPEC)
    data["base_url"] = BASE
    spec = parse_spec(json.dumps(data), base_url="https://example.com")
    assert spec.base_url == BASE


def test_a_different_host_is_rejected() -> None:
    """★★ 模型可能"顺手"把地址改成文档里的示例域名。

    那份配置会带着**别人的域名**跑起来 —— 取题取不到、提交提交不了，
    而错误信息看起来像网络问题。所以主机不一致必须当场拒绝。
    """
    data = dict(SLABMATCH_SPEC)
    data["base_url"] = "https://example.com/other"
    with pytest.raises(SpecError) as exc_info:
        parse_spec(json.dumps(data), base_url="https://real-competition.example.org")
    message = str(exc_info.value)
    assert "example.com" in message
    assert "real-competition" in message


# ══════════════════════════════════════════════════════════
# 试跑
# ══════════════════════════════════════════════════════════


def test_verify_passes_on_the_documented_payload() -> None:
    spec = PlatformSpec.from_dict(SLABMATCH_SPEC)

    async def scenario() -> Any:
        return await verify_spec(
            spec,
            "ak_test",
            transport=httpx.MockTransport(_routes({"/ctf/exercise-list": _ok(LIST_PAYLOAD)})),
        )

    result = _run(scenario())
    assert result.ok is True
    assert result.challenge_count == 2
    assert result.sample is not None and result.sample.title == "easy-web"


def test_verify_tells_the_model_what_to_check_when_the_list_is_empty() -> None:
    """空列表不算通过 —— 而且错误里要给出**具体该检查什么**。

    只说"取不到题"的话，模型只能瞎改；说清"可能是 items_path 指错了、
    或者该填 children_path"，它才知道往哪改。
    """
    spec = PlatformSpec.from_dict(SLABMATCH_SPEC)

    async def scenario() -> Any:
        return await verify_spec(
            spec,
            "ak_test",
            transport=httpx.MockTransport(_routes({"/ctf/exercise-list": _ok([])})),
        )

    result = _run(scenario())
    assert result.ok is False
    assert "items_path" in result.error
    assert "children_path" in result.error


def test_verify_reports_a_transport_failure_with_its_original_type() -> None:
    """网络/鉴权失败时，错误文本里要带**原始异常类型与消息**。

    因为那段文本会被原样回喂给模型 —— 它需要看到"401"才会想到去改
    `auth`，而不是去改字段路径。
    """
    spec = PlatformSpec.from_dict(SLABMATCH_SPEC)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "x", "message": "bad key"})

    async def scenario() -> Any:
        return await verify_spec(spec, "ak_test", transport=httpx.MockTransport(handler))

    result = _run(scenario())
    assert result.ok is False
    assert "401" in result.error or "凭据" in result.error


def test_verify_catches_a_wrong_title_field() -> None:
    """端点对了、但 `fields.title` 指错字段 —— 必须被抓出来。

    这是**最危险**的一类错：请求全都成功，只有读出来的东西是空的。
    不检查的话，界面上会出现一批没有标题的题，而同步"看起来成功了"。
    """
    broken = dict(SLABMATCH_SPEC)
    broken["fields"] = {**SLABMATCH_SPEC["fields"], "title": "not_a_field"}
    spec = PlatformSpec.from_dict(broken)

    async def scenario() -> Any:
        return await verify_spec(
            spec,
            "ak_test",
            transport=httpx.MockTransport(_routes({"/ctf/exercise-list": _ok(LIST_PAYLOAD)})),
        )

    result = _run(scenario())
    assert result.ok is False
    assert "title" in result.error


# ══════════════════════════════════════════════════════════
# 全流程
# ══════════════════════════════════════════════════════════


def test_build_succeeds_on_the_first_try() -> None:
    chat = FakeChat([json.dumps(SLABMATCH_SPEC)])

    async def scenario() -> Any:
        return await build_spec(
            base_url=BASE,
            access_key="ak_test",
            chat=chat,
            document="接口文档……",
            report=_report(),
            transport=httpx.MockTransport(
                _routes({"/ctf/exercise-list": _ok(LIST_PAYLOAD)})
            ),
        )

    result = _run(scenario())
    assert result.ok is True
    assert result.challenge_count == 2
    assert len(result.attempts) == 1
    # 提示词里必须带上实测报告与文档 —— 少任何一样，模型就只能靠猜。
    assert "实测结果" in chat.calls[0][0]["content"]
    assert "接口文档" in chat.calls[0][0]["content"]
    # system prompt 必须是那份平台接入提示词（不是解题提示词）。
    assert "平台接入配置生成" in chat.systems[0]


def test_build_feeds_the_error_back_and_succeeds_on_the_retry() -> None:
    """★★ 闭环的核心：第一次端点写错 → 把**试跑的错误**回喂 → 第二次改对。

    如果失败之后不告诉模型错在哪，它只会重写一份一模一样的配置 ——
    那"重试三次"就只是浪费三次调用。
    """
    wrong = dict(SLABMATCH_SPEC)
    wrong["list_challenges"] = {
        "request": {"method": "GET", "path": "/challenges"},
        "items_path": "",
    }
    chat = FakeChat([json.dumps(wrong), json.dumps(SLABMATCH_SPEC)])

    async def scenario() -> Any:
        return await build_spec(
            base_url=BASE,
            access_key="ak_test",
            chat=chat,
            report=_report(),
            transport=httpx.MockTransport(
                _routes({"/ctf/exercise-list": _ok(LIST_PAYLOAD)})
            ),
        )

    result = _run(scenario())
    assert result.ok is True
    assert len(result.attempts) == 2
    # 第一次的账上记着错误原因 —— 排查"模型为什么总也不对"全靠它。
    assert result.attempts[0].error
    # ★ 第二次调用时，消息里必须**同时**有模型的上一份输出和错误说明。
    second = chat.calls[1]
    assert any("challenges" in m["content"] for m in second if m["role"] == "assistant")
    retry_text = " ".join(m["content"] for m in second if m["role"] == "user")
    assert "没有通过验证" in retry_text
    assert "完整" in retry_text  # 要求重新输出完整配置，而不是补丁


def test_build_feeds_a_validation_error_back_too() -> None:
    """连**校验**都过不去时，错误同样要回喂（而不是直接放弃）。"""
    chat = FakeChat(['{"display_name": "缺了很多东西"}', json.dumps(SLABMATCH_SPEC)])

    async def scenario() -> Any:
        return await build_spec(
            base_url=BASE,
            access_key="ak_test",
            chat=chat,
            report=_report(),
            transport=httpx.MockTransport(
                _routes({"/ctf/exercise-list": _ok(LIST_PAYLOAD)})
            ),
        )

    result = _run(scenario())
    assert result.ok is True
    assert "list_challenges" in result.attempts[0].error
    assert result.challenge_count == 2


def test_build_gives_up_but_keeps_the_last_valid_spec() -> None:
    """三次都取不到题 → 失败，但**那份合法配置要带回去**。

    它离成功最近，用户可以在界面上看到它、手工改一处就成。
    返回 `None` 等于把模型做出来的全部工作扔掉。
    """
    wrong = dict(SLABMATCH_SPEC)
    wrong["list_challenges"] = {
        "request": {"method": "GET", "path": "/nope"},
        "items_path": "",
    }
    chat = FakeChat([json.dumps(wrong)] * 3)

    async def scenario() -> Any:
        return await build_spec(
            base_url=BASE,
            access_key="ak_test",
            chat=chat,
            report=_report(),
            max_attempts=3,
            transport=httpx.MockTransport(
                _routes({"/ctf/exercise-list": _ok(LIST_PAYLOAD)})
            ),
        )

    result = _run(scenario())
    assert result.ok is False
    assert len(result.attempts) == 3
    assert result.spec is not None, "最后一次合法配置应该带回去给用户看"
    assert result.error


def test_build_can_skip_verification() -> None:
    """`verify=False` 只做生成 —— 给"先看看模型会写出什么"这个用法。"""
    chat = FakeChat([json.dumps(SLABMATCH_SPEC)])

    async def scenario() -> Any:
        return await build_spec(
            base_url=BASE,
            access_key="ak_test",
            chat=chat,
            report=_report(),
            verify=False,
        )

    result = _run(scenario())
    assert result.ok is True
    assert result.challenge_count == 0
    assert len(chat.calls) == 1


def test_build_mentions_the_missing_document_to_the_model() -> None:
    """没有文档时，提示词要**明说**这一点 —— 否则模型会以为文档只是
    没贴上，从而按常识补出一些不存在的东西。"""
    chat = FakeChat([json.dumps(SLABMATCH_SPEC)])

    async def scenario() -> Any:
        return await build_spec(
            base_url=BASE,
            access_key="ak_test",
            chat=chat,
            document="",
            report=_report(),
            verify=False,
        )

    _run(scenario())
    assert "没有提供文档" in chat.calls[0][0]["content"]
    assert "宁可留空" in chat.calls[0][0]["content"]
