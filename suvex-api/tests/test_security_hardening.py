"""任务 8 的其余加固项：环境隔离（要求 4）、限流（要求 5）、
授权落库（要求 3）、以及"错误不泄漏内部信息"。

拆成独立文件而不是塞进已有的测试里：这几条都是**上线前检查**的产物，
放在一起才好对照 `docs/security-checklist.md` 逐条读。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import redact
from tests.conftest import GOOD_USERNAME

AUTH_STATEMENT = "我有权对此目标进行安全测试"
VALID_AUTH = {"confirmed": True, "statement": AUTH_STATEMENT}


# ══════════════════════════════════════════════════════════
# 要求 4a：子进程环境变量白名单
# ══════════════════════════════════════════════════════════


def _spec():
    from app.scans.runner import LaunchSpec

    return LaunchSpec(
        scan_id="scan_env_test",
        target="env.example.com",
        target_kind="url",
        skills=("web",),
        run_dir=Path("."),
        log_path=Path("x.log"),
        llm_model="deepseek/deepseek-chat",
        llm_api_key="sk-env-test-abcdefghijklmnop",
        llm_api_base="https://api.deepseek.com",
    )


@pytest.mark.parametrize("runner_name", ["strix", "fake"])
def test_backend_secrets_never_reach_the_scan_subprocess(
    runner_name: str, monkeypatch
) -> None:
    """★ 后端自己的凭据**绝不能**出现在扫描子进程的环境里。

    这一条是任务 8 排查出的**真实缺陷**：原来两个 runner 都是
    `env = dict(os.environ)`，于是把整个后端进程的环境交给了
    一个接下来要执行任意命令的进程。而那个环境里有：

    * `SUPEX_SECRET_KEY` —— 能解开库里**所有** provider 的 API key
    * `SUPEX_DATABASE_URL` —— 含数据库口令，直连业务库（含审计日志）

    strix 的威胁模型是"它会执行从目标上拿到的东西"（这正是要沙箱的原因），
    所以不能假设它内部一定安全。一个被诱导执行 `env` 的 agent
    就把密钥读走了，而拿到密钥 + 一份库快照 = 所有 provider 凭据失守。
    """
    # 模拟"环境里真的有这些机密"（线上就是这个状态）
    monkeypatch.setenv("SUPEX_SECRET_KEY", "super-secret-fernet-key-DO-NOT-LEAK")
    monkeypatch.setenv(
        "SUPEX_DATABASE_URL",
        "postgresql+psycopg://suvex:suvex_dev_local@127.0.0.1:55432/suvex",
    )
    monkeypatch.setenv("SUPEX_SESSION_COOKIE_NAME", "suvex_session")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-should-not-leak-either")

    from app.scans import runner as runner_module

    runner = (
        runner_module.FakeRunner()
        if runner_name == "fake"
        else runner_module.StrixRunner()
    )
    env = runner.env(_spec())

    blob = "\n".join(f"{k}={v}" for k, v in env.items())
    assert "DO-NOT-LEAK" not in blob, "SUPEX_SECRET_KEY 泄漏给了扫描子进程"
    assert "suvex_dev_local" not in blob, "数据库口令泄漏给了扫描子进程"
    assert "aws-should-not-leak-either" not in blob, "无关的云凭据也一起漏了"
    # 连"有哪些 SUPEX_ 变量"这种部署形状都不该给
    assert not [key for key in env if key.upper().startswith("SUPEX_")], (
        f"不该有任何 SUPEX_ 变量，实际有：{[k for k in env if k.upper().startswith('SUPEX_')]}"
    )


@pytest.mark.parametrize("runner_name", ["strix", "fake"])
def test_subprocess_still_gets_what_it_needs_to_run(runner_name: str) -> None:
    """反面：白名单不能把"跑得起来"需要的东西也一起挡掉。

    少了这一条，"什么都不传"也能让上面那条全绿 ——
    而什么都不传的表现是 strix 根本起不来（找不到 docker/nmap）。
    """
    from app.scans import runner as runner_module

    runner = (
        runner_module.FakeRunner()
        if runner_name == "fake"
        else runner_module.StrixRunner()
    )
    env = runner.env(_spec())

    # PATH 是必须的：没有它就找不到 docker / nmap / 任何工具
    assert any(key.upper() == "PATH" for key in env), "PATH 必须传下去"
    # 模型与凭据是**我们显式设的**（不是继承来的）
    assert env["STRIX_LLM"] == "deepseek/deepseek-chat"
    assert env["LLM_API_KEY"] == "sk-env-test-abcdefghijklmnop"

    if runner_name == "strix":
        # 这两条只有真 runner 设：替身不真的去调模型，
        # 所以它不传 base 也不需要遥测开关。断言它们是**替身的行为差异**，
        # 而不是"替身漏了什么" —— 让替身去模拟真实 runner 的每个细节，
        # 只会让"替身与真实实现的差异"变成一处需要维护的谎言。
        assert env["LLM_API_BASE"] == "https://api.deepseek.com"
        # 遥测默认关（契约：把目标地址上报第三方需要用户明确同意）
        assert env["STRIX_TELEMETRY"] == "0"


def test_local_model_does_not_inherit_a_stray_key(monkeypatch) -> None:
    """本地模型（无 key）时，**显式删掉**继承来的 `LLM_API_KEY`。

    否则宿主机 shell 里恰好导出的一个 key 会被悄悄用上，
    而用户以为自己选的是本地模型 —— 于是请求发到了云端。
    """
    monkeypatch.setenv("LLM_API_KEY", "sk-stray-from-shell-abcdefgh")
    from app.scans.runner import LaunchSpec, StrixRunner

    spec = LaunchSpec(
        scan_id="scan_local",
        target="local.example.com",
        target_kind="url",
        skills=("web",),
        run_dir=Path("."),
        log_path=Path("x.log"),
        llm_model="ollama/qwen",
        llm_api_key="",  # 本地模型
        llm_api_base="http://127.0.0.1:11434",
    )
    env = StrixRunner().env(spec)

    assert "LLM_API_KEY" not in env
    assert "sk-stray-from-shell-abcdefgh" not in "\n".join(env.values())


# ══════════════════════════════════════════════════════════
# 要求 4b：脱敏正则
# ══════════════════════════════════════════════════════════

SECRETS: list[tuple[str, str]] = [
    (
        "数据库连接串（任务 8 点名）",
        "postgresql+psycopg://suvex:suvex_dev_local@127.0.0.1:55432/suvex",
    ),
    ("短连接的连接串", "postgresql://u:hunter2@db:5432/suvex"),
    ("JWT", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"),
    ("Bearer 头", "Authorization: Bearer svx_abcdefghijklmnopqrst"),
    ("OpenAI 风格 key", "api key is sk-proj-abcdefghijklmnopqrstuvwx"),
    ("本平台令牌", "token svx_0123456789ABCDEFGHJKM"),
]


@pytest.mark.parametrize(("label", "text"), SECRETS, ids=[s[0] for s in SECRETS])
def test_redact_masks_every_secret_shape(label: str, text: str) -> None:
    """★ 要求 4 点名的三类：**数据库连接串**、JWT、以及 API key。

    每一条都断言"原文不再出现"，而不只是"出现了 `****`" ——
    后者可以被"在末尾补一个 ****"满足，而密钥还在。
    """
    cleaned = redact(text)

    assert "****" in cleaned, f"{label} 没有被脱敏：{cleaned}"
    for fragment in (
        "suvex_dev_local",
        "hunter2",
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        "abcdefghijklmnopqrst",
        "sk-proj-abcdefghijklmnopqrstuvwx",
        "0123456789ABCDEFGHJKM",
    ):
        assert fragment not in cleaned, f"{label} 里还剩 {fragment!r}：{cleaned}"


def test_redact_leaves_ordinary_text_alone() -> None:
    """反面：不能把正常文本也吃掉。

    一个"见什么都脱敏"的实现会让日志彻底失去诊断价值 ——
    而日志没有诊断价值时，人们会去关掉脱敏。
    """
    for text in [
        "扫描 api.example.com 完成，用时 5 秒",
        "postgresql://db:5432/suvex 里没有口令，不该被动",
        "https://api.deepseek.com/v1/chat/completions",
        "sk- 后面什么都没有，不算密钥",
    ]:
        assert redact(text) == text, f"正常文本被动过了：{text!r} → {redact(text)!r}"


# ══════════════════════════════════════════════════════════
# 要求 4c：错误不泄漏内部信息
# ══════════════════════════════════════════════════════════


def test_unhandled_exception_does_not_leak_internals(monkeypatch) -> None:
    """★ 注意这里**不能**用 `ready_client` 夹具。

    `TestClient` 默认 `raise_server_exceptions=True` —— 它会把服务端异常
    重新抛到测试进程里，于是"响应里有没有泄漏"这件事根本测不到
    （请求在抛异常那一步就结束了）。所以这里自己建一个
    `raise_server_exceptions=False` 的客户端，走完整的"500 响应"路径。
    """
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post(
            "/api/v1/auth/setup", json={"username": GOOD_USERNAME, "password": "SuvexDevPass2026"}
        ).status_code == 201
        _assert_no_internals_leak(client, monkeypatch)


def _assert_no_internals_leak(client: TestClient, monkeypatch) -> None:
    """★ 未捕获异常的响应里**没有**堆栈、异常类名、SQL 或文件路径。

    堆栈只进服务端日志。响应里连异常类名都不给 ——
    类名本身就是内部结构信息（"它用了 psycopg"、"它在 `scans/service.py`"）。
    """
    from app.scans import service as scans_service

    def boom(*_args, **_kwargs):
        raise RuntimeError(
            "内部细节：/srv/app/scans/service.py 第 42 行，"
            "postgresql://suvex:suvex_dev_local@127.0.0.1:55432/suvex 连接失败"
        )

    monkeypatch.setattr(scans_service, "list_scans", boom)

    response = client.get("/api/v1/scans")

    assert response.status_code == 500
    body = response.text
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    for leaked in (
        "RuntimeError",
        "Traceback",
        "service.py",
        "suvex_dev_local",
        "postgresql://",
        "第 42 行",
    ):
        assert leaked not in body, f"响应里泄漏了 {leaked!r}：{body[:300]}"


def test_validation_error_does_not_echo_user_input(ready_client: TestClient) -> None:
    """校验错误**不回声**用户输入（契约 §1.3 第 3 条）。

    典型危害：用户把口令打进了"目标"字段，而错误信息把它原样回显 ——
    那条口令就进了响应、进了浏览器历史、进了任何抓这个响应的地方。
    """
    secret_typo = "hunter2-should-not-be-echoed"
    response = ready_client.post(
        "/api/v1/scans",
        json={
            "name": "",
            "target": secret_typo,
            "targetKind": "url",
            "skills": ["web"],
            "providerId": "prov_x",
            "authorization": dict(VALID_AUTH),
        },
    )
    assert response.status_code == 400
    assert secret_typo not in response.text, "校验错误把用户输入原样回显了"


# ══════════════════════════════════════════════════════════
# 要求 5：扫描创建限流
# ══════════════════════════════════════════════════════════


@pytest.fixture()
def provider_id(ready_client: TestClient) -> str:
    created = ready_client.post(
        "/api/v1/providers",
        json={
            "label": "DeepSeek",
            "model": "deepseek-chat",
            "kind": "cloud",
            "apiKey": "sk-test-RATE-3f8a1c4e9b7d30625a8c1f4e",
            "baseUrl": "https://api.deepseek.com",
            "note": "限流测试用",
            "isDefault": True,
        },
    )
    assert created.status_code in (200, 201), created.text
    return created.json()["provider"]["id"]


def _scan_body(provider_id: str, target: str) -> dict:
    return {
        "name": "",
        "target": target,
        "targetKind": "url",
        "skills": ["web"],
        "providerId": provider_id,
        "authorization": dict(VALID_AUTH),
    }


def test_scan_creation_is_rate_limited(
    ready_client: TestClient, provider_id: str, scan_config
) -> None:
    """★ 要求 5：扫描创建要有限流。

    ★ **为什么它需要限流，而系统本来就有并发上限**：
    `max_concurrent_scans` 管的是"同时在跑几个"，超出的会**排队** ——
    所以脚本疯狂提交不会被拒绝，它会把队列堆到几千条，
    每一条都带着一份授权声明进库。那不是"慢"，那是
    **把队列变成一个持久化的攻击排程**。

    断言里同时检查 `Retry-After`：前端拿它告诉用户"多久后再试"，
    而 CORS 的 `expose_headers` 也专门为它放行了这个头 ——
    不返回的话那条配置就是死的。
    """
    scan_config(SUPEX_SCAN_CREATE_LIMIT_PER_MINUTE="2", SUPEX_SCAN_CREATE_LIMIT_PER_DAY="2")

    for index in range(2):
        created = ready_client.post(
            "/api/v1/scans", json=_scan_body(provider_id, f"rate{index}.example.com")
        )
        assert created.status_code == 201, created.text

    blocked = ready_client.post(
        "/api/v1/scans", json=_scan_body(provider_id, "rate-blocked.example.com")
    )
    assert blocked.status_code == 429, blocked.text
    error = blocked.json()["error"]
    assert error["code"] == "RATE_LIMITED"
    assert blocked.headers.get("retry-after"), "429 必须带 Retry-After"


def test_rate_limited_request_leaves_no_scan_behind(
    ready_client: TestClient, provider_id: str, scan_config
) -> None:
    """★ 被限流的那次**不该留下任何痕迹**（连一条 queued 都不该有）。

    限流检查在落库**之前**执行。反过来的话，用户会看到一条
    永远跑不起来的扫描记录，而界面上没有任何解释 ——
    那比直接 429 难懂得多。
    """
    scan_config(SUPEX_SCAN_CREATE_LIMIT_PER_MINUTE="1", SUPEX_SCAN_CREATE_LIMIT_PER_DAY="1")

    assert (
        ready_client.post("/api/v1/scans", json=_scan_body(provider_id, "one.example.com")).status_code
        == 201
    )
    assert (
        ready_client.post("/api/v1/scans", json=_scan_body(provider_id, "two.example.com")).status_code
        == 429
    )

    listed = ready_client.get("/api/v1/scans").json()
    assert len(listed) == 1, f"被限流的请求留下了扫描：{[s['target'] for s in listed]}"


def test_scan_rate_limit_is_per_credential_not_global(
    ready_client: TestClient, provider_id: str, scan_config
) -> None:
    """限流按凭据分桶：一个人的配额不该影响另一个人。

    只按 IP 记账的话，反代后面所有用户共用一个桶 ——
    一个人刷满，其他所有人被 429。那是最常见的限流误伤。
    """
    scan_config(SUPEX_SCAN_CREATE_LIMIT_PER_MINUTE="1", SUPEX_SCAN_CREATE_LIMIT_PER_DAY="1")

    assert (
        ready_client.post("/api/v1/scans", json=_scan_body(provider_id, "sess.example.com")).status_code
        == 201
    )
    # 换成令牌通道 —— 另一个桶，应该还能建
    token = ready_client.post(
        "/api/v1/settings/tokens",
        json={"label": "CI", "scopes": ["scan"], "expiresAt": None},
    ).json()["plaintext"]

    other = ready_client.post(
        "/api/v1/scans",
        headers={"Authorization": f"Bearer {token}"},
        json=_scan_body(provider_id, "token.example.com"),
    )
    assert other.status_code == 201, f"另一个凭据被误伤了：{other.text}"


def test_login_rate_limit_still_works(client: TestClient) -> None:
    """要求 5 的另一半：登录限流**仍然**生效（它属于任务 1，这里只做回归）。

    两条限流器各有自己的 `lru_cache`，容易在重构时漏掉一条 ——
    而漏掉登录限流就是允许在线爆破。
    """
    client.post("/api/v1/auth/setup", json={"username": GOOD_USERNAME, "password": "SuvexDevPass2026"})
    codes = [
        client.post("/api/v1/auth/login", json={"username": GOOD_USERNAME, "password": "wrong-password"}).status_code
        for _ in range(8)
    ]
    assert 429 in codes, f"登录没有被限流：{codes}"
    assert codes.count(401) >= 1, "错误口令应该是 401，不是别的"


# ══════════════════════════════════════════════════════════
# 要求 3：授权确认字段真的落库（含令牌通道）
# ══════════════════════════════════════════════════════════


def test_machine_token_scan_records_who_authorized(
    ready_client: TestClient, provider_id: str, db_session: Session
) -> None:
    """★ 要求 3：授权确认必须**服务端校验并落库**，不是前端一个勾选框。

    而这一条专门盯**令牌通道** —— 因为这里原来有个真 bug：
    `authorized_by` 写的是 `identity.user_id`，而令牌通道下它是 `None`，
    于是"哪个 CI 令牌授权对目标发包"完全没有留痕。
    这恰好是机器场景下最该回答的问题（人点的有人负责，脚本点的谁负责？）。
    """
    created = ready_client.post(
        "/api/v1/settings/tokens",
        json={"label": "夜间扫描", "scopes": ["scan"], "expiresAt": None},
    )
    token_id = created.json()["token"]["id"]
    token = created.json()["plaintext"]
    ready_client.cookies.clear()

    response = ready_client.post(
        "/api/v1/scans",
        headers={"Authorization": f"Bearer {token}"},
        json=_scan_body(provider_id, "authz-token.example.com"),
    )
    assert response.status_code == 201, response.text

    from app.scans.models import Scan

    row = db_session.get(Scan, response.json()["id"])
    assert row is not None
    # 六列留痕一列都不能空
    assert row.authorized_by == f"token:{token_id}"
    assert row.authorization_statement == AUTH_STATEMENT
    assert row.authorization_received_at is not None
    assert row.authorization_ip
    assert row.authorization_user_agent


def test_authorization_is_enforced_not_ignored(
    ready_client: TestClient, provider_id: str
) -> None:
    """★ 要求 3 的实质：这个字段**真的被校验**，而不是被静默忽略。

    四种送法都要有明确结果 —— 尤其"漏传"必须是 403 而不是
    "反正前端会填，后端不管"。
    """
    base = _scan_body(provider_id, "authz-enforced.example.com")

    # 1) 完全漏掉 → 403（不是 400：这是授权缺失，不是内容不合法）
    without = {k: v for k, v in base.items() if k != "authorization"}
    missing = ready_client.post("/api/v1/scans", json=without)
    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "TARGET_NOT_AUTHORIZED"

    # 2) 传了但没勾 → 403
    unchecked = ready_client.post(
        "/api/v1/scans",
        json={**base, "authorization": {"confirmed": False, "statement": AUTH_STATEMENT}},
    )
    assert unchecked.status_code == 403
    assert unchecked.json()["error"]["code"] == "TARGET_NOT_AUTHORIZED"

    # 3) 勾了但措辞不对 → 400（内容不合法，与"没授权"是两回事）
    wrong = ready_client.post(
        "/api/v1/scans",
        json={**base, "authorization": {"confirmed": True, "statement": "我确认过了"}},
    )
    assert wrong.status_code == 400
    assert "authorization.statement" in wrong.json()["error"]["details"]

    # 4) 措辞差一个字也不行（逐字比对，不是"包含"）
    near = ready_client.post(
        "/api/v1/scans",
        json={
            **base,
            "authorization": {"confirmed": True, "statement": AUTH_STATEMENT + "。"},
        },
    )
    assert near.status_code == 400, "措辞多了个句号却通过了 —— 逐字比对没有生效"

    # 一个都不该建出来
    assert ready_client.get("/api/v1/scans").json() == []


def test_operator_strix_configuration_still_passes_through(monkeypatch) -> None:
    """★ 运维设置的 `STRIX_*` 必须透传 —— 白名单不能把运维手段一起挡掉。

    这条是**修我自己引入的回归**时补的：第一版白名单只认固定名字，
    于是运维设的 `STRIX_SANDBOX_MEM_LIMIT`、`STRIX_RUNTIME_BACKEND`
    到不了 strix。而其中几条恰恰是**隔离与限流的手段** ——
    挡掉它们等于"为了安全而削弱了安全"。

    边界是**命名空间**：`STRIX_*` / `DOCKER_*` 属于那个工具的配置，放行；
    `SUPEX_*` 是我们的机密，一律不放。这条测试把两侧都钉住。
    """
    monkeypatch.setenv("STRIX_SANDBOX_MEM_LIMIT", "2g")
    monkeypatch.setenv("STRIX_RUNTIME_BACKEND", "docker")
    monkeypatch.setenv("STRIX_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv("SUPEX_SECRET_KEY", "must-not-leak")

    from app.scans.runner import StrixRunner

    env = StrixRunner().env(_spec())

    assert env.get("STRIX_SANDBOX_MEM_LIMIT") == "2g"
    assert env.get("STRIX_RUNTIME_BACKEND") == "docker"
    assert env.get("STRIX_NO_UPDATE_CHECK") == "1"
    assert "must-not-leak" not in "\n".join(env.values())


def test_sandbox_network_is_forwarded_when_configured(monkeypatch) -> None:
    """★ `SUPEX_SANDBOX_NETWORK` → strix 的 `STRIX_DOCKER_SANDBOX_NETWORK`。

    这是任务 8 在配置层面能给的最直接的隔离收窄：把沙箱放进专用网络，
    别和业务容器共用一个 bridge。见 docs/security-checklist.md 的 F-02。
    """
    from app.core.config import get_settings
    from app.scans.runner import StrixRunner

    monkeypatch.setenv("SUPEX_SANDBOX_NETWORK", "suvex-sandbox")
    get_settings.cache_clear()
    try:
        env = StrixRunner().env(_spec())
        assert env.get("STRIX_DOCKER_SANDBOX_NETWORK") == "suvex-sandbox"
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_sandbox_network_is_absent_by_default(monkeypatch) -> None:
    """未配置时**不动**它 —— 保持 strix 的默认（bridge），不替用户做决定。"""
    from app.core.config import get_settings
    from app.scans.runner import StrixRunner

    monkeypatch.delenv("SUPEX_SANDBOX_NETWORK", raising=False)
    monkeypatch.delenv("STRIX_DOCKER_SANDBOX_NETWORK", raising=False)
    get_settings.cache_clear()
    try:
        env = StrixRunner().env(_spec())
        assert "STRIX_DOCKER_SANDBOX_NETWORK" not in env
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
