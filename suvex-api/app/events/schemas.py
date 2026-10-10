"""实时事件流的出入参（契约 §2.4 / §2.5 / §4.2）。

⚠️ **两个形状，别搞混**（契约 §2.5 标了这是契约里最容易出错的一处）：

|          | 完整事件（这里发的）      | Overview 投影（前端派生）                          |
| -------- | ------------------------- | -------------------------------------------------- |
| 字段     | `id, scanId, agentId, agentPhase, type, summary, detail, at` | `id, scanId, agentId, agentPhase, type, summary, subject, at` |
| 判别式   | `detail.kind`（8 种载荷） | `agentPhase` + `subject`（扁平）                   |
| 事件类型 | **8 种**                  | **6 种**（少 `reasoning` 和 `failed`）             |

后端**只发完整事件**。那 6 种投影是 `data/overview.ts` 里的展示层视图模型，
是为「一行摘要」渲染优化的形状 —— 让后端产出它等于把展示逻辑搬到后端，
UI 一改摘要规则就要动后端。

⚠️ 但 `agentPhase` 是**例外**：它在投影里有、完整事件里原本没有，
而 `ActivityFeedItem` 渲染阶段标签（「利用中」）时必须它。
所以契约要求完整事件里**也带** `agentPhase` —— 这是本次契约相对
前端现状的唯一一处新增字段。
"""

from __future__ import annotations

from typing import Any

from app.core.schemas import CamelModel
from app.core.timeutil import IsoDateTime


class AgentOut(CamelModel):
    """`GET /scans/{id}/agents` 的条目（契约 §2.4）。

    `eventCount` / `lastActiveAt` 是**初始值 / 兜底值**：前端目前会从事件流
    派生并覆盖它们。这不是重复 —— 它让「agent 失败了但没有后续事件」
    这种情况能被表达（纯派生的话，没有事件就等于没有状态变化）。
    """

    id: str
    role: str
    status: str
    target: str
    scanId: str
    eventCount: int
    lastActiveAt: IsoDateTime


class ActivityEventOut(CamelModel):
    """完整事件（契约 §2.5）。`detail` 的形状由 `detail.kind` 判别。"""

    id: str
    scanId: str
    # 非空 —— 前端 `ActivityEvent.agentId` 声明是 `string`（见 events/models.py）。
    agentId: str
    # 契约相对前端现状唯一的新增字段。
    agentPhase: str
    type: str
    summary: str
    # 8 种载荷之一，**整体可为 null**（纯状态事件）。
    #
    # 用 `dict[str, Any]` 而不是 8 个 Pydantic 子模型：detail 由事件产生方
    # 构造，后端不解释它的内容。8 个子模型会引入一处**必须与前端保持同步**
    # 的结构定义 —— 那是第二份 schema，而第二份 schema 必然漂移。
    # 形状的正确性由写入方保证（后续任务的 agent 运行时），
    # 契约 §2.6 的代码块是它的规范。
    detail: dict[str, Any] | None
    at: IsoDateTime


class RecentEventsOut(CamelModel):
    """`GET /scans/{id}/events/recent?since=&limit=` 的响应（契约 §4.4）。

    前端在建立 SSE 之前先拉一次作为 seed（对应 mock 的 `seedCount`）。
    真实实现必须是「先拉历史、再订阅」—— 反过来的话，
    订阅建立之前产生的事件会永久丢失，而用户看到的是活动流里
    少了一开始的几条。
    """

    events: list[ActivityEventOut]
    hasMore: bool
