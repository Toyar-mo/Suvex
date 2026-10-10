"""`providers` 领域的业务逻辑。

## 明文只在一处出现

整个模块里，明文 API key 只在两个函数里存在：

* `_apply_key` —— 写入路径，明文进来，立刻变成密文 + 掩码落库；
* `decrypt_provider_key` —— 读出路径，只有一个调用方：真要去调 LLM 的时候。

其余任何函数（列表、详情、创建、更新、删除、设默认）都**碰不到明文**。
这不是靠自觉：模型上根本没有 `api_key` 属性（见 `models.py` 的说明），
想读也读不到。

> **如实记一条做不到的事**：Python 的 `str` 不可变，也没法手动清零，
> 所以明文在内存里存活的那段时间无法被主动擦除，只能等 GC。
> 想真正控制这段窗口得用 `bytearray` + 手动归零，而 HTTP 客户端
> 最终也要把它变成 `str` 放进请求头。所以这里能做到的是
> **缩小窗口**（只在调用前解密、不缓存、不落日志），而不是消除它。

## 默认 provider 的不变量

**「有账号 ⇒ 恰好一个默认」**（契约 §2.9 的"至多一个" + 前端
`settings-store` 需要的"至少一个"）。

这条不变量由三个**语义不同**的函数维护，而不是一个带开关的大函数：
`_switch_default`（我要换）／`_ensure_default`（补空缺）／
`_reselect_default`（当前这个不当默认了，另挑一个）。
早先把它们塞进一个 `_normalize_default` 里，直接导致了一个真实缺陷 ——
见 `_ensure_default` 的说明。
"""

from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy import func
from sqlalchemy import select, update as sa_update
from sqlalchemy.orm import Session

from app.core.crypto import (
    SecretDecryptError,
    encrypt_secret,
    looks_like_mask,
    mask_secret,
)
from app.core.errors import ApiError, ErrorCode
from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.core.vocab import ABORTABLE_SCAN_STATUSES
from app.providers import llm
from app.providers.models import ProviderAccount
from app.providers.schemas import ProviderCreate, ProviderUpdate
from app.scans.models import Scan

# 凭据被换掉之后 `verifyError` 写什么。
#
# 契约 §3.2 原文是「把 `verified` 置回 `false`、`verifyError` 置 `null`」。
# 这里改成写一句话，是**与前端实现对齐**的结果：`settings-store.ts` 的
# `saveProviderAccount` 里那个分支写的正是「凭据已更改，需要重新验证」，
# 并注释说明了理由。契约那句话的意图是「不要沿用旧 key 的验证结论」，
# 两者都满足；而写一句话更好 —— 前端渲染的是 `verifyError ?? '尚未验证'`，
# 置 null 会显示成"尚未验证"，而真实情况是"换过了、要重验"。
# 已按流程回改契约（§3.2）。
KEY_CHANGED_NOTICE = "凭据已更改，需要重新验证"


# ══════════════════════════════════════════════════════════
# 读
# ══════════════════════════════════════════════════════════


def list_providers(db: Session) -> list[ProviderAccount]:
    """列表。

    排序：默认的排最前，其余按创建时间。
    这样列表的第一项永远是"当前正在用的那个" —— 界面不用额外排序，
    用户也不用在一堆条目里找哪个是默认。
    """
    return list(
        db.execute(
            select(ProviderAccount).order_by(
                ProviderAccount.is_default.desc(), ProviderAccount.created_at
            )
        ).scalars()
    )


def get_provider(db: Session, provider_id: str) -> ProviderAccount:
    provider = db.get(ProviderAccount, provider_id)
    if provider is None:
        raise ApiError.not_found("这个 provider")
    return provider


def decrypt_provider_key(provider: ProviderAccount) -> str:
    """取出明文凭据。**唯一的解密入口。**

    没有凭据（本地模型）时返回空串 —— 那是一个合法状态，
    不是错误。只有"有密文但解不开"才是错误。
    """
    if not provider.api_key_encrypted:
        return ""
    return decrypt_secret_or_raise(provider)


