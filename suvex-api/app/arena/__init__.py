"""`app.arena` —— 对接**赛事方下发的 AI Agent API**（`api_doc.md`）。

## 为什么不复用 `app/ctf2/`

因为契约不同，而且是**结构性的不同**：

| | `app/ctf2/`（ctf2.dasdctf.com） | `app/arena/`（赛事方 Agent API） |
| --- | --- | --- |
| 认证 | `Authorization: Bearer <PAT>` | `X-Agent-AccessKey` |
| 信封 | `{data, success}` | **`{code, message, data}`**，失败也是 HTTP 200 |
| 题目 id | UUID 字符串 | **整数** `exerciseId` |
| 题目列表 | 开放 API **没有**这个接口 | `GET /ctf/exercise-list` |
| 环境就绪 | 本地轮询平台状态 | `isNeedCheck=false` + `endpoints` 非空 |
| 靶机 | `access_urls`（HTTP 地址） | `endpoints[]`（IP/端口/账号/代理映射） |

硬把两套塞进一个 client 的结果是**每个方法里都有一半的 if 在判断"这是哪个平台"**，
而那正是最难维护的形状。所以两个 client 分开。

## 但解题那一侧要复用

"在沙箱里让模型解题、产出 flag、提交"这一段与平台无关 ——
沙箱容器、工具循环、flag 提取都在 `app/ctf2/runner.py` 里，已经过验证。
`arena` 只负责"取题 / 起环境 / 提交"这三件平台相关的事。

## 契约里最容易被误读的三点

1. **失败也是 HTTP 200** —— 只看状态码会把每次失败都当成功；
2. **"答案错误"是业务结果，不是异常** —— 它要和"凭据失效""服务端故障"分开，
   因为处理方式完全不同（换候选 / 重新配 key / 重试）；
3. **环境是异步的** —— `build-exercise-env` 只是受理，必须轮询详情。

这三点都写在 `client.py` 的 docstring 与实现里。
"""

from app.arena.client import (
    ArenaAuthError,
    ArenaClient,
    ArenaEnvironmentTimeout,
    ArenaError,
    ArenaNotFound,
    ArenaUnavailable,
    Endpoint,
    Exercise,
    SubmitResult,
)

__all__ = [
    "ArenaAuthError",
    "ArenaClient",
    "ArenaEnvironmentTimeout",
    "ArenaError",
    "ArenaNotFound",
    "ArenaUnavailable",
    "Endpoint",
    "Exercise",
    "SubmitResult",
]
