"""对话沙箱（v0.107）：让对话页真的能执行东西。

## 这个文件解决的是什么

v0.100 之前，对话页的工具全是**平台操作**（建扫描、改技能、加 MCP）。
模型被问"帮我看看这个站"时只能回一句"我没有执行环境" —— 而那句话
是 `prompts.py` 里**主动写进提示词**的（见 `SYSTEM_PROMPT_TEMPLATE` 的
"【最重要】哪些事你做不到"一段）。用户的原话（m02331）：

> 还有这个对话不能直接解题，需要修

修法不是"再实现一套执行工具"。`app/kernel/runner.py` 里已经有
`run_shell` / `browser` 两个实现，它们**只依赖 `SandboxContainer`**，
不依赖 `SolveSpec`。这个文件就是把它们接过来，而不是抄一份。

## ★ 三个关键设计决定

### 1. 不伪造 `SolveSpec`

`dispatch_tool` 需要 `SolveSpec`（题目 id、白名单、附件、http 历史…），
但对话**没有题目**。伪造一个空壳 `SolveSpec` 会立刻出问题：
`_tool_http_request` 会拿 `spec.access_urls` 当白名单，而空元组意味着
"什么都不能访问"；`_tool_read_attachment` 会查 `spec.attachments`，
空元组意味着"没有附件"。

所以这里**直接调具名实现函数**（`_tool_run_shell` / `run_browser`），
绕过 `dispatch_tool`。这两个函数的签名里只有 `sandbox`，正是我们要的。

### 2. `run_shell` 是 `async def` 但**没有 await**

这是个陷阱。`runner._tool_run_shell` 声明成 `async def`（因为
`dispatch_tool` 在 async 上下文里调它），但函数体里**一个 await 都没有**
—— 它直接调 `sandbox.shell()`，而那是 `subprocess.run(...)` 阻塞调用。

在单 worker 的 uvicorn（`deploy/suvex-api.service` 的 `ExecStart` 没有
`--workers`）里，直接在事件循环里 `await` 它 = **整个 API 冻住**，
最长冻 `DEFAULT_SHELL_TIMEOUT`（300 秒）。所有其他请求（含 SSE 的
keepalive、其他会话的轮次）全部卡死。

所以这里的每一次沙箱调用都包 `asyncio.to_thread(...)`。

### 3. 沙箱按会话惰性创建、跨轮保留

- **惰性**：首次真正需要执行时才 `start()`。一轮纯问答不该付起容器的
  1~3 秒。
- **跨轮保留**：`/work` 里的中间产物（解压出来的文件、写的脚本）下一轮
  还要用。每轮销毁等于让模型每轮从头再来。
- **回收**：`DELETE /chat/sessions/{id}` 时销毁；进程重启时由
  `runner.sweep_orphan_sandboxes()` 兜底（`ctf2-chat-` 前缀天然被
  `ctf2-` 判据覆盖）。

## 无限制档

`SandboxContainer(unrestricted=True)` —— 见 `runner._unrestricted_argv`。
用户原话（m05493）："做一个没有限制的沙箱给他用"。这一档的差异是
`--user 0:0`（容器内 root）+ 可写容器层 + `suvex-agent` 网络（能出网），
但**仍然**没有 `--privileged`、没有 docker.sock、没有 `--network host`。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any

from app.chat.models import ChatSession

_log = logging.getLogger(__name__)

#: 会话 id → 沙箱。进程内单例，靠 `_lock` 保护。
#:
#: ⚠️ 为什么可以放进程内存：`suvex-api.service` 是**单 worker**
#: （`ExecStart` 里没有 `--workers`），所以进程内状态跨请求可见。
#: 哪天加了 `--workers > 1`，这里会变成"有时找得到沙箱、有时找不到" ——
#: 那时必须换成共享存储（Redis 或 DB 表）。这个注释就是那个前提的钉子。
_SANDBOXES: dict[str, Any] = {}
#: 保护 `_SANDBOXES` 本身（只做字典读写，毫秒级）。
_LOCK = threading.Lock()
#: 保护"某个会话的沙箱正在启动"这一段（`docker run` 1~3 秒）。
#:
#: ## 为什么不复用 `_LOCK`
#:
#: 复用会让**所有会话**的首次使用串行：A 会话起容器那 3 秒里，B 会话
#: 连"我有没有沙箱"都查不了。那是把一个会话内部的问题扩散成全局问题。
#: 每会话一把锁，锁粒度正好等于"这个会话的沙箱生命周期"。
_STARTING: dict[str, threading.Lock] = {}

#: 每个会话沙箱的资源配额。
#:
#: 与解题侧对齐（`scans` 的 `sandbox_cpus` / `sandbox_memory_mb` 默认值）。
#: 这台 Kali 只有 8G 内存（`runner.SandboxContainer.__init__` 的注释里
#: 写了实测），而对话沙箱是**长期驻留**的（不像解题沙箱跑完就销毁），
#: 两个会话各占 2G 就已经很紧。给保守值，让"起不来"早暴露。
DEFAULT_CPUS = 2
DEFAULT_MEMORY_MB = 2048


def _sandbox_name(session_id: str) -> str:
    """容器名。

    ## 为什么用 `os.urandom` 而不是只用 session_id

    `session_id` 是数据库主键（形如 `chat_xxxx`），理论上可以复用
    （删了再建）。容器名带一段随机数，即使 session_id 复用也不会撞上
    一个**还活着**的旧容器 —— 那种情况 `docker run` 会报
    "name already in use"，而报错信息完全指不出真正原因。

    ## 为什么截断到 12 个字符

    Docker 容器名上限 63 字符。`ctf2-chat-` (10) + 12 + `-` + 8 = 31，
    留足余量。截断本身不需要保证唯一 —— 唯一性由随机段负责。
    """
    tail = session_id[-12:] if len(session_id) > 12 else session_id
    return f"ctf2-chat-{tail}-{os.urandom(4).hex()}"


def _new_sandbox(session: ChatSession) -> Any:
    """构造（但**不启动**）一个对话沙箱。

    ## 为什么把 import 放在函数里

    `app.kernel.runner` 是**全项目最重的模块**（一万多行，`import` 会
    连带拉起 docker / httpx / 各种 provider 协议判定）。对话页在
    **不执行任何东西**的纯问答轮次里不该付这个成本。

    ⚠️ 这与 `app/kernel/tools.py` 的模块 docstring 里那条"抄一份
    工具清单而不是 import runner"是同一个理由的**两种应对**：那边
    抄了清单 + 对账测试，因为清单是数据；这边 import 的是函数，
    抄函数会把实现搞成两份。所以这里选择延迟 import。
    """
    from app.kernel import runner

    return runner.SandboxContainer(
        name=_sandbox_name(session.id),
        # ★ 无限制档：对话是通用 agent，不是解题。它要能装包、写代码、
        #   出网搜索 —— 这些在受限档（`--read-only` + `--network none`）
        #   里全做不到。见模块 docstring 的"无限制档"。
        unrestricted=True,
        image=runner.IMAGE_BASE,
        cpus=DEFAULT_CPUS,
        memory_mb=DEFAULT_MEMORY_MB,
        host_resources=runner._host_resources_path(),
        # 对话附件目录只读挂进去。`run_shell` 里 `file` / `strings` /
        # `binwalk` 比 `read_attachment`（遇二进制只回 256 字节 hex）有用。
        attachments_dir=None,
        # 不挂持久工作区：对话的工作集应该**随沙箱生灭**。
        # 解题侧挂 `persist` 是因为一道题可能续解好几天；对话没有这个需求，
        # 而挂上去会让"删了会话但 /work/persist 里还留着东西"。
        workspace_dir=None,
        extra_env={"PWNLIB_NOTERM": "1"},
    )


def get_sandbox(session: ChatSession) -> Any:
    """拿到这个会话的沙箱，**必要时启动**。

    ## 为什么整个函数是同步的

    调用方（`engine._execute_tool`）会用 `asyncio.to_thread` 包它 ——
    `start()` 是 `docker run`（1~3 秒），不能占事件循环。

    ## 为什么每会话一把锁，而不是全局一把

    同一会话可能有两轮同时在跑（用户点了发送又插话，或者双标签页）。
    两轮同时看到"没有沙箱"就会各自 `docker run` 一个，其中一个报
    "name already in use" —— 而那个错误发生在**另一个轮次刚建好的
    容器**上，排查起来极难。所以这个会话的启动必须串行。

    但**不能**用全局锁：那样 A 会话起容器的 3 秒里，B 会话连"我有没有
    沙箱"都查不了。锁粒度 = 会话。
    """
    with _LOCK:
        existing = _SANDBOXES.get(session.id)
        if existing is not None:
            return existing
        # 拿（或建）这个会话的启动锁。在 `_LOCK` 里做，避免两个线程
        # 同时 `setdefault` 出两把不同的锁。
        start_lock = _STARTING.setdefault(session.id, threading.Lock())

    with start_lock:
        # 双重检查：等锁期间可能已经有别的线程建好了。
        with _LOCK:
            existing = _SANDBOXES.get(session.id)
            if existing is not None:
                return existing

        sandbox = _new_sandbox(session)
        try:
            sandbox.start()
        except Exception:
            # ★ 启动失败必须把半成品清掉：`SandboxContainer.start()` 在
            #   `_verify_running()` 之前就已经 `docker run` 过了，容器可能
            #   存在但活着活着就退了（见它的注释："docker run 成功不等于
            #   还活着"）。不清就会攒孤儿容器。
            try:
                sandbox.destroy()
            except Exception:  # noqa: BLE001 - 清理失败不该盖住真正原因
                _log.warning("清理启动失败的对话沙箱时出错", exc_info=True)
            with _LOCK:
                _STARTING.pop(session.id, None)
            raise

        with _LOCK:
            _SANDBOXES[session.id] = sandbox
            _STARTING.pop(session.id, None)
        _log.info("对话沙箱已就绪：session=%s container=%s", session.id, sandbox.name)
        return sandbox


def drop_sandbox(session_id: str) -> None:
    """销毁并忘掉一个会话的沙箱。**幂等**。

    调用点：`DELETE /chat/sessions/{id}`。也用于测试清理。
    """
    with _LOCK:
        sandbox = _SANDBOXES.pop(session_id, None)
        _STARTING.pop(session_id, None)
    if sandbox is None:
        return
    try:
        sandbox.destroy()
    except Exception:  # noqa: BLE001 - 销毁失败不该让删会话这个动作失败
        _log.warning("销毁对话沙箱 %s 时出错", session_id, exc_info=True)


def drop_all_sandboxes() -> int:
    """销毁所有对话沙箱，返回销毁了几个。用于测试与进程退出兜底。"""
    with _LOCK:
        items = list(_SANDBOXES.items())
        _SANDBOXES.clear()
        _STARTING.clear()
    for session_id, sandbox in items:
        try:
            sandbox.destroy()
        except Exception:  # noqa: BLE001
            _log.warning("销毁对话沙箱 %s 时出错", session_id, exc_info=True)
    return len(items)


def has_sandbox(session_id: str) -> bool:
    """这个会话是否已经有活着的沙箱。给测试和诊断用，不启动新的。"""
    with _LOCK:
        return session_id in _SANDBOXES


# ══════════════════════════════════════════════════════════
# 工具实现
# ══════════════════════════════════════════════════════════
#
# 这四个函数的签名与 `engine._TOOL_IMPLS` 里其他工具一致：
# `async def impl(ctx, args) -> str`。区别是它们内部不碰 `ctx.db`，
# 只碰沙箱 —— 因为执行结果不需要落库（工具输出会由引擎写成消息）。


async def _tool_run_shell(ctx: Any, args: dict[str, Any]) -> str:
    """在对话沙箱里执行 shell 命令。

    ## 为什么不是直接 `await runner._tool_run_shell(...)`

    那个函数虽然声明成 `async`，内部却是阻塞的 `sandbox.shell()` ——
    直接 await 会冻住整个事件循环（见模块 docstring 决定 2）。
    这里的写法是：把"启动沙箱 + 执行"整段丢进线程。

    ## 参数

    与内核版完全一致（`command` / `timeout_seconds` / `cwd`），因为
    提示词里教模型的就是内核那套用法 —— 两处参数名如果不一样，
    模型会把解题时的习惯带过来然后收到"参数错误"。
    """
    from app.kernel import runner

    command = str(args.get("command") or "").strip()
    if not command:
        return "参数错误：需要 `command`。"

    def _work() -> str:
        sandbox = get_sandbox(ctx.session)
        # 注意：这里调的是**内核的 async 函数**，但因为没有 await 点，
        # 在线程里用 `asyncio.run` 跑它是安全的（它内部不碰事件循环）。
        return asyncio.run(runner._tool_run_shell(args, sandbox=sandbox))

    try:
        return await asyncio.to_thread(_work)
    except RuntimeError as exc:
        # ★ 这条分支只可能来自**我们自己的**编程错误：`_work` 里的
        #   `asyncio.run(...)` 在已经有事件循环的线程里会抛
        #   `RuntimeError: asyncio.run() cannot be called from a running event loop`。
        #
        #   为什么单独接住：它和"宿主 docker 坏了"完全是两回事，但会被
        #   下面那个 `except Exception` 一起吞掉，然后告诉模型"这通常是
        #   宿主 docker 的问题" —— 于是一个**代码缺陷**被包装成一条看起来
        #   像运维问题的消息，排查方向直接错。
        #
        #   实测踩到过：变异验证时把 `to_thread` 删掉，两条测试红在了
        #   "沙箱起不来"上，而真正的原因是事件循环被冻住。
        if "running event loop" in str(exc):
            _log.error(
                "对话 run_shell 在事件循环里跑了阻塞代码 —— `asyncio.to_thread` 被绕过了。"
                "这是代码缺陷，不是环境问题。",
                exc_info=True,
            )
            raise
        _log.warning("对话 run_shell 失败", exc_info=True)
        return (
            f"失败：沙箱起不来（{type(exc).__name__}: {exc}）。\n"
            "这通常是宿主 docker 的问题，不是你的命令写错了。"
            "把这条错误告诉用户，别换个命令重试 —— 重试会一样失败。"
        )
    except Exception as exc:  # noqa: BLE001 - 沙箱起不来不该毁掉整轮
        _log.warning("对话 run_shell 失败", exc_info=True)
        return (
            f"失败：沙箱起不来（{type(exc).__name__}: {exc}）。\n"
            "这通常是宿主 docker 的问题，不是你的命令写错了。"
            "把这条错误告诉用户，别换个命令重试 —— 重试会一样失败。"
        )


async def _tool_browser(ctx: Any, args: dict[str, Any]) -> str:
    """在对话沙箱里操作浏览器（agent-browser）。

    实现与 `run_shell` 同构：内核的 `run_browser` 也是 `async def`
    但没有 await 点，它内部 `_invoke` 走的是阻塞 subprocess。
    """
    from app.kernel import browser as browser_mod

    def _work() -> str:
        sandbox = get_sandbox(ctx.session)
        return asyncio.run(browser_mod.run_browser(args, sandbox=sandbox))

    try:
        return await asyncio.to_thread(_work)
    except RuntimeError as exc:
        # 同 `run_shell`：这是代码缺陷，不是浏览器/环境问题。
        if "running event loop" in str(exc):
            _log.error(
                "对话 browser 在事件循环里跑了阻塞代码 —— `asyncio.to_thread` 被绕过了。",
                exc_info=True,
            )
            raise
        _log.warning("对话 browser 失败", exc_info=True)
        return (
            f"失败：浏览器不可用（{type(exc).__name__}: {exc}）。\n"
            "这通常是沙箱起不来，不是选择器写错了。"
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("对话 browser 失败", exc_info=True)
        return (
            f"失败：浏览器不可用（{type(exc).__name__}: {exc}）。\n"
            "这通常是沙箱起不来，不是选择器写错了。"
        )


__all__ = [
    "DEFAULT_CPUS",
    "DEFAULT_MEMORY_MB",
    "drop_all_sandboxes",
    "drop_sandbox",
    "get_sandbox",
    "has_sandbox",
    # ★ 引擎的 `_TOOL_IMPLS` 用这两个名字注册。对外不带下划线，因为它们
    #   是**跨模块**的注册点 —— 下划线在 `engine.py` 里读起来像"内部函数
    #   被外部调用"，正是那种让人怀疑"是不是该抄一份"的信号。
    #
    #   `load_skill` / `load_recipe` 不在这里：它们纯查表、不碰沙箱，
    #   实现在 `engine.py` 里（见 `_tool_load_skill` / `_tool_load_recipe`）。
    "tool_browser",
    "tool_run_shell",
]

#: 引擎注册表用的公开别名。定义在 `__all__` 之后，避免循环引用。
tool_run_shell = _tool_run_shell
tool_browser = _tool_browser
