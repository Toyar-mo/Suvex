"""凭据台账服务（v1.83）。"""

from __future__ import annotations

from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.errors import ApiError
from app.core.ids import new_id
from app.credentials.models import KINDS, Credential


def _validate(kind: str, target: str, secret: str) -> None:
    if kind not in KINDS:
        raise ApiError.validation(f"kind 只能是 {' / '.join(KINDS)}")
    if not (target or "").strip():
        raise ApiError.validation("target 不能为空")
    if not (secret or "").strip():
        raise ApiError.validation("secret 不能为空")


def create(
    db: Session,
    *,
    kind: str,
    username: str,
    secret: str,
    target: str,
    scan_id: str | None = None,
    finding_id: str | None = None,
    source: str = "",
    notes: str = "",
) -> Credential:
    _validate(kind, target, secret)
    row = Credential(
        id=new_id("cred"),
        scan_id=scan_id,
        finding_id=finding_id,
        kind=kind,
        username=(username or "").strip()[:200],
        target=target.strip()[:512],
        secret_token=encrypt_secret(secret),
        source=(source or "").strip()[:120],
        notes=(notes or "").strip()[:500],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_items(
    db: Session, *, target: str | None = None, scan_id: str | None = None
) -> list[Credential]:
    stmt = select(Credential).order_by(Credential.created_at.desc())
    if target:
        stmt = stmt.where(Credential.target == target.strip())
    if scan_id:
        stmt = stmt.where(Credential.scan_id == scan_id)
    return list(db.execute(stmt).scalars())


def reveal(db: Session, credential_id: str) -> str:
    row = db.get(Credential, credential_id)
    if row is None:
        raise ApiError.not_found("凭据不存在")
    return decrypt_secret(row.secret_token)


def _host(target: str) -> str:
    raw = (target or "").strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        return (urlsplit(raw).hostname or "").lower()
    except ValueError:
        return ""


def _norm(target: str) -> str:
    """同一目标 = 同 host:port；路径不同不算不同目标。"""
    raw = (target or "").strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port else ""
    return host + port


def reuse_plan(db: Session, credential_id: str) -> dict:
    """按“同目标 / 同主机 / 同扫描”给一个粗粒度复用分。

    分数只用于排序候选，不是权限判断；重复使用仍要人工确认。
    """
    row = db.get(Credential, credential_id)
    if row is None:
        raise ApiError.not_found("凭据不存在")
    others = list(
        db.execute(select(Credential).where(Credential.id != row.id)).scalars()
    )
    same_target = [item for item in others if _norm(item.target) == _norm(row.target)]
    same_host = [
        item
        for item in others
        if _host(item.target) == _host(row.target) and item not in same_target
    ]
    same_scan = [
        item
        for item in others
        if item.scan_id and item.scan_id == row.scan_id and item not in same_target
    ]

    basis: list[str] = []
    score = 0.2
    if same_target:
        score = 0.9
        basis.append(f"同一目标已有 {len(same_target)} 条凭据")
    elif same_host:
        score = 0.7
        basis.append(f"同一主机（不同端口/路径）已有 {len(same_host)} 条凭据")
    elif same_scan:
        score = 0.5
        basis.append(f"同一次扫描内已有 {len(same_scan)} 条凭据")
    else:
        basis.append("没有同目标/同主机的记录，只能作为低置信候选")

    return {
        "score": score,
        "basis": basis,
        "sameTarget": len(same_target),
        "sameHost": len(same_host),
        "sameScan": len(same_scan),
    }
