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

import ipaddress
import logging
import os
import shutil
import socket
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

logger = logging.getLogger("suvex.tests")

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ⚠️ 必须在导入任何 app 模块**之前**设置。
# `get_settings()` 是 lru_cache 的单例，一旦被别处调用过就固定了 ——
# 那之后改环境变量不会生效，而症状是"测试跑在了开发库上"。
#
# ══════════════════════════════════════════════════════════════════
# ★★ 没显式指定测试库时，库名**每次运行唯一**（2026-10-21 补）
# ══════════════════════════════════════════════════════════════════
#
# ## 默认值为什么不能是写死的 `suvex_test`
#
# 这台机器上**两个开发会话同时跑全量**是常态（用户明确说过"还有另外
# 一个任务也在跑开发"）。而 `SUPEX_TEST_DATABASE_URL` 在 `/etc/environment`、
# `~/.bashrc`、`~/.profile`、`suvex-api/.env`、systemd unit 里**都没有**
# —— 所以"两个会话都没设它"是最常见的情况，不是例外。
#
# 那种情况下两边都落到同一个 `suvex_test`，而 `_clean_state` 每个用例都要
# `TRUNCATE ... CASCADE`（拿全表排它锁）→ 撞上就是 `DeadlockDetected`。
# 2026-10-21 真的排查过一轮：症状是**大量** errors（Windows 上实测 367 条），
# 报错指向的表与真正的写入方毫无关系，只能靠数据目录 mtime、pytest temp
# root 编号连续性、各脚本里写死的库名三者互相印证才定得下来。
#
# 与其靠人记得"跑全量前先建个独立库"，不如让**结构上撞不上**：
# 没显式设变量时，库名带一个本次运行唯一的后缀。
#
# ## 显式设了就不动
#
# 那条路径本来就是安全的（谁设谁负责），而且 CI 需要一个**可预测**的
# 库名来复用/清理，所以显式值原样使用，一个字都不改。
#
# ## 后缀选什么
#
# `os.getpid()` + `time.time_ns()` 的十六进制。PID 单独不够（容器里
# 两个会话可能拿到同名 PID），时间戳单独也不够（两个进程可能落在同一
# 纳秒），两个拼起来就能区分。
#
# ⚠️ **不要截断**。第一版写了 `[-12:]`，而 `time_ns()` 的十六进制是
#    16 位 —— 截掉后正好把 PID 那段全丢了，剩下纯时间戳，于是"同一纳秒
#    的两个不同 PID"会拼出同一个名字。实测：200000 次采样只剩 78 个
#    唯一值。这种"看起来唯一、其实不是"的标识比不唯一更危险。
#
# ⚠️ 名字必须以 `suvex_test` 结尾 —— `_prepare_database` 里那条
#    "测试引擎指向了非测试库"的断言就是按这个后缀认的。它挡的是
#    "跑错库把开发数据清空"，不能为了让名字好看而放松它。
_DEFAULT_TEST_DB_PREFIX = "suvex_autotest"
_AUTOTEST_DB_TAG = f"{os.getpid():x}_{time.time_ns():x}"
_EXPLICIT_TEST_DATABASE_URL = os.environ.get("SUPEX_TEST_DATABASE_URL")
#: 本次运行是否用的是**自动生成**的库名（teardown 时据此决定要不要删库）。
AUTOTEST_DATABASE_GENERATED = not _EXPLICIT_TEST_DATABASE_URL

if _EXPLICIT_TEST_DATABASE_URL:
    TEST_DATABASE_URL = _EXPLICIT_TEST_DATABASE_URL
else:
    TEST_DATABASE_URL = (
        "postgresql+psycopg://suvex:suvex_dev_local@127.0.0.1:55432"
        f"/{_DEFAULT_TEST_DB_PREFIX}_{_AUTOTEST_DB_TAG}_suvex_test"
    )
    # 把生成值写回环境变量：并发的**子进程**（真起容器的集成测试会 reload
    # 配置）必须看到同一个库，否则它们会各自再生成一个。
    os.environ["SUPEX_TEST_DATABASE_URL"] = TEST_DATABASE_URL

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

# ★★ v0.39：测试里**打开**文档端点。
#
# `SUPEX_EXPOSE_DOCS` 默认是 **False**（公网不该匿名可读全部端点清单），
# 而 `test_health.py::test_openapi_schema_covers_every_contract_endpoint`
# 正是靠读 `/openapi.json` 来对账"契约里定义的端点有没有被悄悄删掉"。
#
# 所以这里显式打开它 —— 那是**测试的诉求**，不是"生产默认值该改"。
# ⚠️ 用 `setdefault` 而不是直接赋值：外部若显式设成 false，
#    那条对账测试会**跳过**（见测试里的说明），而不是拿一个空 schema 崩掉。
os.environ.setdefault("SUPEX_EXPOSE_DOCS", "true")

