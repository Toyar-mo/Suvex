"""对话沙箱（v0.107）：桥接层的行为，全部**不碰真 docker**。

## 为什么这些测试一个容器都不起

这个模块的价值全在**出错路径**上，而起真容器既慢（1~3 秒）又依赖宿主
（CI 没有 docker 就全红）。这里用 `FakeSandbox` 替掉
`runner.SandboxContainer`，直接控制 `start()` 成功/失败、`shell()`
返回什么，把三条真正的风险各自钉住：

1. **冻住事件循环** —— `run_shell` 内部是阻塞 subprocess。测试断言它
   **不阻塞**事件循环（起一个"心跳"协程，看它在 `run_shell` 期间是否
   还在跑）。
2. **攒孤儿容器** —— `start()` 失败时半成品容器必须被销毁。
3. **每会话隔离 + 跨轮复用** —— 两个会话不共享容器，同一会话两轮共用一个。

## ⚠️ 为什么用 `monkeypatch` 换 `_new_sandbox` 而不是换 `SandboxContainer`

`_new_sandbox` 是**唯一**构造容器的地方（这是刻意的：一处构造点 =
一处可以替换的接缝）。换 `runner.SandboxContainer` 需要连 import
一起骗，换 `_new_sandbox` 只换一个函数。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.chat import sandbox as chat_sandbox


class _Result:
    """`runner.CommandResult` 的最小替身：只要 `exit_code` 与 `combined()`。

    ## 为什么单独一个类，而不是在 `shell()` 里内联一个

    内联类体里读 `self.shell_stdout` 是**读不到的** —— 类体作用域不闭包
    外层的 `self`，赋值 `exit_code = self.shell_exit` 会变成"读一个还没
    定义的同名局部"，实测报 `NameError`。分开定义最省事。
    """

    def __init__(self, *, stdout: str, exit_code: int) -> None:
        self.exit_code = exit_code
        self._stdout = stdout

    def combined(self) -> str:
        return self._stdout


class FakeSandbox:
    """够用的假容器：记下调用、能装死不 return。"""

    def __init__(self, *, name: str = "fake", start_error: Exception | None = None) -> None:
        self.name = name
        self.started = False
        self.destroyed = False
        self.destroy_count = 0
        self.shell_calls: list[str] = []
        #: `start()` 抛这个异常（模拟 docker 不可用 / 容器秒退）。
        self.start_error = start_error
        #: `shell()` 每次要睡多久（模拟慢命令）。
        self.shell_sleep = 0.0
        #: `shell()` 返回什么。
        self.shell_stdout = ""
        self.shell_exit = 0

    def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    def destroy(self) -> None:
        self.destroyed = True
        self.destroy_count += 1

    def shell(self, command: str, *, timeout: float = 300) -> Any:
        self.shell_calls.append(command)
        if self.shell_sleep:
            # ⚠️ 故意用阻塞 sleep：真实的 `subprocess.run` 就是这样阻塞的。
            #    用 `asyncio.sleep` 会让本测试**测不出**事件循环被冻住 ——
            #    那正是这里要钉住的缺陷形态。
            time.sleep(self.shell_sleep)

        return _Result(stdout=self.shell_stdout, exit_code=self.shell_exit)


class FakeSession:
    """只需要 `id`。`_ToolContext.session` 也只用到这一个字段。"""

    def __init__(self, session_id: str) -> None:
        self.id = session_id


class FakeCtx:
    def __init__(self, session_id: str) -> None:
        self.session = FakeSession(session_id)


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """每个用例前后清空注册表 —— 它是**进程级**全局状态，不清会互相污染。"""
    chat_sandbox.drop_all_sandboxes()
    yield
    chat_sandbox.drop_all_sandboxes()


def _install(monkeypatch: pytest.MonkeyPatch, sandbox: FakeSandbox) -> None:
    monkeypatch.setattr(chat_sandbox, "_new_sandbox", lambda session: sandbox)


# ══════════════════════════════════════════════════════════
# 注册表：惰性、复用、隔离、回收
# ══════════════════════════════════════════════════════════


def test_a_sandbox_is_not_created_until_someone_asks_for_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """纯问答轮次不该付起容器的代价（1~3 秒）。

    这是"惰性"的意义：`has_sandbox` 在没人要之前必须是 False。
    """
    assert chat_sandbox.has_sandbox("chat_1") is False

    fake = FakeSandbox()
    _install(monkeypatch, fake)
    assert fake.started is False, "还没人要沙箱，它就已经被 start 了"

    chat_sandbox.get_sandbox(FakeSession("chat_1"))
    assert fake.started is True
    assert chat_sandbox.has_sandbox("chat_1") is True


def test_the_same_session_reuses_one_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """跨轮保留：第二轮必须拿到**同一个**容器。

    每轮新建等于让模型每轮从头再来（上一轮解压出来的文件全没了）。
    """
    fakes: list[FakeSandbox] = []

    def _make(session: Any) -> FakeSandbox:
        f = FakeSandbox(name=f"sb-{len(fakes)}")
        fakes.append(f)
        return f

    monkeypatch.setattr(chat_sandbox, "_new_sandbox", _make)

    session = FakeSession("chat_1")
    first = chat_sandbox.get_sandbox(session)
    second = chat_sandbox.get_sandbox(session)

    assert first is second
    assert len(fakes) == 1, f"同一个会话建了 {len(fakes)} 个容器"


def test_two_sessions_get_two_different_sandboxes(monkeypatch: pytest.MonkeyPatch) -> None:
    """会话之间互不可见 —— 这是隔离的**全部**依赖。"""
    fakes: list[FakeSandbox] = []

    def _make(session: Any) -> FakeSandbox:
        f = FakeSandbox(name=f"sb-{len(fakes)}")
        fakes.append(f)
        return f

    monkeypatch.setattr(chat_sandbox, "_new_sandbox", _make)

    a = chat_sandbox.get_sandbox(FakeSession("chat_a"))
    b = chat_sandbox.get_sandbox(FakeSession("chat_b"))

    assert a is not b
    assert len(fakes) == 2


def test_dropping_a_sandbox_destroys_it_and_forgets_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """删会话必须回收容器 —— 否则攒在宿主上且**没有任何界面能看到**。"""
    fake = FakeSandbox()
    _install(monkeypatch, fake)
    chat_sandbox.get_sandbox(FakeSession("chat_1"))

    chat_sandbox.drop_sandbox("chat_1")

    assert fake.destroyed is True
    assert chat_sandbox.has_sandbox("chat_1") is False


def test_dropping_a_sandbox_twice_is_harmless(monkeypatch: pytest.MonkeyPatch) -> None:
    """幂等：DELETE 被重放、或测试清理撞上正常清理，都不该炸。"""
    fake = FakeSandbox()
    _install(monkeypatch, fake)
    chat_sandbox.get_sandbox(FakeSession("chat_1"))

    chat_sandbox.drop_sandbox("chat_1")
    chat_sandbox.drop_sandbox("chat_1")  # 第二次：什么都没有

    assert fake.destroy_count == 1, "第二次 drop 不该再销毁一次"


def test_dropping_an_unknown_session_is_harmless() -> None:
    chat_sandbox.drop_sandbox("chat_never_seen")  # 不抛就算过


# ══════════════════════════════════════════════════════════
# ★ 孤儿容器：start() 失败必须清掉半成品
# ══════════════════════════════════════════════════════════


def test_a_failed_start_destroys_the_half_built_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """`docker run` 成功不等于容器还活着（见 `SandboxContainer.start` 的注释）。

    所以 `start()` 抛异常时，容器**可能已经存在**。不销毁就会攒孤儿 ——
    而孤儿容器占内存、占名字，且没有界面能看到它们。
    """
    fake = FakeSandbox(start_error=RuntimeError("docker run 秒退"))
    _install(monkeypatch, fake)

    with pytest.raises(RuntimeError):
        chat_sandbox.get_sandbox(FakeSession("chat_1"))

    assert fake.destroyed is True, "start 失败后半成品容器没被销毁 —— 会攒孤儿"
    assert chat_sandbox.has_sandbox("chat_1") is False, "启动失败的沙箱不该留在注册表里"


def test_a_failed_start_leaves_the_registry_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    """一次失败不该让这个会话永久不可用（下次重试要能成功）。"""
    bad = FakeSandbox(start_error=RuntimeError("第一次失败"))
    _install(monkeypatch, bad)
    with pytest.raises(RuntimeError):
        chat_sandbox.get_sandbox(FakeSession("chat_1"))

    good = FakeSandbox()
    _install(monkeypatch, good)
    sandbox = chat_sandbox.get_sandbox(FakeSession("chat_1"))

    assert sandbox is good
    assert good.started is True


# ══════════════════════════════════════════════════════════
# ★★ 事件循环不被冻住（最容易写错、后果最大的一条）
# ══════════════════════════════════════════════════════════


async def test_run_shell_does_not_block_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """沙箱执行是阻塞 subprocess，必须走 `to_thread`。

    ## 这条测试在防什么

    `deploy/suvex-api.service` 的 `ExecStart` 是**单 worker**（没有
    `--workers`）。如果 `run_shell` 在事件循环里直接调 `sandbox.shell()`，
    那一次命令执行期间（最长 300 秒）**整个 API 都停摆** ——
    其他会话的 SSE keepalive、其他用户的请求、健康检查全卡住。

    ## 怎么测

    起一个"心跳"协程，每 10ms 记一次时间。然后在 `run_shell` 期间
    （假容器 sleep 200ms）看心跳还在不在跳。心跳停了 = 事件循环被冻住。

    ⚠️ 不能用"总耗时"判断：`to_thread` 不缩短耗时，它只是让出循环。
    """
    fake = FakeSandbox()
    # `_tool_run_shell` 会调 `sandbox.shell()`，后者阻塞 sleep。
    fake.shell_sleep = 0.2
    comment = "心跳"
    fake.shell_stdout = comment
    _install(monkeypatch, fake)

    beats: list[float] = []
    stop = asyncio.Event()

    async def _heartbeat() -> None:
        while not stop.is_set():
            beats.append(time.monotonic())
            await asyncio.sleep(0.01)

    heartbeat = asyncio.create_task(_heartbeat())
    try:
        # 让心跳先跑起来（否则它可能一次都没跳就到断言）。
        await asyncio.sleep(0.05)
        before = len(beats)

        ctx = FakeCtx("chat_1")
        out = await chat_sandbox.tool_run_shell(ctx, {"command": "echo hi"})
        during = len(beats) - before
    finally:
        stop.set()
        await heartbeat

    assert "心跳" in out, f"输出没透传回来：{out!r}"
    # 200ms 阻塞期间，10ms 心跳应该跳了 10+ 次。给宽余量（CI 慢），
    # 只要求"显然跳了好几次"—— 被冻住的话是 0 次。
    assert during >= 3, (
        f"`run_shell` 期间心跳只跳了 {during} 次 —— 事件循环被冻住了。"
        "单 worker 下这意味着整个 API 在这 200ms 里停摆。"
    )


async def test_browser_does_not_block_the_event_loop_either(monkeypatch: pytest.MonkeyPatch) -> None:
    """`browser` 与 `run_shell` 同一个陷阱（内核的 `run_browser` 也阻塞）。"""
    fake = FakeSandbox()
    _install(monkeypatch, fake)

    # `run_browser` 的空参数分支是**纯内存**的，不碰沙箱 —— 用它来验证
    # "调用链通到了内核实现"这件事。真正的阻塞分支要起 agent-browser，
    # 那超出单元测试范围（见 `tests/kernel/test_browser_tool.py`）。
    out = await chat_sandbox.tool_browser(FakeCtx("chat_1"), {})

    assert "command" in out, f"空参数没返回用法提示：{out!r}"


# ══════════════════════════════════════════════════════════
# 失败要说人话，不要说"重试"
# ══════════════════════════════════════════════════════════


async def test_a_sandbox_that_cannot_start_returns_readable_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """宿主 docker 坏了不是模型的错，说清楚它别瞎重试。

    ## 为什么这条重要

    沙箱起不来时如果返回空串或抛异常，模型会以为是自己的命令写错了，
    然后换个命令重试 —— 一样失败，白烧几轮 LLM 调用。返回里明写
    "这通常是宿主 docker 的问题，不是你的命令写错了"，模型才会停下来
    告诉用户。
    """
    fake = FakeSandbox(start_error=RuntimeError("Docker 不可用"))
    _install(monkeypatch, fake)

    out = await chat_sandbox.tool_run_shell(FakeCtx("chat_1"), {"command": "ls"})

    assert out.startswith("失败："), f"没标成失败：{out!r}"
    assert "docker" in out.lower() or "沙箱" in out
    assert "不是你的命令写错了" in out, "没告诉模型别瞎重试 —— 它会换个命令再来一次"


async def test_error_text_has_real_newlines_not_escaped_backslash_n(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """错误文案里必须是**真换行**，不能是字面量反斜杠 + n。

    ## 这条是怎么来的

    这几段文案是从内核侧照抄过来的，抄的时候把 `"\\n"` 抄成了 `"\\\\n"`
    —— 源码里是两个反斜杠，跑起来模型看到的就是六个字符 `\\n` 而不是
    换行。终端里它不报错，只是把整句话糊成一行，所以靠"测试还绿"完全
    发现不了。

    ## 判据

    直接查返回串里有没有**反斜杠 + n 这两个字符**。不能用
    `"\\n" in out` 那种写法 —— Python 里 `"\\n"` 就是换行本身，断言
    永远成立，等于没测。
    """
    fake = FakeSandbox(start_error=RuntimeError("Docker 不可用"))
    _install(monkeypatch, fake)

    shell_out = await chat_sandbox.tool_run_shell(FakeCtx("chat_1"), {"command": "ls"})
    browser_out = await chat_sandbox.tool_browser(FakeCtx("chat_1"), {"command": "open"})

    for label, out in (("run_shell", shell_out), ("browser", browser_out)):
        literal = chr(92) + "n"  # 反斜杠 + n，两个字符
        assert literal not in out, (
            f"{label} 的错误文案里有字面量反斜杠 n，模型会看到六个字符而不是换行：{out!r}"
        )
        assert "\n" in out, f"{label} 的错误文案没有真换行，整句会糊成一行：{out!r}"


async def test_an_empty_command_is_rejected_without_touching_the_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空命令在**起沙箱之前**就该被拒。

    反过来的话，用户点了个空白输入会让宿主起一个容器然后立刻失败 ——
    一次几秒的等待换一句"参数错误"。
    """
    fake = FakeSandbox()
    _install(monkeypatch, fake)

    out = await chat_sandbox.tool_run_shell(FakeCtx("chat_1"), {"command": "   "})

    assert "command" in out
    assert fake.started is False, "空命令不该起沙箱"
    assert fake.shell_calls == [], "空命令不该碰 shell"


