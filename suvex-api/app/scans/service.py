"""`scans` 领域的业务逻辑：创建、认领、启动、收尾、终止、重跑。

## 状态机

```
queued ──启动──> running ──退出码 0/2──> completed
   │                │
   │                ├──退出码其它────────> failed
   │                │
   └────终止────────┴────终止────────────> aborted
```

三条硬约束，每一条都有具体理由：

1. **初始状态是 `queued` 而不是 `running`。** 契约 §3.3 专门标了：
   `running` 意味着"agent 已经在打了"，而实际要等调度器接单。
   **状态语义不能撒谎** —— 用户看到 `running` 会以为已经在测了。
2. **`finishedAt` 只在终态写，`durationSec` 只在终态算。**
   数据库层有一条 CHECK 约束强制这件事（`ck_scans_finished_at_matches_terminal_status`）。
3. **退出码 2 是成功。** 它表示"发现了漏洞"，不是执行失败。
   把结果不干净当成执行失败，是这一层最容易写错的一处。

## 授权留痕（契约 §5.4）

六列全部落库，其中三列**只有服务端能记录**，也正是留痕真正的价值：
`authorization_received_at`（不可伪造的时刻）、`authorization_ip`、
`authorization_user_agent`。另外三列客户端可以伪造（用户 id 在这个
单管理员系统里恒定、措辞、确认时刻）—— 但仍然要存，
因为留痕要能被**独立理解**，而不是"某个人授权了"。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import Identity
from app.core.config import get_settings
from app.core.crypto import SecretDecryptError, decrypt_secret
from app.core.db import warn_if_pending_changes
from app.core.errors import ApiError, ErrorCode
from app.core.ids import new_id
from app.core.ratelimit import Limiter
from app.core.timeutil import now_utc, seconds_between
from app.core.vocab import (
    ABORTABLE_SCAN_STATUSES,
    SCAN_ORIGIN_CTF2,
    AgentPhase,
    AgentRole,
    AgentStatus,
    FindingStatus,
    ScanStatus,
    TargetKind,
)
from app.events.models import Agent
from app.events.service import append_event
from app.providers.models import ProviderAccount
from app.scans import targets, uploads
from app.scans.models import Scan
from app.scans.reachability import (
    Reachability,
    check_reachability,
    check_tcp_reachability,
)
from app.scans.runner import (
    EXIT_FINDINGS,
    EXIT_OK,
    LaunchResult,
    LaunchSpec,
    RunnerUnavailable,
    can_resume,
    # `artifact` 模式的目标目录（任务 17）
    challenge_dir,
    get_runner,
    terminate_tree,
)
from app.settings import service as settings_service

logger = logging.getLogger("suvex.scans")

# 失败时读取日志尾部的行数。给用户看的 `failureReason` 从这里来 ——
# 而它往往就是唯一能解释"为什么没跑起来"的东西
# （典型例子：strix 的 "Docker unavailable"）。
LOG_TAIL_LINES = 25
FAILURE_REASON_MAX = 600


# ══════════════════════════════════════════════════════════
# 授权
# ══════════════════════════════════════════════════════════


@lru_cache(maxsize=1)
def _create_limiter() -> Limiter:
    """新建扫描的限流器（契约 §3.3，任务 8 补）。

    ★ **为什么扫描创建要限流，而它本来就有并发上限**：
    `max_concurrent_scans` 管的是"同时在跑几个"，超出的会**排队**。
    所以一个脚本疯狂提交并不会被拒绝 —— 它会把队列堆到几千条，
    而每一条都带着一份授权声明进库，等它们慢慢跑完。
    那不是"慢"，那是**把队列变成一个持久化的攻击排程**。

    更实际的一层：每次创建都要 fork 一个子进程（并拉起沙箱容器），
    所以"提交一百次"是实打实的开销，哪怕它们全在排队。

    阈值刻意比登录限流**松**（30/分钟、200/天）：正常用户手点碰不到，
    但脚本刷不出量。登录是防在线爆破（严格），创建是防脚本刷
    （够松才不会误伤"一口气建 5 次扫描"这种真实用法）。

    延迟构造（`lru_cache`）与登录限流同因：模块级常量一旦导入就固定，
    测试里改配置不生效，而那种测试会「永远通过」。
    """
    cfg = get_settings()
    return Limiter(
        [(cfg.scan_create_limit_per_minute, 60), (cfg.scan_create_limit_per_day, 86400)],
        name="scan-create",
    )


def reset_limiters() -> None:
    """清空扫描创建的限流状态。**只给测试用** —— 理由同 auth.reset_limiters。"""
    _create_limiter.cache_clear()


def check_create_rate_limit(identity: Identity, ip: str) -> None:
    """按调用方限流。超限抛 429（带 `Retry-After`）。

    ⚠️ **按 `身份 + IP` 组合做 key，不是只按 IP，也不是只按身份。**

    * 只按 IP：一个 CI 令牌与它所在机器上的所有东西共享配额，
      而反代后面所有请求的 IP 可能是同一个。
    * 只按身份：泄漏的令牌换个来源照样刷 —— 限流形同虚设。
      而需求 5 要防的正是"脚本刷"。

    组合起来两个方向都挡：同一凭据从许多 IP 打（分布式刷）会被身份那半拦住，
    同一 IP 换许多凭据打会被 IP 那半拦住。
    """
    _create_limiter().check(f"{identity.describe()}|{ip}")


def _authorized_by(identity: Identity) -> str | None:
    """把身份压成一个可读的"谁授权"。

    返回 `None` 只在两者都没有时发生（理论上不会：`require_scan` 保证了
    身份非空，而 `Identity` 的两种构造方式必有一个 id）。
    真发生时留 `None` 而不是编一个占位串 —— 一个看起来像 id 的假值
    比一个空缺危险得多。
    """
    if identity.user_id:
        return identity.user_id
    if identity.token_id:
        return f"token:{identity.token_id}"
    return None


def check_authorization(payload, *, identity: Identity, ip: str, user_agent: str | None) -> dict:
    """校验并整理授权留痕。返回要写进 `scans` 的那几列。

    三条规则的响应码**不同**，这是契约 §5.3 定死的：

    | 情况                                | 响应                          |
    | ----------------------------------- | ----------------------------- |
    | 缺 `authorization` 字段             | `403 TARGET_NOT_AUTHORIZED`   |
    | `confirmed != true`                 | `403 TARGET_NOT_AUTHORIZED`   |
    | `statement` 与约定声明**不完全一致** | `400 VALIDATION_FAILED`       |

    为什么"没勾选"是 403 而"措辞不对"是 400：前者是**权限/授权缺失**
    （用户没做那个动作），后者是**请求内容不合法**（用户做了，但送来的东西不对）。
    前端的处理不同 —— 前者要引导用户去勾选，后者要提示重新确认声明。
    """
    settings = get_settings()
    auth = getattr(payload, "authorization", None)
    if auth is None or not auth.confirmed:
        raise ApiError.target_not_authorized()

    expected = settings.scan_authorization_statement
    given = (auth.statement or "").strip()
    if given != expected:
        # ⚠️ **不回声用户输入**（契约 §1.3 第 3 条）。只说期望的是什么。
        # 这条措辞是要用户逐字同意的那句话，所以告诉他正确的原文不算泄密，
        # 反而是他能不能同意的前提。
        raise ApiError.validation(
            "授权声明的措辞与约定不一致，请逐字确认后再提交。",
            {"authorization.statement": f"应为「{expected}」"},
        )

    return {
        # ★ 会话记管理员 id；令牌记 `token:<id>`。
        #
        # 原来写的是 `identity.user_id` —— 而**令牌通道下它是 None**
        # （令牌不绑定某个人，见 auth/deps.py 的 Identity）。于是
        # "哪个 CI 令牌授权对目标发包"完全没有留痕，而机器场景下
        # 这正是最该回答的问题。任务 8 的审计检查发现这一条。
        #
        # 前缀 `token:` 是必需的：不加的话，一个 id 恰好叫 `user_x` 的令牌
        # 与一个 id 为 `user_x` 的管理员在这一列里分不开。
        "authorized_by": _authorized_by(identity),
        "authorization_statement": given,
        "authorization_confirmed_at": auth.confirmedAt,
        "authorization_received_at": now_utc(),
        "authorization_ip": ip,
        "authorization_user_agent": (user_agent or "")[:1000] or None,
    }


# ══════════════════════════════════════════════════════════
# 读
# ══════════════════════════════════════════════════════════


def list_scans(
    db: Session,
    *,
    status: str | None = None,
    q: str | None = None,
    sort: str = "recent",
    direction: str = "desc",
    archived: bool = False,
    deleted: bool = False,
) -> list[Scan]:
    """列表 + 筛选 + 排序（契约 §3.3 的查询参数）。

    排序在 SQL 里做，**不在 Python 里**：`severity` 排序要按严重度权重，
    而那是应用层的映射（`SEVERITY_RANK`），所以用 CASE 表达式下推到数据库。
    在 Python 里排意味着先全表取回来 —— 现在扫描数少看不出来，
    等到几百条时会变成一次慢查询，而那时改起来要重新想一遍分页语义。
    """
    stmt = select(Scan)

    # ★ 三档互斥的可见性（v0.13）。
    #
    # 为什么过滤在**服务端**而不是取回全量再前端筛：
    # 列表页的 tab 计数是 `scans.filter(...).length`（"全部"用的是
    # `scans.length`）。把已归档的混在同一个数组里返回，"全部"就会
    # 把归档的也算进去 —— 那个数字会当场撒谎。
    if deleted:
        # 回收站：只看软删除的
        stmt = stmt.where(Scan.deleted_at.is_not(None))
    elif archived:
        # 已归档 tab：归档且**不在回收站**
        stmt = stmt.where(Scan.archived_at.is_not(None)).where(Scan.deleted_at.is_(None))
    else:
        # 默认视图：既不含归档、也不含回收站
        stmt = stmt.where(Scan.archived_at.is_(None)).where(Scan.deleted_at.is_(None))

    if status:
        stmt = stmt.where(Scan.status == status)
    if q:
        # 目标与名字都搜。`ilike` 在 PostgreSQL 上是大小写不敏感，
        # 而目标里大小写是可能有意义的 —— 所以只做不敏感的**匹配**，
        # 不改变存储的值。
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(Scan.name.ilike(pattern) | Scan.target.ilike(pattern))

    descending = direction != "asc"
    if sort == "duration":
        # ⚠️ NULL 必须垫底（契约 §2.1：running 的 durationSec 是 null）。
        # PostgreSQL 默认 `DESC` 时 NULL 排**最前** —— 那会让"正在跑的"
        # 排到"最慢的"位置，正是契约用 ⚠️ 警告过的那个现象。
        column = Scan.duration_sec
        return _finish(
            db, stmt, column.desc().nullslast() if descending else column.asc().nullslast()
        )

    if sort == "severity":
        # 按最严重的档位排。SQL 表达式下推，不把整表拉进内存 ——
        # 在 Python 里排意味着先全表取回来，现在扫描数少看不出来，
        # 等到几百条时会变成一次慢查询，而那时改起来要重新想一遍分页语义。
        import sqlalchemy as sa

        from app.core.vocab import SEVERITY_RANK

        score = None
        for level, rank in SEVERITY_RANK.items():
            # `findings` 是 JSONB，取出来是文本，必须显式转数字再相乘。
            part = (
                func.coalesce(
                    sa.cast(func.jsonb_extract_path_text(Scan.findings, level), sa.Integer), 0
                )
                * rank
            )
            score = part if score is None else score + part
        assert score is not None
        return _finish(db, stmt, score.desc() if descending else score.asc())

    column = Scan.created_at
    return _finish(db, stmt, column.desc() if descending else column.asc())


def _finish(db: Session, stmt, order):  # noqa: ANN001
    """收尾：加排序键 + **稳定次序**。

    `Scan.id` 兜底是必需的：同一时刻创建的记录（批量提交、时钟精度不足）
    如果只按 `created_at` 排，两行的相对次序在两次查询之间可能不同 ——
    表现是翻页时看到重复项、同时漏掉另一个。
    """
    return list(db.execute(stmt.order_by(order, Scan.id)).scalars())


def get_scan(db: Session, scan_id: str) -> Scan:
    scan = db.get(Scan, scan_id)
    if scan is None:
        raise ApiError.not_found("扫描")
    return scan


# ══════════════════════════════════════════════════════════
# 拒绝把「借来的壳」当成自己的扫描（v0.27）
# ══════════════════════════════════════════════════════════

# 这个码对应的那句中文。**必须给出替代动作**（去 CTF2 卡片点解题）——
# 只说"不能这么做"会让用户站在一个刚刚还能点的按钮前面不知所措。
_CTF2_SHELL_REFUSAL = (
    "这条记录是 CTF2 解题任务的活动记录，不是一次渗透扫描。"
    "它的进度由 CTF2 的解题流程维护 —— 请回「CTF2 题目」页，"
    "在对应的题目卡片上点「解题」或「接着解」。"
)


def require_launchable_scan(db: Session, scan_id: str) -> Scan:
    """取一条扫描，并**拒绝** CTF2 借来挂事件流的壳。

    ## ★ 这个函数存在的唯一原因：一次实测的误操作

    2026-10-08，用户对扫描列表里那条 `[CTF2] WEB5[中等]` 点了「重跑」，
    问"为什么没有开始解"。真相是那个按钮属于**扫描模块**，它忠实复制了
    `target = ctf2:1cbe06be-…` 然后交给 strix —— 而 strix 只收 URL /
    仓库 / 目录 / 域名 / IP，收到这个字符串当场拒绝：

        strix: error: Invalid target 'ctf2:1cbe06be-…'

    于是 argparse 以**用法错误**退出（码 2），而 `EXIT_FINDINGS` **也是 2**
    —— 那条什么都没干的记录被记成「已完成 · 0 秒」。用户点了按钮、
    看到"已完成"、而题一个字没动。

    ## 为什么拦在服务层而不是前端

    前端也会把入口藏掉（`authorizedBy` / `origin` 下发之后），但**两层都要有**：
    API 是可以被直接调的，而"点了一个必然静默失败的按钮"这种体验
    不该只靠"界面没画出来"来避免。

    ## 三个入口都用它

    `rerun_scan`（新建壳）、`resume_scan`（改壳状态）、`abort_scan`（杀壳）。
    终止也得拦：CTF2 的解题**确实**可以被取消，但那是 CTF2 自己的
    「取消」语义（状态回 `unsolved`、不落候选），与扫描侧的 `aborted`
    不是一回事。从扫描列表点「终止扫描」写出来的 `aborted` + "已由用户终止"
    会挂在 CTF2 的活动流上，而**题还在跑** —— 两边说法相反。
    """
    scan = get_scan(db, scan_id)
    if scan.origin == SCAN_ORIGIN_CTF2:
        raise ApiError.conflict(ErrorCode.SCAN_IS_CTF2_SHELL, _CTF2_SHELL_REFUSAL)
    return scan


def count_by_status(db: Session) -> dict[str, int]:
    rows = db.execute(select(Scan.status, func.count()).group_by(Scan.status)).all()
    return {status: count for status, count in rows}


# ══════════════════════════════════════════════════════════
# 创建
# ══════════════════════════════════════════════════════════


def _require_provider(db: Session, provider_id: str) -> ProviderAccount:
    """校验 provider 存在。

    ⚠️ **这里就是 `scans.provider_id` 完整性的保证**（任务 2 移除了那条外键）。
    历史引用的完整性该在**写入那一刻**保证，而不是靠"永远不许删除被引用过的东西"。
    """
    provider = db.get(ProviderAccount, provider_id)
    if provider is None:
        raise ApiError.validation(
            "指定的 provider 不存在，可能已被删除。请重新选择模型。",
            {"providerId": "不存在"},
        )
    return provider


def create_scan(
    db: Session,
    payload,
    *,
    identity: Identity,
    ip: str,
    user_agent: str | None,
) -> tuple[Scan, Reachability]:
    """新建扫描（契约 §3.3）。**初始状态 `queued`。**

    返回 `(scan, reach)` —— 第二个是**目标可达性预检**的结果（任务 11）。
    router 会把其中的 `notice` 放进响应，让界面能提示
    "该目标只在 http 下应答"这类情况。
    """
    _require_provider(db, payload.providerId)

    normalized, check = targets.normalize_and_validate(payload.targetKind, payload.target)
    if not check.ok:
        raise ApiError.validation(
            check.error or "目标不合法",
            {"target": check.error or "目标不合法"},
        )

    # ── `artifact`：目标不是地址，而是**用户交上来的附件**（任务 17）──
    #
    # 走到这里 `check.ok` 已经保证 `payload.target` 是空的
    # （见 `targets._validate_artifact`）。现在要把"目标"这件事落在附件上：
    #
    #   · **没有附件就直接拒绝** —— 而不是建一条注定跑不出东西的扫描。
    #     这类题的全部素材都在附件里，没有附件它连"要分析什么"都不知道。
    #   · 有附件时，`scans.target` 存**第一个附件的文件名**。
    #
    # ★ 为什么存附件名，而不是留空或存一个占位串：
    #
    #   `scans.target` 是列表里那一行的主信息，也是"这是个什么目标"的标识。
    #   留空的话列表里是一条没有主体的记录 —— 用户根本认不出哪条是哪道题，
    #   而这恰恰是他同时开着好几道题时最需要看的一眼。
    #   而附件名（`tryre.zip`）本来就是这道题的身份。
    #
    #   多个附件时只取第一个当代表：它只是**标签**，真正的分析对象是
    #   整个 `challenge/` 目录（全部附件都会铺进去）。
    staged: list[dict] = []
    if payload.targetKind == TargetKind.ARTIFACT:
        staged = uploads.peek_staged(payload.attachmentStagingId)
        if not staged:
            raise ApiError.validation(
                "这类题目要靠附件来分析，请先上传题目文件（zip / ELF / 流量包 / 源码都可以）。",
                {"targetKind": "还没有附件"},
            )
        normalized = staged[0]["name"]

    forensic = check_authorization(payload, identity=identity, ip=ip, user_agent=user_agent)

    # ★ 协议预检（任务 11）。
    #
    # 只对 URL 形态的目标做：repo/path 没有"协议"这回事。
    #
    # 它做两件事，缺一不可：
    #   1. 把**真正能用的地址**交给 strix（只提示不修正的话，扫描照样失败）；
    #   2. 给用户一句话（他填 `example.com` 时心里想的可能是 https，
    #      而真相可能相反）。
    #
    # ⚠️ **必须把用户的原始输入交给它，不能给 `normalized`。**
    #
    #   归一化的定义就是「剥掉协议」（契约 §2.8），所以 `normalized` 里
    #   **永远没有协议** —— 预检的 `has_scheme` 因此恒为假，
    #   「用户显式写了协议就绝不改写」那条规则会**静默失效**。
    #
    #   这个 bug 我在实机上抓到了：显式创建 `https://127.0.0.1:5173`
    #   （一个只应答 http 的目标），预检却回「已按 http 发起扫描」并
    #   把 https 改写掉了 —— 用户说了算的那条规则没了。
    #   而单元测试全绿，因为它们都 monkeypatch 掉了 `check_reachability`。
    #
    # **不阻断创建** —— 探测失败不等于扫描没用（目标可能只在内网可达、
    # 或者此刻正在重启）。见 `reachability` 的 docstring。
    reach = Reachability(None, None, False, False)
    if payload.targetKind == "url":
        try:
            reach = check_reachability(normalized, origin=payload.target)
        except Exception:  # noqa: BLE001
            # 预检失败绝不能拦住创建 —— 它只是"顺手做的"。
            logger.warning("目标可达性预检失败，按原地址发起", exc_info=True)
    elif payload.targetKind == TargetKind.SERVICE:
        # 裸 TCP 没有"该用哪个协议"这一问题，所以探的是端口通不通。
        #
        # ★ 这里**复用 `Reachability` 只为把 `notice` 传出去**（路由只消费
        #   那个字段）。`effective_target` 刻意留空：留空时 `build_spec`
        #   会用 `scan.target`，也就是 `host:port` 本身 —— 那正是我们要给
        #   执行后端的值。补一个 `http://` 反而会把它变成另一个东西。
        found = targets.extract_service(normalized)
        if found is not None:
            try:
                tcp = check_tcp_reachability(*found)
                reach = Reachability(None, tcp.notice, False, False)
            except Exception:  # noqa: BLE001
                logger.warning("TCP 目标预检失败，按原地址发起", exc_info=True)

    name = (payload.name or "").strip() or targets.default_name(payload.targetKind, normalized)
    moment = now_utc()

    # ★ 提前生成 id（任务 17）。
    #
    # `challenge/` 要按 `scan_id` 落盘，而它必须在**入库之前**建好 ——
    # 理由见下面那一段。
    scan_id = new_id("scan")

    # ── 把题目素材铺开（任务 17，仅 `artifact`）────────────
    #
    # ⚠️ 必须在 `db.commit()` **之前**，且失败要拦住整个创建。
    #
    # 因为这个目录就是这次扫描的**目标**。若先提交再铺开，铺开失败时库里
    # 已经有了一条 `queued` 的记录，而调度器会在**毫秒级**认领并启动它
    # （路由里的 `_nudge_scheduler`）—— 那一刻 strix 拿到的是一个不存在的
    # 路径，直接 `Invalid target` 退出。于是用户看到的是「扫描失败」，
    # 而真正的问题是「你上传的附件有问题」—— 两者要排查的方向完全不同。
    #
    # 放在提交前，失败就是一次干净的 400，库里什么都没留下。磁盘上那份
    # 半成品由 `build_challenge` 自己清掉（见它的说明）。
    if payload.targetKind == TargetKind.ARTIFACT:
        produced = uploads.build_challenge(
            scan_id=scan_id, staging_id=payload.attachmentStagingId
        )
        logger.info(
            "扫描 %s 的题目素材已铺开（%d 个文件）：%s",
            scan_id,
            len(produced),
            "、".join(row["name"] for row in produced[:10]),
        )

    scan = Scan(
        id=scan_id,
        name=name[:200],
        # ★ 存**归一化后**的目标（契约 §2.8），**不带协议**。
        #
        # ⚠️ 这里不能存 `reach.effective_target` —— 我第一版就是那么写的，
        #    被 `test_target_is_normalized_before_storage` 当场抓住。
        #
        # 归一化的定义是「剥掉协议」：`https://api.example.com/admin?x=1`
        # → `api.example.com`。而补上 `https://` 会让同一个目标在两次
        # 创建里变成两条看起来不同的记录 —— 那条测试要防的正是这件事。
        #
        # 探测出的地址是**给 strix 用的**，存在 `probed_target`（见下），
        # 不进这个展示字段。
        target=normalized,
        # 预检探测到的**可执行地址**（任务 11）。它是"当时观察到的事实"，
        # 与 `target`（用户填的、归一化后的身份）分开存：
        #   · `target`      —— 给人和列表看，也是"同一目标"的判据
        #   · `probed_target` —— 给 strix 用，可能带着 http:// 或 https://
        # 两列分开，才不会有"补了协议导致同一目标被当成两个"的问题。
        probed_target=reach.effective_target,
        target_kind=payload.targetKind,
        # ★ 初始必须是 queued —— 见模块 docstring。
        status=ScanStatus.QUEUED.value,
        skills=list(payload.skills),
        provider_id=payload.providerId,
        started_at=moment,
        # 终态列此刻必须为空：CHECK 约束会拦住"queued 却有 finished_at"。
        finished_at=None,
        duration_sec=None,
        agent_count=0,
        findings={},
        failure_reason=None,
        progress=None,
        # ── 任务背景与成功判据（任务 12）────────────────
        #
        # ★ `brief` 原样存下来，**不做任何改写**。
        #
        # 理由：它是用户的原话，而 agent 要靠它理解考点（CTF 题面里有
        # 诱饵、有 flag 格式、有提示）。我们转述一遍就会丢信息，
        # 而丢掉的往往正是"哪条线索是假的"这类关键区别。
        #
        # 空串归一成 NULL：契约里 `brief` 是 `string | null`，
        # 而 `""` 与"没填"在前端要渲染成两种不同的话 ——
        # 存空串会让"我没写背景"显示成"背景是空的"，读起来像出了错。
        brief=(payload.brief or "").strip() or None,
        goal=payload.goal,
        **forensic,
    )
    db.add(scan)
    db.commit()
    db.refresh(scan)

    # ── 认领暂存的题目附件（任务 12 修正）────────────────
    #
    # ★ 必须在**提交之后**：`claim` 要把文件移到
    #   `_uploads/<scan_id>/`，而 scan_id 是上面刚生成的。
    #
    # ⚠️ 认领失败**不能**让创建失败：附件丢了是次要问题，
    #   而"扫描建不出来"是主要问题。`claim` 自己对不存在的批次
    #   返回空列表（见它的 docstring），所以这里不需要 try。
    if payload.attachmentStagingId:
        claimed = uploads.claim(payload.attachmentStagingId, scan.id)
        if claimed:
            logger.info(
                "扫描 %s 认领了 %d 个题目附件：%s",
                scan.id,
                len(claimed),
                "、".join(row["name"] for row in claimed),
            )

    logger.info("已创建扫描 %s（目标 %s，状态 queued）", scan.id, scan.target)
    if reach.notice:
        # 记进日志也很有用：排查"为什么这次扫描什么都没扫到"时，
        # 第一件要看的就是"创建时目标到底通不通"。
        logger.warning("扫描 %s 的目标预检：%s", scan.id, reach.notice)
    return scan, reach


def detect_run_id(run_dir: str | Path | None) -> str | None:
    """从运行目录里读出执行后端自己的 run 标识。读不到返回 `None`。

    ## 为什么单独包一层

    `detect_run_id` 是执行后端接口的一部分，而"读不到"是**正常情况**
    （目录还没建出来、被清过、换了后端）。调用方要的是"有没有"，
    不该每处都写一遍 try/except —— 漏掉一处的症状是一次 500，
    而不是一次说得清的 409。

    ## ★ 它补的是一个实测撞到的缺口（2026-09-27）

    服务重启时 `recover_orphans()` 会把还在跑的扫描标成 `failed`，
    而平时负责补录 `run_id` 的 `_reap_finished()` 遍历的是**内存里的
    进程句柄表** —— 重启后那张表是空的，所以那条路径永远轮不到它。

    结果是：磁盘上 `strix_runs/<run_id>/` 里 `run.json` 与
    `.state/agents.json` 一应俱全、完全能续跑，而 `can_resume` 因为
    `run_id` 是 NULL 直接判否 —— 点「续跑」稳定 409，
    错误信息还说"会话文件已不在"，**与事实正好相反**。

    `recover_orphans()` 那边现在也补了（治本）；这里的用途是
    **把已经坏掉的记录救回来** —— 它们是在补丁之前产生的。
    """
    if not run_dir:
        return None
    try:
        return get_runner().detect_run_id(Path(run_dir))
    except Exception:  # noqa: BLE001
        logger.warning("读取运行目录 %s 的 run 标识失败（忽略）", run_dir, exc_info=True)
        return None


def resume_scan(
    db: Session,
    scan_id: str,
    payload,
    *,
    identity: Identity,
    ip: str,
    user_agent: str | None,
) -> Scan:
    """续跑：**在同一条记录上接着跑**（任务 16）。

    ## 与「重跑」的区别 —— 这是两个不同的动作，不能合并

    | | 重跑 `rerun_scan` | 续跑 `resume_scan` |
    |---|---|---|
    | 记录 | **新建一条** | **同一条**（状态回到 `queued`）|
    | 起点 | 全新会话、全新工作目录 | 接着上次的会话与工作目录 |
    | 上次的产出 | 留在旧记录里 | 继续累加在同一份历史里 |

    ## 为什么续跑**不**新建记录

    重跑新建记录是为了保住"那次发现了什么"的历史（见 `rerun_scan` 的说明）。
    而续跑是**同一次扫描的中断与继续**，新建一条会把一份连续的会话历史劈成两条 ——
    而 `activity_events` 的唯一键是 `(scan_id, source_row_id)`：
    同一次 strix run 的事件会在两条记录下**各导一遍**，
    活动流里出现两份一模一样的轨迹。数据模型上就不该那么做。

    ## 前置条件（不满足就 409，且说清是哪一条）

    1. 状态可续：`failed` / `aborted`。正在跑的没什么可续；
       `completed` 的也不在这里 —— 它下面的 agent 都已经终结，
       续上去会立刻又结束（那是"再挖一轮"的另一个功能，不是续跑）。
    2. `run_id` 有值（跑过至少一次）。
    3. strix 的两个状态文件都还在（见 `runner.can_resume`）。

    ## 授权同样要重新确认

    与重跑同理：续跑会**继续对真实目标发包**，而"上次确认过"不该永久授权。
    """
    # ★ v0.27：CTF2 的壳不能续跑 —— 它的"上次会话"根本不存在
    #   （strix 从没跑过它），而这条路径最终会走到 `can_resume` 报
    #   "会话文件已不在"，那句**与事实相反**（事实是压根没跑过）。
    source = require_launchable_scan(db, scan_id)
    if source.deleted_at is not None:
        raise ApiError(409, ErrorCode.SCAN_NOT_RESUMABLE, "这条扫描在回收站里，请先恢复再续跑。")

    if source.status not in (ScanStatus.FAILED.value, ScanStatus.ABORTED.value):
        raise ApiError(
            409,
            ErrorCode.SCAN_NOT_RESUMABLE,
            f"只有「失败」或「已终止」的扫描可以续跑（当前状态：{source.status}）。",
        )

    if not source.run_id:
        # ★ 记录里没有就**从磁盘找回来**（2026-09-27 实测的那个 409）。
        #
        # `run_id` 是续跑的凭据之一，而它有可能压根没被记下来 —— 最典型的是
        # 服务重启把扫描打断：进程句柄在内存里丢了，`recover_orphans()`
        # 直接把它标成 failed，而平时补录 `run_id` 的那段逻辑遍历的正是
        # 那张已经空掉的句柄表（详见 `detect_run_id` 的说明）。
        #
        # 不补的话，用户看到的是一句**与事实相反**的"会话文件已不在"，
        # 而被它挡住的恰恰是最该能续的场景：跑了十几分钟被重启带走。
        #
        # 找回来就**顺手写回记录** —— 否则判据每被问一次就要重扫一遍磁盘，
        # 而且下一次启动依然会认为它没跑过。
        recovered = detect_run_id(source.run_dir)
        if recovered:
            source.run_id = recovered
            db.commit()
            logger.info("扫描 %s 的 run 标识从磁盘补回：%s", source.id, recovered)

    if not can_resume(source.run_dir, source.run_id):
        # 这一句必须给出**替代动作**：用户要的是一个结果，
        # 而"不能续"本身不解决问题，"可以重跑"才解决。
        raise ApiError(
            409,
            ErrorCode.SCAN_NOT_RESUMABLE,
            "这次扫描的 strix 会话文件已不在（可能被清理过），无法续跑。"
            "请改用「重跑」——它会带着原来的任务背景重新开始。",
        )

    _require_provider(db, source.provider_id)  # provider 可能已经被删掉了
    forensic = check_authorization(payload, identity=identity, ip=ip, user_agent=user_agent)

    # ★ **就地**回到排队态，而不是新建记录（见 docstring 的说明）。
    #
    # 清掉的都是"上一次尝试的结论"：失败原因、耗时、结束时刻、pid。
    # `run_id` / `run_dir` 必须**留着** —— 它们正是续跑的凭据。
    moment = now_utc()
    source.status = ScanStatus.QUEUED.value
    source.pid = None
    source.failure_reason = None
    source.finished_at = None
    source.duration_sec = None
    source.progress = None
    source.started_at = moment
    source.exit_code = None
    for key, value in forensic.items():
        setattr(source, key, value)
    db.commit()
    db.refresh(source)
    return source


def rerun_scan(
    db: Session,
    scan_id: str,
    payload,
    *,
    identity: Identity,
    ip: str,
    user_agent: str | None,
) -> Scan:
    """重跑：**基于历史配置建一条新记录**（契约 §3.3 / §6）。

    ⚠️ 两件事必须同时成立，契约里都写了：

    1. **是新建一条记录，不是把旧记录改回 running。** 扫描历史有价值
       （哪次发现了什么、修没修），把已完成的记录改回 running 会把那次历史抹掉。
    2. **同样要求授权确认。** 重跑在语义上是一次新扫描，目标可能已经变了
       （域名易主、IP 重分配）。不能因为"上次确认过"就免掉 ——
       那等于让一次确认永久授权对任意时刻的同名目标发包。

    ## ★ v0.27：CTF2 的壳**不能重跑**（这里就是那次事故的入口）

    实测（2026-10-08）：用户对 `[CTF2] WEB5[中等]` 点重跑，本函数忠实
    复制了 `target = ctf2:1cbe06be-…`，而 strix 拒收这个 target ——
    argparse 退 2、被 `finalize` 记成「已完成 · 0 秒」，CTF2 侧零动作。
    用户点了按钮、看到成功、而题没动。

    拦在这里而不是"让它跑一次然后失败"：那次失败**是假的**
    （退出码 2 与 `EXIT_FINDINGS` 撞车，见 `runner.EXIT_FINDINGS`），
    而且没有任何地方提示用户"你该去 CTF2 页面点解题"。
    """
    source = require_launchable_scan(db, scan_id)
    _require_provider(db, source.provider_id)  # provider 可能已经被删掉了
    forensic = check_authorization(payload, identity=identity, ip=ip, user_agent=user_agent)

    moment = now_utc()
    name = (payload.name or "").strip() or source.name
    scan = Scan(
        id=new_id("scan"),
        name=name[:200],
        target=source.target,
        # 重跑同一个目标：协议结论仍然有效，一并继承。
        # 不继承的话，重跑会退回"让 agent 自己猜协议"，
        # 而那次猜测正是上一次白跑的原因。
        probed_target=source.probed_target,
        target_kind=source.target_kind,
        status=ScanStatus.QUEUED.value,
        skills=list(source.skills or []),
        provider_id=source.provider_id,
        started_at=moment,
        finished_at=None,
        duration_sec=None,
        agent_count=0,
        findings={},
        failure_reason=None,
        progress=None,
        # ★ 重跑**继承任务背景与判据**（任务 12）。
        #
        # 这是重跑最容易被忽略的一处：用户重跑通常是因为"上次没跑出来"
        # 或"目标改版了再看一遍"，而**题面/范围一个字都没变**。
        # 不继承的话，他会得到一次完全没有背景信息的扫描 ——
        # agent 只能盲扫，然后他得手工再填一遍。
        #
        # 与 `probed_target` 同一个道理：重跑同一个目标时，
        # 上一次的结论仍然有效，不该退回"让 agent 自己猜"。
        brief=source.brief,
        goal=source.goal,
        **forensic,
    )
    db.add(scan)
    db.commit()
    db.refresh(scan)
    logger.info("已按 %s 的配置创建重跑扫描 %s", source.id, scan.id)
    return scan


# ══════════════════════════════════════════════════════════
# 启动
# ══════════════════════════════════════════════════════════


def build_spec(db: Session, scan: Scan) -> LaunchSpec:
    """把扫描记录翻译成执行后端能懂的东西。

    这里会**解密 provider 凭据** —— 明文在这个函数里出现，随后进
    `LaunchSpec`、再进子进程的环境变量。它不进日志、不进数据库、不进 argv。
    """
    from app.providers.llm import LITELLM_PREFIXES, resolve_base_url

    provider = db.get(ProviderAccount, scan.provider_id)
    if provider is None:
        raise ApiError.validation("这次扫描选用的 provider 已被删除，无法启动。")

    api_key = ""
    if provider.api_key_encrypted:
        try:
            api_key = decrypt_secret(provider.api_key_encrypted)
        except SecretDecryptError as exc:
            raise ApiError(
                500,
                ErrorCode.PROVIDER_KEY_UNREADABLE,
                f"无法解密 provider「{provider.label}」的凭据，扫描无法启动：{exc}",
            ) from exc

    settings = get_settings()
    run_dir = settings.scan_root / scan.id
    # ★ 执行参数取**设置页覆盖后的生效值**（任务 13）：DB 有值就用 DB，
    #   没有就退回 env。读取放在这一层而不是 runner —— 只有这里拿得到会话。
    #
    #   注意它读的是**扫描启动那一刻**的值：之后用户再改设置，正在跑的
    #   这次不受影响（参数已经进了它的命令行，中途换没有意义）。
    execution = settings_service.effective_execution_settings(db)
    # 沙箱配额走只读读取 —— 启动一次扫描不该凭空建出一行配置。
    sandbox = settings_service.read_sandbox_config(db)

    # ★ 续跑判定（任务 16）：**同一个扫描记录再次启动**就是续跑。
    #
    # 判据只看"有没有 `run_id` + strix 的状态文件还在不在"，这是**无歧义**的：
    #   · 全新扫描创建时 `run_id` 是 None，跑过第一次之后才会被写上；
    #   · **重跑**走 `rerun_scan()` —— 它建的是**新记录**（新 run_dir、
    #     `run_id` 为空），所以那条路仍然是全新扫描；
    #   · 于是"queued 且有 run_id"只可能来自 `resume_scan()`。
    #
    # 状态文件不在时**退回全新扫描**：入口处 `resume_scan()` 已经挡过一次，
    # 走到这里说明它在"排队"与"启动"之间消失了（被手工清理）。
    # 那时报错不如照跑 —— 用户的意图是"把这次跑完"。
    resume_run_id: str | None = None
    if scan.run_id:
        if can_resume(scan.run_dir, scan.run_id):
            resume_run_id = scan.run_id
        else:
            logger.warning(
                "扫描 %s 本应续跑（run_id=%s），但 strix 状态文件已不在，改为全新扫描",
                scan.id,
                scan.run_id,
            )

    # ── 目标：`artifact` 换成解压出来的目录（任务 17）──────
    #
    # ★ 这一段就是「附件变成目标」的**全部**实现 —— 也是执行层零改动的原因。
    #
    # strix 对单个文件会直接拒绝（`Path exists but is not a directory`），
    # 所以必须给一个**目录**。给完之后它自己会推断成 `local_code` 并挂进
    # 沙箱 —— 与用户手填一条「本地代码路径」**完全同一条路**。
    #
    # ⚠️ `target_kind` 也一并报成 `"path"`，不是 `"artifact"`。
    #
    #   runner 那边据此往指令里加一句"目标是一个本地代码目录，属于白盒测试"。
    #   若照实报 `"artifact"`，那句说明就不会出现（runner 只认它认识的那几个），
    #   而 agent 会以为自己在测一个网站 —— 又一轮白跑。
    #
    #   我们也不可能去教 strix 认一个新类型：为 `service` 写一段纠正指令
    #   是因为它的类型系统里**没有** TCP 这一类；而这里 strix 有现成的
    #   `local_code`，只是**由谁来准备那个目录**不同而已。所以对我们来说
    #   `artifact` 是**建扫描时**的形态，不是**执行时**的形态。
    if scan.target_kind == TargetKind.ARTIFACT:
        directory = challenge_dir(scan.id)
        if not directory.is_dir():
            # 走到这里说明目录在"建好"与"启动"之间消失了（被手工清过）。
            # 报错而不是照跑：照跑的话 strix 会以 `Invalid target` 退出，
            # 而那句话读起来像"目标填错了"，与真正的原因差得很远。
            raise ApiError.validation(
                "题目素材目录已不在（可能被手工清理过），这次扫描无法启动。"
                "请用「重跑」——它会从保存的附件重新铺一次。",
                {"targetKind": TargetKind.ARTIFACT.value},
            )
        target = str(directory)
        target_kind = TargetKind.PATH.value
    else:
        # ★ 交给 strix 的地址用**预检探测到的**那个（任务 11）。
        #
        # 这正是那次 32 分钟白跑的解药：用户填的目标没带协议，
        # agent 自己补了 https，而靶机只监听 http，于是每个请求
        # 都撞在 TLS 握手上。预检结果让我们把**真的通的那个**给它。
        #
        # 回退到 `scan.target` 是必要的：非 URL 目标、或探测整体失败时
        # `probed_target` 是 NULL。
        target = scan.probed_target or scan.target
        target_kind = scan.target_kind

    return LaunchSpec(
        scan_id=scan.id,
        target=target,
        target_kind=target_kind,
        skills=tuple(scan.skills or []),
        # 任务背景与成功判据（任务 12）。交付给 runner 拼进 instruction。
        brief=scan.brief,
        goal=scan.goal,
        scan_mode=execution.scan_mode,
        scan_max_turns=execution.scan_max_turns,
        # 沙箱容器配额（任务 14）。没有配置行时全部留空 ——
        # 那等于不下发环境变量，也就是 strix/docker 的默认（不限额）。
        sandbox_cpus=sandbox.cpu_limit if sandbox else None,
        sandbox_mem_limit_mb=sandbox.memory_limit_mb if sandbox else None,
        sandbox_network_isolated=bool(sandbox.network_isolated) if sandbox else False,
        # ★ v0.42：越权与注入手法块。与上面三项同源（都来自那张单例行），
        # 但这一项**没有 env 兜底** —— 它是行为开关，没配置行时就是"关"。
        enable_offensive_prompt=bool(sandbox.enable_offensive_prompt) if sandbox else False,
        # 续跑（任务 16）。非空时 `command()` 会改传 `--resume` 而不传
        # `--target` —— 两者互斥，见 runner 的说明。
        resume_run_id=resume_run_id,
        run_dir=run_dir,
        log_path=run_dir / "scan.log",
        # LiteLLM 格式的模型名。前缀按 provider 品牌推断，默认走 openai 兼容
        # —— 大多数网关（含自建）都是那个形状。
        llm_model=f"{_litellm_prefix(provider.label, LITELLM_PREFIXES)}/{provider.model}",
        llm_api_key=api_key,
        llm_api_base=resolve_base_url(provider.base_url, provider.label),
    )


def _litellm_prefix(label: str, table: dict[str, str]) -> str:
    return table.get((label or "").strip().lower(), "openai")


def launch_scan(db: Session, scan: Scan) -> LaunchResult | None:
    """真的启动一次扫描。返回启动结果（含 Popen 句柄），失败返回 None。

    调用前提：`scan.status` 已经是 `running` 且**已提交**。
    先落库再启动（而不是反过来）是刻意的：反过来的话，
    进程起来了但状态还没落库，`abort` 会找不到它 —— 而那是一个
    **无法终止的攻击进程**。
    """
    runner = get_runner()
    try:
        spec = build_spec(db, scan)
    except ApiError as exc:
        finalize(db, scan, exit_code=None, reason=exc.message, status=ScanStatus.FAILED.value)
        return None

    try:
        result = runner.launch(spec)
    except RunnerUnavailable as exc:
        finalize(db, scan, exit_code=None, reason=str(exc), status=ScanStatus.FAILED.value)
        return None

    logger.info("扫描 %s 已启动 pid=%s", scan.id, result.pid)
    scan.pid = result.pid
    scan.runner = runner.name
    scan.run_dir = str(result.run_dir)
    scan.log_path = str(result.log_path)
    db.commit()

    # ★ 竞态防护：**启动之后再看一眼状态**。
    #
    # 调度器与 abort 是两个并发的执行者，中间那个窗口是这样：
    #   调度器：置 running 并提交 …………………… 启动进程 ……… 记 pid
    #   abort：            ↑ 在这个窗口里读到的 pid 还是 None，杀不掉任何东西
    # 于是扫描记录已经变成"已终止"，而进程还在真的打目标。
    #
    # 所以启动完必须复查一次；已经不是 running 就立刻把进程收掉。
    db.refresh(scan)
    if scan.status != ScanStatus.RUNNING.value:
        logger.warning("扫描 %s 在启动过程中已被置为 %s，立即终止其进程", scan.id, scan.status)
        terminate_tree(result.pid, grace_seconds=5.0, log=logger.warning)
        return None

    return result


def finalize(
    db: Session,
    scan: Scan,
    *,
    exit_code: int | None,
    status: str | None = None,
    reason: str | None = None,
    flag: str | None = None,
    strix_ran: bool = True,
) -> None:
    """把一次扫描收尾：定终态、写时间戳、发事件。

    只对 `running` 的扫描生效 —— `aborted` 的扫描进程退出时也会走到这里，
    而那时状态已经定好了，不能覆盖（否则"用户终止的"会变成"失败的"）。

    ## ★ `strix_ran`：退出码 `2` 的二义性必须由调用方消歧

    `exit_code == 2` 有两个**完全不同**的来源（见 `runner` 模块 docstring）：

    | 来源 | 含义 | 该记成 |
    | ---- | ---- | ------ |
    | strix 跑完并发现了漏洞（`EXIT_FINDINGS`） | 成功 | `completed` |
    | argparse 用法错误（如 target 非法） | **什么都没干** | `failed` |

    两者退出码相同、进程相同，**光看退出码分不开**。所以判据来自另一个
    维度：`strix_runs/` 目录存不存在（`runner.did_strix_run`）。

    ⚠️ 为什么由**调用方**传进来，而不是在这里 `Path()` 一下自己看：
    本函数是在一个**已经开着的事务**里被调的，读文件系统会让持锁窗口
    跨越 I/O —— 实测撞过 `DeadlockDetected`（见 `_failure_reason` 上方
    以及 `scheduler._flag_of` 里那段）。这条约束与 `flag` 参数完全一致，
    所以两者的形状也保持一致。

    默认 `True` 是为了**不改变既有调用点**的语义：其余路径
    （`run_scan` 启动前抛错的那些）根本没到"进程退出"这一步，
    它们都显式传了 `status=FAILED`，走不到退出码分支。
    """
    if scan.status != ScanStatus.RUNNING.value:
        return

    moment = now_utc()
    if status is None:
        # 退出码 → 终态。**2 是成功**（发现了漏洞），不是失败。
        #
        # ★ v0.27：但"2"必须**同时**满足"strix 真的跑过"才算成功 ——
        #   argparse 的用法错误也是 2，而那条路径上什么都没发生。
        #   不加这个条件的话，一条 target 非法的扫描会被记成
        #   「已完成 · 0 秒」，用户看到成功而目标压根没被碰过。
        found_findings = exit_code == EXIT_FINDINGS and strix_ran
        status = (
            ScanStatus.COMPLETED.value
            if exit_code == EXIT_OK or found_findings
            else ScanStatus.FAILED.value
        )
        if exit_code == EXIT_FINDINGS and not strix_ran:
            # 出声：这是一个**被纠正过的谎报**，它值得在日志里留痕 ——
            # 否则"为什么这条 failed"在事后只能靠复现来回答。
            logger.warning(
                "扫描 %s 退出码 2 但 strix 从未启动（无 strix_runs），"
                "按失败处理 —— 这通常是命令行用法错误（如目标格式非法）",
                scan.id,
            )

    if status == ScanStatus.FAILED.value and not reason:
        reason = _failure_reason(scan, exit_code, strix_ran=strix_ran)

    # ── CTF 夺旗的结果（任务 18）──────────────────────────
    #
    # ★ 值是**调用方算好传进来的**，不在这里读磁盘。
    #
    #   原因很具体：这个函数是在一个**已经开着的事务**里被调的
    #   （调用方先 `session.get()`，中间还会写事件与漏洞记录）。
    #   在这里读文件会让持锁窗口跨越一次磁盘 I/O —— 实测撞过一次
    #   `DeadlockDetected`，而**关掉抽取就再也复现不出来**。
    #
    #   抽取因此挪到了 `scheduler._reap_finished` 里、**开事务之前**
    #   （见那里的 `_flag_of`）。
    #
    # ⚠️ 只写非空值，**不写空串**：界面靠"NULL 还是有值"区分
    #   "没有"与"拿到了"，多一个空串状态只会让渲染多一条没处理的分支。
    if flag:
        scan.flag = flag
        logger.info("扫描 %s 的 flag 已记录", scan.id)

    scan.status = status
    scan.finished_at = moment
    scan.duration_sec = seconds_between(scan.started_at, moment)
    scan.exit_code = exit_code
    scan.failure_reason = reason
    # 终态不再有"进度"这个概念 —— 留着它会让界面渲染一个已经结束的进度条。
    scan.progress = None
    db.commit()
    db.refresh(scan)

    logger.info(
        "扫描 %s 结束：status=%s exit=%s 用时=%ss",
        scan.id,
        status,
        exit_code,
        scan.duration_sec,
    )
    emit_lifecycle_event(
        db,
        scan,
        event_type="completed" if status == ScanStatus.COMPLETED.value else "failed",
        summary=_terminal_summary(scan, status, reason),
        detail=(
            {"kind": "completed", "result": reason or "扫描完成"}
            if status == ScanStatus.COMPLETED.value
            else {"kind": "failed", "attempted": f"扫描 {scan.target}", "error": reason or "未知原因"}
        ),
        phase=AgentPhase.REPORT,
    )

    # ★ 终态是汇总列最后一次被改动的机会。
    #
    # 正常的收尾路径上调度器刚刚补读过事件与漏洞
    # （`_import_for(backfill=True)` → `refresh_scan_rollup`），这里再刷一次
    # 是幂等的。它保的是**异常路径**：进程起不来、启动前就抛错的那类失败
    # 不会走到导入，`agentCount` 就会停在建行时的 0 ——
    # 而那时编排器 agent 已经存在了，报表上的 0 是假的。
    refresh_scan_rollup(db, scan)


def _terminal_summary(scan: Scan, status: str, reason: str | None) -> str:
    if status == ScanStatus.COMPLETED.value:
        return f"扫描完成，用时 {scan.duration_sec} 秒"
    return f"扫描失败：{(reason or '未知原因')[:120]}"


def _failure_reason(scan: Scan, exit_code: int | None, *, strix_ran: bool = True) -> str:
    """失败原因：**先把真正解释失败的那句话放在最前**，再附日志尾部。

    ## 为什么不能只给日志尾部（原来的做法）

    实测的失败形态是**带重试的 Python 堆栈**：供应商返回 403 之后
    strix 把同一个异常重试了几次，于是日志末尾 25 行全是重复的栈帧

        File "/usr/lib/python3.13/asyncio/tasks.py", line 507, in wait_for
        File ".../openai_chatcompletions.py", line 399, in stream_response

    而唯一有信息量的那一行 ——

        openai.PermissionDeniedError: Error code: 403 - ... insufficient_user_quota

    —— 在几百行之前，**正好落在 `FAILURE_REASON_MAX` 的截断之外**。

    于是用户看到的是"基础设施中断 + 一堆 Python 文件名"，界面还给了个
    展开按钮让他读栈帧。而他真正需要知道的是"**去给模型账户充值**"。
    这不是排版问题：**堆栈把可行动的信息换成了不可行动的信息。**

    ## ★ v0.27：`strix_ran=False` 时，日志尾部**本身就是误导**

    那种情况下日志只有 1003 字节、内容就是 argparse 的用法错误 + 一屏
    `usage:` 参数表。把它原样贴上去，用户看到的是"一长串参数说明"，
    而**真正要做的事**（把 target 改成合法形态）会被淹掉。
    所以这一支走**已知失败**的同一套做法：先说人话，再附原文。
    """
    if not strix_ran:
        # 已知失败的一种，而且成因在**日志里那行 error** 已经写清了。
        # 这里不猜具体是哪一种用法错误（target 非法 / 缺参数 / 未知选项），
        # 而是把那行**原文**提到最前 —— 它本来就是 strix 写给用户看的。
        detail = _find_argparse_error(scan.log_path)
        head = (
            "strix 没能启动（命令行用法错误），这次扫描**没有对目标做任何事**。"
            + (f"\n\n{detail}" if detail else "")
        )
        return head[:FAILURE_REASON_MAX]

    known = _find_failure_line(scan.log_path)
    tail = read_log_tail(scan.log_path)

    if known and tail:
        return (known + "\n\n—— 日志末尾 ——\n" + tail)[:FAILURE_REASON_MAX]
    if known:
        return known[:FAILURE_REASON_MAX]
    if tail:
        return tail[:FAILURE_REASON_MAX]
    code = "未知" if exit_code is None else str(exit_code)
    return f"扫描进程以退出码 {code} 结束，且没有留下日志。请检查执行后端配置。"


# argparse 的报错行：`strix: error: Invalid target '…'`。
#
# ⚠️ 与 `_ERROR_LINE` 分开写，因为那一条要求形如 `XxxError: 说明`，
# 而 argparse 的这行是 `程序名: error: 说明` —— 前缀是**程序名**不是异常类名，
# 正则匹配不到。
_ARGPARSE_ERROR = re.compile(r"^.*?:\s*error:\s*(?P<msg>\S.*)$", re.MULTILINE)


def _find_argparse_error(log_path: str | None) -> str | None:
    """从日志里取出 strix 自己给出的那句用法错误。找不到返回 None。

    取**第一**处匹配：argparse 把它打在 `usage:` 之前，那是整段报错里
    唯一有信息量的一行（后面全是参数表）。
    """
    if not log_path:
        return None
    path = Path(log_path)
    if not path.is_file():
        return None
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return None
    match = _ARGPARSE_ERROR.search(head)
    return match.group(0).strip() if match else None


# 找错误行时最多回溯多少字节。日志可能有几十 MB（agent 会打很多），
# 全读进来会把内存打满；而错误行**总是在靠近末尾的地方** ——
# 它是导致进程退出的那一个，重试只会把它重复几遍，不会推到很前面。
FAILURE_SCAN_BYTES = 262_144

# 一行 `某某Error: 具体说明`。排除 `Traceback (most recent call last):`
# 这类只有结构、没有信息的行。
_ERROR_LINE = re.compile(
    r"^(?!Traceback)(?P<exc>[A-Za-z_][\w.]*(?:Error|Exception)):\s*(?P<msg>\S.*)$",
    re.MULTILINE,
)

# 已知的、用户**有明确下一步动作**的错误 → 人话说明。
#
# ★ 这不是"美化错误信息"。面对一个 403 的 Python 堆栈，用户唯一能得出的
#   结论是"它坏了"；而真相是他该去充值。那句话虽然就在日志里，却埋在一堆
#   重试栈帧里、且被字数上限截掉 —— **我们已经知道这类错误长什么样，
#   知道就该说出来**，让用户去读栈帧是把我们的活推给他。
#
# 判据用「错误码 + 关键短语」，不用模糊匹配：宁可漏判（退回显示原文），
# 也不要错判成一个"看起来很懂"的类别 —— 那会让用户去修一个不存在的问题。
_KNOWN_FAILURES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"insufficient_user_quota|预扣费额度|剩余额度"),
        "模型服务额度不足（供应商返回 insufficient_user_quota）。"
        "扫描在被拒的请求上重试若干次后退出 —— **这不是目标或沙箱的问题**。"
        "请为该供应商的 API key 充值，或在设置里换一个仍有余额的供应商。",
    ),
    (
        re.compile(r"invalid_api_key|incorrect api key|no auth credentials", re.IGNORECASE),
        "模型服务鉴权失败。请检查该供应商的 API key 是否有效、是否已被吊销。",
    ),
    (
        re.compile(r"rate.?limit|\b429\b", re.IGNORECASE),
        "模型服务限流（429）。稍后重试，或把设置里的「同时运行的扫描数」调小。",
    ),
)


def _find_failure_line(log_path: str | None) -> str | None:
    """在日志里找出真正解释失败的那一行。找不到返回 None。

    先匹配**已知错误**（有明确下一步动作的那些），再退回"最后一行
    `某某Error: 说明`"。都没有就返回 None，由调用方退回日志末尾。

    ⚠️ 与 `read_log_tail` 一样必须**过脱敏**：这一行会进数据库、
    进 API 响应、渲染到界面上，而供应商的错误消息里有时会回显请求内容。
    """
    if not log_path:
        return None
    path = Path(log_path)
    if not path.is_file():
        return None

    from app.core.crypto import redact

    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            block = min(size, FAILURE_SCAN_BYTES)
            handle.seek(size - block)
            raw = handle.read(block)
    except OSError:
        return None

    text = raw.decode("utf-8", "replace")
    # 截断处可能是半行，丢掉它
    if block < size and "\n" in text:
        text = text.split("\n", 1)[1]

    for pattern, message in _KNOWN_FAILURES:
        if pattern.search(text):
            return message

    found = _ERROR_LINE.findall(text)
    if not found:
        return None
    # 取**最后一条**：同一次失败会被重试重复打印，最后一条是真正终止它的那个。
    exc, msg = found[-1]
    return f"{exc}: {redact(msg).strip()}"


def read_log_tail(log_path: str | None, *, lines: int = LOG_TAIL_LINES) -> str:
    """读日志尾部若干行，**过一遍脱敏**。

    ⚠️ 脱敏不是可选的：strix 的日志里可能出现它自己回显的配置
    （包括 `LLM_API_KEY` —— 有些工具在报错时会把收到的配置打出来）。
    而 `failureReason` 会进数据库、进 API 响应、渲染到界面上。
    那是一条完整的泄漏路径，所以这里必须过滤。
    """
    if not log_path:
        return ""
    path = Path(log_path)
    if not path.is_file():
        return ""

    from app.core.crypto import redact

    try:
        # 只读尾部：日志可能有几十 MB，全读进来会把内存打满。
        # 按 64KB 的块从尾巴倒着读，够 25 行就行。
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            block = min(size, 65536)
            handle.seek(size - block)
            raw = handle.read(block)
    except OSError:
        return ""

    text = raw.decode("utf-8", "replace")
    # 截断掉的第一个整行可能是半行，丢掉它
    if block < size and "\n" in text:
        text = text.split("\n", 1)[1]
    tail = [line for line in text.splitlines() if line.strip()][-lines:]
    return redact("\n".join(tail)).strip()


# ══════════════════════════════════════════════════════════
# 终止
# ══════════════════════════════════════════════════════════


def abort_scan(db: Session, scan_id: str, *, reason: str = "已由用户终止") -> Scan:
    """终止一次扫描。**只允许 queued / running**（契约 §3.3）。

    ★ 这里有两处必须做对，否则会出现"记录说终止了、进程还在打目标"：

    1. **状态更新是条件式的**（`WHERE status IN (...)`）。
       先读后写会有窗口：调度器在中间把它置成 running 并启动，
       而我们基于旧状态写了 aborted —— 两边都"成功"，结果不一致。
       用带条件的 UPDATE + 检查影响行数，这个窗口就没了。
    2. **`queued` 的直接置终态**（不用杀进程，调度器不会再认领它，
       因为它只认领 `queued`）。`running` 的才需要真的杀。

    ## ★ v0.27：CTF2 的壳不能从这里终止

    CTF2 的解题**确实**可以被取消，但那是 CTF2 自己的「取消」语义：
    状态回 `unsolved`、那一轮跑完不落候选（见 `ctf2.service.cancel_challenge`）。

    从扫描列表点「终止扫描」会写出一对**与事实相反**的记录：
    扫描侧记 `aborted` + "已由用户终止"，而 CTF2 那边的 worker
    **还在跑**（它是线程，杀不掉，见 `ctf2/scheduler.py` 的说明）。
    于是同一件事在两个页面上有两种说法，而活动流里还多一条
    "扫描已终止"的假事件。
    """
    scan = require_launchable_scan(db, scan_id)
    if scan.status not in ABORTABLE_SCAN_STATUSES:
        raise ApiError.conflict(
            ErrorCode.SCAN_NOT_ABORTABLE,
            f"这次扫描已经是「{_status_label(scan.status)}」，无法终止。",
        )

    was_running = scan.status == ScanStatus.RUNNING.value
    pid = scan.pid
    moment = now_utc()

    scan.status = ScanStatus.ABORTED.value
    scan.finished_at = moment
    scan.duration_sec = seconds_between(scan.started_at, moment)
    scan.failure_reason = reason
    scan.progress = None
    # ★ 「终止方」（任务 11 修正）。
    #
    # 这个字段一直存在于模型与契约里、界面也在显示，但**从来没有任何
    # 代码写过它** —— 于是每一次终止的「终止方」都是空的，那一列
    # 永远显示"——"。实测时就是这样：一次真扫描被中断，
    # 界面上看不出是用户停的还是服务停的。
    #
    # 走 `abort_scan` 这条路径的一定是**用户**（它由
    # `POST /scans/{id}/abort` 触发），所以这里写 `user`。
    scan.terminated_by = "user"
    db.commit()
    db.refresh(scan)

    # 状态先落库、再杀进程。反过来的话，进程死了而状态还没写，
    # 调度器的收尾逻辑会把它判成 failed（退出码非 0）——
    # 于是"用户终止的"变成了"失败的"，而那是两件不同的事。
    if was_running and pid:
        gone = terminate_tree(pid, grace_seconds=get_settings().scan_abort_grace_seconds, log=logger.warning)
        if not gone:
            # 杀不掉要如实说。谎报"已终止"会让用户以为目标不再被打了。
            logger.error("扫描 %s 的进程 %s 未能终止", scan.id, pid)
    scan.pid = None
    db.commit()

    emit_lifecycle_event(
        db,
        scan,
        event_type="blocked",
        summary=f"扫描已终止：{reason}",
        detail={"kind": "blocked", "attempted": f"继续扫描 {scan.target}", "policy": "用户终止", "reason": reason},
        phase=AgentPhase.REPORT,
    )
    logger.info("扫描 %s 已终止", scan.id)
    return scan


def _status_label(status: str) -> str:
    return {
        ScanStatus.QUEUED.value: "排队中",
        ScanStatus.RUNNING.value: "运行中",
        ScanStatus.COMPLETED.value: "已完成",
        ScanStatus.FAILED.value: "失败",
        ScanStatus.ABORTED.value: "已终止",
    }.get(status, status)


# ══════════════════════════════════════════════════════════
# 事件桥
# ══════════════════════════════════════════════════════════


def ensure_runner_agent(db: Session, scan: Scan) -> Agent:
    """拿到（或建出）这次扫描的"编排器 agent"。

    为什么需要一个 agent：`activity_events.agent_id` 是**非空**的
    （前端 `ActivityEvent.agentId` 声明是 `string`）。
    而扫描的生命周期事件（启动/完成/失败/终止）不属于 strix 内部的任何
    agent —— 它们属于**我们这一层**。所以给它一个自己的 agent 行，
    角色用 `reporter`（负责收尾与产出结论的那个角色）。
    """
    existing = db.execute(
        select(Agent).where(Agent.scan_id == scan.id, Agent.role == AgentRole.REPORTER.value)
    ).scalars().first()
    if existing is not None:
        return existing

    agent = Agent(
        id=new_id("agent"),
        scan_id=scan.id,
        role=AgentRole.REPORTER.value,
        status=AgentStatus.RUNNING.value,
        target=scan.target,
        event_count=0,
        last_active_at=now_utc(),
    )
    db.add(agent)
    db.flush()
    return agent


def emit_lifecycle_event(
    db: Session,
    scan: Scan,
    *,
    event_type: str,
    summary: str,
    detail: dict | None,
    phase: str,
) -> None:
    """把一次状态变更写成事件 —— Scans 页与活动流都靠它。

    ⚠️ **事件本身失败不能让扫描失败。** 它是可观测性，不是业务。
    所以这里吞掉异常并记录，而不是让它冒泡到状态机里 ——
    一个"因为写不进事件所以扫描起不来"的系统，比少一条事件糟得多。

    ⚠️ **调用方必须先 commit 过。** 与 `audit.record()` 同一条契约：
    本函数自己 `db.commit()`，而 `Session.rollback()` 是**会话级**的，
    失败时会把调用方**未提交**的业务改动一起吞掉。
    3 个调用点（`scans/service.py` 两处 + `scans/scheduler.py:805`）都先 commit 过；
    这个约定以前只写在文档里，现在 `warn_if_pending_changes` 会让违约留下 ERROR 日志。
    """
    pending = warn_if_pending_changes(
        db, where=f"emit_lifecycle_event(scan={scan.id}, type={event_type})"
    )
    try:
        agent = ensure_runner_agent(db, scan)
        append_event(
            db,
            scan_id=scan.id,
            agent_id=agent.id,
            agent_phase=phase,
            type=event_type,
            summary=summary,
            detail=detail,
        )
        agent.event_count = int(agent.event_count or 0) + 1
        agent.last_active_at = now_utc()
        if scan.status in (ScanStatus.COMPLETED.value, ScanStatus.ABORTED.value):
            agent.status = AgentStatus.DONE.value
        elif scan.status == ScanStatus.FAILED.value:
            agent.status = AgentStatus.FAILED.value
        else:
            agent.status = AgentStatus.RUNNING.value
        if pending:
            # ⚠️ 契约被违反：既不 commit（会替别人定事务边界）
            # 也不 rollback（失败时会吞掉别人的改动）—— 只退到保存点。
            with db.begin_nested():
                db.flush()
        else:
            db.commit()
    except Exception:  # noqa: BLE001 - 见 docstring
        if not pending:
            db.rollback()
        logger.exception("为扫描 %s 写事件失败（已忽略）", scan.id)


def findings_counts(db: Session, scan_id: str) -> dict[str, int]:
    """按严重度统计这次扫描的发现数。

    `Partial` 语义：**没有发现某一级别时该键不存在**，不是 0（契约 §2.1）。
    """
    from app.findings.models import Finding

    rows = db.execute(
        select(Finding.severity, func.count())
        .where(Finding.scan_id == scan_id)
        .where(Finding.status != FindingStatus.FALSE_POSITIVE.value)
        .group_by(Finding.severity)
    ).all()
    return {severity: count for severity, count in rows}


def count_scan_agents(db: Session, scan_id: str) -> int:
    """数这条 scan 上挂着几个 agent。

    ## ★ 抽出来是因为有两个调用方，而这个数不能有两个口径

    `refresh_scan_rollup`（收尾/漏洞变动时刷整行）和
    `ctf2.events.ensure_solver_agent`（**解题进行中**新建 solver 时）都
    需要它。抄一份的后果是"扫描列表里的智能体数"与"智能体页能看到的"
    在某个时刻对不上 —— 而那个数正是用户判断"它到底动没动"的依据。

    ## 为什么不做成 `refresh_scan_rollup` 的一部分

    那个函数会 `db.commit()`（见它自己的说明与 `app/core/db.py` 里那条
    "内层函数替外层定事务边界"的告诫）。解题进行中的调用点在 `_emit` 里，
    正处于"写题目状态、写事件"的同一个事务中间 —— 在那里 commit 会把
    调用方未提交的半截工作提前落地。所以这里只算数、不写库、不提交，
    由调用方决定放进哪个事务。
    """
    return int(
        db.execute(
            select(func.count()).select_from(Agent).where(Agent.scan_id == scan_id)
        ).scalar_one()
    )


def refresh_scan_rollup(db: Session, scan: Scan) -> None:
    """把 findings 计数与 agent 数刷进扫描行。

    单独一个函数而不是散在收尾里，是因为任务 4（漏洞）落地之后，
    每次漏洞变动都要重新刷这两个数 —— 口径只有一处才不会有第二个版本。
    """
    scan.findings = findings_counts(db, scan.id)
    scan.agent_count = count_scan_agents(db, scan.id)
    db.commit()


# ══════════════════════════════════════════════════════════
# 归档 / 软删除（v0.13，任务 9）
# ══════════════════════════════════════════════════════════


def _load_for_batch(db: Session, ids: list[str]) -> list[Scan]:
    """批量取扫描。缺 id 时**一次性说清**哪些找不到。

    与 `findings.change_status` 同一条规矩：逐个报错会让用户在批量操作里
    一条条排查，而批量可能一次提交几十条。
    """
    scans = list(db.execute(select(Scan).where(Scan.id.in_(ids))).scalars())
    found = {scan.id for scan in scans}
    missing = [item for item in ids if item not in found]
    if missing:
        raise ApiError.not_found(f"其中 {len(missing)} 次扫描记录（{missing[0]} 等）")
    return scans


def batch_set_archived(
    db: Session, *, ids: list[str], archived: bool
) -> tuple[list[Scan], int]:
    """批量归档 / 取消归档。返回 `(changed, skipped)`。

    **幂等**：已经是目标状态的算 skipped，不重复写 `archived_at`
    （重写会把"当初什么时候归档的"改成"我刚才点了两下"，
    而那条时间正是保留期与事后解释的依据）。

    ⚠️ 不校验 status：归档是与 status **正交**的维度 ——
    completed / failed / aborted 都能归档。见 `models.Scan.archived_at`。
    """
    scans = _load_for_batch(db, ids)
    moment = now_utc()
    changed: list[Scan] = []
    skipped = 0

    for scan in scans:
        if archived:
            if scan.archived_at is not None:
                skipped += 1
                continue
            scan.archived_at = moment
        else:
            if scan.archived_at is None:
                skipped += 1
                continue
            scan.archived_at = None
        scan.updated_at = moment
        changed.append(scan)

    db.commit()
    for scan in changed:
        db.refresh(scan)
    return changed, skipped


def batch_set_deleted(db: Session, *, ids: list[str]) -> tuple[list[Scan], int]:
    """批量**软删除**（进回收站）。返回 `(changed, skipped)`。

    ★ 这里**不物理删除任何东西**。物理删除只在两处发生：
      ① `purge_scan`（用户在回收站里点"永久删除"）；
      ② 保留期到期后的自动清理（`sweep_retention`）。

    用户在列表上点"删除"时数据不丢，是"误删不会立刻丢数据"这条承诺的全部实现。
    """
    scans = _load_for_batch(db, ids)
    moment = now_utc()
    changed: list[Scan] = []
    skipped = 0

    for scan in scans:
        if scan.deleted_at is not None:
            skipped += 1
            continue
        scan.deleted_at = moment
        scan.updated_at = moment
        changed.append(scan)

    db.commit()
    for scan in changed:
        db.refresh(scan)
    return changed, skipped


def batch_restore(db: Session, *, ids: list[str]) -> tuple[list[Scan], int]:
    """从回收站恢复。返回 `(changed, skipped)`。

    ⚠️ **不碰 `archived_at`**：归档状态是删除之前的样子，恢复要还原它，
    而不是顺手取消归档 —— 那会让"恢复"变成"恢复 + 取消归档"两件事，
    而用户只要求了前者。
    """
    scans = _load_for_batch(db, ids)
    moment = now_utc()
    changed: list[Scan] = []
    skipped = 0

    for scan in scans:
        if scan.deleted_at is None:
            skipped += 1
            continue
        scan.deleted_at = None
        scan.updated_at = moment
        changed.append(scan)

    db.commit()
    for scan in changed:
        db.refresh(scan)
    return changed, skipped


def reports_referencing(db: Session, scan_id: str) -> list[str]:
    """哪些报告的冻结内容引用了这次扫描的漏洞。返回**报告编号**列表。

    ★ 这个函数存在的原因是一个真实的破坏路径：

    `report_findings.finding_id → findings ON DELETE CASCADE`，
    而 `findings.scan_id → scans ON DELETE CASCADE`。所以物理删除一次扫描
    会**连带删掉它的漏洞**，进而删掉报告与那些漏洞的关联行 ——
    报告的 `findingIds` 还在，但它们指向的东西没了，那份报告就被掏空了。

    而报告是**交付物**（有编号、客户拿去对账）。让它在 30 天后
    因为有人清了回收站而悄悄变空，是不可接受的。所以：
    **有报告引用时既不能永久删除，也不能被自动清理掉。**
    """
    from app.findings.models import Finding
    from app.reports.models import Report, ReportFinding

    rows = db.execute(
        select(Report.code)
        .join(ReportFinding, ReportFinding.report_id == Report.id)
        .join(Finding, Finding.id == ReportFinding.finding_id)
        .where(Finding.scan_id == scan_id)
    ).all()
    # 去重保序（同一条报告可能被多次命中）
    seen: list[str] = []
    for (code,) in rows:
        if code not in seen:
            seen.append(code)
    return seen


def purge_scan(db: Session, scan_id: str) -> None:
    """**永久删除**一次扫描（物理）。只在回收站内可用。

    两条闸，缺一不可：

    1. **必须先软删除**。对一条不在回收站里的扫描调它返回 `409`。
       "先删进回收站，再永久删除"是两道闸；合成一步等于把 purge
       变成一个不经过回收站的 `rm -rf`。
    2. **被报告引用时拒绝**（`409`），并说出是哪几份报告。
       理由见 `reports_referencing` 的说明 —— 那会掏空一份交付物。
    """
    scan = get_scan(db, scan_id)

    if scan.deleted_at is None:
        raise ApiError.conflict(
            "SCAN_NOT_IN_TRASH",
            "这条扫描不在回收站里。请先删除它（进回收站），再执行永久删除。",
        )

    blocking = reports_referencing(db, scan_id)
    if blocking:
        raise ApiError.conflict(
            "SCAN_REFERENCED_BY_REPORT",
            f"有 {len(blocking)} 份报告引用了这次扫描的漏洞（{'、'.join(blocking[:3])}），"
            "永久删除会掏空那些报告。请先删除相关报告，或保留这条扫描。",
        )

    db.delete(scan)
    db.commit()

    # ★ 删数据库行之后，**也要删磁盘上的产物**（2026-10-11 补）。
    #
    # 在此之前这里就结束了 —— 于是"永久删除"只删了记录，磁盘上那份
    # 5–7 MB 的产物永远留着，而且再没有任何东西引用它（数据库里已经
    # 查不到这个 id）。删得越多，孤儿目录越多。
    #
    # ⚠️ 放在 `db.commit()` **之后**，不是之前：万一 commit 失败
    #    （约束、连接断开），扫描记录还在，产物也不该先没了 ——
    #    那会让一条"存在但读不出产物"的记录看起来像数据损坏。
    #    先落库、再删文件，失败时最坏是留一个孤儿目录，
    #    而那个由 `sweep_orphan_artifacts` 兜底。
    #
    # ⚠️ 删失败**不影响这次 purge 的成功**：数据库那边已经删干净了，
    #    对用户来说这次操作就是成功的。磁盘清理失败只记日志 ——
    #    抛异常会让用户以为"没删掉"，然后重复操作。
    from app.scans import runner as runner_mod

    if not runner_mod.remove_scan_artifacts(scan_id):
        logger.warning(
            "扫描 %s 已从数据库永久删除，但产物目录没能删掉（可能已被手工清理）", scan_id
        )


# ══════════════════════════════════════════════════════════
# 保留期清理（自动归档 + 回收站到期）
# ══════════════════════════════════════════════════════════


def get_retention_config(db: Session):  # noqa: ANN201
    """读保留规则，没有就建一条默认的。

    **读时建行**而不是靠迁移插入初始行：迁移插入的行在"用户从未打开设置页"
    的实例上也会存在，但它的 `updated_at` 会撒谎（显示成一个谁都没操作过的时刻）。
    读时建行则如实反映"这条是用默认值新建的"。
    """
    from app.settings.models import SINGLETON_ID, ScanRetentionConfig

    config = db.get(ScanRetentionConfig, SINGLETON_ID)
    if config is None:
        config = ScanRetentionConfig(id=SINGLETON_ID)
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def sweep_retention(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """按规则做一次清理。返回各项处理了几条。

    ## 两件事

    1. **自动归档**：`failed` / `aborted` 且结束时间早于 N 天前 → 打 `archived_at`。
       （只归档这两种状态 —— 用户的原话是"失败 / 已终止的任务 N 天后自动归档"。
       已完成的扫描是他要回看的结果，替他归档等于把结果藏起来。）
    2. **回收站到期物理删除**：`deleted_at` 早于保留期 → 真删。

    ## ⚠️ 被报告引用的一律跳过（不删，也不报错）

    这里**不能**沿用 `purge_scan` 的"抛 409" —— 那是一个后台清理流程，
    没有请求方可以接收错误。所以**静默跳过并计数**，
    由调用方（启动日志）把"有几条因为被报告引用而保留了"说出来。
    悄悄删掉交付物的来源，比留一条过期的回收站记录严重得多。
    """
    moment = now or now_utc()
    config = get_retention_config(db)
    archived_count = 0
    purged_count = 0
    kept_count = 0

    if config.archive_after_days > 0:
        cutoff = moment - timedelta(days=config.archive_after_days)
        candidates = list(
            db.execute(
                select(Scan)
                .where(Scan.status.in_((ScanStatus.FAILED.value, ScanStatus.ABORTED.value)))
                .where(Scan.archived_at.is_(None))
                .where(Scan.deleted_at.is_(None))
                # 用 `finished_at` 而不是 `started_at`：规则说的是"任务结束 N 天后"，
                # 而一次中断的扫描可能很早就开始、很晚才结束。
                .where(Scan.finished_at.is_not(None))
                .where(Scan.finished_at < cutoff)
            ).scalars()
        )
        for scan in candidates:
            scan.archived_at = moment
            archived_count += 1

    if config.trash_retention_days > 0:
        trash_cutoff = moment - timedelta(days=config.trash_retention_days)
        expired = list(
            db.execute(
                select(Scan).where(Scan.deleted_at.is_not(None)).where(Scan.deleted_at < trash_cutoff)
            ).scalars()
        )
        for scan in expired:
            if reports_referencing(db, scan.id):
                kept_count += 1
                continue
            db.delete(scan)
            purged_count += 1

    config.last_sweep_at = moment
    db.commit()

    # ── 顺带清掉过期的**附件暂存批次**（任务 12）────────────
    #
    # ★ 放在这里而不是另起一个定时任务：`sweep_retention` 已经是
    #   "定期清理"的那个入口（启动时 + 改保留规则时都会调），
    #   而暂存文件正是"用户选了文件却最终没点启动"留下的垃圾 ——
    #   与保留期清理是同一类事。
    #
    # ⚠️ 它清的是 `_uploads/_staging/<批次>/`，**不动**已认领的附件
    #   （那些在 `_uploads/<scan_id>/` 下，是这台机器的正常数据）。
    #   这两条路径只差一级，所以清理函数必须精确匹配 `_staging` ——
    #   见 `uploads.sweep_staging`。
    from app.scans import uploads as uploads_mod

    staging_removed = uploads_mod.sweep_staging(
        older_than_seconds=uploads_mod.STAGING_TTL_SECONDS
    )

    # ── 顺带清掉**孤儿产物目录**（2026-10-11 补）──────────────
    #
    # ★ 与上面那条同一个理由：`sweep_retention` 已经是"定期清理"的
    #   唯一入口（启动时 + 改保留规则时都会调），孤儿产物属于同一类事。
    #
    # ⚠️ 它删的是"数据库里已经没有记录的 `scan_*` 目录"，**不是**
    #   "超过 N 天的目录" —— 后者会误伤正在跑的扫描与已归档的扫描
    #   （归档是"收起来"，产物要留着）。判据与理由见
    #   `runner.sweep_orphan_artifacts` 的 docstring。
    #
    # ⚠️ 也**不会**碰 `_uploads/` / `_ctf2_attachments/` / 转录目录 ——
    #   那些的生命周期不跟扫描走（附件跟任务、题目附件跟题目）。
    from app.scans import runner as runner_mod

    orphans_removed = runner_mod.sweep_orphan_artifacts(db)

    # ── 顺带清掉**已归档且过了产物保留期**的产物目录（2026-10-16 补）──
    #
    # ★★ 这条补的是 README 缺口清单「扫描产物目录只增不减」那一条的**后半段**。上面那个 `sweep_orphan_artifacts`
    #   只清"数据库里已无记录"的目录，而磁盘涨起来的主因是**记录还在、
    #   但早已归档**的那批（归档是自动的，产物却永远留着）。
    #   完整论证见 `runner.sweep_retired_artifacts` 的 docstring。
    #
    # ⚠️ 顺序：放在孤儿清理**之后**。两者判据不相交（一个"记录没了"、
    #   一个"记录还在且已归档"），所以顺序不影响结果 —— 但放在后面
    #   可以让日志里"孤儿 N 个"这个数字不被本轮新删的目录干扰。
    #
    # ⚠️ 保留期从配置读，**不硬编码**：它与上面两个天数同属"扫描保留规则"
    #   那个面板，用户能改。`0` = 关闭这一条。
    retired_removed = runner_mod.sweep_retired_artifacts(
        db, retention_days=config.artifact_retention_days
    )

    # ── 顺带把过期/陈旧的 CTF2 环境标成 expired（v0.40 补）──
    #
    # ★ 与上面几条同一个理由：`sweep_retention` 是"定期清理"的唯一入口。
    #
    # ⚠️ 这条清的不是磁盘、是**状态**：`Ctf2EnvironmentStatus.EXPIRED` 从
    #   定义那天起没有任何一处设置它，于是租期到了行还停在 ready，
    #   界面照旧渲染出一个标着「就绪」的死链接。判据与边界见
    #   `ctf2.service.expire_stale_environments` 的 docstring。
    #
    # ⚠️ 顺序：放在工作区清理**之前**。两者判据不相交（一个看库里
    #   的时间列、一个看磁盘），顺序不影响结果 —— 放前面是因为
    #   "先纠正库内状态、再动磁盘"这条读起来更顺，出问题时日志里
    #   状态那条也排在磁盘那条前面。
    from app.ctf2 import service as ctf2_service

    ctf2_environments_expired = ctf2_service.expire_stale_environments(db)

    # ── 顺带清掉"题目已不存在"的 CTF2 持久工作区（v0.29）──────
    #
    # ★ 加了持久化就必须加清理 —— 否则等于把 README 缺口清单「扫描产物目录只增不减」那一条
    #   （"只增不减"）从扫描产物复制到 CTF2。
    #
    # ⚠️ 判据是"题目记录还在不在"，不是时间：解出后放着不动的题，
    #   产物正是最该留的（复盘、写 WP 都要）。理由与 `sweep_orphan_artifacts`
    #   完全相同，见那个函数的 docstring。
    ctf2_workspaces_removed = runner_mod.sweep_orphan_ctf2_workspaces(db)

    result = {
        "archived": archived_count,
        "purged": purged_count,
        "keptBecauseReferenced": kept_count,
        "stagingSwept": staging_removed,
        "orphanArtifactsSwept": orphans_removed,
        "retiredArtifactsSwept": retired_removed,
        "ctf2WorkspacesSwept": ctf2_workspaces_removed,
        "ctf2EnvironmentsExpired": ctf2_environments_expired,
    }
    if any(result.values()):
        logger.info(
            "扫描保留期清理：归档 %d 条、清除 %d 条、因被报告引用而保留 %d 条、"
            "暂存 %d 批、孤儿产物 %d 个、归档产物 %d 个、CTF2 工作区 %d 个、"
            "CTF2 环境过期 %d 个",
            archived_count, purged_count, kept_count,
            staging_removed, orphans_removed, retired_removed,
            ctf2_workspaces_removed, ctf2_environments_expired,
        )
    return result


def compare_scans(db: Session, *, a_id: str, b_id: str) -> dict:
    """对比两条扫描的发现增减。

    **按 `title` 比，不按 `source_ref` / `id`。**

    理由：`source_ref` 是**外部工具在单次运行内的编号**（`vuln-0001`），
    换个目标、换次运行都会重来一遍 —— 拿它比，两次同一目标的扫描
    会得到"全都不一样"这个毫无意义的结论。

    而人比对两次扫描时问的是"同一个问题这次还在不在"，
    那个"问题"就是它的标题。
    """
    from app.findings.models import Finding

    a = get_scan(db, a_id)
    b = get_scan(db, b_id)

    def titles(scan_id: str) -> list[str]:
        rows = db.execute(
            select(Finding.title).where(Finding.scan_id == scan_id).order_by(Finding.title)
        ).all()
        return [row[0] for row in rows]

    a_titles = titles(a.id)
    b_titles = titles(b.id)
    a_set, b_set = set(a_titles), set(b_titles)

    only_a = sorted(a_set - b_set)
    only_b = sorted(b_set - a_set)
    common = sorted(a_set & b_set)

    if not only_a and not only_b:
        verdict = f"两次扫描的发现完全一致（各 {len(common)} 项）。"
    else:
        parts = []
        if only_b:
            parts.append(f"新增 {len(only_b)} 项")
        if only_a:
            parts.append(f"消失 {len(only_a)} 项")
        verdict = f"与「{a.target}」的较早一次相比：{'、'.join(parts)}，共有 {len(common)} 项。"

    return {
        "a": a,
        "b": b,
        "onlyInA": only_a,
        "onlyInB": only_b,
        "common": common,
        "verdict": verdict,
    }


__all__ = [
    "abort_scan",
    "batch_restore",
    "batch_set_archived",
    "batch_set_deleted",
    "compare_scans",
    "get_retention_config",
    "purge_scan",
    "reports_referencing",
    "sweep_retention",
    "build_spec",
    "check_authorization",
    "check_create_rate_limit",
    "count_by_status",
    "create_scan",
    "emit_lifecycle_event",
    "finalize",
    "findings_counts",
    "get_scan",
    "launch_scan",
    "list_scans",
    "read_log_tail",
    "count_scan_agents",
    "refresh_scan_rollup",
    "rerun_scan",
]
