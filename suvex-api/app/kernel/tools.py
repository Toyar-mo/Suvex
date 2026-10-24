"""内置工具清单与启用开关（v0.82）。

## 这个模块补的是什么

`runner.TOOL_NAMES` 是一份**代码里的元组**：12 个工具名，硬编码，没有
描述、没有分组、没有任何 HTTP 端点能读到它。于是部署者**看不见**智能体
到底能调什么 —— 界面上一片空白，只能靠读源码。

所以这里把清单变成**可读、可开关、可对账**的一等数据。

## ⚠️ 为什么这里又抄了一遍工具名

`TOOL_NAMES` 在 `runner.py`，而 `runner.py` import 成本极高（sandbox /
docker / drivers / LLM 网关），`router` 层每个请求都会经过 service ——
顶层反向 import 会让每个 HTTP 请求都付这份代价。

所以照 `kernel/service.py` 里同类常量的先例办：**抄一份，再用对账测试钉住**
（`tests/kernel/test_tools_catalog.py::test_catalog_matches_runner_tool_names`
断言 `set(TOOL_SPECS 的 name) == set(runner.TOOL_NAMES)` 且长度相等）。
抄漏一个会红，多抄一个也会红。

## ⚠️ 这个开关**不是**安全边界

它回答的是"这一次任务我不想让模型用某几样东西"，不是"防止越权"。
真正的隔离在沙箱容器、`hostguard`、URL 白名单里。写错它的后果是
**能力变差**，不是**越权** —— 把这个区别写进注释，是因为"开关"
这个词很容易让人以为它是一个安全机制，而那个误判会让人以为
"全开就安全"或"关掉就是加固"。

## 为什么存"关掉哪些"（`disabled_tools`）

见 `settings/models.py::KernelConfig.disabled_tools`：这样新增工具
**默认可用**，不需要有人记得来改配置。反过来（存"打开哪些"）会让
每个新工具静默不可用，而那个症状与"模型不会用新工具"无法区分。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════
# 清单
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ToolSpec:
    """一个内置工具的可展示元数据。

    ## 为什么 `label` 是中文而 `name` 不是

    `name` 是**标识符**：模型用它（`ACTION: {"tool": "run_shell"}`），
    界面显示它时也用等宽字体原样显示 —— 改了模型就不认。`label` 是
    **给人看的**，随界面语言走。这与 `navigation.ts` 里"路径保持英文、
    文案用中文"是同一条分工。

    ## 为什么 `requires_connection` 是名字而不是布尔

    MCP 类工具不是"缺一个开关"，而是**缺一条具名连接**。写名字让界面
    能直接说"它需要名为 X 的连接"，而不是笼统的"未就绪"。

    ⚠️ 当前所有内置工具都不需要外部连接（`requires_connection` 全为空串）。
    这个字段保留是为了将来接外部工具时不用改数据结构 —— 判据在
    `service.tools_catalog` 里按名字逐条查，不写死。
    """

    #: 工具名 = 模型在 ACTION 里写的那个字符串。**不要改**，改了模型不认。
    name: str
    #: 界面显示名（中文）。
    label: str
    #: 分组，用于界面分栏。取值见 `TOOL_GROUPS`。
    group: str
    #: 一句话说明它能干什么（中文）。界面与 API 共用这一句。
    summary: str
    #: 参数摘要（给人看的，不是 schema）。模型侧的权威说明在提示词里。
    params: str = ""
    #: 依赖的 MCP 连接名。非空表示"配了那条连接才真的能用"。
    requires_connection: str = ""
    #: 关掉它会明显废掉主力能力（界面要提示这不是一个随手可关的开关）。
    core: bool = False


#: 分组名。顺序即界面上的显示顺序 —— **从"最常用"到"最专用"**，
#: 而不是按字母序：界面上第一栏应该是用户最想确认的那几个。
TOOL_GROUPS: Final[tuple[str, ...]] = (
    "沙箱执行",
    "网络请求",
    "记录与历史",
    "技能与知识",
    "外部工具",
)


TOOL_SPECS: Final[tuple[ToolSpec, ...]] = (
    # ── 沙箱执行 ──────────────────────────────────────────
    ToolSpec(
        name="run_shell",
        label="执行命令",
        group="沙箱执行",
        summary="在沙箱容器里执行一条 shell 命令。智能体的主力工具。",
        params="command（必填）、timeout_seconds（可选）",
        # ⚠️ core=True 不是"它更重要"这种价值判断，而是一个**事实**：
        #    关掉它之后 `kernel_pentest.md` 里绝大多数手法都做不了
        #    （nmap / ffuf / curl 全靠它）。界面据此给出"这会让本次任务
        #    基本失效"的提示，而不是让它和 `list_requests` 看起来同级。
        core=True,
    ),
    ToolSpec(
        name="read_attachment",
        label="读附件",
        group="沙箱执行",
        summary="读取随任务提交的附件内容（宿主侧读已下载的副本）。",
        params="path（附件文件名）、offset、limit（可选）",
    ),
    # ── 网络请求 ──────────────────────────────────────────
    ToolSpec(
        name="http_request",
        label="发 HTTP 请求",
        group="网络请求",
        summary="从宿主发一次 HTTP 请求，受目标白名单限制，自动记入请求历史。",
        params="url（必填）、method、headers、body（可选）",
    ),
    ToolSpec(
        name="list_requests",
        label="列出请求历史",
        group="记录与历史",
        summary="列出本次任务已发过的请求（编号 / 方法 / 地址 / 状态码）。",
        params="limit（可选，默认 50）",
    ),
    ToolSpec(
        name="get_request",
        label="看单条请求",
        group="记录与历史",
        summary="看某条请求的完整请求头、请求体与响应体。",
        params="record_id（必填，形如 req-3）",
    ),
    ToolSpec(
        name="repeat_request",
        label="重放请求",
        group="记录与历史",
        summary="重放一条已发过的请求，可改参数后再打一次。",
        params="record_id（必填）、method、url、headers、body（可选）",
    ),
    # ── 技能与知识 ────────────────────────────────────────
    ToolSpec(
        name="load_skill",
        label="载入技能速查",
        group="技能与知识",
        summary="按需拉取一篇漏洞验证手法或工具打法的速查卡。",
        params="name（技能包名）",
    ),
    # ── 外部工具（MCP 桥）─────────────────────────────────
    ToolSpec(
        name="list_mcps",
        label="列出外部连接",
        group="外部工具",
        summary="列出本次可用的 MCP 连接（外部工具服务）。",
    ),
    ToolSpec(
        name="describe_mcp",
        label="看外部连接详情",
        group="外部工具",
        summary="看某个 MCP 连接提供哪些工具，各带输入 schema。",
        params="connection（必填）",
    ),
    ToolSpec(
        name="call_mcp",
        label="调用外部工具",
        group="外部工具",
        summary="调用一个 MCP 工具。需要先知道连接名与工具名。",
        params="connection、tool（必填）、arguments（JSON 对象，可省略）",
    ),
)


#: 名字 → spec。**构建期**就建好，查找是 O(1) 且顺序稳定。
TOOL_BY_NAME: Final[dict[str, ToolSpec]] = {spec.name: spec for spec in TOOL_SPECS}


# ══════════════════════════════════════════════════════════
# 开关解析
# ══════════════════════════════════════════════════════════


def normalize_disabled(raw: object) -> tuple[str, ...]:
    """把配置列里读出来的东西规整成"已关掉的工具名"元组。

    ## 为什么要容错而不是抛

    `disabled_tools` 是 JSON 列，可能被手工改过、可能是老版本写的
    （那时还没有某个工具名）。**读取侧必须容错**：一行坏配置不该让
    所有任务都跑不起来 —— 最坏情况退化成"按默认值处理"（= 全开），
    那正是引入这个开关之前的行为。

    ⚠️ 未知工具名**不报错、也不丢弃警告**：它多半是"这个工具被删了"
    或者"配置是手改错的"。跳过它并记 warning，因为删掉一个工具时
    不该让旧的配置行变成非法数据（见迁移 `a9d4f2c71e83` 的说明）。
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        logger.warning(
            "disabled_tools 不是列表，按「一件都没关」处理：%r", type(raw).__name__
        )
        return ()

    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            logger.warning("disabled_tools 里有非字符串条目，跳过：%r", type(item).__name__)
            continue
        name = item.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        if name not in TOOL_BY_NAME:
            # 不警告到刷屏：一次任务只解析一次清单，所以最多一条。
            logger.warning("disabled_tools 里有未知工具名 %r，已跳过（它可能已被移除）", name)
            continue
        out.append(name)
    return tuple(out)


