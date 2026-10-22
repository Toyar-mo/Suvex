"""`ctf2.drivers.slabmatch` 的行为：照 `api_doc.md` 逐条对。

## ★ 这里的响应体**逐字抄自 api_doc.md**

不是我自己编的形状。编出来的 mock 只能证明"我的代码和我的想象一致" ——
而接一个新平台时，错的从来不是代码，是**想象**（字段名叫 `flag` 还是
`answer`、`isCorrect` 还是 `is_solved`、分值是不是字符串）。

所以每个用例都把文档里那段 JSON 原样贴进来，再断言驱动从它里面读出了
什么。文档与实现任何一方变了，这些用例会立刻红。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app.kernel.client import KernelAuthExpired, KernelRateLimited, KernelResponseError
from app.kernel.drivers.slabmatch import SlabMatchDriver, normalize_base_url

AGENT_ROOT = "https://example.com/slab-match/api/v1/agent"


def _ok(data: Any) -> dict[str, Any]:
    return {"code": "00000", "message": "", "data": data}


def _fail(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message, "data": None}


def _run(coro: Any) -> Any:
    """跑一个协程。

    ⚠️ **刻意不用** `@pytest.mark.asyncio`：`SlabMatchDriver` 内部的
    `httpx.AsyncClient` 会绑定到创建它的 loop 上，而每个用例只跑一次
    `asyncio.run` —— 用 pytest-asyncio 的共享 loop 反而会让"第二次调用"
    在别的用例里悄悄复用同一个 client（见 `test_runner` 里那条同样的注释）。
    """
    return asyncio.run(coro)


def _driver(handler: Any, *, base: str = "https://example.com") -> SlabMatchDriver:
    return SlabMatchDriver(
        base,
        "ak_test",
        min_interval=0.0,
        transport=httpx.MockTransport(handler),
    )


def _routes(mapping: dict[str, Any]) -> Any:
    """按路径后缀分发的假平台。`mapping` 的值可以是 dict（自动包 200）或 Response。"""

    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, payload in mapping.items():
            if request.url.path.endswith(suffix):
                if isinstance(payload, httpx.Response):
                    return payload
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"code": "404", "message": "no such route"})

    return handler


# ══════════════════════════════════════════════════════════
# Base URL
# ══════════════════════════════════════════════════════════


def test_a_bare_host_gets_the_agent_path_appended() -> None:
    """只填域名也要能用。

    ★ 赛事方给的地址层级不固定：文档写的是完整的
    `{host}/slab-match/api/v1/agent`，而人在设置页里更可能只填域名。
    不补的话症状是一条 **404** —— 而 404 在设置页里看起来像 "key 不对"，
    排查方向会整个错掉。
    """
    assert normalize_base_url("https://example.com") == AGENT_ROOT
    assert normalize_base_url("https://example.com/") == AGENT_ROOT
    assert normalize_base_url("  https://example.com  ") == AGENT_ROOT


def test_a_full_url_is_left_alone() -> None:
    """已经填全的就别再补一遍 —— 补两次会拼出 `.../agent/.../agent`。"""
    assert normalize_base_url(AGENT_ROOT) == AGENT_ROOT
    assert normalize_base_url(AGENT_ROOT + "/") == AGENT_ROOT


def test_an_empty_value_stays_empty() -> None:
    assert normalize_base_url("") == ""
    assert normalize_base_url("   ") == ""


# ══════════════════════════════════════════════════════════
# 传输与统一响应
# ══════════════════════════════════════════════════════════


def test_requests_carry_the_agent_access_key_header() -> None:
    """文档 §「认证 Header」：所有接口都要带 `X-Agent-AccessKey`。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_ok({"note": "注意事项", "rule": "规则"}))

    async def scenario() -> dict[str, Any]:
        driver = _driver(handler)
        try:
            return await driver.match_info()
        finally:
            await driver.aclose()

    info = _run(scenario())
    assert info["rule"] == "规则"
    assert seen[0].headers["X-Agent-AccessKey"] == "ak_test"
    assert seen[0].url.path == "/slab-match/api/v1/agent/match/notice/match-info"


