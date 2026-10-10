"""FastAPI 应用装配。

启动顺序（`lifespan`）是有讲究的：

1. **配置日志**，让后面所有输出都有格式（否则启动阶段的日志是裸 print 风格）。
2. **`assert_bootable()`** —— 只有"配置错了却不会报错、只是悄悄不安全"的
   组合才在这里硬失败（当前只有一条：prod + 未开 Secure Cookie）。
3. **打警告** —— 其余危险配置只警告不拦。单机部署的用户改错一个开关就得到
   「容器反复重启、日志里只有一行校验错」是糟糕的体验。
4. **`verify_database()`** —— 连库 + 查表。缺表时给出的错误消息里带
   `alembic upgrade head` 这条命令。

## CORS 的三条约束（契约 §8）

带凭据（Cookie）的跨源请求：

* `Access-Control-Allow-Credentials` 必须是 `true`；
* `Access-Control-Allow-Origin` **必须是具体源**，不能用 `*` —— 浏览器直接拒绝；
* 因此 `allow_origins` 只能来自配置，不能偷懒写成 `["*"]`。

少了这几条的表现是「登录成功但下一个请求又是未登录」——
因为浏览器根本没把 Cookie 发出去，而接口看起来完全正常。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 这个 import 有副作用：把全部 ORM 模型注册到 Base.metadata。
# 缺了它，metadata 是空的 —— 测试里的建表、以及 alembic 的 autogenerate
# 都会看到一张表都没有的世界。
import app.models  # noqa: F401
from app.audit.router import router as audit_router
from app.auth.router import router as auth_router
from app.core.bootstrap import verify_database
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.ctf2.router import router as ctf2_router
from app.events.router import router as events_router
from app.findings.router import router as findings_router
from app.health import router as health_router
from app.members.router import router as members_router
from app.overview.router import router as overview_router
from app.providers.router import router as providers_router
from app.reports.router import router as reports_router
from app.scans.router import router as scans_router
from app.settings.router import router as settings_router

logger = logging.getLogger("suvex")

API_DESCRIPTION = """\
Suvex 后端 API。字段与端点定义见仓库里的 `docs/api-contract.md` ——
**那份文档是唯一事实来源**，发现遗漏先改文档再改代码。

鉴权是两条独立通道：浏览器用会话 Cookie（HttpOnly + SameSite=Strict），
CI / 脚本用 `Authorization: Bearer svx_…`。管理员会话隐含全部权限。

