"""失败分类：这句话该不该自动重试（v1.22）。

## 为什么单独一个模块

`engine._run_guarded`（请求内）、`resumer.resume_one`（后台接手）、
`router.resume_turn`（人工点重试）三处都要回答同一个问题：
**这次失败，再跑一遍有意义吗？**

三处各写一份判据，迟早会漂；而漂的后果是静默的 ——
要么白烧三轮模型调用（每次 90 秒心跳窗口，用户看着同一条错误反复出现），
要么把一次"密钥过期"判成"可恢复"，后台永远在原地重试。

所以判据只放这里，只依赖字符串，不 import 任何本项目模块（避免循环）。

## 两类错误

- `PROVIDER_AUTH`：接入点拒绝了凭证（401/403、Invalid token）。**重试一万次
  结果一样**，只有人去改配置才会好。必须给用户一句能照着做的话。
- `UNKNOWN`：其余一切。默认按可重试处理 —— 因为"多试一次"的代价是几秒，
  而"该重试却没重试"的代价是任务停在半路。
"""

from __future__ import annotations

import re

#: 接入点凭证类失败。大小写不敏感地出现在异常文本里。
#: 命中的都是实测见过的形态：OpenAI 兼容网关回 `Invalid token` / 
#: `invalid_api_key` / `Incorrect API key`，KernelError 包一层后带 `HTTP 401`。
_AUTH_MARKERS: tuple[str, ...] = (
    "invalid token",
    "invalid_api_key",
    "incorrect api key",
    "no api key",
    "unauthorized",
    "http 401",
    "http 403",
    "authentication",
)

PROVIDER_AUTH = "provider_auth"
UNKNOWN = "unknown"

#: 原文里可能带请求 id、URL、甚至 key 片段。落库/展示前统一截断。
_MAX_DETAIL = 300


def describe(exc: BaseException) -> str:
    """把异常压成一行可展示的短文本。

    ## 为什么取首行

    `KernelError` 的消息常常是 `LLM 拒绝了请求（HTTP 401）：{...}` 后面
    跟着多行 JSON/HTML。整段塞进界面会把错误条撑成一堵墙，而第一行已经
    包含状态码与原因。
    """
    text = f"{type(exc).__name__}: {exc}"
    first = text.strip().splitlines()[0] if text.strip() else type(exc).__name__
    first = re.sub(r"\s+", " ", first).strip()
    return first[:_MAX_DETAIL]


def classify(detail: str) -> str:
    lowered = detail.lower()
    for marker in _AUTH_MARKERS:
        if marker in lowered:
            return PROVIDER_AUTH
    return UNKNOWN


def is_retryable(detail: str) -> bool:
    """自动接手该不该再试。凭证错明确不重试。"""
    return classify(detail) != PROVIDER_AUTH