# ══════════════════════════════════════════════════════════════════
# ★★ 测试**绝不**读部署用的 `.env`（v0.39 补，2026-10-15）
# ══════════════════════════════════════════════════════════════════
#
# `Settings` 的 `env_file` 指向**项目根 `.env`**（`config.py:33`），
# 而那份文件按设计**只属于部署**：`.env` 在 CI 里根本不存在
# （被 `.gitignore` 挡住，checkout 里没有），所以 CI 天然不受它影响。
#
# 问题出在**开发机 / 部署机**上：那里 `.env` 是真文件，于是测试进程
# 会把它当成配置来源。2026-10-15 实测症状 —— Kali 上
# `tests/test_metrics.py` **8 条全红**，而 CI 全绿，代码一字未改。
# 差别只有一个：Kali 的 `.env` 里有
#
#     SUPEX_EXPOSE_METRICS=false        ← v0.39 加固时按公网要求设的
#
# 于是 `/metrics` 返回 404，而那 8 条测试的前提就是"端点可读"。
#
# ## 为什么修的是**测试**而不是把 `.env` 改回去
#
# 「把线上开关改回 true」能让测试变绿，但那等于**用削弱生产安全来
# 换测试通过** —— 而 `/metrics` 匿名可读正是这次要关掉的东西。
# 更根本的是：测试的绿灯**不该取决于部署机碰巧怎么配**。
# 同一条测试在两台机器上得出的结论相反，那它就不是测试。
#
# ## 为什么是 `setdefault` 而不是直接赋值
#
# 与上面 `SUPEX_EXPOSE_DOCS` 同一个理由：外部（CI 变量、`-E` 一次性
# 覆盖）显式设的值必须仍然生效。`setdefault` 只补**没被设过**的那些，
# 所以"有人刻意要跑关掉指标的配置"这条路径没有被堵死。
#
# ⚠️ 三个开关都要写。写漏一个的表现就是"某个测试文件莫名其妙全红"，
#    而错误信息（`assert None ...`）指向断言，不指向配置来源 ——
#    我这次就是从断言一路查到 `.env` 才找到根的。
os.environ.setdefault("SUPEX_EXPOSE_METRICS", "true")
os.environ.setdefault("SUPEX_EXPOSE_SANDBOX_IMAGE", "false")

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

# ══════════════════════════════════════════════════════════════════
# ★★ 外呼护栏：测试**一个包都不许发到本机之外**
# ══════════════════════════════════════════════════════════════════
#
# ## 为什么需要它（2026-10-15 全量跑实测抓到）
#
# 一次全量基线跑里，日志出现：
#
#     ctf2chal_01M4ZT73ZPZPM4MWQQVF3E4GB4 → CTF2 拒绝了这次请求（400）
#     ... 平台标记：INVALID_ID（errors.validation.invalid_id）
#
# 这条**不是** mock 编出来的 —— 它是测试进程真的向
# `https://ctf2.dasctf.com` 发了请求，只因夹具的 PAT 是假的
# （`pat_ctf2_TEST-…`）才被平台以 400 挡回来。
#
# 它为什么违反了红线：**"绝不消耗 CTF2 平台真实额度"是团队的硬约束**，
# 而"这次只是 400、没提交成功"是运气 —— 同样的路径在别的用例里
# 可能真的打到提交端点。测试不该有任何真实外呼，一次都不该有。
#
# ## 根因
#
# `client` fixture 用 `with TestClient(app)`，于是 lifespan 真的跑：
# 它 `ctf2_scheduler.start()` + `get_autosync().start()`，两个后台线程
# 就此起来。而扫描那条路早就有对策（`SUPEX_SCAN_POLL_SECONDS=3600`
# 让调度线程睡死），**CTF2 没有对应处理** —— 它的认领间隔来自库里
# 的设置，默认 1 秒。
#
# 于是某个用例把一道题置成 `solving` 之后，后台线程**真的**去认领、
# 真的启动 worker、真的连平台。
#
# ## 为什么护栏做在 socket 层，而不是"记得给每个用例打桩"
#
# 打桩要靠人记得。漏掉一处就是一个静默的真实外呼 —— 而它的表现
# 是"某个用例偶尔慢几秒"，没人会去查。做在 socket 层则是**默认安全**：
# 新写的用例天然被罩住，忘了打桩也不会打出去。
#
# ## 判据：只放行回环
#
# 放行 `127.0.0.0/8` / `::1` / `localhost`，其余一律拒。
# 本项目的测试只需要连本机的 PostgreSQL（`127.0.0.1:55432` 或 CI 的
# `127.0.0.1:5432`）—— 那是回环，不受影响。
#
# ⚠️ 打的是**模块里的** `socket.getaddrinfo` 与 `socket.socket.connect`：
#    `httpx` → `httpcore` → `anyio` 最终都落到这两个符号上（实测确认
#    anyio 走的是 `socket.getaddrinfo` 而不是自己缓存一份）。
#
# ⚠️ 拦截**不是**静默跳过：抛的是明确的 `OSError`，调用方要么把这次
#    请求当成失败（正是我们要的：测试里不该有成功的外呼），要么自己
#    打桩。绝不吞掉。


