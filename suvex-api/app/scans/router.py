"""`scans` 领域路由（契约 §3.3，任务 3）。

| 方法 | 路径                     | 鉴权     |
| ---- | ------------------------ | -------- |
| GET  | `/scans`                 | read     |
| POST | `/scans`                 | **scan** |
| GET  | `/scans/queue`           | read     |
| GET  | `/scans/{scanId}`        | read     |
| POST | `/scans/{scanId}/abort`  | **scan** |
| POST | `/scans/{scanId}/rerun`  | **scan** |
| GET  | `/scans/{scanId}/agents` | read     |

写操作是 `scan` 级而不是 `read`，因为：

* `POST /scans` 与 `rerun` **会对真实目标发包** —— 这正是 `scan` scope
  存在的理由（契约 §1.2：「能访问到 API」不该等于「能发起扫描」）；
* `abort` 也归 `scan`：能终止扫描的人，本来就该是有权发起扫描的人。
  给它单独的更低级别没有意义 —— 终止是"少做一些事"，
  但它同样需要理解自己在动什么。
"""

from __future__ import annotations

import logging
import threading
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import Identity, client_ip, require_read, require_scan
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import ApiError, ErrorCode
from app.core.ids import new_id
from app.core.vocab import TERMINAL_SCAN_STATUSES, AuditAction, ScanGoal, ScanStatus, values
from app.events import service as events_service
from app.events.schemas import AgentOut
from app.scans import artifacts, service, writeup
from app.scans.runner import get_runner
from app.scans.scheduler import get_scheduler
from app.scans import uploads
from app.scans.schemas import (
    BatchArchiveIn,
    BatchIdsIn,
    BatchRerunFailure,
    BatchRerunIn,
    BatchRerunOut,
    BatchResultOut,
    CompareOut,
    ScanCreate,
    ScanAttachmentOut,
    ScanOut,
    ScanRerun,
    ScanStatsOut,
    StagedAttachmentsOut,
    WriteupOut,
)

logger = logging.getLogger("suvex.scans")

router = APIRouter(prefix="/scans", tags=["扫描"])

ReadDep = Annotated[Identity, Depends(require_read)]
ScanDep = Annotated[Identity, Depends(require_scan)]
DbDep = Annotated[Session, Depends(get_db)]


def _nudge_scheduler() -> None:
    """新建之后立刻捅一下调度器。

    不捅的话要等下一次轮询（默认 2 秒）才会启动 —— 用户点完
    "开始扫描"会看到状态卡在"排队中"两秒，然后才跳成"运行中"。
    那两秒没有传递任何信息，只是让人怀疑是不是没生效。

    放在**后台线程**里：启动扫描要 fork 一个进程，而请求线程不该为它多等。
    异常全部吞掉并记录 —— 创建已经成功并提交了，
    调度器的一次抖动不该让用户看到 500。
    """

    def run() -> None:
        try:
            get_scheduler().tick()
        except Exception:  # noqa: BLE001
            logger.exception("新建扫描后触发调度失败（扫描仍在队列里，下一轮会重试）")

    threading.Thread(target=run, name="suvex-scan-nudge", daemon=True).start()


# ══════════════════════════════════════════════════════════
# 读
# ══════════════════════════════════════════════════════════


