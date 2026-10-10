"""处理人名单路由（契约 §3.10）。

| 方法   | 路径                  | 鉴权           |
| ------ | --------------------- | -------------- |
| GET    | `/members`            | **read**       |
| POST   | `/members`            | **admin**      |
| PATCH  | `/members/{id}`       | **admin**      |
| DELETE | `/members/{id}`       | **admin**      |

读要放宽到 `read`：漏洞列表上"负责人"那一列读的就是这批数据，
而列表页本身是 `read` 级别 —— 如果名单要求 `admin`，
一个只有 `read` 的机器令牌拉列表时就会因为解析不出负责人名字而失败。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import Identity, require_admin, require_read
from app.core.db import get_db
from app.core.vocab import AuditAction
from app.members import service
from app.members.schemas import (
    MemberCreateIn,
    MemberDeleteResult,
    MemberOut,
    MemberUpdateIn,
)

router = APIRouter(prefix="/members", tags=["处理人"])


@router.get("", response_model=list[MemberOut], summary="处理人名单")
def list_members(
    _identity: Annotated[Identity, Depends(require_read)],
    db: Annotated[Session, Depends(get_db)],
    includeInactive: bool = True,  # noqa: N803 - 与前端查询参数一致
) -> list[MemberOut]:
    """默认**含已停用**。

    历史指派要能显示名字，而界面上"负责人"那一列读的就是这批数据。
    只想要候选（下拉框）时传 `includeInactive=false`。
    """
    return [
        service.to_out(member)
        for member in service.list_members(db, include_inactive=includeInactive)
    ]


@router.post(
    "",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
    summary="新增处理人",
)
def create_member(
    payload: MemberCreateIn,
    request: Request,
    identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> MemberOut:
    member = service.create_member(db, name=payload.name, email=payload.email)
    # ★ 留痕。成员名单决定"漏洞归谁"，而"谁把人加进来的"是
    # 事后追溯职责边界时必须能回答的问题。
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.MEMBER_CREATE,
        summary=f"新增处理人「{member.name}」",
        target=member.id,
    )
    return service.to_out(member)


@router.patch("/{memberId}", response_model=MemberOut, summary="改名 / 改邮箱 / 停用启用")
def update_member(
    memberId: str,  # noqa: N803 - 路径参数按契约用 camelCase
    payload: MemberUpdateIn,
    request: Request,
    identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> MemberOut:
    member = service.update_member(
        db,
        memberId,
        name=payload.name,
        email=payload.email,
        active=payload.active,
    )
    # 只在与原来不同时才记 —— 空操作不该进审计（同 §3.4 的判定）
    if payload.active is not None:
        summary = f"{'启用' if member.active else '停用'}处理人「{member.name}」"
    else:
        summary = f"更新处理人「{member.name}」"
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.MEMBER_UPDATE,
        summary=summary,
        target=member.id,
        detail={"active": member.active, "name": member.name},
    )
    return service.to_out(member)


@router.delete(
    "/{memberId}",
    response_model=MemberDeleteResult,
    summary="删除处理人（会解除其名下漏洞的指派）",
)
def delete_member(
    memberId: str,  # noqa: N803
    request: Request,
    identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> MemberDeleteResult:
    """硬删。

    ★ 会**解除**该处理人名下所有漏洞的指派（`ON DELETE SET NULL`），
    并把这个条数**返回给调用方** —— 见 `MemberDeleteResult` 的说明：
    204 会让那件事无声发生，而实测证明用户完全看不出来。

    真想让名字留在历史里，应该用 `PATCH {"active": false}` 停用，
    而不是删。
    """
    affected = service.delete_member(db, memberId)
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.MEMBER_DELETE,
        summary=f"删除处理人（{affected} 条漏洞被解除指派）",
        target=memberId,
        detail={"unassigned": affected},
    )
    return MemberDeleteResult(unassigned=affected)