class EgressBlocked(OSError):
    """测试里试图向本机之外发请求。见上面那段说明。"""


_ALLOWED_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost"}
# ⚠️ **不按端口白名单**放行整个回环是刻意的：回环上的其它端口是
#    "测试自己起的本地 server"（比如验证 302 跟随的靶子），那些**应该**
#    被允许。所以判据只有一条：**非回环地址一律拒**，端口不加限制。


def _is_loopback(host: Any) -> bool:
    """这个主机是不是本机。`host` 可能是域名、IP 字面量或 bytes。

    ⚠️ `None` 与空串**算本机**：`getaddrinfo(None, port)` 的语义是
       "给我一个可用于**本机绑定**的通配地址"（`AI_PASSIVE`），它不指向
       任何外部主机 —— 拦它只会误伤"测试自己起一个本地 server"。
       真正的出站风险由 `connect()` 那一道守（那里没有 None 的可能）。
    """
    if host is None:
        return True
    text = host.decode() if isinstance(host, bytes) else str(host)
    if not text:
        return True
    if text in _ALLOWED_HOSTS:
        return True
    try:
        return ipaddress.ip_address(text.strip("[]")).is_loopback
    except ValueError:
        return False


@pytest.fixture(scope="session", autouse=True)
def _block_egress() -> Iterator[None]:
    """整个会话：把"本机之外"的网络访问掐掉。

    ⚠️ session 级 + autouse：它必须在**任何**测试之前生效，
       而且不能有用例跑在护栏之外。
    """
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any):  # noqa: ANN202
        if not _is_loopback(host):
            raise EgressBlocked(
                f"测试试图解析非本机地址 {host!r} —— 测试不该有任何真实外呼。"
                "要测网络行为请打桩（见 tests/conftest.py 的说明）。"
            )
        return real_getaddrinfo(host, *args, **kwargs)

    def _check(address: Any) -> None:
        # AF_UNIX 的地址是路径字符串，不是 (host, port) —— 放行。
        if isinstance(address, (str, bytes)):
            return
        # ⚠️ 这里**不**复用 `_is_loopback` 对 None/空串的宽松（那是给
        #    `getaddrinfo` 的通配绑定语义留的）：连接一个说不清的目标
        #    应当**拒绝**，而不是放行。fail-closed。
        host = address[0] if isinstance(address, tuple) and address else None
        if not host or not _is_loopback(host):
            raise EgressBlocked(
                f"测试试图连接非本机地址 {address!r} —— 测试不该有任何真实外呼。"
                "要测网络行为请打桩（见 tests/conftest.py 的说明）。"
            )

    def guarded_connect(self: socket.socket, address: Any):  # noqa: ANN202
        _check(address)
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any):  # noqa: ANN202
        _check(address)
        return real_connect_ex(self, address)

    socket.getaddrinfo = guarded_getaddrinfo
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    try:
        yield
    finally:
        socket.getaddrinfo = real_getaddrinfo
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex


from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from alembic import command  # noqa: E402
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


