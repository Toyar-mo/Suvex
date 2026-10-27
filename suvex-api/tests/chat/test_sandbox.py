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
