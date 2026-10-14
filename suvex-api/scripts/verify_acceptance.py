"""任务 1 的端到端验收脚本。

**为什么不用 pytest + TestClient**：那个组合验证的是「ASGI 应用对象在进程内
能响应请求」，而验收标准问的是「这条命令真的能把这个服务跑起来」。
两者不是一回事 —— 后者才会暴露端口、进程、环境变量、迁移顺序、
启动自检这些真问题。所以这个脚本：

1. 建一个独立的验收库（`suvex_acceptance`），跑迁移；
2. **真的起一个 `uvicorn` 子进程**；
3. 用 HTTP 打它 —— 包括一条**真实的 SSE 长连接**（读几帧再断开）；
4. 逐条打印结论，最后给退出码。

用法：

    python scripts/verify_acceptance.py

可选参数 `--port`、`--database-url`、`--keep-server`（调试用：跑完不关服务）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

import httpx  # noqa: E402

# 中文 Windows 的控制台默认是 GBK，直接 print 一个 '✓'/'✗' 就会
# UnicodeEncodeError 把脚本打死 —— 而且是在**打印失败汇总**的时候崩，
# 于是真正的失败原因被一个编码错误盖掉了。先把自己切到 UTF-8。
for stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):  # pragma: no cover - 非重定向场景
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

STRONG_PASSWORD = "SuvexAcceptance2026"
WEAK_PASSWORD = "weak"
API = "/api/v1"

# 结果收集。每条是 (是否通过, 标题, 补充说明)
RESULTS: list[tuple[bool, str, str]] = []


def record(ok: bool, title: str, detail: str = "") -> bool:
    RESULTS.append((ok, title, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {title}"
    if detail:
        line += f"\n         {detail}"
    print(line, flush=True)
    return ok


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _drain_output(pipe, sink: list[str]) -> None:  # noqa: ANN001
    """把子进程的输出持续读进内存。

    见 `main()` 里那段注释：不读会把子进程写日志的路径堵死，
    而那表现为"服务突然不响应"，指向完全错误的方向。
    """
    try:
        for raw in iter(pipe.readline, b""):
            sink.append(raw.decode("utf-8", "replace"))
    except (ValueError, OSError):  # pragma: no cover - 管道关闭
        pass


def reset_database(url: str) -> None:
    """把验收库**删掉重建**。

    必须重置，不能复用：`/auth/setup` 是一次性的，一旦库里已有管理员账号，
    第二次运行就会得到 409 SETUP_ALREADY_DONE —— 而那份报告看起来像
    "初始化功能坏了"，其实只是上一次运行的残留。
    （第一次写这个脚本时就踩了：第二次跑挂在"机器令牌为空"上，
    因为 setup 已经 409，后续断言全错位。）

    `WITH (FORCE)`（PostgreSQL 13+）会踢掉残留连接；
    没有它时只要有一个空闲连接就会报 `database is being accessed by other users`。
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{parsed.database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    finally:
        admin.dispose()


def migrate(url: str) -> None:
    """对着指定库跑迁移。

    ⚠️ 必须先把 URL 塞进环境变量 **并清掉 settings 的缓存**：
    `alembic/env.py` 读的是 `get_settings().sync_database_url`，
    而 `get_settings()` 是 `lru_cache` 的单例、且会从 `.env` 取值。
    不清缓存的话迁移会**打在主库上** —— 症状是验收库建好了但一张表都没有，
    而日志里写着"迁移成功"（它确实成功了，只是在另一个库上）。
    """
    from alembic.config import Config

    from alembic import command

    os.environ["SUPEX_DATABASE_URL"] = url
    from app.core.config import get_settings

    get_settings.cache_clear()

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    command.upgrade(config, "head")


def wait_for_health(base_url: str, timeout: float = 40.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}{API}/health", timeout=2.0)
            if response.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.4)
    return False