def test_http_200_with_a_failure_code_is_still_an_error() -> None:
    """★★ 这个平台**失败也返回 HTTP 200** —— 判成功只能看 `code`。

    只看状态码的后果很隐蔽：`data` 会是 `None`/`{}`，而上层把它读成
    "没有 isCorrect 字段"，最终表现成**一道题反复提交、永远不结束** ——
    那看起来像调度器的 bug，而不是"我们没读 code"。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fail("40001", "参数不合法"))

    async def scenario() -> None:
        driver = _driver(handler)
        try:
            with pytest.raises(KernelResponseError) as exc_info:
                await driver.match_info()
        finally:
            await driver.aclose()
        assert exc_info.value.code == "40001"
        assert "参数不合法" in str(exc_info.value)

    _run(scenario())


def test_a_response_without_the_code_envelope_is_rejected() -> None:
    """没有 `{code, message, data}` 包装 = 这根本不是文档里那套接口。

    必须**当场报错**而不是尽力解析：一份裸 JSON 可能能被猜着读出来，
    但猜对了没人知道，猜错了要等到提交那一步才炸。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    async def scenario() -> None:
        driver = _driver(handler)
        try:
            with pytest.raises(KernelResponseError) as exc_info:
                await driver.match_info()
        finally:
            await driver.aclose()
        assert "code" in str(exc_info.value)

    _run(scenario())


def test_429_and_401_map_to_their_own_error_types() -> None:
    """限流与鉴权失败要能被上层分别处理。

    ⚠️ 它们**不能**混进"平台判错"里：那会让一次限流被记成"答案错了"，
    白白消耗不可再生的错误提交次数。
    """

    async def scenario() -> None:
        for status, expected, headers in (
            (429, KernelRateLimited, {"retry-after": "7"}),
            (401, KernelAuthExpired, {}),
        ):
            def handler(request: httpx.Request, _s: int = status, _h: dict = headers) -> httpx.Response:
                return httpx.Response(_s, json={"code": "x"}, headers=_h)

            driver = _driver(handler)
            try:
                with pytest.raises(expected):
                    await driver.match_info()
            finally:
                await driver.aclose()

    _run(scenario())