# ══════════════════════════════════════════════════════════
# 命名：必须被孤儿回收认领
# ══════════════════════════════════════════════════════════


def test_sandbox_names_are_claimed_by_the_orphan_sweeper() -> None:
    """容器名必须以 `ctf2-` 开头。

    `runner.sweep_orphan_sandboxes()` 用 `docker ps --filter name=^ctf2-`
    认领孤儿。对话沙箱如果不落在这个前缀下，进程被 `systemctl restart`
    杀掉时（`finally` 没机会跑）它们会一个个攒在宿主上，而**没有任何
    界面能看到它们**。
    """
    from app.kernel import runner

    name = chat_sandbox._sandbox_name("chat_abcdefghijklmnop")
    assert name.startswith(runner.SANDBOX_NAME_PREFIX), (
        f"容器名 {name!r} 不以 {runner.SANDBOX_NAME_PREFIX!r} 开头 —— 孤儿回收认领不到它"
    )
    assert name.startswith(runner.AGENT_SANDBOX_PREFIX), (
        f"容器名 {name!r} 不以 {runner.AGENT_SANDBOX_PREFIX!r} 开头"
    )


def test_sandbox_names_are_unique_even_for_the_same_session() -> None:
    """同一个 session_id 建两次，名字必须不同（旧的还活着时不会撞名）。"""
    a = chat_sandbox._sandbox_name("chat_same")
    b = chat_sandbox._sandbox_name("chat_same")
    assert a != b