@router.get("", response_model=list[ScanOut], summary="扫描列表（?status=&q=&sort=&dir=）")
def list_scans(
    _identity: ReadDep,
    db: DbDep,
    status_filter: Annotated[
        str | None, Query(alias="status", description="queued|running|completed|failed|aborted")
    ] = None,
    q: Annotated[str | None, Query(description="按名称或目标模糊搜索")] = None,
    sort: Annotated[Literal["recent", "severity", "duration"], Query()] = "recent",
    dir: Annotated[Literal["asc", "desc"], Query()] = "desc",
    # ★ 可见性三档（v0.13）。默认两 false = **默认视图**：
    #   既不含已归档、也不含回收站。过滤在服务端做，
    #   因为前端的 tab 计数直接数这个数组 —— 混进来就会让数字撒谎。
    archived: Annotated[bool, Query(description="只看已归档")] = False,
    deleted: Annotated[bool, Query(description="只看回收站")] = False,
) -> list[ScanOut]:
    """列表。

    查询参数的取值用**前端已有的枚举**（契约 §3.3）：`status` 是那五个值、
    `sort` 对应 `SORT_LABEL` 的键、`dir` 是 `asc|desc`。
    用 `Literal` 而不是自由字符串：写错的值会得到 400 而不是"静默忽略然后
    返回全部"，后者会让前端以为筛选生效了。
    """
    if status_filter is not None and status_filter not in values(ScanStatus):
        raise ApiError.validation(
            f"status 取值不合法：{status_filter!r}",
            {"status": f"应为 {'、'.join(values(ScanStatus))} 之一"},
        )
    scans = service.list_scans(
        db,
        status=status_filter,
        q=q,
        sort=sort,
        direction=dir,
        archived=archived,
        deleted=deleted,
    )
    return [ScanOut.from_model(scan) for scan in scans]


@router.get(
    "/queue",
    response_model=ScanStatsOut,
    summary="调度器状态（为什么我的扫描还在排队）",
)
def queue_status(_identity: ReadDep, db: DbDep) -> ScanStatsOut:
    """回答一个前端**必须**能回答的问题。

    用户点了开始，扫描一直是"排队中" —— 最常见的解释是并发上限满了。
    没有这个端点，界面只能显示"排队中"，而那看起来像坏了。
    """
    scheduler = get_scheduler()
    ready, error = scheduler.runner_status()
    counts = service.count_by_status(db)
    return ScanStatsOut(
        running=scheduler.running_count,
        limit=get_settings().max_concurrent_scans,
        queued=counts.get(ScanStatus.QUEUED.value, 0),
        runner=get_runner().name,
        runnerReady=ready,
        runnerError=error,
    )


# ══════════════════════════════════════════════════════════
# 批量操作（v0.13，任务 9）
# ══════════════════════════════════════════════════════════
#
# ⚠️ **这些固定路径必须声明在 `/{scanId}` 之前。**
#
# FastAPI 按声明顺序匹配，先匹配上的赢。`POST /scans/batch/rerun` 与
# `POST /scans/{scanId}/rerun` 的形状完全一样（两段），所以只要
# `{scanId}` 那条先声明，`batch` 就会被当成一个 scanId 吃掉 ——
# 症状是"批量重跑报 404 找不到扫描 batch"，而路径看起来完全正确。
#
# 与 `reports/router.py` 里 `/reports/preview` 必须排在
# `/reports/{reportId}` 之前是同一条规矩。


def _batch_out(changed, skipped: int) -> BatchResultOut:  # noqa: ANN001
    return BatchResultOut(
        changed=len(changed),
        skipped=skipped,
        scans=[ScanOut.from_model(scan) for scan in changed],
    )


@router.post("/batch/archive", response_model=BatchResultOut, summary="批量归档 / 取消归档")
def batch_archive(
    payload: BatchArchiveIn, request: Request, identity: ScanDep, db: DbDep
) -> BatchResultOut:
    """归档或取消归档（`archived` 决定方向）。

    **归档是与 status 正交的维度**，不是新的状态值 —— 一条 `failed` 的
    扫描可以同时是"已归档"的。把它塞进 status 会让"已归档的失败扫描"
    无处表达。
    """
    changed, skipped = service.batch_set_archived(
        db, ids=payload.ids, archived=payload.archived
    )
    # 只在真的改了的时候留痕（全量幂等重放不是一次操作）。
    # 归档要留痕是因为它让记录**从默认视图里消失** ——
    # 事后必须能回答"那次扫描怎么不见了"。
    if changed:
        verb = "归档" if payload.archived else "取消归档"
        audit.record(
            db,
            request=audit.from_http(request, identity),
            action=AuditAction.SCAN_ARCHIVE,
            summary=f"{verb}了 {len(changed)} 次扫描",
            target=",".join(scan.id for scan in changed)[:512],
            detail={"archived": payload.archived, "ids": [s.id for s in changed]},
        )
    return _batch_out(changed, skipped)


