"""构造前置数据的工厂。

单独一个模块而不是写在各测试文件里：同一份「一次扫描 + 一个 agent + 几条事件」
在后面几个任务（扫描、漏洞、报告）的测试里还要用，复制粘贴出来的第二份
必然与第一份漂移 —— 而漂移的那一份通常就是断言失败时最难解释的那个。

所有工厂都**显式 commit**：测试里另一个连接（TestClient 的请求会话）
要能看见这些行，未提交的数据对它不可见。
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.core.vocab import (
    TERMINAL_SCAN_STATUSES,
    AgentPhase,
    AgentRole,
    AgentStatus,
    ProviderKind,
    ScanStatus,
    SkillId,
    TargetKind,
)
from app.events.models import ActivityEvent, Agent
from app.events.service import append_event
from app.providers.models import ProviderAccount
from app.scans.models import Scan


def make_provider(
    db: Session,
    *,
    provider_id: str = "prov_deepseek",
    default: bool = True,
    api_key: str = "sk-not-a-real-key-0000",
) -> str:
    """造一个 provider。

    凭据走**真实的加密路径**（`crypto.encrypt_secret`），不是塞一段假密文 ——
    否则"读取时要能解密"这件事在测试里从来没有被验证过，
    而真跑到 `verify` 才会发现密钥对不上。
    """
    from app.core.crypto import encrypt_secret, mask_secret

    provider = ProviderAccount(
        id=provider_id,
        label="DeepSeek",
        model="deepseek-chat",
        kind=ProviderKind.CLOUD.value,
        api_key_encrypted=encrypt_secret(api_key) if api_key else None,
        api_key_hint=mask_secret(api_key) if api_key else None,
        base_url="https://api.deepseek.com",
        is_default=default,
        verified=True,
        note="按量计费，覆盖大多数场景。",
    )
    db.add(provider)
    db.commit()
    return provider.id


def make_scan(
    db: Session,
    *,
    provider_id: str = "prov_deepseek",
    target: str = "api.acme.internal",
    status: str = ScanStatus.QUEUED.value,
    scan_id: str | None = None,
) -> Scan:
    if db.get(ProviderAccount, provider_id) is None:
        make_provider(db, provider_id=provider_id)

    moment = now_utc()
    # ⚠️ 终态必须有 `finished_at`，非终态必须没有 —— 这是 `scans` 上的
    # CHECK 约束 `ck_scans_finished_at_matches_terminal_status`。
    # 工厂不遵守它的话，造一个 `completed` 扫描就会撞在数据库约束上，
    # 而报错看起来像"工厂写错了"而不是"约束在起作用"。
    terminal = status in TERMINAL_SCAN_STATUSES
    scan = Scan(
        id=scan_id or new_id("scan"),
        name=target,
        target=target,
        target_kind=TargetKind.URL.value,
        status=status,
        skills=[SkillId.WEB.value, SkillId.API.value],
        provider_id=provider_id,
        started_at=moment,
        finished_at=moment + timedelta(seconds=120) if terminal else None,
        # `durationSec` 只在终态有值；running/queued 时是 NULL 而**不是 0**
        # （契约 §2.1：存 0 会把"正在跑的"排到"最快的"位置）。
        duration_sec=120 if terminal else None,
        agent_count=1,
        # `Partial`：只写有值的档位，没有的那档**键不存在**（不是 0）。
        findings={"high": 1},
        progress=None if terminal else 0.25,
        failure_reason="测试：目标不可达" if status == ScanStatus.FAILED.value else None,
        # 授权留痕六列。真实流程里由 POST /scans 写入（任务 3），
        # 测试里直接造 —— 但六列都要有值，否则"留痕完整"这件事没被覆盖。
        authorized_by="user_test",
        authorization_statement="我有权对此目标进行安全测试",
        authorization_confirmed_at=moment - timedelta(minutes=1),
        authorization_received_at=moment,
        authorization_ip="127.0.0.1",
        authorization_user_agent="pytest",
    )
    db.add(scan)
    db.commit()
    return scan


def _httpx_shim(**overrides):  # noqa: ANN003, ANN202
    """造一个"只在指定名字上偏离真 httpx"的**模块替身**。

    ## ★★ 为什么需要它（与 `tests/kernel/conftest.py` 那个是同一族问题）

    `app.providers.llm.httpx` 与 `app.kernel.client.httpx` 一样，**都是全局
    那一个 httpx 模块对象**。所以原来那种
    `monkeypatch.setattr("app.providers.llm.httpx.Client", factory)`
    改的是**httpx 模块自己的属性** —— 任何在测试期间读 `httpx.Client`
    的代码都会拿到工厂函数。

    ⚠️ 这条**目前是潜伏的**：`netguard.ensure_guarded` 之前判的是
    `httpx.AsyncClient`，而这里替换的是 `Client`，所以还没人踩到。
    但只要哪天有人去读 `httpx.Client`（判类型、做 isinstance），
    就会得到同一种 `TypeError`。所以一并修掉，不留同一族的第二个坑。

    ## 转发是**全量**的，不留手写清单

    用 `__getattr__` 把没覆盖的名字全部转发给真模块 —— 手写清单会漂移，
    而漂移的症状是 `AttributeError`，看着像"库换了 API"。
    （`llm.py` 用到 `Client` / `ConnectError` / `HTTPError` / `Timeout` /
    `TimeoutException`，真 httpx 模块里这些名字都在，转发覆盖得到。）
    """
    import types

    import httpx

    shim = types.ModuleType("httpx_shim")
    shim.__dict__.update(overrides)
    shim.__dict__["__getattr__"] = lambda name: getattr(httpx, name)
    return shim


def patch_llm_transport(monkeypatch, handler) -> None:  # noqa: ANN001
    """把 `app.providers.llm` **这个模块里**的 `httpx.Client` 换成走
    `MockTransport` 的版本。

    打的是模块里那个真实的 `httpx.Client` 类，所以 `llm.probe` 里的
    URL 构造、请求头组装、状态码与响应解析**全都真的跑一遍** ——
    只有网络那一层是假的。这比直接 mock 掉 `probe()` 有价值得多：
    后者会把最值得测的那部分（"我们到底发了什么请求、怎么理解对方的回应"）
    一起跳过。

    ## ★★ 换的是**模块绑定**，不是 httpx 模块的属性

    见 `_httpx_shim` 的说明：原来的写法会污染全局 httpx 模块。

    ⚠️ `real_client = httpx.Client` 必须在造 shim **之前**取 ——
    在工厂里引用"刚被替换的名字"会拿到工厂自己 → `RecursionError`。
    """
    import httpx

    real_client = httpx.Client

    def factory(*args, **kwargs):  # noqa: ANN001
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    # ⚠️ 对**模块绑定**打补丁（不是 `…httpx.Client` 那种属性路径）。
    monkeypatch.setattr("app.providers.llm.httpx", _httpx_shim(Client=factory))


def llm_ok(content: str = "pong"):  # noqa: ANN201
    """一个永远成功的假 LLM 端点（OpenAI 兼容形状）。"""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": content}}]},
            request=request,
        )

    return handler


def llm_status(status: int, message: str = "denied"):  # noqa: ANN201
    """一个永远返回指定错误的假端点。"""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": message}}, request=request)

    return handler


def make_agent(
    db: Session,
    scan_id: str,
    *,
    role: str = AgentRole.SCANNER.value,
    status: str = AgentStatus.RUNNING.value,
    agent_id: str | None = None,
) -> Agent:
    agent = Agent(
        id=agent_id or new_id("agent"),
        scan_id=scan_id,
        role=role,
        status=status,
        target="api.acme.internal",
        event_count=0,
        last_active_at=now_utc(),
    )
    db.add(agent)
    db.commit()
    return agent


def make_event(
    db: Session,
    *,
    scan_id: str,
    agent_id: str,
    type: str,
    summary: str,
    agent_phase: str = AgentPhase.SCAN.value,
    detail: dict | None = None,
    at_offset_seconds: int = 0,
) -> ActivityEvent:
    event = append_event(
        db,
        scan_id=scan_id,
        agent_id=agent_id,
        agent_phase=agent_phase,
        type=type,
        summary=summary,
        detail=detail,
        at=now_utc() + timedelta(seconds=at_offset_seconds),
    )
    db.commit()
    db.refresh(event)
    return event


def make_scan_with_events(db: Session, *, count: int = 3) -> tuple[Scan, Agent, list[ActivityEvent]]:
    """一次扫描 + 一个 agent + `count` 条完整事件（8 种类型里挑几种）。"""
    scan = make_scan(db)
    agent = make_agent(db, scan.id)
    events = [
        make_event(
            db,
            scan_id=scan.id,
            agent_id=agent.id,
            type="tool_call",
            agent_phase=AgentPhase.SCAN.value,
            summary="执行 nmap -sV 目标端口扫描",
            detail={
                "kind": "tool_call",
                "tool": "nmap",
                "command": "nmap -sV api.acme.internal",
                # exitCode 可为 null（进程被信号杀死时没有退出码）
                "exitCode": None,
                "durationMs": 4200,
            },
            at_offset_seconds=index,
        )
        for index in range(count)
    ]
    return scan, agent, events