本服务是**安全扫描器**：`POST /scans` 会真的对目标发包，
所以它要求逐字确认的授权声明，并把确认时刻与服务端收到的来源一并落库。
"""


def _configure_logging(level: str) -> None:
    """日志配置。实现搬到 `core/logging.py` 了 —— 那里同时装了脱敏兜底。

    留这个薄封装是因为调用点（lifespan）读起来仍然是一行，
    而"日志从哪配"这个问题只有答案一处。
    """
    from app.core.logging import configure_logging

    configure_logging(level)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_level)

    settings.assert_bootable()
    for warning in settings.startup_warnings():
        logger.warning(warning)

    verify_database()

    # ★ 审计保留期清理：每次启动跑一次。
    #
    # 不设定时任务：单机部署里为"清理及时几小时"多养一个后台线程不划算，
    # 而审计记录多留几个小时没有任何危害（它的保留期是 365 天）。
    # 异常吞掉 —— 清理失败不该拦住启动，而启动不了会让整个平台不可用。
    try:
        from app.audit.service import purge_expired
        from app.core.db import session_scope

        with session_scope() as session:
            purge_expired(session)
    except Exception:  # noqa: BLE001
        logger.exception("审计保留期清理失败（不影响启动）")

    # ★ 扫描保留期清理（v0.13）：自动归档 + 回收站到期。
    #
    # 与审计清理同样放在启动时跑一次，不设后台定时任务 ——
    # 理由一致：单机部署里为"清理及时几小时"多养一个常驻线程不划算，
    # 而且记录多留几小时没有任何危害。
    #
    # ⚠️ 这里**不需要**担心"用户没进过设置页"：`get_retention_config()`
    #    读不到就按默认值建一条，所以首次启动就会用 30 天的默认规则。
    try:
        from app.scans.service import sweep_retention
        from app.core.db import session_scope as scan_session_scope

        with scan_session_scope() as session:
            sweep_retention(session)
    except Exception:  # noqa: BLE001
        logger.exception("扫描保留期清理失败（不影响启动）")

    # ── 补齐缺失的 SLA 到期时刻（任务 10 P3）────────────────
    #
    # `sla_due_at` 是派生列，而它是在功能上线时才加的 —— 那时库里
    # 已有的漏洞全是 NULL，于是列表上一个倒计时都不显示（实测就是
    # 这样：配置对、接口对、`slaState` 却清一色 `none`）。
    #
    # 做成**启动自愈**而不是一次性回填脚本：这条不变量应该始终成立
    # （"未终结的漏洞必须有到期时刻"），将来任何一条忘了调
    # `refresh_sla` 的新代码路径都会被兜住。而它只补 NULL、走索引，
    # 补完之后每次启动都是空扫描。
    try:
        from app.findings import service as findings_service
        from app.core.db import session_scope as sla_session_scope

        with sla_session_scope() as session:
            findings_service.backfill_sla(session)
    except Exception:  # noqa: BLE001
        logger.exception("补齐修复时限失败（不影响启动）")

    # ★ 调度器要在**孤儿回收之前**启动还是之后？
    # 之后 —— 回收会把上一次运行遗留的 `running` 扫描清掉，
    # 而调度器一旦开始跑，就可能把新的 queued 扫描置成 running，
    # 那时回收逻辑会把刚启动的当成"上一次的遗留"给杀掉。
    from app.scans.runner import get_runner
    from app.scans.scheduler import get_scheduler

    scheduler = get_scheduler()
    recovered = scheduler.recover_orphans()
    if recovered:
        logger.warning("上次运行遗留了 %d 条未结束的扫描，已在启动时清理", recovered)

    ready, runner_error = scheduler.runner_status()
    if not ready:
        # 不硬失败：认证、看历史记录、改设置都还能用。
        # 但要说清楚 —— 否则用户点"开始扫描"只会看到扫描立刻失败。
        logger.error("扫描执行后端不可用，新的扫描会立即失败：%s", runner_error)
    scheduler.start()

    # ★ CTF2 解题调度器（v0.20）。
    #
    # 与扫描调度器同一套启动顺序：**先回收孤儿，再启动**。
    # `solving` 的含义是"已受理、正在跑"，而新进程里没有任何对应的
    # worker —— 不清理的话，那些题在界面上永远显示"解题中"，
    # 而用户再点解题也不会重新触发（状态已经是 solving）。
    #
    # ⚠️ 回收会**中断**上一次运行留下的解题（沙箱容器可能是孤儿，
    #    但本函数只改数据库状态）。这与扫描调度器的取舍一致：
    #    宁可重跑，也不要留一个看不见的进程。
    from app.ctf2.scheduler import get_scheduler as get_ctf2_scheduler

    ctf2_scheduler = get_ctf2_scheduler()
    ctf2_recovered = ctf2_scheduler.recover_orphans()
    if ctf2_recovered:
        logger.warning("上次运行遗留了 %d 道 CTF2 题目，已重置为未解出", ctf2_recovered)

    # ★★ 顺带清掉上一次运行遗留的**沙箱容器**（v0.23）。
    #
    # `recover_orphans()` 只改**数据库状态** —— 它把遗留的 `solving`
    # 重置为未解出，但那道题留下的 Docker 容器还在跑。原因见
    # `runner.sweep_orphan_sandboxes` 的 docstring：worker 是线程，
    # 进程退出时杀不掉，`finally` 里的销毁没机会执行。
    #
    # 实测（2026-10-01）：一轮部署里重启几次服务，`docker ps` 就留下了
    # 两个 ctf2-* 容器，而数据库里那两道题已经 `solved`、环境记录是
    # `stopped` —— 两边说法不一致，且没有界面能看出来。
    #
    # ⚠️ **启动时是唯一可靠的时机**：新进程里不可能有属于它的合法容器，
    #    所以"带 ctf2- 前缀的全是遗留"这个判据此刻成立。
    #
    # ⚠️ 清理失败**不阻断启动**（函数内部只记 warning）—— 一次清理
    #    失败不该升级成服务起不来。
    from app.ctf2 import runner as ctf2_runner

    ctf2_runner.sweep_orphan_sandboxes()

    # ★★ 保证解题沙箱所在的 Docker 网络存在（2026-10-01 实测补）。
    #
    # 原来 `CTF2_NETWORK`（"suvex-ctf2"）**只被引用、从来没人创建**，
    # 于是每道题都倒在 `docker run` 那一步：
    #
    #     failed to set up container networking: network suvex-ctf2 not found
    #
    # 而界面上只显示"启动解题沙箱失败" —— 真正的原因（一个本该由我们建的
    # 网络不存在）完全没露出来。实测两道题各轮询两次全部倒在这句上。
    #
    # ⚠️ 它与 `sweep_orphan_sandboxes()` 是同一类动作：**保证运行前提**。
    #    所以放在同一处，而不是塞进"起容器前的自检"——那会让每次起容器
    #    都查一遍（虽然 `SandboxContainer.start` 里也补了一次，那是为了
    #    挡住"服务跑着的时候网络被删"，见那里的说明）。
    #
    # ⚠️ 失败不阻断启动（函数内部只记 warning），与清理那条同一理由。
    ctf2_runner.ensure_ctf2_network()

    ctf2_scheduler.start()

    # ── CTF2 自动增量同步（v0.24：抢首杀）────────────────────
    #
    # ★ 为什么在**清理之后、调度器启动之后**：它启动后会做一次同步，
    #   而同步可能把新题直接推进解题队列 —— 那要求调度器已经在跑。
    #   反过来（先 autosync 再 scheduler）会让"自动开解"推入的题
    #   等一个还没启动的调度器。
    #
    # ⚠️ 它在设置页里可关（`auto_sync_seconds = 0`），而那个值**每轮
    #    重读** —— 所以这里无条件启动线程是对的：关闭状态由间隔值表达，
    #    不由"线程在不在"表达（否则关掉再打开就得重启服务）。
    from app.ctf2.autosync import get_autosync

    get_autosync().start()

    logger.info(
        "Suvex API %s 已就绪（env=%s，前缀=%s，扫描后端=%s，并发上限=%d，CORS 源=%s）",
        settings.app_version,
        settings.env,
        settings.api_prefix,
        get_runner().name,
        settings.max_concurrent_scans,
        ", ".join(settings.cors_origin_list) or "（无）",
    )
    try:
        yield
    finally:
        # 停止时**终止在跑的扫描** —— 见 `scheduler.stop` 的说明：
        # 留着它们会变成没有任何界面能终止的攻击进程。
        scheduler.stop()
        # ⚠️ CTF2 的停止语义不同：线程杀不掉，所以它只"不再启动新的 +
        #    等一会儿"。超时后残留的沙箱容器是**已知且无法在这里解决**的
        #    （见 ctf2/scheduler 的 docstring），所以它会在超时时打一行
        #    warning 提醒去看 `docker ps`。
        from app.ctf2.scheduler import get_scheduler as get_ctf2_scheduler

        get_ctf2_scheduler().stop()

        # 自动同步线程：它可能在一次网络往返里，**不等它跑完** ——
        # 一次同步最多几十秒，而停机不该被一个后台作业拖住。
        from app.ctf2.autosync import get_autosync

        get_autosync().stop()

        logger.info("Suvex API 已停止")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Suvex API",
        version=settings.app_version,
        description=API_DESCRIPTION,
        lifespan=lifespan,
        # 文档路径放在 API 前缀下，与端点同一个命名空间。
        # 放在根上会让「这个 /docs 是哪个服务的」在反代后面变得含糊。
        docs_url=f"{settings.api_prefix}/docs",
        redoc_url=None,
        openapi_url=f"{settings.api_prefix}/openapi.json",
        openapi_tags=[
            {"name": "认证", "description": "登录、初始化、会话"},
            {"name": "健康检查", "description": "无需鉴权的探活"},
            {"name": "扫描", "description": "高风险：会真的对目标发包"},
            {"name": "漏洞", "description": "两个轴：证据强度 + 处置状态"},
            {"name": "报告", "description": "冻结快照，编号由生成动作分配"},
            {"name": "实时事件流", "description": "SSE，带 Last-Event-ID 补发"},
            {"name": "全局统计", "description": "全部派生，不写死"},
            {"name": "LLM Provider", "description": "凭据管理，apiKey 永不出现在响应里"},
            {"name": "设置", "description": "沙箱、通知、令牌、口令"},
            {"name": "审计", "description": "只读。谁在什么时候对什么做了什么"},
            {
                "name": "CTF2",
                "description": "对接 ctf2.dasctf.com：同步题目、沙箱解题、确认提交",
            },
        ],
    )

    # 异常处理器必须在 include_router 之前注册吗？不必 —— 它们挂在 app 上，
    # 而路由是在请求时才查处理器。放在前面只是为了读起来有顺序。
    register_exception_handlers(app)

    app.add_middleware(
        CORSMiddleware,
        # ⚠️ 不能是 ["*"]：带凭据的跨源请求里通配符会被浏览器拒绝。
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "Last-Event-ID"],
        # `Retry-After` 要给前端读到 —— 限流时它会用来告诉用户"多久后再试"。
        # 自定义响应头默认对 JS 不可见，必须显式放出来。
        expose_headers=["Retry-After"],
    )

    for router in (
        health_router,
        auth_router,
        events_router,
        scans_router,
        findings_router,
        reports_router,
        overview_router,
        providers_router,
        settings_router,
        members_router,
        audit_router,
        ctf2_router,
    ):
        app.include_router(router, prefix=settings.api_prefix)

    return app


app = create_app()