@router.post("/batch/delete", response_model=BatchResultOut, summary="批量删除（进回收站）")
def batch_delete(payload: BatchIdsIn, _identity: ScanDep, db: DbDep) -> BatchResultOut:
    """**软删除** —— 数据一条都不丢，只打 `deletedAt` 标记。

    "删除"这个词在界面上会让用户以为数据没了，而这个端点保证它还在
    回收站里。真正的物理删除只在两处：`purge` 与保留期到期。
    """
    changed, skipped = service.batch_set_deleted(db, ids=payload.ids)
    return _batch_out(changed, skipped)


@router.post("/batch/restore", response_model=BatchResultOut, summary="批量恢复")
def batch_restore(payload: BatchIdsIn, _identity: ScanDep, db: DbDep) -> BatchResultOut:
    changed, skipped = service.batch_restore(db, ids=payload.ids)
    return _batch_out(changed, skipped)


@router.post("/batch/rerun", response_model=BatchRerunOut, summary="批量重跑")
def batch_rerun(
    payload: BatchRerunIn, request: Request, identity: ScanDep, db: DbDep
) -> BatchRerunOut:
    """批量重跑 —— **逐条授权确认 + 逐条留痕**。

    ⚠️ 单条重跑强制要求逐字授权声明（契约 §5.3，法律要求）。
    批量重跑是 **N 次真的发包**，不能因为"批量"就绕过它 ——
    那样这个端点会变成一条**绕过授权确认的捷径**：
    点一次批量重跑，N 个目标被发包，而没有任何一条留痕。

    **逐条处理、逐条失败**：一条起不来不该让其余的全军覆没，
    所以每条的失败单独收集在 `failed` 里返回。
    """
    created: list[ScanOut] = []
    failed: list[BatchRerunFailure] = []

    for scan_id in payload.ids:
        try:
            scan = service.rerun_scan(
                db,
                scan_id,
                payload,
                identity=identity,
                ip=client_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
        except ApiError as exc:
            failed.append(BatchRerunFailure(scanId=scan_id, error=exc.message))
            continue
        created.append(ScanOut.from_model(scan))
        # ★ 每一条都留痕，不是"批量一条"。审计要能回答
        #   "哪个目标被重新发包了"，而 N 条合成一条会把目标信息抹掉。
        audit.record(
            db,
            request=audit.from_http(request, identity),
            action=AuditAction.SCAN_RERUN,
            summary=f"批量重跑：基于 {scan_id} 新建 {scan.id}（目标 {scan.target}）",
            target=scan.id,
            detail={"sourceScanId": scan_id, "target": scan.target, "viaBatch": True},
        )

    if created:
        _nudge_scheduler()
    return BatchRerunOut(created=created, failed=failed)


@router.get("/compare", response_model=CompareOut, summary="对比两条扫描的发现增减")
def compare_scans(
    _identity: ReadDep,
    db: DbDep,
    a: Annotated[str, Query(description="较早的那次扫描 id")],
    b: Annotated[str, Query(description="较新的那次扫描 id")],
) -> CompareOut:
    """对比。按**漏洞标题**比，不按 id —— 理由见 `service.compare_scans`。"""
    result = service.compare_scans(db, a_id=a, b_id=b)
    return CompareOut(
        a=ScanOut.from_model(result["a"]),
        b=ScanOut.from_model(result["b"]),
        onlyInA=result["onlyInA"],
        onlyInB=result["onlyInB"],
        common=result["common"],
        verdict=result["verdict"],
    )


@router.get("/{scanId}", response_model=ScanOut, summary="扫描详情")
def read_scan(scanId: str, _identity: ReadDep, db: DbDep) -> ScanOut:
    return ScanOut.from_model(service.get_scan(db, scanId))


@router.get("/{scanId}/agents", response_model=list[AgentOut], summary="该扫描的 agent 名册")
def read_agents(scanId: str, _identity: ReadDep, db: DbDep) -> list[AgentOut]:
    """Activity 页左侧名册消费它（契约 §2.4）。

    名册为空是**正常答案**，不是错误：本次扫描还没有 agent 被创建。
    返回 404 会让前端把"还没开始"渲染成"出错了"。
    """
    service.get_scan(db, scanId)
    return [AgentOut.model_validate(agent) for agent in events_service.agents_for_scan(db, scanId)]


@router.get("/{scanId}/writeup", response_model=WriteupOut, summary="CTF 题解（Writeup）")
def read_writeup(scanId: str, _identity: ReadDep, db: DbDep) -> WriteupOut:
    """把这次 CTF 扫描的产物整理成一份**题解**。

    ## 为什么是我们拼，而不是让 agent 直接输出题解

    做不到 —— strix 的报告标题（`# Security Penetration Test Report`）
    与分节标题都是写死的，`finish_scan` 也只有那四个渗透口径的字段。
    详见 `scans/writeup.py` 的模块说明。

    ## 三种成因走同一个 409，但**话不一样**

    判据不是夺旗 / 还没跑完 / 报告文件已不在 —— 补救动作都不是在界面上
    能做的（换判据重跑、或者等它跑完），所以共用一个错误码；
    真正要区分的是那句话，而 `message` 本来就是写给用户看的中文原话。
    分开三个码只会让前端写三段其实一样的分支。
    """
    scan = service.get_scan(db, scanId)

    if scan.goal != ScanGoal.CAPTURE_FLAG.value:
        raise ApiError(
            409,
            ErrorCode.WRITEUP_UNAVAILABLE,
            "这次扫描的判据不是「CTF 夺旗」，所以没有题解。"
            "题解只在夺旗任务下生成——渗透评估的产物是漏洞报告，不是解题过程。",
        )

    if scan.status not in TERMINAL_SCAN_STATUSES:
        raise ApiError(
            409,
            ErrorCode.WRITEUP_UNAVAILABLE,
            "这次扫描还没有结束，题解要等它跑完才有。",
        )

    # ★ 读一次报告，然后**传进去**，不让 `build_writeup` 自己再读一遍。
    #   读两遍不只是浪费：两次之间文件可能被清理，于是"判断有没有"
    #   与"实际用来拼的那份"可以不是同一个东西 —— 而症状是
    #   返回了一份只有标题和 flag 的空题解，看起来还像成功了。
    report = artifacts.latest_report(scan.run_dir)
    if not report.strip():
        raise ApiError(
            409,
            ErrorCode.WRITEUP_UNAVAILABLE,
            "这次扫描没有留下报告文件（产物目录可能被手工清理过），拼不出题解。",
        )

    return WriteupOut(
        markdown=writeup.build_writeup(scan, report=report),
        filename=writeup.filename_for(scan),
    )


# ══════════════════════════════════════════════════════════
# 写
# ══════════════════════════════════════════════════════════


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ScanOut,
    summary="新建扫描 —— 高风险，需授权确认",
)
def create_scan(
    payload: ScanCreate,
    request: Request,
    identity: ScanDep,
    db: DbDep,
) -> ScanOut:
    """新建一次扫描。

    ★ **响应里的 `status` 是 `queued`，不是 `running`。**
    契约 §3.3 专门标了这条：`running` 意味着"agent 已经在打了"，
    而实际要等调度器接单（并发满了还要排队）。**状态语义不能撒谎。**

    授权确认的三条校验在这里（`service.check_authorization`）：
    缺字段或 `confirmed != true` → `403 TARGET_NOT_AUTHORIZED`；
    `statement` 与约定措辞**不完全一致** → `400 VALIDATION_FAILED`。
    """
    # ★ 限流**在落库之前**：超限时不该留下任何痕迹（连一条 queued 都不该有）。
    service.check_create_rate_limit(identity, client_ip(request))
    scan, reach = service.create_scan(
        db,
        payload,
        identity=identity,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    # ★ 审计留痕（契约 §3.9）。写在**落库成功之后** ——
    # 一次被拒的创建没有后果，不需要进那份要留一年的记录。
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.SCAN_CREATE,
        summary=f"对 {scan.target} 发起了扫描「{scan.name}」",
        target=scan.id,
        detail={
            "target": scan.target,
            "targetKind": scan.target_kind,
            "skills": list(scan.skills or []),
            "providerId": scan.provider_id,
        },
    )
    _nudge_scheduler()
    # ★ 把预检结论带回去（任务 11）。用 `model_copy` 而不是给
    # `from_model` 加参数：那个方法被七八处调用，为一个只在创建时
    # 有值的字段去改它，会让其余调用点都要多传一个 None。
    out = ScanOut.from_model(scan)
    if reach.notice:
        out = out.model_copy(update={"notice": reach.notice})
    return out


