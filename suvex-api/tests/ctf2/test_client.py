"""`Ctf2Client` 的行为：覆盖 401/403/429/退避/轮询超时。

## 打桩打在**传输层**，不打在方法上

所有用例都走 `patch_ctf2_transport`（替换模块里的 `httpx.AsyncClient`），
于是 `Ctf2Client` 自己那套 URL 构造、请求头、状态码解释、退避与轮询
**全部真的跑一遍**。直接 mock `client.list_competitions` 会让
"我们到底发了什么请求、怎么理解对方的回应"整块跳过 ——
而那正是这个文件最值得测的部分。与 `tests/test_providers.py`
打 LLM 探测的方式一致。

## 时间不真等

每个涉及等待的用例都用 `fake_clock`：它让 `sleep(n)` 立刻返回、
但把时钟推进 n 秒，所以"300 秒超时"与"退避到上限"都在微秒内跑完，
走的却是与生产**完全相同**的那条代码路径。
"""

from __future__ import annotations

import logging
from datetime import UTC
from typing import Any

import httpx
import pytest

from app.ctf2 import client as ctf2
from tests.ctf2.conftest import (
    TEST_PAT,
    FakeClock,
    json_response,
    patch_ctf2_transport,
    sequence_handler,
)

P = "/api/open/v1/user"


def _client(fake_clock: FakeClock, **overrides) -> ctf2.Ctf2Client:
    """默认参数的客户端。测试要改的只有那几项，其余保持生产形状。"""
    kwargs = {
        "token": TEST_PAT,
        "clock": fake_clock,
        "sleep": fake_clock.sleep,
        # 抖动关掉：带抖动的退避没法断言具体秒数，而"退避是不是真的
        # 指数增长"是这个文件要验的东西之一。抖动本身另有断言。
        "retry": ctf2.RetryPolicy(
            max_attempts=4, base_delay=1.0, max_delay=8.0, jitter_ratio=0.0
        ),
    }
    kwargs.update(overrides)
    return ctf2.Ctf2Client(**kwargs)


# ══════════════════════════════════════════════════════════
# 构造与凭据卫生
# ══════════════════════════════════════════════════════════


def test_client_refuses_to_start_without_a_token() -> None:
    """没凭据就构造不出来。

    放行的后果很具体：每个请求都拿 401，而 401 的补救动作是
    "重新授权" —— 真实原因却是"调用方忘了传 token"。
    那个误导会把排查方向带偏一整轮。
    """
    with pytest.raises(ValueError, match="PAT"):
        ctf2.Ctf2Client(token="")


def test_client_refuses_a_non_official_base_url() -> None:
    """base_url 不是官方域名时**在构造时就炸**。

    这是凭据边界：一个被配错的 base_url 会把 PAT 发到**每一次**请求上。
    等到第一次 401 才发现，PAT 已经出去了。
    """
    with pytest.raises(ctf2.Ctf2Error, match="不在允许范围"):
        ctf2.Ctf2Client(token=TEST_PAT, base_url="https://ctf2-dasctf.evil.example")


def test_official_subdomain_is_allowed() -> None:
    """官方域名的**子域**放行（用 `host == x or host.endswith("." + x)`）。

    这条同时是"后缀匹配写法是错的"的回归测试：写成
    `endswith("dasctf.com")` 会放过 `evil-dasctf.com`（见下一个用例），
    而写成正确形式就能收窄到真子域。
    """
    client = ctf2.Ctf2Client(token=TEST_PAT, base_url="https://api.ctf2.dasctf.com")
    assert client is not None


@pytest.mark.parametrize(
    "url",
    [
        "https://evil-dasctf.com",  # 后缀匹配会错误放行的那一类
        "https://dasctf.com.attacker.net",  # 同上，另一种写法
        "http://169.254.169.254/latest",  # 云元数据
        "ftp://ctf2.dasctf.com/x",  # 协议不对
    ],
)
def test_target_whitelist_rejects_lookalikes(url: str) -> None:
    with pytest.raises(ctf2.Ctf2Error):
        ctf2.assert_allowed_target(url)


def test_target_whitelist_accepts_the_challenge_environment() -> None:
    """本题的 access_url 由调用方传进来 —— 而它必须**逐题**给。"""
    ctf2.assert_allowed_target("http://1.2.3.4:8080", allowed_extra=("1.2.3.4:8080",))
    with pytest.raises(ctf2.Ctf2Error):
        ctf2.assert_allowed_target("http://5.6.7.8:8080", allowed_extra=("1.2.3.4:8080",))


