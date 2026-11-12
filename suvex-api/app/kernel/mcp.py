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

## 传输：stdio + streamable-HTTP（v1.62 起）

MCP 有两种传输：**stdio**（子进程，按行分隔的 JSON-RPC）与
**streamable-HTTP**（POST JSON-RPC 到一个 URL，响应可以是 application/json
或 SSE）。两个都支持：

  - **stdio**：`command` + `args` + `env` 拉起子进程。覆盖绝大多数本地
    server（`npx @modelcontextprotocol/server-xxx`、`python -m xxx_mcp`）。
  - **streamable-HTTP**：`type="http"` + `url` + `headers`。覆盖托管型
    server（如 `https://ctf2.dasctf.com/api/ai/v1/mcp`）。`headers` 语义与
    `env` 同级：**可含凭据、只进请求、不回显、不记日志**。

⚠️ v1.61 及之前**只做 stdio**，于是 http 条目会被当坏条目跳过 / 被模型引去
`npx` spawn 进程、连不上远端 URL 报「stdout 已关闭（server 退出了）」——
一个 grep 不到的假象。v1.62 补上 http。

关于 cancel scope（strix `session.py` 那条血泪警告）：它针对的是"长连接 +
独立 supervising task"的架构。我们这里两种传输都走**即用即连即断**（见下面
「生命周期」），每次 list/call 现开、用完关，全程在调用方那条任务里，没有
跨任务持有的 transport，也就没有 cancel scope 串台的可能。

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
from collections.abc import AsyncIterator, Awaitable, Callable
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
    #: stdio 连接**必填**；http 连接**不用**（留空即可）。
    command: str = ""
    #: 传给 `command` 的参数（如 `["-y", "@modelcontextprotocol/server-filesystem", "/data"]`）。
    args: list[str] = field(default_factory=list)
    #: 追加进子进程的环境变量（可含 token；**不落盘、不记日志**）。
    env: dict[str, str] = field(default_factory=dict)
    #: ★ v1.62：传输类型。`"stdio"`（默认，command 拉起子进程）或
    #: `"http"`（streamable-HTTP，直接打一个远端 URL）。
    #:
    #: ## 为什么加它（用户实测踩到的）
    #:
    #: 用户贴了一条 `{"type":"http","url":"https://.../mcp"}` 的配置，
    #: 老版本只认 command，模型被引去 `npx` spawn 进程、连不上远端 URL，
    #: 报「stdout 已关闭（server 退出了）」—— 一个 grep 不到的假象：
    #: 问题不在 stdio，在于**这条连接根本不该走 stdio**。
    type: str = "stdio"
    #: ★ v1.62：http 连接的地址（如 `https://ctf2.dasctf.com/api/ai/v1/mcp`）。
    #: stdio 连接不用（留空）。
    url: str = ""
    #: ★ v1.62：http 连接附加的请求头（如 `{"Authorization": "Bearer xxx"}`）。
    #: 语义与 `env` 同级：**可含凭据、只进请求、不回显、不记日志**。
    headers: dict[str, str] = field(default_factory=dict)
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

    def is_http(self) -> bool:
        """这条连接是否走 streamable-HTTP。（大小写不敏感，容错手填。）"""
        return self.type.strip().lower() == "http"

    def public_dict(self) -> dict[str, Any]:
        """给日志/回执用的安全视图：**去掉 env / headers**（可能含凭据）。"""
        return {
            "name": self.name,
            "type": self.type,
            "command": self.command,
            "args": list(self.args),
            # http 连接的 url 不是凭据（部署者本来就要能看到它连去哪），回显；
            # 但 headers 可能含 token —— 与 env 同级，**不回显**。
            "url": self.url,
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
        # ★ v1.62：type 决定这条连接走 stdio 还是 http。缺省按 stdio（向后兼容：
        # 历史配置里的条目本来就只有 command，没有 type）。
        conn_type = str(item.get("type") or "stdio").strip().lower() or "stdio"
        url = str(item.get("url") or "").strip()
        # http 条目只要 name+url（**不需要** command）；stdio 条目要 name+command。
        # 这是老版本把 http 条目当坏条目跳过的根因：它只认 command。
        needs = "name" if (conn_type == "http" and url) else "name/command"
        if not name or (conn_type == "http" and not url) or (conn_type != "http" and not command):
            logger.warning(
                "mcp_servers[%d] 缺 %s（type=%s），跳过", index, needs, conn_type
            )
            continue
        if name in seen:
            logger.warning("mcp_servers[%d] 连接名重复（%s），跳过后者", index, name)
            continue
        seen.add(name)
        args = item.get("args")
        args = [str(a) for a in args] if isinstance(args, list) else []
        env = item.get("env")
        env = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
        headers = item.get("headers")
        headers = (
            {str(k): str(v) for k, v in headers.items()}
            if isinstance(headers, dict)
            else {}
        )
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
                type=conn_type,
                command=command,
                args=args,
                env=env,
                url=url,
                headers=headers,
                notes=str(item.get("notes") or ""),
                allowed_tools=allowed,
                call_timeout=call_timeout,
            )
        )
    return tuple(out)


