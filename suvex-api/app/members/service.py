"""处理人名单的业务逻辑。

三条规则值得单独说，因为它们都是"不加就会在别处变成 bug"的那类：

1. **名字唯一**（strip 之后）。数据库上有个 `uq_members_name` 唯一约束兜底，
   但这里要先查一次并给出**说人话的错误** —— 让用户看到
   `IntegrityError` 的英文约束名，他只会以为系统坏了。

2. **停用而不是删除**。见 `models.Member` 的 docstring：删掉会让历史指派
   突然变成"未指派"，抹掉一段真实的协作记录。

3. **删除前要问一句"还有人指着它吗"**（`assigned_count`）。硬删时
   `ON DELETE SET NULL` 会把那些漏洞静默变成未指派 —— 数据库层面没问题，
   但对用户是信息丢失，所以接口在删之前把这个数字告诉他。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.ids import new_id
from app.members.models import MAX_MEMBERS, Member
from app.members.schemas import MemberOut


def _out(member: Member) -> MemberOut:
    return MemberOut(
        id=member.id,
        name=member.name,
        email=member.email,
        active=member.active,
        createdAt=member.created_at,
    )


def to_out(member: Member) -> MemberOut:
    return _out(member)


def list_members(db: Session, *, include_inactive: bool = True) -> list[Member]:
    """名单。

    默认**含已停用**：历史指派要能显示名字，而界面上"负责人"那一列
    读的就是这批数据。只想要候选（下拉框）时传 `include_inactive=False`。
    """
    stmt = select(Member)
    if not include_inactive:
        stmt = stmt.where(Member.active.is_(True))
    # 按名字排，不按创建时间：名单是给人扫的，字典序才找得到人
    return list(db.execute(stmt.order_by(Member.name)).scalars())


def get_member(db: Session, member_id: str) -> Member:
    member = db.get(Member, member_id)
    if member is None:
        raise ApiError.not_found(f"处理人 {member_id}")
    return member


def _ensure_name_free(db: Session, name: str, *, except_id: str | None = None) -> None:
    stmt = select(Member).where(Member.name == name)
    if except_id:
        stmt = stmt.where(Member.id != except_id)
    if db.execute(stmt).scalar_one_or_none() is not None:
        raise ApiError.validation(
            f"已经有一个叫「{name}」的处理人了。",
            {"name": "名字重复 —— 同名会让「按负责人筛选」被拆成两组"},
        )


def create_member(db: Session, *, name: str, email: str | None) -> Member:
    clean = name.strip()
    if not clean:
        raise ApiError.validation("处理人姓名不能为空。", {"name": "不能只有空格"})

    total = db.execute(select(func.count()).select_from(Member)).scalar_one()
    if total >= MAX_MEMBERS:
        raise ApiError.validation(f"处理人最多 {MAX_MEMBERS} 个。")

    _ensure_name_free(db, clean)

    member = Member(id=new_id("member"), name=clean, email=(email or "").strip() or None)
    db.add(member)
    try:
        db.commit()
    except IntegrityError as exc:
        # 唯一约束兜底：上面那次 SELECT 与这次 INSERT 之间有并发窗口，
        # 两个请求可能同时通过检查。数据库是最后一道，但错误要说人话。
        db.rollback()
        raise ApiError.validation(
            f"已经有一个叫「{clean}」的处理人了。", {"name": "名字重复"}
        ) from exc
    db.refresh(member)
    return member


def update_member(
    db: Session,
    member_id: str,
    *,
    name: str | None,
    email: str | None,
    active: bool | None,
) -> Member:
    member = get_member(db, member_id)

    if name is not None:
        clean = name.strip()
        if not clean:
            raise ApiError.validation("处理人姓名不能为空。", {"name": "不能只有空格"})
        if clean != member.name:
            _ensure_name_free(db, clean, except_id=member.id)
            member.name = clean

    if email is not None:
        member.email = email.strip() or None

    if active is not None:
        member.active = active

    db.commit()
    db.refresh(member)
    return member


def assigned_count(db: Session, member_id: str) -> int:
    """这个处理人手上还有几条漏洞（**含已关闭/误报**）。

    不过滤状态：这个数字的用途是"删之前告诉你会影响多少条记录"，
    而关闭与误报的条目同样会因为删除而失去负责人。
    """
    from app.findings.models import Finding

    return db.execute(
        select(func.count()).select_from(Finding).where(Finding.assignee_id == member_id)
    ).scalar_one()


def delete_member(db: Session, member_id: str) -> int:
    """硬删。返回**被解除指派的漏洞条数**。

    `ON DELETE SET NULL` 会让那些漏洞变成"未指派" —— 这是正确的
    （人走了，漏洞还在），但调用方要把这个数字回给用户，
    否则那些指派是在无人知晓的情况下消失的。
    """
    member = get_member(db, member_id)
    affected = assigned_count(db, member_id)
    db.delete(member)
    db.commit()
    return affected
