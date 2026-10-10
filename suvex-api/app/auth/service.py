"""认证域的业务逻辑。

分两层看：

* **口令**：argon2id 哈希、强度校验。规则与前端 `checkPassword` 一致（契约 §3.1）。
* **凭据**：会话（浏览器）与机器令牌（CI）两条通道，各自签发、校验、吊销。

⚠️ 本模块里**没有任何一行日志打出口令或令牌明文**。这不是疏忽 ——
日志会进文件、进容器 stdout、进日志聚合系统，而它们是凭据最容易泄漏的地方
（比数据库更容易：数据库要拖库，日志只要有人 `docker logs` 一下）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.auth.models import AdminAccount, ApiToken, AuthSession
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.security import (
    hash_password,
    looks_like_machine_token,
    new_machine_token,
    new_session_token,
    password_error_message,
    password_problems,
    token_digest,
    verify_password,
)
from app.core.timeutil import now_utc


# ══════════════════════════════════════════════════════════
# 账号
# ══════════════════════════════════════════════════════════


def has_any_account(db: Session) -> bool:
    """系统是否已初始化。

    每次 `/auth/state` 与 `/auth/setup` 都要问一次，所以写成 `SELECT 1 LIMIT 1`
    而不是 `count(*)`：单管理员系统里这张表只会有 0 或 1 行，
    数一遍和看一眼的代价在这种调用频率下是有区别的。
    """
    return db.execute(select(AdminAccount.id).limit(1)).first() is not None


def get_account(db: Session) -> AdminAccount | None:
    return db.execute(select(AdminAccount).limit(1)).scalar_one_or_none()


def create_account(db: Session, password: str) -> AdminAccount:
    """首次初始化：建管理员账号。

    **这是唯一一处会创建账号的代码**，且调用方必须先确认
    「系统里还没有账号」（`router.setup` 会查）。做成"一次性初始化"而不是
    开放注册是契约 §0.3 的决定：`/auth/setup` 在已初始化后彻底关闭，
    否则它就是一个"任意人可重设管理员口令"的后门。
    """
    problems = password_problems(password)
    if problems:
        raise ApiError.validation(password_error_message(problems), {"password": "；".join(problems)})

    account = AdminAccount(id=new_id("user"), password_hash=hash_password(password))
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def authenticate(db: Session, password: str) -> AdminAccount:
    """校验口令。**只有"口令不对"一种失败**（契约 §3.1）。

    不做「账号不存在」与「口令错误」的区分 —— 那是账号枚举漏洞。
    本系统只有一个账号，所以这条更简单：任何失败都是同一句话。
    也没写「口令错误」以外的信息（比如剩余尝试次数）——那会帮到爆破者。

    ⚠️ 账号不存在时**仍然走一次 verify_password**（拿一个固定的假哈希）。
    否则响应时间会泄露"这个系统有没有初始化"，而这个区别正好告诉攻击者
    该去打 `/auth/setup` 还是 `/auth/login`。
    """
    account = get_account(db)
    if account is None:
        # 一个固定的、格式合法但永不匹配的 argon2 哈希。
        # 用它跑一次 verify，让耗时与"账号存在但口令错"一致。
        verify_password(_DUMMY_HASH, password)
        raise ApiError.unauthenticated("口令不正确。")

    if not verify_password(account.password_hash, password):
        raise ApiError.unauthenticated("口令不正确。")
    return account


# 一个真实的 argon2id 哈希（`hash_password("suvex-timing-equalization-dummy")`
# 的产物）。它必须**真的是合法哈希**，不能是一段长得像的字符串：
# 形状不对会让 argon2 直接抛 InvalidHashError，verify 走的是
# 异常分支的快速路径 —— 于是"账号不存在"比"口令错"快一个数量级，
# 时序差异没消除，反而更明显。
#
# 这个口令值本身不重要（它不属于任何账号），重要的是每次 verify 都真的
# 跑完整套 argon2 计算（约 64 MiB 内存、3 轮）。
_DUMMY_HASH = "$argon2id$v=19$m=65536,t=3,p=4$glZpgKJ6jvF83WklqkxvpQ$IqTGRtPJyiCYHsM2TT2XbjhHrTy0ZLRel3Goxv9yv1U"


def change_password(db: Session, account: AdminAccount, current: str, new: str) -> int:
    """改口令。返回被吊销的会话数。

    ★ 改口令后**必须吊销全部既有会话**（契约 §1.2 的核心理由）：
    「JWT 无法即时吊销（改密码后旧令牌仍有效）」正是契约拒绝 JWT 的原因，
    所以这个行为是那项决策的兑现 —— 漏掉它，选不透明令牌就白选了。

    返回的数量用于让调用方能如实报告「已使 N 个其他会话失效」。
    """
    if not verify_password(account.password_hash, current):
        # ⚠️ 文案只写「当前密码不正确」，不透露任何额外信息（契约 §1.3，
        # 前端 changePassword 的错误文案已经定了调子）。
        raise ApiError.validation("当前密码不正确。", {"current": "当前密码不正确"})

    problems = password_problems(new)
    if problems:
        raise ApiError.validation(password_error_message(problems), {"next": "；".join(problems)})

    if new == current:
        raise ApiError.validation("新密码不能与当前密码相同。", {"next": "不能与当前密码相同"})

    account.password_hash = hash_password(new)
    account.updated_at = now_utc()
    revoked = revoke_all_sessions(db, account.id)
    db.commit()
    return revoked


# ══════════════════════════════════════════════════════════
# 会话（人 / 浏览器）
# ══════════════════════════════════════════════════════════


def create_session(
    db: Session, account: AdminAccount, *, ip: str | None, user_agent: str | None, ttl_hours: int
) -> tuple[str, AuthSession]:
    """签发会话。返回 `(明文令牌, 会话行)`。

    明文只在这一刻存在于内存里，随后进 `Set-Cookie`；
    库里只有 SHA-256 摘要。
    """
    raw = new_session_token()
    session = AuthSession(
        id=new_id("sess"),
        token_hash=token_digest(raw),
        user_id=account.id,
        expires_at=now_utc() + timedelta(hours=ttl_hours),
        last_seen_at=now_utc(),
        ip=ip,
        user_agent=(user_agent or "")[:1000] or None,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return raw, session


def resolve_session(db: Session, raw: str) -> AuthSession | None:
    """按明文令牌找有效会话。无效一律返回 None（不区分原因）。"""
    if not raw:
        return None
    return db.execute(
        select(AuthSession)
        .where(AuthSession.token_hash == token_digest(raw))
        .where(AuthSession.revoked_at.is_(None))
        .where(AuthSession.expires_at > now_utc())
    ).scalar_one_or_none()


def touch_session(db: Session, session: AuthSession, *, ttl_hours: int) -> None:
    """滚动续期。

    这就是「refresh token」在这个设计里的等价物：会话在**每次使用时**续期，
    所以活跃用户不会在干到一半被踢出去。而绝对过期仍然存在 ——
    一个放着不动的会话最多活 `ttl_hours`。

    节流：只在距上次记录超过 60 秒时才写库。否则每个请求一次 UPDATE，
    一个开着活动页的浏览器会每秒写好几行 —— 那是纯粹的写放大。
    """
    moment = now_utc()
    if session.last_seen_at and (moment - session.last_seen_at) < timedelta(seconds=60):
        return
    session.last_seen_at = moment
    session.expires_at = moment + timedelta(hours=ttl_hours)
    db.commit()


def revoke_session(db: Session, raw: str) -> None:
    """登出：软吊销。"""
    session = resolve_session(db, raw)
    if session is None:
        return
    session.revoked_at = now_utc()
    db.commit()


def revoke_all_sessions(db: Session, user_id: str, *, except_id: str | None = None) -> int:
    """吊销某个用户的全部会话。返回吊销条数。

    `except_id` 给「改密码后保持当前会话」用 —— 但 `change_password`
    刻意**不用它**：改口令之后保留任何旧会话都说不清是哪一个，
    统一失效再让用户重新登录是最不容易出错的语义。
    """
    stmt = (
        update(AuthSession)
        .where(AuthSession.user_id == user_id)
        .where(AuthSession.revoked_at.is_(None))
        .values(revoked_at=now_utc())
    )
    if except_id is not None:
        stmt = stmt.where(AuthSession.id != except_id)
    result = db.execute(stmt)
    return int(result.rowcount or 0)


def purge_expired_sessions(db: Session, *, older_than_days: int = 30) -> int:
    """清理早就过期/吊销的会话行。

    不删的话这张表会随每次登录无限增长。保留 30 天是为了安全复盘
    （「这个账号上周从哪个 IP 登录过」），更久的历史没有查询场景。
    """
    from sqlalchemy import delete

    cutoff = now_utc() - timedelta(days=older_than_days)
    result = db.execute(
        delete(AuthSession).where(
            (AuthSession.expires_at < cutoff) | (AuthSession.revoked_at < cutoff)
        )
    )
    db.commit()
    return int(result.rowcount or 0)


# ══════════════════════════════════════════════════════════
# 机器令牌
# ══════════════════════════════════════════════════════════


def create_token(
    db: Session,
    *,
    label: str,
    scopes: list[str],
    expires_at: datetime | None,
) -> tuple[ApiToken, str]:
    """签发机器令牌。返回 `(令牌行, 明文)`。**明文只此一次。**"""
    raw, prefix = new_machine_token()
    token = ApiToken(
        id=new_id("tok"),
        label=label,
        prefix=prefix,
        token_hash=token_digest(raw),
        scopes=list(scopes),
        expires_at=expires_at,
    )
    db.add(token)
    db.commit()
    db.refresh(token)
    return token, raw


def resolve_token(db: Session, raw: str) -> ApiToken | None:
    """按明文令牌找有效令牌。

    先看形状（`svx_` 前缀）再查库：没有这一步，任何人往 `Authorization`
    头里塞点东西都会触发一次数据库查询 —— 一个不需要认证的放大面。
    """
    if not looks_like_machine_token(raw):
        return None
    token = db.execute(
        select(ApiToken).where(ApiToken.token_hash == token_digest(raw))
    ).scalar_one_or_none()
    if token is None or not token.is_active():
        return None
    return token


def mark_token_used(db: Session, token: ApiToken) -> None:
    """记录最后使用时间。

    同样节流（60 秒）：`ApiToken.lastUsedAt` 是给人看的（"这个令牌还在用吗"），
    精确到秒没有意义，而每个请求一次 UPDATE 是实实在在的写放大。
    """
    moment = now_utc()
    if token.last_used_at and (moment - token.last_used_at) < timedelta(seconds=60):
        return
    token.last_used_at = moment
    db.commit()


def list_tokens(db: Session) -> list[ApiToken]:
    return list(db.execute(select(ApiToken).order_by(ApiToken.created_at.desc())).scalars())


def revoke_token(db: Session, token_id: str) -> None:
    token = db.get(ApiToken, token_id)
    if token is None:
        raise ApiError.not_found("令牌")
    token.revoked_at = now_utc()
    db.commit()


def count_active_tokens(db: Session) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(ApiToken)
            .where(ApiToken.revoked_at.is_(None))
            .where((ApiToken.expires_at.is_(None)) | (ApiToken.expires_at > now_utc()))
        ).scalar_one()
    )
