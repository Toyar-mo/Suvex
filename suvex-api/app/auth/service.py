"""认证域的业务逻辑。

分两层看：

* **口令**：argon2id 哈希、强度校验。规则与前端 `checkPassword` 一致（契约 §3.1）。
* **凭据**：会话（浏览器）与机器令牌（CI）两条通道，各自签发、校验、吊销。

⚠️ 本模块里**没有任何一行日志打出口令或令牌明文**。这不是疏忽 ——
日志会进文件、进容器 stdout、进日志聚合系统，而它们是凭据最容易泄漏的地方
（比数据库更容易：数据库要拖库，日志只要有人 `docker logs` 一下）。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.auth.models import AdminAccount, ApiToken, AuthSession
from app.core.errors import ApiError, ErrorCode
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
from app.core.vocab import UserRole, values

# ══════════════════════════════════════════════════════════
# 账号
# ══════════════════════════════════════════════════════════


def has_any_account(db: Session) -> bool:
    """系统是否已初始化。

    每次 `/auth/state` 与 `/auth/setup` 都要问一次，所以写成 `SELECT 1 LIMIT 1`
    而不是 `count(*)`：这张表通常只有几行，但调用频率高，代价有区别。

    ⚠️ 语义是「**还有没有账号**」而不是"是不是单管理员系统"。
    多用户之后它的用途只剩一个：判断要不要显示初始化页。
    """
    return db.execute(select(AdminAccount.id).limit(1)).first() is not None


# 用户名的合法字符：Unicode 字母、数字、下划线、点、连字符。
#
# ## ★ 为什么必须收紧（真机实测发现的）
#
# 第一版只做了 `strip().lower()`，于是这些都**被接受了**：
#
#     'zhang san'      → 存为 'zhang san'（中间空格保留）
#     'zhang\tsan'     → 存为 'zhang\tsan'
#     'zhang\u3000san' → 全角空格
#     'zhang\xa0san'   → 不换行空格
#
# 而 `strip()` **只去首尾**，中间那些原样留下。后果是**看起来一样的
# 两个名字进不去**：
#
#   · 用户注册 `zhang san`（打了一个空格），登录时输 `zhangsan` ——
#     界面上两个名字视觉上几乎一样，而系统说"用户名或口令不正确"；
#   · 更糟的是 `zhang\tsan` 与 `zhang san`：**在输入框里看不出区别**，
#     而它们是两个不同的账号。
#
# 所以判据从"去掉首尾空白"改成"**只允许明确列出的字符**"。
# 白名单而不是黑名单：黑名单永远漏（Unicode 里的空白字符有十几种，
# 还有各种不可见字符），而白名单漏掉的代价只是"用户被要求换个名字"。
#
# ## 为什么允许中文
#
# `张三` 是合法的 —— 这个系统的使用者是中文团队，强制拼音会让他们
# 记一个自己不用的名字。`\w` 在 Python 的 str 正则下是 Unicode 模式，
# 所以中日韩字母天然通过。
#
# ## 为什么允许 `.` 与 `-`
#
# `zhang.san` / `zhang-san` 是常见写法，而且它们**在视觉上可区分**
# （不像空白字符）。限制它们只是"更整齐"，代价是用户被拒。
_USERNAME_ALLOWED = re.compile(r"^[\w.-]+$")


def normalize_username(raw: str) -> str:
    """归一化登录名：去首尾空白 + 转小写。

    ★ **大小写不敏感**是刻意的：用户不该因为记错大小写而登不进去，
    而"Alice"与"alice"是两个账号只会带来困惑（谁是谁？）。

    与 `Scan.target` 存归一化值是同一条原则：**让同一个东西只有一种表示**。
    存原始大小写、查询时 `lower()` 比较也能工作，但那意味着
    "库里存的值"与"能登录的值"不是同一个东西 —— 而排查登录问题时
    第一件要看的就是库里那个值。

    ⚠️ **它不做格式校验** —— 校验在 `username_problem`。
    分开的理由见那个函数的说明（登录路径不该因格式不符而报错）。
    """
    return raw.strip().lower()


def username_problem(username: str) -> str | None:
    """用户名的格式问题。没问题返回 `None`。

    ## 与 `normalize_username` 分开的理由

    归一化要在**登录路径**上也跑（大小写、首尾空白必须一致），
    而格式校验只应该在**创建**时给出明确错误。

    登录时格式不符（比如用户输了一个含空格的旧名字）应当按
    "用户不存在"处理 —— 照样跑一次假哈希、照样回同一句话。
    在那里报"用户名格式不对"会泄露"这个格式不对"这个信息，
    而攻击者能据此判断哪些输入值得试。

    所以两个函数，两个调用点。
    """
    cleaned = username.strip()
    if not cleaned:
        return "不能为空"
    if len(cleaned) > 64:
        return "长度超出上限（最多 64 个字符）"
    if not _USERNAME_ALLOWED.match(cleaned):
        # 错误消息要说清**允许什么**，否则用户只能反复试
        return "只能包含字母、数字、下划线、点与连字符（不能有空格）"
    return None


def get_account(db: Session) -> AdminAccount | None:
    """取**任意一个**账号。**已废弃的语义，只留给旧调用点。**

    ⚠️ 多用户之后"当前用户"必须从 `Identity.user_id` 拿，不能"取第一个"。
    这个函数保留是因为 `read_me` 等旧调用点还在用它 —— 而那些调用点
    本身就要改（见 `router.read_me` 的说明）。

    新代码不要用它。
    """
    return db.execute(select(AdminAccount).limit(1)).scalar_one_or_none()


def get_account_by_id(db: Session, user_id: str) -> AdminAccount | None:
    """按 id 取账号。**这是多用户下取"当前用户"的正确方式。**"""
    return db.get(AdminAccount, user_id)


def get_account_by_username(db: Session, username: str) -> AdminAccount | None:
    """按归一化后的用户名取账号。登录走这条路。"""
    return db.execute(
        select(AdminAccount).where(AdminAccount.username == normalize_username(username))
    ).scalar_one_or_none()


def list_accounts(db: Session) -> list[AdminAccount]:
    """全部账号，按创建时间正序。

    正序而不是倒序：账号列表很短，而"第一个是初始管理员"这个顺序
    比"最近加的排最前"更有用（前者回答了"这个系统从谁开始"）。
    """
    return list(db.execute(select(AdminAccount).order_by(AdminAccount.created_at)).scalars())


def count_admins(db: Session) -> int:
    """还有几个管理员。

    ★ 这个计数是**防自锁**用的：删掉最后一个管理员、或者把他降级成
    viewer，都会让系统**再也没有人能改设置** —— 而那只能改库才能救回来。
    所以删除与降级前都要问一次它。
    """
    return int(
        db.execute(
            select(func.count())
            .select_from(AdminAccount)
            .where(AdminAccount.role == UserRole.ADMIN.value)
        ).scalar_one()
    )


def create_account(
    db: Session, password: str, *, username: str = "admin", role: str = UserRole.ADMIN.value
) -> AdminAccount:
    """建账号。

    ## 两个调用场景，语义不同

    1. **首次初始化**（`/auth/setup`）—— 建第一个管理员。此时
       `username` 默认 `"admin"`、`role` 默认 `admin` 都是对的：
       一个刚装好的系统需要有人能进去。
    2. **管理员加人**（`POST /users`）—— 走 `create_user`，那里
       **没有默认角色**（见下）。

    ⚠️ 这两个场景共用本函数，但**调用方必须清楚自己在做哪一个**。
    第 1 种只应该由 `router.setup` 调用，而它已经先确认过
    "系统里还没有账号" —— 那个检查是本函数**不做**的，
    因为它无法区分"初始化"与"加人"。
    """
    normalized = normalize_username(username)
    # ★ 格式校验（v0.24 补）。第一版只判了"非空"，于是含中间空格的
    #   用户名被接受 —— 而那种名字在输入框里与不含空格的**看起来一样**。
    #   见 `username_problem` 与 `_USERNAME_ALLOWED` 的说明。
    problem = username_problem(username)
    if problem is not None:
        raise ApiError.validation(f"用户名不合法：{problem}", {"username": problem})

    problems = password_problems(password)
    if problems:
        raise ApiError.validation(
            password_error_message(problems), {"password": "；".join(problems)}
        )

    if get_account_by_username(db, normalized) is not None:
        raise ApiError.conflict(
            ErrorCode.VALIDATION_FAILED,
            f"用户名「{normalized}」已被占用。",
        )

    account = AdminAccount(
        id=new_id("user"),
        username=normalized,
        password_hash=hash_password(password),
        role=role,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def create_user(db: Session, *, username: str, password: str, role: str) -> AdminAccount:
    """管理员加人。**`role` 没有默认值 —— 这是刻意的。**

    给一个 `role="admin"` 的默认值会让"忘了传角色"静默地造出一个管理员，
    而管理员能做的第一件事就是**发起扫描（对外发包）**。
    参数缺失应该在调用处就报错，而不是在数据库里变成一个提权。
    """
    if role not in set(values(UserRole)):
        raise ApiError.validation(
            {"role": f"应为 {'、'.join(values(UserRole))} 之一"}
        )
    return create_account(db, password, username=username, role=role)


def update_user_role(db: Session, user_id: str, role: str) -> AdminAccount:
    """改角色。

    ★ **不允许把最后一个管理员降级。** 那会让系统再也没有人能改设置，
    而恢复只能靠直接改库 —— 一个通过界面走进去的死胡同。

    判据是"改完之后还有没有管理员"，不是"当前是不是管理员"：
    把一个 viewer 改成 viewer 是幂等的，不该被拦。
    """
    if role not in set(values(UserRole)):
        raise ApiError.validation({"role": f"应为 {'、'.join(values(UserRole))} 之一"})

    account = get_account_by_id(db, user_id)
    if account is None:
        raise ApiError.not_found("用户")

    if (
        account.role == UserRole.ADMIN.value
        and role != UserRole.ADMIN.value
        and count_admins(db) <= 1
    ):
        raise ApiError.conflict(
            ErrorCode.LAST_ADMIN,
            "这是最后一个管理员，不能降级为只读 —— 否则没人能再改设置。"
            "请先指定另一个管理员。",
        )

    account.role = role
    db.commit()
    db.refresh(account)
    return account


def delete_user(db: Session, user_id: str, *, actor_id: str) -> None:
    """删账号。

    两条拒绝（都是"通过界面走进去的死胡同"，只能改库才能救）：

      · **不能删自己** —— 删完立刻掉线，而如果同时是最后一个管理员，
        就再也进不来了；
      · **不能删最后一个管理员** —— 同 `update_user_role`。

    会话随账号级联删除（`AuthSession.user_id` 上是 `ondelete="CASCADE"`），
    所以被删的人不会留着一条还能用的会话。
    """
    if user_id == actor_id:
        raise ApiError.conflict(ErrorCode.CANNOT_DELETE_SELF, "不能删除当前登录的账号。")

    account = get_account_by_id(db, user_id)
    if account is None:
        raise ApiError.not_found("用户")

    if account.role == UserRole.ADMIN.value and count_admins(db) <= 1:
        raise ApiError.conflict(
            ErrorCode.LAST_ADMIN,
            "这是最后一个管理员，不能删除 —— 否则没人能再改设置。",
        )

    db.delete(account)
    db.commit()


def authenticate(db: Session, username: str, password: str) -> AdminAccount:
    """按**用户名 + 口令**校验。失败一律同一句话。

    ## 两条时序防线（都不能省）

    1. **用户名不存在时也跑一次 argon2**（用固定的假哈希）。
       否则响应时间会泄露"这个用户名存不存在"，而那是账号枚举的入口。
    2. **口令错时也跑完整校验**（`verify_password` 本来就是完整的）。

    ## 为什么错误消息不区分"用户不存在"与"口令错"

    那是账号枚举漏洞。本系统里用户名的集合很小（一个团队几个人），
    但攻击者能从"哪些用户名报的是'口令错'"推出哪些是真实账号 ——
    而对一个**会对目标发包**的平台来说，拿到一个有效用户名就是
    爆破的一半。

    ⚠️ 注意这里**没有**"本系统只有一个账号所以更简单"那句话了 ——
    那是单管理员时代的注释，多用户之后前半句不再成立。
    """
    account = get_account_by_username(db, username)
    if account is None:
        # 一个固定的、格式合法但永不匹配的 argon2 哈希。
        # 用它跑一次 verify，让耗时与"账号存在但口令错"一致。
        verify_password(_DUMMY_HASH, password)
        raise ApiError.unauthenticated("用户名或口令不正确。")

    if not verify_password(account.password_hash, password):
        raise ApiError.unauthenticated("用户名或口令不正确。")
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
