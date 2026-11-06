"""每题一个共享 findings 总线（学 ctf-agent `message_bus.py`）。

append-only findings + per-model cursor。使得一个模型卡住时，能拿到别的
模型已经发现了什么（作为 bump 时喂进去的「兄弟洞察」），避免多个模型
各自在同一个死胡同里穷尽。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

#: 保留的 findings 上限——防止一场长赛把内存撑爆。超了削最旧的并同步挪
#: cursor，避免 cursor 指向被削掉的项。
MAX_FINDINGS = 200


@dataclass
class Finding:
    model: str
    content: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class ChallengeMessageBus:
    """append-only 共享 findings + per-model cursor。

    用法：
      * `post(model, content)` —— 某模型报告一条进展（通常是被平台确认前的
        发现摘要，或「试过 X 不行」这类负结果）。
      * `check(model)` —— 取「别的模型」自上次之后的新增，并推进 cursor。
      * `format_unread(findings)` —— 拼成给模型看的一段。
    """

    findings: list[Finding] = field(default_factory=list)
    cursors: dict[str, int] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def post(self, model: str, content: str) -> None:
        content = (content or "").strip()
        if not content:
            return
        async with self._lock:
            self.findings.append(Finding(model=model, content=content))
            overflow = len(self.findings) - MAX_FINDINGS
            if overflow > 0:
                self.findings = self.findings[overflow:]
                # cursor 是「已读到第几条」的绝对下标，削掉 overflow 条要同步减。
                self.cursors = {k: max(0, v - overflow) for k, v in self.cursors.items()}

    async def check(self, model: str) -> list[Finding]:
        """取其它模型的新 findings 并推进本模型 cursor。"""
        async with self._lock:
            cursor = self.cursors.get(model, 0)
            unread = [f for f in self.findings[cursor:] if f.model != model]
            self.cursors[model] = len(self.findings)
            return unread

    async def broadcast(self, content: str, source: str = "coordinator") -> None:
        await self.post(source, content)

    @staticmethod
    def format_unread(findings: list[Finding]) -> str:
        if not findings:
            return ""
        parts = [f"[{f.model}] {f.content}" for f in findings]
        return "**其它模型已经发现 / 试过：**\n\n" + "\n\n".join(parts)


__all__ = ["ChallengeMessageBus", "Finding", "MAX_FINDINGS"]