@router.post("/{scanId}/abort", response_model=ScanOut, summary="终止扫描")
def abort_scan(  # noqa: N803
    scanId: str, request: Request, identity: ScanDep, db: DbDep
) -> ScanOut:
    """终止。

    只允许 `queued` / `running`；其它状态返回 `409 SCAN_NOT_ABORTABLE`
    （前端 `abortScan` 对已完成扫描返回 false）。

    终止会**杀掉整个进程树**（不只是直接子进程）—— 见
    `runner.terminate_tree` 的说明：strix 自己会起 docker 客户端与一堆工具进程，
    只杀直接子进程会留下再也无人认领的孙子进程。
    """
    scan = service.abort_scan(db, scanId)
    # ★ abort 是 `scan` 级（不是 read）：它是"少做一些事"，但同样需要
    # 理解自己在动什么的人来做。它也有后果（进程被杀、那次结果不完整），
    # 所以同样留痕。
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.SCAN_ABORT,
        summary=f"终止了扫描「{scan.name}」（目标 {scan.target}）",
        target=scan.id,
        detail={"target": scan.target},
    )
    return ScanOut.from_model(scan)


@router.delete("/{scanId}/purge", status_code=status.HTTP_204_NO_CONTENT, summary="永久删除")
def purge_scan(  # noqa: N803
    scanId: str, request: Request, identity: ScanDep, db: DbDep
) -> None:
    """**永久删除**（物理）。只在回收站内可用。

    两种 `409`：不在回收站里（`SCAN_NOT_IN_TRASH`）、
    被报告引用（`SCAN_REFERENCED_BY_REPORT`）。后者见
    `service.reports_referencing` 的说明 —— 那会掏空一份交付物。
    """
    service.purge_scan(db, scanId)
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.SCAN_PURGE,
        summary=f"永久删除了一次扫描（{scanId}）",
        target=scanId,
    )