def test_retry_after_is_parsed_from_the_header() -> None:
    """`Retry-After` 要带出来 —— 上层的等待时长靠它，不靠猜。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"code": "x"}, headers={"retry-after": "7"})

    async def scenario() -> None:
        driver = _driver(handler)
        try:
            with pytest.raises(KernelRateLimited) as exc_info:
                await driver.match_info()
        finally:
            await driver.aclose()
        assert exc_info.value.retry_after == 7.0

    _run(scenario())


# ══════════════════════════════════════════════════════════
# 题目列表（文档 §4）
# ══════════════════════════════════════════════════════════

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


def test_exercise_list_is_flattened_with_its_category() -> None:
    """★ 列表是**两级**的（分类 → corpus），而本地要的是扁平题目。

    分类名必须被带上（`category`）—— 它决定选哪个沙箱镜像
    （逆向题用 `ctf2-sandbox-re`，见 `_profile_for`）。丢掉它会让所有
    题都落到基础镜像上，而症状是"逆向题里跑不动 gdb"，很难归因。
    """

    async def scenario() -> Any:
        driver = _driver(_routes({"/ctf/exercise-list": _ok(LIST_PAYLOAD)}))
        try:
            return await driver.fetch_challenges(known_ids=set())
        finally:
            await driver.aclose()

    challenges = _run(scenario())
    assert [c.remote_id for c in challenges] == ["1001", "1002"]
    assert challenges[0].category == "Web"
    assert challenges[0].group_name == "Web"
    assert challenges[0].handle == {"exerciseId": 1001}
    assert challenges[0].order == 1


def test_unopened_challenges_are_marked_and_solved_ones_are_marked() -> None:
    """★★ `isOpen` / `hasSolved` 必须读出来。

    * `isOpen=False` —— 比赛常常**分批放题**。一道还没开放的题如果被排进
      队列，它会占着并发名额、烧模型额度，最后必然失败。抢首杀要的是
      "开放的那一刻就能开始"，而不是"提前扑空"。
    * `hasSolved=True` —— 可能有人在网页上手工交过，或另一个实例解了。
      平台侧这个字段是唯一的事实来源。
    """

    async def scenario() -> Any:
        driver = _driver(_routes({"/ctf/exercise-list": _ok(LIST_PAYLOAD)}))
        try:
            return await driver.fetch_challenges(known_ids=set())
        finally:
            await driver.aclose()

    by_id = {c.remote_id: c for c in _run(scenario())}
    assert by_id["1001"].is_open is True
    assert by_id["1001"].solved is False
    assert by_id["1002"].is_open is False
    assert by_id["1002"].solved is True


def test_list_items_without_an_id_are_skipped() -> None:
    """没有 id 的条目跳过而不是让整次同步炸掉。"""

    async def scenario() -> Any:
        payload = [{"id": 10, "name": "Web", "corpus": [{"name": "匿名题"}, {"id": 3, "name": "ok"}]}]
        driver = _driver(_routes({"/ctf/exercise-list": _ok(payload)}))
        try:
            return await driver.fetch_challenges(known_ids=set())
        finally:
            await driver.aclose()

    challenges = _run(scenario())
    assert [c.remote_id for c in challenges] == ["3"]


# ══════════════════════════════════════════════════════════
# 题目详情（文档 §5）
# ══════════════════════════════════════════════════════════

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
    "endpointType": "monopoly",
    "currentTime": 1780000000000,
    "isNeedCheck": False,
}


def test_detail_score_is_a_string_in_the_doc_and_must_become_an_int() -> None:
    """★★ 文档里 `"score": "100"` —— **字符串**。

    直接塞进本地那个整数列会炸（或者更糟：静默存成 0）。而"某道题分值
    读不出来"这种错很难被发现 —— 它只影响"按分值排队"这个策略，
    表现成"高分题没排在前面"。
    """

    async def scenario() -> Any:
        driver = _driver(_routes({"/ctf/exercise": _ok(DETAIL_PAYLOAD)}))
        try:
            return await driver.fetch_challenge({"exerciseId": 1001})
        finally:
            await driver.aclose()

    challenge = _run(scenario())
    assert challenge is not None
    assert challenge.score == 100
    assert isinstance(challenge.score, int)
    assert challenge.difficulty == "EASY"


def test_detail_attachments_are_parsed() -> None:
    async def scenario() -> Any:
        driver = _driver(_routes({"/ctf/exercise": _ok(DETAIL_PAYLOAD)}))
        try:
            return await driver.fetch_challenge({"exerciseId": 1001})
        finally:
            await driver.aclose()

    challenge = _run(scenario())
    assert challenge is not None
    assert len(challenge.attachments) == 1
    assert challenge.attachments[0].url == "https://example.com/file"
    assert challenge.attachments[0].ext == "zip"


def test_a_challenge_without_an_exercise_id_handle_returns_none() -> None:
    """handle 不对时返回 `None` 而不是抛异常。

    上层用它来决定"要不要走补题路径"，抛异常会让一次无关的寻址失败
    中断整次同步。
    """

    async def scenario() -> Any:
        driver = _driver(_routes({"/ctf/exercise": _ok(DETAIL_PAYLOAD)}))
        try:
            return await driver.fetch_challenge({})
        finally:
            await driver.aclose()

    assert _run(scenario()) is None


# ══════════════════════════════════════════════════════════
# 环境（文档 §5 / §6 / §7）
# ══════════════════════════════════════════════════════════


def _env_payload(*, need_check: bool, endpoints: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload = dict(DETAIL_PAYLOAD)
    payload["isNeedCheck"] = need_check
    payload["endpoints"] = endpoints if endpoints is not None else DETAIL_PAYLOAD["endpoints"]
    return payload


def test_environment_ready_needs_both_conditions() -> None:
    """★★ 文档写明"直到 `isNeedCheck=false` **且** `endpoints` 可用"。

    只认 `isNeedCheck` 会踩到一个真实的坑：平台说"不检查了"，但连接
    信息还是空的 —— 模型会拿到一份空的靶机信息，然后卡在"没有靶机地址"
    上，而它会以为是自己命令写错了。那是最费时间的一种卡法。
    """

    async def scenario() -> None:
        cases = [
            (False, DETAIL_PAYLOAD["endpoints"], True, "两个条件都满足"),
            (True, DETAIL_PAYLOAD["endpoints"], False, "还在准备中"),
            (False, [], False, "不检查了但没给连接信息"),
        ]
        for need_check, endpoints, expected, label in cases:
            payload = _env_payload(need_check=need_check, endpoints=endpoints)

            def handler(request: httpx.Request, _p: dict = payload) -> httpx.Response:
                return httpx.Response(200, json=_ok(_p))

            driver = _driver(handler)
            try:
                env = await driver.read_environment({"exerciseId": 1001})
            finally:
                await driver.aclose()
            assert env.ready is expected, label

    _run(scenario())


def test_endpoint_carries_credentials_and_proxy_mapping() -> None:
    """靶机四要素（IP/端口/账号/代理映射）一个都不能丢。

    丢掉账号密码 = 模型连不上靶机 = 环境白起。
    """

    async def scenario() -> Any:
        driver = _driver(_routes({"/ctf/exercise": _ok(DETAIL_PAYLOAD)}))
        try:
            return await driver.read_environment({"exerciseId": 1001})
        finally:
            await driver.aclose()

    env = _run(scenario())
    assert env.ready is True
    endpoint = env.endpoints[0]
    assert endpoint.expose_ips == ["10.0.0.10"]
    assert endpoint.ports == ["80", "22"]
    assert endpoint.users == [("root", "password")]
    assert endpoint.port_mappings == [{"type": "tcp", "port": "80", "proxy": "30080"}]
    assert endpoint.expire_time is not None


def test_environment_prompt_tells_the_model_how_to_connect() -> None:
    """提示词里必须出现 IP、端口、**账号密码** —— 那三样缺一就连不上。"""

    async def scenario() -> str:
        driver = _driver(_routes({"/ctf/exercise": _ok(DETAIL_PAYLOAD)}))
        try:
            env = await driver.read_environment({"exerciseId": 1001})
            return await driver.environment_prompt(env)
        finally:
            await driver.aclose()

    text = _run(scenario())
    assert "10.0.0.10" in text
    assert "80" in text
    assert "root / password" in text
    assert "30080" in text


def test_environment_prompt_is_empty_without_endpoints() -> None:
    """没有环境（纯附件题）时返回空串，而不是一段"靶机：（无）"。"""

    async def scenario() -> str:
        driver = _driver(_routes({"/ctf/exercise": _ok(_env_payload(need_check=False, endpoints=[]))}))
        try:
            env = await driver.read_environment({"exerciseId": 1001})
            return await driver.environment_prompt(env)
        finally:
            await driver.aclose()

    assert _run(scenario()) == ""


def test_start_and_recover_hit_the_documented_paths() -> None:
    """启动是 `POST /ctf/build-exercise-env`，回收是 `.../recover-exercise-env`。"""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=_ok(None))

    async def scenario() -> None:
        driver = _driver(handler)
        try:
            await driver.start_environment({"exerciseId": 1001})
            await driver.stop_environment({"exerciseId": 1001})
        finally:
            await driver.aclose()

    _run(scenario())
    assert paths[0].endswith("/ctf/build-exercise-env")
    assert paths[1].endswith("/ctf/recover-exercise-env")


def test_recovering_an_environment_that_was_never_started_does_not_raise() -> None:
    """★ 回收一个没起过的环境会报错，而那是**正常**的。

    我们的目的是"别在平台侧留资源"，不是为了拿到成功回执。抛出去会让
    "停止解题"这个动作失败 —— 而它本该总是成功（否则残留的环境会一直
    占着平台侧的配额）。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fail("50002", "环境不存在"))

    async def scenario() -> None:
        driver = _driver(handler)
        try:
            await driver.stop_environment({"exerciseId": 1001})
        finally:
            await driver.aclose()

    _run(scenario())


