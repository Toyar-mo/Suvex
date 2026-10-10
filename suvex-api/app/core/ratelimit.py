"""限流：进程内滑动窗口。

**为什么是进程内而不是 Redis**：契约的部署形态是「本地/单机 Compose」，
一个 uvicorn 进程。为限流引一个 Redis 会让 compose 从两个服务变成三个，
而换来的能力（多实例共享计数）在这里用不上。
代价要如实记：**多 worker 或将来横向扩展时，限流会按进程各算一份**。
到那时换成 Redis 或网关层限流，调用点是这一个 `Limiter.check()`。

**为什么必须限流**：契约 §3.1 —— 口令是本系统唯一的凭据，
不限流等于允许在线爆破。这里的窗口刻意用「滑动」而不是「固定分钟」：
固定窗口在边界上允许 2 倍突发（59 秒打 5 次、61 秒再打 5 次），
而滑动窗口没有这个洞。
"""

from __future__ import annotations

import threading
import time
from collections import deque

from app.core.errors import ApiError


class Limiter:
    """多条规则同时生效（例如 5 次/分钟 **且** 15 次/小时）。

    规则是**与**关系：任何一条超了就拒。想「或」的话应该建两个 Limiter ——
    「5 次/分钟或 15 次/小时」这种语义本身就不成立（分钟级必然先触发）。
    """

    def __init__(self, rules: list[tuple[int, int]], *, name: str = "limiter") -> None:
        if not rules:
            raise ValueError("至少需要一条限流规则")
        self._rules = sorted(rules, key=lambda r: r[1])  # 窗口短的先判，Retry-After 更准
        self._name = name
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}
        self._longest_window = max(w for _, w in self._rules)

    def check(self, key: str) -> None:
        """记一次并判断。超限抛 429；否则静默返回。"""
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            self._prune(bucket, now)
            for limit, window in self._rules:
                recent = [t for t in bucket if t > now - window]
                if len(recent) >= limit:
                    # Retry-After 取「最早那次还需多久滑出去」，向上取整且至少 1 秒
                    # （给 0 会让客户端立刻重试，等于没限流）。
                    oldest = min(recent)
                    retry_after = max(1, int(window - (now - oldest)) + 1)
                    raise ApiError.rate_limited(retry_after)
            bucket.append(now)

    def _prune(self, bucket: deque[float], now: float) -> None:
        """丢掉已经滑出最长窗口的时间戳。

        不 prune 的话字典会随来源 IP 数无限增长 —— 慢速内存泄漏，
        而表现形式是「跑了两周之后进程 OOM」，排查时完全想不到限流。
        """
        cutoff = now - self._longest_window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def reset(self, key: str | None = None) -> None:
        """测试用。也给「登录成功后清空计数」留口子。"""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    def remaining(self, key: str) -> int:
        """当前窗口还剩几次。给测试断言用。"""
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.get(key)
            if not bucket:
                return self._rules[0][0] if self._rules else 0
            self._prune(bucket, now)
            limit, window = self._rules[0]
            return max(0, limit - len([t for t in bucket if t > now - window]))