def _drop_database(url: str) -> None:
    """删掉本次运行自动建的测试库（`_ensure_database` 的逆操作）。

    ## 为什么需要这一步

    自动生成的库名每跑一次都是新的，不删的话会一直积在
    `pg_database` 里 —— 那是把一个"偶发死锁"换成"磁盘慢慢被占满"，
    只是换了个更难察觉的麻烦。

    ## 为什么要先断连接

    PostgreSQL **不允许删除还有活动连接的库**：会得到
    `database "xxx" is being used by other users`。所以先
    `pg_terminate_backend` 把所有后端踢掉 —— 测试进程自己也可能还挂着
    一个连接（`db_module.reset()` 之后不一定立刻归还）。

    ## 为什么失败只警告不抛

    teardown 抛异常会让整轮测试的结果变成 ERROR，而"库没删掉"只是一件
    需要人顺手清理的小事，不该盖住真正的测试结论。

    ⚠️ 只有**自动生成**的库才走这里。显式设了 `SUPEX_TEST_DATABASE_URL`
       的人自己负责那台库 —— 悄悄删掉别人指定要复用的库是灾难。
    """
    parsed = make_url(url)
    name = parsed.database
    if not name:
        return
    admin_url = parsed.set(database="postgres")
    try:
        engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                        " WHERE datname = :name AND pid <> pg_backend_pid()"
                    ),
                    {"name": name},
                )
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        finally:
            engine.dispose()
    except Exception:  # noqa: BLE001 - 删不掉不该盖住测试结论
        logger.warning("自动测试库 %s 没删掉，请手动清理", name, exc_info=True)


def _upgrade_to_head() -> None:
    """对着测试库跑迁移。**不是 `create_all()`** —— 见模块 docstring。"""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    command.upgrade(config, "head")


# ══════════════════════════════════════════════════════════════════
# ★ 加密密钥：给测试补一条**临时的** SUPEX_SECRET_KEY
# ══════════════════════════════════════════════════════════════════
#
# ## 为什么需要它（2026-10-10 第 7 次 CI 暴露）
#
# 前 6 次 CI 全死在 alembic 那一步，`pytest` 从没真正跑起来过。
# 修好那一步后第一次跑全量，立刻红了 7 个用例，原因全是同一个：
#
#     app.core.crypto.SecretKeyMissing:
#     尚未配置 SUPEX_SECRET_KEY，无法加密或解密 provider 凭据。
#
# 而**本地一直是绿的** —— 因为开发机上有一份 `.env`，里面是真密钥。
# CI 上没有 `.env`（它本就不该入库，见 hygiene job 的硬闸门），
# 于是"跑测试"隐含了一个没人写下来的前提：**你得先自己配一把密钥**。
#
# ## 为什么用 fixture，而不是写进上面那段环境变量
#
# 上面那段（`SUPEX_TEST_DATABASE_URL` 等）是在**导入 app 模块之前**
# 设死的。密钥不能那样设：pydantic-settings 的优先级是
# **环境变量 > .env 文件**，所以在这里写
# `os.environ["SUPEX_SECRET_KEY"] = <随机值>` 会把开发机 `.env` 里那把
# **真密钥顶掉** —— 密钥一换，本地库里已加密的 provider 凭据就全解不开了。
#
# 所以用 fixture + `if is_configured(): return`：外部（CI 变量或本地
# `.env`）**已经配了**就一个字都不动，只有真没配时才补一把临时密钥，
# 且用例结束后自动还原。
#
# ## 为什么必须清 get_settings 的缓存
#
# `get_settings()` 是 lru_cache 单例，不清缓存的话改环境变量不生效，
# 症状是"测试用的是上一个用例的配置"——看起来像配置没读对，
# 其实是缓存没失效。这条在 `tests/kernel/conftest.py` 里已经踩过一回。
@pytest.fixture(autouse=True)
def _secret_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """确保有 `SUPEX_SECRET_KEY`。已配（CI 变量或本地 `.env`）则不动它。"""
    from app.core import crypto

    if crypto.is_configured():
        yield
        return
    monkeypatch.setenv("SUPEX_SECRET_KEY", crypto.generate_key())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True, scope="session")
