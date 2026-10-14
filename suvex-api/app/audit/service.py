"""审计日志的读写。

## 唯一入口是 `record()`

所有埋点都走它，好处是**脱敏只有一个地方要做**：审计记录会被长期保留，
把它写进去等于永久化，所以"这条路经不会带进凭据"必须在写入前就成立，
而不是指望读的时候过滤。

## 写失败绝不能让业务失败

与 `emit_lifecycle_event` 同一个判断：审计是**可观测性**，不是业务。
一次扫描已经真的启动了、一个漏洞状态已经真的改了 —— 这时候因为
审计表写不进去就把整个请求变成 500，用户会重试，而重试会**再做一遍
那件有后果的事**（再发一次扫描）。那比丢一条审计记录糟得多。

代价如实记：**审计可能缺条**。所以它在写失败时打 `exception` 级日志 ——
那条日志会进应用日志，而应用日志与审计日志两处都查不到的概率很低。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.audit.models import AuditLog
from app.auth.deps import Identity
from app.core.crypto import redact
from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.core.vocab import AUDIT_ACTION_LABELS, AuditAction

logger = logging.getLogger("suvex.audit")

# ★ 保留策略。与应用日志的**区别就在这里**：
#
# 应用日志按大小/天数轮转，通常几天到几周；审计记录保留 **365 天**。
# 为什么是这个数：安全事件的发现往往滞后（一次未被察觉的扫描可能
# 在几个月后的合规审计里才被问到），而一年的窗口能覆盖
# "上个财年的操作还能查"这个最常见的诉求。
#
# 它**不是**无限保留：单机自托管场景下，无限保留意味着磁盘最终被写满，
# 而"磁盘满"会让整个服务不可用 —— 那比"查不到两年前的记录"严重得多。
AUDIT_RETENTION_DAYS = 365


@dataclass(frozen=True, slots=True)
class AuditRequest:
    """一条审计记录的请求侧上下文。

    与 `Identity` 分开是因为它还要带上**来源**（IP / UA）——
    那两项不在 `Identity` 里（它是"谁"，不是"从哪来"）。
    """

    identity: Identity
    ip: str | None = None
    user_agent: str | None = None


def from_http(request: Any, identity: Identity) -> AuditRequest:
    """从 FastAPI 的 `Request` 组装上下文。

    存在的理由：来源 IP 必须走 `client_ip()`，而那个函数**刻意不读
    `X-Forwarded-For`**（可伪造）。每个埋点自己拼 IP 的话，
    迟早有人顺手写成 `request.headers["x-forwarded-for"]` ——
    于是审计里的来源变成攻击者自己填的值，而记录本身看起来完全正常。
    收敛到这一个函数，那条规矩就只有一处要守。
    """
    from app.auth.deps import client_ip

    return AuditRequest(
        identity=identity,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


def record(
    db: Session,
    *,
    request: AuditRequest,
    action: str,
    summary: str,
    target: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """写一条审计记录。**异常一律吞掉**（见模块 docstring）。

    `summary` 由调用方给：只有它知道"这次操作具体做了什么"
    （"对 api.example.com 发起了扫描" vs "把 2 条漏洞标为已确认"）。
    通用文案在这里拼不出来，硬拼会得到一串没有信息量的记录。
    """
    try:
        row = AuditLog(
            id=new_id("aud"),
            at=now_utc(),
            action=action,
            actor_kind=request.identity.kind,
            actor_label=request.identity.describe(),
            actor_id=request.identity.user_id or request.identity.token_id,
            target=(target or None),
            # ★ 过一遍脱敏。调用方**本来就不该**把凭据放进 summary/detail，
            # 但这里是最后一道 —— 审计是永久化的，漏一次就永远漏着。
            summary=redact(summary),
            ip=request.ip,
            user_agent=(request.user_agent or "")[:1000] or None,
            detail=_redact_detail(detail),
        )
        db.add(row)
        db.commit()
    except Exception:  # noqa: BLE001 - 见模块 docstring
        db.rollback()
        logger.exception(
            "写审计记录失败（业务操作本身已成功）：action=%s target=%s", action, target
        )


def _redact_detail(detail: dict[str, Any] | None) -> dict[str, Any] | None:
    """递归脱敏 `detail` 里的字符串。

    递归而不是只看顶层：`detail` 里的嵌套结构（比如漏洞 id 列表）同样
    可能被塞进一个 token。成本很低，而漏掉的代价是永久的。
    """
    if not detail:
        return None
    if isinstance(detail, dict):
        return {str(k): _redact_detail(v) for k, v in detail.items()}
    if isinstance(detail, (list, tuple)):
        return [_redact_detail(item) for item in detail]  # type: ignore[return-value]
    if isinstance(detail, str):
        return redact(detail)  # type: ignore[return-value]
    return detail  # type: ignore[return-value]


# ══════════════════════════════════════════════════════════
# 读
# ══════════════════════════════════════════════════════════


def list_entries(
    db: Session,
    *,
    limit: int = 50,
    cursor: str | None = None,
    action: str | None = None,
    actor: str | None = None,
    since: datetime | None = None,
) -> list[AuditLog]:
    """按时间倒序取，游标翻页。

    **用游标不用 offset**：审计表是持续增长的，而 `OFFSET` 在深分页时
    会越翻越慢（数据库要扫过前面所有行），且翻页期间新写入的记录
    会让相邻两页**重叠或漏条** —— 对一份用于追责的记录来说，
    "漏掉一条"是不可接受的。

    ⚠️ **游标必须同时用 `at` 与 `id`，不能只用 `id`。**
    `new_ulid()` 的前 48 位是毫秒时间戳，**字典序 = 时间序**只到毫秒一级 ——
    同一毫秒内产生的多条记录，后面的 80 位是随机的，相对顺序是随机的。
    而批量状态流转、以及扫描创建时同一请求内写多条，都可能在
    同一毫秒内写多条。只用 `id < cursor` 会**漏掉**同一毫秒里
    排在游标之后、但 id 更小的那些记录。
    所以用 `(at, id)` 元组比较，`id` 只作为同一时刻内的稳定次序。
    """
    stmt = select(AuditLog).order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(limit)
    if cursor:
        cursor_at, id_part = _parse_cursor(cursor)
        if cursor_at is not None and id_part:
            stmt = stmt.where(
                (AuditLog.at < cursor_at)
                | ((AuditLog.at == cursor_at) & (AuditLog.id < id_part))
            )
        else:
            # 形状不对的游标当成"从头开始"，而不是报错 ——
            # 一个坏游标不该让审计**查不出来**。
            logger.warning("审计游标形状不对，已忽略：%r", cursor)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if actor:
        # 匹配 label 而不是 id：读日志的人拿到的是"令牌「CI 流水线」"
        # 这样的称呼，而不是一个 ulid。模糊匹配是刻意的 ——
        # 精确匹配要求使用者先知道那个 id。
        stmt = stmt.where(AuditLog.actor_label.ilike(f"%{actor}%"))
    if since:
        stmt = stmt.where(AuditLog.at >= since)
    return list(db.execute(stmt).scalars())


def cursor_for(entry: AuditLog) -> str:
    """给下一页用的游标。形状见 `_parse_cursor`。"""
    return f"{entry.at.isoformat()}|{entry.id}"


def _parse_cursor(cursor: str) -> tuple[datetime | None, str]:
    """解析 `"<ISO 时刻>|<id>"`。

    ⚠️ **不用 `core.timeutil.to_iso` / `parse_iso`**：那两个的精度是毫秒，
    而游标比较的是**数据库里的 `at` 列**（微秒精度）。把游标截到毫秒之后，
    落在同一毫秒内的记录会被判成"等于游标时刻"，于是 `id < cursor_id`
    这条分支要承担全部次序判断 —— 而 ULID 在同一毫秒内的次序是**随机**的
    （见 `list_entries` 的说明）。结果是**漏条**。

    所以这里用 `datetime.isoformat()` / `fromisoformat()`：它们无损，
    且 Python 3.11+ 的 `fromisoformat` 能认 `Z` 后缀。
    """
    at_part, _, id_part = cursor.partition("|")
    if not id_part:
        return None, ""
    try:
        parsed = datetime.fromisoformat(at_part)
    except ValueError:
        return None, ""
    if parsed.tzinfo is None:
        # 库里都是 tz-aware，naive 的游标按 UTC 解释而不是直接判坏 ——
        # 与 `to_iso` 对 naive 值的处理保持一致。
        parsed = parsed.replace(tzinfo=UTC)
    return parsed, id_part


def count(db: Session) -> int:
    return int(db.execute(select(func.count()).select_from(AuditLog)).scalar_one())


def action_label(action: str) -> str:
    """动作的中文标签。未知动作**原样返回**，不编一个说法 ——
    「记录里出现了一个我不认识的动作」本身就是有用的信息。"""
    return AUDIT_ACTION_LABELS.get(action, action)


# ══════════════════════════════════════════════════════════
# 保留策略
# ══════════════════════════════════════════════════════════


def purge_expired(db: Session, *, days: int = AUDIT_RETENTION_DAYS, now: datetime | None = None) -> int:
    """删掉超过保留期的记录。返回删了几条。

    ⚠️ **这是本模块唯一允许删除的路径**，而且它只按时间删 ——
    没有任何"按 id 删"的接口，因为那会变成一条抹掉某次操作痕迹的捷径。

    启动时调一次（见 `main.lifespan`）。不设定时任务：单机部署里
    多一个后台线程去换"清理及时几小时"并不划算，而审计记录
    多留几个小时没有任何危害。
    """
    cutoff = (now or now_utc()) - timedelta(days=days)
    result = db.execute(delete(AuditLog).where(AuditLog.at < cutoff))
    db.commit()
    removed = int(result.rowcount or 0)
    if removed:
        logger.info("清理了 %d 条超过 %d 天的审计记录", removed, days)
    return removed


__all__ = [
    "AUDIT_RETENTION_DAYS",
    "AuditRequest",
    "action_label",
    "count",
    "cursor_for",
    "from_http",
    "list_entries",
    "purge_expired",
    "record",
]

# `AuditAction` 在这里 re-export，让埋点处只 import 一个模块。
_ = AuditAction