def test_sandbox_names_stay_within_the_docker_length_limit() -> None:
    """Docker 容器名上限 63 字符。超了 `docker run` 直接报错。"""
    long_id = "chat_" + "x" * 200
    name = chat_sandbox._sandbox_name(long_id)
    assert len(name) <= 63, f"容器名 {len(name)} 字符，超过 Docker 上限 63"


# ══════════════════════════════════════════════════════════
# 空闲回收：唯一在正常运行期真会发生的回收路径
# ══════════════════════════════════════════════════════════
#
# ## 这一节为什么存在
#
# v0.107 交付时我以为"回收"已经有了：归档会话会销毁、进程启动会清孤儿。
# 端到端实测把这个假设打掉了 —— 一台机器跑几轮验证就攒了 6 个
# `ctf2-chat-*`（每个 `--memory 2g`，配额 12 GiB），而宿主只有 7.9 GiB，
# `available` 掉到 930 MiB。
#
# 根因不是"忘了写销毁"，是**两条销毁路径在正常使用中都不会被触发**：
# 归档路由真实语义是"软删/可取消"，几乎不调；sweep 只在重启时跑。
# 所以判据不是"有没有回收代码"，而是
# 「正常运行一小时内，那个函数被调到几次」—— 答案是 0。
#
# 下面这几条钉住的是：**空闲到达阈值时，回收真的会发生，且只发生在
# 该被回收的那些沙箱上**。


