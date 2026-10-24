"""`settings` 领域路由（契约 §3.7）。

| 方法   | 路径                                   | 状态            |
| ------ | -------------------------------------- | --------------- |
| GET    | `/settings`                            | **已实现**（v0.22）|
| GET    | `/settings/sandbox`                    | **已实现**      |
| PUT    | `/settings/sandbox`                    | **已实现**      |
| GET    | `/settings/notices/rule`               | **已实现**（v0.22）|
| PUT    | `/settings/notices/rule`               | **已实现**（v0.22）|
| GET    | `/settings/notices/channels`           | **已实现**（v0.22）|
| POST   | `/settings/notices/channels`           | **已实现**（v0.22）|
| PATCH  | `/settings/notices/channels/{id}`      | **已实现**（v0.22）|
| DELETE | `/settings/notices/channels/{id}`      | **已实现**（v0.22）|
| POST   | `/settings/notices/channels/{id}/test` | **已实现**（v0.22）|
| GET    | `/settings/tokens`                     | **已实现**      |
| POST   | `/settings/tokens`                     | **已实现**      |
| DELETE | `/settings/tokens/{id}`                | **已实现**      |
| POST   | `/settings/password`                   | **已实现**      |

令牌管理与改口令属于**认证**，所以在任务 1 就实现（它们直接用
`app/auth/service.py`，没有额外业务逻辑）。沙箱与通知是真正的
settings 内容。

⚠️ 实现沙箱配置时的两条约束（契约 §2.9 / §6）：

* `networkIsolated` 的默认值是 **`false`（允许出网）**。这不是"更安全就一定
  更好"的开关 —— 扫描经常需要出网打外部目标。打开它会让针对外部目标的
  扫描直接失败，所以界面上必须说清这个取舍。
* `PUT /settings/sandbox` 带 `allowVolumeWrite=true` 时属于高风险操作，
  前端已有文案说明"仅在你确信目标代码可丢弃时开启"，沿用为准。

## 任务 13：执行类配置进了这个端点

`sandbox_config` 新增了三列（并发 / 回合上限 / 扫描深度），它们与沙箱配置
一起在这个端点上读写 —— 因为它们回答的是同一类问题："扫描跑起来是什么样"。

## v0.22：最后六个 501 清零

`GET /settings` 与五个通知端点是本模块**最后未实现**的部分。它们此前是
501，理由是"四块快照缺三块却回 200 会让调用方以为拿全了" —— 那个理由
当时成立，但它的代价是**设置页有一整块是死的**。

现在四块（accounts / sandbox / notices / tokens）都能拼出来了，所以
`GET /settings` 可以真的返回完整快照。**判断标准没有变**：
只要有一块拼不出来，这个端点就该继续 501，而不是回一个半成品。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import service as auth_service
from app.auth.deps import Identity, require_admin
from app.auth.schemas import ApiTokenOut, CreatedTokenOut
from app.core.db import get_db
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.results import SaveOk
from app.core.timeutil import now_utc
from app.core.vocab import NotifyChannelKind, Severity, values
from app.providers.schemas import ProviderOut
from app.settings import dispatch
from app.settings import service as settings_service
from app.settings.schemas import (
    ChangePasswordRequest,
    CreateTokenRequest,
    ExecutionSourceOut,
    FindingSlaIn,
    FindingSlaOut,
    NoticeRuleIn,
    NoticeRuleOut,
    NoticesOut,
    NotifyChannelIn,
    NotifyChannelOut,
    NotifyChannelPatch,
    SandboxIn,
    SandboxOut,
    ScanRetentionIn,
    ScanRetentionOut,
    ScanViewOut,
    ScanViewsIn,
    ScanViewsOut,
    SettingsSnapshotOut,
)

router = APIRouter(prefix="/settings", tags=["设置"])

_TASK = "7"


# ══════════════════════════════════════════════════════════
# 令牌（已实现）
# ══════════════════════════════════════════════════════════


@router.get("/tokens", response_model=list[ApiTokenOut], summary="令牌列表（只有 prefix）")
def list_tokens(
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> list[ApiTokenOut]:
    """契约 §2.9：**只有 `prefix`，永远没有 `value`**。

    已吊销的令牌也返回（带 `revokedAt`？—— 契约的 `ApiToken` 类型里没有这个字段，
    所以这里**不返回**已吊销的）。契约没定"吊销后还列不列"，实现取
    「不列」：界面上一个灰掉的、永远不能用的令牌没有价值，
    而它的历史价值在 `lastUsedAt` 已经表达过了。
    """
    tokens = [t for t in auth_service.list_tokens(db) if t.revoked_at is None]
    return [ApiTokenOut.model_validate(token) for token in tokens]


@router.post(
    "/tokens",
    status_code=status.HTTP_201_CREATED,
    response_model=CreatedTokenOut,
    summary="创建令牌 —— 完整令牌只在此响应出现一次",
)
def create_token(
    payload: CreateTokenRequest,
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatedTokenOut:
    """⚠️ `plaintext` **只在这里出现一次**。服务端存的是 SHA-256 摘要，
    之后任何接口都拿不到它 —— 这是所有正经产品的做法
    （GitHub、Slack、Stripe 都如此），因为「能再看一次」等于
    「它以可逆形式存着」，而那就等于泄漏时可以直接用。"""
    token, plaintext = auth_service.create_token(
        db,
        label=payload.label,
        scopes=payload.scopes,
        expires_at=payload.expiresAt,
    )
    return CreatedTokenOut(token=ApiTokenOut.model_validate(token), plaintext=plaintext)


@router.delete(
    "/tokens/{tokenId}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="吊销令牌",
)
def revoke_token(
    tokenId: str,
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """软吊销（写 `revoked_at`），不删行 ——

    「这个令牌什么时候没的」在安全复盘时是必须能回答的，
    而删行会让那个问题永远没有答案。"""
    auth_service.revoke_token(db, tokenId)


# ══════════════════════════════════════════════════════════
# 改口令（已实现）
# ══════════════════════════════════════════════════════════


@router.post("/password", response_model=SaveOk, summary="修改管理员口令")
def change_password(
    payload: ChangePasswordRequest,
    identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> SaveOk:
    """★ 改口令后**吊销全部既有会话**。

    这是契约 §1.2 拒绝 JWT 的那条理由的兑现 ——
    「JWT 无法即时吊销（改密码后旧令牌仍有效）」正是它被否掉的原因，
    所以这个行为必须真的发生，否则选不透明令牌就白选了。

    副作用是**当前会话也会失效**，用户需要重新登录。
    这是刻意的：保留某几个旧会话要解释「保留哪一个、为什么」，
    而统一失效的语义不需要解释，也更容易验证。
    """
    if payload.confirm is not None and payload.confirm != payload.next:
        raise ApiError.validation("两次输入的新密码不一致。", {"confirm": "两次输入不一致"})

    account = auth_service.get_account(db)
    if account is None:  # pragma: no cover - require_admin 已保证账号存在
        raise ApiError.unauthenticated()

    auth_service.change_password(db, account, payload.current, payload.next)
    return SaveOk(at=now_utc())


# ══════════════════════════════════════════════════════════
# 沙箱 / 通知（任务 7）
# ══════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════
# 扫描保留规则（v0.13，任务 9）
# ══════════════════════════════════════════════════════════


@router.get("/scan-retention", response_model=ScanRetentionOut, summary="扫描保留规则")
def read_scan_retention(
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> ScanRetentionOut:
    from app.scans import service as scans_service

    config = scans_service.get_retention_config(db)
    return ScanRetentionOut(
        archiveAfterDays=config.archive_after_days,
        trashRetentionDays=config.trash_retention_days,
        artifactRetentionDays=config.artifact_retention_days,
        lastSweepAt=config.last_sweep_at,
    )


@router.put("/scan-retention", response_model=ScanRetentionOut, summary="改扫描保留规则")
def update_scan_retention(
    payload: ScanRetentionIn,
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> ScanRetentionOut:
    """保存后**立刻跑一次清理**。

    不跑的话用户会盯着"30 天"这个数字等下一次启动，
    而"我改了规则但什么都没发生"看起来就是没生效。
    清理本身是幂等的，多跑一次没有副作用。

    ⚠️ `artifactRetentionDays` 为 `None` 时**保持现值**（不是改回默认）——
    理由见 `ScanRetentionIn` 的说明：它是为了消掉"旧前端 + 新后端"
    那个部署窗口，而"不传 = 保持"比"不传 = 用默认值"更安全
    （后者会把用户改过的值悄悄改回去）。
    """
    from app.scans import service as scans_service

    config = scans_service.get_retention_config(db)
    config.archive_after_days = payload.archiveAfterDays
    config.trash_retention_days = payload.trashRetentionDays
    if payload.artifactRetentionDays is not None:
        config.artifact_retention_days = payload.artifactRetentionDays
    db.commit()

    scans_service.sweep_retention(db)
    db.refresh(config)
    return ScanRetentionOut(
        archiveAfterDays=config.archive_after_days,
        trashRetentionDays=config.trash_retention_days,
        artifactRetentionDays=config.artifact_retention_days,
        lastSweepAt=config.last_sweep_at,
    )


# ══════════════════════════════════════════════════════════
# SLA 修复时限（v0.16，任务 10 P3）
# ══════════════════════════════════════════════════════════


def _sla_out(config) -> FindingSlaOut:  # noqa: ANN001
    return FindingSlaOut(
        criticalDays=config.critical_days,
        highDays=config.high_days,
        mediumDays=config.medium_days,
        lowDays=config.low_days,
        infoDays=config.info_days,
        startFrom=config.start_from,
        updatedAt=config.updated_at,
    )


@router.get("/finding-sla", response_model=FindingSlaOut, summary="漏洞修复时限（SLA）")
def read_finding_sla(
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> FindingSlaOut:
    from app.findings import service as findings_service

    return _sla_out(findings_service.sla_config(db))


@router.put("/finding-sla", response_model=FindingSlaOut, summary="改漏洞修复时限")
def update_finding_sla(
    payload: FindingSlaIn,
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> FindingSlaOut:
    """保存后**立刻重算所有未终结漏洞的到期时刻**。

    不重算的话，用户把高危从 7 天改成 3 天，回到列表看到的还是按 7 天
    算出来的倒计时 —— 而"我改了规则但没生效"看起来就是没保存成功。

    只算**未终结**的那些：已关闭/误报本来就不计时（`sla_due_at` 为 NULL），
    重算它们是白做功。

    这件事是**同步**做的（不像保留规则那样丢给清理任务），因为它必须
    立刻可见 —— 而它只更新一列、不删数据，代价可控。
    """
    from sqlalchemy.orm import selectinload

    from app.core.vocab import FindingStatus
    from app.findings import service as findings_service
    from app.findings.models import Finding

    config = findings_service.sla_config(db)
    config.critical_days = payload.criticalDays
    config.high_days = payload.highDays
    config.medium_days = payload.mediumDays
    config.low_days = payload.lowDays
    config.info_days = payload.infoDays
    config.start_from = payload.startFrom
    db.commit()

    open_findings = list(
        db.execute(
            select(Finding)
            # ★ 必须 selectinload 时间线：`start_from=confirmed` 时
            #   `compute_sla_due` 要读它找第一次确认的时刻，而惰性加载
            #   会在几百条上逐条发查询（N+1）—— 那是一次"看起来莫名其妙慢"
            #   的保存，而且只在用户切到 confirmed 起算时才出现。
            .options(selectinload(Finding.timeline))
            .where(Finding.status.notin_((FindingStatus.CLOSED.value, FindingStatus.FALSE_POSITIVE.value)))
        ).scalars()
    )
    if findings_service.refresh_sla(db, open_findings):
        db.commit()
    db.refresh(config)
    return _sla_out(config)


# ══════════════════════════════════════════════════════════
# 「我的视图」（v0.13）
# ══════════════════════════════════════════════════════════


@router.get("/scan-views", response_model=ScanViewsOut, summary="我的视图列表")
def list_scan_views(
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> ScanViewsOut:
    return ScanViewsOut(views=[_view_out(v) for v in _load_views(db)])


@router.put("/scan-views", response_model=ScanViewsOut, summary="覆盖式保存我的视图")
def save_scan_views(
    payload: ScanViewsIn,
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> ScanViewsOut:
    """全量覆盖。

    ★ **存后端不存 localStorage**（项目既有原则：凭据与状态不写浏览器存储）。
    视图里可能含目标名这类敏感信息，而同源的任何脚本都能读 localStorage。
    """
    from app.settings.models import MAX_SCAN_VIEWS, ScanView

    if len(payload.views) > MAX_SCAN_VIEWS:
        raise ApiError.validation(f"视图最多 {MAX_SCAN_VIEWS} 个。")

    # 名字去重：两个同名视图在界面上分不出来，用户会以为其中一个没保存
    names = [v.name.strip() for v in payload.views]
    if len(set(names)) != len(names):
        raise ApiError.validation("视图名称不能重复。", {"views": "有两个视图用了同一个名字"})

    for existing in _load_views(db):
        db.delete(existing)

    for item in payload.views:
        db.add(
            ScanView(
                id=item.id or new_id("view"),
                name=item.name.strip(),
                query=item.query.strip().lstrip("?"),
            )
        )
    db.commit()
    return ScanViewsOut(views=[_view_out(v) for v in _load_views(db)])


def _load_views(db: Session) -> list:  # noqa: ANN001
    from app.settings.models import ScanView

    return list(db.execute(select(ScanView).order_by(ScanView.created_at)).scalars())


def _view_out(view) -> ScanViewOut:  # noqa: ANN001
    return ScanViewOut(id=view.id, name=view.name, query=view.query, createdAt=view.created_at)


@router.get(
    "",
    response_model=SettingsSnapshotOut,
    summary="一次返回 SettingsSnapshot（accounts/sandbox/notices/tokens）",
)
def read_settings(
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> SettingsSnapshotOut:
    """设置页的**一次拉全**。

    ## v0.22 实现 —— 而它此前是 501，那个决定是对的

    这个端点长期是 501，理由写在当时的注释里：

    > 用一个只填了 sandbox 的半成品顶上，调用方会以为自己拿到了完整快照。

    那条判断**至今成立**，本轮也没有推翻它 —— 推翻的是它的前提：
    现在四块（accounts / sandbox / notices / tokens）**都拼得出来**了，
    所以可以真的返回完整快照。

    ⚠️ 这条规矩要留着：**只要还有一块拼不出来，这个端点就该继续 501**。
    回一个缺字段的 200 比 501 糟得多 —— 501 至少说的是实话。

    ## 为什么 `status` / `error` 不在响应里

    前端 `SettingsSnapshot` 上那两个字段是**本地加载状态**（`'loading'`
    / `'ready'` / `'error'` 与一个 `ApiError`）。它们描述的是"这次请求
    进行得怎么样"，而不是服务端的数据 —— 让服务端下发它们等于让
    服务端替前端声明自己的状态机。契约 §3.7 也按这个口径写。
    """
    from app.providers import service as providers_service

    sandbox = _sandbox_out(db)
    rule = settings_service.get_notice_rule(db)
    channels = settings_service.list_channels(db)
    tokens = auth_service.list_tokens(db)

    return SettingsSnapshotOut(
        accounts=[ProviderOut.from_model(account) for account in providers_service.list_providers(db)],
        sandbox=sandbox,
        notices=NoticesOut(
            rule=NoticeRuleOut(
                minSeverity=rule.min_severity,
                notifyOnScanComplete=rule.notify_on_scan_complete,
                notifyOnCriticalImmediate=rule.notify_on_critical_immediate,
                updatedAt=rule.updated_at,
            ),
            channels=[_channel_out(channel) for channel in channels],
        ),
        tokens=[ApiTokenOut.model_validate(token) for token in tokens],
    )


# ══════════════════════════════════════════════════════════
# 沙箱与执行环境（任务 13）
# ══════════════════════════════════════════════════════════


def _sandbox_out(db: Session, *, config=None) -> SandboxOut:  # noqa: ANN001
    """把单例行 + 生效值拼成出参。

    执行类三件套返回的是**生效值**（DB 覆盖 -> env 兜底），
    同时用 `executionSource` 标出它当前来自哪一侧。
    """
    from app.settings import service as settings_service

    config = config if config is not None else settings_service.get_sandbox_config(db)
    effective = settings_service.effective_execution_settings(db)
    return SandboxOut(
        engine=config.engine,
        status=config.status,
        image=config.image,
        cpuLimit=config.cpu_limit,
        memoryLimitMb=config.memory_limit_mb,
        timeoutMinutes=config.timeout_minutes,
        networkIsolated=config.network_isolated,
        allowVolumeWrite=config.allow_volume_write,
        maxConcurrentScans=effective.max_concurrent_scans,
        scanMaxTurns=effective.scan_max_turns,
        scanMode=effective.scan_mode,
        executionSource=ExecutionSourceOut(
            # `is not None` 而不是真值判断：`0` 是合法输入（虽然会被区间校验挡住），
            # 用真值判断会让"设过 0"显示成"没设过"。
            maxConcurrentScans=("user" if config.max_concurrent_scans is not None else "default"),
            scanMaxTurns="user" if config.scan_max_turns is not None else "default",
            scanMode="user" if config.scan_mode else "default",
        ),
        # ★ v0.42：原样回读。它没有 env 兜底，所以不经过 `effective`、
        # 也不出现在 `executionSource` 里 —— 那一组只标"DB 覆盖还是 env 兜底"，
        # 而这一列只有 DB 一个来源，标它反而会暗示存在另一个。
        enableOffensivePrompt=config.enable_offensive_prompt,
        updatedAt=config.updated_at,
    )


@router.get("/sandbox", response_model=SandboxOut, summary="沙箱与执行环境配置")
def read_sandbox(
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> SandboxOut:
    """读沙箱与执行配置。

    ⚠️ 没有单独写一个 `GET /settings` 而是加了这个：那个端点的契约是
    "四块快照"，而通知那两块还没实现。返回一个缺一半的 200 比 501 更糟 ——
    调用方无从知道少了什么。
    """
    return _sandbox_out(db)


@router.put("/sandbox", response_model=SandboxOut, summary="改沙箱与执行环境配置")
def update_sandbox(
    payload: SandboxIn,
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> SandboxOut:
    """保存沙箱与执行配置。

    ## ★ 不需要重启服务

    * **并发**（`maxConcurrentScans`）—— 调度器**每 tick 重读**，
      保存后下一个 tick（默认 2 秒）就生效；
    * **回合上限 / 扫描深度** —— 在**启动扫描时**读取，对之后新发起的
      扫描生效；正在跑的那次不受影响（参数已经进了它的命令行）。

    这一点在本系统里不是锦上添花：`recover_orphans()` 会在服务重启时
    **终止所有正在跑的扫描**（见 scheduler 的说明），所以"改个配置要重启"
    的代价是**杀掉用户正在解的那道题**。

    ## 为什么校验放在 service 而不是靠 Field 约束

    区间同时写在 schema（`Field(ge=, le=)`）与 service 里，看着重复，
    但两者挡的不是同一件事：schema 挡的是 HTTP 入参，而 service 是可被
    内部调用（比如将来的 CLI）复用的那一层。真正要紧的是**都不静默截断** ——
    把用户填的 999 悄悄改成 8 并返回成功，他下次会看到一个自己没设过的值。
    """
    from app.settings import service as settings_service

    settings_service.validate_execution_settings(
        max_concurrent_scans=payload.maxConcurrentScans,
        scan_max_turns=payload.scanMaxTurns,
        scan_mode=payload.scanMode,
    )

    config = settings_service.get_sandbox_config(db)
    config.image = payload.image
    config.cpu_limit = payload.cpuLimit
    config.memory_limit_mb = payload.memoryLimitMb
    config.timeout_minutes = payload.timeoutMinutes
    config.network_isolated = payload.networkIsolated
    config.allow_volume_write = payload.allowVolumeWrite
    # ★ 三件套**写具体值**（不再是 NULL）—— 从此这一侧就是"用户覆盖过"。
    config.max_concurrent_scans = payload.maxConcurrentScans
    config.scan_max_turns = payload.scanMaxTurns
    config.scan_mode = payload.scanMode
    # ★ v0.42：手法块开关。同样**写具体值**，且它与上面三件套不同的是
    # 它根本没有 NULL 这一档 —— 列非空、默认 false。
    config.enable_offensive_prompt = payload.enableOffensivePrompt
    db.commit()
    db.refresh(config)
    return _sandbox_out(db, config=config)


# ══════════════════════════════════════════════════════════
# 通知（v0.22 —— 本模块最后一块）
# ══════════════════════════════════════════════════════════
#
# ## 规则与渠道是**两个端点、两个维度**
#
# `NoticeRule`（什么情况下发）是**单例**：它必须有值。
# `NotifyChannel`（怎么发）是**列表**：空表是正常状态。
#
# 合成一个对象会让「邮件收全部、Slack 只收严重」无处表达 ——
# 契约 §2.9 有完整论证。


def _channel_out(channel) -> NotifyChannelOut:  # noqa: ANN001
    return NotifyChannelOut(
        id=channel.id,
        kind=channel.kind,
        label=channel.label,
        target=channel.target,
        enabled=channel.enabled,
        verified=channel.verified,
        verifyError=channel.verify_error,
        createdAt=channel.created_at,
        updatedAt=channel.updated_at,
    )


def _rule_out(rule) -> NoticeRuleOut:  # noqa: ANN001
    return NoticeRuleOut(
        minSeverity=rule.min_severity,
        notifyOnScanComplete=rule.notify_on_scan_complete,
        notifyOnCriticalImmediate=rule.notify_on_critical_immediate,
        updatedAt=rule.updated_at,
    )


@router.get("/notices/rule", response_model=NoticeRuleOut, summary="通知规则")
def read_notice_rule(
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> NoticeRuleOut:
    """读通知规则。**没有行就建一条默认的** —— 规则是单例，它必须有值。"""
    return _rule_out(settings_service.get_notice_rule(db))


@router.put("/notices/rule", response_model=NoticeRuleOut, summary="改通知规则")
def update_notice_rule(
    payload: NoticeRuleIn,
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> NoticeRuleOut:
    """改通知规则。

    ⚠️ `minSeverity` 走**白名单校验**，不是自由字符串：
    它是 `Severity` 的取值之一，而 DB 上还有一条 CHECK 约束
    （`min_severity_valid`）兜底。这里先校验是为了给出**中文错误消息** ——
    让用户看到"应为 critical、high、medium、low、info 之一"，
    而不是一条 Postgres 的约束违反错误。
    """
    if payload.minSeverity not in set(values(Severity)):
        raise ApiError.validation({"minSeverity": f"应为 {'、'.join(values(Severity))} 之一"})

    rule = settings_service.get_notice_rule(db)
    rule.min_severity = payload.minSeverity
    rule.notify_on_scan_complete = payload.notifyOnScanComplete
    rule.notify_on_critical_immediate = payload.notifyOnCriticalImmediate
    db.commit()
    db.refresh(rule)
    return _rule_out(rule)


@router.get("/notices/channels", response_model=list[NotifyChannelOut], summary="通知渠道列表")
def list_channels(
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> list[NotifyChannelOut]:
    """**裸数组**，不是 `{channels: [...]}` —— 与 `GET /settings/tokens`
    同形状，前端两处可以共用同一个渲染组件（契约 §3.7）。"""
    return [_channel_out(channel) for channel in settings_service.list_channels(db)]


@router.post(
    "/notices/channels",
    status_code=201,
    response_model=NotifyChannelOut,
    summary="新增通知渠道",
)
def create_channel(
    payload: NotifyChannelIn,
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> NotifyChannelOut:
    """新建渠道。

    ⚠️ `verified` **不接受客户端传**（schema 里就没有这个字段）——
    "已验证"是服务端真的发过一次测试通知的**事实**，不是用户的声明。
    让请求体能带它，界面上那个绿勾就变得毫无意义。
    """
    _validate_channel_kind(payload.kind)
    channel = settings_service.create_channel(
        db,
        kind=payload.kind,
        label=payload.label,
        target=payload.target,
        enabled=payload.enabled,
    )
    return _channel_out(channel)


@router.patch(
    "/notices/channels/{channelId}",
    response_model=NotifyChannelOut,
    summary="修改通知渠道",
)
def update_channel(
    channelId: str,
    payload: NotifyChannelPatch,
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> NotifyChannelOut:
    """部分更新。改 `target` 会把 `verified` 打回 `False`（见 service 的说明）。"""
    channel = settings_service.update_channel(
        db,
        channelId,
        label=payload.label,
        target=payload.target,
        enabled=payload.enabled,
    )
    return _channel_out(channel)


@router.delete("/notices/channels/{channelId}", status_code=204, summary="删除通知渠道")
def delete_channel(
    channelId: str,
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """**硬删除**。渠道是一次性配置，不像漏洞/扫描那样是证据。"""
    settings_service.delete_channel(db, channelId)


@router.post(
    "/notices/channels/{channelId}/test",
    response_model=NotifyChannelOut,
    summary="发测试通知（动作型）",
)
def test_channel(
    channelId: str,
    _identity: Annotated[Identity, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> NotifyChannelOut:
    """发一条测试通知，并把结果记进 `verified` / `verifyError`。

    ## v0.89：这一步已经**真的发**了

    v0.22 那一版只做本地校验，并在 `verifyError` 里诚实地写"真实发送尚未
    接入"——那时这是对的，因为假装发了会让界面显示一个绿勾、而真出事时
    一条通知都发不出去。

    现在 webhook 与 slack 两条链路已经在 `app/settings/dispatch.py` 里接上
    （同一件事的两种载荷，都是 `POST` JSON），所以这里改成：

        形状校验 → 真发一次 → 按**真实结果**写 verified / verifyError

    ## 三条边界

    * **邮件仍然不发**：SMTP 需要凭据与证书信任策略，`smtplib` 还是同步
      阻塞的。这一轮不做半吊子实现，`verifyError` 里照实写"尚未接入"。
    * **测试通知不进业务规则**：它不看 `min_severity`、不看
      `notify_on_scan_complete` —— 用户点"发送测试"就是想现在收到一条。
    * **失败一定带原因**：`verified=False` 时 `verifyError` 不能为空，
      否则界面上只剩一个红点，用户无从知道该改哪里。
    """
    channel = settings_service.get_channel(db, channelId)

    # ① 地址形状。形状不对就别真发 —— 那只会得到一次注定失败的请求，
    #    而且是**出站**的（形状错的地址常常仍指向一台真实的外部主机）。
    problem = _target_shape_problem(channel.kind, channel.target)
    if problem is not None:
        channel = settings_service.mark_channel_verified(db, channelId, ok=False, error=problem)
        return _channel_out(channel)

    # ② 这条渠道这一轮发得出去吗（邮件发不出去、未知类型发不出去）。
    unsupported = dispatch.channel_problem(channel.kind, channel.target)
    if unsupported is not None:
        channel = settings_service.mark_channel_verified(db, channelId, ok=False, error=unsupported)
        return _channel_out(channel)

    # ③ 真发。
    #    ★ 形状校验已经 commit 过（`mark_channel_verified` 里有 commit），
    #    所以这里**没有**开着的写事务，网络 I/O 不夹在两次 commit 之间。
    #    这与 `finalize_scan` 那条「持锁窗口不跨 I/O」是同一条约束。
    outcome = dispatch.dispatch_one(channel, _probe_payload(channel))

    channel = settings_service.mark_channel_verified(
        db, channelId, ok=outcome.sent, error=None if outcome.sent else outcome.reason
    )
    return _channel_out(channel)


def _probe_payload(channel) -> dispatch.NoticePayload:  # noqa: ANN001
    """测试通知的载荷。

    ★ 文案里写明「测试」，因为这条会真的到达用户的 Slack / 工单系统里。
    一条长得像真实告警的测试消息会让人半夜爬起来看。`scan_id` 留空、
    `scan_url` 为 `None` —— 它没有对应的扫描，编一个 id 出来只会制造
    "点进去 404"。
    """
    return dispatch.NoticePayload(
        scan_id="",
        target=f"[测试] {channel.label}",
        status="completed",
        summary="这是一条来自 Suvex 的测试通知 —— 收到它说明这个渠道是通的",
        severity="info",
    )


def _validate_channel_kind(kind: str) -> None:
    """渠道类型白名单。DB 上有 CHECK 兜底，这里先校验是为了中文消息。"""
    if kind not in set(values(NotifyChannelKind)):
        raise ApiError.validation({"kind": f"应为 {'、'.join(values(NotifyChannelKind))} 之一"})


def _target_shape_problem(kind: str, target: str) -> str | None:
    """按渠道类型校验地址形状。返回问题描述，没问题返回 `None`。

    三类各自的判据**刻意宽松**：这是配置校验，不是安全边界。
    过严会把合法地址挡在门外（内网 SMTP、带端口的 webhook、
    非标准 TLD），而用户无法绕过 —— 与 `validate-target` 那条
    "刻意不做得太严"是同一条理由（契约 §2.8）。
    """
    if kind == NotifyChannelKind.EMAIL.value:
        # 只要求有个 @ 且两边都非空 —— 邮箱的完整正则是个无底洞，
        # 而"发不出去"最终由真实发送来判定
        local, _, domain = target.partition("@")
        if not local or not domain:
            return "邮箱地址缺少 @ 或 @ 两侧为空。"
        return None

    if kind in (NotifyChannelKind.WEBHOOK.value, NotifyChannelKind.SLACK.value):
        if not target.startswith(("http://", "https://")):
            return "Webhook 地址必须以 http:// 或 https:// 开头。"
        return None

    return f"未知的渠道类型：{kind}"
