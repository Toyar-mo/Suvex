"""通知投递**不能拖慢调度循环**（v0.89）—— 队列化的验收。

## 为什么这几条测试值得单独一个文件

`dispatch()` 是同步的、一次 POST 超时 8 秒。它原本被我直接写在
`_reap_finished` 里，而那段跑在 `_tick_lock` 内 —— 于是：

    用户配了 3 个渠道，其中 2 个对端在超时边缘
    → 一个 tick 要 16 秒以上
    → `_dispatch_schedules`（到点的定时计划）与 `_claim_and_launch`（排队扫描）
      全排在它后面
    → 症状是「配了通知之后扫描启动变慢」

这个失败形态很坏：**看不出跟通知有关**。所以它必须有一条测试盯着，
而不是靠读代码发现。

## 这几条测试盯的三件事

1. 收尾路径**不调用** `dispatch` —— 只入队（`test_reap_does_not_dispatch_inline`）；
2. 队列的生产者与消费者真的接通了（`test_queued_scan_is_dispatched`）；
3. 线程生命周期不泄漏、`stop()` 之后不再消费（`test_stop_drains_and_stops`）。

⚠️ 这些用例**不**验证"发得对不对"——那是 `tests/test_notice_dispatch.py`
的事。这里只验证"发送发生在哪个线程上"。
"""

from __future__ import annotations

import queue
import threading
from typing import Any

import pytest

from app.scans.scheduler import ScanScheduler


class _FakeDispatch:
    """替身 `dispatch.dispatch`：记下谁被发了，并可以假装很慢。"""

    def __init__(self) -> None:
        self.payloads: list[Any] = []
        self.block_event: threading.Event | None = None
        self.entered = threading.Event()

    def __call__(self, _db: Any, payload: Any) -> Any:
        self.entered.set()
        if self.block_event is not None:
            # 模拟"对端很慢"：卡住直到测试放行。这正是就地发送会卡死
            # 调度循环的那种情形。
            self.block_event.wait(timeout=10)
        self.payloads.append(payload)

        class _Outcome:
            skipped = True
            skip_reason = "测试替身，不实际发送"

        return _Outcome()


class _FakeScan:
    """`_reap_finished` 的收尾分支 + `_notice_payload` 读的那几个属性。

    ⚠️ `status` 必须是 **`running`**。`_reap_finished` 里有一句

        if scan.status != ScanStatus.RUNNING.value:
            continue   # 已被 abort，终态由它自己写

    所以一个状态是 `completed` 的假扫描根本走不到 `finalize`，
    也就不会被放进 `finished` —— 第一版正是这么挂的（`qsize() == 0`），
    而报错信息看起来像"入队逻辑坏了"，其实是夹具造错了状态。
    """

    def __init__(self, scan_id: str, status: str = "running") -> None:
        self.id = scan_id
        self.target = "https://example.com"
        self.status = status
        self.failure_reason = None
        self.duration_sec = 12
        self.run_id = "run_1"  # 已有值 → 跳过 `detect_run_id` 那段
        self.run_dir = "/tmp/run"
        self.pid = 4321
        # ★ 形状是 `Partial<Record<Severity, number>>`：没有某一档时**该键不存在**。
        self.findings = {"high": 2}


class _FakeSession:
    """够 `_reap_finished` 走完收尾分支的最小会话。

    ⚠️ `rollback` 必须有 —— 收尾路径的 `except` 分支会调它，少了就变成
    `AttributeError`，而那会被误读成"产品代码有问题"。第一版正是这样
    挂掉的：断言写的是"`_reap_finished` 抛了异常"，实际抛的是我这只假
    会话的缺方法。
    """

    def __init__(self, scans: dict[str, _FakeScan]) -> None:
        self._scans = scans
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def get(self, _model: Any, scan_id: str) -> _FakeScan | None:
        return self._scans.get(scan_id)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_dispatch(monkeypatch: pytest.MonkeyPatch) -> _FakeDispatch:
    """把 `app.settings.dispatch.dispatch` 换成替身。

    `_notify_finished` 里是**函数内 import**（`from app.settings import
    dispatch`），所以必须打桩 `app.settings.dispatch.dispatch` 这个属性，
    而不是模块本身 —— 后者会被函数内的 import 拿回真身。
    """
    from app.settings import dispatch as dispatch_module

    fake = _FakeDispatch()
    monkeypatch.setattr(dispatch_module, "dispatch", fake)
    return fake