def _age(session_id: str, seconds: float) -> None:
    """把一个会话的 `last_used` 往回拨 `seconds` 秒。

    直接改 `_Entry` 而不是 sleep：把用例的耗时从"分钟级"降到"微秒级"，
    否则只能靠 `time.sleep(阈值)` 来造空闲，一个用例就得跑三十秒。
    """
    with chat_sandbox._LOCK:
        entry = chat_sandbox._SANDBOXES[session_id]
        entry.last_used = time.monotonic() - seconds


def test_an_idle_sandbox_is_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """空闲超过阈值的沙箱必须被销毁并忘掉。

    这条是本节的核心断言：不成立的话，进程跑一整天就会攒下
    "用户开过几个会话"个常驻容器。
    """
    fake = FakeSandbox()
    _install(monkeypatch, fake)
    chat_sandbox.get_sandbox(FakeSession("chat_idle"))
    assert fake.destroyed is False

    _age("chat_idle", chat_sandbox.IDLE_REAP_SECONDS + 1)
    reaped = chat_sandbox.reap_idle_sandboxes()

    assert reaped == 1, "空闲超阈值的沙箱没被回收"
    assert fake.destroyed is True, "沙箱被忘了但容器没销毁 —— 会变成孤儿"
    assert chat_sandbox.has_sandbox("chat_idle") is False


