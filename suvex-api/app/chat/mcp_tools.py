"""对话页的 MCP 工具桥 —— 把会话挂载的 server 变成模型能直接调的工具。

## 为什么需要这一层（而不是直接用 `kernel/mcp.py` 的 `call_mcp`）

扫描链路把 MCP 配置装进 `SolveSpec`，由 strix 的 `list_mcps` / `describe_mcp`
/ `call_mcp` 三个**通用工具**按需发现。对话页的形态不一样：

  · 用户是**按会话**挂载 server 的（`ChatSession.mcp_server_names`）；
  · 对话模型走的是原生 tool-calling，工具必须以**扁平的名字**出现在
    `tools=` 里，没有"先 describe 再 call"这一步；
  · 一轮对话里模型只看到这一轮挂载的工具，服务器连不上不能拖垮整轮。

所以这里做三件事，且只做这三件事：

  1. **发现**：按会话挂载名单，并行试连每个 server，`tools/list` 取工具；
  2. **命名**：暴露成 `mcp__<server>__<tool>`，同时保留
     `(server, tool)` 绑定 —— 执行时按暴露名查回真实工具名，
     不靠字符串反解析（server 名里可能有 `__`）；
  3. **降级**：一个 server 拉不起来只记一条状态（给界面看），
     其余 server 的工具照常可用。

## 与 `McpRegistry` 的关系

执行仍走 `McpRegistry.call`：它已经处理了白名单、未知工具时回真实清单、
`call_timeout`、以及"即连即断"的生命周期。这里不复制那些判断，只负责
**选出这一轮该暴露哪些工具**，以及在调用外面再套一层总超时 —— 连接阶段
（initialize）也吃时间，只靠 `call_timeout` 管不住一个握手就卡死的 server。

## 安全

server 的 `command` / `url` 由部署者在「工具」页配置，模型只能调本会话
**已挂载且发现成功**的工具。`env` / `headers` 只进子进程或请求头，
不进提示词、不进返回值。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from app.kernel import mcp as mcp_mod

logger = logging.getLogger(__name__)

#: 暴露给模型的工具名前缀。与内核的 `call_mcp` 区分开，也方便审批层
#: 一眼认出"这是动态 MCP 工具，不在 `CHAT_TOOLS` 里"。
MCP_TOOL_PREFIX = "mcp__"
#: 前缀与工具名之间的分隔符。
MCP_TOOL_SEP = "__"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


#: 并行试连的**单个** server 超时（秒）。
#:
#: 与 `mcp.INIT_TIMEOUT`（120 秒）不是一回事：那个是"给 npx 首次拉包"的
#: 宽容窗口，适合一次性试连；对话页每轮都要发现，不能为一条坏连接让用户
#: 干等两分钟。默认 10 秒，可用 `SUPEX_CHAT_MCP_DISCOVERY_TIMEOUT` 调整。
MCP_DISCOVERY_TIMEOUT = _env_float("SUPEX_CHAT_MCP_DISCOVERY_TIMEOUT", 10.0)

#: 调工具时在 server 自己的 `call_timeout` 之外额外给的连接余量（秒）。
#:
#: `McpClient.call_tool` 的超时只管 `tools/call` 这一次 RPC；而我们是
#: 即连即断，前面还有 spawn + initialize。不给余量的话，一个正常但启动慢的
#: server 会在握手阶段就被总超时砍掉，报错却长得像"工具太慢"。
MCP_CALL_CONNECT_GRACE = 15.0

_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_-]+")


class McpToolError(Exception):
    """MCP 工具调用的失败（带一句给人看的中文）。"""


def _sanitize_component(raw: str) -> str:
    """把 server / tool 名收拾成 OpenAI 工具名允许的字符集。

    原生 tool-calling 对工具名有字符限制（字母数字下划线横线），而 MCP
    server 的名字是部署者手填的，可能带点、空格、中文。不清理的话，
    有些网关会**静默丢掉**整个 tools 数组 —— 用户看到的是"模型不调工具"，
    而真正原因在名字里。
    """
    cleaned = _COMPONENT_RE.sub("_", raw.strip()).strip("_")
    return cleaned or "unnamed"


def is_mcp_tool_name(name: str) -> bool:
    """这个工具名是不是本模块暴露的动态 MCP 工具。"""
    return bool(name) and name.startswith(MCP_TOOL_PREFIX)


def server_from_tool_name(name: str, mounted: list[str] | tuple[str, ...]) -> str | None:
    """从暴露名里找回**挂载清单里**的 server 名。

    ## 为什么不能简单 `split("__")`

    server 名本身可能含 `__`（`my__server`），工具名也可能含 `__`。
    所以不在字符串上做无歧义的解析，而是拿**候选清单**去匹配前缀 ——
    候选来自会话设置，是有限且可信的集合。多个候选同时匹配时取最长的
    （`a` 与 `a__b` 都能解释 `mcp__a__b__x` 时，选 `a__b` 才是对的）。
    """
    if not is_mcp_tool_name(name):
        return None
    best: str | None = None
    for raw in mounted:
        server = str(raw or "").strip()
        if not server:
            continue
        # 暴露名里的 server 段经过 `_sanitize_component`（原生工具名字符集
        # 限制），所以带点/空格/中文的 server 名要按**清理后**的前缀匹配。
        prefixes = (
            f"{MCP_TOOL_PREFIX}{server}{MCP_TOOL_SEP}",
            f"{MCP_TOOL_PREFIX}{_sanitize_component(server)}{MCP_TOOL_SEP}",
        )
        if name.startswith(prefixes) and (best is None or len(server) > len(best)):
            best = server
    return best


@dataclass(frozen=True)
class McpServerStatus:
    """一台已挂载 server 这一轮的真实状态（给界面与提示词用）。"""

    name: str
    connected: bool
    tool_count: int = 0
    reason: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "connected": self.connected,
            "toolCount": self.tool_count,
            "reason": self.reason or None,
        }


@dataclass(frozen=True)
class McpToolBinding:
    """一个暴露给模型的工具：暴露名 → (server, 真实工具名, schema)。"""

    exposed_name: str
    server: str
    tool: str
    description: str
    input_schema: dict[str, Any]
    call_timeout: float | None = None


@dataclass
class McpToolCatalog:
    """本轮对话的 MCP 工具目录（发现结果 + 执行入口）。"""

    bindings: dict[str, McpToolBinding] = field(default_factory=dict)
    statuses: list[McpServerStatus] = field(default_factory=list)
    registry: mcp_mod.McpRegistry | None = None

    def __bool__(self) -> bool:
        return bool(self.bindings)

    def get(self, exposed_name: str) -> McpToolBinding | None:
        return self.bindings.get(exposed_name)

    def tool_schema(self) -> list[dict[str, Any]]:
        """OpenAI 风格的 `tools=` 片段（与 `build_tools_schema` 同形）。"""
        out: list[dict[str, Any]] = []
        for binding in self.bindings.values():
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": binding.exposed_name,
                        "description": binding.description,
                        "parameters": binding.input_schema,
                    },
                }
            )
        return out

    def prompt_section(self) -> str:
        """系统提示词里那段"MCP 工具清单"。

        ## 为什么原生 tool-calling 下还要写

        `tools=` 里已经有 schema，但系统提示词负责回答"这些工具为什么
        在这里、哪台 server 连不上"。后者模型在 schema 里看不到 ——
        不写的话，它会把某个工具"消失"当成自己记错了，转而重试一个
        不存在的名字。文本协议网关（不认 `tools=`）则完全依赖这一段
        才知道有哪些工具。
        """
        if not self.statuses:
            return ""
        lines = ["\n\n## 本对话挂载的 MCP 工具\n"]
        if self.bindings:
            lines.append(
                "下面是用户在这个对话里挂载的 MCP server 工具，名字带 "
                "`mcp__<server>__<tool>` 前缀，直接调用即可："
            )
            for binding in self.bindings.values():
                lines.append(f"- `{binding.exposed_name}`：{binding.description}")
        else:
            lines.append("本对话挂载的 MCP server 这一轮没有可用工具。")
        failed = [s for s in self.statuses if not s.connected]
        if failed:
            lines.append("")
            lines.append("以下挂载的 server 本轮连不上，**不要调用**它们的工具：")
            for status in failed:
                lines.append(f"- `{status.name}`：{status.reason or '连接失败'}")
        return "\n".join(lines) + "\n"

    async def call(self, binding: McpToolBinding, arguments: dict[str, Any]) -> str:
        """执行一个暴露工具，失败转成 `McpToolError`。

        ⚠️ 失败**只影响这一次调用**：调用方把它渲染成一条失败的工具结果，
          模型可以换路子继续，整轮对话不受影响。
        """
        if self.registry is None:
            raise McpToolError("这一轮没有可用的 MCP 连接。")
        timeout = (binding.call_timeout or mcp_mod.DEFAULT_MCP_TIMEOUT) + MCP_CALL_CONNECT_GRACE
        try:
            return await asyncio.wait_for(
                self.registry.call(binding.server, binding.tool, arguments),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise McpToolError(
                f"MCP 工具 `{binding.tool}`（连接 `{binding.server}`）"
                f"超过 {timeout:.0f} 秒没有返回，已放弃。"
            ) from exc
        except mcp_mod.McpError as exc:
            raise McpToolError(exc.message) from exc
        except Exception as exc:  # noqa: BLE001 - 一个坏 server 不该毁掉整轮
            logger.warning(
                "MCP 工具调用失败 server=%s tool=%s", binding.server, binding.tool, exc_info=True
            )
            raise McpToolError(
                f"MCP 工具 `{binding.tool}` 调用失败（{type(exc).__name__}）。"
            ) from exc

    def status_payload(self) -> dict[str, Any]:
        connected = sum(1 for status in self.statuses if status.connected)
        return {
            "mountedCount": len(self.statuses),
            "connectedCount": connected,
            "servers": [status.public_dict() for status in self.statuses],
        }


async def _discover_one(
    cfg: mcp_mod.McpServerConfig,
) -> tuple[McpServerStatus, list[dict[str, Any]]]:
    """试连一台 server 并取工具清单。任何失败都收成一条状态，不抛。"""

    async def _list() -> list[dict[str, Any]]:
        async with mcp_mod.McpClient(cfg) as client:
            return await client.list_tools()

    try:
        # ⚠️ 超时必须包住**整个** `async with`（含 spawn + initialize），
        #   不能只包 `list_tools()`：`McpClient.__aenter__` 的握手窗口是
        #   `INIT_TIMEOUT`（120 秒），只包后一步的话，一个连不上的 server
        #   仍能让整轮对话干等两分钟 —— 正是这条设计要防的情况。
        tools = await asyncio.wait_for(_list(), timeout=MCP_DISCOVERY_TIMEOUT)
    except TimeoutError:
        return (
            McpServerStatus(
                name=cfg.name,
                connected=False,
                reason=f"试连超时（>{MCP_DISCOVERY_TIMEOUT:.0f} 秒）",
            ),
            [],
        )
    except mcp_mod.McpError as exc:
        return McpServerStatus(name=cfg.name, connected=False, reason=exc.message), []
    except Exception as exc:  # noqa: BLE001 - 试连就是把异常变成一句话
        logger.warning("MCP 试连失败 server=%s", cfg.name, exc_info=True)
        return (
            McpServerStatus(
                name=cfg.name,
                connected=False,
                reason=f"{type(exc).__name__}: {exc}",
            ),
            [],
        )
    return McpServerStatus(name=cfg.name, connected=True, tool_count=len(tools)), tools


def _normalize_schema(raw: Any) -> dict[str, Any]:
    """把 MCP 的 `inputSchema` 收拾成 providers 能接受的 JSON Schema。"""
    if not isinstance(raw, dict):
        return {"type": "object", "properties": {}}
    schema = dict(raw)
    if schema.get("type") != "object":
        schema["type"] = "object"
    if not isinstance(schema.get("properties"), dict):
        schema["properties"] = {}
    return schema


def _exposed_name(server: str, tool: str, used: set[str]) -> str:
    """生成一个在 providers 里合法的暴露名；重名时追加序号。"""
    base = f"{MCP_TOOL_PREFIX}{_sanitize_component(server)}{MCP_TOOL_SEP}{_sanitize_component(tool)}"
    name = base
    index = 2
    while name in used:
        name = f"{base}{MCP_TOOL_SEP}{index}"
        index += 1
    used.add(name)
    return name


async def discover_mounted(db: Any, session: Any) -> McpToolCatalog:
    """按 `session.mcp_server_names` 发现本轮可用的 MCP 工具。

    ## 为什么并行、且每台单独限时

    一台 `npx` server 冷启动要几秒，五台串行就是几十秒 —— 而用户只是在
    对话页打了一句话。并行之后总耗时只取决于**最慢的那台**；再给每台
    一个独立的 `MCP_DISCOVERY_TIMEOUT`，坏的那台到点就走，不拖住其余。

    ## 为什么失败也要留在 `statuses` 里

    界面上要能说"已挂载 2 台 / 1 台试连失败（原因）"。只静默丢掉失败项
    的话，用户勾了却看不到工具，会以为功能坏了，没有任何线索。
    """
    mounted = [str(name).strip() for name in (session.mcp_server_names or []) if str(name).strip()]
    catalog = McpToolCatalog()
    if not mounted:
        return catalog

    # 延迟导入：`kernel.service` 会拉起大片业务模块，而本模块被 engine /
    # router 在导入期引用，放顶层容易把紧耦合变成循环依赖。
    try:
        from app.kernel import service as kernel_service

        raw_servers = kernel_service.get_mcp_servers(db)
    except Exception:  # noqa: BLE001 - 读配置失败不该让整轮对话起不来
        logger.warning("读取 MCP 配置失败，本轮按未挂载处理", exc_info=True)
        raw_servers = []

    configs = {cfg.name: cfg for cfg in mcp_mod.parse_server_configs(raw_servers)}

    todo: list[mcp_mod.McpServerConfig] = []
    for name in mounted:
        cfg = configs.get(name)
        if cfg is None:
            catalog.statuses.append(
                McpServerStatus(
                    name=name,
                    connected=False,
                    reason="平台里没有这台 MCP server（可能已被删除，去「工具」页确认）",
                )
            )
            continue
        todo.append(cfg)

    results = await asyncio.gather(
        *(_discover_one(cfg) for cfg in todo), return_exceptions=True
    )

    used: set[str] = set()
    for cfg, result in zip(todo, results, strict=True):
        if isinstance(result, BaseException):
            # `_discover_one` 自己已经兜住了所有异常；走到这里说明是
            # 更意外的编程错误。仍然只降级这一台，不取消其余。
            logger.warning("MCP 发现阶段意外失败 server=%s", cfg.name, exc_info=result)
            catalog.statuses.append(
                McpServerStatus(
                    name=cfg.name,
                    connected=False,
                    reason=f"{type(result).__name__}: {result}",
                )
            )
            continue
        status, tools = result
        catalog.statuses.append(status)
        if not status.connected:
            continue
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_name = str(tool.get("name") or "").strip()
            if not tool_name:
                continue
            # 部署者白名单与服务端 `McpRegistry.call` 用**同一个**字段：
            # 这里提前挡掉，模型就不会看到一个调了必被拒的工具。
            if cfg.allowed_tools is not None and tool_name not in cfg.allowed_tools:
                continue
            description = str(tool.get("description") or "").strip() or "（无描述）"
            schema = _normalize_schema(tool.get("inputSchema") or tool.get("input_schema"))
            exposed = _exposed_name(cfg.name, tool_name, used)
            catalog.bindings[exposed] = McpToolBinding(
                exposed_name=exposed,
                server=cfg.name,
                tool=tool_name,
                description=f"「{cfg.name}」的 MCP 工具：{description}",
                input_schema=schema,
                call_timeout=cfg.call_timeout,
            )

    catalog.registry = mcp_mod.McpRegistry(tuple(todo))
    return catalog
