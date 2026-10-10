"""★ 密钥全链路卫生：不出现在日志里、不以明文落盘。

这是任务 2 的验收标准，逐条对应：

> 密钥全链路不出现明文落盘或明文日志（检查一下日志中间件有没有把请求体
> 整个打出来，那样会把明文 api_key 打进日志，这是常见的安全事故点，务必检查）

## 审计结论（先说结果）

**本后端没有请求体日志中间件。** 唯一的请求日志是 uvicorn 的 access log，
它只记 `方法 路径 HTTP状态`，不含 header 与 body；FastAPI / Starlette
自身也不打印请求体。`app/core/logging.py` 的模块 docstring 里有完整说明。

但"现在没有"不等于"以后不会有" —— 下面第一条测试就是那个审计结论的
**可执行版本**：它捕获整个请求周期内**所有**被送进 logging 的消息，
断言明文 key 一次都没出现。哪天有人加了一个打印 body 的中间件，
它当场变红。

## 为什么在 `Logger._log` 上抓，而不是用 `caplog`

`caplog` 挂在 handler 层，而**脱敏过滤器会就地改写 `record.msg`**。
如果脱敏先跑，`caplog` 看到的就已经是掩码了 —— 断言永远通过，
测试变成一句空话。

`Logger._log` 是 handler 之前的一步：它拿到的是**调用方原本想打的东西**。
在那一点上抓，测的才是"有没有人把密钥写进日志"，
而不是"我们的脱敏正则灵不灵"。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import crypto
from app.providers.models import ProviderAccount

# 一个形态真实、足够长、不会被脱敏正则误伤的测试凭据。
# 用常量而不是随手写一个：它要在多个断言里出现，拼错一次就会得到
# "测试通过但什么都没测"。
PLAINTEXT_KEY = "sk-live-TESTONLY-9f3c1a7b42e8d5061c4b7a2e"

# 敏感的请求体字段：这些值绝不该出现在日志里。
SENSITIVE = ("password", "apiKey", "plaintext", "token")


class _LogSpy(logging.Handler):
    """把所有日志消息按**原始形态**收下来。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - 由断言驱动
        try:
            self.messages.append(record.getMessage())
        except Exception:  # noqa: BLE001
            self.messages.append(str(record.msg))
        if record.exc_info:
            self.messages.append(logging.Formatter().formatException(record.exc_info))

    @property
    def text(self) -> str:
        return "\n".join(self.messages)


@pytest.fixture()
def log_spy() -> Iterator[_LogSpy]:
    """挂在**根 logger 之前**的探针：拦截 `Logger._log`。

    直接接管 `Logger._log` 而不是加 handler —— 见模块 docstring：
    handler 层已经过了脱敏，那样测不出东西。
    """
    spy = _LogSpy()
    original = logging.Logger._log

    def spy_log(self, level, msg, args, *rest, **kwargs):  # noqa: ANN001
        record = logging.LogRecord(self.name, level, "(spy)", 0, msg, args, None, func=None)
        spy.emit(record)
        # ⚠️ 转发必须**原样**（`*rest, **kwargs`），不能手写位置参数。
        # `_log` 的签名是 `(level, msg, args, exc_info=None, extra=None,
        # stack_info=False, stacklevel=1)` —— 手写转发时很容易把 `stack_info`
        # 塞进 `extra` 的位置，而 `extra` 期望一个 dict。
        # 后果是 `TypeError: 'bool' object is not iterable`，
        # 出现在 logging 库内部，看起来完全不像"转发参数写错了"。
        return original(self, level, msg, args, *rest, **kwargs)

    logging.Logger._log = spy_log  # type: ignore[method-assign]
    try:
        yield spy
    finally:
        logging.Logger._log = original  # type: ignore[method-assign]


def _create_via_api(client: TestClient, **overrides) -> dict:
    payload = {
        "label": "DeepSeek",
        "model": "deepseek-chat",
        "kind": "cloud",
        "apiKey": PLAINTEXT_KEY,
        "baseUrl": "https://api.deepseek.com",
        "note": "测试用",
        "isDefault": True,
    }
    payload.update(overrides)
    response = client.post("/api/v1/providers", json=payload)
    assert response.status_code in (200, 201), response.text
    return response.json()["provider"]


# ══════════════════════════════════════════════════════════
# 日志
# ══════════════════════════════════════════════════════════