def test_a_busy_sandbox_is_not_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """刚用过的沙箱不许回收。

    ⚠️ 这条比上一条更重要：误杀正在用的沙箱会让模型在下一轮看到
    `/work` 空了、正在跑的任务被打断。宁可多占一会儿内存。
    """
    fake = FakeSandbox()
    _install(monkeypatch, fake)
    chat_sandbox.get_sandbox(FakeSession("chat_busy"))

    # 只往回拨 1 秒 —— 远没到阈值。
    _age("chat_busy", 1.0)
    reaped = chat_sandbox.reap_idle_sandboxes()

    assert reaped == 0
    assert fake.destroyed is False, "刚用过的沙箱被误杀了"
    assert chat_sandbox.has_sandbox("chat_busy") is True


def test_using_a_sandbox_resets_its_idle_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """**每次命中 `get_sandbox` 都要刷新时间戳。**

    ## 这条钉住的是什么缺陷

    最初的实现里 `get_sandbox` 命中缓存时直接 `return existing`，没有更新
    `last_used`。后果不是"立刻出错"，而是：一个每分钟用一次的会话，
    它的 `last_used` 永远停在**创建那一刻**，于是启动满 30 分钟后被
    回收 —— 而它明明一直在用。

    ## ⚠️ 时序必须这样构造（第一版写错过）

    第一版把沙箱老化到 `阈值 - 60 秒`，然后"用一次"再回收。看起来合理，
    但**变异验证时它没红**：因为不刷新时间戳时 `age = 1740s < 1800s`，
    本来就不该回收 —— 刷新与不刷新结果一样，测不出差别。

    正确构造是：先把沙箱老化到**已经超过阈值**（`阈值 + 1`），此时它
    按道理该被回收；再用一次 —— 这一次必须把它救回来。于是：
      - 刷新了 → `age ≈ 0` → 不回收（期望）
      - 没刷新 → `age = 1801s` → 被回收（缺陷）
    两种实现的**结果相反**，这才有判别力。
    """
    fake = FakeSandbox()
    _install(monkeypatch, fake)
    session = FakeSession("chat_active")
    chat_sandbox.get_sandbox(session)

    # 弄老到"已经该被回收"的程度。
    _age("chat_active", chat_sandbox.IDLE_REAP_SECONDS + 1)

    # 再用一次 —— 这一次必须把时间戳拨回"现在"，把它救回来。
    chat_sandbox.get_sandbox(session)

    assert chat_sandbox.reap_idle_sandboxes() == 0, (
        "命中缓存的 `get_sandbox` 没有刷新 last_used —— "
        "一直在用的会话会被误当成空闲回收掉"
    )
    assert fake.destroyed is False