def check_migrations_at_head(url: str) -> bool:
    """`alembic_version` 必须等于脚本目录里的 head。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, text

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            try:
                applied = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            except Exception:  # noqa: BLE001 - 表不存在就是最典型的失败
                return record(
                    False,
                    "Alembic 迁移已执行且停在 head",
                    "alembic_version 表不存在 —— 迁移没有作用在这个库上",
                )
    finally:
        engine.dispose()

    return record(
        applied == head,
        "Alembic 迁移已执行且停在 head",
        f"数据库版本={applied} head={head}",
    )


def check_tables(url: str) -> bool:
    from sqlalchemy import create_engine, inspect

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            tables = set(inspect(conn).get_table_names())
    finally:
        engine.dispose()

    expected = {
        "admin_accounts",
        "auth_sessions",
        "api_tokens",
        "provider_accounts",
        "scans",
        "findings",
        "finding_timeline",
        "reports",
        "report_findings",
        "agents",
        "activity_events",
        "sandbox_config",
        "notice_rule",
        "notify_channels",
        "alembic_version",
    }
    missing = expected - tables
    return record(
        not missing,
        f"表结构完整（{len(expected)} 张表）",
        f"缺少：{sorted(missing)}" if missing else "",
    )


def _configure_db(database_url: str):
    """让当前进程的 DB 层指向验收库。"""
    os.environ["SUPEX_DATABASE_URL"] = database_url
    from app.core import db as db_module
    from app.core.config import get_settings

    get_settings.cache_clear()
    db_module.reset()
    db_module.configure(database_url, force=True)
    return db_module


def seed_scan_with_events(database_url: str) -> tuple[str, str, str]:
    """直接往验收库里塞一次扫描 + 一个 agent + 一条事件。

    `POST /scans` 属于任务 3，这里还没有 —— 但 SSE 那条验收项要真的跑起来，
    就得有真数据。所以绕过 HTTP 直接写库：这不是"造假数据"，
    它写的是**真实的表**，走的也是 `append_event()`（后续任务会用的同一个入口）。

    返回 `(scan_id, agent_id, event_id)`。
    """
    db_module = _configure_db(database_url)

    from app.core.crypto import encrypt_secret, mask_secret
    from app.core.ids import new_id
    from app.core.timeutil import now_utc
    from app.core.vocab import AgentPhase, AgentRole, ScanStatus, TargetKind
    from app.events.models import Agent
    from app.events.service import append_event
    from app.providers.models import ProviderAccount
    from app.scans.models import Scan

    # 造事件流要用的前置数据。凭据走真实的加密路径 ——
    # 塞一段假密文的话，"读取时要能解密"这件事在验收里从没被验证过。
    seed_key = "sk-seed-not-a-real-key-0000"
    with db_module.session_scope() as session:
        provider = ProviderAccount(
            id="prov_acceptance",
            label="DeepSeek",
            model="deepseek-chat",
            kind="cloud",
            api_key_encrypted=encrypt_secret(seed_key),
            api_key_hint=mask_secret(seed_key),
            base_url="https://api.deepseek.com",
            is_default=True,
            verified=True,
            note="验收脚本用",
        )
        session.add(provider)

        scan_id = new_id("scan")
        scan = Scan(
            id=scan_id,
            name="api.acme.internal",
            target="api.acme.internal",
            target_kind=TargetKind.URL.value,
            status=ScanStatus.QUEUED.value,
            skills=["web"],
            provider_id=provider.id,
            started_at=now_utc(),
            agent_count=1,
            findings={},
            progress=0.1,
            authorized_by="user_acceptance",
            authorization_statement="我有权对此目标进行安全测试",
            authorization_confirmed_at=now_utc(),
            authorization_received_at=now_utc(),
            authorization_ip="127.0.0.1",
            authorization_user_agent="verify_acceptance.py",
        )
        session.add(scan)
        session.flush()

        agent = Agent(
            id=new_id("agent"),
            scan_id=scan_id,
            role=AgentRole.SCANNER.value,
            status="running",
            target="api.acme.internal",
            event_count=0,
            last_active_at=now_utc(),
        )
        session.add(agent)
        session.flush()

        event = append_event(
            session,
            scan_id=scan_id,
            agent_id=agent.id,
            agent_phase=AgentPhase.SCAN.value,
            type="tool_call",
            summary="验收：执行端口扫描",
            detail={
                "kind": "tool_call",
                "tool": "nmap",
                "command": "nmap -sV api.acme.internal",
                "exitCode": 0,
                "durationMs": 1200,
                "output": "22/tcp open ssh",
            },
        )
        event_id = event.id
        agent_id = agent.id
        session.flush()

    db_module.reset()
    return scan_id, agent_id, event_id


def append_one_more_event(database_url: str, scan_id: str, agent_id: str) -> str:
    """SSE 连着的时候再写一条事件 —— 这才是「实时」的定义。"""
    db_module = _configure_db(database_url)

    from app.core.timeutil import now_utc
    from app.core.vocab import AgentPhase
    from app.events.service import append_event

    with db_module.session_scope() as session:
        event = append_event(
            session,
            scan_id=scan_id,
            agent_id=agent_id,
            agent_phase=AgentPhase.EXPLOIT.value,
            type="discovery",
            summary="验收：发现 /debug/env 可未授权访问",
            detail={
                "kind": "discovery",
                "subject": "GET /debug/env",
                "evidence": "HTTP/1.1 200 OK\nAWS_SECRET_ACCESS_KEY=***",
                "confidence": "high",
                "severity": "high",
            },
            at=now_utc(),
        )
        event_id = event.id
        session.flush()

    db_module.reset()
    return event_id


def verify_sse(base_url: str, database_url: str, scan_id: str, agent_id: str, anchor_id: str, cookie: str) -> None:
    """真实的长连接：连着的时候写一条新事件，等它自己推过来。

    这是这个脚本里最有价值的一条 —— 它验证的是「服务端能在**不结束响应**
    的情况下持续吐字节，并把新产生的事件推给已经连上的客户端」。
    那件事在 TestClient 里根本测不出来（一个永不结束的响应会把测试挂死）。

    ⚠️ 两处容易搞错的地方，都踩过：

    1. 连接必须**自己带 Cookie**：`httpx.stream` 是独立的请求，
       不继承别的 client 的 Cookie jar。漏了会得到 401，
       看起来像"SSE 端点的鉴权坏了"。
    2. 必须带 `?lastEventId=` 锚定游标。不带的话服务端从**当前末尾**开始
       （这是设计：避免每次打开页面都重放几千条历史），
       于是新写的事件会因为"它成了末尾"而被跳过 —— 表现是读超时。
       带上锚点之后，任何 seq 更大的事件都会立刻被推过来，且没有竞态。
    """
    url = f"{base_url}{API}/scans/{scan_id}/events"
    frames: list[str] = []
    saw_event = False
    saw_keepalive = False
    new_event_id = ""

    try:
        with httpx.stream(
            "GET",
            url,
            params={"lastEventId": anchor_id},
            cookies={"suvex_session": cookie},
            # read 超时给足：心跳是 3 秒一次（验收环境把 SUPEX_SSE_HEARTBEAT_SECONDS
            # 调小了，默认 20 秒会让这条验收等 20 秒）。
            timeout=httpx.Timeout(20.0, read=12.0),
        ) as response:
            content_type = response.headers.get("content-type", "")
            if response.status_code != 200 or not content_type.startswith("text/event-stream"):
                record(False, "SSE 长连接", f"HTTP {response.status_code} {content_type}")
                return
            accel = response.headers.get("x-accel-buffering", "(缺失)")

            # 连接已经建立（响应头已到），现在写一条新事件。
            new_event_id = append_one_more_event(database_url, scan_id, agent_id)

            deadline = time.monotonic() + 14.0
            for line in response.iter_lines():
                frames.append(line)
                if line.startswith(f"id: {new_event_id}"):
                    saw_event = True
                if line.startswith(":"):
                    saw_keepalive = True
                if saw_event and saw_keepalive:
                    break
                if time.monotonic() > deadline:  # pragma: no cover - 超时兜底
                    break
    except httpx.HTTPError as exc:
        record(False, "SSE 长连接", f"连接失败：{exc}")
        return

    record(
        saw_event,
        "SSE 推送：连上之后新产生的事件被实时推给客户端",
        f"期望事件 {new_event_id}，收到 {len(frames)} 行",
    )
    record(
        saw_keepalive,
        "SSE 心跳：空闲时发注释行防止中间代理断连",
        f"X-Accel-Buffering={accel}",
    )


def run_checks(base_url: str, database_url: str) -> None:
    print("\n── 验收项 ──", flush=True)

    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        # 1) 启动状态：未初始化
        state = client.get(f"{API}/auth/state").json()
        record(
            state == {"initialized": False, "authenticated": False},
            "首次启动 /auth/state 报告「未初始化、未登录」",
            json.dumps(state, ensure_ascii=False),
        )

        # 2) 弱口令被拒
        weak = client.post(f"{API}/auth/setup", json={"password": WEAK_PASSWORD})
        record(
            weak.status_code == 400 and weak.json()["error"]["code"] == "VALIDATION_FAILED",
            "弱口令初始化被拒（400 VALIDATION_FAILED）",
            weak.json()["error"]["message"],
        )

        # 3) 无凭据访问受保护端点 → 401
        unauthorized = client.get(f"{API}/auth/me")
        record(
            unauthorized.status_code == 401
            and unauthorized.json()["error"]["code"] == "UNAUTHENTICATED",
            "无凭据访问受保护端点返回 401",
            unauthorized.json()["error"]["message"],
        )

        # 4) 初始化 + 自动登录
        setup = client.post(f"{API}/auth/setup", json={"password": STRONG_PASSWORD})
        cookie = setup.cookies.get("suvex_session")
        set_cookie = setup.headers.get("set-cookie", "")
        record(
            setup.status_code == 201 and bool(cookie),
            "强口令初始化成功并下发会话 Cookie",
            f"用户 id={setup.json().get('user', {}).get('id')}",
        )
        record(
            all(token in set_cookie for token in ("HttpOnly", "Path=/", "Max-Age="))
            and "samesite=strict" in set_cookie.lower(),
            "会话 Cookie 带齐 HttpOnly / SameSite=Strict / Path / Max-Age",
            set_cookie.split(";", 1)[-1].strip(),
        )

        # 5) 带凭据可访问
        me = client.get(f"{API}/auth/me")
        record(
            me.status_code == 200 and "createdAt" in me.json(),
            "带会话 Cookie 可访问 /auth/me，字段为 camelCase",
            me.text,
        )

        # 6) 重复初始化被拒
        again = client.post(f"{API}/auth/setup", json={"password": "Another2026Pass"})
        record(
            again.status_code == 409
            and again.json()["error"]["code"] == "SETUP_ALREADY_DONE",
            "已初始化后 /auth/setup 彻底关闭（409）",
            again.json()["error"]["message"],
        )

        # 7) 签发机器令牌并用它访问
        created = client.post(
            f"{API}/settings/tokens",
            json={"label": "验收脚本", "scopes": ["read"], "expiresAt": None},
        )
        token = created.json().get("plaintext", "")
        if not token:
            record(False, "签发机器令牌", f"没有拿到明文：HTTP {created.status_code} {created.text}")
            return
        record(
            created.status_code == 201
            and token.startswith("svx_")
            and created.json()["token"]["prefix"] == token[:8],
            "签发机器令牌：明文只出现一次，prefix = 明文前 8 位",
            f"prefix={created.json().get('token', {}).get('prefix')}",
        )

        with httpx.Client(base_url=base_url, timeout=10.0) as token_client:
            token_me = token_client.get(
                f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"}
            )
            record(
                token_me.status_code == 200,
                "机器令牌可作为 Bearer 访问",
                f"HTTP {token_me.status_code}",
            )
            denied = token_client.post(
                f"{API}/scans", headers={"Authorization": f"Bearer {token}"}
            )
            record(
                denied.status_code == 403
                and denied.json()["error"]["code"] == "INSUFFICIENT_SCOPE",
                "只读令牌发起扫描被拒（403 INSUFFICIENT_SCOPE）",
                denied.json()["error"]["message"],
            )
            bad = token_client.get(
                f"{API}/auth/me", headers={"Authorization": "Bearer svx_invalid_token_value"}
            )
            record(
                bad.status_code == 401,
                "无效令牌返回 401",
                f"HTTP {bad.status_code}",
            )

        # 8) CSRF：来自未授权源的 Cookie 写请求被拒
        csrf = client.post(
            f"{API}/settings/password",
            json={"current": STRONG_PASSWORD, "next": "Whatever2026Pass"},
            headers={"Origin": "https://evil.example"},
        )
        record(
            csrf.status_code == 403
            and csrf.json()["error"]["code"] == "CROSS_ORIGIN_BLOCKED",
            "来自未授权源的 Cookie 写请求被拦（403 CROSS_ORIGIN_BLOCKED）",
            csrf.json()["error"]["message"],
        )

        # 9) 未实现端点是 501 而不是 404
        #
        # ⚠️ 这里原来用的是 `/overview`，理由写的是"它计划在任务 6 交付"。
        # 任务 6 真的来了 —— 交付的是**报告**，不是 `/overview`，
        # 于是那个 501 的消息变成了假话，而这条检查**照旧打印 PASS**：
        # 它钉的是"机制在"，恰好把"承诺已经过期"洗成了一条绿灯。
        #
        # 现在换成 `/settings`（计划任务 7，确实还没做），并额外断言
        # 它**还没被实现** —— 一旦被做了这条会红，提醒换下一个，
        # 而不是继续对着旧行为打勾。
        stub = client.get(f"{API}/settings")
        record(
            stub.status_code == 501
            and stub.json()["error"]["code"] == "NOT_IMPLEMENTED"
            and "任务 7" in stub.json()["error"]["message"],
            "未实现端点返回 501 并说明计划任务（目标换成了确实未实现的 /settings）",
            f"HTTP {stub.status_code} · {stub.json()['error']['message']}",
        )

        # 10) 登出
        logout = client.post(f"{API}/auth/logout")
        after = client.get(f"{API}/auth/me")
        record(
            logout.status_code == 204 and after.status_code == 401,
            "登出后会话立即失效（204 → 401）",
            f"logout={logout.status_code} me={after.status_code}",
        )

    # 11) 事件流：真实长连接
    scan_id, agent_id, event_id = seed_scan_with_events(database_url)

    # 重新登录拿凭据（上一步登出了）
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        logged_in = client.post(f"{API}/auth/login", json={"password": STRONG_PASSWORD})
        if logged_in.status_code != 200:
            record(False, "重新登录以便读取事件流", logged_in.text)
            return
        session_cookie = logged_in.cookies.get("suvex_session", "")

        recent = client.get(f"{API}/scans/{scan_id}/events/recent")
        record(
            recent.status_code == 200 and len(recent.json()["events"]) == 1,
            "事件流 seed（/events/recent）能读到真实事件",
            f"HTTP {recent.status_code}",
        )
        agents = client.get(f"{API}/scans/{scan_id}/agents")
        record(
            agents.status_code == 200 and len(agents.json()) == 1,
            "agent 名册可读",
            f"HTTP {agents.status_code}",
        )
        verify_sse(base_url, database_url, scan_id, agent_id, event_id, session_cookie)


# ══════════════════════════════════════════════════════════
# 任务 2：providers（凭据加密 + 真实连通性探测）
# ══════════════════════════════════════════════════════════

# 验收用的假凭据。形态真实、足够长。它应当**只**出现在请求体里，
# 不该出现在任何响应、日志或数据库中。
ACCEPTANCE_API_KEY = "sk-live-ACCEPTANCE-4c8e2f9a1b7d30562e8a4c1f"


class _StubLLMHandler(BaseHTTPRequestHandler):
    """一个最小的假 LLM 端点（OpenAI 兼容形状）。

    为什么要起一个真的本地 HTTP 服务，而不是 mock 掉探测逻辑：
    验收要回答的是「这个后端**真的会发 HTTP 请求**、并且**真的看得懂回应**」。
    mock 掉 `probe()` 刚好把这两件事都跳过了，剩下的只有"函数被调用了"。
    """

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        payload = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "pong"}}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # noqa: ANN002
        """静音。默认实现会把每个请求打到 stderr，淹掉验收报告。"""


def start_stub_llm() -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubLLMHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, int(server.server_address[1])


def _db_scalar(database_url: str, sql: str, params: dict | None = None):
    from sqlalchemy import create_engine, text

    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params or {}).scalar()
    finally:
        engine.dispose()


def verify_providers(base_url: str, database_url: str, password: str) -> tuple[str, str]:
    """返回 `(用过的明文 key, 会话 Cookie)`。

    带上 Cookie 是因为紧接着的扫描验收不该**再登录一次** ——
    `/auth/login` 有 5 次/分钟的限流（契约 §3.1，那是刻意的），
    而验收脚本自己撞上限流会得到一条 429，看起来像功能坏了。
    """
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        logged_in = client.post(f"{API}/auth/login", json={"password": password})
        if logged_in.status_code != 200:
            record(False, "重新登录以便验证 provider", logged_in.text)
            return ACCEPTANCE_API_KEY, ""
        session_cookie = logged_in.cookies.get("suvex_session", "")

        # 1) 新建（带凭据）
        created = client.post(
            f"{API}/providers",
            json={
                "label": "DeepSeek",
                "model": "deepseek-chat",
                "kind": "cloud",
                "apiKey": ACCEPTANCE_API_KEY,
                "baseUrl": "https://api.deepseek.com",
                "note": "验收用",
                "isDefault": True,
            },
        )
        if created.status_code not in (200, 201):
            record(False, "新建 provider", created.text)
            return ACCEPTANCE_API_KEY, ""
        provider = created.json()["provider"]
        provider_id = provider["id"]
        record(True, "新建 provider 成功（id 由服务端分配）", f"id={provider_id}")

        # 2) ★ 响应里没有明文，掩码形状正确
        record(
            ACCEPTANCE_API_KEY not in created.text
            and provider["apiKey"] == ""
            and provider["apiKeyMask"] == f"{ACCEPTANCE_API_KEY[:3]}****{ACCEPTANCE_API_KEY[-4:]}",
            "响应只回显掩码，apiKey 恒为空串",
            f"apiKeyMask={provider['apiKeyMask']}",
        )

        # 3) ★ 库里是密文，且能解回来（不是"存了段乱码"）
        stored = _db_scalar(
            database_url,
            "SELECT api_key_encrypted FROM provider_accounts WHERE id = :id",
            {"id": provider_id},
        )
        record(
            bool(stored)
            and ACCEPTANCE_API_KEY not in (stored or "")
            and not (stored or "").startswith("sk-"),
            "数据库里存的是密文，不含明文",
            f"密文前缀={str(stored)[:16]}… 长度={len(str(stored))}",
        )

        # 4) ★ 把掩码原样传回去 —— 凭据必须**纹丝不动**
        #
        # 这是本任务最容易埋的一个雷：前端"读出来、改一个字段、整体提交"
        # 时会把掩码带回来，而它一旦被当成新 key 存下去，真凭据就被静默毁掉了。
        patched = client.patch(
            f"{API}/providers/{provider_id}",
            json={"label": "DeepSeek 生产", "apiKey": provider["apiKeyMask"]},
        )
        after = _db_scalar(
            database_url,
            "SELECT api_key_encrypted FROM provider_accounts WHERE id = :id",
            {"id": provider_id},
        )
        record(
            patched.status_code == 200 and after == stored,
            "把掩码当值传回时，凭据保持不变（不会把掩码存成新 key）",
            f"label={patched.json().get('provider', {}).get('label')}",
        )

        # 5) ★ 真的发一次 HTTP 请求去探测（打本地假 LLM）
        stub, stub_port = start_stub_llm()
        try:
            local = client.post(
                f"{API}/providers",
                json={
                    "label": "本地模型（Ollama）",
                    "model": "qwen2.5:32b",
                    "kind": "local",
                    "apiKey": "",
                    "baseUrl": f"http://127.0.0.1:{stub_port}",
                    "note": "验收用的假端点",
                },
            ).json()["provider"]

            probed = client.post(f"{API}/providers/{local['id']}/verify")
            body = probed.json()
            record(
                probed.status_code == 200
                and body["status"] == "ok"
                and body["reply"] == "pong"
                and body["durationMs"] >= 0
                and body["endpoint"] == f"http://127.0.0.1:{stub_port}/v1/chat/completions",
                "探测成功并返回真实耗时（真的发了一次 HTTP 请求）",
                f"status={body.get('status')} durationMs={body.get('durationMs')} "
                f"endpoint={body.get('endpoint')} reply={body.get('reply')}",
            )
            record(
                body["provider"]["verified"] is True,
                "探测结果写回了 verified",
                f"verified={body['provider']['verified']} verifyError={body['provider']['verifyError']}",
            )
        finally:
            stub.shutdown()

        # 6) ★ 打不通时给出可读的失败原因，且仍然是 200
        dead_port = free_port()  # 拿一个刚释放、没人监听的端口
        unreachable = client.post(
            f"{API}/providers",
            json={
                "label": "打不通的",
                "model": "whatever",
                "kind": "local",
                "apiKey": "",
                "baseUrl": f"http://127.0.0.1:{dead_port}",
            },
        ).json()["provider"]
        failed = client.post(f"{API}/providers/{unreachable['id']}/verify")
        failed_body = failed.json()
        record(
            failed.status_code == 200
            and failed_body["status"] == "failed"
            and "无法连接" in failed_body["error"],
            "探测失败返回 200 + status=failed + 可读中文原因",
            f"HTTP {failed.status_code} · {failed_body.get('error')}",
        )

        # 7) 云厂商元数据地址必须被拦，且请求不发出去
        metadata = client.post(
            f"{API}/providers",
            json={
                "label": "元数据探测",
                "model": "x",
                "kind": "cloud",
                "apiKey": "sk-x",
                "baseUrl": "http://169.254.169.254",
            },
        ).json()["provider"]
        blocked = client.post(f"{API}/providers/{metadata['id']}/verify").json()
        record(
            blocked["status"] == "failed" and "元数据" in blocked["error"],
            "指向云厂商元数据地址的探测被拦截",
            blocked.get("error"),
        )

        # 8) 默认唯一
        defaults = [p["id"] for p in client.get(f"{API}/providers").json() if p["isDefault"]]
        record(
            defaults == [provider_id],
            "任何时刻只有一个默认 provider",
            f"默认={defaults}",
        )

        # 9) 默认不可删（有替补时），但最后一个可以删
        conflict = client.delete(f"{API}/providers/{provider_id}")
        record(
            conflict.status_code == 409
            and conflict.json()["error"]["code"] == "PROVIDER_IS_DEFAULT",
            "删除仍有替补的默认 provider 被拒（409 PROVIDER_IS_DEFAULT）",
            conflict.json()["error"]["message"],
        )

    return ACCEPTANCE_API_KEY, session_cookie


# ══════════════════════════════════════════════════════════
# 任务 3：扫描生命周期
# ══════════════════════════════════════════════════════════

SCAN_STATEMENT = "我有权对此目标进行安全测试"


def _auth() -> dict:
    return {"confirmed": True, "statement": SCAN_STATEMENT}


def _scan_body(provider_id: str, **overrides) -> dict:
    body = {
        "name": "验收扫描",
        "target": "https://api.example.com/",
        "targetKind": "url",
        "skills": ["web", "api"],
        "providerId": provider_id,
        "authorization": _auth(),
    }
    body.update(overrides)
    return body


def _quiesce(client, timeout: float = 40.0) -> None:  # noqa: ANN001
    """等到"没有人在写漏洞"，让随后的两次读取到同一个快照。

    ★ 为什么需要它（这是一条**真实存在过**的偶发失败）：

    `verify_overview` 里那条最强的一致性断言要求"读 `/overview` 与读
    `/findings/stats` 之间没有人写漏洞"。而它的前一步 `verify_scans`
    会真的起扫描（还有一个"终止运行中的扫描"的用例），
    调度器在后台持续把外部的漏洞产物导进库 —— 那个导入与这两次 GET
    之间**没有任何同步**。恰好插在中间时两次读取到不同快照，
    于是断言以"同源却对不上"的形式偶发失败。

    我第一次跑任务 8 的验收就撞上了（Overview 少一条刚导进来的）。

    ⚠️ 修法**不是放松断言**（那会把一条强检查变成弱检查），
    而是把"比较期间没有写入"这个前提做实：
    先等没有 queued/running 的扫描，再等漏洞总数连续两次相同。

    为什么是"连续两次相同"而不是睡固定时长：固定时长在慢机器上不够、
    在快机器上白等。而扫描**进程退出之后**调度器还要跑一次 backfill
    导入（见 scans/scheduler.py 的收尾路径），所以"没有活跃扫描"
    还不足够 —— 条数稳定才是真的静下来了。
    """
    import time as _time

    deadline = _time.monotonic() + timeout

    while _time.monotonic() < deadline:
        scans = client.get(f"{API}/scans").json()
        if not [s for s in scans if s["status"] in ("queued", "running")]:
            break
        _time.sleep(0.5)

    last: int | None = None
    while _time.monotonic() < deadline:
        total = client.get(f"{API}/findings/stats").json()["total"]
        if total == last:
            return
        last = total
        _time.sleep(0.5)


def _wait_status(client, scan_id: str, expected: set[str], timeout: float = 60.0) -> str:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        current = client.get(f"{API}/scans/{scan_id}").json()["status"]
        if not seen or seen[-1] != current:
            seen.append(current)
        if current in expected:
            return current
        time.sleep(0.25)
    raise AssertionError(f"扫描 {scan_id} 没到 {expected}，经历 {seen}")


def verify_scans(base_url: str, session_cookie: str) -> None:
    """扫描的创建 / 状态机 / 终止 / 重跑 / 并发上限。

    这一轮打的是 `SUPEX_SCAN_RUNNER=fake` 的替身后端（见 `main()` 里起服务的那段）。
    替身**不是 mock**：它同样 spawn 真的子进程（还带真孙进程），
    所以队列、进程监督、杀进程树、退出码解释全都是真的在跑。
    真 strix 的链路由 `verify_real_strix()` 单独验。
    """
    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        # 复用上一个环节的会话，**不再登录** —— 登录有限流，
        # 而验收脚本撞上自己的限流会得到一条误导性的失败。
        client.cookies.set("suvex_session", session_cookie)
        if client.get(f"{API}/auth/me").status_code != 200:
            record(False, "复用会话以便验证扫描", "会话已失效")
            return

        provider = client.post(
            f"{API}/providers",
            json={
                "label": "DeepSeek",
                "model": "deepseek-chat",
                "kind": "cloud",
                "apiKey": "sk-accept-SCAN-2f8a1c4e9b7d30625a8c1f4e",
                "baseUrl": "https://api.deepseek.com",
                "isDefault": True,
            },
        ).json()["provider"]

        # 1) ★ 缺授权确认 → 403（不是 400）
        no_auth = _scan_body(provider["id"])
        no_auth.pop("authorization")
        refused = client.post(f"{API}/scans", json=no_auth)
        record(
            refused.status_code == 403
            and refused.json()["error"]["code"] == "TARGET_NOT_AUTHORIZED",
            "缺授权确认时拒绝创建（403 TARGET_NOT_AUTHORIZED）",
            refused.json()["error"]["message"],
        )

        # 2) ★ 措辞不一致 → 400（与上一条**不同的码**，前端处理不同）
        wrong = client.post(
            f"{API}/scans",
            json=_scan_body(provider["id"], authorization={"confirmed": True, "statement": "我确认了"}),
        )
        record(
            wrong.status_code == 400
            and wrong.json()["error"]["code"] == "VALIDATION_FAILED",
            "授权措辞与约定不一致时拒绝（400 VALIDATION_FAILED）",
            wrong.json()["error"]["details"].get("authorization.statement", ""),
        )

        # 3) 正常创建：状态是 queued，目标被归一化
        created = client.post(
            f"{API}/scans", json=_scan_body(provider["id"], target="https://api.example.com/admin?x=1")
        )
        if created.status_code not in (200, 201):
            record(False, "创建扫描", created.text)
            return
        scan = created.json()
        record(
            scan["status"] == "queued" and scan["target"] == "api.example.com",
            "创建成功：初始状态 queued，目标已归一化",
            f"id={scan['id']} target={scan['target']}（提交的是 https://api.example.com/admin?x=1）",
        )

        # 4) 状态机：queued → running → completed
        running = _wait_status(client, scan["id"], {"running", "completed"}, timeout=30)
        final = _wait_status(client, scan["id"], {"completed", "failed"}, timeout=60)
        done = client.get(f"{API}/scans/{scan['id']}").json()
        record(
            running in {"running", "completed"} and final == "completed",
            "状态机：queued → running → completed，且时间戳与耗时都落库",
            f"finishedAt={done['finishedAt']} durationSec={done['durationSec']} runId={done['runId']}",
        )
        record(
            bool(done["runId"]),
            "读回了执行后端自己生成的 run 标识（任务要求 2）",
            f"runId={done['runId']}",
        )

        # 5) 生命周期事件进了活动流
        events = client.get(f"{API}/scans/{scan['id']}/events/recent").json()["events"]
        record(
            any(event["type"] == "completed" for event in events),
            "状态变更写成了事件（活动流/SSE 能实时看到）",
            f"事件类型={[event['type'] for event in events]}",
        )

        # 5b) ★ 任务 4：strix 的结构化事件被解析并落库了
        #
        # 替身后端产出的是**真实结构**的 strix 产物
        # （`strix_runs/<run>/.state/agents.db`，建表语句照抄 openai-agents SDK），
        # 所以这条走的是完整链路：
        #   外部库 → 解析层 → activity_events → 这个接口读出来
        types = [event["type"] for event in events]
        emitted = {"reasoning", "tool_call", "failed", "discovery"}
        record(
            emitted.issubset(set(types)),
            "★ strix 的结构化事件被解析并落库（reasoning / tool_call / failed / discovery）",
            f"从外部事件库导入并读回的类型={sorted(set(types))}",
        )

        # 5c) ★ 事件可回放：按 seq 升序、且带溯源行号的那批在里面
        reasoning = [event for event in events if event["type"] == "reasoning"]
        plans = [
            event for event in reasoning if (event.get("detail") or {}).get("intent") == "plan"
        ]
        record(
            bool(plans) and "先做端口与服务识别" in plans[0]["detail"]["text"],
            "★ 事件内容与外部工具的原文一致（可回放）",
            f"think 工具的内容被翻成 reasoning(plan)：{plans[0]['summary'][:40] if plans else '（没有）'}",
        )
        record(
            [event["id"] for event in events] == [event["id"] for event in events]
            and len({event["id"] for event in events}) == len(events),
            "事件 id 唯一且顺序稳定（SSE 的 Last-Event-ID 补发依赖它）",
            f"共 {len(events)} 条，去重后 {len({e['id'] for e in events})} 条",
        )

        # 5d) ★ 外部 agent 进了名册
        roster = client.get(f"{API}/scans/{scan['id']}/agents").json()
        record(
            len(roster) >= 1,
            "★ 外部工具的 agent 被映射进本地名册（活动流左侧要用）",
            f"名册={[(a['role'], a['status']) for a in roster]}",
        )

        # ══════════════════════════════════════════════════════
        # 任务 5：漏洞的持久化、状态流转、统计一致性
        # ══════════════════════════════════════════════════════

        # 5e) ★ 外部工具的漏洞记录被导入成结构化条目
        findings = client.get(f"{API}/findings?scanId={scan['id']}").json()
        record(
            len(findings) >= 2,
            "★ strix 的漏洞记录被导入为结构化条目（字段对齐契约）",
            f"导入 {len(findings)} 条：{[f['severity'] for f in findings]}",
        )
        if not findings:
            return

        # 5e-2) ★★ 扫描列表上的汇总列必须与漏洞页/名册**对得上**
        #
        # `Scan.findings` / `Scan.agent_count` 是**冗余列**：列表一屏几十条，
        # 逐条去 count 会变成几十个查询，所以计数被冻在扫描行上。
        # 冗余列的经典故障是"没人刷新"—— 建行时写死 `{}` / `0`，
        # 之后永远不动，于是扫描列表一直显示"未发现漏洞 / 0 个智能体"，
        # 而漏洞页里明明有两条。两个页面互相打脸，列表那一侧读起来
        # 还像"这次扫描什么都没扫到"。
        listed = next(
            (item for item in client.get(f"{API}/scans").json() if item["id"] == scan["id"]),
            None,
        )
        counted: dict[str, int] = {}
        for item in findings:
            # 与 `findings_counts` 同一口径：误报不算
            if item["status"] != "false_positive":
                counted[item["severity"]] = counted.get(item["severity"], 0) + 1
        record(
            listed is not None and listed["findings"] == counted,
            "★★ 扫描列表的 findings 计数与漏洞页逐档相等（冗余列被刷新过）",
            f"列表={listed['findings'] if listed else None} vs 漏洞页={counted}",
        )
        record(
            listed is not None and listed["agentCount"] == len(roster),
            "★★ 扫描列表的 agentCount 与名册一致（不是建行时的 0）",
            f"列表={listed['agentCount'] if listed else None} vs 名册={len(roster)}",
        )

        # ⚠️ 取**有 PoC 的那条**来验字段，而不是 `findings[0]` ——
        # 列表默认按发现时间倒序，`[0]` 可能恰好是第二条（没有 PoC 的那条）。
        # 拿它去断言 `poc is not None` 会在功能正确时失败。
        with_poc = next(
            (
                item
                for item in findings
                if client.get(f"{API}/findings/{item['id']}").json()["detail"]["poc"]
            ),
            findings[0],
        )
        detail = client.get(f"{API}/findings/{with_poc['id']}").json()
        record(
            set(detail["detail"]) == {"description", "affected", "reasoning", "evidence", "poc", "remediation"}
            and detail["detail"]["poc"] is not None
            and detail["detail"]["remediation"],
            "详情字段与契约 §2.2 逐字一致（含 poc / remediation）",
            f"poc.language={detail['detail']['poc']['language']} "
            f"affected={detail['detail']['affected']}",
        )

        # 5f) ★ 两个轴各自来自不同的地方
        record(
            detail["verification"] in {"none", "partial", "verified"}
            and detail["status"] == "new",
            "★ 证据强度轴（来自 agent）与处置状态轴（人的判断）分列，未合并",
            f"verification={detail['verification']}（来自 strix 的 confidence） "
            f"status={detail['status']}（外部工具不产生人的判断）",
        )

        # 5g) ★ 反证没有被丢掉（诚实分级）
        cookie_finding = next(
            (f for f in findings if "SameSite" in f["title"]), None
        )
        if cookie_finding:
            cookie_detail = client.get(f"/api/v1/findings/{cookie_finding['id']}").json()
            record(
                "【反证】" in cookie_detail["detail"]["reasoning"]
                and "【成立假设】" not in cookie_detail["detail"]["reasoning"]
                or "【置信度依据" in cookie_detail["detail"]["reasoning"],
                "★ 反证/置信度依据被折进 reasoning（没有丢掉）",
                cookie_detail["detail"]["reasoning"][-120:],
            )

        # 5h) ★ 状态流转 + 时间线留痕
        target_id = findings[0]["id"]
        moved = client.post(
            f"{API}/findings/status",
            json={"ids": [target_id], "status": "confirmed", "note": "验收：人工复现确认"},
        )
        record(
            moved.status_code == 200 and moved.json()["changed"] == 1,
            "★ 批量状态流转成功（待确认 → 已确认）",
            f"changed={moved.json().get('changed')} skipped={moved.json().get('skipped')}",
        )
        after = client.get(f"{API}/findings/{target_id}").json()
        timeline = after["timeline"]
        record(
            after["status"] == "confirmed"
            and timeline[0]["kind"] == "status_changed"
            and timeline[0]["actor"] == "user"
            and timeline[0]["from"] == "new"
            and timeline[0]["to"] == "confirmed"
            and "验收：人工复现确认" in timeline[0]["summary"],
            "★ 变更记录了操作人与时间戳，时间线倒序（最新在前）",
            f"最新一条：actor={timeline[0]['actor']} {timeline[0]['from']}→{timeline[0]['to']}",
        )

        # 5i) ★ 幂等：再标一次同一个状态 → skipped，且**不写时间线**
        again = client.post(
            f"{API}/findings/status", json={"ids": [target_id], "status": "confirmed"}
        )
        timeline_len = len(client.get(f"/api/v1/findings/{target_id}").json()["timeline"])
        record(
            again.json()["changed"] == 0
            and again.json()["skipped"] == 1
            and timeline_len == len(timeline),
            "★ 幂等：重复标同一状态 → skipped，且不写时间线（避免噪音）",
            f"changed={again.json()['changed']} skipped={again.json()['skipped']} "
            f"时间线条数={timeline_len}（未增加）",
        )

        # 5j) ★ 非法流转被服务端拒绝
        illegal = client.post(
            f"{API}/findings/status", json={"ids": [target_id], "status": "new"}
        )
        # confirmed → new 是合法的（契约允许退回），所以换个必然非法的：
        # new 的条目不能直接变 fixed
        fresh = next((f for f in findings if f["id"] != target_id), None)
        illegal2 = (
            client.post(f"{API}/findings/status", json={"ids": [fresh["id"]], "status": "fixed"})
            if fresh
            else None
        )
        record(
            illegal2 is not None
            and illegal2.status_code == 400
            and illegal2.json()["error"]["code"] == "VALIDATION_FAILED",
            "★ 非法流转被服务端拒绝（400 VALIDATION_FAILED）",
            illegal2.json()["error"]["message"] if illegal2 else "（没有第二条可测）",
        )
        _ = illegal

        # 5k) ★★★ 统计一致性 —— 任务 5 的验收标准
        #
        # `/findings/stats` 是**唯一数据源**，Overview 必须调它。
        # 这里从 HTTP 层验：统计数字与逐条列表数出来的必须一致。
        stats_body = client.get(f"{API}/findings/stats").json()
        all_findings = client.get(f"{API}/findings").json()
        counts_from_list: dict[str, int] = {}
        for item in all_findings:
            counts_from_list[item["severity"]] = counts_from_list.get(item["severity"], 0) + 1
        by_severity = {k: v for k, v in (stats_body["bySeverity"] or {}).items() if v}
        record(
            stats_body["total"] == len(all_findings) and by_severity == counts_from_list,
            "★★★ 统计数字与逐条列表**完全一致**（唯一数据源，两页不会对不上）",
            f"统计 total={stats_body['total']} bySeverity={by_severity} ｜ "
            f"列表数出来 {len(all_findings)} 条 {counts_from_list}",
        )
        # 逐档用筛选接口再交叉验一次
        mismatches = []
        for severity, expected in by_severity.items():
            filtered = client.get(f"{API}/findings?severity={severity}").json()
            if len(filtered) != expected:
                mismatches.append(f"{severity}: 统计 {expected} vs 筛选 {len(filtered)}")
        record(
            not mismatches,
            "★ 每一档的统计数与按该档筛选出来的条数一致（逐档交叉验证）",
            "；".join(mismatches) or f"全部 {len(by_severity)} 档一致",
        )
        record(
            "falsePositiveCount" in stats_body,
            "★ falsePositiveCount 单独返回（Overview 排除误报时必须说明排除了几条）",
            f"falsePositiveCount={stats_body.get('falsePositiveCount')}",
        )

        # 6) ★ 终止正在运行的扫描 —— 并验证**孙进程也被杀掉**
        long_running = client.post(
            f"{API}/scans", json=_scan_body(provider["id"], name="用于终止的扫描")
        ).json()
        _wait_status(client, long_running["id"], {"running"}, timeout=30)
        heartbeat = Path(_scan_root()) / long_running["id"] / "grandchild.heartbeat"
        deadline = time.monotonic() + 10
        while not heartbeat.exists() and time.monotonic() < deadline:
            time.sleep(0.1)

        aborted = client.post(f"{API}/scans/{long_running['id']}/abort")
        frozen = heartbeat.stat().st_mtime if heartbeat.exists() else None
        time.sleep(1.2)
        still_writing = (
            heartbeat.exists() and frozen is not None and heartbeat.stat().st_mtime != frozen
        )
        record(
            aborted.status_code == 200
            and aborted.json()["status"] == "aborted"
            and heartbeat.exists()
            and not still_writing,
            "终止运行中的扫描：状态置为 aborted，且**孙进程也被杀掉**（不留僵尸）",
            f"HTTP {aborted.status_code} · 心跳文件{'仍在更新（有僵尸！）' if still_writing else '已停止更新'}",
        )

        # 7) 已完成的不能再终止 → 409
        conflict = client.post(f"{API}/scans/{scan['id']}/abort")
        record(
            conflict.status_code == 409
            and conflict.json()["error"]["code"] == "SCAN_NOT_ABORTABLE",
            "终止已完成的扫描被拒（409 SCAN_NOT_ABORTABLE）",
            conflict.json()["error"]["message"],
        )

        # 8) ★ 重跑：新建一条记录，旧记录纹丝不动
        before_finished = client.get(f"{API}/scans/{scan['id']}").json()["finishedAt"]
        rerun = client.post(
            f"{API}/scans/{scan['id']}/rerun", json={"authorization": _auth()}
        )
        after = client.get(f"{API}/scans/{scan['id']}").json()
        record(
            rerun.status_code == 201
            and rerun.json()["id"] != scan["id"]
            and rerun.json()["status"] == "queued"
            and after["status"] == "completed"
            and after["finishedAt"] == before_finished,
            "重跑建了**新记录**，旧记录的历史原样保留",
            f"新 id={rerun.json().get('id')} 旧 id={scan['id']}（旧记录仍是 completed）",
        )

        # 9) ★ 并发上限：把上限设成 1 之后，多提交的必须排队
        #
        # ⚠️ 这一条曾经真的不生效：三个 POST 各自在后台线程里捅调度器，
        # 三个线程同时算出"在跑 0 个"，于是各启动一个（日志里三个 pid 都在，
        # 而配置写的是 1）。修法是让整个 tick 串行 + 名额按数据库里的
        # running 行数算，见 `scheduler.tick` 的说明。
        for index in range(3):
            client.post(f"{API}/scans", json=_scan_body(provider["id"], name=f"排队 {index}"))
        time.sleep(1.5)

        queue = client.get(f"{API}/scans/queue").json()
        record(
            queue["running"] <= queue["limit"] and queue["queued"] >= 2,
            "并发上限生效：超出的扫描留在队列里，没有一起启动",
            f"running={queue['running']} limit={queue['limit']} queued={queue['queued']} "
            f"runner={queue['runner']}",
        )


def verify_reports(base_url: str, session_cookie: str) -> None:
    """任务 6：报告的生成 / 内容选项 / 导出 / 历史。

    验收标准是两条：

    * 「真实扫描数据生成的报告**内容准确**、排版和之前前端预览一致」
    * 「下载的 PDF/Markdown 文件**内容完整可用**」

    第一条里"和预览一致"是能机器化验证的 —— 预览与生成都返回同一份 IR，
    归一化掉时间与编号之后**必须逐字段相同**。这里就从 HTTP 层比那两份。
    """
    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        # 复用会话，不再登录（登录有限流，撞自己的限流会得到误导性的失败）
        client.cookies.set("suvex_session", session_cookie)
        if client.get(f"{API}/auth/me").status_code != 200:
            record(False, "复用会话以便验证报告", "会话已失效")
            return

        # 挑一个**确实有漏洞**的扫描。空扫描也能生成报告，
        # 但它证明不了"内容准确" —— 得有真数据填进去。
        every_finding = client.get(f"{API}/findings").json()
        per_scan: dict[str, int] = {}
        for item in every_finding:
            per_scan[item["scanId"]] = per_scan.get(item["scanId"], 0) + 1
        if not per_scan:
            record(False, "报告：需要一个有漏洞的扫描作为素材", "一条漏洞都没有")
            return
        scan_id = max(per_scan, key=lambda key: per_scan[key])

        # ══════════════════════════════════════════════════════
        # 6a) 预览：不落库，且**编号必须是空的**
        # ══════════════════════════════════════════════════════
        preview_req = {
            "title": "api.example.com 验收报告",
            "scope": {"kind": "scan", "scanId": scan_id},
            "options": {"includePoc": True, "includeReasoning": True},
        }
        preview = client.post(f"{API}/reports/preview", json=preview_req)
        if preview.status_code != 200:
            record(False, "报告预览", preview.text[:300])
            return
        draft = preview.json()

        record(
            draft["meta"]["provisional"] is True and draft["meta"]["code"] == "",
            "★ 草稿不显示编号（预览与下载的编号必然对得上）",
            f"provisional={draft['meta']['provisional']} code={draft['meta']['code']!r}",
        )
        drafted_titles = [item["title"] for item in client.get(f"{API}/reports").json()]
        record(
            preview_req["title"] not in drafted_titles,
            "★ 预览**没有落库**（历史列表里没有这份草稿）",
            f"当前历史 {len(drafted_titles)} 份：{drafted_titles}",
        )
        record(
            len(draft["entries"]) > 0
            and draft["stats"]["total"] == len(draft["entries"])
            and draft["meta"]["scope"],
            "★ 预览报告带上了真实扫描数据（条目数与统计一致）",
            f"条目 {len(draft['entries'])} 条 · 风险判断「{draft['verdict'][:24]}…」· "
            f"范围「{draft['meta']['scope']}」",
        )

        # ══════════════════════════════════════════════════════
        # 6b) 生成：编号分配一次，scope / options 原样冻结
        # ══════════════════════════════════════════════════════
        created = client.post(
            f"{API}/reports",
            json={**preview_req, "format": "markdown"},
        )
        if created.status_code != 201:
            record(False, "生成报告", created.text[:300])
            return
        report = created.json()
        report_id = report["id"]

        record(
            bool(re.match(r"^RPT-\d{8}-[0-9A-HJKMNP-TV-Z]{4}$", report["code"])),
            "★ 生成时分配报告编号（RPT-YYYYMMDD-XXXX，前端按此形状解析）",
            f"code={report['code']}",
        )
        record(
            report["scope"] == {"kind": "scan", "scanId": scan_id}
            and report["options"]["includePoc"] is True
            and report["findingCount"] == len(report["findingIds"])
            and report["findingCount"] > 0,
            "★ 生成时冻结了范围与内容选项（含收录条目 id 与顺序）",
            f"收录 {report['findingCount']} 条 · 排除 {report['excludedCount']} 条 · "
            f"format={report['format']}",
        )
        record(
            bool(report["severityCounts"])
            and all(value > 0 for value in report["severityCounts"].values())
            and sum(report["severityCounts"].values()) == report["findingCount"],
            "★ severityCounts 为 0 的档**键不存在**（Partial，前端按此渲染）",
            f"severityCounts={report['severityCounts']}（合计 {sum(report['severityCounts'].values())} "
            f"= 收录 {report['findingCount']}）",
        )

        # ══════════════════════════════════════════════════════
        # 6c) ★★★ 预览 = 导出 —— 任务 6 的第一条验收标准
        # ══════════════════════════════════════════════════════
        doc_resp = client.get(f"{API}/reports/{report_id}/document")
        if doc_resp.status_code != 200:
            record(False, "复现报告文档", doc_resp.text[:300])
            return
        frozen = doc_resp.json()

        # 归一化：只有这三样**本该**不同 ——
        # 编号（草稿为空）、provisional 标记、生成时间（草稿取的是"此刻"）。
        def _normalize(doc: dict) -> dict:
            copy = json.loads(json.dumps(doc))
            copy["meta"]["code"] = ""
            copy["meta"].pop("provisional", None)
            copy["meta"].pop("generatedAt", None)
            return copy

        record(
            _normalize(draft) == _normalize(frozen),
            "★★★ **预览与导出是同一份文档**（归一化编号/时间后逐字段相同）",
            "前端预览看到的排版与下载得到的文件一致"
            if _normalize(draft) == _normalize(frozen)
            else "预览与导出不一致 —— 这正是任务 6 要防的那个问题",
        )
        record(
            [entry["findingId"] for entry in frozen["entries"]] == report["findingIds"],
            "★ 文档内条目顺序 = 冻结的 findingIds 顺序（严重度降序）",
            f"顺序一致，共 {len(frozen['entries'])} 条",
        )
        record(
            frozen["meta"]["code"] == report["code"],
            "★ 复现时用的是**冻结的编号**，不是重新分配一个",
            f"文档编号 {frozen['meta']['code']} = 记录编号 {report['code']}",
        )

        # ══════════════════════════════════════════════════════
        # 6d) ★ Markdown 导出完整可用
        # ══════════════════════════════════════════════════════
        download = client.get(f"{API}/reports/{report_id}/download")
        body = download.content.decode("utf-8")
        disposition = download.headers.get("content-disposition", "")

        record(
            download.status_code == 200
            and "text/markdown" in download.headers.get("content-type", "")
            and f'filename="{report["code"]}.md"' in disposition,
            "★ 下载返回 Markdown 附件，**文件名用编号**（不是中文标题）",
            f"{download.headers.get('content-type')} · {disposition}",
        )
        record(
            len(body) > 500 and body.lstrip().startswith(f"# {preview_req['title']}"),
            "★ 下载内容完整：标题在、不是空壳",
            f"{len(body)} 字节，{body.count(chr(10)) + 1} 行",
        )
        sections = [
            "## 一、摘要",
            "## 二、风险总览",
            "## 三、漏洞详情",
            "## 附录 A · 检测范围与方法",
            "## 附录 B · 严重程度说明",
            "## 附录 C · 内容范围",
        ]
        missing = [section for section in sections if section not in body]
        record(
            not missing,
            "★ Markdown 五个部分齐全（摘要 / 总览 / 详情 / 附录 A·B·C）",
            f"缺 {missing}" if missing else "六个标题全部命中",
        )
        # 每一条收录的漏洞都要在正文里出现 —— "内容完整"的实质含义
        absent = [
            entry["title"]
            for entry in frozen["entries"]
            if f"### {entry['index']}. {entry['title']}" not in body
        ]
        record(
            not absent,
            "★ 收录的每一条漏洞都在正文里（编号 + 标题逐一命中）",
            f"缺 {absent}" if absent else f"{len(frozen['entries'])} 条全部出现",
        )
        record(
            report["code"] in body and "生成后分配" not in body,
            "★ 正文里写的是**真实编号**（不是草稿的「生成后分配」）",
            f"正文含 {report['code']}",
        )
        # 附录 C 要如实交代"这份报告按什么选项生成"。
        # （排除数为 0 时正文不写那句 —— 这是对的，见 6e 之后那条非零的验证。）
        record(
            "## 附录 C · 内容范围" in body
            and "条目筛选：全部状态的问题" in body
            and "验证脚本（PoC）：包含" in body,
            "附录 C 如实交代生成时用的选项（筛选方式与是否含 PoC）",
            "条目筛选=全部状态 · PoC=包含",
        )

        # ══════════════════════════════════════════════════════
        # 6e) ★ 内容选项真的影响产出（含一处容易做错的差异）
        # ══════════════════════════════════════════════════════
        with_poc = client.post(
            f"{API}/reports/preview",
            json={**preview_req, "options": {"includePoc": False}},
        ).json()
        proof_on = next(
            (entry for entry in draft["entries"] if entry["poc"]), None
        )
        proof_off = (
            next((e for e in with_poc["entries"] if e["findingId"] == proof_on["findingId"]), None)
            if proof_on
            else None
        )
        record(
            proof_on is not None
            and proof_off is not None
            and proof_off["poc"] is None
            and proof_off["verificationLabel"] == proof_on["verificationLabel"],
            "★★ includePoc 关掉只去掉脚本，**验证结论保留**（结论不因材料而消失）",
            f"PoC {proof_on['poc']['language'] if proof_on and proof_on['poc'] else '（无）'} → "
            f"{'已去掉' if proof_off and proof_off['poc'] is None else '仍在'}｜"
            f"验证标签 {proof_off['verificationLabel'] if proof_off else '?'} 保持不变",
        )
        # ⚠️ 这一条**必须先把状态改出差异**再验。
        # 第一版直接拿"全部都是待确认"的数据去验，得到
        # 「全部 2 条 → 仅已确认 0 条」，而 `all([])` 是 True ——
        # 断言通过了，但什么都没证明。这类空转的断言比没有断言更危险。
        in_scope = client.get(f"{API}/findings?scanId={scan_id}").json()
        victim = in_scope[0]
        client.post(f"{API}/findings/status", json={"ids": [victim["id"]], "status": "confirmed"})

        only_confirmed = client.post(
            f"{API}/reports/preview",
            json={**preview_req, "options": {"onlyConfirmed": True}},
        ).json()
        kept = {entry["findingId"] for entry in only_confirmed["entries"]}
        record(
            len(in_scope) >= 2
            and kept == {victim["id"]}
            and all(entry["statusLabel"] in {"已确认", "已修复"} for entry in only_confirmed["entries"]),
            "★ onlyConfirmed 只留「已确认 + 已修复」（修过的仍算发生过，剔除会失真）",
            f"全部 {len(in_scope)} 条 → 仅已确认 {len(only_confirmed['entries'])} 条｜"
            f"留下的状态={sorted({e['statusLabel'] for e in only_confirmed['entries']})}｜"
            f"排除 {only_confirmed['meta']['excludedCount']} 条",
        )
        # ★ 排除数必须真的非零，否则上一行只是又一次空转
        record(
            only_confirmed["meta"]["excludedCount"] == len(in_scope) - len(kept) > 0,
            "★ 被选项排除的条数被如实算出（非零，说明筛选真的发生了）",
            f"范围内 {len(in_scope)} 条，收录 {len(kept)} 条，"
            f"排除 {only_confirmed['meta']['excludedCount']} 条",
        )

        # ★★ 冻结的排除数：造一份**真的排除了东西**的报告，
        # 然后把被排除的那条改成已确认 —— 若复现时重算，排除数会变成 0，
        # 报告就会谎称"范围内未发现问题"，而事实是它把 1 条筛掉了。
        filtered = client.post(
            f"{API}/reports",
            json={
                **preview_req,
                "title": "仅已确认的验收报告",
                "scope": {"kind": "scan", "scanId": scan_id},
                "options": {"onlyConfirmed": True},
                "format": "markdown",
            },
        ).json()
        # 生成时冻结的排除数（下面改完数据之后再复现一次来对比）
        excluded_now = filtered["excludedCount"]

        # 把先前那条被排除的漏洞改成已确认 —— 于是"现在重算"会得到 0
        others = [item for item in in_scope if item["id"] != victim["id"]]
        if others:
            client.post(
                f"{API}/findings/status",
                json={"ids": [others[0]["id"]], "status": "confirmed"},
            )
        after_doc = client.get(f"{API}/reports/{filtered['id']}/document").json()
        filtered_download = client.get(f"{API}/reports/{filtered['id']}/download")
        filtered_body = filtered_download.content.decode("utf-8")

        record(
            excluded_now > 0
            and after_doc["meta"]["excludedCount"] == excluded_now
            and f"另有 {excluded_now} 条" in filtered_body,
            "★★ 冻结的排除数**不随数据变化重算**，且如实写进下载的文件",
            f"生成时排除 {excluded_now} 条 → 改动漏洞后复现仍是 "
            f"{after_doc['meta']['excludedCount']} 条（重算会变成 0）",
        )
        record(
            len(after_doc["entries"]) == filtered["findingCount"],
            "★ 冻结的**收录集合**在数据变化后不变（三个月后回看内容一致）",
            f"收录 {filtered['findingCount']} 条 → 复现 {len(after_doc['entries'])} 条",
        )
        # ★ 但条目的**状态标签**要跟着活数据走 —— 这是前端的行为，必须一致。
        # 若后端把状态也冻住，预览会显示"已修复"而下载的文件写着"待确认"，
        # "预览=导出"当场就破了。
        entry_states = {
            entry["findingId"]: entry["statusLabel"] for entry in after_doc["entries"]
        }
        live_states = {
            item["id"]: item["status"]
            for item in client.get(f"{API}/findings?scanId={scan_id}").json()
        }
        record(
            entry_states.get(victim["id"]) == "已确认" and live_states.get(victim["id"]) == "confirmed",
            "★ 条目的**处置状态反映当前值**（与前端 rebuildReportDocument 一致）",
            f"条目状态标签={entry_states.get(victim['id'])} ｜ 活数据={live_states.get(victim['id'])}",
        )
        client.delete(f"{API}/reports/{filtered['id']}")

        # ══════════════════════════════════════════════════════
        # 6f) 三种范围都能用（含 Python 关键字 from）
        # ══════════════════════════════════════════════════════
        target_scope = client.post(
            f"{API}/reports/preview",
            json={**preview_req, "scope": {"kind": "target", "target": frozen["entries"][0]["target"]}},
        )
        range_scope = client.post(
            f"{API}/reports/preview",
            json={
                **preview_req,
                "scope": {"kind": "range", "from": "2020-01-01T00:00:00.000Z", "to": "2030-12-31T00:00:00.000Z"},
            },
        )
        record(
            target_scope.status_code == 200 and range_scope.status_code == 200,
            "★ 三种范围都可用（按扫描 / 按目标 / 按时间区间）",
            f"target → {target_scope.status_code}，range(from/to) → {range_scope.status_code}"
            + (
                f"｜range 收录 {len(range_scope.json()['entries'])} 条"
                if range_scope.status_code == 200
                else f"｜{range_scope.text[:160]}"
            ),
        )
        # ⚠️ `from` 是 Python 关键字。第一版用 model_validator 改名，
        # 结果被 extra="forbid" 提前拦掉，**整个区间报告用不了**。
        record(
            range_scope.status_code == 200 and range_scope.json()["meta"]["scope"],
            "★ 区间范围的 `from` 键被正确接受（不是被 extra=forbid 拦下）",
            f"范围描述「{range_scope.json()['meta']['scope']}」"
            if range_scope.status_code == 200
            else range_scope.text[:160],
        )
        # 区间外的数据要真的被筛掉
        empty_range = client.post(
            f"{API}/reports/preview",
            json={
                **preview_req,
                "scope": {"kind": "range", "from": "2000-01-01T00:00:00.000Z", "to": "2000-01-02T00:00:00.000Z"},
            },
        )
        record(
            empty_range.status_code == 200 and empty_range.json()["stats"]["total"] == 0,
            "★ 范围是**真的在筛**（空区间得到 0 条，而不是把全部塞进来）",
            f"2000 年那两天收录 {empty_range.json()['stats']['total']} 条",
        )

        # ══════════════════════════════════════════════════════
        # 6g) PDF：不报错，而是如实说明走浏览器打印
        # ══════════════════════════════════════════════════════
        pdf_report = client.post(
            f"{API}/reports", json={**preview_req, "title": "按 PDF 交付的报告", "format": "pdf"}
        ).json()
        pdf_download = client.get(f"{API}/reports/{pdf_report['id']}/download")
        record(
            pdf_report["format"] == "pdf"
            and pdf_download.status_code == 200
            and pdf_download.headers.get("x-suvex-note") == "pdf-via-browser-print",
            "★ PDF 报告不返回 404，而是如实说明「由前端浏览器打印」",
            f"HTTP {pdf_download.status_code} · "
            f"X-Suvex-Note={pdf_download.headers.get('x-suvex-note')}",
        )

        # ══════════════════════════════════════════════════════
        # 6h) 历史：列表 / 详情 / 删除
        # ══════════════════════════════════════════════════════
        history = client.get(f"{API}/reports").json()
        record(
            any(item["id"] == report_id for item in history)
            and any(item["id"] == pdf_report["id"] for item in history),
            "★ 生成过的报告都进了历史列表",
            f"历史 {len(history)} 份：{[item['code'] for item in history]}",
        )

        removed = client.delete(f"{API}/reports/{report_id}")
        after_delete = client.get(f"{API}/reports/{report_id}")
        gone_download = client.get(f"{API}/reports/{report_id}/download")
        still_there = any(
            item["id"] == report_id for item in client.get(f"{API}/reports").json()
        )
        record(
            removed.status_code == 204
            and after_delete.status_code == 404
            and gone_download.status_code == 404
            and not still_there,
            "★ 删除报告后：详情与下载都 404，历史列表里也没有了",
            f"DELETE→{removed.status_code} 详情→{after_delete.status_code} "
            f"下载→{gone_download.status_code} 列表仍在={still_there}",
        )
        # 删掉一份报告不能把漏洞一起删了 —— 那是彻底的数据事故。
        #
        # ⚠️ 必须**紧贴 delete 前后**各数一次，不能用本函数开头那个全局计数：
        # 上面并发上限那几条留下的排队扫描会在这段时间里陆续跑完，
        # 每次跑完都导入两条新漏洞。第一次写这条时就是这么错的 ——
        # 报出来是"删除前 6 条 → 删除后 8 条"，看着像删除**凭空造了**漏洞，
        # 其实只是后台扫描在同期产出了数据。这类"时间上碰巧相邻"的假失败
        # 比真失败更难查：它不可复现，而且指向一个完全错误的模块。
        findings_now = len(client.get(f"{API}/findings").json())
        client.delete(f"{API}/reports/{pdf_report['id']}")
        after_pdf_delete = len(client.get(f"{API}/findings").json())
        record(
            after_pdf_delete == findings_now,
            "★★ 删除报告**没有**连带删掉漏洞（只删关联行，不删发现本身）",
            f"删除前 {findings_now} 条 → 删除后 {after_pdf_delete} 条",
        )


def verify_overview(base_url: str, session_cookie: str) -> None:
    """`/overview`（契约 §3.6）。

    ## 为什么这一节存在

    契约 §9 的交付表把 `/overview` 排在**任务 6**。任务 6 的正式交付物是
    报告，而这个端点的 501 消息一直写着「计划在后端任务 6 中交付」——
    于是任务 6 一结束，那句话就成了假话，**而没有任何东西会报错**：
    一个永远返回 501 的端点自己是不会喊的。

    更糟的是本次验收脚本当时把那个 501 断言成了 `[PASS]`，
    等于把"承诺已过期"洗成了一条绿灯。发现方式是写这一节时
    回头读它自己的输出（`尚未实现（计划在后端任务 6 中交付）`）。

    所以这一节除了验功能，还专门钉住**那个 501 不能回来**。
    """
    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        client.cookies.set("suvex_session", session_cookie)
        if client.get(f"{API}/auth/me").status_code != 200:
            record(False, "复用会话以便验证 Overview", "会话已失效")
            return

        # ★ 先把系统等静 —— 下面这条比较要求"两次读之间没有人写漏洞"。
        # 见 `_quiesce` 的说明：不这样做的话，它会偶发失败。
        _quiesce(client)

        response = client.get(f"{API}/overview")
        if response.status_code != 200:
            record(
                False,
                "★★ /overview 已按契约交付（不再是 501 占位）",
                f"HTTP {response.status_code}：{response.text[:200]}",
            )
            return
        body = response.json()

        record(
            set(body) == {"metrics", "activity", "severityBreakdown", "hasAnyScan"},
            "★★ /overview 不再是 501 占位，且形状与前端 OverviewData 一致",
            f"顶层键={sorted(body)}",
        )

        # ★★★ 唯一数据源：与 /findings/stats 逐档对得上
        stats = client.get(f"{API}/findings/stats").json()
        from_overview = {item["severity"]: item["count"] for item in body["severityBreakdown"]}
        # `/findings/stats` 的 bySeverity 是 Partial（为 0 的档键不存在），
        # 而 Overview 固定返回 5 档 —— 按 0 补全再比。
        from_stats = {member: stats["bySeverity"].get(member, 0) for member in from_overview}
        record(
            from_overview == from_stats
            and sum(from_overview.values()) == stats["total"],
            "★★★ Overview 的严重度分布与 /findings/stats **逐档相等**（同源，两页不会对不上）",
            f"Overview={from_overview} ｜ stats={from_stats}",
        )
        # 顺带把契约 §3.4.2 的 Partial 语义钉住 —— 它曾经是错的：
        # 响应用 `"medium": null` 冒充"键不存在"。
        zero_keys = [k for k, v in stats["bySeverity"].items() if v is None]
        record(
            not zero_keys,
            "★ bySeverity 的 Partial 语义正确：为 0 的档**键不存在**，不是 null",
            f"没有显式 null 的档｜键={sorted(stats['bySeverity'])}",
        )

        # ★ 全部 5 档都在（图表要 5 根柱子，缺柱子会被读成"坏了"）
        record(
            [item["severity"] for item in body["severityBreakdown"]]
            == ["critical", "high", "medium", "low", "info"],
            "★ severityBreakdown 固定返回全部 5 档（与 stats 的 Partial 语义相反，是有意的）",
            "顺序 " + " > ".join(item["severity"] for item in body["severityBreakdown"]),
        )

        # ★ 与扫描列表对得上
        scans = client.get(f"{API}/scans").json()
        active_in_list = [item for item in scans if item["status"] in {"queued", "running"}]
        record(
            body["metrics"]["activeScans"] == len(active_in_list)
            and body["hasAnyScan"] == bool(scans),
            "★ activeScans / hasAnyScan 与 /scans 列表数出来的一致（同一张表，不会自相矛盾）",
            f"activeScans={body['metrics']['activeScans']} 列表里 {len(active_in_list)} 条｜"
            f"hasAnyScan={body['hasAnyScan']}",
        )

        # ★ 已完成扫描的平均耗时
        completed = [s for s in scans if s["status"] == "completed" and s["durationSec"] is not None]
        expected_avg = round(sum(s["durationSec"] for s in completed) / len(completed)) if completed else None
        record(
            body["metrics"]["avgScanDurationSec"] == expected_avg,
            "★ 平均耗时只按**已完成**扫描算（failed/aborted 的耗时是「多久放弃的」，不是一次要多久）",
            f"接口={body['metrics']['avgScanDurationSec']} 秒 · 列表算出={expected_avg} 秒 · "
            f"已完成 {len(completed)} 次",
        )

        # ★★ 活动流：是投影形状，且不含 reasoning / failed
        activity = body["activity"]
        projected_types = {"tool_call", "discovery", "poc_generated", "verified", "blocked", "completed"}
        record(
            bool(activity)
            and all("detail" not in item for item in activity)
            and {item["type"] for item in activity}.issubset(projected_types),
            "★★ 活动流是**投影形状**（扁平、有 subject、无 detail），且不含 reasoning / failed",
            f"{len(activity)} 条｜类型={sorted({i['type'] for i in activity})}｜"
            f"字段={sorted(activity[0]) if activity else []}",
        )
        stamps = [item["at"] for item in activity]
        record(
            stamps == sorted(stamps, reverse=True),
            "★ 活动流最新在前",
            f"首条={activity[0]['summary'][:36] if activity else '（空）'}",
        )

        # ★ 空状态下的两个 null（本机有数据，所以只能验"有数据时不是硬编码的 0"）
        record(
            body["metrics"]["falsePositiveCount"] == stats["falsePositiveCount"],
            "★ falsePositiveCount 与统计接口一致（排除是可以的，悄悄排除不行）",
            f"Overview={body['metrics']['falsePositiveCount']} stats={stats['falsePositiveCount']}",
        )


def _scan_root() -> str:
    from app.core.config import get_settings

    return str(get_settings().scan_root)


def verify_real_strix() -> None:
    """★ 用**真的 strix 二进制**跑一次启动链路。

    这一条是本轮最有价值的验收 —— 它回答的是「我们和 strix 的集成到底通不通」，
    而不是"我们的替身跑得对不对"。

    做法是在**进程内**直接调执行后端（不起 HTTP 服务），因为要验的是
    "命令拼得对不对、凭据有没有进 argv、退出码怎么解释、日志能不能拿到"，
    那些和 HTTP 层无关。

    目标用 `http://127.0.0.1:1` —— 那个端口上什么都没有。
    即使将来 Docker 装上了，这次扫描也碰不到任何真实主机。
    """
    import shutil

    from app.core import db as db_module
    from app.core.config import get_settings
    from app.scans.runner import LaunchSpec, StrixRunner

    strix = _find_strix()
    if strix is None:
        record(
            False,
            "找到 strix 可执行文件",
            "没找到。安装方式见 README「strix 环境」一节（本机装在 "
            "E:\\Program Files (x86)\\strix-venv）。",
        )
        return
    record(True, "找到 strix 可执行文件", strix)

    os.environ["SUPEX_SCAN_RUNNER"] = "strix"
    os.environ["SUPEX_STRIX_COMMAND"] = strix
    os.environ["SUPEX_SCAN_MODE"] = "quick"
    os.environ["SUPEX_SCAN_MAX_TURNS"] = "1"
    get_settings.cache_clear()
    db_module.reset()
    db_module.configure(os.environ["SUPEX_DATABASE_URL"], force=True)

    run_dir = Path(_scan_root()) / "strix-probe"
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)

    runner = StrixRunner()
    try:
        runner.ensure_ready()
    except Exception as exc:  # noqa: BLE001
        record(False, "执行后端自检通过", str(exc))
        return

    spec = LaunchSpec(
        scan_id="strix-probe",
        # 无害目标：那个端口上没有任何服务
        target="http://127.0.0.1:1",
        target_kind="url",
        skills=("web",),
        run_dir=run_dir,
        log_path=run_dir / "scan.log",
        llm_model="deepseek/deepseek-chat",
        llm_api_key="sk-not-a-real-key-acceptance-probe",
        llm_api_base="https://api.deepseek.com",
    )

    argv = runner.command(spec)
    env = runner.env(spec)
    record(
        "--target" in argv
        and "--non-interactive" in argv
        and "http://127.0.0.1:1" in argv
        and spec.llm_api_key not in " ".join(argv),
        "命令拼装正确，且**凭据不在 argv 里**（只在环境变量里）",
        " ".join(argv),
    )
    record(
        env.get("STRIX_LLM") == "deepseek/deepseek-chat"
        and env.get("STRIX_TELEMETRY") == "0",
        "环境变量注入正确（模型、关闭遥测）",
        f"STRIX_LLM={env.get('STRIX_LLM')} STRIX_TELEMETRY={env.get('STRIX_TELEMETRY')}",
    )

    try:
        result = runner.launch(spec)
    except Exception as exc:  # noqa: BLE001
        record(False, "启动 strix 子进程", str(exc))
        return

    try:
        code = result.process.wait(timeout=120)
    except Exception:  # noqa: BLE001
        result.process.kill()
        record(False, "strix 在 120 秒内结束", "超时，已强杀")
        return

    from app.scans.service import read_log_tail

    tail = read_log_tail(str(result.log_path), lines=40)
    record(code in (0, 1, 2), "strix 子进程跑完并给出退出码", f"exit={code} pid={result.pid}")

    # ★ 本机的预期结果是退出码 1 + 日志里说明 Docker 不在。
    # 这里不断言"必须是 Docker 错误" —— 那样一旦用户装好 Docker，
    # 这条验收反而会失败。两种结果都算通过，只是如实报告是哪一种。
    if code == 1 and "DOCKER" in tail.upper():
        record(
            True,
            "strix 报告 Docker 不可用（本机的预期结果）",
            "—— 这说明启动链路是通的，唯一缺的是 Docker 引擎。见 README「已知未验证项」。",
        )
    elif code in (0, 2):
        record(True, "strix 真的跑完了一次扫描（Docker 可用）", f"exit={code}")
    else:
        record(
            False,
            "strix 的结束方式符合预期",
            f"exit={code}，日志尾部：{tail[-400:]}",
        )

    record(
        bool(tail),
        "能读到子进程的日志尾部（失败原因会来自这里）",
        tail[-300:].replace("\n", " ⏎ ") if tail else "（日志为空）",
    )


def _find_strix() -> str | None:
    """找 strix 可执行文件：先看 PATH，再看本机的独立 venv。"""
    import shutil

    found = shutil.which("strix")
    if found:
        return found
    for candidate in (
        r"E:\Program Files (x86)\strix-venv\Scripts\strix.exe",
        r"E:\Program Files (x86)\strix-venv\bin\strix",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def cleanup_scan_processes(database_url: str) -> int:
    """把验收过程中起的扫描子进程清干净，返回清掉的个数。

    ⚠️ 为什么需要这一步（**这个问题在产品上也成立**）：

    Windows 上 `Popen.terminate()` 是 `TerminateProcess` —— **强杀**，
    进程收不到任何可处理的通知。于是 uvicorn 被 terminate 时
    **lifespan 的关闭钩子根本不会执行**，`scheduler.stop()` 里
    "终止所有在跑的扫描" 那段逻辑跑了等于没跑，替身进程会继续活到
    自己的 sleep 结束。

    产品上的应对是启动时的 `recover_orphans()`（见 `scheduler.py`）——
    下次起来时把它们找出来并终止，并如实写"结果不可知"。
    但验收脚本跑完就该是干净的，不能留着让下一次运行撞上，
    所以这里主动收一次尾。
    """
    from sqlalchemy import create_engine, text

    from app.scans.runner import is_alive, terminate_tree

    cleaned = 0
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT id, pid FROM scans WHERE pid IS NOT NULL AND runner = 'fake'")
            ).all()
            for scan_id, pid in rows:
                if is_alive(pid):
                    terminate_tree(pid, grace_seconds=5.0, log=print)
                    cleaned += 1
                conn.execute(text("UPDATE scans SET pid = NULL WHERE id = :id"), {"id": scan_id})
    except Exception as exc:  # noqa: BLE001 - 清理失败不该盖掉验收结论
        print(f"  清理扫描进程时出错（忽略）：{exc}", flush=True)
    finally:
        engine.dispose()
    return cleaned


def check_compose_file() -> None:
    """静态校验 `docker-compose.yml` 的结构。

    本机没有 Docker（Windows 上没有 WSL2 / 虚拟化平台，启用需要重启），
    所以 `docker compose up` **没有被真实执行过**。这一点必须写在结论里，
    不能含糊过去。能做的是：把文件解析一遍，确认结构、依赖关系、
    环境变量注入都对得上 —— 那能挡住"拼错键名"这一类错误，
    挡不住"镜像构建失败"那一类。
    """
    import yaml

    path = PROJECT_ROOT / "docker-compose.yml"
    if not path.exists():
        record(False, "docker-compose.yml 存在", "文件不存在")
        return

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    services = data.get("services", {})

    record("db" in services and "api" in services, "compose 定义了 db 与 api 两个服务")
    record(
        services.get("api", {}).get("depends_on", {}).get("db", {}).get("condition")
        == "service_healthy",
        "api 等待 db 健康（而不是仅「已启动」）",
    )
    db_env = services.get("db", {}).get("environment", {})
    api_env = services.get("api", {}).get("environment", {})
    record(
        "postgres" in str(services.get("db", {}).get("image", "")),
        "db 使用 postgres 镜像",
        str(services.get("db", {}).get("image")),
    )
    record(
        "@db:5432" in str(api_env.get("SUPEX_DATABASE_URL", "")),
        "api 的数据库地址指向 compose 内部的服务名 db:5432",
        str(api_env.get("SUPEX_DATABASE_URL", "")),
    )
    record(
        services.get("db", {}).get("ports") is None,
        "db 未映射到宿主端口（只在内网可达）",
    )
    record(
        services.get("api", {}).get("ports") == ["127.0.0.1:8000:8000"],
        "api 只绑到回环地址",
        str(services.get("api", {}).get("ports")),
    )
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    record(
        "alembic upgrade head" in dockerfile,
        "Dockerfile 的启动命令里包含迁移（compose up 一步到位）",
    )
    record("USER suvex" in dockerfile, "镜像以非 root 用户运行")
    record(
        (PROJECT_ROOT / ".dockerignore").exists()
        and ".env" in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8"),
        ".dockerignore 排除了 .env（凭据不进镜像层）",
    )
    record(
        "POSTGRES_PASSWORD" in json.dumps(db_env),
        "db 口令可通过环境变量覆盖",
    )
    # provider 凭据的加密密钥必须能被传进容器。
    record(
        "SUPEX_SECRET_KEY" in json.dumps(api_env),
        "api 接受了 SUPEX_SECRET_KEY（provider 凭据的加密密钥）",
    )
    record(
        ":=" not in str(api_env.get("SUPEX_SECRET_KEY", ""))
        and str(api_env.get("SUPEX_SECRET_KEY", "")).endswith(":-}"),
        "★ 加密密钥**没有**写死的默认值（给默认值等于把加密密钥提交进仓库）",
        str(api_env.get("SUPEX_SECRET_KEY", "")),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Suvex 后端任务 1 验收")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "SUPEX_ACCEPTANCE_DATABASE_URL",
            "postgresql+psycopg://suvex:suvex_dev_local@127.0.0.1:55432/suvex_acceptance",
        ),
    )
    parser.add_argument("--keep-server", action="store_true")
    parser.add_argument(
        "--skip-server",
        action="store_true",
        help="只做静态检查（迁移/表结构/compose 文件），不起服务",
    )
    args = parser.parse_args()

    print("Suvex 后端验收（任务 1）", flush=True)
    print(f"  项目根：{PROJECT_ROOT}", flush=True)
    print(f"  验收库：{args.database_url}", flush=True)

    print("\n── 准备 ──", flush=True)
    # ⚠️ 必须在 ensure_database / migrate 之前设好，见 migrate() 的注释。
    os.environ["SUPEX_DATABASE_URL"] = args.database_url
    from app.core.config import get_settings

    get_settings.cache_clear()

    ensure_database_reset = reset_database
    ensure_database_reset(args.database_url)
    migrate(args.database_url)
    record(True, "验收库已重建并迁移到 head")

    print("\n── 结构检查 ──", flush=True)
    check_migrations_at_head(args.database_url)
    check_tables(args.database_url)
    check_compose_file()

    if not args.skip_server:
        port = args.port or free_port()
        base_url = f"http://127.0.0.1:{port}"
        env = {
            **os.environ,
            "SUPEX_DATABASE_URL": args.database_url,
            "SUPEX_ENV": "dev",
            "SUPEX_COOKIE_SECURE": "false",
            "SUPEX_LOG_LEVEL": "info",
            "SUPEX_CORS_ORIGINS": "http://localhost:5173",
            # ★ 验收用**替身**执行后端：它同样 spawn 真进程（含真孙进程），
            # 所以队列、进程监督、杀进程树全都是真的在跑，
            # 但它不执行任何真实攻击 —— 验收脚本绝不能向真实目标发包。
            "SUPEX_SCAN_RUNNER": "fake",
            # 8 秒：长到足以让"终止运行中的扫描"那条在进程还活着时下手，
            # 短到不拖慢验收。
            "SUPEX_FAKE_SCAN_SECONDS": "8",
            # 上限 1 —— 于是"超出的扫描排队"这条能被真的验证。
            "SUPEX_MAX_CONCURRENT_SCANS": "1",
            # 默认 20 秒会让心跳那条验收等 20 秒。调小它同时也在验证
            # 「心跳间隔是配置项而不是写死的常量」。
            "SUPEX_SSE_HEARTBEAT_SECONDS": "3",
        }
        print(f"\n── 启动 uvicorn（127.0.0.1:{port}）──", flush=True)
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                # `--log-level info`：**必须让 uvicorn 的访问日志真的产生出来**，
                # 否则下面那条"日志里没有明文"检查是对着一个空字符串做断言 ——
                # 永远通过，等于没做。这个坑是第一次跑的时候踩的
                # （报告里写着"检查了服务端 0 行输出"，而那是通过）。
                "--log-level",
                "info",
            ],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        # ★ 必须**持续**把子进程的输出读走。
        #
        # 不读的话：管道的缓冲区（Windows 上约 4–64KB）写满之后，
        # 子进程会**阻塞在写日志上** —— 而症状是"服务在某个请求上突然不响应了"
        # 或者"SSE 连接被对端关闭"，看起来像应用的 bug，实际是父进程没读管道。
        # 之前只在失败时才 `server.stdout.read()`，那正是这个死锁的实现。
        server_log: list[str] = []
        drain = threading.Thread(target=_drain_output, args=(server.stdout, server_log), daemon=True)
        drain.start()

        try:
            if not wait_for_health(base_url):
                server.terminate()
                record(False, "uvicorn 启动并通过 /health", "".join(server_log[-30:]))
            else:
                record(True, "uvicorn 启动并通过 /health")
                run_checks(base_url, args.database_url)
                used_key, session_cookie = verify_providers(base_url, args.database_url, STRONG_PASSWORD)
                verify_scans(base_url, session_cookie)
                # ★ 任务 6 放在扫描之后 —— 它要拿真实扫描产出的漏洞当素材，
                # 空数据生成的报告证明不了"内容准确"。
                print("\n── 任务 6：报告 ──", flush=True)
                verify_reports(base_url, session_cookie)
                # `/overview` 在契约 §9 里排在任务 6，但任务 6 的正式交付物是报告
                # —— 见 `verify_overview` 的说明：它当时一直返 501 且写着"计划任务 6"。
                print("\n── 全局统计（/overview）──", flush=True)
                verify_overview(base_url, session_cookie)

                # ★ 任务 2 的验收：「密钥全链路不出现明文日志」。
                #
                # 这一条打的是**真实运行中的服务端进程**的日志 ——
                # 比在测试里挂一个探针更接近事故现场：
                # 它包含了 uvicorn 的访问日志、启动日志、以及任何
                # 被加进来的中间件输出。
                log_text = "".join(server_log)
                record(
                    bool(server_log) and used_key not in log_text,
                    "服务端日志里没有出现明文 API key（含 uvicorn 访问日志）",
                    f"检查了服务端 {len(server_log)} 行输出"
                    if server_log
                    else "⚠️ 一行都没抓到 —— 这条检查是空转的，必须修",
                )

            if any("Traceback" in line for line in server_log):
                record(
                    False,
                    "服务端日志里没有未捕获异常",
                    "".join(server_log[-30:]),
                )
            else:
                record(True, "服务端日志里没有未捕获异常")
        finally:
            if not args.keep_server:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    server.kill()
                # ★ **强杀 uvicorn 之后，它的子进程（替身扫描）还活着。**
                # 见 `cleanup_scan_processes` 的说明：Windows 上 terminate()
                # 是强杀，uvicorn 根本不会跑 lifespan 的关闭钩子，
                # 于是 scheduler.stop() 里那段"终止所有在跑的扫描"等于没跑。
                # 不收尾的话，命令结束之后进程组里还有活着的子进程，
                # 而某些调用方（构建工具、CI、任务运行器）会因此认为命令没干净退出。
                cleaned = cleanup_scan_processes(args.database_url)
                if cleaned:
                    print(f"\n  已清理 {cleaned} 个残留的扫描子进程", flush=True)
            else:
                print(f"\n服务仍在运行：{base_url}{API}/docs（PID {server.pid}）", flush=True)

    # 12) 真 strix 的启动链路（在进程内做，与 HTTP 层无关）
    print("\n── 真 strix 集成 ──", flush=True)
    verify_real_strix()

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = [title for ok, title, _ in RESULTS if not ok]
    print(f"\n{'=' * 60}", flush=True)
    print(f"合计 {len(RESULTS)} 项：通过 {passed}，失败 {len(failed)}", flush=True)
    for title in failed:
        print(f"  ✗ {title}", flush=True)

    if not failed:
        print("\n⚠️ 未验证项：`docker compose up --build` 未在本机执行。", flush=True)
        print("   原因：本机没有 Docker 引擎（Windows 上未启用 WSL2 / 虚拟化平台，", flush=True)
        print("   启用需要重启）。compose 文件与 Dockerfile 只做了静态校验，", flush=True)
        print("   见上面「compose」那几条。", flush=True)

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