def decrypt_secret_or_raise(provider: ProviderAccount) -> str:
    from app.core.crypto import decrypt_secret

    try:
        return decrypt_secret(provider.api_key_encrypted or "")
    except SecretDecryptError as exc:
        raise ApiError(
            500,
            ErrorCode.PROVIDER_KEY_UNREADABLE,
            # 这段文案是给用户看的，所以要说清"发生了什么"和"怎么办"，
            # 而不是把 cryptography 的异常原样透出去。
            f"无法解密 provider「{provider.label}」的凭据：{exc}",
        ) from exc


# ══════════════════════════════════════════════════════════
# 写
# ══════════════════════════════════════════════════════════


def _apply_key(provider: ProviderAccount, raw: str | None) -> bool:
    """写入或保留凭据。返回「凭据是否被改动」。

    三种情况都视为**不改动**：

    1. `None` / 省略 —— 契约 §2.9 的部分更新语义；
    2. 空串或纯空白 —— 前端的输入框在"不回填"时就是这个值，
       而它的含义是"沿用已存的那把钥匙"。**这条漏掉，用户改个显示名
       就会把凭据清空**（前端注释里记过这个隐蔽 bug）；
    3. **值等于当前掩码** —— 见 `crypto.looks_like_mask` 的说明。
       这是第二道防线：设计上掩码根本不会出现在 `apiKey` 里，
       但任何一个"读出来、改一个字段、整体提交"的客户端都可能把它带回来，
       而后果是真凭据被静默替换成一个假字符串。

    写入时密文与掩码**必须一起更新**（读取路径靠掩码，不做解密）。
    """
    if raw is None:
        return False
    value = raw.strip()
    if not value or looks_like_mask(value):
        return False

    provider.api_key_encrypted = encrypt_secret(value)
    provider.api_key_hint = mask_secret(value)
    return True


def _current_default_id(db: Session) -> str | None:
    return db.execute(
        select(ProviderAccount.id).where(ProviderAccount.is_default).limit(1)
    ).scalar_one_or_none()


def _best_candidate(db: Session, *, exclude: str | None = None) -> str | None:
    """挑一个替补默认。

    优先**已验证**的 —— 把一个打不通的 provider 设成默认，
    会让下一次扫描在建好之后才失败，而那时用户已经离开向导了。
    同档内按创建时间，先建的先用。
    """
    stmt = select(ProviderAccount.id).order_by(
        ProviderAccount.verified.desc(), ProviderAccount.created_at
    )
    if exclude is not None:
        stmt = stmt.where(ProviderAccount.id != exclude)
    return db.execute(stmt.limit(1)).scalar_one_or_none()


def _clear_defaults(db: Session) -> None:
    """把所有行清成 `is_default = false`。

    ★ 为什么用 bulk UPDATE + `synchronize_session=False`，而不是改 ORM 属性：

    ORM 的工作单元（unit of work）在 flush 时按主键排序发出 UPDATE，
    **不按我写的顺序**。于是"先把别的清成 false、再把这一个设成 true"
    这个顺序在 ORM 层面是没有保证的 —— 一旦反过来，
    部分唯一索引 `uq_provider_accounts_default` 会立刻报唯一冲突，
    而且它只在"库里本来就有默认"的时候才出现（新库上跑得好好的）。

    bulk 语句由 `Session.execute` **立即**发到数据库，顺序因此是确定的。
    `synchronize_session=False` 是为了不让 SQLAlchemy 去猜怎么同步内存对象
    （它会为此多发一条 SELECT）；代价是内存实例会变陈旧，
    所以每个改默认的函数末尾都要 `expire_all()`。
    """
    db.execute(
        sa_update(ProviderAccount)
        .values(is_default=False)
        .execution_options(synchronize_session=False)
    )