def test_plaintext_key_never_reaches_the_logging_module(
    ready_client: TestClient, log_spy: _LogSpy, monkeypatch
) -> None:
    """★ 走完整条链路（新建 → 列表 → 更新 → 设默认 → 验证），
    明文 key **一次都没有**被送进 logging。

    这是"日志中间件有没有把请求体整个打出来"这个问题的机器化答案。

    验证那一步用假 transport（离线、确定），它照样会走完
    URL 构造 → 请求头组装 → 响应解析 → 落库这一整条路径。
    """
    import httpx

    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]}, request=request),
    )

    created = _create_via_api(ready_client)
    provider_id = created["id"]

    ready_client.get("/api/v1/providers")
    ready_client.patch(
        f"/api/v1/providers/{provider_id}",
        json={"label": "DeepSeek 生产", "apiKey": PLAINTEXT_KEY},
    )
    ready_client.post(f"/api/v1/providers/{provider_id}/default")
    ready_client.post(f"/api/v1/providers/{provider_id}/verify")

    # 自检：先证明探针**确实在工作**。
    # 少了这一步，如果 `Logger._log` 没被挂上，下面那条断言会永远通过 ——
    # 一个不会失败的检查比没有检查更坏。
    logging.getLogger("suvex.test").info("log-spy-selftest")
    assert "log-spy-selftest" in log_spy.text, "探针没抓到日志，这条测试是空转的"

    assert PLAINTEXT_KEY not in log_spy.text, (
        "明文 API key 出现在了日志里。\n"
        "排查方向：是不是有人加了打印请求体的中间件？\n"
        f"--- 捕获到的日志 ---\n{log_spy.text[-4000:]}"
    )


def test_plaintext_password_never_reaches_the_logging_module(
    client: TestClient, log_spy: _LogSpy
) -> None:
    """同一件事对**口令**也成立。它是另一条独立的高价值凭据。

    与上一条拆开写，是因为它们的泄漏路径不同：apiKey 走请求体，
    口令走登录端点 —— 而登录端点更容易被"打印一下入参方便调试"顺手带出来。
    """
    secret_password = "VerySecret2026Pass"

    client.post("/api/v1/auth/setup", json={"password": secret_password})
    client.post("/api/v1/auth/login", json={"password": "wrong-one-here"})
    client.get("/api/v1/auth/me")

    logging.getLogger("suvex.test").info("log-spy-selftest")
    assert "log-spy-selftest" in log_spy.text, "探针没抓到日志，这条测试是空转的"

    assert secret_password not in log_spy.text
    assert "wrong-one-here" not in log_spy.text


def test_redacting_formatter_masks_an_accidental_key(log_spy: _LogSpy) -> None:
    """兜底那一层：万一有人真把密钥打进了日志，落盘的是掩码。

    这条测的是**意图层面**的日志（`Logger._log` 之前），所以它验证的是
    "探针能看见明文"这件事本身 —— 然后单独验证脱敏函数能把它变成掩码。
    两件事分开测，才不会出现"脱敏把它修好了，于是看不出有人写了明文"。
    """
    accidental = f"calling provider with key {PLAINTEXT_KEY}"

    # 1) 探针确实能看见明文 —— 证明上一条测试不是空转
    logging.getLogger("suvex.test").info(accidental)
    assert PLAINTEXT_KEY in log_spy.text

    # 2) 脱敏后不再有明文
    assert PLAINTEXT_KEY not in crypto.redact(accidental)
    assert "****" in crypto.redact(accidental)


def test_redacting_filter_does_not_swallow_the_log_record() -> None:
    """改写 `record.msg` 之后必须清空 `args`，否则那条日志会**彻底消失**。

    细节：原消息是 `"key=%s"` 加一个 args。只改 msg 不改 args 的话，
    `%` 格式化会拿到一个参数但找不到占位符 →
    `TypeError: not all arguments converted` → 被 logging 自己吞掉
    （输出 "--- Logging error ---"）。**改日志的代码把日志弄丢**，
    是最讽刺的一类 bug，所以钉一条测试。
    """
    from app.core.logging import RedactingFilter

    record = logging.LogRecord(
        "t", logging.INFO, "f", 1, "provider key=%s", (PLAINTEXT_KEY,), None
    )
    assert RedactingFilter().filter(record) is True
    # 不抛异常，且真的能格式化出结果
    rendered = record.getMessage()
    assert PLAINTEXT_KEY not in rendered
    assert "****" in rendered


# ══════════════════════════════════════════════════════════
# 落盘
# ══════════════════════════════════════════════════════════