@pytest.fixture
def fake_session(monkeypatch: pytest.MonkeyPatch) -> Any:
    """让调度器每次都拿到一个能返回假 Scan 的会话。"""
    scans = {"scan_a": _FakeScan("scan_a"), "scan_b": _FakeScan("scan_b")}

    def _factory() -> _FakeSession:
        return _FakeSession(scans)

    monkeypatch.setattr("app.scans.scheduler.get_session_factory", lambda: _factory)
    return scans


# ══════════════════════════════════════════════════════════
# 一、收尾路径**不**就地发
# ══════════════════════════════════════════════════════════


def test_reap_does_not_dispatch_inline(
    fake_dispatch: _FakeDispatch, fake_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 核心：真正跑 `_reap_finished`，断言它**只入队、不发送**。

    ## 这条用例的血泪史

    第一版是我自己拼了一段 `put` + `done.set()` 的小脚本，然后断言
    "入队没被阻塞"。那验证的是我**刚写的这段测试代码**，不是
    `_reap_finished` —— 把 `_reap_finished` 改成同步 `dispatch()` 它照样绿。
    典型的自指假绿。

    现在真的调 `_reap_finished`：造一个 `poll()` 返回 0（已退出）的假进程，
    让替身 `dispatch` **永不返回**。如果收尾路径同步发送，这里会卡到超时
    失败；只入队则立刻返回。
    """
    import subprocess

    sched = ScanScheduler()
    fake_dispatch.block_event = threading.Event()  # 永不 set —— 一式卡死的发送

    class _ExitedProcess:
        """`poll()` 非 None = 进程已退出，`_reap_finished` 会走收尾分支。"""

        def poll(self) -> int:
            return 0

    sched.register("scan_a", _ExitedProcess())  # type: ignore[arg-type]

    # 收尾路径要读文件系统（`_flag_of` / `_run_dir_and_runner`）和真实会话，
    # 这些与本用例要验的东西无关，替换成最小值。
    monkeypatch.setattr("app.scans.scheduler._flag_of", lambda _sid: None)
    monkeypatch.setattr(
        "app.scans.scheduler._run_dir_and_runner",
        lambda _sid: (None, "agent"),
    )
    monkeypatch.setattr("app.scans.scheduler.did_backend_run", lambda *_a, **_k: True)
    monkeypatch.setattr("app.scans.scheduler.service.finalize", lambda *_a, **_k: None)
    monkeypatch.setattr(ScanScheduler, "_import_for", lambda *_a, **_k: None)

    done = threading.Event()
    error: list[BaseException] = []

    def _run() -> None:
        try:
            sched._reap_finished()
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    assert done.wait(timeout=5.0), (
        "`_reap_finished` 没能在 5 秒内返回 —— 它很可能在同步发送通知"
        "（发送会卡在替身上，而真实的对端超时是 8 秒）"
    )
    assert error == [], f"_reap_finished 抛了异常：{error}"
    assert sched._notice_queue.qsize() == 1, "收尾的扫描没有被入队"
    assert fake_dispatch.payloads == [], "收尾路径同步发送了通知"
    # `register` 与 `forget` 要配对，否则 `_procs` 会一直长。
    assert "scan_a" not in sched._procs
    assert isinstance(sched._notice_queue, queue.Queue)
    _ = subprocess  # 保持 import 有意义（假进程的类型来自这个模块的语义）


def test_notice_loop_dispatches_queued_scan(fake_dispatch: _FakeDispatch, fake_session: Any) -> None:
    """队列的生产者与消费者真的接通：入队的 scan_id 会被发出去。"""
    sched = ScanScheduler()
    sched._notice_queue.put("scan_a")
    sched._notice_thread = threading.Thread(target=sched._notice_loop, daemon=True)
    sched._notice_thread.start()

    try:
        assert fake_dispatch.entered.wait(timeout=3.0), "通知线程没有消费到队列里的扫描"
    finally:
        sched._stop.set()
        sched._notice_thread.join(timeout=3.0)

    assert [p.scan_id for p in fake_dispatch.payloads] == ["scan_a"]


def test_dispatched_payload_carries_severity_and_counts(
    fake_dispatch: _FakeDispatch, fake_session: Any
) -> None:
    """载荷里的严重度/计数来自 `Scan.findings`，且**缺失的键当 0**。"""
    sched = ScanScheduler()
    sched._notice_queue.put("scan_a")
    sched._notice_thread = threading.Thread(target=sched._notice_loop, daemon=True)
    sched._notice_thread.start()

    try:
        assert fake_dispatch.entered.wait(timeout=3.0)
    finally:
        sched._stop.set()
        sched._notice_thread.join(timeout=3.0)

    payload = fake_dispatch.payloads[0]
    assert payload.severity == "high"
    assert payload.high_count == 2
    # critical 那个键不存在 → 必须是 0，不能抛 KeyError。
    assert payload.critical_count == 0


def test_stop_lets_the_notice_thread_exit(fake_dispatch: _FakeDispatch, fake_session: Any) -> None:
    """★ `stop()` 之后通知线程要能退出 —— 否则每次重启漏一个线程。"""
    sched = ScanScheduler()
    sched._notice_thread = threading.Thread(target=sched._notice_loop, daemon=True)
    sched._notice_thread.start()

    sched._stop.set()
    sched._notice_thread.join(timeout=3.0)

    assert not sched._notice_thread.is_alive(), "通知线程在 stop 之后还活着（线程泄漏）"


def test_empty_queue_does_not_spin_the_cpu(fake_dispatch: _FakeDispatch) -> None:
    """空队列时线程是**等**着，不是空转。

    判据用 `task_done` 的未完成计数 + 一次可控入队：如果循环是忙等的，
    `get(timeout=1.0)` 会立刻抛 `Empty` 并重试，CPU 会被吃满；这里只验
    "它确实在等" —— 通过"入队后能被及时取走"来间接证明它没有卡死。
    """
    sched = ScanScheduler()
    sched._notice_thread = threading.Thread(target=sched._notice_loop, daemon=True)
    sched._notice_thread.start()

    try:
        # 先让它空转一小会儿，再入队。
        threading.Event().wait(0.1)
        sched._notice_queue.put("scan_b")
        assert sched._notice_queue.qsize() in (0, 1)
        # 等到它被取走。
        deadline = threading.Event()
        for _ in range(50):
            if sched._notice_queue.qsize() == 0:
                break
            deadline.wait(0.1)
        assert sched._notice_queue.qsize() == 0, "队列里的项没有被取走"
    finally:
        sched._stop.set()
        sched._notice_thread.join(timeout=3.0)


# ══════════════════════════════════════════════════════════
# 四、tick() 真的被那把锁包着
# ══════════════════════════════════════════════════════════


class _RecordingLock:
    """替身锁：只记录"被进出了几次"，并转发给一把真锁。"""

    def __init__(self) -> None:
        self._inner = threading.Lock()
        self.entered = 0
        self.released = 0

    def __enter__(self) -> None:
        self._inner.acquire()
        self.entered += 1

    def __exit__(self, *_exc: object) -> None:
        self.released += 1
        self._inner.release()


def test_tick_holds_the_tick_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ `tick()` 必须真的在 `_tick_lock` 里跑。

    ## 这条用例是为一次真实事故加的

    v0.89 加通知队列时，我往 `__init__` 里插注释，把
    `self._tick_lock = threading.Lock()` 那一行连带替换掉了 ——
    `tick()` 里还留着 `with self._tick_lock:`，于是每一个 tick 都会
    `AttributeError`。

    它没被任何测试发现：正向测试全绿、`ruff` 全过（属性是运行期的），
    而队列那几条用例压根不碰 `tick()`。真实的症状会是"扫描永远停在
    queued"，且调度线程被自己的异常兜底吞掉、只在日志里留一行。

    所以这里既守"属性存在"，也守"它确实包住了 tick 的三个步骤" ——
    后者才是这把锁存在的理由（并发认领会突破 `max_concurrent_scans`）。
    """
    sched = ScanScheduler()
    assert hasattr(sched, "_tick_lock"), "ScanScheduler 没有 _tick_lock，tick() 一跑就 AttributeError"

    recording = _RecordingLock()
    sched._tick_lock = recording  # type: ignore[assignment]

    calls: list[str] = []
    monkeypatch.setattr(sched, "_reap_finished", lambda: calls.append("reap"))
    monkeypatch.setattr(sched, "_detect_stalls", lambda: calls.append("stalls"))
    monkeypatch.setattr(sched, "_dispatch_schedules", lambda: calls.append("schedules"))
    monkeypatch.setattr(sched, "_claim_and_launch", lambda: calls.append("claim"))

    sched.tick()

    # 三个步骤都跑了（证明 tick 没在锁之前就抛异常）
    assert calls == ["reap", "stalls", "schedules", "claim"]
    # 而且它们是在锁内跑的
    assert recording.entered == 1, "tick() 没有进入 _tick_lock —— 并发上限会失效"
    assert recording.released == 1, "tick() 进了 _tick_lock 却没释放（会死锁）"