async def test_token_is_only_in_the_header_never_in_the_url(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """★ 凭据卫生：token 只在 `Authorization` 头里。

    URL 进的是 uvicorn 的 access log、反代的 access log、抓包记录 ——
    而 access log 是最常被复制粘贴分享的东西。所以"它在头里"这件事
    必须有回归测试，而不是靠 code review。
    """
    recorded = patch_ctf2_transport(monkeypatch, json_response(200, [{"id": 1, "name": "x"}]))
    async with _client(fake_clock) as client:
        await client.list_competitions()

    assert len(recorded) == 1
    assert recorded.auth_headers() == [f"Bearer {TEST_PAT}"]
    assert TEST_PAT not in recorded.urls[0]


async def test_token_never_appears_in_logs(
    monkeypatch, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """★ 令牌不进日志：连**多次重试**的日志路径也要干净。

    这条针对的是"日志只打 URL 所以安全"这个假设 —— 它不总成立：
    一个 `httpx` 的异常消息里就可能带上请求。这里让平台返回 5xx
    触发重试日志，再断言全文里没有 token。
    """
    patch_ctf2_transport(monkeypatch, json_response(500, {"error": "boom"}))
    with caplog.at_level(logging.DEBUG):
        async with _client(fake_clock) as client:
            with pytest.raises(ctf2.Ctf2Unavailable):
                await client.list_competitions()

    assert TEST_PAT not in caplog.text


# ══════════════════════════════════════════════════════════
# 401 / 403
# ══════════════════════════════════════════════════════════


async def test_401_raises_auth_expired_and_does_not_retry(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """401 → 领域异常，且**一次都不重试**。

    重试 401 是最坏的一种：对着一个已经失效的凭据反复发请求，
    运维看到的可能是"平台一直报错"，而且可能触发平台侧风控。
    """
    recorded = patch_ctf2_transport(
        monkeypatch, json_response(401, {"error": "invalid token"})
    )
    async with _client(fake_clock) as client:
        with pytest.raises(ctf2.Ctf2AuthExpired) as excinfo:
            await client.list_competitions()

    assert len(recorded) == 1, "401 不该重试"
    # 消息必须说"要重新绑定"，而不是泛泛的"调用失败"——
    # 用户看到后者只会一直点重试。
    assert "重新绑定" in str(excinfo.value)


async def test_401_detail_is_redacted(monkeypatch, fake_clock: FakeClock) -> None:
    """detail 带平台原话（排查要用），且**即使平台回显了请求头也不会漏 token**。

    这条测的是 `_safe_excerpt`：它存在的唯一理由就是这类
    "对方把我们的头回显回来"的实现。
    """
    patch_ctf2_transport(
        monkeypatch, json_response(401, {"error": f"bad credentials: Bearer {TEST_PAT}"})
    )
    async with _client(fake_clock) as client:
        with pytest.raises(ctf2.Ctf2AuthExpired) as excinfo:
            await client.list_competitions()

    assert TEST_PAT not in (excinfo.value.detail or "")


async def test_403_carries_the_missing_scope(monkeypatch, fake_clock: FakeClock) -> None:
    """403 → 异常里带出**具体**缺失的 scope。

    只报"权限不足"会让用户去平台上把全部权限翻一遍 ——
    而那些权限项在平台上并不总是自解释的。
    """
    patch_ctf2_transport(
        monkeypatch,
        json_response(
            403, {"error": {"message": "forbidden", "missing_scope": "environment:start"}}
        ),
    )
    async with _client(fake_clock) as client:
        with pytest.raises(ctf2.Ctf2ScopeMissing) as excinfo:
            await client.start_environment(practice_id="1", challenge_id="2")

    assert excinfo.value.missing_scope == "environment:start"
    assert "environment:start" in excinfo.value.describe()


async def test_403_without_a_named_scope_still_works(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """挖不出 scope 时返回 None，**不猜**。

    一个猜错的 scope 比没有更坏：用户会照着去勾一个不相干的权限，
    然后问题照旧，而他会以为是我们看错了。
    """
    patch_ctf2_transport(monkeypatch, json_response(403, {"detail": "no permission"}))
    async with _client(fake_clock) as client:
        with pytest.raises(ctf2.Ctf2ScopeMissing) as excinfo:
            await client.list_practice()

    assert excinfo.value.missing_scope is None
    assert excinfo.value.describe()  # 仍然能给出可展示的一句话


@pytest.mark.parametrize(
    "payload",
    [
        {"missing_scope": ["a:read", "b:write"]},  # 列表形态
        {"error": {"required_scope": "x:y"}},  # 嵌套 + 换名字
        {"data": {"scope": "z:w"}},  # data 下
    ],
)
async def test_403_scope_is_found_in_several_shapes(
    monkeypatch, fake_clock: FakeClock, payload: dict
) -> None:
    """响应形状是**别人家的**，所以尽量认几种 —— 但不认的就说不认。"""
    patch_ctf2_transport(monkeypatch, json_response(403, payload))
    async with _client(fake_clock) as client:
        with pytest.raises(ctf2.Ctf2ScopeMissing) as excinfo:
            await client.list_practice()
    assert excinfo.value.missing_scope


# ══════════════════════════════════════════════════════════
# 429 与 Retry-After
# ══════════════════════════════════════════════════════════


async def test_429_waits_exactly_retry_after_then_succeeds(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """429 → 按 `Retry-After` 等待，然后成功。

    断言的是**具体秒数**（7 秒），不是"等过一会儿"——
    用退避值兜底也能让用例通过，但那样平台给的 `Retry-After`
    就从"被遵守"退化成"被忽略"。
    """
    handler = sequence_handler([(429, {"error": "slow down"}), (200, [{"id": 1}])])

    def with_header(request: httpx.Request) -> httpx.Response:
        response = handler(request)
        if response.status_code == 429:
            response.headers["Retry-After"] = "7"
        return response

    patch_ctf2_transport(monkeypatch, with_header)
    async with _client(fake_clock) as client:
        result = await client.list_competitions()

    assert result == [{"id": 1}]
    assert fake_clock.sleeps == [7.0]


async def test_429_without_retry_after_falls_back_to_backoff(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """没有 `Retry-After` 时退回退避，**不是 0 秒**。

    等 0 秒等于立刻重试 —— 那会把限流打成更严重的限流。
    """
    patch_ctf2_transport(monkeypatch, sequence_handler([(429, {}), (200, [])]))
    async with _client(fake_clock) as client:
        await client.list_competitions()

    assert fake_clock.sleeps == [1.0]  # base_delay


async def test_retry_after_as_http_date_is_understood(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """`Retry-After` 也可能是 HTTP-date（RFC 9110 允许两种）。

    只认秒数的话，平台改用 date 形态时我们会**静默退回退避** ——
    实际等待远短于要求，然后被继续 429，而日志里看不出原因。
    """
    from datetime import datetime, timedelta
    from email.utils import format_datetime

    moment = datetime.now(UTC) + timedelta(seconds=11)

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(429, json={}, request=request)
        response.headers["Retry-After"] = format_datetime(moment, usegmt=True)
        return response

    patch_ctf2_transport(monkeypatch, handler)
    async with _client(fake_clock, retry=ctf2.RetryPolicy(max_attempts=1)) as client:
        with pytest.raises(ctf2.Ctf2RateLimited) as excinfo:
            await client.list_competitions()

    # date 形态会有几百毫秒的解析误差（now 与 moment 之间真实流逝），
    # 所以断言区间而不是精确值。
    assert excinfo.value.retry_after is not None
    assert 9.0 <= excinfo.value.retry_after <= 12.0


async def test_huge_retry_after_is_refused_instead_of_slept(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """★ 一个 86400 秒的 `Retry-After` 不会被真的等。

    干等一天会把 worker 挂死一整天，而请求方早就超时走了 ——
    那是资源泄漏，不是"尊重平台"。尊重在这里的正确表达是
    "停下来，把这个情况报给人"。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(429, json={}, request=request)
        response.headers["Retry-After"] = "86400"
        return response

    patch_ctf2_transport(monkeypatch, handler)
    async with _client(fake_clock, retry=ctf2.RetryPolicy(max_retry_after=120.0)) as client:
        with pytest.raises(ctf2.Ctf2RateLimited) as excinfo:
            await client.list_competitions()

    assert fake_clock.sleeps == [], "不该真的睡下去"
    assert excinfo.value.retry_after == 86400.0


async def test_429_exhausting_attempts_raises_rate_limited(
    monkeypatch, fake_clock: FakeClock
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={}, headers={"Retry-After": "1"}, request=request)

    patch_ctf2_transport(monkeypatch, handler)
    async with _client(fake_clock, retry=ctf2.RetryPolicy(max_attempts=3, base_delay=1.0)) as client:
        with pytest.raises(ctf2.Ctf2RateLimited):
            await client.list_competitions()

    assert len(fake_clock.sleeps) == 2  # 3 次尝试之间睡 2 次


# ══════════════════════════════════════════════════════════
# 指数退避
# ══════════════════════════════════════════════════════════


async def test_5xx_backoff_is_exponential(monkeypatch, fake_clock: FakeClock) -> None:
    """★ 退避**指数**增长：1 → 2 → 4。

    断言的是这一串精确数字。写成"至少等过 1 秒"这种松断言，
    退化成固定间隔也能通过 —— 而固定间隔正是要把这个问题修掉的东西：
    平台侧抖了 30 秒，固定 1 秒重试会打 30 次，每次都在它还没恢复时。
    """
    patch_ctf2_transport(monkeypatch, json_response(503, {"error": "down"}))
    async with _client(
        fake_clock,
        retry=ctf2.RetryPolicy(max_attempts=4, base_delay=1.0, max_delay=60.0, jitter_ratio=0.0),
    ) as client:
        with pytest.raises(ctf2.Ctf2Unavailable):
            await client.list_competitions()

    assert fake_clock.sleeps == [1.0, 2.0, 4.0]


async def test_backoff_is_capped_at_max_delay(monkeypatch, fake_clock: FakeClock) -> None:
    """退避有**上限** —— 否则第 10 次重试要等 512 秒。"""
    patch_ctf2_transport(monkeypatch, json_response(500, {}))
    async with _client(
        fake_clock,
        retry=ctf2.RetryPolicy(max_attempts=6, base_delay=1.0, max_delay=4.0, jitter_ratio=0.0),
    ) as client:
        with pytest.raises(ctf2.Ctf2Unavailable):
            await client.list_competitions()

    assert fake_clock.sleeps == [1.0, 2.0, 4.0, 4.0, 4.0]
    assert max(fake_clock.sleeps) <= 4.0


def test_backoff_jitter_spreads_retries() -> None:
    """★ 抖动**必须非零**，且落在配置的比例内。

    完全确定的退避会让所有并发调用在同一毫秒醒来，形成雪崩式重试 ——
    而那种模式恰好会再次触发平台限流，于是永远好不了。

    这里用 `random.Random(seed)` 让断言可复现：测的是"抖动生效且成比例"，
    不是"随机数等于某个值"。
    """
    import random

    policy = ctf2.RetryPolicy(base_delay=10.0, max_delay=1000.0, jitter_ratio=0.25)
    rng = random.Random(20260928)
    samples = [policy.backoff(1, rng=rng) for _ in range(50)]

    assert len(set(samples)) > 1, "抖动没生效"
    assert all(7.5 <= s <= 12.5 for s in samples)
    # 均值要接近原值 —— 抖动是"打散"，不是"整体变慢"。
    assert 9.0 <= sum(samples) / len(samples) <= 11.0


def test_zero_jitter_is_allowed_for_deterministic_tests() -> None:
    """抖动能关掉（测试要断言精确秒数）。关掉时退避仍是纯指数。"""
    policy = ctf2.RetryPolicy(base_delay=1.0, max_delay=100.0, jitter_ratio=0.0)
    assert [policy.backoff(n) for n in (1, 2, 3)] == [1.0, 2.0, 4.0]


# ══════════════════════════════════════════════════════════
# 网络异常与响应形状
# ══════════════════════════════════════════════════════════


async def test_network_error_is_retried_then_reported(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """连接失败要重试（链路抖动很常见），用尽后抛 `Ctf2Unavailable`。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    patch_ctf2_transport(monkeypatch, handler)
    async with _client(
        fake_clock, retry=ctf2.RetryPolicy(max_attempts=3, base_delay=1.0, jitter_ratio=0.0)
    ) as client:
        with pytest.raises(ctf2.Ctf2Unavailable):
            await client.list_competitions()

    assert fake_clock.sleeps == [1.0, 2.0]


async def test_4xx_other_than_auth_is_not_retried(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """404 / 400 这类**不重试** —— "请求错的或平台改了接口"，重试没有意义。"""
    recorded = patch_ctf2_transport(monkeypatch, json_response(404, {"error": "nope"}))
    async with _client(fake_clock) as client:
        with pytest.raises(ctf2.Ctf2ResponseError):
            await client.get_practice_challenge("1", "2")

    assert len(recorded) == 1
    assert fake_clock.sleeps == []


async def test_corrupt_json_is_parsed_as_plain_text(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """2xx 但不是 JSON —— **不报错**，当纯文本收下。

    CTF2 的提交端点在某些情况下返回纯文本。硬要求 JSON 会把一次
    **成功的提交**判成失败，而后果是重复提交（扣额度）——
    这个方向的错误比"少解析一层"贵得多。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok, flag accepted", request=request)

    patch_ctf2_transport(monkeypatch, handler)
    async with _client(fake_clock) as client:
        result = await client.get_practice_challenge("1", "2")

    assert result == {"raw_text": "ok, flag accepted"}


async def test_empty_body_is_accepted(monkeypatch, fake_clock: FakeClock) -> None:
    """204 空体是合法的（DELETE 类端点常见）。

    对空体报错会让"停止环境"这种端点在平台改成空响应时**全部失败**，
    而环境不释放的代价是实实在在占着平台资源。
    """
    patch_ctf2_transport(
        monkeypatch, lambda request: httpx.Response(204, content=b"", request=request)
    )
    async with _client(fake_clock) as client:
        await client.stop_environment(practice_id="1", challenge_id="2")


# ══════════════════════════════════════════════════════════
# 提交：不幂等，所以重试策略不同
# ══════════════════════════════════════════════════════════


async def test_submit_is_not_retried_on_network_failure(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """★ 提交在**不确定送达**的失败上不重试。

    一次提交被平台收到就算数（对的记为解出，错的**扣额度**）。
    响应超时后重试，而请求其实已经送达，就白扣一次额度 ——
    而额度是不可再生的。所以这里宁可漏、不可重：
    让它留在 `pending`，由 `list_stage_submissions` 对账。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    recorded = patch_ctf2_transport(monkeypatch, handler)
    async with _client(fake_clock) as client:
        with pytest.raises(ctf2.Ctf2Unavailable):
            await client.submit_flag(practice_id="1", challenge_id="2", flag="flag{x}")

    assert len(recorded) == 1
    assert fake_clock.sleeps == []


async def test_submit_sends_sub_flag_id_only_when_present(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """套题才带 `sub_flag_id`；普通题**不带这个键**。

    带一个空的 `sub_flag_id` 与不带它是两件事：前者会让平台去校验
    一个不存在的套题位置，而报错看起来像"题目配置有问题"。
    """
    import json as jsonlib

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(jsonlib.loads(request.content or b"{}"))
        return httpx.Response(200, json={"correct": True, "id": 99}, request=request)

    patch_ctf2_transport(monkeypatch, handler)
    async with _client(fake_clock) as client:
        await client.submit_flag(practice_id="1", challenge_id="2", flag="flag{a}")
        await client.submit_flag(
            practice_id="1", challenge_id="2", flag="flag{b}", sub_flag_id="sf_1"
        )

    # `confirmation` 恒为 True —— 它不是可选项：官方的 OpenAPI 文档
    # 把它列为必填，而实测（2026-09-29，真实平台）不带它时直接
    # 400 `INVALID_REQUEST`，`params` 指向 `{confirmation: true}`。
    assert bodies[0] == {"flag": "flag{a}", "confirmation": True}
    assert bodies[1] == {"flag": "flag{b}", "confirmation": True, "sub_flag_id": "sf_1"}


# ══════════════════════════════════════════════════════════
# 环境启动：幂等轮询
# ══════════════════════════════════════════════════════════


async def test_environment_polls_until_access_ready(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """轮询到 `access_ready=true` 为止，返回地址。"""
    handler = sequence_handler(
        [
            (200, {"access_ready": False}),
            (200, {"access_ready": False}),
            (
                200,
                {
                    "access_ready": True,
                    "access_url": "http://1.2.3.4:8080",
                    "access_urls": ["http://1.2.3.4:8080", "1.2.3.4:9999"],
                },
            ),
        ]
    )
    recorded = patch_ctf2_transport(monkeypatch, handler)
    async with _client(fake_clock) as client:
        access = await client.start_environment_until_ready(
            practice_id="1", challenge_id="2", interval_seconds=3.0
        )

    assert access.access_url == "http://1.2.3.4:8080"
    assert access.access_urls == ("http://1.2.3.4:8080", "1.2.3.4:9999")
    # 3 次 start 调用 —— 每次迭代都是一次**幂等**的 start，
    # 这是平台的设计（没有单独的"查状态"端点）。
    assert len(recorded) == 3
    assert recorded.methods == ["POST", "POST", "POST"]
    assert fake_clock.sleeps == [3.0, 3.0]


async def test_environment_404_means_the_challenge_has_no_target(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """★ 纯附件题的环境端点返回 404 —— 那不是错误，是"这题没有靶机"。

    实测（2026-09-29，真实平台，`practice=BUUCTF` 的 hashcat 题）：

        GET  …/challenges/{challengeId}/                   → 200（题目确实在）
        POST …/challenges/{challengeId}/environment/start/ → 404
             {"params": {"resource": "practice_challenge"}}

    同一个组合上详情读得到、环境端点却 404。以前这个 404 一路抛到
    worker，结果是**所有纯附件题一道都解不了**（实测症状：耗时 0.8s、
    提交记录 0 条），而 MISC / CRYPTO 里的多数题都是这一类。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "error": {
                    "code": "NOT_FOUND",
                    "key": "errors.common.not_found",
                    "params": {"resource": "practice_challenge"},
                },
                "success": False,
            },
            request=request,
        )

    recorded = patch_ctf2_transport(monkeypatch, handler)
    async with _client(fake_clock) as client:
        access = await client.start_environment_until_ready(
            practice_id="1", challenge_id="2"
        )

    # 只发了一次就返回 —— 没有退化成"轮询 200 次直到超时"
    assert len(recorded) == 1
    assert access.access_url is None
    assert access.access_urls == ()
    assert access.raw.get("environment") == "none"


async def test_environment_urls_come_from_objects(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """★★ 环境地址是**对象数组**时必须取出 `url`，不能 `str()` 成 dict repr。

    实测（2026-10-01，[护网杯 2018]easy_tornado）—— CTF2 的真实返回：

        "access_urls": [
            {"nc_ssl": null, "type": "http", "url": "http://xxx.http-ctf2.dasctf.com:80"}
        ]

    旧实现 `str(v)` 把它变成：
        "{'nc_ssl': None, 'type': 'http', 'url': 'http://xxx…:80'}"

    那一个 `str()` 同时打穿两条链路，而**两条都不报错**：

    · `allowed_hosts()` 抽不出主机名 → 允许集合为空 →
      模型对靶机的每一次请求都被拒；
    · `_initial_prompt()` 把同一份值渲染进提示词 → 模型看到的
      "可访问地址"就是那串垃圾，它**根本不知道靶机在哪**。

    症状极具误导性：环境记录正常、`access_urls` 非空、界面一切正常，
    只有模型那边全军覆没 —— 看起来像"模型太笨"。
    """
    payload = {
        "access_ready": True,
        "access_urls": [
            {
                "nc_ssl": None,
                "type": "http",
                "url": "http://38b82f9b4622989b93e1f0f3.http-ctf2.dasctf.com:80",
            }
        ],
    }
    patch_ctf2_transport(monkeypatch, json_response(200, payload))
    async with _client(fake_clock) as client:
        access = await client.start_environment_until_ready(
            practice_id="1", challenge_id="2"
        )

    assert access.access_urls == (
        "http://38b82f9b4622989b93e1f0f3.http-ctf2.dasctf.com:80",
    )
    # ★ 关键断言：**绝不能**是 dict 的 repr
    assert "nc_ssl" not in access.access_urls[0]
    assert "{" not in access.access_urls[0]


async def test_environment_url_object_without_url_is_dropped(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """对象里取不出 `url` 时**丢弃**，绝不退回 `str(dict)`。

    宁可少一个地址，也不要把一句"看起来像地址的垃圾"灌进白名单和提示词 ——
    后者不会报错，只会让模型对着它干瞪眼（见上一条用例的事故）。
    """
    patch_ctf2_transport(
        monkeypatch,
        json_response(200, {"access_ready": True, "access_urls": [{"nc_ssl": None}]}),
    )
    async with _client(fake_clock) as client:
        access = await client.start_environment_until_ready(
            practice_id="1", challenge_id="2"
        )
    assert access.access_urls == ()


async def test_environment_other_4xx_still_raises(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """★ 只有 404 被解释成"没有靶机"，别的 4xx 必须照旧抛。

    放宽成"任何 4xx 都当没环境"会把真正的错误（参数错、权限错）
    悄悄吞掉，症状是"解题莫名其妙地跳过了环境那一步"。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"code": "INVALID_REQUEST"}}, request=request
        )

    patch_ctf2_transport(monkeypatch, handler)
    async with _client(fake_clock) as client:
        with pytest.raises(ctf2.Ctf2ResponseError) as info:
            await client.start_environment_until_ready(practice_id="1", challenge_id="2")

    # 状态码必须被带出来：调用方靠它分类，而不是靠 message 文案。
    assert info.value.status_code == 400


@pytest.mark.parametrize(
    "payload",
    [
        {"accessReady": True},  # 驼峰
        {"environment": {"access_ready": True}},  # 嵌在子对象里
        {"access_url": "http://1.2.3.4:1"},  # 兜底：有地址就算就绪
    ],
)
async def test_environment_ready_is_recognised_in_several_shapes(
    monkeypatch, fake_clock: FakeClock, payload: dict
) -> None:
    """就绪信号认三种形状，且**有 access_url 也算就绪**。

    最后那条兜底是必要的：如果某个端点版本从不返回 `access_ready`，
    只认那个字段会让轮询永远超时 —— 而地址其实就在响应里。
    """
    patch_ctf2_transport(monkeypatch, json_response(200, payload))
    async with _client(fake_clock) as client:
        access = await client.start_environment_until_ready(
            practice_id="1", challenge_id="2"
        )
    assert access.raw == payload


async def test_environment_poll_times_out_on_wall_clock(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """★ 总超时：平台一直说"还没好"时我们必须能停下来。

    异常类型是 `Ctf2EnvironmentTimeout` 而不是"启动失败"——
    环境**可能还在起**，补救动作是"过一会儿再启动一次"（幂等，安全），
    不是"去看日志"。
    """
    patch_ctf2_transport(monkeypatch, json_response(200, {"access_ready": False}))
    async with _client(fake_clock) as client:
        with pytest.raises(ctf2.Ctf2EnvironmentTimeout) as excinfo:
            await client.start_environment_until_ready(
                practice_id="1",
                challenge_id="2",
                total_timeout_seconds=10.0,
                interval_seconds=3.0,
                max_polls=1000,
            )

    assert excinfo.value.elapsed_seconds >= 10.0
    assert excinfo.value.polls >= 3
    # 没有真的等 10 秒 —— 假时钟把这段压缩到了微秒。
    assert fake_clock.total_slept >= 10.0


async def test_environment_poll_stops_at_max_polls(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """★ 次数上限是**独立**于墙钟的第二道保险。

    没有它：平台每次立刻返回 false 时，要等满墙钟才能停下 ——
    那会变成上百次请求打在同一秒上，平台角度看就是攻击。
    """
    patch_ctf2_transport(monkeypatch, json_response(200, {"access_ready": False}))
    async with _client(fake_clock) as client:
        with pytest.raises(ctf2.Ctf2EnvironmentTimeout) as excinfo:
            await client.start_environment_until_ready(
                practice_id="1",
                challenge_id="2",
                total_timeout_seconds=10_000.0,  # 故意放到够不着
                interval_seconds=1.0,
                max_polls=4,
            )

    assert excinfo.value.polls == 4
    assert "次数" in str(excinfo.value)


async def test_environment_poll_sleep_is_clamped_by_the_deadline(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """最后一轮只睡到**截止时刻**，不睡满一个完整间隔。

    睡满会在最后一轮多等一个 interval —— 那是白白拖长的总时长
    （3 秒级的可见差异，而用户就盯着那个转圈）。
    """
    patch_ctf2_transport(monkeypatch, json_response(200, {"access_ready": False}))
    async with _client(fake_clock) as client:
        with pytest.raises(ctf2.Ctf2EnvironmentTimeout):
            await client.start_environment_until_ready(
                practice_id="1",
                challenge_id="2",
                total_timeout_seconds=5.0,
                interval_seconds=3.0,
                max_polls=100,
            )

    # 3.0 + 2.0（被截止夹住），而不是 3.0 + 3.0。
    assert fake_clock.sleeps == [3.0, 2.0]


async def test_environment_start_failure_is_propagated_not_swallowed(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """轮询里出现 401/403 时**立刻抛出**，不继续轮询。

    吞掉它继续轮询会得到一个"转 5 分钟然后超时"的体验，
    而真正的原因（凭据失效）在一开始就确定了 ——
    用户白等 5 分钟才看到一句"环境启动超时"，方向完全错。
    """
    patch_ctf2_transport(monkeypatch, json_response(401, {"error": "expired"}))
    async with _client(fake_clock) as client:
        with pytest.raises(ctf2.Ctf2AuthExpired):
            await client.start_environment_until_ready(
                practice_id="1", challenge_id="2", total_timeout_seconds=600.0
            )


async def test_environment_429_does_not_abandon_the_whole_poll(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """★★ 起环境途中被限流时**继续轮询**，而不是让整道题失败。

    实测（2026-10-01，用户一次点 6 道题）：4 道卡在**起环境**这一步，
    界面显示「启动失败 · CTF2 限流（429），已重试到上限」，而每一道都是
    `turns=0` —— 一个 token 都没花在解题上。

    根因：`_request` 在 4 次重试后抛 `Ctf2RateLimited`，而当时的轮询
    循环只捕了 404，429 直接穿出去，把 **300 秒的预算**整个作废。
    换句话说，我们因为"被要求等 1 秒"而放弃了"还能等 300 秒"的机会。

    这里构造的正是那个形状：先连续 429（每次带 `Retry-After: 1`），
    然后环境就绪。修复前它会抛 `Ctf2RateLimited`；修复后应当拿到地址。
    """
    # `_request` 对 429 会自己重试 `max_attempts` 次才抛 —— 所以要喂
    # 足够多的 429 让它先抛出来，再由**轮询循环**接手继续等。
    responses: list[Any] = [(429, {"error": {"code": "RATE_LIMIT_EXCEEDED"}})] * 4
    responses.append((200, {"access_ready": False}))
    responses.append(
        (
            200,
            {
                "access_ready": True,
                "access_url": "http://1.2.3.4:8080",
                "access_type": "http",
            },
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        status, payload = responses.pop(0) if responses else (200, {"access_ready": True})
        headers = {"retry-after": "1"} if status == 429 else {}
        return httpx.Response(status, json=payload, headers=headers, request=request)

    recorded = patch_ctf2_transport(monkeypatch, handler)
    async with _client(fake_clock) as client:
        access = await client.start_environment_until_ready(
            practice_id="1",
            challenge_id="2",
            total_timeout_seconds=300.0,
            interval_seconds=3.0,
        )

    assert access.access_url == "http://1.2.3.4:8080", "限流过后环境仍然起来了"
    # 4 次撞墙 + 2 次成功轮询：限流期间的调用**没有**被当成"轮询过了"
    assert len(recorded) == 6
    # 限流那一段每次按 Retry-After 等 1 秒 —— 而不是按 3 秒的轮询间隔。
    # 平台说了等 1 秒就够，多等是白等。
    assert fake_clock.sleeps[:3] == [1.0, 1.0, 1.0]


async def test_environment_429_still_gives_up_at_the_wall_clock(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """★ 耐心**不是无限的**：一直 429 到墙钟用尽，仍然抛超时。

    这条测试守的是上一条的边界。`polls` 在限流路径上不增加（一次 429
    什么都没问出来），所以循环顶部那个 `polls >= max_polls` **管不到**
    它 —— 唯一的护栏是墙钟。把这条钉住，免得将来有人以为"429 会自己
    停下来"。

    抛的必须是 `Ctf2EnvironmentTimeout`（"可能还在起，过会儿再试"），
    而**不是** `Ctf2RateLimited`（"别试了"）—— 两者的补救动作不同。
    """
    patch_ctf2_transport(
        monkeypatch,
        json_response(
            429,
            {"error": {"code": "RATE_LIMIT_EXCEEDED"}},
            headers={"retry-after": "1"},
        ),
    )
    async with _client(fake_clock) as client:
        with pytest.raises(ctf2.Ctf2EnvironmentTimeout):
            await client.start_environment_until_ready(
                practice_id="1", challenge_id="2", total_timeout_seconds=10.0
            )


# ══════════════════════════════════════════════════════════
# 令牌桶限流
# ══════════════════════════════════════════════════════════


async def test_token_bucket_serves_the_burst_without_waiting() -> None:
    """桶容量内的请求**不等**。

    一次同步会连着发十几个请求。让它们全部排队会白白慢一个数量级，
    而平台在意的是长期速率。
    """
    clock = FakeClock()
    bucket = ctf2.TokenBucket(2.0, 5, clock=clock, sleep=clock.sleep)
    for _ in range(5):
        assert await bucket.acquire() == 0.0
    assert clock.sleeps == []


async def test_token_bucket_throttles_beyond_the_burst() -> None:
    """超过桶容量就排队，且等待时间 = 补满一个令牌的时间。"""
    clock = FakeClock()
    bucket = ctf2.TokenBucket(2.0, 1, clock=clock, sleep=clock.sleep)
    await bucket.acquire()
    waited = await bucket.acquire()
    assert waited == pytest.approx(0.5)  # 2 个/秒 → 一个令牌 0.5 秒
    assert clock.sleeps == [0.5]


async def test_token_bucket_rate_covers_a_sync_burst(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """端到端：一次同步打十几个请求时**确实**被限速。

    单测桶本身只是"算法对"，这条测的是"客户端真的用了它"——
    少一个 `await self._bucket.acquire()` 就会让这条红，
    而算法单测不会。
    """
    patch_ctf2_transport(monkeypatch, json_response(200, {"id": 1}))
    async with _client(fake_clock, rate_per_second=1.0, burst=1) as client:
        for _ in range(4):
            await client.get_practice_challenge("1", "2")

    # 首次 0 秒，之后每次等 1 秒（rate=1/s, burst=1）。
    assert fake_clock.sleeps == [1.0, 1.0, 1.0]


def test_token_bucket_rejects_impossible_parameters() -> None:
    with pytest.raises(ValueError):
        ctf2.TokenBucket(0, 1)
    with pytest.raises(ValueError):
        ctf2.TokenBucket(1.0, 0)


# ══════════════════════════════════════════════════════════
# URL 与端点形状
# ══════════════════════════════════════════════════════════


async def test_endpoint_paths_match_the_platform_contract(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """★ 端点路径的对账测试。

    路径拼错是这一层最可能的 bug，而它的症状是 404 ——
    会被 `Ctf2ResponseError` 报成"平台接口可能变更了"，
    排查方向直接跑到"平台改接口了"那里去。
    所以把全部路径写成一张表断言一次。
    """
    recorded = patch_ctf2_transport(monkeypatch, json_response(200, {}))
    async with _client(fake_clock) as client:
        await client.list_competitions()
        await client.competition_status("1")
        await client.list_stages("1")
        await client.list_stage_challenges("s1")
        await client.list_stage_submissions("s1")
        await client.list_practice()
        await client.get_practice_challenge("1", "2")
        await client.start_environment(practice_id="1", challenge_id="2")
        await client.stop_environment(practice_id="1", challenge_id="2")
        await client.submit_flag(practice_id="1", challenge_id="2", flag="flag{a}")

    assert recorded.paths == [
        f"{P}/competitions/",
        f"{P}/competitions/1/status/",
        f"{P}/competitions/1/stages/",
        f"{P}/stages/s1/challenges/",
        f"{P}/stages/s1/submissions/",
        f"{P}/practice/",
        f"{P}/practice/1/challenges/2/",
        f"{P}/practice/1/challenges/2/environment/start/",
        f"{P}/practice/1/challenges/2/environment/",
        f"{P}/practice/1/challenges/2/submit/",
    ]
    assert recorded.methods == [
        "GET",
        "GET",
        "GET",
        "GET",
        "GET",
        "GET",
        "GET",
        "POST",
        "DELETE",
        "POST",
    ]


async def test_list_accepts_a_bare_array(monkeypatch, fake_clock: FakeClock) -> None:
    """裸列表（`[{...}]`）。"""
    patch_ctf2_transport(monkeypatch, json_response(200, [{"id": 2}]))
    async with _client(fake_clock) as client:
        assert await client.list_competitions() == [{"id": 2}]


async def test_list_accepts_a_results_wrapper(monkeypatch, fake_clock: FakeClock) -> None:
    """`{"results": [...]}` 这种分页包法。

    ⚠️ 分页本身**不在这里做** —— 那是 `service.py` 的事
    （它知道"要同步多少"）。这一层只负责把一页的形状抹平。

    ⚠️ 与上一条**是两个独立的用例**，不是同一个函数里 patch 两次：
    `patch_ctf2_transport` 内部会先读一次当前的 `httpx.AsyncClient` 作为
    "真实类"，第二次 patch 时读到的已经是**第一次装上的 factory** ——
    于是 handler 被套了两层，第二次的响应里混着第一次的桩。
    实测撞到过，症状是"第二个响应返回了第一个的数据"，而报错完全不提 patch。
    """
    patch_ctf2_transport(monkeypatch, json_response(200, {"results": [{"id": 1}]}))
    async with _client(fake_clock) as client:
        assert await client.list_competitions() == [{"id": 1}]


def test_list_accepts_the_real_platform_envelope() -> None:
    """★ 平台**真实**的形状：`{"data": {"items": [...], "total": N}, "success": true}`。

    这一条是用真 PAT 打出来的（`scripts/probe_ctf2_endpoints.py` 会打同样
    的东西）。在修 `_as_list` 之前，这个形状**每一次同步都静默变成空表**：

        同步完成：赛事 0 · 新增题目 0

    而用户看到的是"同步不了题目" —— 一个不报错、只是什么都没有的失败。
    根因是原来的实现只在**顶层**找容器键，而列表在 `data` 里面。

    ⚠️ 同时钉住"不做随便找一个列表的兜底"：见下一条。
    """
    payload = {
        "data": {"items": [{"id": 1}, {"id": 2}], "total": 2},
        "success": True,
    }
    assert ctf2._as_list(payload) == [{"id": 1}, {"id": 2}]  # noqa: SLF001 - 测的就是它


def test_list_does_not_guess_when_there_is_no_container() -> None:
    """★ 认不出时返回空表，**绝不**"信封里只有一个列表就用它"。

    那个兜底很诱人，但 `{"data": {"scopes": [...]}}` 会被当成题目列表 ——
    库里多出一堆垃圾行，而日志一声不响。猜错的方向是"数据看起来有了、
    但全是错的"，比"什么都没有"难查得多。

    所以这条用**只有一个列表**的信封来试：它必须仍然返回空表。
    """
    assert ctf2._as_list({"data": {"scopes": ["read", "scan"]}}) == []  # noqa: SLF001
    assert ctf2._as_list({"data": {"roles": [{"name": "admin"}]}}) == []  # noqa: SLF001


def test_team_membership_error_is_explained_in_plain_words() -> None:
    """★ 平台给的是 `errors.team.membership_required`，用户要看到的是"该做什么"。

    这一条有出处：用真 PAT 调 `/competitions/{id}/stages/` 与
    `/competitions/{id}/status/` 时**实测**得到这个 403 ——
    那场赛事是团队赛，而这个 PAT 对应的账号没加入任何战队。

    平台不会说"请去加入战队"，它只说一个 i18n 键。而那是"为什么同步不了
    赛事题目"的唯一原因 —— 如果不能翻成人话，用户只能看到一串英文。
    """
    hint = ctf2._identity_hint(  # noqa: SLF001
        ("TEAM_MEMBERSHIP_REQUIRED", "errors.team.membership_required")
    )
    assert "TEAM_MEMBERSHIP_REQUIRED" in hint, "原始标记要保留（便于对照平台文档）"
    assert "战队" in hint, "没有给出用户能执行的那一步"


def test_unknown_error_key_is_only_quoted_never_invented() -> None:
    """★ 没见过的键**原样引用**，不编一句解释。

    编错的解释比没有解释更坏：用户会照着它去改一个不相干的地方。
    """
    hint = ctf2._identity_hint(("SOMETHING_NEW", "errors.new.thing"))  # noqa: SLF001
    assert "SOMETHING_NEW" in hint
    assert "战队" not in hint and "权限不足" not in hint


async def test_unknown_list_shape_returns_empty_instead_of_raising(
    monkeypatch, fake_clock: FakeClock, caplog
) -> None:
    """认不出的形状返回空表 + 一条警告，**不抛异常**。

    抛异常会让整次同步失败，连赛事都同步不下来；返回空表时
    用户看到的是"这场赛事没有题目"，还可以自己判断。

    ⚠️ 但那条警告必须带上**信封内层的键名** —— 否则下次平台改形状时，
    排查的人手上只有一句"形状不认识"，还得自己去猜该看哪一层
    （这正是 `data.items` 那次踩到的：日志说了顶层键，没说 `data` 里有什么）。
    """
    patch_ctf2_transport(monkeypatch, json_response(200, {"weird": 1}))
    with caplog.at_level(logging.WARNING, logger="suvex.ctf2"):
        async with _client(fake_clock) as client:
            assert await client.list_competitions() == []

    assert "信封内层" in caplog.text, "警告里没有说清信封里有什么键"


async def test_data_wrapper_is_unwrapped_for_object_responses(
    monkeypatch, fake_clock: FakeClock
) -> None:
    """`{"data": {...}}` 这种包装要剥掉 —— 否则上层读到的全是 None。"""
    patch_ctf2_transport(
        monkeypatch, json_response(200, {"data": {"id": "7", "title": "签到题"}})
    )
    async with _client(fake_clock) as client:
        result = await client.get_practice_challenge("1", "7")
    assert result == {"id": "7", "title": "签到题"}


# ══════════════════════════════════════════════════════════
# ★★ 夹具不许污染全局 httpx（2026-10-16 那 51 条回归的闸）
# ══════════════════════════════════════════════════════════
#
# 背景：`app.ctf2.client.httpx` 与 `app.providers.llm.httpx` **都是全局
# 那一个 httpx 模块对象**（实测 `app.ctf2.client.httpx is httpx` → True）。
# 所以 `monkeypatch.setattr("…httpx.AsyncClient", factory)` 改的是
# **httpx 模块自己的属性**，于是任何在测试期间读 `httpx.AsyncClient`
# 的代码都会拿到工厂函数 —— `netguard.ensure_guarded` 里那句
# `isinstance(client, httpx.AsyncClient)` 直接
# `TypeError: arg 2 must be a type`，**51 条用例全倒**。
#
# 修法是把补丁打在**模块绑定**上（换 shim），真模块一个属性都不动。
# 下面几条把这个性质钉住。


def test_the_fixture_does_not_pollute_the_global_httpx_module(
    monkeypatch,
) -> None:
    """★★ 夹具生效期间，全局 `httpx.AsyncClient` **必须仍是真类**。

    ## 这条是那个 51 条回归的回归闸

    判据很直接：`isinstance(真类, type)` 为 `True`。被替换成工厂函数时
    它是 `False`（工厂是 function，不是 type），于是
    `isinstance(client, httpx.AsyncClient)` 会抛 `TypeError`。

    ⚠️ **这条在污染版下必须是红的**（实测过），否则它证明不了任何事。

    ⚠️ 同时断言"夹具**确实生效了**"：只验"没污染"的话，一个
    "什么都不替换"的夹具也能过 —— 而它会让所有走 mock 的用例真的发请求。
    """
    patch_ctf2_transport(monkeypatch, json_response(200, {}))

    # ① 污染判据：全局 httpx 模块里的名字必须仍是类
    assert isinstance(httpx.AsyncClient, type), (
        "夹具把全局 `httpx.AsyncClient` 替换成了非类型对象 —— "
        "任何在测试期间读这个名字的代码（如 netguard 的 isinstance）都会炸"
    )
    assert httpx.AsyncClient.__name__ == "AsyncClient"
    # 同一族的其它名字也不该被动
    assert isinstance(httpx.Client, type)
    assert isinstance(httpx.HTTPError, type)

    # ② 夹具确实生效：目标模块里的绑定**已经**是工厂
    from app.ctf2 import client as client_mod

    assert client_mod.httpx.AsyncClient is not httpx.AsyncClient, (
        "夹具没生效 —— 它应当替换 `app.ctf2.client` 里的那个绑定"
    )
    # 而它转发的名字仍是真的（漏转发会引入新的假失败）
    assert client_mod.httpx.HTTPError is httpx.HTTPError
    assert client_mod.httpx.Response is httpx.Response
    assert client_mod.httpx.Timeout is httpx.Timeout


def test_the_fixture_shim_forwards_every_other_httpx_name(
    monkeypatch,
) -> None:
    """★ shim 必须**全量转发**，只有显式覆盖的名字是假的。

    漏转发一个名字的症状是 `AttributeError: module 'httpx_shim' has no
    attribute …` —— 那看着像"库换了 API"，而不是"夹具漏转发了"，
    排查方向会跑偏。

    ⚠️ `app/ctf2/client.py` 实测用到 **4** 个名字
    （`AsyncClient` / `HTTPError` / `Response` / `Timeout`）——
    这里逐个断言它们都能从 shim 上取到。
    """
    patch_ctf2_transport(monkeypatch, json_response(200, {}))
    from app.ctf2 import client as client_mod

    for name in ("AsyncClient", "HTTPError", "Response", "Timeout"):
        assert getattr(client_mod.httpx, name) is not None, f"shim 没转发 {name}"
    # 只覆盖了一个名字，其余都该是真模块里的东西
    assert client_mod.httpx.Timeout is httpx.Timeout
    assert client_mod.httpx.Response is httpx.Response
    assert client_mod.httpx.HTTPError is httpx.HTTPError


def test_a_missing_name_on_the_shim_raises_instead_of_returning_none(
    monkeypatch,
) -> None:
    """★★ 漏转发的症状必须是**响亮的 `AttributeError`**，不能是静默的 `None`。

    ## 这条守的是"假失败被吞掉"

    上面那条只做了**正向**断言（用到的名字都能取到）。正向断言挡不住
    一种更坏的实现：给 `__getattr__` 兜一个默认值

        shim.__dict__["__getattr__"] = lambda name: getattr(httpx, name, None)

    那样漏转发不再抛错，而是**静默返回 `None`** —— 症状会变成后面某处
    `None.AsyncClient` 之类的 `AttributeError`，位置离真正的原因很远。

    实测的实现（`tests/ctf2/conftest.py:237`）是

        shim.__dict__["__getattr__"] = lambda name: getattr(httpx, name)

    **没给默认值**，所以这里钉住"取一个真 httpx 也没有的名字必须抛
    `AttributeError`"，把兜默认值这条路堵死。
    """
    patch_ctf2_transport(monkeypatch, json_response(200, {}))
    from app.ctf2 import client as client_mod

    missing = "no_such_name_in_httpx_at_all"
    assert not hasattr(httpx, missing), "httpx 真的长出了这个名字，换一个"
    with pytest.raises(AttributeError):
        getattr(client_mod.httpx, missing)


def test_every_module_that_builds_a_client_is_covered(
    monkeypatch,
) -> None:
    """★★ 每个**自己构造平台** `httpx.AsyncClient` 的 `app/ctf2/` 模块都必须被覆盖。

    ## 这条守的是一类"改了夹具才露出来"的失效

    夹具原来打的是 httpx **模块的属性**，所以它**顺带**盖住了所有模块。
    改成"换模块绑定"之后那个顺带覆盖没了 —— 实测只换 `client.py` 时，
    `attachments.py` 拿真 `httpx` 建 client、请求真的发出去，
    5 条 `test_attachments` 用例以"测试试图解析非本机地址"失败。
    那个症状看起来像**网络问题**，而不是"夹具漏覆盖一个模块"。

    所以这条用**源码扫描**把两者的对应关系钉住：`app/ctf2/` 下凡是
    会构造 `httpx.AsyncClient(` 的模块，要么在 `CTF2_CLIENT_MODULES` 里，
    要么在下面那张**写明理由**的豁免表里。

    ⚠️ 用源码扫描而不是逐个 import 检查：新模块可能还没被任何人 import，
    而"没被 import"正是它最容易被漏掉的时候。
    """
    import ast
    import pathlib

    from tests.ctf2.conftest import CTF2_CLIENT_MODULES

    #: 会构造 client、但**不需要**被本夹具覆盖的模块（附理由）。
    #:
    #: ⚠️ 豁免必须有理由：这张表的作用是"让例外可见"，不是"让扫描闭嘴"。
    NOT_NEEDED = {
        # `runner.py` 的两处都是**非平台**的出站：
        #   · `LlmGateway.chat`（llm 网关）—— `test_runner.py` 用 `_FakeGateway`
        #     整体替掉，走不到真的 client；
        #   · `http_request` 工具那条路 —— 测试里用 `_FakeSandbox` 打桩。
        # 本夹具的语义是"把**平台**的传输换成 MockTransport"，套到 LLM 网关
        # 上语义就不对了（那个 handler 是平台形状的）。实测：只覆盖那 4 个
        # 平台模块时 `test_runner.py` 全绿。
        "app.ctf2.runner",
    }

    covered = set(CTF2_CLIENT_MODULES) | NOT_NEEDED
    app_root = pathlib.Path(__file__).resolve().parents[2] / "app"
    ctf2_root = app_root / "ctf2"

    builders: set[str] = set()
    for path in ctf2_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr == "AsyncClient"
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "httpx"
            ):
                rel = path.relative_to(app_root.parent)
                # ⚠️ 用 `as_posix()` 再换 `.`：直接用 `str(path)` 在 Windows 上
                #    得到反斜杠，而 `.replace("/", ".")` 对它无效 ——
                #    结果是"扫描出来的名字"与表里的名字永远对不上，
                #    两个断言同时变红（实测在本机踩到过）。
                builders.add(rel.with_suffix("").as_posix().replace("/", "."))
                break

    missing = builders - covered
    assert not missing, (
        "这些模块自己构造 `httpx.AsyncClient`，但既不在 `CTF2_CLIENT_MODULES` 里、"
        "也不在 `NOT_NEEDED` 里 —— 它们的用例会拿真 httpx 建 client 而"
        "**真的发外呼**（症状是「测试试图解析非本机地址」，看起来像网络问题）："
        f"{sorted(missing)}"
    )
    # 另一侧：表里不该有已经不再自己建 client 的模块（那是漂移的开始）
    stale = covered - builders
    assert not stale, (
        f"夹具覆盖表里有不再自己建 client 的模块：{sorted(stale)} —— "
        "表与代码漂移了，请同步"
    )


async def test_the_production_guard_survives_the_fixture(monkeypatch) -> None:
    """★★★ 端到端：夹具生效时 `netguard.ensure_guarded` 仍能正常工作。

    ## 这条是那个 51 条回归的**直接**回归闸

    它复现的就是失败现场：夹具已经替换了 `app.ctf2.client.httpx`，
    此时构造一个真 client 并调 `ensure_guarded`。

    * 修复前：`TypeError: isinstance() arg 2 must be a type`（51 条就死在这）；
    * 修复后：按**池**的类型判异步，不读任何可被替换的名字 → 正常返回。

    ⚠️ 本文件是 `async def` 用例（`pyproject.toml` 的 `asyncio_mode = "auto"`
    让它们直接跑，见 `tests/ctf2/conftest.py` 顶部那段说明）——
    所以这里直接 `await`，不用 `asyncio.run`。
    """
    patch_ctf2_transport(monkeypatch, json_response(200, {}))

    from app.core import netguard

    # 异步 client：走 async 分支
    client = httpx.AsyncClient(trust_env=False)
    try:
        netguard.ensure_guarded(client)  # 修复前这里抛 TypeError
        assert isinstance(
            client._transport._pool._network_backend, netguard.GuardedAsyncBackend
        )
    finally:
        await client.aclose()


def test_the_production_guard_also_works_for_a_sync_client(monkeypatch) -> None:
    """★ 同步 client 走 sync 分支 —— 按池判之后这条路同样成立。

    ⚠️ 单独一条（而不是并进上面那条）：上面是 async 用例，而这条是同步的
    ——`httpx.Client` 在异步用例里关不掉（`aclose` 是协程），
    混在一起会让"关了没"变得含糊。
    """
    patch_ctf2_transport(monkeypatch, json_response(200, {}))

    from app.core import netguard

    client = httpx.Client(trust_env=False)
    try:
        netguard.ensure_guarded(client)
        assert isinstance(
            client._transport._pool._network_backend, netguard.GuardedSyncBackend
        )
    finally:
        client.close()