def _stable_getsource() -> Iterator[None]:
    """把 `inspect.getsource` 换成本项目的抗漂移版本（`tests/source_guard.py`）。

    ## 为什么需要

    本项目有 38 处**源码级断言**：`assert "coord.note_action(" in
    inspect.getsource(multiagent.run_agent_loop)`。这类断言用
    `__code__.co_firstlineno` 去**当前磁盘文件**按行号切片，隐含假设
    「磁盘文件 == 被 import 的那份」。

    全量测试跑 35–45 分钟，中途只要有人（并行会话、变异测试脚本、
    自己边跑边改）动了被 import 的源文件，行号就漂了 —— 断言切到
    **别的函数**，于是报红，而红的理由与要守的东西毫无关系。

    ## 实测过的事故（不是假想）

    - `pytest tests/kernel/` 全量 43 分钟那次，3 条接线测试报红，报错里
      打印的 `src` 开头是 `@dataclass class AgentLoopResult` ——
      而它们断言的是 `run_agent_loop` 的内容。
    - 清掉 `__pycache__` 后单跑 `tests/kernel/test_termination.py`
      → 54 passed；全量复跑 → 1640 passed / 0 failed。
    - 所以那 3 条既不是代码回归、也不是 stale pyc 单独造成的，
      是「进程内已 import + 磁盘文件被改」这个组合。

    ## 换了之后

    `tests/source_guard.getsource` 不用行号，按**名字**解析磁盘文件的 AST
    找定义节点再切片，文件被加几百行也不影响。同名定义用限定名链消歧。

    ## 为什么是 session 级、且是 autouse

    - 38 处调用点分散在 12 个文件里，逐个改 import 既啰嗦又容易漏；
      替换函数本身，所有既有调用点自动生效。
    - session 级：替换/还原各一次，不给每个用例增加开销。
    """
    import inspect

    from tests.source_guard import getsource as stable_getsource

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(inspect, "getsource", stable_getsource)
        yield


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
    # ★ 自动生成的库名每次运行都是新的，用完必须删 —— 否则每跑一次就在
    #   `pg_database` 里多留一个，把"偶发死锁"换成"磁盘慢慢被占满"。
    #   ⚠️ 必须放在 `db_module.reset()` **之后**：那一步才归还连接，
    #   而 PostgreSQL 不允许删除还有活动连接的库。
    if AUTOTEST_DATABASE_GENERATED:
        _drop_database(TEST_DATABASE_URL)


@pytest.fixture(scope="session", autouse=True)
def _ctf2_background_threads_stay_idle() -> Iterator[None]:
    """★ lifespan 起的那两个 CTF2 后台循环**不许在测试里自己动**（2026-10-15 补）。

    ## 为什么需要它（全量跑实测抓到的一条 ERROR）

    全量基线跑挂在：

        ERROR tests/kernel/test_solve_attempt.py::test_v5_without_an_attempt_it_falls_back_to_global
        E  psycopg.errors.DeadlockDetected: deadlock detected
        E  Process 7036 waits for AccessExclusiveLock on relation 24693
        E  ...  [SQL: TRUNCATE "provider_accounts", ..., "ctf2_writeups" ...]

    与 conftest 里那条"扫描版偶发"是**同一类**：`_clean_state` 要拿
    全表排它锁，而某个后台线程正持着行锁写库。区别只在于这次的写入方
    是 CTF2 的解题 worker。

    ## 链条（每一环都实测过）

    1. `client` fixture 用 `with TestClient(app)` → lifespan 真的跑；
    2. lifespan 里 `ctf2_scheduler.start()`（`main.py:214`）与
       `get_autosync().start()`（`main.py:228`）把两个循环拉起来；
    3. 扫描那条路早有对策 —— `SUPEX_SCAN_POLL_SECONDS=3600` 让扫描
       调度循环一轮之后睡死。**CTF2 没有对应处理**；
    4. CTF2 的认领间隔来自库里的设置，默认
       `DEFAULT_CTF2_SOLVE_POLL_SECONDS = 1.0`（**每秒**一次）；
    5. 于是某个用例把一道题置成 `solving` 之后，后台循环**真的**认领、
       **真的**启动 worker（`service.start_solve` → `_enqueue` →
       `tick()` 这条路径在测试里被真实走到）；
    6. worker 持行锁写库，撞上下一个用例 setup 的 `TRUNCATE` → 死锁。

    ## 三层防护，各挡一件事

    | 防护 | 挡什么 |
    | --- | --- |
    | `_ctf2_solver_never_runs_for_real`（替身 worker） | **真正的写入方**：不跑解题就没有持锁写库的线程 |
    | `_block_egress`（socket 护栏） | **真实外呼**：即便有漏网的路径也发不出去 |
    | `stop_ctf2_background_loops()`（在 `client` 里调） | 后台循环的**空转认领**：不必反复查库、反复认领 |

    ⚠️ 第三层为什么是"启动后立刻停"而不是"根本不启动"：lifespan 的接线
       **必须**被真实走过（`start()` 被调用、日志照打、`recover_orphans`
       与 `ensure_ctf2_network` 照跑）。停掉的是**行为**，不是接线。
       这与扫描那条"让循环睡死"是同一个效果的两种表达 —— 差别只在于
       CTF2 的间隔是**库里的设置**（没有环境变量可拉长），所以用 `stop()`
       表达更直接，且**不需要往库里写一行配置**（写配置会改变
       "没有配置行时用代码默认值"这个被文档化的语义）。

    ⚠️ 停的时机是安全的：两个循环的第一步都是 `self._stop.wait(interval)`
       （`scheduler._loop` / `autosync._loop`），而 `interval ≥ 0.5` 秒 ——
       所以"进入 TestClient 之后立刻 stop"必然发生在**第一次 tick 之前**。
    """
    try:
        yield
    finally:
        # 单例是**全局**状态，留着会让后续阶段继承一个残留的循环。
        #
        # ⚠️ 必须先 `stop()` 再 `reset_*()`：`reset_autosync()` 的 docstring
        #    写明它"**不**负责停线程，调用方自己 stop" —— 只 reset 会把一个
        #    还在跑的线程丢掉引用（再也没人能停它）。
        try:
            from app.kernel.autosync import get_autosync, reset_autosync
            from app.kernel.scheduler import get_scheduler, reset_scheduler

            get_scheduler().stop(timeout=5.0)
            get_autosync().stop(timeout=5.0)
            reset_scheduler()
            reset_autosync()
        except Exception:  # noqa: BLE001
            pass


