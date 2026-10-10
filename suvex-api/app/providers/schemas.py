"""`providers` 领域的出入参。

## ★ 出参里凭据相关的三个字段，各管一件事

| 字段         | 值                                   | 谁用                        |
| ------------ | ------------------------------------ | --------------------------- |
| `apiKey`     | **恒为空串**                         | 契约要求的"不回填"位         |
| `hasKey`     | `boolean`                            | 区分"配了但不回填"与"没配"   |
| `apiKeyMask` | `sk-****abcd` 或空串                 | 界面上显示"有哪一把钥匙"     |

**为什么不直接把掩码放进 `apiKey`** —— 这是本任务里最容易埋雷的一处，
理由值得写全：

前端 `ProviderPanel` 的保存路径是这样拼 payload 的（`handleSave` / `handleVerify`）：

```ts
apiKey: keyEdited ? draft.apiKey : account.apiKey
```

也就是**没动 key 时把 `account.apiKey` 原样带回服务端**。这是"密钥不回填"能成立的
前提（输入框是空的，但保存时必须带上原值，否则改个显示名就把凭据清空了）。

如果服务端在这里返回掩码，那么用户只改了一个显示名、点保存，
服务端收到的就是 `sk-****abcd` —— 而它会作为**新的 API key** 被加密存下来。
真凭据被静默毁掉，界面还显示"已保存"，没有任何报错。
这个 bug 只在"后端返回掩码" + "这个表单"两者相遇时才出现。

把掩码放进独立字段，`apiKey` 永远是空串，上面那条 payload 拼装就自然正确。
**这是"让连接本身不产生危险"**，而不是"加一层校验挡住危险"。

（服务端仍然加了一道 `looks_like_mask` 兜底 —— 见 `service._apply_key`。
但那是第二道防线，不是设计依据。）
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core.schemas import CamelModel, StrictModel
from app.core.timeutil import IsoDateTime

ProviderKindLiteral = Literal["cloud", "local"]


class ProviderOut(CamelModel):
    """provider 的完整对外形状。字段名与前端 `ProviderAccount` 逐字一致。

    ⚠️ 从模型构造**必须走 `from_model`**，不要用 `model_validate(orm_object)`：
    后者要求 ORM 上存在同名属性，而 `api_key` 属性在模型上是**刻意不存在**的
    （见 `models.ProviderAccount` 的说明）。走 `from_model` 时
    `apiKey` 是硬编码的空串 —— 这条路径上没有任何变量能通向明文。
    """

    id: str
    label: str
    model: str
    kind: str
    # 恒为空串。见模块 docstring。
    apiKey: str = ""
    hasKey: bool
    apiKeyMask: str
    baseUrl: str
    isDefault: bool
    verified: bool
    verifyError: str | None
    note: str

    @classmethod
    def from_model(cls, provider) -> "ProviderOut":  # noqa: ANN001 - 避免 core→providers 的循环导入
        return cls(
            id=provider.id,
            label=provider.label,
            model=provider.model,
            kind=provider.kind,
            # ★ 硬编码。没有任何分支会把它填成别的值。
            apiKey="",
            hasKey=provider.has_key,
            apiKeyMask=provider.api_key_mask,
            baseUrl=provider.base_url,
            isDefault=provider.is_default,
            verified=provider.verified,
            verifyError=provider.verify_error,
            note=provider.note,
        )


class ProviderCreate(StrictModel):
    """`POST /providers`（契约 §3.2）。

    ⚠️ `verified` / `verifyError` **不在请求体里** —— 它们是服务端owned 的。
    放进来的后果是客户端可以自称"已验证可用"，而新建扫描向导的 `configured`
    读的正是它。`extra="forbid"` 会让它们被明确拒绝，而不是被静默忽略
    （静默忽略同样是漏洞：前端会以为设置生效了）。
    """

    label: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=1, max_length=160)
    kind: ProviderKindLiteral
    # 空串 = 没配凭据。合法状态（本地模型本来就不需要；云端也可以先建条目后补 key）。
    apiKey: str = ""
    baseUrl: str = ""
    note: str = ""
    isDefault: bool = False


class ProviderUpdate(StrictModel):
    """`PATCH /providers/{id}`。**部分更新**：省略的字段不动。

    全部可空是刻意的：`None` 表示"这次不改这一项"，与"改成空值"是两回事。
    尤其是 `apiKey` —— 契约 §2.9 那条「省略 `apiKey` 或传空串 = 保持原值不变」，
    就是靠这个区分实现的。漏掉它，用户改个显示名就会把凭据清空，
    而前端注释里明确记过这个隐蔽 bug。
    """

    label: str | None = Field(default=None, min_length=1, max_length=40)
    model: str | None = Field(default=None, min_length=1, max_length=160)
    kind: ProviderKindLiteral | None = None
    apiKey: str | None = None
    baseUrl: str | None = None
    note: str | None = None
    isDefault: bool | None = None


class ProviderSavedOk(CamelModel):
    """保存成功（契约 §3.7 的 `SaveResult.ok`）。"""

    status: Literal["ok"] = "ok"
    at: IsoDateTime
    provider: ProviderOut


class ProviderSavedNoChange(CamelModel):
    """**没有任何实际改动**。

    ⚠️ 这是独立一态，不是 `ok` 的一种。用户点了保存但没改过任何东西，
    不能报「已保存」—— 那是虚假的动作感。前端 `SaveState` 状态机有五个态
    （`idle/saving/saved/failed/unchanged`），后端要能表达 `nochange`
    才能驱动它（契约 §3.7）。

    注意这里**没有 `at`**：没有发生动作，就没有"动作发生时刻"。
    给一个时间戳会让"已保存于 xx:xx"这种显示有可能被渲染出来。
    """

    status: Literal["nochange"] = "nochange"
    provider: ProviderOut


# 保存类端点的联合。**不含 `failed`** —— 失败走统一错误信封（§1.3），
# 由前端的 api 封装转成 `SaveResult.failed`。在 200 里再放一个 failed
# 会让"HTTP 200 但业务失败"变成第二种失败语义，而两套语义必然分叉。
ProviderSaveResult = ProviderSavedOk | ProviderSavedNoChange


class ProviderVerifyOk(CamelModel):
    """探测成功。形状是契约 §3.2 的 `ActionResult` 加上任务 2 要求的耗时。"""

    status: Literal["ok"] = "ok"
    at: IsoDateTime
    # 真实往返耗时（毫秒）。不是估算 —— 用户看它就是为了判断这条链路慢不慢。
    durationMs: int
    # 实际请求的 URL。**不含凭据**，可以安全展示；
    # 它对排查"baseUrl 填错了"极有价值。
    endpoint: str
    # 模型的回话（截断）。它同时是"真的打通了"的证据。
    reply: str | None
    # 探测会写回 verified / verifyError，所以把更新后的 provider 一并返回 ——
    # 让前端不必再发一次请求（而再发一次会取到"别人的验证结果"这种竞态）。
    provider: ProviderOut


class ProviderVerifyFailed(CamelModel):
    """探测失败。

    ⚠️ **失败不是 HTTP 错误**：契约 §3.2 的措辞是「验证失败 = 存进去了但打不通」，
    所以它必须返回 `200` + `status: "failed"`，而不是 4xx/5xx。
    返回 4xx 会让前端把它当成"保存失败"，而用户不知道该重填还是该重试 ——
    这正好是契约里论证「保存与验证必须是两个动作」时举的那个例子。
    """

    status: Literal["failed"] = "failed"
    # 可直接展示的中文一句话。
    error: str
    durationMs: int
    endpoint: str
    provider: ProviderOut


ProviderVerifyResult = ProviderVerifyOk | ProviderVerifyFailed
