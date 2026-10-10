"""动作型响应的两个形状（契约 §3.7）。

前端把「保存」和「验证」分成两个动作，因为语义不同：
**保存失败 = 没存进去；验证失败 = 存进去了但打不通。**
混成一个，用户不知道该重填还是该重试。所以后端也必须有两个形状。

```ts
SaveResult   = {status:'ok', at} | {status:'nochange'} | {status:'failed', error}
ActionResult = {status:'ok', at} | {status:'failed', error}      // 没有 nochange
```

⚠️ **`nochange` 是独立一态**，不是 `ok` 的一种。用户点了保存但没改过任何东西，
**不能报「已保存」** —— 那是虚假的动作感。前端 `SaveState` 状态机有五个态
（`idle/saving/saved/failed/unchanged`），后端要能表达 `nochange` 才能驱动它。

而 `ActionResult` **刻意没有 `nochange`**：验证是一个动作，
"验证通过"本身就是结果，不存在"什么都没变"这种状态。
给它加 `nochange` 会让调用方多一个永远走不到的分支。
"""

from __future__ import annotations

from typing import Literal

from app.core.schemas import CamelModel
from app.core.timeutil import IsoDateTime


class SaveOk(CamelModel):
    status: Literal["ok"] = "ok"
    at: IsoDateTime


class SaveNoChange(CamelModel):
    status: Literal["nochange"] = "nochange"


class SaveFailed(CamelModel):
    status: Literal["failed"] = "failed"
    # 可直接展示的中文完整句。
    error: str


class ActionOk(CamelModel):
    status: Literal["ok"] = "ok"
    at: IsoDateTime


class ActionFailed(CamelModel):
    status: Literal["failed"] = "failed"
    error: str


SaveResult = SaveOk | SaveNoChange | SaveFailed
ActionResult = ActionOk | ActionFailed