@router.post(
    "/{scanId}/resume",
    status_code=status.HTTP_201_CREATED,
    response_model=ScanOut,
    summary="续跑（同一条记录，接着上次的会话跑；同样需授权确认）",
)
def resume_scan(
    scanId: str,
    payload: ScanRerun,
    request: Request,
    identity: ScanDep,
    db: DbDep,
) -> ScanOut:
    """**在同一条记录上**接着上次的会话跑。

    ## 与「重跑」的区别

    * 重跑 = 新建一条记录、从零开始（保住上次的历史）；
    * 续跑 = 同一条记录、接回上次的会话与工作目录。

    续跑能接回的是执行后端（strix）的 **root 与每个未终结子 agent 的完整
    LLM 历史与拓扑**，外加工作目录里的 `notes.json` / `threat_models.json` /
    `todos.json` / `coverage.json`。对一次跑了十几分钟才倒下的扫描，
    这比从头再来省得多。

    ⚠️ 请求体沿用 `ScanRerun`：它要求的正是**重新确认授权**，
    而续跑同样会继续对真实目标发包 —— 一次确认不该永久授权。
    """
    # 与新建/重跑共用一个额度池：三者都会对真实目标发包。
    service.check_create_rate_limit(identity, client_ip(request))
    scan = service.resume_scan(
        db,
        scanId,
        payload,
        identity=identity,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.SCAN_RESUME,
        summary=f"续跑了扫描 {scanId}（run={scan.run_id}，目标 {scan.target}）",
        target=scan.id,
        detail={"scanId": scanId, "runId": scan.run_id, "target": scan.target},
    )
    _nudge_scheduler()
    return ScanOut.from_model(scan)