def test_reaping_only_touches_the_idle_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    """多个会话时，只回收空闲的那些。

    这条防的是"回收器把注册表整个清空"这种粗暴实现 —— 那在只有一个
    沙箱的用例里完全看不出来。
    """
    idle = FakeSandbox(name="idle")
    busy = FakeSandbox(name="busy")
    session_to_sandbox = {"chat_idle": idle, "chat_busy": busy}
    monkeypatch.setattr(
        chat_sandbox,
        "_new_sandbox",
        lambda session: session_to_sandbox[session.id],
    )

    chat_sandbox.get_sandbox(FakeSession("chat_idle"))
    chat_sandbox.get_sandbox(FakeSession("chat_busy"))

    _age("chat_idle", chat_sandbox.IDLE_REAP_SECONDS + 1)

    assert chat_sandbox.reap_idle_sandboxes() == 1
    assert idle.destroyed is True
    assert busy.destroyed is False, "正在用的沙箱被一起回收了"
    assert chat_sandbox.has_sandbox("chat_busy") is True


def test_reaping_an_empty_registry_is_harmless() -> None:
    """没有沙箱时回收必须什么都不做（后台线程每分钟都会调它）。"""
    assert chat_sandbox.reap_idle_sandboxes() == 0


def test_a_failed_destroy_does_not_stop_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    """一个容器删不掉，不能连累其余沙箱的回收。

    ## 为什么这条真实存在

    `docker rm` 会因为"容器有进程正在用"、"docker daemon 正忙"等
    各种原因失败。如果 `reap_idle_sandboxes` 让第一个异常逃出去，
    后台线程会带着它死掉 —— 之后永不回收，而症状只是"内存又开始涨"，
    界面上完全看不出来。
    """
    doomed = FakeSandbox(name="doomed")
    healthy = FakeSandbox(name="healthy")

    def failing_destroy() -> None:
        doomed.destroy_count += 1
        raise RuntimeError("docker rm 失败")

    doomed.destroy = failing_destroy  # type: ignore[method-assign]
    session_to_sandbox = {"chat_a": doomed, "chat_b": healthy}
    monkeypatch.setattr(
        chat_sandbox,
        "_new_sandbox",
        lambda session: session_to_sandbox[session.id],
    )

    chat_sandbox.get_sandbox(FakeSession("chat_a"))
    chat_sandbox.get_sandbox(FakeSession("chat_b"))
    _age("chat_a", chat_sandbox.IDLE_REAP_SECONDS + 1)
    _age("chat_b", chat_sandbox.IDLE_REAP_SECONDS + 1)

    # 不抛异常，且 healthy 仍然被销毁了。
    assert chat_sandbox.reap_idle_sandboxes() == 2
    assert healthy.destroyed is True, "一个 destroy 失败连累了另一个沙箱的回收"