def _safe_excerpt(resp: Any, limit: int = 300) -> str:
    """把 HTTP 响应体截一小段做成可读摘要（避免把整个 body 塞进错误信息）。"""
    try:
        text = resp.text or ""
    except Exception:  # noqa: BLE001 - 取不到就当空
        return ""
    text = text.strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _safe_excerpt_text(text: str, limit: int = 300) -> str:
    """与 `_safe_excerpt` 同口径，但接受已读出的文本（流式路径用）。"""
    text = (text or "").strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _json_messages(raw: str) -> list[dict[str, Any]]:
    """把一段 SSE data 解析成 JSON-RPC 消息列表（单条/批量/坏 JSON）。"""
    data = (raw or "").strip()
    if not data or data == "[DONE]":
        return []
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return []
    if isinstance(obj, dict):
        return [obj]
    if isinstance(obj, list):
        return [item for item in obj if isinstance(item, dict)]
    return []


def _parse_sse_messages(body: str) -> list[dict[str, Any]]:
    """从 SSE（text/event-stream）body 里抽出所有 JSON-RPC 消息对象。

    streamable-HTTP 用 SSE 时，每条消息是一行 `data: {...}`（事件之间用空行
    分隔）。我们只关心 `data:` 行里的 JSON；`event:`/`id:`/`retry:` 行以及
    注释（`:` 开头）一律忽略。一条也解析不出来时报错——那说明 server 回的
    不是我们认识的 streamable-HTTP。
    """
    messages: list[dict[str, Any]] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            messages.append(obj)
        elif isinstance(obj, list):
            messages.extend(o for o in obj if isinstance(o, dict))
    return messages