def test_starting_an_environment_failure_is_not_swallowed() -> None:
    """启动失败**必须**冒出来 —— 与回收相反，它要能被重试。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fail("50001", "资源不足"))

    async def scenario() -> None:
        driver = _driver(handler)
        try:
            with pytest.raises(KernelResponseError):
                await driver.start_environment({"exerciseId": 1001})
        finally:
            await driver.aclose()

    _run(scenario())


# ══════════════════════════════════════════════════════════
# 提交（文档 §3）
# ══════════════════════════════════════════════════════════


def test_a_correct_answer_is_reported_as_solved() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json=_ok({"isCorrect": True}))

    async def scenario() -> Any:
        driver = _driver(handler)
        try:
            return await driver.submit({"exerciseId": 1001}, "flag{test}")
        finally:
            await driver.aclose()

    outcome = _run(scenario())
    assert outcome.accepted is True
    assert outcome.is_solved is True
    # 文档 §3 的 body 形状：`{"exerciseId": 1001, "flag": "example"}`
    assert b'"exerciseId":1001' in bodies[0].replace(b" ", b"")
    assert b'"flag":"flag{test}"' in bodies[0].replace(b" ", b"")


def test_a_wrong_answer_is_accepted_but_not_solved() -> None:
    """★★ "平台收下了并判错" 与 "平台没受理" 必须分开。

    文档只说"答案错误时以返回的 code 和 message 为准"，**没说具体码值** ——
    所以这里的判定是：非限流/鉴权类的失败一律算"收下并判错"。

    为什么默认选这一侧：HTTP 429/401 已经在传输层被单独拦掉了，走到
    这里的非零 code 基本都是业务判定；而反过来（把判错当成没受理）
    会让错误提交次数永远不增长，`max_wrong_submissions` 那道刹车就
    失效了 —— 系统会拿同一个错答案无限重试。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fail("40002", "flag 错误"))

    async def scenario() -> Any:
        driver = _driver(handler)
        try:
            return await driver.submit({"exerciseId": 1001}, "wrong")
        finally:
            await driver.aclose()

    outcome = _run(scenario())
    assert outcome.accepted is True
    assert outcome.is_solved is False
    assert "flag 错误" in outcome.message


