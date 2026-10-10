"""测试夹具。

## 为什么对**真实 PostgreSQL** 跑，而不是 SQLite

本项目用了大量 PostgreSQL 专有行为：`JSONB`、`BIGSERIAL`、
`CheckConstraint` 里的布尔等价式、部分唯一索引
（`uq_provider_accounts_default` 的 `postgresql_where`）。

SQLite 上这些要么没有、要么语义不同 —— 用 SQLite 跑测试会得到一套
**在真实环境里不成立**的绿灯，比没有测试更坏，因为它给了虚假的把握。

代价是测试需要一个可连的 PostgreSQL。本地开发用
`README.md` 里的本机实例（默认 `127.0.0.1:55432`），CI 里用 service container。
连不上时相关测试会**明确失败**而不是静默跳过 —— 静默跳过的鉴权测试
等于没有这个测试。

## 数据库怎么准备

1. 建库（若不存在）—— 连 `postgres` 维护库做 `CREATE DATABASE`，要 AUTOCOMMIT。
2. **跑 alembic 迁移**，不是 `create_all()`。
   用 `create_all` 的话，迁移文件本身就是唯一没被测到的东西 ——
   而"迁移能不能在空库上跑通"正是要验收的那一条。

## 每个用例之间怎么隔离

`TRUNCATE ... RESTART IDENTITY CASCADE` 所有表。
比"每个用例一个事务再回滚"慢，但**它测的是真实提交后的状态** ——
回滚式隔离会掩盖「忘了 commit」这类 bug，而那是真实存在的失败模式。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ⚠️ 必须在导入任何 app 模块**之前**设置。
# `get_settings()` 是 lru_cache 的单例，一旦被别处调用过就固定了 ——
# 那之后改环境变量不会生效，而症状是"测试跑在了开发库上"。
TEST_DATABASE_URL = os.environ.setdefault(
    "SUPEX_TEST_DATABASE_URL",
    "postgresql+psycopg://suvex:suvex_dev_local@127.0.0.1:55432/suvex_test",
)
os.environ["SUPEX_DATABASE_URL"] = TEST_DATABASE_URL
# 开发态默认值，但显式写出来：测试不该受开发机 .env 的影响。
os.environ.setdefault("SUPEX_ENV", "dev")
os.environ.setdefault("SUPEX_COOKIE_SECURE", "false")

# ══════════════════════════════════════════════════════════════════
# ★ 安全网：测试**绝不能**跑真实渗透测试
# ══════════════════════════════════════════════════════════════════
#
# `SUPEX_SCAN_RUNNER=fake` 在**导入任何 app 模块之前**设死。
# 少了这一行，任何一个碰巧创建了扫描的测试都会真的拉起 strix ——
# 而 strix 会向目标发包。那不是"测试慢一点"，那是违法的行为。
#
# 它也防住了"某人以后把默认值改回 strix 然后跑测试"这条路径：
# 这里用的是 setdefault，所以外部显式设成 strix 仍然生效 ——
# 但那时是**有人刻意要跑真扫描**，而不是不小心。
os.environ.setdefault("SUPEX_SCAN_RUNNER", "fake")
os.environ.setdefault("SUPEX_FAKE_SCAN_SECONDS", "1.0")
os.environ.setdefault("SUPEX_FAKE_SCAN_EXIT_CODE", "0")
os.environ.setdefault("SUPEX_FAKE_SCAN_GRANDCHILD", "true")

# ★ 调度轮询间隔拉到一小时，让后台线程**在一轮之后就睡死**。
#
# 这样做的效果是：调度器只在两处真的跑起来 ——
#   · TestClient 启动时的那一次（那时队列是空的）；
#   · `POST /scans` 之后的 `_nudge_scheduler()`（这正是线上行为）。
#
# 于是 `factories.make_scan(status='queued')` 造出来的数据
# **不会**被后台线程悄悄认领并启动 —— 那些是给别的测试用的前置数据，
# 不是要真的跑起来的东西。测试要跑调度就自己调 `get_scheduler().tick()`。
os.environ.setdefault("SUPEX_SCAN_POLL_SECONDS", "3600")
# 扫描产物写进临时目录，别污染项目根（`_scan_runs/` 会积累几百 MB）。
SCAN_WORK_ROOT = tempfile.mkdtemp(prefix="suvex-test-scans-")
os.environ["SUPEX_SCAN_WORK_ROOT"] = SCAN_WORK_ROOT
# ★ 附件也写进临时目录（任务 10 P6-d）。
#
# 少了这一行，上传附件的测试会把文件写进**项目根的 `_uploads/`** ——
# 测试跑完留下一堆垃圾，而且真实的 `.gitignore` 未必覆盖它。
# 与 SCAN_WORK_ROOT 同一个理由，只是那个变量早就设了、这个忘了设 ——
# 而"忘了设"的表现不是测试失败，是仓库里多出东西。
ATTACHMENT_ROOT = tempfile.mkdtemp(prefix="suvex-test-uploads-")
os.environ["SUPEX_ATTACHMENT_ROOT"] = ATTACHMENT_ROOT

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core import db as db_module  # noqa: E402
from app.core.config import get_settings  # noqa: E402

# 一个满足前端 `checkPassword` 三条硬要求的口令（≥12 位 + 大小写 + 数字）。
GOOD_PASSWORD = "SuvexDevPass2026"
OTHER_PASSWORD = "AnotherPass2026x"


def _ensure_database(url: str) -> None:
    """建测试库（若不存在）。

    `CREATE DATABASE` 不能在事务里跑，所以维护连接要用 AUTOCOMMIT ——
    用默认隔离级别会得到 `CREATE DATABASE cannot run inside a transaction block`，
    而那个报错看起来像是权限问题。
    """
    parsed = make_url(url)
    admin_url = parsed.set(database="postgres")
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": parsed.database},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    finally:
        engine.dispose()


def _upgrade_to_head() -> None:
    """对着测试库跑迁移。**不是 `create_all()`** —— 见模块 docstring。"""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    command.upgrade(config, "head")


@pytest.fixture(scope="session", autouse=True)
def _prepare_database() -> Iterator[None]:
    """整个测试会话一次：建库 + 迁移。"""
    _ensure_database(TEST_DATABASE_URL)
    _upgrade_to_head()
    # 换掉可能在导入期就建好的引擎（它可能指向开发库）。
    db_module.reset()
    db_module.configure(TEST_DATABASE_URL, force=True)
    # 确认真的连到了测试库 —— 这条断言挡住的是"跑错库"，
    # 而跑错库的后果是测试把开发数据清空。
    assert str(db_module.get_engine().url).endswith("suvex_test"), (
        f"测试引擎指向了非测试库：{db_module.get_engine().url}"
    )
    yield
    db_module.reset()
    shutil.rmtree(SCAN_WORK_ROOT, ignore_errors=True)
    shutil.rmtree(ATTACHMENT_ROOT, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    """每个用例前清空所有表 + 重置限流器。

    ## ★ 为什么清表之前要先「等后台静默」（任务 10 补）

    扫库用的是 `TRUNCATE ... CASCADE`，它要拿**所有相关表的排它锁**。
    而扫扫描的测试会真的起调度器，那个后台线程可能还在把外部分析产物
    导进库（它自己持有 findings / scans / providers 的行锁）。

    两者撞上就是 PostgreSQL 的 `DeadlockDetected`。症状很难查：
    **单独跑那个用例永远通过**，只有全量跑（跑了六分半、前面有若干
    真扫描）才在**下一个用例的 setup 阶段**炸出来 —— 而报错指向的是
    "建 provider 失败"，跟真正的写入方（上一个用例的后台线程）毫无关系。

    这个偶发在本项目真实发生过两次（同一处、同一姿势），所以不是
    "重跑一下就算了"的那类。修法是把它缺的前提做实：**先等没有在跑的
    扫描，再清表**，而不是放松断言或者把错误吞掉。
    """
    import time

    from sqlalchemy import select

    from app.core.db import get_engine
    from app.scans.models import Scan

    engine = get_engine()

    # 等在途的扫描落地（最多 ~10 秒）。超时也继续 ——
    # 一个卡死的扫描不该让整个测试会话挂住，而清表失败会有它自己的报错。
    with db_module.session_scope() as session:
        for _ in range(50):
            active = session.execute(
                select(Scan.id).where(Scan.status.in_(("queued", "running"))).limit(1)
            ).first()
            if active is None:
                break
            time.sleep(0.2)

    # ★ 表清单从**数据库**读，不用 `Base.metadata`（任务 11 修）。
    #
    # 用 `Base.metadata.sorted_tables` 的写法有个隐式前提：所有模型模块
    # 都已经 import 过了。而 `_clean_state` 是 autouse 的，它跑在
    # `client` fixture **之前** —— 那一刻 `app.main` 还没被导入，
    # metadata 里只有恰好被直接 import 过的那几张表。
    #
    # 症状因此极具迷惑性：`admin_accounts` 漏在清理之外，前一个用例留下的
    # 管理员行活到下个用例，`POST /auth/setup` 直接 409 SETUP_ALREADY_DONE。
    # 而**单独跑那个文件必错、全量跑永远通过** —— 因为全量跑时前面的
    # 文件早就把模型都 import 齐了。我最初就是照"是不是顺序污染"去查的，
    # 方向完全错了。
    #
    # `alembic_version` 必须排除：清掉它会让下次启动的 `upgrade head`
    # 把已经建好的表再建一遍，报 `relation already exists`。
    with engine.begin() as conn:
        names = [
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                )
            )
        ]
        if names:
            listing = ", ".join(f'"{n}"' for n in names)
            conn.execute(text(f"TRUNCATE {listing} RESTART IDENTITY CASCADE"))

    # 限流器是进程内的，不清的话第二个用例会因为第一个用满了配额而 429 ——
    # 而失败的样子是"偶发 429"，排查会往限流实现上找错方向。
    from app.auth import router as auth_router
    from app.scans import service as scans_service

    auth_router.reset_limiters()
    scans_service.reset_limiters()
    yield


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """带 lifespan 的测试客户端。

    必须用 `with` —— 不用的话 lifespan 不跑，`verify_database()` 也不会执行，
    于是"启动自检"这条逻辑在测试里完全没被覆盖。
    """
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def db_session() -> Iterator[object]:
    """直接操作数据库的会话。给"构造前置数据"用（比如造扫描与事件）。"""
    with db_module.session_scope() as session:
        yield session


@pytest.fixture()
def ready_client(client: TestClient) -> TestClient:
    """已完成初始化的客户端：管理员已建、且已登录。

    复用 setup 端点而不是直接插数据库 —— 这样 `ready_client` 依赖的
    那条路径（setup → 自动登录 → Cookie 下发）在每个用例里都被真实走过一遍。
    """
    response = client.post("/api/v1/auth/setup", json={"password": GOOD_PASSWORD})
    assert response.status_code == 201, response.text
    return client


@pytest.fixture()
def settings():
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture()
def scan_config(monkeypatch):
    """临时改扫描相关配置，并在结束后还原。

    ⚠️ 这里必须**手动**清 `get_settings` 的 lru_cache：它是单例，
    不清的话改环境变量不会生效，而症状是"测试跑的是默认配置" ——
    看起来像配置没读对，实际是缓存没失效。

    teardown 里显式 `monkeypatch.undo()` 再清缓存，顺序反了的话
    缓存会在环境还没还原的时候被重新填上。
    """

    def apply(**kwargs):
        for key, value in kwargs.items():
            monkeypatch.setenv(key, str(value))
        get_settings.cache_clear()
        return get_settings()

    yield apply

    monkeypatch.undo()
    get_settings.cache_clear()

    from app.scans.scheduler import reset_scheduler

    reset_scheduler()


# ══════════════════════════════════════════════════════════
# 漏洞前置数据（`test_findings` 与 `test_members` 共用）
# ══════════════════════════════════════════════════════════
#
# ★ 放在 conftest 而不是某个测试文件里（任务 10 P2 搬上来的）。
#
# `test_members` 的指派测试同样需要"有几条已导入的漏洞"，而 fixture
# 定义在 `test_findings.py` 里时它拿不到 —— pytest 不会跨测试文件共享
# 局部 fixture。当时的第一反应是"在 test_members 里再写一份"，
# 但那意味着**两份造数据的口径会各自漂移**，而它们本来就应该造出
# 一模一样的漏洞（否则"3 条漏洞"在两个文件里指的不是同一件事）。


def vuln_record(**overrides) -> dict:
    """一条 strix 形状的漏洞记录。

    字段名与"有才写"的规则都照源码：`report/state.py` 里每个字段都是
    `if xxx: report["xxx"] = ...`，所以缺字段是**正常状态**。
    """
    record = {
        "id": "vuln-0001",
        "title": "调试接口未授权可读环境变量",
        "severity": "high",
        # ⚠️ strix 的时间戳**不是 ISO 8601**：`strftime("%Y-%m-%d %H:%M:%S UTC")`
        "timestamp": "2026-09-24 10:00:00 UTC",
        "description": "GET /debug/env 未做鉴权即可读取进程环境变量。",
        "target": "api.example.com",
        "technical_analysis": "路由注册在鉴权中间件之前。",
        "evidence": "HTTP/1.1 200 OK\nAWS_SECRET_ACCESS_KEY=***",
        "poc_description": "直接请求该路径即可。",
        "poc_script_code": "import requests\nprint(requests.get('https://api.example.com/debug/env').text)",
        "remediation_steps": "把该路由挂到鉴权中间件之后。",
        "confidence": "high",
        "cwe": "CWE-200",
        "cvss": 7.5,
        "finding_class": "dynamic",
    }
    record.update(overrides)
    return record


def write_vulns(run_dir, records: list[dict], *, run_name: str = "run-1"):
    """把漏洞记录写进 `<run_dir>/strix_runs/<run_name>/vulnerabilities.json`。"""
    import json
    from pathlib import Path

    target = Path(run_dir) / "strix_runs" / run_name
    target.mkdir(parents=True, exist_ok=True)
    path = target / "vulnerabilities.json"
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.fixture()
def scan(db_session, tmp_path):
    """一条指向临时运行目录、状态已完成的扫描。"""
    from pathlib import Path

    from tests.factories import make_scan

    created = make_scan(db_session, status="completed")
    created.run_dir = str(tmp_path / "scan")
    created.runner = "fake"
    db_session.commit()
    Path(created.run_dir).mkdir(parents=True, exist_ok=True)
    return created


@pytest.fixture()
def imported(db_session, scan):
    """导入了三条不同严重度/证据强度的漏洞。"""
    from pathlib import Path

    from sqlalchemy import select

    from app.findings import service
    from app.findings.models import Finding

    write_vulns(
        Path(scan.run_dir),
        [
            vuln_record(id="vuln-0001", title="严重问题", severity="critical", confidence="high"),
            vuln_record(id="vuln-0002", title="高危问题", severity="high", confidence="medium"),
            vuln_record(id="vuln-0003", title="低危问题", severity="low", confidence="low"),
        ],
    )
    service.import_from_run(db_session, scan)
    return list(
        db_session.execute(
            select(Finding).where(Finding.scan_id == scan.id).order_by(Finding.source_ref)
        ).scalars()
    )


# ══════════════════════════════════════════════════════════
# provider 前置数据（任务 11 从 test_scans 提上来的）
# ══════════════════════════════════════════════════════════
#
# 与 `scan` / `imported` 同一性质：多个测试文件都要造"一个可用的
# provider"。原来是 `test_scans.py` 的局部 fixture，而
# `test_scan_stall_and_reachability` 也要用它 —— pytest 不跨文件
# 共享局部 fixture，所以在两个文件里各写一份才是真的坏。
#
# KEY 是一个**假的** sk- 串：provider 的校验只做形态检查，
# 而任何测试都不该带真 key。

_PROVIDER_TEST_KEY = "sk-test-SCAN-3f8a1c4e9b7d30625a8c1f4e"


@pytest.fixture()
def provider_id(ready_client):
    """建一个默认 provider 并返回它的 id。"""
    created = ready_client.post(
        "/api/v1/providers",
        json={
            "label": "DeepSeek",
            "model": "deepseek-chat",
            "kind": "cloud",
            "apiKey": _PROVIDER_TEST_KEY,
            "baseUrl": "https://api.deepseek.com",
            "note": "测试用",
            "isDefault": True,
        },
    )
    assert created.status_code in (200, 201), created.text
    return created.json()["provider"]["id"]
