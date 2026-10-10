"""CTF2 自动增量同步与自动开解（v0.24：抢首杀）。

## 它解决的问题：**发现得太晚**

比赛规则里有两条直接指向这个模块：

> 每道题目前三名成功解题的团队将额外获得本题分值的 15%、10% 和 5%
> 的奖励分
> 题量设计明显超过人工手工完成能力

"前三名"把得分从"解得多快"改成"**多早开始**"。而在 v0.23 之前，
一道题在平台上挂出来之后的处境是：

    平台上有了 → （系统里不存在）→ 用户想起来点同步 → 才入队

第一段箭头里的人工延迟是纯粹被让掉的分，而且它与解题能力无关：
对手的 Agent 如果每分钟同步一次，他在这道题上已经领先一个身位。

## ★ 为什么是**独立线程**，不并进调度器的循环

调度器 `_loop` 的 tick 是"回收 + 认领"（毫秒级），而同步是网络操作
（增量几秒，首次全量可达 45 秒）。把同步塞进 tick 循环里的后果是：

    一次 45 秒的全量同步 → 调度线程 45 秒不认领 → 解题队列停摆

那是**比赛现场最不该出现**的故障形态（看起来在跑，其实一道题都没动）。
所以这里是独立线程，调度器完全不知道它的存在。

## 三条纪律

1. **绝不抛异常出去**。这是后台线程：一次未捕获的异常会让它静默死掉，
   而症状是"新题再也不会自动出现"——用户不会怀疑一个他从没见过的线程。
2. **绝不与手动同步并跑**。靠 `service.try_sync` 的非阻塞抢锁实现，
   拿不到就**跳过这一轮**（而不是排队）——排队会让"用户手点一次"之后
   积压一串重复同步，那正好是最浪费请求预算的行为。
3. **只碰新题**。给新题发优先级、开解，都不动任何既有题目的状态 ——
   详见 `service.apply_new_challenge_priority` 里那段"绝不重排全表"。
"""

from __future__ import annotations

import logging
import threading
import time

from app.core.db import get_session_factory
from app.core.vocab import (
    DEFAULT_CTF2_AUTO_SYNC_SECONDS,
    MAX_CTF2_AUTO_SYNC_SECONDS,
    MIN_CTF2_AUTO_SYNC_SECONDS,
)

logger = logging.getLogger("suvex.ctf2")

# 自动同步**关闭时**的回看间隔（秒）。
#
# ⚠️ 关了不能 `wait(0)`：那会让循环变成忙等，一个"关掉的功能"反而
#    把 CPU 吃满。也不能睡很久（比如一小时），否则用户在设置页打开它
#    之后要等很久才生效 —— 而"我明明开了"是最容易被当成 bug 的现象。
_DISABLED_RECHECK_SECONDS = 30.0