@router.post(
    "/{scanId}/rerun",
    status_code=status.HTTP_201_CREATED,
    response_model=ScanOut,
    summary="重跑（新建记录，同样需授权确认）",
)
def rerun_scan(
    scanId: str,
    payload: ScanRerun,
    request: Request,
    identity: ScanDep,
    db: DbDep,
) -> ScanOut:
    """基于历史配置**建一条新记录**。

    ⚠️ 两件事同时成立（契约 §3.3 / §6）：

    1. **不是复用旧的 run。** 把已完成的记录改回 `running` 会把那次历史抹掉，
       而"哪次发现了什么、修没修"正是扫描历史的价值所在。
    2. **同样要求授权确认。** 重跑在语义上是一次新扫描，目标可能已经变了
       （域名易主、IP 重分配）—— 一次确认不该永久授权对同名目标发包。
    """
    # 重跑同样会对真实目标发包，所以与新建共用一个额度池。
    service.check_create_rate_limit(identity, client_ip(request))
    scan = service.rerun_scan(
        db,
        scanId,
        payload,
        identity=identity,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.SCAN_RERUN,
        summary=f"基于 {scanId} 重跑了一次扫描（新记录 {scan.id}，目标 {scan.target}）",
        target=scan.id,
        detail={"sourceScanId": scanId, "target": scan.target},
    )
    _nudge_scheduler()
    return ScanOut.from_model(scan)


# ══════════════════════════════════════════════════════════
# 扫描附件（任务 12 阶段 4）
# ══════════════════════════════════════════════════════════
#
# ⚠️ 路由顺序：这些都必须在 `/{scanId}` 之前吗？**不必** ——
#    它们的第一段就是 `{scanId}`，不存在"被当成固定路径"的问题。
#    （与 `/findings/stats` 那类不同：那个的冲突点是固定名会撞上
#    `{findingId}`。这里 `/scans/stats` 确实存在，它声明在前面。）
#
# ⚠️ **但 `/attachments/staging` 是例外** —— 它的第一段是固定名
#    `attachments`，会与 `/{scanId}/...` 竞争。所以它必须声明在
#    `/{scanId}/attachments` **之前**，否则 `attachments` 会被当成
#    一个 scanId。这正是那个"踩过三次"的路由顺序坑。


@router.post(
    "/attachments/staging",
    response_model=StagedAttachmentsOut,
    status_code=status.HTTP_201_CREATED,
    summary="把题目附件传进暂存区（创建扫描之前）",
)
async def stage_scan_attachments(
    request: Request,
    identity: ScanDep,
    db: DbDep,
    files: Annotated[list[UploadFile], File(description="题目附件（任意类型）")],
) -> StagedAttachmentsOut:
    """把附件先放进暂存区，返回一个 `stagingId`。

    ## ★ 为什么需要"创建之前"这一步

    向导里的顺序是：填目标 → 贴题面 → **选附件** → 点启动。
    而扫描一旦创建就毫秒级被调度器认领，所以
    「建完再往 `/{id}/attachments` 传」这个窗口**根本不存在** ——
    那条路会稳定地拿到 409。我第一版就是那么设计的，被测试当场抓住。

    所以附件走**先暂存、创建时认领**：这里拿到 `stagingId`，
    启动时把它放进 `POST /scans` 的 `attachmentStagingId`。

    ## 没有扫描 id 也能校验

    这一步还没有 scan，所以"这次扫描能不能收附件"这类校验都做不了。
    但**文件本身的校验照做**（大小、非空、文件名消毒）——
    它们与落点无关，见 `uploads._write`。

    ⚠️ 暂存目录里的文件**不会被自动清理**吗？会的 ——
    用户选了文件却没点启动时，那些文件由 `uploads.sweep_staging`
    按时间扫掉（它挂在 `service.sweep_retention` 里跑）。
    """
    if len(files) > uploads.MAX_COUNT:
        raise ApiError.validation(
            f"一次最多上传 {uploads.MAX_COUNT} 个附件",
            {"files": f"收到了 {len(files)} 个"},
        )

    staging_id = new_id("stage")
    rows: list[ScanAttachmentOut] = []
    for item in files:
        content = await item.read()
        name, size = uploads.store_staged(
            staging_id=staging_id, filename=item.filename or "attachment", content=content
        )
        rows.append(ScanAttachmentOut(name=name, sizeBytes=size))

    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.SCAN_ATTACH,
        summary=f"暂存了 {len(rows)} 个题目附件（尚未关联扫描）",
        target=staging_id,
        detail={"names": [row.name for row in rows]},
    )
    return StagedAttachmentsOut(stagingId=staging_id, files=rows)


