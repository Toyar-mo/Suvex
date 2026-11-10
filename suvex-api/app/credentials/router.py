"""凭据台账端点（v1.83）。

| 方法 | 路径 | 鉴权 |
| --- | --- | --- |
| POST | `/credentials` | scan |
| GET | `/credentials` | read |
| POST | `/credentials/{id}/reveal` | admin（写审计） |
| GET | `/credentials/{id}/reuse-plan` | read |
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import Identity, require_admin, require_read, require_scan
from app.core.db import get_db
from app.core.errors import ApiError
from app.credentials import service
from app.credentials.schemas import (
    CredentialIn,
    CredentialOut,
    CredentialPatch,
    ReusePlanOut,
    RevealOut,
)

router = APIRouter(prefix="/credentials", tags=["凭据"])

ReadDep = Annotated[Identity, Depends(require_read)]
ScanDep = Annotated[Identity, Depends(require_scan)]
AdminDep = Annotated[Identity, Depends(require_admin)]
DbDep = Annotated[Session, Depends(get_db)]


@router.post("", response_model=CredentialOut, status_code=201, summary="登记凭据")
def create_credential(
    payload: CredentialIn,
    _identity: ScanDep,
    db: DbDep,
) -> CredentialOut:
    return CredentialOut.from_row(
        service.create(
            db,
            kind=payload.kind,
            username=payload.username,
            secret=payload.secret,
            target=payload.target,
            scan_id=payload.scanId,
            finding_id=payload.findingId,
            source=payload.source,
            notes=payload.notes,
        )
    )


@router.get("", response_model=list[CredentialOut], summary="凭据列表（脱敏）")
def list_credentials(
    _identity: ReadDep,
    db: DbDep,
    target: Annotated[str | None, Query(max_length=512)] = None,
    scanId: Annotated[str | None, Query(max_length=64)] = None,  # noqa: N803
) -> list[CredentialOut]:
    return [
        CredentialOut.from_row(row)
        for row in service.list_items(db, target=target, scan_id=scanId)
    ]


@router.post("/{credentialId}/reveal", response_model=RevealOut, summary="读出明文（admin + 审计）")
def reveal_credential(
    credentialId: str,  # noqa: N803
    identity: AdminDep,
    request: Request,
    db: DbDep,
) -> RevealOut:
    secret = service.reveal(db, credentialId)
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action="credential.reveal",
        summary="读取了一条凭据的明文",
        target=credentialId,
    )
    return RevealOut(secret=secret)


@router.get(
    "/{credentialId}/reuse-plan",
    response_model=ReusePlanOut,
    summary="凭据复用评分（粗粒度候选）",
)
def reuse_plan(
    credentialId: str,  # noqa: N803
    _identity: ReadDep,
    db: DbDep,
) -> ReusePlanOut:
    return ReusePlanOut.model_validate(service.reuse_plan(db, credentialId))


@router.delete("/{credentialId}", summary="删除一条凭据")
def delete_credential(
    credentialId: str,  # noqa: N803
    identity: ScanDep,
    request: Request,
    db: DbDep,
) -> dict[str, bool]:
    if not service.delete(db, credentialId):
        raise ApiError.not_found("凭据不存在")
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action="credential.delete",
        summary="删除了一条凭据",
        target=credentialId,
    )
    return {"deleted": True}


@router.patch("/{credentialId}", response_model=CredentialOut, summary="修改凭据（secret 留空则不变）")
def update_credential(
    credentialId: str,  # noqa: N803
    payload: CredentialPatch,
    identity: ScanDep,
    request: Request,
    db: DbDep,
) -> CredentialOut:
    row = service.update(
        db,
        credentialId,
        kind=payload.kind,
        username=payload.username,
        target=payload.target,
        source=payload.source,
        notes=payload.notes,
        secret=payload.secret,
    )
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action="credential.update",
        summary="修改了一条凭据",
        target=credentialId,
        detail={"fields": list(payload.model_dump(exclude_none=True))},
    )
    return CredentialOut.from_row(row)