def test_database_never_stores_the_plaintext(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 库里存的是密文，明文一个字都不在。"""
    created = _create_via_api(ready_client)

    row = db_session.execute(
        select(ProviderAccount).where(ProviderAccount.id == created["id"])
    ).scalar_one()

    assert row.api_key_encrypted, "密文列为空，说明凭据根本没存进去"
    assert PLAINTEXT_KEY not in row.api_key_encrypted
    # 也不该是"看起来像凭据"的任何变体
    assert not row.api_key_encrypted.startswith("sk-")
    # 能解回来才说明是有效的密文，而不只是一段乱码
    assert crypto.decrypt_secret(row.api_key_encrypted) == PLAINTEXT_KEY


def test_mask_column_reveals_only_prefix_and_suffix(
    ready_client: TestClient, db_session: Session
) -> None:
    """掩码列只暴露前 3 后 4，中间不可还原。"""
    created = _create_via_api(ready_client)

    row = db_session.execute(
        select(ProviderAccount).where(ProviderAccount.id == created["id"])
    ).scalar_one()

    hint = row.api_key_hint or ""
    assert hint == crypto.mask_secret(PLAINTEXT_KEY)
    assert hint == f"{PLAINTEXT_KEY[:3]}****{PLAINTEXT_KEY[-4:]}"
    # 掩码不能把中间那段带出来
    assert PLAINTEXT_KEY[6:20] not in hint
    # 掩码本身也不该等于明文
    assert hint != PLAINTEXT_KEY


def test_short_key_is_fully_hidden() -> None:
    """短于 12 位的值整体隐掉。

    「前 3 + 后 4」用在一个 10 位的值上会暴露 7 位 —— 那不是掩码，是缩写。
    前端 `secretPreview` 用的也是 12 这个阈值。
    """
    assert crypto.mask_secret("shortkey12") == "********"
    assert crypto.mask_secret("") == ""
    assert crypto.mask_secret("sk-abcdefghijklmnop") == "sk-****mnop"


def test_encrypt_is_nondeterministic() -> None:
    """同一个明文两次加密得到**不同**密文（Fernet 每次用新 IV）。

    这条不是细节洁癖：确定性密文会让"两个 provider 用了同一个 key"
    在数据库里一眼可见，而那是别人不该知道的信息。
    """
    first = crypto.encrypt_secret(PLAINTEXT_KEY)
    second = crypto.encrypt_secret(PLAINTEXT_KEY)

    assert first != second
    assert crypto.decrypt_secret(first) == crypto.decrypt_secret(second) == PLAINTEXT_KEY


def test_repr_never_contains_the_credential(
    ready_client: TestClient, db_session: Session
) -> None:
    """`repr(provider)` 里没有明文、也没有密文。

    repr 会进日志、异常消息、调试器面板 —— 而调试器面板是最容易被截图分享的。
    """
    created = _create_via_api(ready_client)
    row = db_session.execute(
        select(ProviderAccount).where(ProviderAccount.id == created["id"])
    ).scalar_one()

    text = repr(row)
    assert PLAINTEXT_KEY not in text
    assert (row.api_key_encrypted or "") not in text
    assert row.id in text  # 但它该有的诊断信息还在


def test_orm_model_has_no_api_key_attribute() -> None:
    """模型上**根本不存在** `api_key` 属性。

    让 ORM 对象没有一条通往明文的路，比"提供一个会返回空串或掩码的 api_key
    属性"更安全：后者会诱使别处写 `provider.api_key`，而读到的东西取决于
    那个属性当时的实现 —— 某天有人把它改成返回明文来"修一个 bug"，
    就没有任何东西会挡住。
    """
    assert not hasattr(ProviderAccount, "api_key")


def test_provider_error_text_is_redacted(
    ready_client: TestClient, db_session: Session, monkeypatch
) -> None:
    """对方服务在错误里**复述了我们的密钥**时，落库的 `verify_error` 里不能有它。

    这是真实会发生的事：有些网关把收到的 Authorization 头原样放进错误信息。
    而 `verify_error` 会被存进数据库、渲染到界面上 —— 一条完整的泄漏路径。
    """
    created = _create_via_api(ready_client)

    def echo_the_key(request):  # noqa: ANN001
        import httpx

        return httpx.Response(
            401,
            json={"error": {"message": f"invalid key: {PLAINTEXT_KEY}"}},
            request=request,
        )

    _patch_transport(monkeypatch, echo_the_key)
    ready_client.post(f"/api/v1/providers/{created['id']}/verify")

    row = db_session.execute(
        select(ProviderAccount).where(ProviderAccount.id == created["id"])
    ).scalar_one()
    assert row.verify_error
    assert PLAINTEXT_KEY not in row.verify_error
    assert "****" in row.verify_error


def _patch_transport(monkeypatch, handler) -> None:
    """见 `factories.patch_llm_transport` 的说明。这里转一手只是为了
    让本文件里读起来短一点。"""
    from tests.factories import patch_llm_transport

    patch_llm_transport(monkeypatch, handler)