def _mark_default(db: Session, provider_id: str) -> None:
    db.execute(
        sa_update(ProviderAccount)
        .where(ProviderAccount.id == provider_id)
        .values(is_default=True)
        .execution_options(synchronize_session=False)
    )


def _switch_default(db: Session, provider_id: str) -> None:
    """把默认切到指定 provider。"""
    _clear_defaults(db)
    _mark_default(db, provider_id)
    db.expire_all()


def _ensure_default(db: Session) -> None:
    """**仅当当前没有任何默认时**，补一个。

    ★ 这个"仅当"是本文件里最容易被忽略、后果又最隐蔽的一处。

    早先这里的实现是"每次都清空再重挑"，于是出现了一个真实的行为缺陷：
    用户把 A 设为默认，然后新建了 B（没勾"设为默认"）——
    这一次看似无关的创建会**清掉 A 的默认、并把 B 提为默认**。
    用户明明做过一次明确的选择，却因为一个无关动作被换掉了，
    而且界面上没有任何提示。这个缺陷是验收脚本抓到的
    （"任何时刻只有一个默认 provider"那条报出了 `prov_acceptance`）。

    所以现在分开成三个语义明确的函数：
    `_switch_default`（我要换）／`_ensure_default`（补空缺）／
    `_reselect_default`（当前这个不当默认了，另挑一个）——
    而不是一个带一堆开关的 `_normalize_default`。
    """
    if _current_default_id(db) is not None:
        return
    candidate = _best_candidate(db)
    if candidate is not None:
        _mark_default(db, candidate)
        db.expire_all()


def _reselect_default(db: Session, *, exclude: str) -> None:
    """把 `exclude` 从默认位置上摘下来，并从**其余的**里挑一个。

    只在"用户主动取消了这个 provider 的默认"时调用 —— 那时必须换一个，
    因为"有账号却没有默认"是总览页状态条读不出来的状态。

    排除了自己还挑不到（只剩它一个）时会**把它自己设回去**：
    用户勾掉了唯一一个 provider 的默认，界面会立刻又勾上。
    这看起来像"点了没反应"，但比留下一个非法状态好 ——
    而真正的出口是删除它（见 `delete_provider` 对最后一个的例外）。
    """
    _clear_defaults(db)
    candidate = _best_candidate(db, exclude=exclude) or _best_candidate(db)
    if candidate is not None:
        _mark_default(db, candidate)
    db.expire_all()


def _snapshot(provider: ProviderAccount) -> tuple:
    """用于判断"这次保存到底改了什么"。

    `api_key_encrypted` 也进快照，但它只在真的换了 key 时才会变
    （Fernet 每次加密都用新 IV，所以"重新提交同一个 key"也会被算作改动）。
    这个边界是有意接受的：那种情况下 `updated_at` 确实动了，
    而对用户来说他刚刚做了一次"重填凭据"的动作。
    要精确到"值一样就不算改"，就得在更新路径上解密比对 ——
    那会把明文拉进一条本来不需要它的路径，不划算。
    """
    return (
        provider.label,
        provider.model,
        provider.kind,
        provider.base_url,
        provider.note,
        provider.is_default,
        provider.api_key_encrypted,
        provider.verified,
        provider.verify_error,
    )


def create_provider(db: Session, payload: ProviderCreate) -> ProviderAccount:
    provider = ProviderAccount(
        id=new_id("prov"),
        label=payload.label.strip(),
        model=payload.model.strip(),
        kind=payload.kind,
        base_url=payload.baseUrl.strip().rstrip("/"),
        note=payload.note.strip(),
        is_default=False,  # 先建出来，默认交给 _switch_default / _ensure_default 处理
        verified=False,
        verify_error=None,
    )
    _apply_key(provider, payload.apiKey)
    db.add(provider)
    db.flush()  # 拿到 id 之后再处理默认

    if payload.isDefault:
        _switch_default(db, provider.id)
    else:
        # ★ 只是"补空缺"，**不是**"重新挑一个"。
        # 写成后者的话，新建一个无关的 provider 会把用户已经选好的默认换掉 ——
        # 见 `_ensure_default` 的说明。
        _ensure_default(db)

    db.commit()
    db.refresh(provider)
    return provider


