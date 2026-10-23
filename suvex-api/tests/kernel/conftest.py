"""`ctf2` 子包的测试夹具。

## ★ 它**不**重复父 conftest 已经做好的事

`tests/conftest.py` 里两个 autouse 的夹具（session 级的
`_prepare_database` = 建库 + `alembic upgrade head`，function 级的
`_clean_state` = `TRUNCATE ... CASCADE`）对**子目录同样生效** ——
pytest 会把祖先目录的 conftest 一路加载下来。所以这里没有：

* 建库与迁移（父 conftest 做了，而且是**对着真 PostgreSQL**）；
* 清表；
* 重定向 `SUPEX_SCAN_WORK_ROOT` / `SUPEX_ATTACHMENT_ROOT` 到临时目录。

照抄一遍的代价不是"多几行"，而是**两份口径各自漂移**：
哪天父 conftest 换一种隔离方式（比如改成 per-test 事务回滚），
子目录这份还停在 TRUNCATE 上，而症状是"ctf2 的测试偶发互串"。

## 本文件新增的四件事

1. **`SUPEX_SECRET_KEY`** —— 账号夹具要走**真实的加密路径**
   （`crypto.encrypt_secret`），没有密钥会直接抛 `SecretKeyMissing`。
   父 conftest **刻意没设**它（多数领域用不到）。
2. **假时钟（`FakeClock`）** —— 让"300 秒超时"与"退避到上限"在
   微秒内跑完，走的却是与生产完全相同的代码路径。
3. **假传输（`patch_ctf2_transport`）** —— 仿 `tests/factories.patch_llm_transport`。
4. **异步用例的 marker** —— 见下面那个 hook。
"""

from __future__ import annotations

import asyncio
import time
import types
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.core.timeutil import now_utc

# ══════════════════════════════════════════════════════════
# 异步用例：**没有 hook、没有 marker**
# ══════════════════════════════════════════════════════════
#
# ⚠️ 这里原本有一个 `pytest_collection_modifyitems`，作用是自动给
# `async def test_*` 补 `@pytest.mark.asyncio`（比改全局配置"局部"）。
# **它在 pytest-asyncio 1.4 上不生效**，实测撞到：
#
#     The test <...> is marked with '@pytest.mark.asyncio' but it is
#     not an async function. Please remove the asyncio mark.
#
# 30 多个 async 用例全红，而报错指向的是"这个函数不是 async" ——
# 与真实原因（marker 加得太晚）毫无关系。
#
# 所以改用 `pyproject.toml` 的 `asyncio_mode = "auto"`（见那里的说明：
# 影响面只有 async 用例，同步用例一行不变）。
# 删掉 hook 而不是留着它 —— 留着会在 auto 模式下叠加出更难懂的行为。


# ══════════════════════════════════════════════════════════
# 加密密钥
# ══════════════════════════════════════════════════════════

TEST_PAT = "pat_ctf2_TEST-0000-1111-2222-333344445555"


@pytest.fixture(autouse=True)
def _ctf2_secret_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """确保有 `SUPEX_SECRET_KEY`，并在结束后还原。

    ⚠️ 必须清 `get_settings` 的 lru_cache：它是**单例**，不清的话
    改环境变量不会生效，而症状是"测试跑的是上一个用例的配置"——
    看起来像配置没读对，实际是缓存没失效。

    用 `if not crypto.is_configured()` 而不是无条件 setenv：
    外部（CI）如果已经显式设了真密钥，那应该用它 —— 无条件覆盖
    会把这个夹具变成一个"偷偷换密钥"的动作，而密钥一换，
    同一个会话里先前加密过的数据就解不开了。
    """
    from app.core import crypto
    from app.core.config import get_settings

    if crypto.is_configured():
        yield
        return
    monkeypatch.setenv("SUPEX_SECRET_KEY", crypto.generate_key())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ══════════════════════════════════════════════════════════
