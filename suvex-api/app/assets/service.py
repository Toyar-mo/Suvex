"""`assets` 领域 —— **按目标聚合的资产视图**（v0.85）。

## 为什么需要这个领域

系统原本是「以扫描为中心」的：一个目标扫了 5 次，这 5 次就是列表里
5 行互不相干的记录。用户真正想知道的是别的：

> 我这个域名，历史上被查出过什么？最近一次什么时候扫的？
> 哪些还没修？

这三个问题**现有界面一个都答不了**。`/scans/compare` 只能两条两条地比，
而且它比的是"发现增减"，不是"这个资产当前的风险面"。

竞品（XBOW、Pentera 这一类）都是以**资产**为第一视角的，扫描是资产的
从属动作。这个模块补的就是那个视角。

## 这个领域**没有表**

和 `overview` 一样，一切从 `scans` / `findings` 派生。不建 `assets` 表
是刻意的：

1. **没有第二个真相源**。资产身份就是归一化后的 `target`，而它已经在
   `scans.target` / `findings.target` 两列里。再建一张表，就要回答
   「资产改名了怎么办」「扫描里的 target 与资产表不一致时以谁为准」——
   这些问题现在不存在，因为只有一个来源。
2. **两张表的 `target` 是同一个口径**：`scans.target` 是**归一化后**的
   （见 `app/scans/models.py` 的注释），`findings.target` 由导入时从扫描
   带过来。所以按字符串直接分组是安全的 —— 这条前提一旦破了，
   分组就会把同一个目标拆成两条资产。

## ★ "最近一次扫描"要包含全部状态

初版只算 `completed` 的，理由是"失败的扫描没产出，不能代表资产状态"。
但回看用户的问题 —— 「最近一次什么时候扫的」问的是**动作发生过没有**，
不是「最近一次成功是什么时候」。一个目标连续失败 3 次，界面显示
「上次扫描 20 天前」是**错的**：20 天前的是上次成功，而那 3 次失败
恰恰是该被看到的信息（可能服务挂了、可能凭证过期了）。

所以 `lastScanAt` 取**所有状态**的最大值，同时单独给
`lastCompletedAt` 与 `consecutiveFailures` —— 让"多久没成功过"
可以被正面看到，而不是靠从两个时间戳里相减去猜。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.vocab import FindingStatus, ScanStatus
from app.findings.models import Finding
from app.scans.models import Scan

#: 一次查询最多返回多少个资产。
#:
#: 500 是权衡：真实环境里一个团队的目标数在几十到几百量级，
#: 一次全给比做分页省事；而上限存在的意义是**防止有人扫了上万个
#: 子域**之后界面直接卡死。超了要如实告诉调用方 `truncated`，
#: 不能静默截断成"就这么多"。
MAX_ASSETS = 500

#: 「已终结」的漏洞状态 —— 不计入 `openFindingCount`。
#:
#: ⚠️ `false_positive` 也算终结：它不是"还没修"，它是"不用修"。
#: 把它算进未修复数会让用户以为有一堆活要干，而真实工作量是 0。
#: `retesting` **不算**终结 —— 它恰恰是"修了但还没确认"，风险仍在。
TERMINAL_FINDING_STATUSES = (
    FindingStatus.FIXED.value,
    FindingStatus.CLOSED.value,
    FindingStatus.FALSE_POSITIVE.value,
)

#: 资产视图里"这个资产目前有多危险"只关心这几个严重度。
RISK_SEVERITIES = ("critical", "high")


def _open_condition():  # noqa: ANN202 - SQLAlchemy 表达式，类型没法直接标注
    """「这条漏洞还开着」的 SQL 条件。

    单点定义，因为下面三个计数（总数 / 未修复 / 高危未修复）都必须
    用**同一个**口径 —— 否则会出现「未修复 5 条、高危未修复 7 条」
    这种自己打自己的数字。
    """
    return Finding.status.not_in(TERMINAL_FINDING_STATUSES)


def _target_union():  # noqa: ANN202 - SQLAlchemy 子查询，类型没法直接标注
    """「有哪些资产」的唯一口径。

    以 `scans.target` 的**并集**为准，再并上 `findings.target`：
    正常情况下 findings 的 target 都来自某个 scan，但历史数据里
    可能有 finding 的 target 与 scan 的不一致（导入时外部工具给的值）。
    只取 scans 会漏掉那些；只取 findings 会漏掉"扫了但没发现"的目标。

    单点定义：列表、真实总数、全量统计三处必须用同一个并集，
    否则"卡上说 1200 个目标、列表却只能数出 1198 个"。
    """
    scan_targets = select(Scan.target).distinct()
    finding_targets = select(Finding.target).distinct()
    return scan_targets.union(finding_targets).subquery()


def asset_count(db: Session) -> int:
    """资产总数 —— **不受 `MAX_ASSETS` 截断影响**。

    列表最多返回 500 个，但"一共多少个目标"是一个独立的事实。
    拿返回页的长度冒充总数，会让截断提示说"只显示了前 500 个，共 500 个"。
    """
    union = _target_union()
    return int(db.execute(select(func.count()).select_from(union)).scalar_one())


def list_assets(db: Session, *, limit: int = MAX_ASSETS) -> tuple[list[dict], bool, int]:
    """按 `target` 聚合出资产清单。

    返回 `(assets, truncated, total)`：

      · `assets`     —— 最多 `limit` 条；
      · `truncated`  —— 目标数超过 `limit`，还有更多没返回；
      · `total`      —— **全量**目标数（不是 `len(assets)`）。

    `total` 单独给，是因为它和 `truncated` 是一对：
    只给布尔值的话，界面只能说"还有更多"，说不出"还有多少"。
    """

    # ── 1. 先确定"有哪些资产"──────────────────────────────
    union = _target_union()
    all_targets = select(union.c.target).order_by(union.c.target)

    rows = list(db.execute(all_targets).scalars())
    # ⚠️ 变量名不能叫 `total`：下面按严重度聚合的循环里有一个
    #    `total = int(row.total or 0)`，会把这里的值**静默覆盖**成
    #    "最后一条严重度统计的条数" —— 症状是 total 随机变成 1/2/3，
    #    而 items 是正常的。2026-11-09 实测踩到（total=1、items=3）。
    target_total = len(rows)
    truncated = target_total > limit
    targets = rows[:limit]
    if not targets:
        return [], False, 0

    # ── 2. 每个目标的扫描统计 ──────────────────────────────
    #
    # 一次 GROUP BY 全部拿回来，而不是每个 target 发一次查询（N+1）。
    scan_stats = {
        row.target: row
        for row in db.execute(
            select(
                Scan.target.label("target"),
                func.count().label("scan_count"),
                func.max(Scan.created_at).label("last_scan_at"),
                func.max(Scan.started_at).label("last_started_at"),
            )
            .where(Scan.target.in_(targets))
            .group_by(Scan.target)
        )
    }

    # 最近一次**成功**扫描的时刻 —— 单独一条查询，因为它带状态过滤。
    completed_stats = {
        row.target: row
        for row in db.execute(
            select(
                Scan.target.label("target"),
                func.max(Scan.created_at).label("last_completed_at"),
            )
            .where(Scan.target.in_(targets), Scan.status == ScanStatus.COMPLETED.value)
            .group_by(Scan.target)
        )
    }

    # 每个目标最近一次扫描的状态 + 它前面连续失败了几次。
    #
    # ⚠️ 「连续失败」必须真的按时间倒序数，不能用
    #    `failed_count / total_count` 之类的比例冒充 —— 那个数字
    #    在"5 次里失败 3 次、最后两次成功"时也会给出 0.6，
    #    而资产此刻是健康的。
    recent = db.execute(
        select(Scan.target, Scan.status, Scan.id)
        .where(Scan.target.in_(targets))
        .order_by(Scan.target, Scan.created_at.desc(), Scan.id)
    )
    consecutive: dict[str, int] = {}
    last_status: dict[str, str] = {}
    stopped: set[str] = set()
    for target, status, _scan_id in recent:
        if target in stopped:
            # 这一串失败已经数完了（更早的行都属于"上一次出事"）。
            continue
        first = target not in last_status
        if target not in last_status:
            last_status[target] = status
            consecutive[target] = 0
            if status != ScanStatus.FAILED.value:
                # ★ 最近一次就不是失败 → 连续失败必然是 0。
                # 这一步不能省：`consecutive` 初始化为 0 后，下面的
                # `elif consecutive > 0` 永远进不去，`stopped` 就永远
                # 不会把后面的失败行排除掉，于是一路把**历史**失败也数进来。
                # （实测症状：最近两次成功、前三次失败 → 报 3。）
                stopped.add(target)
                continue
        if status == ScanStatus.FAILED.value:
            consecutive[target] += 1
        elif not first:
            # 遇到第一个非失败就停 —— 之前数到的那串才是"最近连续失败"。
            # 用 `stopped` 集合而不是 `break`：`recent` 是**全部目标**的
            # 行交错排在一起，break 会把别的目标的行也一起丢掉。
            stopped.add(target)

    # ── 3. 每个目标的漏洞统计 ──────────────────────────────
    #
    # ⚠️ 这里用 `case(...)` 而不是 `func.sum(func.cast(条件, Integer))`：
    #    `func.cast(...)` 在 SQLAlchemy 2.x 里会走 `_type_from_args` 去推断
    #    返回类型，而 `_open_condition()` 是一个 `not_in` **表达式**（不是列），
    #    推断时访问 `._isnull` 直接 `AttributeError`。用 `case` 显式给出
    #    else 与类型就没有这个推断路径，而且读起来也更接近 SQL 的
    #    `COUNT(CASE WHEN ... THEN 1 END)`。
    open_flag = case((_open_condition(), 1), else_=0)
    severity_rows = db.execute(
        select(
            Finding.target.label("target"),
            Finding.severity.label("severity"),
            func.count().label("total"),
            func.sum(open_flag).label("open_total"),
        )
        .where(Finding.target.in_(targets))
        .group_by(Finding.target, Finding.severity)
    )

    per_target: dict[str, dict] = {
        target: {
            "target": target,
            "findingCount": 0,
            "openFindingCount": 0,
            "bySeverity": {},
            "openBySeverity": {},
        }
        for target in targets
    }

    for row in severity_rows:
        bucket = per_target.get(row.target)
        if bucket is None:  # 理论上不会 —— targets 是从并集里取的
            continue
        total = int(row.total or 0)
        open_total = int(row.open_total or 0)
        bucket["findingCount"] += total
        bucket["openFindingCount"] += open_total
        bucket["bySeverity"][row.severity] = total
        if open_total:
            bucket["openBySeverity"][row.severity] = open_total

    # ── 4. 拼装 ────────────────────────────────────────────
    assets: list[dict] = []
    for target in targets:
        bucket = per_target[target]
        stats = scan_stats.get(target)
        completed = completed_stats.get(target)
        open_by_sev: dict = bucket["openBySeverity"]
        assets.append(
            {
                "target": target,
                # 扫描
                "scanCount": int(stats.scan_count) if stats else 0,
                "lastScanAt": stats.last_scan_at if stats else None,
                "lastScanStatus": last_status.get(target),
                "lastCompletedAt": completed.last_completed_at if completed else None,
                "consecutiveFailures": consecutive.get(target, 0),
                # 漏洞
                "findingCount": bucket["findingCount"],
                "openFindingCount": bucket["openFindingCount"],
                "openCritical": int(open_by_sev.get("critical", 0)),
                "openHigh": int(open_by_sev.get("high", 0)),
                "bySeverity": bucket["bySeverity"],
            }
        )

    # 排序：**风险优先**，然后最近活动。
    #
    # 这个次序是这一页存在的理由 —— 用户打开它就是要看"哪个资产最该处理"。
    # 按名字排会让「A 开头的那个，其实什么问题都没有」占住第一屏。
    # 同风险时用「最近扫过」靠前：正在被关注的目标应该在上面。
    #
    # ⚠️ 排序键**必须是字典里的键名**，不能是临时算出来的值 —— 第一版
    #    在这里用一个 `_sort_key` 函数只看 openCritical/openHigh，
    #    于是"已修复的 critical"（openCritical=0）和"什么都没有"
    #    排出来一模一样。反向验证当场抓到：把 `openFindingCount`
    #    换成 `findingCount` 测试照样绿。
    assets.sort(
        key=lambda a: (
            -a["openCritical"],
            -a["openHigh"],
            -a["openFindingCount"],
            -(a["lastScanAt"].timestamp() if a["lastScanAt"] else 0),
            a["target"],
        )
    )
    return assets, truncated, target_total


def summary_all(db: Session) -> dict:
    """顶部统计条 —— **全量口径**，与列表是否截断无关。

    ## 为什么不从返回的这一页累加

    卡片写着「目标总数」「带开着的高危」。列表被 `MAX_ASSETS` 截断时，
    从 500 条累加出来的数字会被当成全站事实，而它只是"前 500 个里有多少" ——
    用户会在卡上和列表之间找不到对应关系，而他没有任何线索知道
    差异来自截断。

    这几条都是**聚合查询**，不随目标数增长取回行数：
    总数、漏洞数、带高危的目标数（distinct target）、从未扫描、从未跑完。

    与 `summary(assets)` 的关系：那个按传入的资产列表算，保留给
    "只要这一页的画像"的调用方与测试；接口一律用全量这个。
    """
    union = _target_union()
    open_findings = _open_condition()

    target_count = int(db.execute(select(func.count()).select_from(union)).scalar_one())

    finding_total = int(
        db.execute(select(func.count()).select_from(Finding)).scalar_one()
    )
    open_total = int(
        db.execute(
            select(func.count()).select_from(Finding).where(open_findings)
        ).scalar_one()
    )

    high_targets = int(
        db.execute(
            select(func.count(func.distinct(Finding.target))).where(
                open_findings, Finding.severity.in_(RISK_SEVERITIES)
            )
        ).scalar_one()
    )

    # 只有 findings、一次扫描都没有的目标（历史导入造成的）
    scan_targets = select(Scan.target).distinct()
    never_scanned = int(
        db.execute(
            select(func.count(func.distinct(Finding.target))).where(
                Finding.target.not_in(scan_targets)
            )
        ).scalar_one()
    )

    # 有扫描、但一次都没成功过的目标 —— "看起来干净"只是因为没被检查过
    completed_targets = select(Scan.target).where(
        Scan.status == ScanStatus.COMPLETED.value
    ).distinct()
    never_completed = int(
        db.execute(
            select(func.count(func.distinct(Scan.target))).where(
                Scan.target.not_in(completed_targets)
            )
        ).scalar_one()
    )

    return {
        "targetCount": target_count,
        "findingCount": finding_total,
        "openFindingCount": open_total,
        "targetsWithOpenHigh": high_targets,
        "neverScanned": never_scanned,
        "neverCompleted": never_completed,
        # 服务端时间：客户端算"多久之前"应该用自己的时钟
        "generatedAt": datetime.now(tz=None),
    }


def summary(assets: list[dict]) -> dict:
    """顶部统计条要的几个数。

    ⚠️ `targetsWithOpenHigh` 数的是**资产个数**，不是漏洞条数 ——
    它回答的是"有多少台机器现在带着高危"，而"高危漏洞有几条"
    是另一个问题（在 `/findings/stats`）。混在一起会让用户
    分不清自己在看机器数还是漏洞数。
    """
    now = datetime.now(tz=None)
    return {
        "targetCount": len(assets),
        "findingCount": sum(a["findingCount"] for a in assets),
        "openFindingCount": sum(a["openFindingCount"] for a in assets),
        "targetsWithOpenHigh": sum(1 for a in assets if a["openCritical"] > 0 or a["openHigh"] > 0),
        "neverScanned": sum(1 for a in assets if a["scanCount"] == 0),
        # 从未成功过的（有扫描、但一次都没 completed）—— 这类资产"看起来
        # 没问题"其实是因为**从来没真正检查过**，这是最容易被误读的状态。
        "neverCompleted": sum(1 for a in assets if a["scanCount"] > 0 and a["lastCompletedAt"] is None),
        # 只为让调用方知道这个字段不是随便给的：它是服务端时间，
        # 客户端要算"多久之前"应该用自己的时钟
        "generatedAt": now,
    }