def update_provider(
    db: Session, provider_id: str, payload: ProviderUpdate
) -> tuple[ProviderAccount, bool]:
    """部分更新。返回 `(provider, 是否有实际改动)`。

    第二个返回值是给 `SaveResult` 的 `nochange` 态用的（契约 §3.7）：
    用户点了保存但什么都没改，**不能报"已保存"** —— 那是虚假的动作感。
    """
    provider = get_provider(db, provider_id)
    before = _snapshot(provider)
    was_default = provider.is_default

    if payload.label is not None:
        provider.label = payload.label.strip()
    if payload.model is not None:
        provider.model = payload.model.strip()
    if payload.kind is not None:
        provider.kind = payload.kind
    if payload.baseUrl is not None:
        # 去尾斜杠，避免拼出 `//v1`（前端 `saveProviderAccount` 同样处理）
        provider.base_url = payload.baseUrl.strip().rstrip("/")
    if payload.note is not None:
        provider.note = payload.note.strip()

    # ★ 换 key 必须作废"已验证"。
    #
    # 实测抓到的问题（前端注释里记过）：改 key 后 verified 沿用旧值 true，
    # 于是一个**从未验证过的新 key** 显示成"已验证可用"，而新建扫描向导的
    # `configured` 读的正是它 —— 用户选中后会在扫描跑到一半时认证失败。
    if _apply_key(provider, payload.apiKey):
        provider.verified = False
        provider.verify_error = KEY_CHANGED_NOTICE

    provider.updated_at = now_utc()
    db.flush()

    if payload.isDefault is True:
        _switch_default(db, provider.id)
    elif payload.isDefault is False and was_default:
        # 取消默认。**只有它原本就是默认时**才需要换人 ——
        # 对一个本来就不是默认的 provider 传 `isDefault: false` 是幂等的空操作，
        # 不该顺手去动别人。
        _reselect_default(db, exclude=provider.id)

    db.flush()
    changed = _snapshot(provider) != before
    db.commit()
    db.refresh(provider)
    return provider, changed


def set_default(db: Session, provider_id: str) -> tuple[ProviderAccount, bool]:
    """设为默认（列表里的快捷操作）。

    它是 `PATCH` 的一个特例，但**单独一个端点**是有价值的：
    它的语义是"切换当前使用的模型"，不是"改这条记录的一个字段"，
    而前端也是从一个不同的入口（列表行上的星标）触发它。
    """
    provider = get_provider(db, provider_id)
    if provider.is_default:
        # 已经是默认了 —— `nochange`，而不是"设置成功"。
        return provider, False

    _switch_default(db, provider.id)
    db.commit()
    db.refresh(provider)
    return provider, True