# 假时钟 / 假 sleep
# ══════════════════════════════════════════════════════════


class FakeClock:
    """手动推进的单调时钟 + 与之**联动**的 sleep。

    ## 为什么两者必须联动

    `TokenBucket` 与 `start_environment_until_ready` 都是
    "读时钟 → 决定睡多久 → 睡完再读时钟"。如果 sleep 只是记录
    调用而不推进时钟，那么睡完之后时钟**还在原地** ——
    循环永远不会超时（死循环），而限流桶永远拿不到新令牌。

    这个类把两者绑在一起：`sleep(n)` 立刻返回，但把时钟推进 n 秒。
    于是"等 300 秒超时"这个用例在真实时间里只花几微秒，
    而逻辑走的是**与生产完全相同的那条路径**。
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        # 记录所有 sleep，给"按 Retry-After 等待"这类断言用。
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)
        # 真正让出一次事件循环 —— 不 await 的话，一个只含
        # `await self._sleep()` 的循环会变成一个不 yield 的紧循环。
        await asyncio.sleep(0)

    @property
    def total_slept(self) -> float:
        return sum(self.sleeps)


@pytest.fixture()
def fake_clock() -> FakeClock:
    return FakeClock()


# ══════════════════════════════════════════════════════════
# 假传输（仿 `tests/factories.patch_llm_transport`）
# ══════════════════════════════════════════════════════════


class RecordedRequests:
    """记录客户端**实际发出**的请求。

    断言"发了什么"与断言"收到了什么"同等重要 —— 而本次最要紧的
    一条断言就是"Authorization 头里有 Bearer，且 URL 里没有 token"。
    只断言响应的话，那条永远验不到。
    """

    def __init__(self) -> None:
        self.items: list[httpx.Request] = []

    def __len__(self) -> int:
        return len(self.items)

    @property
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.items]

    @property
    def paths(self) -> list[str]:
        return [r.url.path for r in self.items]

    @property
    def methods(self) -> list[str]:
        return [r.method for r in self.items]

    def auth_headers(self) -> list[str | None]:
        return [r.headers.get("authorization") for r in self.items]


#: 会**自己构造** `httpx.AsyncClient` 的模块 —— 夹具必须逐个换掉它们的绑定。
#:
#: ## ★★ 为什么是一张表而不是只换 `client.py`
#:
#: 原来（全局污染那版）只写了 `app.kernel.client.httpx.AsyncClient`，
#: 而它**恰好**把别的模块也盖住了 —— 因为那样改的是 **httpx 模块自己的
#: 属性**，谁都看得见。改成"换模块绑定"之后，这个"顺带覆盖"消失了：
#: 只换 `client.py` 的话，`attachments.py` 仍然拿真 `httpx` 去建 client，
#: 于是它的请求**真的**打出去 —— 实测被 `_block_egress` 拦住，
#: 5 条 `test_attachments` 用例以"测试试图解析非本机地址"失败。
#: 所以这张表**必须**列全：漏一个模块 = 那个模块的用例真的发外呼。
#:
#: ⚠️ 实测（`grep -rn "httpx.AsyncClient(" app/`）当前是这四处。
#: 将来在 `app/kernel/` 下新增会自己建 client 的模块时，**要把它加进来**，
#: 否则那个模块的用例会开始真的发外呼（而症状是"连不上/被护栏拦住"，
#: 看起来像网络问题而不是夹具漏覆盖）。
CTF2_CLIENT_MODULES: tuple[str, ...] = (
    "app.kernel.client",
    "app.kernel.attachments",
    "app.kernel.adapters.transport",
    "app.kernel.adapters.probe",
    # v0.73：`run_multi_agent` 为整轮编排共享**一个** http client
    # （`async with httpx.AsyncClient(...)`，供 root 与所有子 agent 复用）。
    # 它自己构造 client，所以任何驱动 `run_multi_agent` 的用例若没被本夹具
    # 覆盖，就会拿真 httpx 建 client 而**真的发外呼**。登记在这里让夹具
    # 把它的传输也换成 MockTransport。
    "app.kernel.multiagent",
)


def _httpx_shim(**overrides: Any) -> Any:
    """造一个"只在指定名字上偏离真 httpx"的**模块替身**。

    ## ★★ 为什么需要它（2026-10-16 修的那个 51 条回归）

    夹具原来打的是 `app.kernel.client.httpx.AsyncClient` —— 而
    `app.kernel.client.httpx` **就是全局那一个 httpx 模块对象**
    （实测 `app.kernel.client.httpx is httpx` → `True`）。所以那一步改的是
    **httpx 模块自己的属性**：任何在测试运行期间去读 `httpx.AsyncClient`
    的代码都会拿到夹具那个工厂函数。

    后果实测：`app/core/netguard.py` 的 `ensure_guarded` 里
    `isinstance(client, httpx.AsyncClient)` 抛
    `TypeError: arg 2 must be a type` —— **51 条用例全倒在这一行**。

    ## 修法是"换绑定"，不是"换属性"

    `monkeypatch.setattr("app.kernel.client.httpx", shim)` 只换掉**那个模块里
    的名字**指向谁，真 httpx 模块**一个属性都没动**。于是：

    * 夹具照常生效（`client_mod.httpx.AsyncClient` 是工厂）；
    * `netguard` 里的 `httpx` 仍是真模块，`httpx.AsyncClient` 仍是真类。

    ## ⚠️ 转发必须**全覆盖**目标模块用到的名字

    漏一个就会引入一批**新的**假失败，而且症状是
    `AttributeError: module 'httpx_shim' has no attribute …`，
    看着像"库换了 API"而不是"夹具漏转发了"。

    所以这里不留一份手写的名字清单（那会漂移），而是用 `__getattr__`
    **全量转发**给真模块，只有显式 `overrides` 那几个名字是假的。
    实测过：`HTTPError` / `Response` / `Timeout` / `MockTransport` /
    `Request` 都正常转发。
    """
    shim = types.ModuleType("httpx_shim")
    shim.__dict__.update(overrides)
    # ★ `__getattr__` 在模块上的语义：**找不到的属性**才走它。
    #   所以 overrides 里的名字由 `__dict__` 命中，其余全部转发给真模块。
    shim.__dict__["__getattr__"] = lambda name: getattr(httpx, name)
    return shim


def patch_ctf2_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> RecordedRequests:
    """把 CTF2 各模块里的 `httpx.AsyncClient` 换成走 `MockTransport` 的版本。

    ## 打的是**模块里那个真实的** `AsyncClient` 类

    与 `factories.patch_llm_transport` 完全同一条思路：所以
    `KernelClient` 里的 URL 构造、请求头拼装、状态码解释、重试与退避
    **全都真的跑一遍**，只有网络那一层是假的。

    ## ★★ 但换的是**每个模块的绑定**，不是 httpx 模块的属性

    * 见 `_httpx_shim` 的说明 —— 原来的写法会污染**全局 httpx 模块**，
      而那是 2026-10-16 那 51 条失败的根因；
    * 见 `CTF2_CLIENT_MODULES` 的说明 —— 只换 `client.py` **不够**，
      `attachments.py` 会拿真 httpx 去建 client 而真的发外呼。

    ⚠️ `real_client = httpx.AsyncClient` 必须在造 shim **之前**取：
    在工厂里再去引用"刚被替换的名字"，拿到的是工厂自己 → `RecursionError`。
    （实测过。真夹具原来就是对的顺序，这里保持。）

    ⚠️ `httpx.MockTransport` 的 handler 是**同步可调用**的，
    而它被 AsyncClient 用（httpx 内部会调 `handle_async_request`）。
    """
    recorded = RecordedRequests()
    real_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        def recording_handler(request: httpx.Request) -> httpx.Response:
            recorded.items.append(request)
            return handler(request)

        kwargs["transport"] = httpx.MockTransport(recording_handler)
        return real_client(*args, **kwargs)

    # ⚠️ 对**模块绑定**打补丁（不是 `…httpx.AsyncClient` 那种属性路径）：
    #    后者会落到全局 httpx 模块上，正是要避免的那个污染。
    shim = _httpx_shim(AsyncClient=factory)
    for module in CTF2_CLIENT_MODULES:
        monkeypatch.setattr(f"{module}.httpx", shim)
    return recorded


def json_response(status: int, payload: Any, *, headers: dict[str, str] | None = None):
    """造一个 handler，永远返回同一份 JSON。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload, headers=headers or {}, request=request)

    return handler


