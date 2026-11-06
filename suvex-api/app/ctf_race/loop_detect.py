"""工具调用签名去重：检测原地打转（学 ctf-agent `loop_detect.py`）。

## 与 Suvex 既有「重复动作闸」的关系

Suvex 的 `nudge` 已在**轮次层**判重（同一命令连跑 N 次就判定打转、收尾）。
本模块是**工具层**的更细补充：它不终止解题，而是在「同一签名出现 ≥3 次」
时返回 `warn`（调用方可把换思路提示塞进工具结果里）、「≥5 次」时返回
`break`（调用方可直接拦掉这次调用、返回警告而不执行）。早一步止血，
比等到轮次收尾才判更省。

两者不冲突：轮次级管「整轮跑死了」，工具级管「这一步在鬼打墙」。
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field


@dataclass
class LoopDetector:
    """跟踪最近的工具调用签名，检测重复。

    `check(tool_name, args)` 返回值：
      * ``None`` —— 没有形成循环
      * ``"warn"`` —— 到达 warn 阈值（默认重复 3 次），提醒换思路
      * ``"break"`` —— 到达 break 阈值（默认重复 5 次），应强制拦下
    """

    window: int = 12
    warn_threshold: int = 3
    break_threshold: int = 5
    _recent: deque = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.break_threshold <= self.warn_threshold:
            raise ValueError("break_threshold 必须大于 warn_threshold")
        self._recent = deque(maxlen=self.window)

    def check(self, tool_name: str, args: dict | str | None = None) -> str | None:
        if args:
            raw = json.dumps(args, sort_keys=True, ensure_ascii=False) if isinstance(args, dict) else str(args)
            sig = f"{tool_name}:{raw[:500]}"
        else:
            sig = tool_name
        self._recent.append(sig)
        count = sum(1 for s in self._recent if s == sig)
        if count >= self.break_threshold:
            return "break"
        if count >= self.warn_threshold:
            return "warn"
        return None

    @property
    def last_sig(self) -> str:
        return self._recent[-1] if self._recent else ""

    def reset(self) -> None:
        self._recent.clear()


#: 塞进工具结果 / bump hint 的换思路提示（中文，含具体建议）。
LOOP_WARNING_MESSAGE = (
    "⚠️ **你似乎陷入了循环** —— 你连续用几乎完全相同的调用、拿到相同的结果。"
    "请**停止重复**这一步，退一步换个方向：\n"
    "- 如果你在 grep/搜索，改用 Python 脚本做更系统的提取；\n"
    "- 如果你在反复分析文件的同一处，换另一处或换一种工具；\n"
    "- 还有哪些角度你没试过？先列出 3 个不同思路再动手。"
)


__all__ = ["LOOP_WARNING_MESSAGE", "LoopDetector"]
