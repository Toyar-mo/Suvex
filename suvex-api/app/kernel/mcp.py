"""MCP（Model Context Protocol）工具桥 —— 学 strix 的 `list_mcps`/`describe_mcp`/`call_mcp`。

## 这是什么、为什么需要

MCP 已经是"把外部工具接进 agent"的事实标准：一个 MCP server 用 JSON-RPC 2.0
暴露若干工具（每带一份 JSON Schema），agent 侧按 `tools/list` 发现、按
`tools/call` 调用。strix 把这件事做成了**三个通用工具**而不是"每个 MCP 工具
各注册一个 agent 工具"：

  - `list_mcps()`            → 这次运行能连到哪些 server（id/名字/描述/工具数）
  - `describe_mcp(conn)`     → 某个 server 提供哪些工具，各带输入 schema
  - `call_mcp(conn, tool, arguments)` → 调一个工具

这样系统提示词里**不需要**预先塞进所有 MCP 工具的清单（那会随连接数线性
膨胀），模型按需发现即可。

## 与 STRIX 的关系：借形状，不引依赖

strix 的实现依赖 `mcp` SDK + `agents.mcp`（openai-agents）。本项目**不引这两
个依赖**，理由与 `skills.py` 同一条：能力要能在**离线、可复现**的部署里跑，
少一个网络/版本依赖就少一处"部署时才发现装不上"。所以我们自己实现一个
**极小的 JSON-RPC 2.0 over stdio 客户端**，只覆盖 MCP 需要的那几个方法：
`initialize` / `notifications/initialized` / `tools/list` / `tools/call`。

## 传输：只做 stdio

MCP 有 stdio 与 streamable-HTTP 两种传输。这里**只做 stdio**（子进程，
按行分隔的 JSON-RPC）。HTTP 传输要处理 SSE、断线重连、鉴权、cancel scope
跨任务这些坑（strix 的 `session.py` 用了整整一套 supervising-task 架构就是
为此），投入产出比在这一版不划算。stdio 覆盖了绝大多数本地 MCP server
（`npx @modelcontextprotocol/server-xxx`、以及各种 `python -m xxx_mcp`）。

⚠️ 将来若要加 HTTP：务必把连接**放在独立任务**上持有（strix 的
`session.py` 头注释讲得很清楚——transport 的 cancel scope 在哪条任务上进入
就绑在哪条任务上，放错任务会让一次后台 403 把整个扫描 cancel 掉，还被误
标成"用户中断"）。这条经验是别人用血换的，别重新踩。

## 生命周期：每次调用即连即断

为了不泄漏子进程、也不需要在两个入口（单循环 `solve_in_sandbox` 与编排
`run_multi_agent`）都维护一套长连接与清理逻辑，这里的 `McpRegistry` 采用
**每次调用临时起一次**：spawn → initialize → list/call → 关闭。MCP server 的
启动开销通常是一次性的进程拉起（几十~几百毫秒），相对一次 LLM 调用
（数秒）可忽略。代价是连续调同一个 server 的多个工具会重复 initialize——
先保证正确与无泄漏，性能优化留到有实测瓶颈再说。

## 安全

- server 的 `command` 由**部署者**在设置里配置（`KernelConfig.mcp_servers`），
  不是模型能改的；模型只能对已配置的连接做 list/describe/call。
- `env` 里的值（可能含 token）**只进子进程环境，不进事件流、不进日志**。
- 参数按 JSON 对象透传，本模块**不解释**参数语义（由 server 自己校验）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: MCP 协议版本（客户端在 initialize 里声明自己支持的版本）。
#: 2024-11-05 是当前被最广泛实现的稳定版；server 若回别的版本我们照收
#: （协议要求客户端取 server 回的版本，不一致由客户端决定是否继续 ——
#: 这里选择继续，因为多数 server 只是把版本号原样带回）。
MCP_PROTOCOL_VERSION = "2024-11-05"

#: 单次 JSON-RPC 方法调用的默认超时（秒）。MCP 工具可能跑得比普通工具久
#: （它可能是一条慢查询 / 一次扫描），所以给得比 shell 默认更宽。
DEFAULT_MCP_TIMEOUT = 60.0

#: initialize 握手超时（秒）。server 拉起本身可能慢（npx 首次要下包），
#: 所以握手窗口比普通调用更宽。
INIT_TIMEOUT = 120.0

#: 单条 JSON-RPC 消息（含响应）允许的最大字节数。防止一个失控的 server
#: 往 stdout 灌一条几十 MB 的 JSON 把宿主内存打爆（与 `_do_http` 的读取
#: 上限是同一条纪律：模型能看到的有限，读更多纯属攻击面）。
MAX_MESSAGE_BYTES = 8 * 1024 * 1024

#: `describe_mcp` 单次返回的最大字符数（工具多时把 schema 截断）。
MAX_DESCRIBE_CHARS = 24_000


class McpError(Exception):
    """MCP 连接/协议/调用失败的统一异常。

    带一句**给人看的中文**（`message`），也带原始细节（`detail`）供日志。
    工具层捕获它后把 `message` 回给模型——模型据此换路子，而不是被
    traceback 打断循环。
    """

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class McpServerConfig:
    """一个 MCP server 连接的配置（部署者在设置里配，模型不可改）。

    字段刻意与 strix 的 `McpConnectionConfig` 对齐子集，但**只保留 stdio
    需要的**：`command` + `args` + `env`。HTTP 的 `url`/`auth` 这一版不支持
    （见模块 docstring）。
    """

    #: 连接名 = 模型在 list_mcps/describe_mcp/call_mcp 里用的 id。必填、唯一。
    name: str
    #: 拉起 server 的可执行文件（如 `npx` / `python3` / 绝对路径）。
    command: str
    #: 传给 `command` 的参数（如 `["-y", "@modelcontextprotocol/server-filesystem", "/data"]`）。
    args: list[str] = field(default_factory=list)
    #: 追加进子进程的环境变量（可含 token；**不落盘、不记日志**）。
    env: dict[str, str] = field(default_factory=dict)
    #: 可选的用途说明（进 `list_mcps` 的描述位，帮模型判断该不该用）。
    notes: str = ""
    #: 可选的白名单：只允许调这些工具名（None = 全放）。
    #: 注意这是**部署者的护栏**，不是安全边界（server 本身仍可能做任何事）——
    #: 真正的隔离靠"只配可信 server"。
    allowed_tools: list[str] | None = None
    #: ★ v0.83：单次 `tools/call` 的超时（秒）。`None` = 用 `DEFAULT_MCP_TIMEOUT`。
    #:
    #: ## 为什么必须可配（实测踩到的）
    #:
    #: 全局写死 60 秒时，慢工具**必然失败**。实测过两类：
    #:   · 需要先批量预处理再执行的：光展开上万个模板文件就要 **102 秒**；
    #:   · 网段扫描：一个 /24 约 **85 秒**。
    #:
    #: 而失败的样子是"读取超时"——模型看到的是 MCP 层的错，会以为是连接坏了，
    #: 于是改配置、换参数，而不是"这个操作本来就慢"。**超时值不该由
    #: 一个与具体 server 无关的常量决定**：`npx` 拉一个文件系统 server 和
    #: 跑一次全网段测绘，合理耗时差两个数量级。
    #:
    #: ⚠️ 如果 server 自己有内部超时，**这个值要与它对齐** ——
    #: 否则外层先超时、内层还在跑，子进程要等外层 kill。
    call_timeout: float | None = None

    def public_dict(self) -> dict[str, Any]:
        """给日志/回执用的安全视图：**去掉 env**（可能含凭据）。"""
        return {
            "name": self.name,
            "command": self.command,
            "args": list(self.args),
            "notes": self.notes,
            "allowed_tools": list(self.allowed_tools) if self.allowed_tools else None,
            # ★ v0.83：超时不是凭据，可以回显 —— 而且**应该**回显：
            #   部署者需要能看到"这条连接的超时到底是多少"，否则
            #   "我明明配了 300 秒"与"实际还是 60 秒"无从区分。
            "call_timeout": self.call_timeout,
        }


def parse_server_configs(raw: Any) -> tuple[McpServerConfig, ...]:
    """把 `KernelConfig.mcp_servers`（JSON 列）解析成配置元组。

    ## 为什么容错到"跳过坏条目"而不是整体抛错

    这一列是部署者手填的 JSON。一条写错（少 command、name 重复）不该让
    整次解题起不来——跳过坏的那条并记 warning，其余照用。但**全坏**时
    等价于"没配 MCP"，工具会如实说"没有可用的 MCP 连接"。

    ⚠️ 与项目一贯纪律一致：坏条目**记 warning**而不是静默丢弃，否则部署者
    会以为某个 server 生效了、实际没有。
    """
    if not raw:
        return ()
    # 接受 list 与 tuple：DB JSON 列给的是 list，而 `SolveSpec.mcp_servers`
    # 存的是 tuple（不可变）。两边都收，否则「把 spec 上的元组再解析一遍」
    # 会静默变成"未配置"——而那正是这条链路上会被踩到的形状。
    if not isinstance(raw, (list, tuple)):
        logger.warning("mcp_servers 配置不是列表，按未配置处理：%r", type(raw).__name__)
        return ()
    seen: set[str] = set()
    out: list[McpServerConfig] = []
    for index, item in enumerate(raw):
        # 已经是 `McpServerConfig` 的直接收（`SolveSpec.mcp_servers` 存的就是
        # 这种形状；再解析一遍会把它们当"坏条目"跳过，症状是"配了却说我
        # 没配"——这条链路最容易踩的形状）。
        if isinstance(item, McpServerConfig):
            if item.name in seen:
                logger.warning("mcp_servers[%d] 连接名重复（%s），跳过后者", index, item.name)
                continue
            seen.add(item.name)
            out.append(item)
            continue
        if not isinstance(item, dict):
            logger.warning("mcp_servers[%d] 不是对象，跳过：%r", index, type(item).__name__)
            continue
        name = str(item.get("name") or "").strip()
        command = str(item.get("command") or "").strip()
        if not name or not command:
            logger.warning("mcp_servers[%d] 缺 name 或 command，跳过", index)
            continue
        if name in seen:
            logger.warning("mcp_servers[%d] 连接名重复（%s），跳过后者", index, name)
            continue
        seen.add(name)
        args = item.get("args")
        args = [str(a) for a in args] if isinstance(args, list) else []
        env = item.get("env")
        env = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
        allowed = item.get("allowed_tools")
        allowed = [str(t) for t in allowed] if isinstance(allowed, list) else None
        # ★ v0.83：call_timeout（秒）。非法值按"没配"处理而不是报错 ——
        # 一条连接的字段写坏不该让整次任务起不来（与其余字段同一条容错纪律），
        # 但要记 warning，否则部署者会以为配的 300 秒生效了、实际还是 60 秒。
        raw_timeout = item.get("call_timeout")
        call_timeout: float | None = None
        if raw_timeout is not None:
            try:
                parsed = float(raw_timeout)
            except (TypeError, ValueError):
                parsed = 0.0
            if parsed > 0:
                call_timeout = parsed
            else:
                logger.warning(
                    "mcp_servers[%d] 的 call_timeout 不是正数（%r），按默认超时处理",
                    index,
                    raw_timeout,
                )
        out.append(
            McpServerConfig(
                name=name,
                command=command,
                args=args,
                env=env,
                notes=str(item.get("notes") or ""),
                allowed_tools=allowed,
                call_timeout=call_timeout,
            )
        )
    return tuple(out)


def _format_tool(tool: dict[str, Any]) -> str:
    """把一个 MCP 工具描述渲染成一段给人看的文本（名字+描述+schema）。"""
    name = str(tool.get("name") or "(无名)")
    desc = str(tool.get("description") or "").strip() or "（无描述）"
    schema = tool.get("inputSchema") or tool.get("input_schema") or {"type": "object"}
    try:
        schema_text = json.dumps(schema, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        schema_text = "{}"
    return f"- {name}: {desc}\n  输入 schema:\n{schema_text}"


class McpClient:
    """一个 MCP server 的 **单次** stdio 连接（即用即连即断）。

    用法（都在 async 上下文里）::

        async with McpClient(cfg) as client:
            tools = await client.list_tools()
            result = await client.call_tool("echo", {"text": "hi"})

    `__aenter__` 会 spawn 子进程并完成 initialize 握手；`__aexit__` 关闭
    子进程。任何协议错误都转成 `McpError`（带中文 message）。
    """

    def __init__(self, cfg: McpServerConfig) -> None:
        self.cfg = cfg
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._stderr_task: asyncio.Task[None] | None = None

    # ---- 生命周期 ---------------------------------------------------
    async def __aenter__(self) -> McpClient:
        await self._start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _start(self) -> None:
        env = dict(os.environ)
        # 配置里的 env 覆盖/追加进来（可含 token）。只进子进程。
        env.update(self.cfg.env)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.cfg.command,
                *self.cfg.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # ★ v0.83：**必须**显式给 `limit`，否则大结果会读崩。
                #
                # 实测：一次网段扫描的 JSON 结果行会超过 asyncio 默认行缓冲
                # （64 KiB），于是 `stdout.readline()` 直接抛：
                #
                #     ValueError: Separator is found, but chunk is longer than limit
                #
                # ⚠️ 这个错误**极其难读懂** —— 它看起来像协议错误或连接问题，
                #    而真实原因是"这一行太长了"。模型只会看到一句莫名其妙的
                #    异常，然后去重试一个永远不可能成功的调用。
                #
                # 设成 `MAX_MESSAGE_BYTES`（8 MiB）而不是更大：那个常量本来就是
                # 我们允许的单条消息上限（防止失控 server 把宿主内存打爆），
                # 两处用同一个数，才不会出现"读取层允许、校验层拒绝"的分裂。
                limit=MAX_MESSAGE_BYTES,
            )
        except FileNotFoundError as exc:
            raise McpError(
                f"MCP 连接 `{self.cfg.name}` 的可执行文件找不到：`{self.cfg.command}`。"
                "请检查部署配置里的 command（镜像/宿主里是否真的装了这个程序）。",
                detail=str(exc),
            ) from exc
        except OSError as exc:
            raise McpError(
                f"MCP 连接 `{self.cfg.name}` 启动失败：{exc}", detail=str(exc)
            ) from exc
        # 排空 stderr（否则管道写满会让 server 阻塞）。只记 debug，不记内容
        # 到事件流——stderr 可能含路径/敏感信息。
        if self._proc.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._initialize()

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                logger.debug("MCP[%s] stderr: %s", self.cfg.name, line.decode("utf-8", "replace").rstrip())
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 排空失败不该影响主流程
            return

    async def close(self) -> None:
        proc = self._proc
        self._proc = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        if proc is None:
            return
        # 先关 stdin 让 server 自己退，等一会儿；不退再 terminate → kill。
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                proc.kill()
                # 收尸不留痕：进程已经被 kill，wait() 只会返回退出码；
                # 极少数情况下管道已断，`wait()` 会抛，但那时也确实没别的事可做。
                with contextlib.suppress(Exception):
                    await proc.wait()

    # ---- JSON-RPC 收发 ---------------------------------------------
    async def _send(self, method: str, params: dict[str, Any]) -> int:
        assert self._proc is not None and self._proc.stdin is not None
        self._next_id += 1
        msg_id = self._next_id
        payload = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()
        return msg_id

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def _read_response(self, msg_id: int, *, timeout: float) -> dict[str, Any]:
        """读到 id==msg_id 的那条响应为止（跳过无关的通知/别的响应）。"""
        assert self._proc is not None and self._proc.stdout is not None
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise McpError(
                    f"MCP 连接 `{self.cfg.name}` 在 {timeout:.0f} 秒内没有返回 `{msg_id}` 的响应。"
                    "server 可能卡住了。"
                )
            try:
                raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=remaining)
            except TimeoutError:
                raise McpError(
                    f"MCP 连接 `{self.cfg.name}` 读取超时（等待响应 id={msg_id}）。"
                ) from None
            if not raw:
                raise McpError(
                    f"MCP 连接 `{self.cfg.name}` 的 stdout 已关闭（server 退出了）。"
                )
            if len(raw) > MAX_MESSAGE_BYTES:
                raise McpError(
                    f"MCP 连接 `{self.cfg.name}` 返回的消息过大（>{MAX_MESSAGE_BYTES} 字节），已拒绝读取。"
                )
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # 有些 server 会在 stdout 里混入非 JSON 的日志行，跳过。
                logger.debug("MCP[%s] 跳过非 JSON 行：%s", self.cfg.name, line[:200])
                continue
            if not isinstance(msg, dict):
                continue
            # 通知（没有 id）跳过；别的 id 的响应也跳过（并发调用时用）。
            if "id" not in msg:
                continue
            if msg.get("id") != msg_id:
                continue
            if "error" in msg:
                err = msg["error"]
                message = err.get("message") if isinstance(err, dict) else str(err)
                raise McpError(
                    f"MCP 连接 `{self.cfg.name}` 报了错误：{message}",
                    detail=json.dumps(err, ensure_ascii=False),
                )
            result = msg.get("result")
            return result if isinstance(result, dict) else {"result": result}

    async def _request(self, method: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        msg_id = await self._send(method, params)
        return await self._read_response(msg_id, timeout=timeout)

    # ---- MCP 方法 ---------------------------------------------------
    async def _initialize(self) -> None:
        await self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "suvex-kernel", "version": "1.0"},
            },
            timeout=INIT_TIMEOUT,
        )
        # 协议要求初始化后再发一条 initialized 通知（无 id、无响应）。
        await self._send_notification("notifications/initialized")

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {}, timeout=DEFAULT_MCP_TIMEOUT)
        tools = result.get("tools")
        return tools if isinstance(tools, list) else []

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> Any:
        """调一个 MCP 工具。

        ★ v0.83：超时取**这条连接自己的** `call_timeout`，没配才回落到
        全局默认。见 `McpServerConfig.call_timeout` 的说明 —— 全局写死
        60 秒时，慢工具（模板批处理 102 秒 / 网段扫描 85 秒）都会必然超时，
        而报错长得像"连接坏了"。

        ⚠️ `tools/list` 仍然用全局默认：列工具清单本来就该是快的，
        给它配长超时只会掩盖"server 起不来"这个问题。
        """
        timeout = self.cfg.call_timeout or DEFAULT_MCP_TIMEOUT
        result = await self._request(
            "tools/call",
            {"name": tool, "arguments": arguments},
            timeout=timeout,
        )
        return result


# ── 注册表：一次解题持有的全部 MCP 连接配置 ──────────────────────────


class McpRegistry:
    """一次解题持有的 MCP 连接集合（按配置，非按连接）。

    它**不持有活的子进程**——每次 `list`/`describe`/`call` 都临时起一个
    `McpClient`（见模块 docstring 的"即连即断"）。所以它本身无常驻资源、
    不需要 aclose，可以安全地随 `spec` 存活。

    工具层只通过这个对象访问 MCP，不直接碰 `McpClient`。
    """

    def __init__(self, servers: tuple[McpServerConfig, ...] = ()) -> None:
        self._servers = servers
        self._by_name = {s.name: s for s in servers}
        # 缓存每个 server 的工具清单（一次解题内 server 的工具集不会变，
        # 缓存能省掉重复的 initialize+tools/list 往返）。
        self._tools_cache: dict[str, list[dict[str, Any]]] = {}

    def __bool__(self) -> bool:
        return bool(self._servers)

    def __len__(self) -> int:
        return len(self._servers)

    def names(self) -> list[str]:
        return [s.name for s in self._servers]

    def get(self, name: str) -> McpServerConfig | None:
        return self._by_name.get(name)

    def summaries(self) -> list[dict[str, Any]]:
        """`list_mcps` 用的摘要（不连 server，只报配置；工具数需先 describe）。"""
        out: list[dict[str, Any]] = []
        for s in self._servers:
            cached = self._tools_cache.get(s.name)
            out.append(
                {
                    "id": s.name,
                    "name": s.name,
                    "description": s.notes or "（未填写用途说明）",
                    "tool_count": len(cached) if cached is not None else None,
                }
            )
        return out

    async def fetch_tools(self, name: str) -> list[dict[str, Any]]:
        """取某个 server 的工具清单（带缓存）。连接失败转 McpError。"""
        cfg = self.get(name)
        if cfg is None:
            available = ", ".join(self.names()) or "（无）"
            raise McpError(f"没有名为 `{name}` 的 MCP 连接。可用的连接：{available}")
        if name in self._tools_cache:
            return self._tools_cache[name]
        async with McpClient(cfg) as client:
            tools = await client.list_tools()
        self._tools_cache[name] = tools
        return tools

    async def describe(self, name: str) -> str:
        """渲染某个 server 的工具清单（给 `describe_mcp` 工具用）。"""
        cfg = self.get(name)
        if cfg is None:
            available = ", ".join(self.names()) or "（无）"
            return f"没有名为 `{name}` 的 MCP 连接。可用的连接：{available}"
        tools = await self.fetch_tools(name)
        if not tools:
            return f"MCP 连接 `{name}` 没有提供任何工具。"
        header = f"MCP 连接 `{name}`（{cfg.notes or '无用途说明'}）提供 {len(tools)} 个工具："
        body = "\n".join(_format_tool(t) for t in tools)
        text = f"{header}\n{body}"
        if len(text) > MAX_DESCRIBE_CHARS:
            text = text[:MAX_DESCRIBE_CHARS] + f"\n\n……（已截断，原文 {len(text)} 字符）"
        return text

    async def call(self, name: str, tool: str, arguments: dict[str, Any]) -> str:
        """调一个工具（给 `call_mcp` 工具用），结果渲染成文本。"""
        cfg = self.get(name)
        if cfg is None:
            available = ", ".join(self.names()) or "（无）"
            return f"没有名为 `{name}` 的 MCP 连接。可用的连接：{available}"
        # 部署者白名单：只允许调这些工具名。
        if cfg.allowed_tools is not None and tool not in cfg.allowed_tools:
            allowed = ", ".join(cfg.allowed_tools) or "（无）"
            return (
                f"拒绝：连接 `{name}` 的配置不允许调用工具 `{tool}`。"
                f"允许的工具：{allowed}"
            )
        async with McpClient(cfg) as client:
            result = await client.call_tool(tool, arguments)
        return _render_call_result(result)


def _render_call_result(result: Any) -> str:
    """把 `tools/call` 的 MCP 结果渲染成一段文本给模型。

    MCP 的标准结果形状是 `{"content": [{"type": "text", "text": ...}, ...],
    "isError": bool}`。我们拼出所有 text 片段；非 text 片段（图片等）标注
    类型但不透明传（模型读不了二进制）。
    """
    if not isinstance(result, dict):
        return str(result)
    is_error = bool(result.get("isError"))
    content = result.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            itype = item.get("type")
            if itype == "text":
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(f"（{itype or '未知'} 类型内容，已略过）")
    elif content is not None:
        parts.append(str(content))
    text = "\n".join(p for p in parts if p) or "（工具没有返回文本内容）"
    if is_error:
        return f"MCP 工具返回错误：\n{text}"
    return text


def registry_from_spec(spec: Any) -> McpRegistry:
    """从 `SolveSpec` 上取（或惰性建）它的 MCP 注册表。

    ## 为什么缓存在 spec 上

    单循环与多智能体两条路径共用同一个 `spec` 对象（编排里 spec 是共享的），
    把注册表挂在它上面，两条路径的工具分发都能拿到**同一份**缓存——尤其
    多智能体时多个子 agent 反复 describe 同一个 server 不会每次都重启进程。
    """
    existing = getattr(spec, "mcp_registry", None)
    if isinstance(existing, McpRegistry):
        return existing
    servers = parse_server_configs(getattr(spec, "mcp_servers", ()))
    reg = McpRegistry(servers)
    # spec 若是 slots dataclass 且没声明该字段，赋值会失败。此时退回
    # "每次新建"（不缓存）——功能正确，只是多几次 tools/list 往返。
    with contextlib.suppress(AttributeError, TypeError):
        spec.mcp_registry = reg
    return reg


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "DEFAULT_MCP_TIMEOUT",
    "INIT_TIMEOUT",
    "MAX_MESSAGE_BYTES",
    "MAX_DESCRIBE_CHARS",
    "McpError",
    "McpServerConfig",
    "McpClient",
    "McpRegistry",
    "parse_server_configs",
    "registry_from_spec",
]