def sequence_handler(responses: list[Any]):
    """按顺序返回一组响应，用完之后**重复最后一个**。

    "重复最后一个"是刻意的：让"我预期它调了 3 次"这类断言失败时
    看到的是**具体行为**（第 4 次也返回了同一个东西），
    而不是一个 StopIteration 的 traceback —— 后者指向的是测试代码，
    而不是被测行为。
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        index = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        item = responses[index]
        if callable(item):
            return item(request)
        status, payload = item
        return httpx.Response(status, json=payload, request=request)

    return handler


# ══════════════════════════════════════════════════════════
# 数据库夹具（真 PostgreSQL）
# ══════════════════════════════════════════════════════════


@pytest.fixture()
def ctf2_account(db_session: Session):
    """一个绑好的 CTF2 账号。

    凭据走**真实的加密路径**（`crypto.encrypt_secret`），不是塞一段假密文 ——
    与 `factories.make_provider` 同一条理由：否则"读取时要能解密"这件事
    在测试里从来没有被验证过，而真去调 CTF2 才会发现密钥对不上。
    """
    from app.core.crypto import encrypt_secret, mask_secret
    from app.kernel.models import KernelAccount

    account = KernelAccount(
        id=new_id("ctf2acc"),
        label="测试号",
        token_encrypted=encrypt_secret(TEST_PAT),
        token_hint=mask_secret(TEST_PAT),
        status="active",
        remote_user_id="u_1001",
        remote_username="tester",
        scopes=["competition:read", "challenge:read", "environment:start"],
        last_synced_at=now_utc(),
    )
    db_session.add(account)
    db_session.commit()
    return account


@pytest.fixture()
def ctf2_competition(db_session: Session, ctf2_account):
    from app.kernel.models import KernelCompetition

    competition = KernelCompetition(
        id=new_id("ctf2comp"),
        account_id=ctf2_account.id,
        remote_id="1234",
        kind="practice",
        name="2026 秋季练习赛",
        remote_status="ongoing",
        challenge_count=0,
        synced_at=now_utc(),
    )
    db_session.add(competition)
    db_session.commit()
    return competition


def _make_challenge(db: Session, competition, *, remote_id: str, **overrides):
    from app.core.vocab import KernelChallengeStatus, KernelSandboxProfile
    from app.kernel.models import KernelChallenge

    kwargs: dict[str, Any] = {
        "id": new_id("ctf2chal"),
        "competition_id": competition.id,
        "remote_id": remote_id,
        "stage_id": None,
        "title": f"题 {remote_id}",
        "category": "web",
        "description": "登录口有弱口令。",
        "attachments": [],
        "score": 100,
        "status": KernelChallengeStatus.UNSOLVED.value,
        # ★ 拆列之后是**两个**字段（见 models 里那两列的说明）。
        #   `candidate_flag` = 等确认的候选；`confirmed_flag` = 平台判过对的。
        #   两者由数据库 CHECK 保证**不会同时有值**。
        "candidate_flag": None,
        "confirmed_flag": None,
        "wrong_submission_count": 0,
        "max_wrong_submissions": 3,
        "candidate_count": 0,
        "sandbox_profile": KernelSandboxProfile.BASE.value,
    }
    kwargs.update(overrides)
    challenge = KernelChallenge(**kwargs)
    db.add(challenge)
    db.commit()
    return challenge


@pytest.fixture()
def ctf2_challenge(db_session: Session, ctf2_competition):
    """一道**未解出**的 web 题。

    ⚠️ `status` 与 `confirmed_flag` 的组合必须满足
    `ck_ctf2_challenges_solved_implies_flag` 与
    `ck_ctf2_challenges_confirmed_only_when_solved` —— 造一个 `solved`
    但没有 flag 的行、或者一个非 solved 却有 `confirmed_flag` 的行，
    都会直接撞数据库约束。而报错看起来像"夹具写错了"，
    不是"约束在起作用"（与 `make_scan` 里那句注释同一条）。
    """
    return _make_challenge(db_session, ctf2_competition, remote_id="4567")


@pytest.fixture()
def ctf2_reverse_challenge(db_session: Session, ctf2_competition):
    """一道**逆向**题（用来验证镜像按题型选择）。"""
    from app.core.vocab import KernelSandboxProfile

    return _make_challenge(
        db_session,
        ctf2_competition,
        remote_id="4568",
        title="re1: 反调试",
        category="reverse",
        sandbox_profile=KernelSandboxProfile.REVERSE.value,
    )


@pytest.fixture()
def ctf2_ready_environment(db_session: Session, ctf2_challenge):
    """一个已就绪的环境。

    ⚠️ `status='ready'` 时 `access_url` 必须非空（约束
    `access_url_only_when_ready` 管的是反向：非 ready 不能有 url）。
    这里两个都给上，是合法且完整的形状。
    """
    from app.kernel.models import KernelEnvironment

    environment = KernelEnvironment(
        id=new_id("ctf2env"),
        challenge_id=ctf2_challenge.id,
        status="ready",
        sandbox_profile="suvex-sandbox",
        access_url="http://1.2.3.4:8080",
        access_urls=["http://1.2.3.4:8080", "1.2.3.4:9999"],
        poll_count=3,
        started_at=now_utc(),
        ready_at=now_utc(),
    )
    db_session.add(environment)
    db_session.commit()
    return environment


@pytest.fixture()
def ctf2_config(db_session: Session):
    """CTF2 配置行（默认值）。"""
    from app.kernel.service import get_ctf2_config

    row = get_ctf2_config(db_session)
    return row


@pytest.fixture()
def ctf2_provider(db_session: Session) -> str:
    """一个可用的 LLM provider。

    ★ 为什么有些用例需要它：CTF2 的事件会被写进现有 events 流，而
    `activity_events.scan_id` 指向 `scans`，`scans.provider_id` 是
    **NOT NULL**（见 `ctf2/events.py` 的 `_default_provider_id`）。

    所以"建一条解题 scan"这件事的前提是"有 provider"—— 而没有 provider
    时事件会被跳过（记 warning），状态机照旧推进。两种行为都有测试盯着，
    所以这个夹具**不做成 autouse**：依赖它的用例应该显式声明，
    这样读测试的人能看到"这条用例假设有 provider"。
    """
    from tests.factories import make_provider

    return make_provider(db_session, provider_id="prov_ctf2_test")


@pytest.fixture()
def real_time_guard() -> Iterator[None]:
    """提醒"这个文件的用例不该依赖真实时间"。

    保留它是为了让 review 的人看到意图 —— 所有涉及等待的用例
    都必须走 `FakeClock`。一个真的 sleep 3 秒的用例会让整个套件
    从"几秒"变成"几分钟"，而那种成本会让人开始给测试打跳过标记，
    于是覆盖悄悄退化。
    """
    started = time.monotonic()
    yield
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, (
        f"这个用例跑了 {elapsed:.1f} 秒 —— 很可能忘了用 fake_clock，"
        "而真实等待会让整个套件变慢。"
    )
