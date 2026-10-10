"""`settings` 领域路由（契约 §3.7）。

| 方法   | 路径                                   | 状态            |
| ------ | -------------------------------------- | --------------- |
| GET    | `/settings`                            | 501（任务 7）   |
| GET    | `/settings/sandbox`                    | **已实现**      |
| PUT    | `/settings/sandbox`                    | **已实现**      |
| PUT    | `/settings/notices/rule`               | 501（任务 7）   |
| POST   | `/settings/notices/channels`           | 501（任务 7）   |
| PATCH  | `/settings/notices/channels/{id}`      | 501（任务 7）   |
| DELETE | `/settings/notices/channels/{id}`      | 501（任务 7）   |
| POST   | `/settings/notices/channels/{id}/test` | 501（任务 7）   |
| GET    | `/settings/tokens`                     | **已实现**      |
| POST   | `/settings/tokens`                     | **已实现**      |
| DELETE | `/settings/tokens/{id}`                | **已实现**      |
| POST   | `/settings/password`                   | **已实现**      |

令牌管理与改口令属于**认证**，所以在任务 1 就实现（它们直接用
`app/auth/service.py`，没有额外业务逻辑）。沙箱与通知是真正的
settings 内容，属于任务 7。

⚠️ 实现沙箱配置时的两条约束（契约 §2.9 / §6）：

* `networkIsolated` 的默认值是 **`false`（允许出网）**。这不是"更安全就一定
  更好"的开关 —— 扫描经常需要出网打外部目标。打开它会让针对外部目标的
  扫描直接失败，所以界面上必须说清这个取舍。
* `PUT /settings/sandbox` 带 `allowVolumeWrite=true` 时属于高风险操作，
  前端已有文案说明"仅在你确信目标代码可丢弃时开启"，沿用为准。

## 任务 13：执行类配置进了这个端点

`sandbox_config` 新增了三列（并发 / 回合上限 / 扫描深度），它们与沙箱配置
一起在这个端点上读写 —— 因为它们回答的是同一类问题："扫描跑起来是什么样"。

`GET /settings` 那个"四块快照"**仍然没实现**，这一点是刻意的：
用一个只填了 sandbox 的半成品顶上，调用方会以为自己拿到了完整快照。
要读沙箱请用 `/settings/sandbox`。
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
from app.core.stubs import pending
from app.core.timeutil import now_utc
from app.settings.schemas import (
    ChangePasswordRequest,
    CreateTokenRequest,
    ExecutionSourceOut,
    FindingSlaIn,
    FindingSlaOut,
    SandboxIn,
    SandboxOut,
    ScanRetentionIn,
    ScanRetentionOut,
    ScanViewOut,
    ScanViewsIn,
    ScanViewsOut,
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
    """
    from app.scans import service as scans_service

    config = scans_service.get_retention_config(db)
    config.archive_after_days = payload.archiveAfterDays
    config.trash_retention_days = payload.trashRetentionDays
    db.commit()

    scans_service.sweep_retention(db)
    db.refresh(config)
    return ScanRetentionOut(
        archiveAfterDays=config.archive_after_days,
        trashRetentionDays=config.trash_retention_days,
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
            .where(
                Finding.status.notin_(
                    (FindingStatus.CLOSED.value, FindingStatus.FALSE_POSITIVE.value)
                )
            )
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
        raise ApiError.validation(
            "视图名称不能重复。", {"views": "有两个视图用了同一个名字"}
        )

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
    return ScanViewOut(
        id=view.id, name=view.name, query=view.query, createdAt=view.created_at
    )


@router.get("", summary="一次返回 SettingsSnapshot（accounts/sandbox/notices/tokens）")
def read_settings(_identity: Annotated[Identity, Depends(require_admin)]) -> None:
    # ⚠️ 仍然没实现，而且**不打算**用一个只填了 sandbox 的半成品顶上：
    # 这个端点的契约是"一次返回四块快照"，缺三块却返回 200，
    # 调用方会以为自己拿到了完整快照。要读沙箱用下面那个专门的端点。
    pending(_TASK, "设置快照")


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
            maxConcurrentScans=(
                "user" if config.max_concurrent_scans is not None else "default"
            ),
            scanMaxTurns="user" if config.scan_max_turns is not None else "default",
            scanMode="user" if config.scan_mode else "default",
        ),
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
    db.commit()
    db.refresh(config)
    return _sandbox_out(db, config=config)


@router.put("/notices/rule", summary="通知规则")
def update_notice_rule(_identity: Annotated[Identity, Depends(require_admin)]) -> None:
    pending(_TASK, "通知规则")


@router.post("/notices/channels", status_code=201, summary="新增通知渠道")
def create_channel(_identity: Annotated[Identity, Depends(require_admin)]) -> None:
    pending(_TASK, "通知渠道")


@router.patch("/notices/channels/{channelId}", summary="修改通知渠道")
def update_channel(
    channelId: str, _identity: Annotated[Identity, Depends(require_admin)]
) -> None:
    pending(_TASK, "通知渠道")


@router.delete(
    "/notices/channels/{channelId}", status_code=204, summary="删除通知渠道"
)
def delete_channel(
    channelId: str, _identity: Annotated[Identity, Depends(require_admin)]
) -> None:
    pending(_TASK, "通知渠道")


@router.post("/notices/channels/{channelId}/test", summary="发测试通知（动作型）")
def test_channel(
    channelId: str, _identity: Annotated[Identity, Depends(require_admin)]
) -> None:
    pending(_TASK, "测试通知")
