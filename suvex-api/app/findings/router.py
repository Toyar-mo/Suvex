"""`findings` 领域路由（契约 §3.4，任务 5）。

| 方法 | 路径               | 鉴权     |
| ---- | ------------------ | -------- |
| GET  | `/findings`        | read     |
| GET  | `/findings/stats`  | read     |
| POST | `/findings/status` | **scan** |
| GET  | `/findings/{id}`   | read     |

`POST /findings/status` 是 `scan` 级而不是 `read`：它**改变数据**，
而 `read` 的定义是"只读"。给它更低级别的话，一个只读令牌就能把
一堆漏洞全标成误报 —— 而那是**掩盖问题**，比读到数据严重得多。

⚠️ **路由顺序**：`/findings/stats` 必须声明在 `/findings/{findingId}` **之前**，
否则 `stats` 会被当成一个 finding id 匹配掉，
而症状是「统计接口返回 404 找不到漏洞 stats」。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import Identity, require_read, require_scan
from app.core.db import get_db
from app.core.errors import ApiError
from app.core.timeutil import now_utc
from app.core.vocab import AuditAction, FindingCategory, FindingStatus, Severity, values
from app.findings import attachments as attachments_mod
from app.findings import sarif as sarif_mod
from app.findings import service
from app.findings.schemas import (
    AssignRequest,
    AssignResponse,
    AttachmentOut,
    BatchIdsRequest,
    FindignStatusResponse,
    FindingDeletedResponse,
    FindingGroupOut,
    FindingOut,
    FindingStatsOut,
    FindingSummaryOut,
    StatusChangeRequest,
)

router = APIRouter(prefix="/findings", tags=["漏洞"])

ReadDep = Annotated[Identity, Depends(require_read)]
ScanDep = Annotated[Identity, Depends(require_scan)]
DbDep = Annotated[Session, Depends(get_db)]


def _validate_multi(field: str, value: str | None, allowed: tuple[str, ...]) -> None:
    """校验一个（可能是逗号分隔的）多值筛选参数。

    ★ **逐个取值校验**，不是校验整串。

    校验整串的话 `?severity=high,low` 会因为 `"high,low"` 不在枚举里
    而被 400 —— 而那条错误消息看起来像"这个取值不存在"，
    不是"你不该用逗号"。逐个校验才能给出准确的提示。

    ⚠️ 一定要在**路由层**校验而不是只在 service 里：`_split_multi` 对
    非法值会照样拼出 `IN ('nonsense')`，那会静默筛出 0 条 ——
    而"筛出 0 条"和"这个筛选值写错了"在界面上长得一模一样。
    """
    if value is None:
        return
    for piece in value.split(","):
        token = piece.strip()
        if not token:
            continue
        if token not in allowed:
            raise ApiError.validation(
                f"{field} 取值不合法：{token!r}",
                {field: f"应为 {'、'.join(allowed)} 之一（多个用逗号分隔）"},
            )


@router.get("", response_model=list[FindingSummaryOut], summary="漏洞列表")
def list_findings(
    _identity: ReadDep,
    db: DbDep,
    severity: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query(description="按标题或目标模糊搜索")] = None,
    scanId: Annotated[str | None, Query()] = None,  # noqa: N803 - 契约里的参数名
    assigneeId: Annotated[str | None, Query(description="只看这个人名下的")] = None,  # noqa: N803
    unassigned: Annotated[bool, Query(description="只看还没指派负责人的")] = False,
    slaState: Annotated[  # noqa: N803
        str | None,
        Query(description="按修复时限筛：overdue / dueSoon / onTrack"),
    ] = None,
    deleted: Annotated[bool, Query(description="只看回收站里的（P6-a）")] = False,
    sort: Annotated[Literal["recent", "severity"], Query()] = "recent",
    dir: Annotated[Literal["asc", "desc"], Query()] = "desc",
) -> list[FindingSummaryOut]:
    """列表。

    返回的是**摘要**（不含 `detail` 与 `timeline`）：列表页一次可能拉
    几百条，每条都带上完整证据文本会让响应到几 MB，而列表并不渲染它们。
    要完整内容就按 id 取详情 —— 那也是详情页本来的做法。

    筛选值逐个校验而不是自由字符串：写错的值会得到 400，
    而不是"静默忽略然后返回全部"——后者会让用户以为筛选生效了。

    ⚠️ `severity` / `status` / `category` 支持**逗号分隔的多个取值**
    （v0.18）：界面的筛选是多选的，而导出必须能表达同一个筛选 ——
    否则用户勾了两个严重度再导出，要么报错、要么**悄悄只导出一个**。
    """
    _validate_multi("severity", severity, values(Severity))
    _validate_multi("status", status, values(FindingStatus))
    _validate_multi("category", category, values(FindingCategory))

    findings = service.list_findings(
        db,
        severity=severity,
        status=status,
        category=category,
        q=q,
        scan_id=scanId,
        assignee_id=assigneeId,
        unassigned=unassigned,
        sla_state=slaState,
        deleted=deleted,
        sort=sort,
        direction=dir,
    )
    # 走 `summaries` 而不是自己 map：它顺带把负责人名字批量装配好
    return service.summaries(db, findings)


# ⚠️ 必须在 `/{findingId}` **之前**声明，否则 `stats` 会被当成一个 id。
@router.get(
    "/stats",
    response_model=FindingStatsOut,
    # ★ `exclude_none=True` 才能让 `bySeverity` 真正满足契约的 `Partial` 语义。
    #
    # 契约 §3.4.2 写的是「为 0 的档**键不存在**」，但 `SeverityCounts` 的字段
    # 是 `int | None`，Pydantic 默认会把它们**序列化成显式的 null**：
    #     {"critical": 1, "high": null, "medium": null, ...}
    # 而不是
    #     {"critical": 1}
    #
    # 这是任务 5 遗留的一处偏离，是写 `/overview` 的测试时对账发现的。
    # 它一直没暴露，因为前端读写都兼容（`counts[sev] ?? 0`）——
    # 而**两边都接受**恰恰是它危险的地方：契约说"键不存在"，
    # 响应对不上，下一个按契约写解析逻辑的人就会踩空。
    #
    # ⚠️ 这个开关在这条路由上是**精确命中**的：`FindingStatsOut` 里
    # 只有 `bySeverity` 的字段可空（`byStatus` / `byCategory` 是
    # `dict[str, int]`，永不为 None），所以不会误伤别的字段。
    response_model_exclude_none=True,
    summary="各维度计数（唯一数据源）",
)
def finding_stats(  # noqa: PLR0913
    _identity: ReadDep,
    db: DbDep,
    scanId: Annotated[str | None, Query(description="只统计这一次扫描")] = None,  # noqa: N803
    severity: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    assigneeId: Annotated[str | None, Query()] = None,  # noqa: N803
    unassigned: Annotated[bool, Query()] = False,
    slaState: Annotated[str | None, Query()] = None,  # noqa: N803
    deleted: Annotated[bool, Query()] = False,
    facets: Annotated[  # noqa: N803
        bool,
        Query(
            description="角标口径：算某一维度时忽略该维度自己的筛选条件，"
            "这样筛了「高危」之后仍能切到「中危」（P6-b）"
        ),
    ] = False,
) -> FindingStatsOut:
    """★ **Findings 页与 Overview 页共用的统计**（任务 5 要求 4）。

    两页的角标与图表必须调它，不能在各自的页面里再数一遍 ——
    那样两次独立计数在并发编辑下会取到不同快照，而差值看起来像 bug，
    用户会以为其中一个是错的。

    `scanId` 可选：传了就只统计那一次扫描。
    """
    return service.stats(
        db,
        scan_id=scanId,
        severity=severity,
        status=status,
        category=category,
        q=q,
        assignee_id=assigneeId,
        unassigned=unassigned,
        sla_state=slaState,
        deleted=deleted,
        facets=facets,
    )


@router.post("/status", response_model=FindignStatusResponse, summary="批量状态流转")
def change_status(
    payload: StatusChangeRequest,
    request: Request,
    identity: ScanDep,
    db: DbDep,
) -> FindignStatusResponse:
    """批量流转（对应前端 Findings 页的批量操作）。

    三条规则见 `service.change_status`：非法流转 400、幂等跳过不写时间线、
    每次变更记录**操作人**与时间戳。

    响应里的 `changed` / `skipped` 是给前端提示"实际改了几条、跳过几条"的 ——
    全部选中项都已是目标状态时前端会禁用按钮，但后端仍要做幂等校验（双保险）。
    """
    changed, skipped = service.change_status(
        db,
        ids=payload.ids,
        target=payload.status,
        note=payload.note,
        identity=identity,
    )
    # ★ 只在**真的改了**的时候留痕（契约 §3.9）。
    #
    # 全量跳过（changed 为空）是幂等重放，不是一次操作 ——
    # 把它也记进去会让审计里塞满"改了 0 条"的噪音，
    # 而审计的价值恰恰在于每一条都对应一个真实的动作。
    if changed:
        audit.record(
            db,
            request=audit.from_http(request, identity),
            action=AuditAction.FINDING_STATUS_CHANGE,
            summary=(
                f"把 {len(changed)} 条漏洞的状态改为"
                f"「{service.STATUS_LABELS.get(payload.status, payload.status)}」"
            ),
            target=",".join(finding.id for finding in changed)[:512],
            detail={
                "targetStatus": payload.status,
                "changedIds": [finding.id for finding in changed],
                "skipped": skipped,
                "note": payload.note,
            },
        )
    return FindignStatusResponse(
        changed=len(changed),
        skipped=skipped,
        findings=service.summaries(db, changed),
    )


@router.post("/assign", response_model=AssignResponse, summary="批量指派负责人")
def assign(
    payload: AssignRequest,
    request: Request,
    identity: ScanDep,
    db: DbDep,
) -> AssignResponse:
    """批量指派（任务 10 P2）。

    ⚠️ **必须声明在 `/{findingId}` 之前**，否则 `assign` 会被当成一个
    finding id 匹配掉 —— 症状是「指派接口 404 说找不到叫 assign 的漏洞」。
    这个顺序陷阱在本文件顶部的 docstring 里已经记过一次（`/stats`）。

    鉴权用 `scan` 级（与 `/status` 一致）：它**改变数据**，
    而 `read` 的定义是只读。给只读令牌改指派，等于让一份只读凭据
    能重塑"谁负责什么"。

    `assigneeId: null` = **取消指派**（明确的操作，不是"没传"）。
    """
    changed, skipped = service.assign_findings(
        db,
        ids=payload.ids,
        assignee_id=payload.assigneeId,
        identity=identity,
    )
    # 只在真改了的时候留痕（同 §3.4 的判定）：全量跳过是幂等重放，
    # 把它也记进去会让审计塞满"改了 0 条"的噪音。
    if changed:
        name = service.assignee_name(db, payload.assigneeId) if payload.assigneeId else None
        audit.record(
            db,
            request=audit.from_http(request, identity),
            action=AuditAction.FINDING_ASSIGN,
            summary=(
                f"把 {len(changed)} 条漏洞指派给「{name}」"
                if name
                else f"取消了 {len(changed)} 条漏洞的指派"
            ),
            target=",".join(finding.id for finding in changed)[:512],
            detail={
                "assigneeId": payload.assigneeId,
                "assigneeName": name,
                "changedIds": [finding.id for finding in changed],
                "skipped": skipped,
            },
        )
    return AssignResponse(
        changed=len(changed),
        skipped=skipped,
        findings=service.summaries(db, changed),
    )


@router.get(
    "/groups",
    response_model=list[FindingGroupOut],
    summary="按漏洞类型分组（同一类问题影响了哪些目标）",
)
def group_findings(
    _identity: ReadDep,
    db: DbDep,
    severity: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    scanId: Annotated[str | None, Query()] = None,  # noqa: N803
    assigneeId: Annotated[str | None, Query()] = None,  # noqa: N803
    unassigned: Annotated[bool, Query()] = False,
) -> list[FindingGroupOut]:
    """分组视图的数据（任务 10 P4）。

    ⚠️ **必须声明在 `/{findingId}` 之前** —— 否则 `groups` 会被当成一个
    finding id，症状是「分组接口 404 说找不到叫 groups 的漏洞」。
    本文件顶部的 docstring 与 `/stats`、`/assign` 都记过同一个坑。

    **筛选参数与 `/findings` 完全一致**：分组视图不是另一套数据，
    它是同一批数据换个切法。参数不一致的话，用户切到分组视图会看到
    与列表不同的范围，而他会以为筛选失效了。
    """
    return [
        FindingGroupOut(**group)
        for group in service.group_findings(
            db,
            severity=severity,
            status=status,
            category=category,
            q=q,
            scan_id=scanId,
            assignee_id=assigneeId,
            unassigned=unassigned,
        )
    ]


# ══════════════════════════════════════════════════════════
# CSV 导出（任务 10 P6-c）
# ══════════════════════════════════════════════════════════
#
# ⚠️ 同样必须在 `/{findingId}` 之前 —— 否则 `export` 会被当成一个 id。


@router.get("/export", summary="导出漏洞为 CSV（P6-c）")
def export_findings(
    _identity: ReadDep,
    db: DbDep,
    severity: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    scanId: Annotated[str | None, Query()] = None,  # noqa: N803
    assigneeId: Annotated[str | None, Query()] = None,  # noqa: N803
    unassigned: Annotated[bool, Query()] = False,
    slaState: Annotated[str | None, Query()] = None,  # noqa: N803
    deleted: Annotated[bool, Query()] = False,
) -> Response:
    """导出**当前筛选结果**为 CSV。

    ## 筛选参数与 `/findings` 完全一致

    这是这个端点唯一的行为约定，也是它最容易被做错的地方：
    如果导出忽略了筛选，用户筛出 3 条高危、点导出却拿到全部 200 条，
    而他会把那份表发出去 —— **错误的数据比没有数据更糟**。

    所以参数逐个对齐 `/findings`（含 `deleted`），并且走**同一个**
    `list_findings`。加筛选参数时不可能只改一边。

    ## 为什么走后端而不是像扫描列表那样在前端生成

    `lib/scan-export.ts` 的选择是前端生成，理由是"数据全在列表里了"。
    漏洞这里不成立：列表返回的是**摘要**（契约 §3.4 明确不含 `detail`），
    而导出的价值有一半在"这条问题到底是什么"。前端要生成就得
    逐条再取详情（N 个请求）。后端一次查询就能给全。

    ⚠️ 但它**不发 `detail.evidence`**（原始响应片段）——
    见 `service.export_csv` 的说明。
    """
    rows = service.list_findings(
        db,
        severity=severity,
        status=status,
        category=category,
        q=q,
        scan_id=scanId,
        assignee_id=assigneeId,
        unassigned=unassigned,
        sla_state=slaState,
        deleted=deleted,
        sort="severity",
        direction="desc",
    )
    body = service.export_csv(db, rows=rows)
    stamp = now_utc().strftime("%Y%m%d")
    filename = f"suvex-findings-{stamp}.csv"
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            # ⚠️ 文件名用 ASCII（`suvex-findings-20260925.csv`）：
            # `Content-Disposition` 里放中文要按 RFC 5987 编码
            # （`filename*=UTF-8''...`），不编码时某些客户端会乱码或截断。
            # 这里不值得为文件名引入编码复杂度 —— 日期已经够区分。
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )


@router.get("/export/sarif", summary="导出漏洞为 SARIF 2.1.0（v0.70，对齐 strix）")
def export_findings_sarif(
    _identity: ReadDep,
    db: DbDep,
    severity: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    scanId: Annotated[str | None, Query()] = None,  # noqa: N803
    assigneeId: Annotated[str | None, Query()] = None,  # noqa: N803
    unassigned: Annotated[bool, Query()] = False,
    slaState: Annotated[str | None, Query()] = None,  # noqa: N803
    deleted: Annotated[bool, Query()] = False,
) -> Response:
    """导出**当前筛选结果**为 SARIF 2.1.0（CI / 代码扫描平台的通用输入）。

    ## 为什么和 CSV 对齐同一套筛选参数

    与 `/findings/export` 同一条纪律：导出若忽略筛选，用户筛出 3 条高危、
    点导出却拿到全部 200 条，而他会把那份发进 CI —— **错误的数据比没有
    数据更糟**。所以走同一个 `list_findings`，参数逐个对齐。

    ## 为什么这条路由要在 `/findings/{findingId}` 之前

    同 `/findings/stats`：`export/sarif` 里的 `export` 会被当成 finding id
    匹配掉 —— 因此它必须声明在 `{findingId}` 之前（本文件末尾的那些动态
    路由之前）。`/findings/export`（CSV）已在前面，SARIF 紧跟其后。
    """
    rows = service.list_findings(
        db,
        severity=severity,
        status=status,
        category=category,
        q=q,
        scan_id=scanId,
        assignee_id=assigneeId,
        unassigned=unassigned,
        sla_state=slaState,
        deleted=deleted,
        sort="severity",
        direction="desc",
    )
    body = sarif_mod.build_sarif(rows)
    stamp = now_utc().strftime("%Y%m%d")
    filename = f"suvex-findings-{stamp}.sarif"
    return Response(
        content=body.encode("utf-8"),
        # SARIF 的正式 MIME 是 application/sarif+json；很多接收方也认
        # application/json，但发规范的那个更稳。
        media_type="application/sarif+json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ══════════════════════════════════════════════════════════
# 软删除 / 恢复（任务 10 P6-a）
# ══════════════════════════════════════════════════════════


@router.post(
    "/batch/delete",
    response_model=FindingDeletedResponse,
    summary="批量软删除（进回收站）",
)
def batch_delete(
    payload: BatchIdsRequest,
    request: Request,
    identity: ScanDep,
    db: DbDep,
) -> FindingDeletedResponse:
    """**软删除** —— 数据一条都不丢，只打 `deletedAt` 标记。

    鉴权是 **scan** 级：它改变数据。`read` 的定义是"只读"，
    给更低级别的话一个只读令牌就能把一批漏洞从列表里抹掉。
    """
    changed, skipped = service.batch_set_deleted(db, ids=payload.ids)
    if changed:
        audit.record(
            db,
            request=audit.from_http(request, identity),
            action=AuditAction.FINDING_DELETE,
            summary=f"把 {len(changed)} 条漏洞移入回收站",
            target=",".join(f.id for f in changed)[:512],
            detail={"changedIds": [f.id for f in changed], "skipped": skipped},
        )
    return FindingDeletedResponse(
        changed=len(changed),
        skipped=skipped,
        findings=service.summaries(db, changed),
    )


@router.post(
    "/batch/restore",
    response_model=FindingDeletedResponse,
    summary="从回收站恢复",
)
def batch_restore(
    payload: BatchIdsRequest,
    request: Request,
    identity: ScanDep,
    db: DbDep,
) -> FindingDeletedResponse:
    """从回收站恢复。**不动处置状态** —— 恢复的是"记录还在"。"""
    changed, skipped = service.batch_restore(db, ids=payload.ids)
    if changed:
        audit.record(
            db,
            request=audit.from_http(request, identity),
            action=AuditAction.FINDING_RESTORE,
            summary=f"从回收站恢复 {len(changed)} 条漏洞",
            target=",".join(f.id for f in changed)[:512],
            detail={"changedIds": [f.id for f in changed], "skipped": skipped},
        )
    return FindingDeletedResponse(
        changed=len(changed),
        skipped=skipped,
        findings=service.summaries(db, changed),
    )


@router.delete(
    "/{findingId}/purge",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="永久删除（回收站内）",
)
def purge_finding(  # noqa: N803
    findingId: str,  # noqa: N803
    request: Request,
    identity: ScanDep,
    db: DbDep,
) -> Response:
    """**永久删除**（物理）。只在回收站内可用，且被报告引用时拒绝。

    两道闸的理由见 `service.purge_finding` ——
    一次误点的 `rm -rf` 与"先删进回收站再确认"是两件事。
    """
    service.purge_finding(db, findingId)
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.FINDING_PURGE,
        summary="永久删除一条漏洞（不可逆）",
        target=findingId,
        detail={"findingId": findingId},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ══════════════════════════════════════════════════════════
# 附件（任务 10 P6-d）
# ══════════════════════════════════════════════════════════


def _attachment_out(row) -> AttachmentOut:  # noqa: ANN001
    return AttachmentOut(
        id=row.id,
        findingId=row.finding_id,
        filename=row.filename,
        contentType=row.content_type,
        sizeBytes=row.size_bytes,
        uploadedAt=row.uploaded_at,
        uploadedBy=row.uploaded_by,
    )


@router.get(
    "/{findingId}/attachments",
    response_model=list[AttachmentOut],
    summary="附件列表",
)
def list_attachments(findingId: str, _identity: ReadDep, db: DbDep) -> list[AttachmentOut]:  # noqa: N803
    """一条漏洞的附件。**按上传时间正序**（证据的完整序列）。"""
    # 先确认漏洞存在 —— 否则对不存在的 id 会返回空列表，
    # 而"这条漏洞没有附件"与"这条漏洞不存在"是两件事。
    service.get_finding(db, findingId)
    return [_attachment_out(row) for row in attachments_mod.list_for(db, findingId)]


@router.post(
    "/{findingId}/attachments",
    response_model=list[AttachmentOut],
    status_code=status.HTTP_201_CREATED,
    summary="上传附件",
)
async def upload_attachments(
    findingId: str,  # noqa: N803
    request: Request,
    identity: ScanDep,
    db: DbDep,
    files: Annotated[list[UploadFile], File(description="图片 / PDF / 文本")],
) -> list[AttachmentOut]:
    """上传一个或多个附件。

    ⚠️ 这是 `async` 端点，而本文件其余都是同步 `def`。
    原因：`UploadFile.read()` 是 async 的（Starlette 的 `UploadFile`
    把请求体缓存在 `SpooledTemporaryFile` 里，读它要走线程池）。
    写成同步 def 也能跑，但会在事件循环里做阻塞 IO，而上传正是
    最不该阻塞的地方。

    ## 三道闸

    1. **数量上限**（`attachment_max_count`）：一次传 500 个会占满磁盘。
    2. **大小上限**：按**真实字节数**判，在 `attachments.store` 里 ——
       路由层按 `Content-Length` 判的那道可以被客户端撒谎绕过。
    3. **类型白名单**：见 `attachments.ALLOWED_EXTENSIONS`。

    ## 为什么一次可以传多个

    处置一条漏洞往往要传"修复前截图 + 修复后截图"。逐张上传意味着
    用户要点两次、看两次进度、失败时还要判断是哪张失败。
    批量上传让那次操作是一个原子动作（要么都传上去，要么报错）。
    """
    from app.core.config import get_settings

    finding = service.get_finding(db, findingId)

    settings = get_settings()
    if len(files) > settings.attachment_max_count:
        raise ApiError.validation(
            f"一次最多上传 {settings.attachment_max_count} 个附件",
            {"files": f"收到了 {len(files)} 个"},
        )

    created = []
    for upload in files:
        content = await upload.read()
        created.append(
            attachments_mod.store(
                db,
                finding_id=finding.id,
                filename=upload.filename or "未命名附件",
                content=content,
                content_type=upload.content_type or "",
                uploaded_by=getattr(identity, "user_id", None),
            )
        )

    # 附件本身不写时间线（它不是状态流转），但**要审计** ——
    # 那是"谁在什么时候把什么放进了这条记录"，属于留痕要求。
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.FINDING_ATTACH,
        summary=f"给漏洞上传了 {len(created)} 个附件",
        target=finding.id,
        detail={
            "findingId": finding.id,
            "attachmentIds": [row.id for row in created],
            "filenames": [row.filename for row in created],
        },
    )
    return [_attachment_out(row) for row in created]


@router.get("/{findingId}/attachments/{attachmentId}/content", summary="下载附件内容")
def download_attachment(  # noqa: N803
    findingId: str,  # noqa: N803
    attachmentId: str,  # noqa: N803
    _identity: ReadDep,
    db: DbDep,
) -> Response:
    """读回附件内容。

    ⚠️ **`Content-Type` 用存储时的值**（上传时按白名单判定过），
    并带 `Content-Disposition: attachment` —— 后者让浏览器**下载**
    而不是内联渲染。

    这一条对 `image/svg+xml` 尤其要紧：SVG 可以内嵌 `<script>`，
    内联渲染等于让一个上传的文件在本站的原点上执行。
    加 `attachment` 之后它只会被存下来，不会被执行。

    ⚠️ 路径来自 `stored_path`，且 `resolve_stored_path` 会验证它
    落在附件根之内 —— 见 `attachments.py` 的说明（纵深防御）。
    """
    row = attachments_mod.get_attachment(db, findingId, attachmentId)
    path = attachments_mod.resolve_stored_path(row.stored_path)
    if not path.exists():
        # 文件不在（被手工删过、或磁盘故障）。如实说，不要让前端
        # 收到一个空文件 —— 那看起来像"附件是空的"。
        raise ApiError.not_found("附件文件（记录还在，但磁盘上的文件不见了）")
    return Response(
        content=path.read_bytes(),
        media_type=row.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{_ascii_filename(row.filename)}"',
            # 附件是不可变内容（id 唯一、不会被改写），缓存它是安全的
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.delete(
    "/{findingId}/attachments/{attachmentId}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除附件",
)
def delete_attachment(  # noqa: N803
    findingId: str,  # noqa: N803
    attachmentId: str,  # noqa: N803
    request: Request,
    identity: ScanDep,
    db: DbDep,
) -> Response:
    """删除一个附件（文件 + 元信息）。"""
    attachments_mod.delete_attachment(db, findingId, attachmentId)
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.FINDING_ATTACH_DELETE,
        summary="删除了一个附件",
        target=findingId,
        detail={"findingId": findingId, "attachmentId": attachmentId},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _ascii_filename(name: str) -> str:
    """把文件名降级成 ASCII 安全的形态，用于 `Content-Disposition`。

    ⚠️ 中文文件名**不能**直接放进 `filename="..."` —— 某些客户端
    会乱码或截断。RFC 5987 的 `filename*=UTF-8''...` 是正解，
    但它要额外编码，且这里真正重要的是**内容本身**能被下载下来，
    文件名只是附带信息（用户在列表上已经看到了原名）。

    所以：非 ASCII 字符替换成 `_`，并保留扩展名 —— 至少让
    浏览器知道该用什么打开它。
    """
    unsafe = {'"', "\\"}
    safe = "".join(ch if ch.isascii() and ch not in unsafe else "_" for ch in name)
    safe = safe.strip(". ") or "attachment"
    return safe[:200]


@router.get("/{findingId}", response_model=FindingOut, summary="漏洞详情（含 detail + timeline）")
def read_finding(findingId: str, _identity: ReadDep, db: DbDep) -> FindingOut:  # noqa: N803
    """详情。**时间线是倒序的**（最新在前）—— 契约 §2.2 明确要求。"""
    return service.to_detail(service.get_finding(db, findingId))