def test_a_rate_limit_code_is_not_counted_as_a_wrong_answer() -> None:
    """限流码 → `accepted=False`（**不计错误次数**，那是不可再生资源）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fail("TOO_MANY_REQUESTS", "请求过于频繁"))

    async def scenario() -> Any:
        driver = _driver(handler)
        try:
            return await driver.submit({"exerciseId": 1001}, "x")
        finally:
            await driver.aclose()

    outcome = _run(scenario())
    assert outcome.accepted is False
    assert outcome.is_solved is False


def test_submitting_without_a_handle_raises() -> None:
    """handle 里没有 exerciseId 时**抛异常**而不是猜一个 id 提交。

    猜错的代价是把答案交到另一道题上 —— 那会计一次错误提交，
    而用户完全不知道发生了什么。
    """

    async def scenario() -> None:
        driver = _driver(_routes({}))
        try:
            with pytest.raises(KernelResponseError):
                await driver.submit({}, "flag")
        finally:
            await driver.aclose()

    _run(scenario())


# ══════════════════════════════════════════════════════════
# 平台元信息
# ══════════════════════════════════════════════════════════


def test_answer_max_length_follows_the_document() -> None:
    """文档 §3：`flag` 最长 **256** 字符 —— 比 CTF2 的 1000 短得多。"""

    async def scenario() -> None:
        driver = _driver(_routes({}))
        try:
            assert driver.answer_max_length == 256
        finally:
            await driver.aclose()

    _run(scenario())


def test_this_platform_declares_environment_support() -> None:
    async def scenario() -> None:
        driver = _driver(_routes({}))
        try:
            assert driver.supports_environment is True
            assert driver.slug == "slabmatch"
        finally:
            await driver.aclose()

    _run(scenario())