def test_reaping_takes_a_custom_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """阈值可传参 —— 后台线程用默认值，诊断时可以立刻回收。"""
    fake = FakeSandbox()
    _install(monkeypatch, fake)
    chat_sandbox.get_sandbox(FakeSession("chat_x"))

    _age("chat_x", 5.0)

    # 默认阈值下不该回收。
    assert chat_sandbox.reap_idle_sandboxes() == 0
    # 显式给 1 秒就该回收。
    assert chat_sandbox.reap_idle_sandboxes(idle_seconds=1.0) == 1
    assert fake.destroyed is True


# ══════════════════════════════════════════════════════════
# 回收线程：必须活着、必须停得掉、必须不把异常抛出去
# ══════════════════════════════════════════════════════════


def test_the_reaper_thread_reaps_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """线程真的会自己调回收 —— 不只是"有个 start_reaper 函数"。

    这是"接线"断言：`start_reaper()` 写在 `app/main.py` 里，如果它
    只是把线程起起来而线程里什么都不做，前面所有回收测试仍然全绿。
    """
    fake = FakeSandbox()
    _install(monkeypatch, fake)
    chat_sandbox.get_sandbox(FakeSession("chat_bg"))
    _age("chat_bg", chat_sandbox.IDLE_REAP_SECONDS + 1)

    # 间隔给 20ms，等最多 2 秒 —— 足够宽松，不会在慢机器上偶发。
    chat_sandbox.start_reaper(interval_seconds=0.02)
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not fake.destroyed:
            time.sleep(0.01)
        assert fake.destroyed is True, "回收线程起来了但从不真的回收"
    finally:
        chat_sandbox.stop_reaper()


def test_the_reaper_stops_cleanly() -> None:
    """`stop_reaper()` 返回后线程必须已经退出。

    不退出的话，测试之间会留下跨用例的后台线程去动下一个用例的注册表，
    表现为"单跑全绿、全量偶发红"。
    """
    import threading

    before = {t.name for t in threading.enumerate()}
    chat_sandbox.start_reaper(interval_seconds=0.01)
    during = {t.name for t in threading.enumerate()}
    assert "chat-sandbox-reaper" in during, "start_reaper 没起线程"

    chat_sandbox.stop_reaper()

    after = {t.name for t in threading.enumerate()}
    assert "chat-sandbox-reaper" not in after, "stop_reaper 返回后线程还活着"
    # 冒烟：确认前后集合差异只有这一个线程（别顺手起了别的）。
    assert during - before == {"chat-sandbox-reaper"}


def test_starting_the_reaper_twice_is_idempotent() -> None:
    """重复 start 不该起两个线程（lifespan 重入 / 测试重复调）。"""
    import threading

    chat_sandbox.start_reaper(interval_seconds=0.05)
    try:
        chat_sandbox.start_reaper(interval_seconds=0.05)
        names = [t.name for t in threading.enumerate()]
        assert names.count("chat-sandbox-reaper") == 1, "起了不止一个回收线程"
    finally:
        chat_sandbox.stop_reaper()


def test_reaper_is_running_reports_the_truth() -> None:
    """`reaper_is_running()` 必须反映真实状态 —— 它是**唯一**可靠判据。

    ## 为什么不能靠 `ps -T` 查线程名（实测踩过）

    CPython 起线程时不会把系统层面的 `comm` 设成
    `threading.Thread(name=...)` 给的名字。实测：往一个进程里起一个叫
    `chat-sandbox-reaper` 的线程，`ps -T -o comm` 输出的仍是 `python`；
    在 uvicorn 进程里则全显示成 `uvicorn`。

    所以我第一次部署后核查「回收器在不在跑」时，看到线程列表里没有
    `chat-sandbox-reaper`，一度以为它没启动 —— 白查一轮。这个函数
    就是为了不再重复那次误判：它有判别力，`ps -T` 没有。
    """
    chat_sandbox.stop_reaper()
    assert chat_sandbox.reaper_is_running() is False, (
        "还没 start 就报告在跑"
    )

    chat_sandbox.start_reaper(interval_seconds=0.05)
    try:
        assert chat_sandbox.reaper_is_running() is True, (
            "start_reaper 之后报告没在跑"
        )
    finally:
        chat_sandbox.stop_reaper()

    assert chat_sandbox.reaper_is_running() is False, (
        "stop_reaper 之后仍报告在跑"
    )