def _format_tool(tool: dict[str, Any]) -> str:
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

    def __init__(self, cfg: McpServerConfig, *, http_client: Any = None) -> None:
        self.cfg = cfg
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._stderr_task: asyncio.Task[None] | None = None
        # ★ v1.62：HTTP 传输相关。
        # `_http` 是 httpx.AsyncClient；`_session_id` 是 server 在 initialize
        # 响应头 `Mcp-Session-Id` 里回推的会话标识（streamable-HTTP 规范要求
        # 后续每个请求都带上它）；`_http_client_factory` 允许测试注入
        # MockTransport（与 `client.py` 的 `client=` 注入同一套纪律）。
        self._http: Any = None
        self._http_client_factory = http_client
        self._session_id: str | None = None
        self._protocol_version: str | None = None

    # ---- 生命周期 ---------------------------------------------------
    async def __aenter__(self) -> McpClient:
        await self._start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _start(self) -> None:
        # ★ v1.62：http 传输不 spawn 子进程，直接建 HTTP 客户端并握手。
        if self.cfg.is_http():
            await self._start_http()
            return
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
        # ★ v1.62：http 传输的关闭是关 httpx 客户端。
        if self.cfg.is_http():
            await self._close_http()
            return
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

    # ---- HTTP（streamable-HTTP）传输 -------------------------------
    #
    # ★ v1.62：MCP 的 streamable-HTTP 传输。与 stdio 的关键差异：
    #   · 不用子进程，直接 POST JSON-RPC 到 `cfg.url`；
    #   · 服务端可以回 `application/json`（一次性响应）或
    #     `text/event-stream`（SSE，data: 行里塞 JSON-RPC 消息）；
    #   · initialize 响应头里可能带 `Mcp-Session-Id`，后续请求要原样带回；
    #   · 通知（notifications/initialized）走 POST 且服务端回 202 无 body。
    #
    # ## 关于 cancel scope（模块 docstring 里那条血泪警告）
    #
    # 那条警告针对的是"**长连接 + 独立 supervising task**"的架构（strix 的
    # session.py）。我们这里是**即用即连即断**：每次 list/call 现开一个
    # httpx.AsyncClient、发一两个请求、关掉，全程就在调用方那条任务里跑，
    # 没有跨任务持有的 transport、也就没有 cancel scope 串台的可能。
    # 这正是模块生命周期设计（见 docstring「每次调用即连即断」）顺带规避掉的风险。

    async def _start_http(self) -> None:
        if not self.cfg.url:
            raise McpError(
                f"MCP 连接 `{self.cfg.name}` 是 http 类型，但没有填 url。"
            )
        import httpx

        # 关掉 env 代理（与 attachments.py 同一条纪律：代理会把内网地址也劫走）。
        # trust_env=False 让配置里的 headers 是唯一的凭据来源，可预测。
        self._http = (
            self._http_client_factory
            if self._http_client_factory is not None
            else httpx.AsyncClient(
                timeout=httpx.Timeout(INIT_TIMEOUT),
                trust_env=False,
                follow_redirects=True,
            )
        )
        await self._initialize()

    async def _close_http(self) -> None:
        http = self._http
        self._http = None
        if http is None or self._http_client_factory is not None:
            # 注入的 client 由调用方（测试）负责关，我们不越权关它。
            return
        with contextlib.suppress(Exception):
            await http.aclose()

    def _http_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        # 会话标识建起来之后，每个请求都要带回（streamable-HTTP 规范）。
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        # 配置里的 headers 最后叠加（可含 Authorization 等；覆盖同名默认头）。
        headers.update(self.cfg.headers)
        return headers

    async def _http_post_messages(
        self, payload: dict[str, Any], *, timeout: float
    ) -> AsyncIterator[dict[str, Any]]:
        """POST 一条 JSON-RPC 消息，**逐条**产出响应里的 JSON-RPC 消息。

        与 `_http_post` 的区别：这个在 SSE 响应上边读边产，于是
        `notifications/progress` 能在请求还没结束时就送到界面；
        `_http_post` 只是把它收成 list，行为与改造前一致。

        分帧规则与 stdio 相同：空行结束一帧，`data:` 多行拼接；
        非 JSON 的注释/日志行跳过。
        """
        assert self._http is not None

        async with self._http.stream(
            "POST",
            self.cfg.url,
            json=payload,
            headers=self._http_headers(),
            timeout=timeout,
        ) as resp:
            sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
            if sid:
                self._session_id = sid
            pver = resp.headers.get("MCP-Protocol-Version") or resp.headers.get(
                "mcp-protocol-version"
            )
            if pver:
                self._protocol_version = pver

            if resp.status_code == 202:
                return
            if resp.status_code >= 400:
                body = await resp.aread()
                raise McpError(
                    f"MCP 连接 `{self.cfg.name}` 返回 HTTP {resp.status_code}："
                    f"{_safe_excerpt_text(body.decode('utf-8', 'replace'))}",
                )
            ctype = (resp.headers.get("content-type") or "").lower()
            if "text/event-stream" in ctype:
                data_lines: list[str] = []
                async for line in resp.aiter_lines():
                    if line == "":
                        if not data_lines:
                            continue
                        raw = "\n".join(data_lines)
                        data_lines = []
                        parsed = _json_messages(raw)
                        for message in parsed:
                            yield message
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif line.startswith(":"):
                        continue
                if data_lines:
                    for message in _json_messages("\n".join(data_lines)):
                        yield message
                return
            body = await resp.aread()
            if not body:
                return
            try:
                data = json.loads(body.decode("utf-8", "replace"))
            except (ValueError, json.JSONDecodeError) as exc:
                raise McpError(
                    f"MCP 连接 `{self.cfg.name}` 返回的不是合法 JSON："
                    f"{_safe_excerpt_text(body.decode('utf-8', 'replace'))}",
                    detail=str(exc),
                ) from exc
            if isinstance(data, list):
                for message in data:
                    if isinstance(message, dict):
                        yield message
            elif isinstance(data, dict):
                yield data

    async def _http_post(
        self, payload: dict[str, Any], *, timeout: float
    ) -> list[dict[str, Any]]:
        """POST 并收集全部消息（兼容既有调用方）。"""
        return [message async for message in self._http_post_messages(payload, timeout=timeout)]

    async def _http_request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        progress_token: str | None = None,
    ) -> dict[str, Any]:
        """HTTP 上发一条**请求**（有 id）并取回对应的 result。"""
        self._next_id += 1
        msg_id = self._next_id
        payload = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
        if on_progress is not None:
            async for msg in self._http_post_messages(payload, timeout=timeout):
                if msg.get("id") != msg_id:
                    if (
                        msg.get("method") == "notifications/progress"
                        and isinstance(msg.get("params"), dict)
                        and progress_token is not None
                        and str(msg["params"].get("progressToken")) == progress_token
                    ):
                        await on_progress(msg["params"])
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
            raise McpError(
                f"MCP 连接 `{self.cfg.name}` 的响应里没有 id={msg_id} 的结果。"
                "server 可能没有按 JSON-RPC 回复。"
            )
        messages = await self._http_post(payload, timeout=timeout)
        for msg in messages:
            if msg.get("id") != msg_id:
                continue  # 跳过通知 / 别的 id
            if "error" in msg:
                err = msg["error"]
                message = err.get("message") if isinstance(err, dict) else str(err)
                raise McpError(
                    f"MCP 连接 `{self.cfg.name}` 报了错误：{message}",
                    detail=json.dumps(err, ensure_ascii=False),
                )
            result = msg.get("result")
            return result if isinstance(result, dict) else {"result": result}
        raise McpError(
            f"MCP 连接 `{self.cfg.name}` 的响应里没有 id={msg_id} 的结果。"
            "server 可能没有按 JSON-RPC 回复。"
        )

    async def _http_notify(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        """HTTP 上发一条**通知**（无 id，服务端回 202）。"""
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._http_post(payload, timeout=INIT_TIMEOUT)

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

    async def _read_response(
        self,
        msg_id: int,
        *,
        timeout: float,
        on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        progress_token: str | None = None,
    ) -> dict[str, Any]:
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
            # 通知（没有 id）：只有带匹配 progressToken 的
            # `notifications/progress` 要分发给界面，其余照旧跳过。
            if "id" not in msg:
                if (
                    on_progress is not None
                    and msg.get("method") == "notifications/progress"
                ):
                    params = msg.get("params")
                    if (
                        isinstance(params, dict)
                        and progress_token is not None
                        and str(params.get("progressToken")) == progress_token
                    ):
                        await on_progress(params)
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

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        # ★ v1.62：http 传输走独立的 POST 路径（stdio 的 send/read 是管道行协议）。
        token: str | None = None
        if on_progress is not None:
            # `_meta.progressToken` 由客户端选。用下一个 id 派生，保证
            # 与本请求一一对应；server 回 `notifications/progress` 时必须
            # 带同一个 token，否则是别的调用的进度，忽略。
            token = f"suvex-progress-{self._next_id + 1}"
            meta = dict(params.get("_meta") or {})
            meta["progressToken"] = token
            params = {**params, "_meta": meta}
        if self.cfg.is_http():
            return await self._http_request(
                method, params, timeout=timeout, on_progress=on_progress, progress_token=token
            )
        msg_id = await self._send(method, params)
        return await self._read_response(
            msg_id,
            timeout=timeout,
            on_progress=on_progress,
            progress_token=token,
        )

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
        if self.cfg.is_http():
            await self._http_notify("notifications/initialized")
        else:
            await self._send_notification("notifications/initialized")

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {}, timeout=DEFAULT_MCP_TIMEOUT)
        tools = result.get("tools")
        return tools if isinstance(tools, list) else []

    async def call_tool(
        self,
        tool: str,
        arguments: dict[str, Any],
        on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> Any:
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
            on_progress=on_progress,
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
                    "type": s.type,
                    # http 连接回显 url（非凭据），stdio 回显 command —— 帮模型
                    # 判断这条连接是什么（连去哪 / 拉起什么）。
                    "endpoint": s.url if s.is_http() else s.command,
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

    async def call(
        self,
        name: str,
        tool: str,
        arguments: dict[str, Any],
        on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> str:
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
            try:
                result = await client.call_tool(tool, arguments, on_progress=on_progress)
            except McpError as exc:
                hint = await _unknown_tool_hint(client, cfg, tool, exc)
                if hint is not None:
                    return hint
                raise
        return _render_call_result(result)


async def _unknown_tool_hint(
    client: McpClient, cfg: McpServerConfig, tool: str, exc: Exception
) -> str | None:
    """工具名不存在时，把**真实清单**回给模型。

    ## 为什么必须有它（2026-11-08 实测）

    4 道题跑了 25 分钟、沙箱里一个文件都没有：模型反复调
    `ctf2_list_practice_ground_challenges` 这类**这个连接根本没有的工具**，
    每次只收到一句 `unknown tool "…"` —— 它没有任何依据去猜对的
    名字，于是同一个错误重复到轮次烧完。

    `tools/list` 本来就在协议里，代价一次往返。相比"再烧 20 轮"，
    这是最便宜的一次降级。

    ⚠️ 只在**明确是未知工具**时触发：连接超时/服务挂了也去列清单，
      只会把一次网络错误拖成两次超时。
    """
    text = str(exc).lower()
    if "unknown tool" not in text and "method not found" not in text:
        return None
    try:
        tools = await client.list_tools()
    except Exception:  # noqa: BLE001 - 列清单失败就退回原始错误
        return None
    names = [str(t.get("name")) for t in tools if isinstance(t, dict) and t.get("name")]
    if not names:
        return None
    return (
        f"连接 `{cfg.name}` 没有工具 `{tool}`。它实际提供：{', '.join(names)}。"
        f"下次从这份清单里选；参数不确定时先 describe_mcp。"
    )


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