class Ctf2AutoSync:
    """一个后台线程：定时增量同步 → 给新题发优先级 →（可选）自动开解。"""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 防止"定时触发"与"手动 run_once"重叠。串行循环本身不会重叠，
        # 但 `run_once` 是公开的（测试与将来的接口都会直接调它）。
        self._lock = threading.Lock()
        self._last_run_at: float | None = None
        self._last_new = 0
        self._last_error: str | None = None

    # ══════════════════════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════════════════════

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="suvex-ctf2-autosync", daemon=True
        )
        self._thread.start()
        logger.info(
            "CTF2 自动同步已启动（间隔 %ds，可在设置页改或填 0 关闭）",
            DEFAULT_CTF2_AUTO_SYNC_SECONDS,
        )

    def stop(self, *, timeout: float = 5.0) -> None:
        """停线程。**不等同步跑完** —— 它最多几十秒，而停机不该被它拖住。"""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))

    # ══════════════════════════════════════════════════════
    # 循环
    # ══════════════════════════════════════════════════════

    def _loop(self) -> None:
        while True:
            interval = self._interval()
            # 关闭时用一个较短的固定间隔回看（见 `_DISABLED_RECHECK_SECONDS`）。
            wait = interval if interval > 0 else _DISABLED_RECHECK_SECONDS
            if self._stop.wait(wait):
                return
            if interval <= 0:
                # 关着：什么都不做，只等下一次判定。
                continue
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - 线程绝不能死（纪律 1）
                logger.exception("CTF2 自动同步这一轮异常（下一轮会重试）")
                # ★ 把原因记下来（2026-10-10 补）。
                #
                # 这个字段是给 `stats()` 用的，而 `stats()` 的 docstring 写着
                # 它是「比赛规则里"查看运行状态"的那一部分」。原来全文件
                # **没有任何一处给它赋过错误值**（唯一一次赋值是置回 None），
                # 于是 `lastError` 恒为 `null` —— 自动同步连续失败十轮，
                # 界面上仍然显示一切正常。
                #
                # 与 `_interval` 那条"读库失败退回默认值而不是 0"是同一条纪律：
                # **状态要如实反映现实**，哪怕现实不好看。
                self._last_error = f"{type(exc).__name__}: {exc}"

    def _interval(self) -> int:
        """读一次自动同步间隔（秒）。0 = 关闭。

        ★ 每轮都读，理由与 `scheduler._interval` 一样：用户改了设置之后
        期望"马上就变"，而缓存一个旧值会让生效时刻取决于旧值有多大。

        ⚠️ 读库失败退回默认值而**不是** 0：退回 0 意味着"数据库抖一下，
        自动同步就永久静默"，而用户完全不会知道。退回默认值（30 秒）
        最多是打多几次请求，那是更安全的一侧。
        """
        try:
            from app.ctf2 import service as ctf2_service

            factory = get_session_factory()
            with factory() as session:
                value = int(ctf2_service.effective_ctf2_settings(session).auto_sync_seconds)
        except Exception:  # noqa: BLE001 - 见上
            logger.warning("CTF2 读取自动同步间隔失败，本轮用默认值", exc_info=True)
            return DEFAULT_CTF2_AUTO_SYNC_SECONDS
        return min(max(value, MIN_CTF2_AUTO_SYNC_SECONDS), MAX_CTF2_AUTO_SYNC_SECONDS)

    # ══════════════════════════════════════════════════════
    # 一轮
    # ══════════════════════════════════════════════════════

    def stats(self) -> dict[str, object]:
        """给接口/日志看的运行状态（比赛规则里"查看运行状态"的那一部分）。"""
        return {
            "lastRunAt": self._last_run_at,
            "lastNewChallenges": self._last_new,
            "lastError": self._last_error,
            "running": self._thread is not None and self._thread.is_alive(),
        }

    def run_once(self) -> int:
        """跑一轮，返回**发现的新题数**。重叠时直接返回 0。"""
        if not self._lock.acquire(blocking=False):
            logger.debug("CTF2 自动同步：上一轮还在跑，跳过这一轮")
            return 0
        try:
            return self._run_locked()
        finally:
            self._lock.release()

    def _run_locked(self) -> int:
        from app.ctf2 import service as ctf2_service

        factory = get_session_factory()

        # ── 先读一次配置（同步之前读，而不是之后）──
        #
        # 顺序是有意的：如果同步跑了 40 秒而用户在中间把"新题自动开解"
        # 关掉了，那么**这次**仍然按他按下按钮那一刻的意图走。反过来
        # （同步后再读）会让"我刚关掉它却还是自动开解了"成为可能。
        with factory() as session:
            if ctf2_service.get_account(session) is None:
                # 没绑账号是**正常状态**（用户可能先启动服务再绑 PAT），
                # 不是错误 —— 不要往 `last_error` 里写东西吓人。
                return 0
            settings = ctf2_service.effective_ctf2_settings(session)
            auto_priority = bool(settings.auto_priority_new)
            auto_start = bool(settings.auto_start_new)

        with factory() as session:
            # ★ 非阻塞抢同步锁：手动同步正在跑时**跳过这一轮**。
            #   排队会让"用户手点一次"之后积压一串重复同步，那正好是
            #   最浪费请求预算的行为（见模块 docstring 的纪律 2）。
            result = ctf2_service.try_sync(session)
        if result is None:
            logger.debug("CTF2 自动同步：手动同步正在进行，跳过这一轮")
            return 0

        self._last_run_at = time.time()
        self._last_error = None
        new_ids = list(result.new_challenge_ids)
        self._last_new = len(new_ids)
        if not new_ids:
            return 0

        logger.info(
            "CTF2 自动同步发现 %d 道新题（%s）",
            len(new_ids),
            result.summary(),
        )

        if auto_priority:
            with factory() as session:
                ctf2_service.apply_new_challenge_priority(session, new_ids)

        if auto_start:
            self._auto_start(new_ids)

        return len(new_ids)

    def _auto_start(self, challenge_ids: list[str]) -> None:
        """把新题推进解题队列。**逐题独立容错**。

        ⚠️ 一道题失败（没有可用的 LLM provider、题目当前不可解、平台不
        支持提交）绝不能影响其余几道 —— "批量操作做到一半停住"是这类
        代码最常见的失败形态，而且它失败得**很安静**：日志里只有一行
        异常，界面上什么都没发生。
        """
        from app.ctf2 import service as ctf2_service

        factory = get_session_factory()
        started = 0
        for challenge_id in challenge_ids:
            try:
                with factory() as session:
                    challenge = ctf2_service.get_challenge_or_404(session, challenge_id)
                    ctf2_service.start_solve(session, challenge)
                started += 1
            except Exception as exc:  # noqa: BLE001 - 见上
                logger.info("CTF2 新题自动开解跳过 %s：%s", challenge_id, exc)
        if started:
            logger.info("CTF2 新题自动开解：%d 道进入队列", started)


# ── 单例（与 `scheduler.get_scheduler` 同一个模式）──────────

_autosync: Ctf2AutoSync | None = None
_singleton_lock = threading.Lock()


def get_autosync() -> Ctf2AutoSync:
    global _autosync
    with _singleton_lock:
        if _autosync is None:
            _autosync = Ctf2AutoSync()
        return _autosync


def reset_autosync() -> None:
    """测试用：丢掉单例（**不**负责停线程，调用方自己 stop）。"""
    global _autosync
    with _singleton_lock:
        _autosync = None


__all__ = ["Ctf2AutoSync", "get_autosync", "reset_autosync"]