def test_the_reaper_logs_that_it_started(caplog: pytest.LogCaptureFixture) -> None:
    """启动必须留一行日志。

    ## 为什么"打日志"在这里是功能而不是装饰

    回收器失效是**完全静默**的：线程没起、或起在一次未捕获异常之后
    死掉，都不会有任何报错。可见的症状要几小时后才出现（内存涨了、
    别人的沙箱 `docker run` 失败），那时没有任何线索指向这里。

    而它也没有别的观测面：没有容器要回收时 `docker ps` 跑一万次也
    看不出差别；`ps -T` 又读不到线程名（见上一条）。所以这行日志是
    运维唯一能确认"它真的起来了"的凭据 —— 这是可测试的。
    """
    import logging

    caplog.set_level(logging.INFO)
    chat_sandbox.start_reaper(interval_seconds=0.05)
    chat_sandbox.stop_reaper()

    started = [
        r.getMessage() for r in caplog.records if "对话沙箱空闲回收已启动" in r.getMessage()
    ]
    assert started, (
        "回收器启动没有留下日志 —— 运维无法确认它是否在跑，"
        "而它失效时的症状要几小时后才可见。"
        f"实际日志：{[r.getMessage() for r in caplog.records]}"
    )
    # 日志里要带上两个关键数字，否则运维看不出"多久判定为空闲"。
    assert "30" in started[0] or "1800" in started[0] or "空闲超过" in started[0], (
        f"启动日志没说清回收阈值：{started[0]}"
    )


def test_the_reaper_survives_a_failed_round(monkeypatch: pytest.MonkeyPatch) -> None:
    """**一轮回收抛异常，线程必须继续跑下一轮。**

    ## 这条为什么必须存在（变异验证抓出来的缺口）

    最初的这一节只测了"线程会自己回收"。把 `_loop` 里的 try/except 删掉
    之后，**所有测试仍然全绿** —— 因为正常路径上从来没有异常，那层
    保护根本没有被测到。

    而这个保护的失效方式是**完全静默**的：线程死在一次偶发的
    `docker rm` 失败上，之后永不回收。症状不是报错，是几小时后
    "内存又涨了、别人的沙箱起不来" —— 没有任何界面能看出回收器
    已经不在跑了。所以它必须被测到。

    构造方式：让 `reap_idle_sandboxes` 前两次调用抛异常，第三次
    才真正干活 —— 然后断言它真的干到了活。
    """
    fake = FakeSandbox()
    _install(monkeypatch, fake)
    chat_sandbox.get_sandbox(FakeSession("chat_resilient"))
    _age("chat_resilient", chat_sandbox.IDLE_REAP_SECONDS + 1)

    real = chat_sandbox.reap_idle_sandboxes
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> int:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("偶发的 docker rm 失败")
        return real(*args, **kwargs)

    monkeypatch.setattr(chat_sandbox, "reap_idle_sandboxes", flaky)

    chat_sandbox.start_reaper(interval_seconds=0.02)
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not fake.destroyed:
            time.sleep(0.01)
        assert calls["n"] > 2, "回收线程在前两轮异常之后就死了，没再调第三次"
        assert fake.destroyed is True, "线程活着但异常之后再也不干活了"
    finally:
        chat_sandbox.stop_reaper()


def test_stopping_a_reaper_that_never_started_is_harmless() -> None:
    """`stop_reaper()` 在没起过线程时必须直接返回（停机路径幂等）。"""
    chat_sandbox.stop_reaper()
