"""`reports` 领域路由（契约 §3.5，任务 6）。

| 方法   | 路径                     | 鉴权 |
| ------ | ------------------------ | ---- |
| GET    | `/reports`               | read |
| POST   | `/reports`               | read |
| POST   | `/reports/preview`       | read |
| GET    | `/reports/{id}`          | read |
| GET    | `/reports/{id}/document` | read |
| GET    | `/reports/{id}/download` | read |
| DELETE | `/reports/{id}`          | **admin** |

**除 `DELETE` 外都是 `read` 级**：报告是**从已有数据派生**的读操作，
不改变任何漏洞或扫描的状态，也不发起扫描、不发包 —— 所以不需要 `scan` 级。

⚠️ **`DELETE` 是例外，它要 `admin`**（契约 §3.5 的 v0.10 修正）。
原来的 `read` 与本契约 §1.2 对 `read` 的定义（「只读」）自相矛盾，
而且它是这一组里唯一不可逆的动作。见 `docs/security-checklist.md` 的 A-03。

⚠️ **路由顺序**：`/reports/preview` 必须声明在 `/reports/{reportId}` **之前**，
否则 `preview` 会被当成一个 report id 匹配掉。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import Identity, require_admin, require_read
from app.core.db import get_db
from app.core.vocab import AuditAction
from app.reports import service
from app.reports.schemas import (
    ReportDocumentOut,
    ReportGenerateRequest,
    ReportOut,
    ReportPreviewRequest,
)

logger = logging.getLogger("suvex.reports")

router = APIRouter(prefix="/reports", tags=["报告"])

ReadDep = Annotated[Identity, Depends(require_read)]
# ★ 删除是这一组里唯一的销毁型操作，所以是 `admin` 而不是 `read`
#   （契约 §3.5 的 v0.10 修正，任务 8 的鉴权排查发现）。
AdminDep = Annotated[Identity, Depends(require_admin)]
DbDep = Annotated[Session, Depends(get_db)]


@router.get("", response_model=list[ReportOut], summary="历史报告列表")
def list_reports(_identity: ReadDep, db: DbDep) -> list[dict[str, Any]]:
    return [service.to_out(report) for report in service.list_reports(db)]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ReportOut,
    summary="生成一份报告（落库并冻结内容）",
)
def create_report(
    payload: ReportGenerateRequest, _identity: ReadDep, db: DbDep
) -> dict[str, Any]:
    """生成并**冻结**。

    冻结三样：`findingIds`（含顺序）、`excludedCount`、`severityCounts`。
    之后漏洞怎么变，报告的内容都不变 —— 它是一份**交付物**。

    ⚠️ `format: "pdf"` 时**不会**产出 PDF 文件（契约 §3.5.1 定的：
    本轮 PDF 走浏览器打印）。记录 `format` 是为了让历史列表能显示
    "这份当初是按 PDF 交付的"，而下载端点只给 markdown。
    """
    report = service.generate(
        db,
        title=payload.title,
        scope=payload.scope.to_domain(),
        options=payload.options.to_domain(),
        fmt=payload.format,
    )
    return service.to_out(report)


# ⚠️ 必须在 `/{reportId}` **之前**声明。
@router.post("/preview", response_model=ReportDocumentOut, summary="预览（不落库）")
def preview_report(
    payload: ReportPreviewRequest, _identity: ReadDep, db: DbDep
) -> dict[str, Any]:
    """预览：返回 `ReportDocument` IR，**不落库**。

    ★ `meta.code` 是**空串**，`meta.provisional` 是 `true`。
    这不是占位符，是如实的表达：**编号由生成动作分配，生成之前它不存在。**

    前端渲染器见到 `provisional` 要显示"生成后分配"。草稿若显示一个
    "将来的编号"，用户会看到预览 RPT-...-3253、下载得到 RPT-...-3310 ——
    而编号正是客户用来对账的东西。

    ⚠️ **预览与生成必须走同一份 IR**（契约 §3.5.1）：两者都调
    `ir.build_document`，只有 `provisional` 与 `reportId` 不同。
    写两套模板必然漂移 —— 那正是前端整套报告 IR 要消灭的东西。
    """
    return service.preview(
        db,
        title=payload.title,
        scope=payload.scope.to_domain(),
        options=payload.options.to_domain(),
    )


@router.get("/{reportId}", response_model=ReportOut, summary="报告详情（含冻结的 findingIds）")
def read_report(reportId: str, _identity: ReadDep, db: DbDep) -> dict[str, Any]:  # noqa: N803
    return service.to_out(service.get_report(db, reportId))


@router.get(
    "/{reportId}/document",
    response_model=ReportDocumentOut,
    summary="复现报告文档（用冻结的快照重建）",
)
def read_document(reportId: str, _identity: ReadDep, db: DbDep) -> dict[str, Any]:  # noqa: N803
    """按**冻结的 `findingIds`** 重建文档，而不是按 scope 重新过滤。

    按 scope 重算的话，三个月后回看会看到"和当初不一样"的内容 ——
    期间修好的漏洞消失、新发现的凭空出现。那就是同一份交付物有两个版本。

    这个端点给前端的"查看历史报告预览"用；下载走 `/{id}/download`。
    """
    return service.document_for(db, service.get_report(db, reportId))


@router.get("/{reportId}/download", summary="下载（本轮只支持 format=markdown）")
def download_report(  # noqa: N803
    reportId: str, request: Request, identity: ReadDep, db: DbDep
) -> Response:
    """下载。

    * `format=markdown` → `200` + `text/markdown` + 用**报告编号**命名的附件
    * `format=pdf` → 契约 §3.5.1 定了本轮由**前端走浏览器打印**，
      所以这里不产出 PDF。但真被调到时不报错，而是如实回一句说明 ——
      报 404 会让前端以为"这份报告不存在"，而它存在，只是没有服务端 PDF。

    ⚠️ **文件名用编号不用标题**（契约 §3.5）：标题是中文、可能带空格和斜杠，
    直接当文件名会出现编码差异甚至被截断。编号是 ASCII 且天然唯一，
    客户拿到文件后也能和正文里的编号对上。
    """
    report = service.get_report(db, reportId)
    if report.format == "pdf":
        # 不用 4xx：这不是"客户端错了"，也不是"资源不存在"。
        # 200 + 一句说明，前端按 format 分发的逻辑本来就不会调到这里。
        return Response(
            content=(
                "本报告当初按 PDF 交付。按契约 §3.5.1 的决策，"
                "PDF 由前端用浏览器打印引擎产出，服务端不提供 PDF 文件。\n"
                f"报告编号：{report.code}\n"
                "请在报告预览页使用「打印 / 另存为 PDF」。\n"
            ),
            media_type="text/plain; charset=utf-8",
            headers={"X-Suvex-Note": "pdf-via-browser-print"},
        )

    filename, content = service.render_for_download(db, report)
    # ★ 审计留痕（契约 §3.9）：报告一旦被下载就不受控了，
    # "谁把哪一份带出去了"是事后必须能回答的问题。
    # 只在**真的产出了文件**时记 —— 上面 PDF 那条分支只回一句说明，
    # 没有任何数据离开系统，记进去会让审计里混入没有后果的条目。
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.REPORT_DOWNLOAD,
        summary=f"下载了报告 {report.code}「{report.title}」",
        target=report.code,
        detail={"reportId": report.id, "format": report.format, "bytes": len(content)},
    )
    # 编号是纯 ASCII，所以不需要 RFC 5987 的 `filename*`。
    # 但仍然带 `filename=`，因为有些客户端只认它。
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/{reportId}", status_code=status.HTTP_204_NO_CONTENT, summary="删除报告")
def delete_report(  # noqa: N803
    reportId: str, request: Request, identity: AdminDep, db: DbDep
) -> None:
    """删除。

    级联删掉 `report_findings` 的关联行（模型上配了
    `cascade="all, delete-orphan"`）—— 留下悬空关联会让复现时
    读到一堆找不到的 id。
    """
    # 先取编号与标题：删完就查不到了，而审计要记"删的是哪一份"。
    # 记编号而不是 id 是刻意的 —— 编号是人拿去对账的东西。
    report = service.get_report(db, reportId)
    code, title = report.code, report.title
    service.delete_report(db, reportId)
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.REPORT_DELETE,
        summary=f"删除了报告 {code}「{title}」",
        target=code,
        detail={"reportId": reportId},
    )