def delete_provider(db: Session, provider_id: str) -> None:
    """删除。两种情况会拒绝，都是 `409` —— 但**错误码不同**。

    为什么要分成两个码而不是一个笼统的 409：调用方要做的补救动作不同。
    `PROVIDER_IS_DEFAULT` → 先去设另一个为默认；
    `PROVIDER_IN_USE` → 先去终止那些扫描。给同一个码的话，
    前端只能把两句话都显示出来，而其中一句是错的。
    """
    provider = get_provider(db, provider_id)

    if provider.is_default:
        # ★ 「默认不可删」有**一个例外：它是最后一个**。
        #
        # 没有这个例外会死锁：新建的第一个 provider 会被自动提为默认
        # （`_ensure_default` 的规则），而默认不可删 ——
        # 于是只有一个 provider 的用户**永远删不掉它**。
        # 这个死锁是写测试时抓到的（`test_delete_removes_the_provider`）。
        #
        # 例外是合理的：这条规则的目的是"不要让默认指向一个已删除的行"，
        # 而删掉最后一个 provider 之后没有任何行需要指向，
        # "有账号 ⇒ 有默认" 这条不变量也自然变成空真。
        remaining = db.execute(
            select(func.count()).select_from(ProviderAccount).where(ProviderAccount.id != provider_id)
        ).scalar_one()
        if remaining:
            raise ApiError.conflict(
                ErrorCode.PROVIDER_IS_DEFAULT,
                f"「{provider.label}」是当前默认 provider，不能删除。"
                "请先把另一个 provider 设为默认。",
            )

    in_use = list(
        db.execute(
            select(Scan).where(
                Scan.provider_id == provider_id,
                Scan.status.in_(tuple(ABORTABLE_SCAN_STATUSES)),
            )
        ).scalars()
    )
    if in_use:
        # 契约 §6 要求「说明是哪些扫描」—— 只说"有扫描在用"会让用户
        # 去扫描列表里自己找，而那个列表可能很长。
        names = "、".join(f"{scan.name}（{scan.id}）" for scan in in_use[:5])
        more = f" 等 {len(in_use)} 次" if len(in_use) > 5 else ""
        raise ApiError.conflict(
            ErrorCode.PROVIDER_IN_USE,
            f"「{provider.label}」正在被未结束的扫描使用：{names}{more}。"
            "请先终止这些扫描，或等它们结束。",
        )

    # 走到这里说明它不是默认（是默认的话上面已经抛了 409）。
    #
    # ⚠️ 于是"删掉默认之后立刻补一个"这个分支**在这里不存在** ——
    # 那是刻意的，不是漏了：契约 §3.2 规定默认 provider 不可删。
    # 对比一下前端 mock（`removeProviderAccount` 会自动挑一个替补默认），
    # 两者行为不同，而这里按契约走：
    #
    # 默认 provider 决定了**扫描会真的用哪个模型**。自动替换意味着
    # 用户删掉 A 之后，下一次扫描悄悄用了 B —— 那是他不一定选过的那个。
    # 要求他显式选一次，成本是一次点击，换掉的是"模型被自己换掉"这类
    # 事后很难解释的行为。
    #
    # 也刻意**不**在这里顺手去动默认：删除一个非默认的 provider 时
    # 去重挑默认，会把当前的默认换掉 —— 用户没要求那件事。
    db.execute(sa_delete(ProviderAccount).where(ProviderAccount.id == provider_id))
    db.commit()


# ══════════════════════════════════════════════════════════
# 连通性验证
# ══════════════════════════════════════════════════════════


def verify_provider(db: Session, provider_id: str) -> tuple[ProviderAccount, llm.ProbeOutcome]:
    """用**已保存的**配置真的发一次最小化请求，并把结论写回。

    任务要求「前端保存配置时可以调这个做即时校验」——所以它验证的是
    库里的配置，不是请求体里的草稿。这条边界是有意的：
    草稿里的东西还没经过校验与归一化（去空格、去尾斜杠），
    拿它去发请求会得到"配置看起来对但就是打不通"的结果。

    解密失败**不走异常路径**，而是变成一个正常的探测失败：
    对用户来说"凭据解不开"和"凭据被拒绝"是一样的处境 ——
    都要重新填一次 key。报 500 只会让他以为平台坏了。
    """
    provider = get_provider(db, provider_id)

    try:
        api_key = decrypt_provider_key(provider)
    except ApiError as exc:
        outcome = llm.ProbeOutcome(ok=False, duration_ms=0, error=exc.message)
        provider.verified = False
        provider.verify_error = outcome.error
        db.commit()
        db.refresh(provider)
        return provider, outcome

    outcome = llm.probe(
        base_url=provider.base_url,
        label=provider.label,
        model=provider.model,
        api_key=api_key,
        kind=provider.kind,
    )

    provider.verified = outcome.ok
    provider.verify_error = None if outcome.ok else outcome.error
    provider.updated_at = now_utc()
    db.commit()
    db.refresh(provider)
    return provider, outcome