def enabled_tool_names(disabled: object) -> tuple[str, ...]:
    """按开关算出**本次实际可用**的工具名。

    ⚠️ 返回值保持 `TOOL_SPECS` 的声明顺序，而不是"关掉的挪到后面"——
    提示词里那句「可用工具只有 X / Y / Z」的顺序跟着它走，
    顺序稳定才让历史日志之间可比。
    """
    off = set(normalize_disabled(disabled))
    return tuple(spec.name for spec in TOOL_SPECS if spec.name not in off)


def effective_disabled(disabled: object) -> tuple[str, ...]:
    """规整后的"已关掉"清单（只含认识的名字、去重、保序）。"""
    return normalize_disabled(disabled)


# ══════════════════════════════════════════════════════════
# 对账断言（供测试与运行期自检使用）
# ══════════════════════════════════════════════════════════


def assert_catalog_consistent(tool_names: tuple[str, ...]) -> None:
    """断言本模块的清单与引擎里的 `TOOL_NAMES` 是同一集合。

    ★ **故意做成可调用的函数而不是只在测试里写一遍**：`runner.TOOL_NAMES`
    在模块导入时就确定了，所以这条对账既能在测试里跑（有人改了清单会红），
    也能在部署后由运维脚本调一次（确认装上去的版本没有错配）。
    只写在测试里的话，`suvex-api` 与 `suvex-web` 分开发布时的那次
    不一致就没人发现。
    """
    catalog = tuple(spec.name for spec in TOOL_SPECS)
    if set(catalog) != set(tool_names) or len(catalog) != len(tool_names):
        missing = sorted(set(tool_names) - set(catalog))
        extra = sorted(set(catalog) - set(tool_names))
        raise AssertionError(
            "工具清单与引擎的 TOOL_NAMES 不一致："
            f"清单里缺 {missing}，清单里多 {extra}。"
            "两处必须逐字一致（见本模块文件头「为什么这里又抄了一遍」）。"
        )


__all__ = [
    "TOOL_BY_NAME",
    "TOOL_GROUPS",
    "TOOL_SPECS",
    "ToolSpec",
    "assert_catalog_consistent",
    "effective_disabled",
    "enabled_tool_names",
    "normalize_disabled",
]