@router.get(
    "/{scanId}/attachments",
    response_model=list[ScanAttachmentOut],
    summary="任务附件列表",
)
def list_scan_attachments(
    scanId: str, _identity: ReadDep, db: DbDep  # noqa: N803
) -> list[ScanAttachmentOut]:
    """列出随任务交给 agent 的附件（任务 12）。

    先确认扫描存在：对不存在的 id 返回空列表会让前端把笔误
    显示成"没有附件"。
    """
    service.get_scan(db, scanId)
    return [ScanAttachmentOut(**row) for row in uploads.list_for(scanId)]


@router.post(
    "/{scanId}/attachments",
    response_model=list[ScanAttachmentOut],
    status_code=status.HTTP_201_CREATED,
    summary="上传任务附件（multipart）",
)
async def upload_scan_attachments(
    scanId: str,  # noqa: N803
    request: Request,
    identity: ScanDep,
    db: DbDep,
    files: Annotated[list[UploadFile], File(description="题目附件（任意类型）")],
) -> list[ScanAttachmentOut]:
    """上传题目附件 —— 它们会在启动时被投进沙箱工作区。

    ⚠️ 这是 **multipart** 端点，属于 CSRF 防线的**显式例外**
    （见 `auth/deps.py` 的 `MULTIPART_WRITE_ENDPOINTS` 与契约 §3.15.1）。
    新增上传端点必须登记进那张表，否则会被 400 挡掉 ——
    有一条自检测试盯着这件事。

    ## 为什么只允许在**未启动**时上传

    附件是在 `command()` 里编译成 `--workspace-file` 的，
    而那一次编译发生在**启动那一刻**。扫描开跑之后再传，
    文件会躺在磁盘上但**永远不会被 agent 看到** ——
    那比拒绝更糟：用户以为交上去了。

    所以这里对非 `queued` 的扫描直接 409，并说清原因。
    """
    scan = service.get_scan(db, scanId)

    if scan.status != ScanStatus.QUEUED.value:
        # 只有还没被调度器认领的扫描才收附件（见 docstring）
        raise ApiError.conflict(
            "SCAN_ALREADY_STARTED",
            "这次扫描已经开始，附件无法再送进它的工作区了。"
            "请把附件留着，下次发起扫描时一起上传。",
        )

    if len(files) > uploads.MAX_COUNT:
        raise ApiError.validation(
            f"一次最多上传 {uploads.MAX_COUNT} 个附件",
            {"files": f"收到了 {len(files)} 个"},
        )

    created: list[ScanAttachmentOut] = []
    for item in files:
        content = await item.read()
        name, size = uploads.store(
            scan_id=scan.id, filename=item.filename or "attachment", content=content
        )
        created.append(ScanAttachmentOut(name=name, sizeBytes=size))

    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.SCAN_ATTACH,
        # ⚠️ 只记**文件名与数量**，不记内容。
        # 附件是用户上传的业务数据（题目二进制、可能含凭据的配置），
        # 而审计表的保留期远长于扫描产物。
        summary=f"给扫描 {scan.id} 上传了 {len(created)} 个任务附件",
        target=scan.id,
        detail={"names": [row.name for row in created]},
    )
    return created


@router.delete(
    "/{scanId}/attachments/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除任务附件",
)
def delete_scan_attachment(  # noqa: N803
    scanId: str,  # noqa: N803
    name: str,
    request: Request,
    identity: ScanDep,
    db: DbDep,
) -> Response:
    """删掉一个任务附件。同样只允许在扫描启动之前。"""
    scan = service.get_scan(db, scanId)
    if scan.status != ScanStatus.QUEUED.value:
        raise ApiError.conflict(
            "SCAN_ALREADY_STARTED",
            "这次扫描已经开始，它的工作区不再接受改动。",
        )
    uploads.delete_for(scanId, name)
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.SCAN_ATTACH_DELETE,
        summary=f"删除扫描 {scan.id} 的任务附件",
        target=scan.id,
        detail={"name": name},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)