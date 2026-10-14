"""`providers` 领域路由（契约 §3.2）。

| 方法   | 路径                      | 鉴权  |
| ------ | ------------------------- | ----- |
| GET    | `/providers`              | admin |
| POST   | `/providers`              | admin |
| PATCH  | `/providers/{providerId}` | admin |
| DELETE | `/providers/{providerId}` | admin |
| POST   | `/providers/{providerId}/verify`  | admin |
| POST   | `/providers/{providerId}/default` | admin |

**全部是 `admin` 级**：能改 provider 就能改"扫描会调用谁"，
而且这套接口能读到掩码、能触发对外请求 —— 这不是只读操作。

## 两条容易搞错的边界

1. **`verify` 失败返回 `200` 而不是 4xx。** 契约 §3.2 把「验证失败」定义为
   「存进去了但打不通」——它是一个**正常的业务结果**，不是请求错误。
   返回 4xx 会让前端走"保存失败"的分支，而用户不知道该重填还是该重试。
   这正好是契约论证「保存与验证必须是两个动作」时举的那个例子。

2. **响应里永远不会出现明文凭据。** 三个相关字段各管一件事
   （`apiKey` 恒空 / `hasKey` / `apiKeyMask`），
   理由见 `schemas.py` 的模块 docstring —— 那里也写了
   "为什么不能把掩码放进 `apiKey`"。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.auth.deps import Identity, require_admin, require_read
from app.core.db import get_db
from app.core.timeutil import now_utc
from app.providers import service
from app.providers.schemas import (
    ProviderCreate,
    ProviderOut,
    ProviderSavedNoChange,
    ProviderSavedOk,
    ProviderSaveResult,
    ProviderUpdate,
    ProviderVerifyFailed,
    ProviderVerifyOk,
    ProviderVerifyResult,
)

router = APIRouter(prefix="/providers", tags=["LLM Provider"])

AdminDep = Annotated[Identity, Depends(require_admin)]
ReadDep = Annotated[Identity, Depends(require_read)]
DbDep = Annotated[Session, Depends(get_db)]


@router.get("", response_model=list[ProviderOut], summary="provider 列表（不含明文凭据）")
def list_providers(_identity: ReadDep, db: DbDep) -> list[ProviderOut]:
    """列出 provider。

    ## ★ v0.24 把这一条的鉴权从 `admin` 放宽到 `read`

    这不是"顺手放宽"，而是**只读角色能用的前提**：

    前端顶部状态条要显示"当前模型 XXX"，而它读的就是这个端点
    （`Layout` 的 `ensureSettingsLoaded()`）。若它要求 `admin`，
    那么一个 `viewer` 登录后会看到状态条报错 ——
    而那个错误看起来像"系统坏了"，不像是权限问题。

    放宽是安全的，因为**这个端点不返回任何明文凭据**：
    `apiKey` 恒为空串，只有 `apiKeyMask`（见 ADR-003）。
    写操作（新建 / 改 / 删 / 验证 / 设默认）**仍然是 admin**。

    这条判断可以推广成一句话：**"能不能看到"与"能不能改"是两件事**，
    而角色模型的分界线主要画在后者上。例外是那些**内容本身就是秘密**
    的东西（用户列表、审计日志），它们连看都要 admin。
    """
    return [ProviderOut.from_model(provider) for provider in service.list_providers(db)]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ProviderSaveResult,
    summary="新建 provider",
)
def create_provider(
    payload: ProviderCreate, _identity: AdminDep, db: DbDep
) -> ProviderSaveResult:
    """新建。返回 `SaveResult` 形状并附带新建的 provider。

    附带 provider 是必需的：`id` 由**服务端**分配（契约 §1.1 的字符串主键），
    客户端必须拿到它才能做后续的 PATCH / DELETE / verify。
    """
    provider = service.create_provider(db, payload)
    return ProviderSavedOk(at=now_utc(), provider=ProviderOut.from_model(provider))


@router.patch(
    "/{providerId}", response_model=ProviderSaveResult, summary="更新 provider（省略 apiKey = 不改凭据）"
)
def update_provider(
    providerId: str, payload: ProviderUpdate, _identity: AdminDep, db: DbDep
) -> ProviderSaveResult:
    """部分更新。

    ⚠️ **省略 `apiKey`、传空串、以及传回当前掩码，三种都表示「不改凭据」。**
    最后一种不是设计，是防御：任何一个"读出来、改一个字段、整体提交"的
    客户端都可能把掩码带回来，而它一旦被当成新 key 存下去，
    真凭据就被静默毁掉了。见 `crypto.looks_like_mask`。
    """
    provider, changed = service.update_provider(db, providerId, payload)
    out = ProviderOut.from_model(provider)
    if not changed:
        # 什么都没改 —— 报 `nochange` 而不是 `ok`（契约 §3.7）。
        return ProviderSavedNoChange(provider=out)
    return ProviderSavedOk(at=now_utc(), provider=out)


@router.delete(
    "/{providerId}", status_code=status.HTTP_204_NO_CONTENT, summary="删除 provider"
)
def delete_provider(providerId: str, _identity: AdminDep, db: DbDep) -> None:
    """删除。两种情况拒绝，都是 409 但**错误码不同**：

    * `PROVIDER_IS_DEFAULT` —— 先去把另一个设为默认；
    * `PROVIDER_IN_USE` —— 先去终止那些未结束的扫描（响应里会列出是哪几次）。
    """
    service.delete_provider(db, providerId)


@router.post(
    "/{providerId}/verify",
    response_model=ProviderVerifyResult,
    summary="测试连接（动作型，返回成功/失败与真实耗时）",
)
def verify_provider(providerId: str, _identity: AdminDep, db: DbDep) -> ProviderVerifyResult:
    """用**已保存的**配置发一次最小化请求。

    响应里三类信息缺一不可：

    * `status` —— 成功/失败，驱动界面上那两个不同的分支；
    * `durationMs` —— **真实往返耗时**，用户看它就是为了判断这条链路慢不慢；
    * `provider` —— 验证会写回 `verified` / `verifyError`，一并返回更新后的状态，
      前端不必再发一次请求（再发一次会取到"别人的验证结果"这种竞态）。
    """
    provider, outcome = service.verify_provider(db, providerId)
    out = ProviderOut.from_model(provider)

    if outcome.ok:
        return ProviderVerifyOk(
            at=now_utc(),
            durationMs=outcome.duration_ms,
            endpoint=outcome.endpoint,
            reply=outcome.reply,
            provider=out,
        )
    return ProviderVerifyFailed(
        # 兜底文案：`probe` 保证失败时一定有 error，这里是防御性的。
        error=outcome.error or "验证失败，但对方没有给出原因。",
        durationMs=outcome.duration_ms,
        endpoint=outcome.endpoint,
        provider=out,
    )


@router.post(
    "/{providerId}/default", response_model=ProviderSaveResult, summary="设为默认"
)
def set_default_provider(
    providerId: str, _identity: AdminDep, db: DbDep
) -> ProviderSaveResult:
    """切换默认。

    互斥性由**数据库的部分唯一索引** `uq_provider_accounts_default` 强制，
    而不是只靠应用层判断 —— 两个并发请求同时"设为默认"时，
    应用层各自都会看到"当前没有冲突"，然后都写下去。
    唯一索引让第二个事务直接失败，而不是静默留下两个默认。
    """
    provider, changed = service.set_default(db, providerId)
    out = ProviderOut.from_model(provider)
    if not changed:
        return ProviderSavedNoChange(provider=out)
    return ProviderSavedOk(at=now_utc(), provider=out)