def stop_ctf2_background_loops() -> None:
    """停掉 CTF2 的两个后台循环（由 `client` / `viewer_client` 在 lifespan 之后调用）。

    见 `_ctf2_background_threads_stay_idle` 的说明：停的是**行为**，
    lifespan 的接线照旧被走过。

    ⚠️ 用 `get_*()` 拿**单例**（就是 lifespan 启动的那两个），
       而不是新建实例 —— 新建的实例本来就没在跑，停了等于没停。
    """
    try:
        from app.kernel.autosync import get_autosync
        from app.kernel.scheduler import get_scheduler

        get_scheduler().stop(timeout=5.0)
        get_autosync().stop(timeout=5.0)
    except Exception:  # noqa: BLE001 - 没有单例（未导入）时跳过
        pass


@pytest.fixture(autouse=True)
def _ctf2_solver_never_runs_for_real() -> Iterator[None]:
    """★ 测试里**绝不真的解题**（2026-10-15 补）。

    与 `SUPEX_SCAN_RUNNER=fake` 是同一条纪律的另一半：扫描有替身后端，
    而 CTF2 的解题 worker 没有 —— 它是"起沙箱容器 → 连平台 → 跑 LLM
    循环 → 提交 flag"的真家伙。测试里让它跑起来，后果是：

    * 向 `ctf2.dasctf.com` 发真实请求（实测抓到过，见
      `_block_egress` 的说明）；
    * 可能真的 `docker run` 起解题沙箱；
    * 持行锁写库，与 `_clean_state` 的 `TRUNCATE` 撞成死锁。

    ★ 注意它**不能**只靠"把后台循环停掉"来替代：`service.start_solve`
    → `_enqueue` → `tick()` 会**同步**启动 worker，那条路径不经过后台
    循环 —— 测试里它被真实走到，所以必须在这里把 worker 本身换掉。

    替身只记账、不干活：不碰网络、不碰 Docker、不写库。

    ⚠️ 用 `monkeypatch`（function 级）而不是直接改模块属性：用例自己的
       `monkeypatch.setattr("app.kernel.runner.run_worker", …)` 会**覆盖**
       它，跑完自动还原回这一份 —— 两边不会打架。

    ⚠️ 替身**不改数据库状态**。生产语义里 worker 结束时会把题目状态
       写回（solved / failed / …），测试里保持 `solving` 即可 ——
       没有任何用例依赖"后台把状态改走"（实测确认；状态机由
       `test_runner.py` 直接调 `_advance_challenge` 覆盖）。

    ⚠️★ 替身必须用 `functools.wraps` 包住**真实的那份**：
       `test_runner.py` 有一批 `inspect.getsource(runner.run_worker)` 的
       **源码级**断言（比如"必须把 `attempt_id` 透传给 `_run_worker`"）。
       裸替身会让 `getsource` 读到替身自己 → 那批断言全红，而红的理由
       与它们要守的东西毫无关系。`wraps` 让 `signature` / `getsource`
       / `getsourcefile` 全部穿透到真实实现（已实测确认），只有**调用**
       落到替身上。
    """
    import functools

    from app.kernel import runner as ctf2_runner

    def _noop(challenge_id: str, *, resume: bool = False, attempt_id: str | None = None) -> None:
        logger.debug("测试替身：跳过真实解题 %s", challenge_id)

    stub = functools.wraps(ctf2_runner.run_worker)(_noop)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ctf2_runner, "run_worker", stub)
        yield


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

    ## ★ 2026-10-15：CTF2 那一半也补上了

    同一条 ERROR 后来又在 **CTF2 的用例**上出现（全量跑，
    `test_solve_attempt.py` 的 setup 阶段）—— 写入方换成了解题 worker。
    三层防护见 `_ctf2_background_threads_stay_idle` 的表格。

    这里只做一件事：清表之后把 CTF2 的两个后台间隔重新写成静音
    （TRUNCATE 会把 `ctf2_config` 那行一起清掉，所以不能只在会话开始时写一次）。
    """
    import time

    from sqlalchemy import select

    from app.core.db import get_engine
    from app.scans.models import Scan

    engine = get_engine()

    # ★ CTF2 的两个后台循环：**抢锁之前**先让它们停手（2026-10-15 补）。
    #
    # ⚠️ 位置必须在 `TRUNCATE` **之前** —— 放到后面等于没做：死锁就是
    #    "TRUNCATE 拿排它锁"与"后台写手持行锁"互相等，先清表再停已经撞上了。
    #
    # 为什么放在这里而不是只在 `client` 里停：lifespan 的入口不止一个
    # （`client` / `viewer_client` / `test_users_and_roles` 里两处自建的
    # `TestClient`），每个都记得调一次是**靠人记得**的约定 —— 漏一个就是
    # 一个静默的后台写手。放在 autouse 里则是默认安全。
    stop_ctf2_background_loops()

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


def _no_sweep_orphan_sandboxes() -> int:
    """`runner.sweep_orphan_sandboxes` 的测试替身：不碰 Docker。

    见 `client` fixture 的说明 —— lifespan 里那次 sweep 会 `docker rm -f`
    掉**所有** `ctf2-` 前缀的容器，包括生产服务正在解题的那一个。
    """
    logger.debug("测试替身：跳过 sweep_orphan_sandboxes（不碰 Docker）")
    return 0


#: 给用例一个能验证"替身确实装上了"的标记。
#: 真正的守卫是 `tests/kernel/test_sandbox_sweep.py::test_the_test_client_never_sweeps_real_containers`
#: —— 它读这个标记。这里原先把名字写成了另一个并不存在的用例，
#: 指向不存在的守卫比没有注释更糟：读的人会以为那道防线已经有了。
_no_sweep_orphan_sandboxes._is_test_stub = True  # type: ignore[attr-defined]


async def _chat_resumer_stays_idle(*_args: object, **_kwargs: object) -> None:
    """替身：测试里**不真跑**对话轮的接手循环（v1.19）。

    ## 为什么必须替身掉

    lifespan 会 `asyncio.create_task(resumer.run_forever(...))`，而它每 10 秒
    扫一次"心跳过期但没结束"的轮次并**真的接着跑**（那意味着真实调用模型）。
    而本仓库有一批用例**故意**造出 `interrupted` / `running` 的行来测判据 ——
    后台循环一跑起来就会把它们 claim 走，测试当场变成"有时通过"。

    这与 `_no_sweep_orphan_sandboxes` 是同一条纪律：**接线必须被真实走过**
    （lifespan 里那行 `create_task` 照旧执行、日志照打），但**行为必须静音**。
    """


def _install_noop_resumer(monkeypatch: pytest.MonkeyPatch) -> None:
    """把接手循环换成空协程（必须在 `TestClient(app)` 进入**之前**调用）。"""
    from app.chat import resumer as chat_resumer

    monkeypatch.setattr(chat_resumer, "run_forever", _chat_resumer_stays_idle)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """带 lifespan 的测试客户端。

    必须用 `with` —— 不用的话 lifespan 不跑，`verify_database()` 也不会执行，
    于是"启动自检"这条逻辑在测试里完全没被覆盖。

    ★ 进入之后**立刻停掉 CTF2 的两个后台循环**（2026-10-15 补）：
    lifespan 会把它们启动（`main.py:214` / `:228`），而它们的认领间隔
    默认 1 秒 —— 在测试里那就是"后台真的去认领并解题"，后果既包括
    真实外呼，也包括与 `_clean_state` 的 `TRUNCATE` 抢锁（全量跑实测
    抓到过死锁）。接线必须照跑，行为必须静音 —— 见
    `_ctf2_background_threads_stay_idle` 的说明。

    ★★ 进 lifespan 之前**先把 `sweep_orphan_sandboxes` 换成替身**（2026-10-17 补，
    真事故）。这一条比上面那条严重得多，因为它删的不是测试自己的东西：

        `main.py:194` 在 lifespan 里调 `sweep_orphan_sandboxes()`，而它做的是
        `docker ps -aq --filter name=^ctf2-` → `docker rm -f <全部 id>`。
        在**生产进程**里这个动作是对的（注释里那句"新进程里不可能有属于它的
        合法容器"在生产进程内成立）。但测试进程启动时，**这台机器上可能正有
        一道题的解题沙箱在跑** —— 于是跑一次 pytest 就把人家 `docker rm -f`
        掉了。

    事故现场（2026-10-17 12:28，EDT）：三道题在 40 秒内接连失败，每题都是
    起容器后 1–2 秒沙箱消失、`run_shell` 直接报
    `Error response from daemon: No such container: ctf2-…`，
    `docker run -d --rm` 返回码还是 0。docker daemon 日志那三条
    `removal of container … is already in progress` 就是"AutoRemove 与
    另一路删除撞车"。用户看到的结果是：轮数 2、耗时 8 秒、未解出。

    ⚠️ **必须 patch 在 `with TestClient(app)` 外面**：lifespan 在 `__enter__`
       里就跑完了，进了 `with` 再 patch 已经晚了。
    ⚠️ **只 patch 在这一个 fixture 上**，不要做成 autouse：
       `tests/kernel/test_sandbox_sweep.py` 要测的就是这个函数的真实逻辑
       （它自己 patch `_run` 来伪造 Docker），全局替换会让那批用例失去意义。
    ⚠️ 用 `monkeypatch` 而非直接赋值：用例自己的 `monkeypatch.setattr`
       可以覆盖它，跑完自动还原，两边不打架。
    """
    from app.kernel import runner as ctf2_runner
    from app.main import app

    monkeypatch.setattr(ctf2_runner, "sweep_orphan_sandboxes", _no_sweep_orphan_sandboxes)
    # ★ v1.19：对话轮的接手循环同样只许接线、不许真跑（见 _chat_resumer_stays_idle）
    _install_noop_resumer(monkeypatch)

    with TestClient(app) as test_client:
        stop_ctf2_background_loops()
        yield test_client


@pytest.fixture()
def db_session() -> Iterator[object]:
    """直接操作数据库的会话。给"构造前置数据"用（比如造扫描与事件）。"""
    with db_module.session_scope() as session:
        yield session


# 测试用的账号常量（v0.24 起登录要用户名）。
#
# 放在模块级而不是写死在 fixture 里：**很多测试需要"以管理员身份"
# 或"以只读身份"发请求**，而它们要能引用同一个名字。
GOOD_USERNAME = "admin"
VIEWER_USERNAME = "reader"
VIEWER_PASSWORD = "SuvexViewerPass2026"


@pytest.fixture()
def ready_client(client: TestClient) -> TestClient:
    """已完成初始化的客户端：管理员已建、且已登录。

    复用 setup 端点而不是直接插数据库 —— 这样 `ready_client` 依赖的
    那条路径（setup → 自动登录 → Cookie 下发）在每个用例里都被真实走过一遍。

    ⚠️ v0.24 起 `setup` 要 `username` —— 多用户之后"第一个管理员"
    也得有个名字。
    """
    response = client.post(
        "/api/v1/auth/setup",
        json={"username": GOOD_USERNAME, "password": GOOD_PASSWORD},
    )
    assert response.status_code == 201, response.text
    return client


@pytest.fixture()
def viewer_client(ready_client: TestClient, db_session) -> TestClient:
    """**已登录的只读用户**客户端（v0.24）。

    ## 怎么造

    用管理员（`ready_client`）通过 `POST /users` 建一个 viewer，
    然后**换一个干净的客户端**登录成它。

    为什么不复用同一个 client：Cookie 是客户端级别的，同一个
    TestClient 上重新登录会覆盖 Cookie —— 那样"管理员建的 viewer"
    与"viewer 自己"是同一个连接，测出来的东西不说明问题。
    （而且 `ready_client` 的 Cookie 一旦被覆盖，后续要用管理员的
    测试就悄悄变成了 viewer 身份，失败信息会指向完全无关的地方。）

    ## 它验的是什么

    角色模型的**核心断言**：viewer 能读、不能写、不能发起扫描。
    这一组如果漏了，"分两档角色"就只是一句声明 ——
    而权限模型的漏判是最不该靠人工 review 的东西。
    """
    from fastapi.testclient import TestClient as _TestClient

    from app.main import app

    created = ready_client.post(
        "/api/v1/auth/users",
        json={
            "username": VIEWER_USERNAME,
            "password": VIEWER_PASSWORD,
            "role": "viewer",
        },
    )
    assert created.status_code == 201, created.text

    with _TestClient(app) as viewer:
        logged_in = viewer.post(
            "/api/v1/auth/login",
            json={"username": VIEWER_USERNAME, "password": VIEWER_PASSWORD},
        )
        assert logged_in.status_code == 200, logged_in.text
        yield viewer


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
