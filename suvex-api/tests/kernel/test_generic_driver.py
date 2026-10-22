"""配置解释器（`GenericDriver`）与配置校验（`PlatformSpec`）。

## ★ 这组用例真正要证明的一件事

"填个表，agent 自动对接"能不能成立，取决于**一份配置能不能替代一段
代码**。所以这里的做法是：把 `api_doc.md` 描述的那个平台写成一份配置，
喂**同一批文档里的示例响应**，然后断言它读出来的东西与手写驱动
（`test_slabmatch_driver.py`）一致。

如果这组用例绿，那么换一个形状相同的平台时，要做的只是"再写一份
配置"—— 不需要动一行代码。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app.kernel.adapters.generic import GenericDriver
from app.kernel.adapters.spec import PlatformSpec, SpecError, dig_int

# ══════════════════════════════════════════════════════════
# 一份**照 api_doc.md 写的**配置
# ══════════════════════════════════════════════════════════

SLABMATCH_SPEC: dict[str, Any] = {
    "display_name": "比赛平台",
    "base_url": "https://example.com/slab-match/api/v1/agent",
    "auth": {"style": "header", "name": "X-Agent-AccessKey"},
    "envelope": {
        "success_path": "code",
        "success_value": "00000",
        "data_path": "data",
        "message_path": "message",
    },
    "list_challenges": {
        "request": {"method": "GET", "path": "/ctf/exercise-list"},
        "items_path": "",
        "children_path": "corpus",
        "category_from_group": True,
    },
    "challenge_detail": {
        "method": "GET",
        "path": "/ctf/exercise",
        "params": {"exerciseId": "{id}"},
    },
    "submit": {
        "method": "POST",
        "path": "/answer-panel/answer",
        "body": {"exerciseId": "{id}", "flag": "{answer}"},
    },
    "submit_solved_path": "isCorrect",
    "start_env": {
        "method": "POST",
        "path": "/ctf/build-exercise-env",
        "body": {"exerciseId": "{id}"},
    },
    "stop_env": {
        "method": "POST",
        "path": "/ctf/recover-exercise-env",
        "body": {"exerciseId": "{id}"},
    },
    "environment": {
        "from_detail": True,
        "endpoints_path": "endpoints",
        "ready_path": "isNeedCheck",
        "ready_negate": True,
        "can_refresh_path": "canRefreshEndpoint",
    },
    "answer_max_length": 256,
    "fields": {
        "id": "id",
        "title": "name",
        "description": "description",
        "score": "score",
        "solved": "hasSolved",
        "is_open": "isOpen",
        "difficulty": "difficulty",
        "attachments": "attachment.files",
        "attachment_url": "url",
        "attachment_name": "name",
        "attachment_ext": "ext",
    },
}

LIST_PAYLOAD = [
    {
        "id": 10,
        "name": "Web",
        "order": 1,
        "corpus": [
            {"id": 1001, "name": "easy-web", "order": 1, "isOpen": True, "hasSolved": False},
            {"id": 1002, "name": "hard-web", "order": 2, "isOpen": False, "hasSolved": True},
        ],
    }
]

DETAIL_PAYLOAD = {
    "id": 1001,
    "name": "easy-web",
    "description": "题目描述",
    "hasSolved": False,
    "score": "100",
    "difficulty": "EASY",
    "attachment": {
        "files": [{"name": "attachment.zip", "url": "https://example.com/file", "ext": "zip"}]
    },
    "endpoints": [
        {
            "exposeIps": ["10.0.0.10"],
            "ports": ["80", "22"],
            "users": [{"username": "root", "password": "password"}],
            "portMappings": [{"type": "tcp", "port": "80", "proxy": "30080"}],
            "proxyIps": ["1.2.3.4"],
            "isProxy": False,
            "expireTime": 1780000000000,
        }
    ],
    "canRefreshEndpoint": False,
    "isNeedInit": False,
    "isNeedCheck": False,
}


def _ok(data: Any) -> dict[str, Any]:
    return {"code": "00000", "message": "", "data": data}


def _fail(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message, "data": None}


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _driver(handler: Any, spec: dict[str, Any] | None = None) -> GenericDriver:
    return GenericDriver(
        PlatformSpec.from_dict(spec or SLABMATCH_SPEC),
        "ak_test",
        min_interval=0.0,
        transport=httpx.MockTransport(handler),
    )


def _routes(mapping: dict[str, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, payload in mapping.items():
            if request.url.path.endswith(suffix):
                if isinstance(payload, httpx.Response):
                    return payload
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"code": "404", "message": "no such route"})

    return handler


# ══════════════════════════════════════════════════════════
# 配置校验
# ══════════════════════════════════════════════════════════


def test_a_minimal_spec_parses() -> None:
    spec = PlatformSpec.from_dict(SLABMATCH_SPEC)
    assert spec.base_url == "https://example.com/slab-match/api/v1/agent"
    assert spec.auth.name == "X-Agent-AccessKey"
    assert spec.envelope.success_value == "00000"
    assert spec.list_challenges.children_path == "corpus"
    assert spec.answer_max_length == 256
    assert spec.slug_hint() == "example-com"


def test_a_spec_without_a_challenge_source_is_rejected() -> None:
    """没有 `list_challenges` 就等于没有取题能力 —— 必须当场拒绝。

    放过去的话，症状是"同步永远 0 道题"，而用户会以为是平台没放题。
    """
    broken = {k: v for k, v in SLABMATCH_SPEC.items() if k != "list_challenges"}
    with pytest.raises(SpecError) as exc_info:
        PlatformSpec.from_dict(broken)
    assert "list_challenges" in str(exc_info.value)


def test_an_envelope_with_a_success_path_but_no_value_is_rejected() -> None:
    """只说"看哪个字段"而不说"等于什么"，等于没判成功。

    ★ 这条拦的是一个**很贵**的错：如果只配了 `success_path` 而没配值，
    任何响应（包括失败的）都会被当成成功，然后 `data` 是空的 ——
    最终表现成一道题反复提交、永远不结束。
    """
    broken = dict(SLABMATCH_SPEC)
    broken["envelope"] = {"success_path": "code", "data_path": "data"}
    with pytest.raises(SpecError) as exc_info:
        PlatformSpec.from_dict(broken)
    assert "success_value" in str(exc_info.value)


def test_a_non_http_base_url_is_rejected() -> None:
    broken = dict(SLABMATCH_SPEC)
    broken["base_url"] = "example.com"
    with pytest.raises(SpecError) as exc_info:
        PlatformSpec.from_dict(broken)
    assert "http" in str(exc_info.value)


def test_error_messages_name_the_offending_field() -> None:
    """★ 错误信息会被**原样回喂给大模型**重试。

    所以它必须说清"哪一项、期望什么" —— 一句"配置格式错误"会让模型
    盲猜，而它多半会猜同一个错。
    """
    broken = dict(SLABMATCH_SPEC)
    broken["auth"] = {"style": "magic"}
    with pytest.raises(SpecError) as exc_info:
        PlatformSpec.from_dict(broken)
    message = str(exc_info.value)
    assert "auth.style" in message
    assert "magic" in message


def test_dig_int_accepts_the_string_score_the_document_uses() -> None:
    """文档写的是 `"score": "100"` —— 字符串。"""
    assert dig_int({"score": "100"}, "score") == 100
    assert dig_int({"score": 100}, "score") == 100
    assert dig_int({"score": "abc"}, "score") is None
    assert dig_int({}, "score") is None


# ══════════════════════════════════════════════════════════
# 配置驱动 vs 手写驱动：读出来的东西必须一样
# ══════════════════════════════════════════════════════════


def test_configured_list_matches_what_the_manual_driver_reads() -> None:
    """★★ 两级列表展平 + 分类 + isOpen + hasSolved。

    这是"配置能替代代码"的核心断言 —— 同样的响应，配置驱动必须读出与
    手写驱动（`test_slabmatch_driver.py` 里那一组）完全相同的结果。
    """

    async def scenario() -> Any:
        driver = _driver(_routes({"/ctf/exercise-list": _ok(LIST_PAYLOAD)}))
        try:
            return await driver.fetch_challenges(known_ids=set())
        finally:
            await driver.aclose()

    challenges = _run(scenario())
    assert [c.remote_id for c in challenges] == ["1001", "1002"]
    assert challenges[0].title == "easy-web"
    # ★ 分类来自**外层组名**（`category_from_group`）。它决定选哪个沙箱
    #   镜像 —— 丢掉它，逆向题会落到基础镜像上。
    assert challenges[0].category == "Web"
    assert challenges[0].is_open is True
    assert challenges[1].is_open is False
    assert challenges[1].solved is True
    # ★ handle 的键固定是 `id`（解释器与配置之间的约定），不是平台自己的
    #   `exerciseId` —— 那是**我们**造的寻址，用哪个键由我们决定。
    assert challenges[0].handle == {"id": "1001"}


def test_configured_detail_reads_the_string_score_as_an_int() -> None:
    async def scenario() -> Any:
        driver = _driver(_routes({"/ctf/exercise": _ok(DETAIL_PAYLOAD)}))
        try:
            return await driver.fetch_challenge({"id": "1001"})
        finally:
            await driver.aclose()

    challenge = _run(scenario())
    assert challenge is not None
    assert challenge.score == 100
    assert challenge.difficulty == "EASY"
    assert challenge.attachments[0].url == "https://example.com/file"


def test_configured_submit_sends_an_integer_id() -> None:
    """★★ `{"exerciseId": "{id}"}` 换出来的必须是**整数** 1001。

    整个模板正好等于占位符时返回原始标量（见 `RequestSpec.render`）。
    发成字符串 `"1001"` 的后果是平台的类型校验拒掉它，或者更糟 ——
    被当成另一个 id，于是答案交到了**另一道题**上。
    """
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json=_ok({"isCorrect": True}))

    async def scenario() -> Any:
        driver = _driver(handler)
        try:
            return await driver.submit({"id": "1001"}, "flag{test}")
        finally:
            await driver.aclose()

    outcome = _run(scenario())
    assert outcome.accepted is True
    assert outcome.is_solved is True
    sent = bodies[0].replace(b" ", b"")
    assert b'"exerciseId":1001' in sent
    assert b'"flag":"flag{test}"' in sent


def test_configured_wrong_answer_is_accepted_but_not_solved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fail("40002", "flag 错误"))

    async def scenario() -> Any:
        driver = _driver(handler)
        try:
            return await driver.submit({"id": "1001"}, "wrong")
        finally:
            await driver.aclose()

    outcome = _run(scenario())
    assert outcome.accepted is True
    assert outcome.is_solved is False


def test_configured_rate_limit_code_is_not_a_wrong_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fail("TOO_MANY_REQUESTS", "太快了"))

    async def scenario() -> Any:
        driver = _driver(handler)
        try:
            return await driver.submit({"id": "1001"}, "x")
        finally:
            await driver.aclose()

    outcome = _run(scenario())
    assert outcome.accepted is False


# ══════════════════════════════════════════════════════════
# 环境
# ══════════════════════════════════════════════════════════


def test_configured_environment_ready_needs_both_conditions() -> None:
    """★★ `ready_path` + `ready_negate` 必须能表达"反着的布尔值"。

    比赛平台的 `isNeedCheck` 是**反的**（false 才是就绪）。不能取反的话
    判据会整个倒过来 —— 那意味着"环境好了我们以为没好、一直等到超时"。

    同时这条守住"**且** endpoints 可用"那半句：平台说不检查了、但连接
    信息还是空的，仍然不算就绪。
    """

    async def scenario() -> None:
        cases = [
            (False, DETAIL_PAYLOAD["endpoints"], True, "两个条件都满足"),
            (True, DETAIL_PAYLOAD["endpoints"], False, "还在准备中"),
            (False, [], False, "不检查了但没给连接信息"),
        ]
        for need_check, endpoints, expected, label in cases:
            payload = {**DETAIL_PAYLOAD, "isNeedCheck": need_check, "endpoints": endpoints}

            def handler(request: httpx.Request, _p: dict = payload) -> httpx.Response:
                return httpx.Response(200, json=_ok(_p))

            driver = _driver(handler)
            try:
                env = await driver.read_environment({"id": "1001"})
            finally:
                await driver.aclose()
            assert env.ready is expected, label

    _run(scenario())


def test_configured_endpoint_keeps_credentials() -> None:
    async def scenario() -> Any:
        driver = _driver(_routes({"/ctf/exercise": _ok(DETAIL_PAYLOAD)}))
        try:
            return await driver.read_environment({"id": "1001"})
        finally:
            await driver.aclose()

    env = _run(scenario())
    endpoint = env.endpoints[0]
    assert endpoint.expose_ips == ["10.0.0.10"]
    assert endpoint.users == [("root", "password")]
    assert endpoint.port_mappings == [{"type": "tcp", "port": "80", "proxy": "30080"}]
    assert endpoint.expire_time is not None


def test_configured_environment_prompt_names_the_credentials() -> None:
    async def scenario() -> str:
        driver = _driver(_routes({"/ctf/exercise": _ok(DETAIL_PAYLOAD)}))
        try:
            env = await driver.read_environment({"id": "1001"})
            return await driver.environment_prompt(env)
        finally:
            await driver.aclose()

    text = _run(scenario())
    assert "10.0.0.10" in text
    assert "root / password" in text


def test_configured_start_and_stop_hit_the_configured_paths() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=_ok(None))

    async def scenario() -> None:
        driver = _driver(handler)
        try:
            await driver.start_environment({"id": "1001"})
            await driver.stop_environment({"id": "1001"})
        finally:
            await driver.aclose()

    _run(scenario())
    assert paths[0].endswith("/ctf/build-exercise-env")
    assert paths[1].endswith("/ctf/recover-exercise-env")


def test_a_platform_without_environment_support_reports_so() -> None:
    """没配环境端点的平台：`supports_environment` 为假，且读写都不报错。

    ★ 这一点对"附件题平台"很实际：一道纯附件的题也会走到"起环境"这一步
    （流程是统一的），而它没有环境可言 —— 抛异常会让这类题**全部失败**，
    而它们本该是最容易拿分的那一批。
    """

    async def scenario() -> None:
        spec = {
            k: v
            for k, v in SLABMATCH_SPEC.items()
            if k not in ("start_env", "stop_env", "environment")
        }
        driver = _driver(_routes({}), spec=spec)
        try:
            assert driver.supports_environment is False
            await driver.start_environment({"id": "1001"})
            await driver.stop_environment({"id": "1001"})
            env = await driver.read_environment({"id": "1001"})
            assert env.ready is False
        finally:
            await driver.aclose()

    _run(scenario())


# ══════════════════════════════════════════════════════════
# 平台元信息
# ══════════════════════════════════════════════════════════


def test_answer_max_length_comes_from_the_config() -> None:
    async def scenario() -> None:
        driver = _driver(_routes({}))
        try:
            assert driver.answer_max_length == 256
        finally:
            await driver.aclose()

    _run(scenario())


def test_a_wrong_handle_raises_on_submit_but_returns_none_on_detail() -> None:
    """handle 不对时：提交**抛异常**（猜一个 id 的代价太大），
    取详情返回 `None`（上层据此决定要不要走补题路径）。"""

    async def scenario() -> None:
        driver = _driver(_routes({"/ctf/exercise": _ok(DETAIL_PAYLOAD)}))
        try:
            assert await driver.fetch_challenge({}) is None
            from app.kernel.client import Ctf2ResponseError

            with pytest.raises(Ctf2ResponseError):
                await driver.submit({}, "flag")
        finally:
            await driver.aclose()

    _run(scenario())
