"""对话页的系统提示词与工具清单（v0.93）。

## 为什么单独一个虚拟文件

`models.py` / `schemas.py` / `service.py` 这三层都不需要知道"模型能调什么"
—— 那是 `engine.py` 的事。但工具清单同时要给两处用：

1. `engine.py` 拼系统提示词时要把清单写进去（模型得知道有什么工具）；
2. 未来的 `GET /chat/tools` 端点、前端的"这个对话能做什么"提示条，
   也要读同一份清单。

放在 `engine.py` 里会让第 2 类消费者为了读一个常量去 import 一个
带了 httpx / LLM 网关 / 沙箱的模块。这与 `app/kernel/tools.py` 里
「为什么这里又抄了一遍工具名」是同一个权衡 —— 只不过这里不需要抄，
因为清单**本来就只在这一处定义**。

## 与解题引擎工具集的关系（重要）

对话工具是**平台操作**（建扫描、改技能、加 MCP），不是解题工具。
解题那 12 个工具（`runner.TOOL_NAMES`）在对话里**不重复实现** ——
原因是它们需要沙箱容器，而对话页**没有**沙箱：

* 建一个容器要几秒到几十秒，而用户只是问了一句"你好"；
* 容器是「一次解题」的生命周期物（`SandboxContainer.destroy()` 必须在
  try/finally 里调），挂在对话上意味着它可能挂几天没人销毁；
* 真要在对话里跑 shell，正确的做法是**建一个扫描/解题任务**，
  而不是在对话进程里开容器 —— 那也正好是 `create_scan` / `start_ctf_solve`
  这两个工具存在的意义。

所以对话的工具集是**收敛的**：它把"要跑重活"的请求**转成任务**，
而不是自己上手跑。这一点必须写进提示词，否则模型会一直想调 `run_shell`。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChatToolSpec:
    """一个对话工具的可展示元数据。

    与 `kernel/tools.py::ToolSpec` 同一分工：`name` 是标识符（模型用它，
    界面等宽显示），`label` 是给人看的中文名，`params` 是给模型看的参数说明。

    `scope` 是**执行时必须具备的最小 scope**。它不是装饰：
    `engine` 在真正执行前会用它再判一次（见 `engine._execute_tool`），
    而那是"服务端每一层独立校验"这条纪律的最后一道。
    """

    name: str
    label: str
    description: str
    params: str
    #: 执行这个工具需要的最小 scope。`None` = 只读，任何登录用户都能用。
    scope: str | None = None


#: 对话工具清单。
#:
#: ⚠️ 顺序有意义：它同时是提示词里的列举顺序。把 `create_scan` 排在
#: `start_ctf_solve` 前面，是因为建扫描更常用（渗透/复测都是它）。
CHAT_TOOLS: tuple[ChatToolSpec, ...] = (
    ChatToolSpec(
        name="list_scans",
        label="列出扫描任务",
        description="看最近的扫描任务（含状态、目标、结论数）。用来回答「我上次扫了什么」。",
        params="limit（可选，默认 10）",
    ),
    ChatToolSpec(
        name="create_scan",
        label="创建扫描任务",
        description=(
            "针对一个目标建扫描。这是**真任务**：建完会排队、会真的对目标发包，"
            "几分钟后能在扫描列表里看到结果。"
        ),
        params=(
            "target（必填，地址/仓库/路径）、targetKind（url|repo|path|service）、"
            "skills（必填，列表，取值 web|api|container|cloud|dependencies|secrets|protocol）、"
            "goal（可选，findVulns|captureFlag|verifyFix）、brief（可选，任务背景）"
        ),
        scope="scan",
    ),
    ChatToolSpec(
        name="list_challenges",
        label="列出 CTF 题目",
        description="看平台库里收录的 CTF 题目及其状态（未解/已解/失败）。",
        params="limit（可选，默认 10）、status（可选，unsolved|solved|failed）",
    ),
    ChatToolSpec(
        name="start_ctf_solve",
        label="开始解一道 CTF 题",
        description=(
            "让解题引擎去解一道**已经在库里**的题。这是重活（几十分钟），"
            "会在后台跑，过程到「解题」页看。"
        ),
        params="challengeId（必填）、mode（可选，safe|auto）、brief（可选，人工提示）",
        scope="scan",
    ),
    ChatToolSpec(
        name="list_skills",
        label="查看技能库",
        description="列出技能包（含内置/自定义来源、描述）。改之前先看一遍。",
        params="category（可选，按分类过滤）",
    ),
    ChatToolSpec(
        name="read_skill",
        label="读一篇技能",
        description="读某个技能包的正文（SKILL.md 全文），以及它有哪些附件。",
        params="name（必填，技能名）",
    ),
    ChatToolSpec(
        name="save_skill",
        label="新建/修改技能",
        description=(
            "写一篇**自定义**技能。同名会覆盖（那就是「修改」）。"
            "内置技能改不了 —— 它随代码分发。"
        ),
        params=(
            "name（必填，小写字母数字与连字符）、category（必填）、"
            "description（必填，一句话）、body（必填，SKILL.md 正文 markdown）"
        ),
        scope="admin",
    ),
    ChatToolSpec(
        name="delete_skill",
        label="删除技能",
        description="删一篇自定义技能。内置技能删不掉。",
        params="name（必填）、category（必填）",
        scope="admin",
    ),
    ChatToolSpec(
        name="list_mcp_servers",
        label="查看 MCP 连接",
        description="列出已配置的 MCP 服务器（凭据不回显，只告诉你有几个环境变量）。",
        params="（无）",
    ),
    ChatToolSpec(
        name="add_mcp_server",
        label="添加 MCP 服务器",
        description=(
            "加一条 MCP 连接。**加了就会尝试拉起它并握手** —— "
            "失败会明确告诉你为什么（找不到命令 / 握手超时 / 返回不符协议）。"
        ),
        params=(
            "name（必填，唯一）、command（必填，可执行文件）、"
            "args（可选，列表）、env（可选，对象，**凭据**）、"
            "allowedTools（可选，列表，白名单）"
        ),
        scope="admin",
    ),
)

#: 名字集合，供 engine 做"模型是不是在编工具名"的快速判断。
CHAT_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in CHAT_TOOLS)


def render_tool_catalog() -> str:
    """把工具清单渲染成提示词里那一段。

    格式刻意与 `runner._initial_prompt` 里的工具说明一致（`- name（label）: …`
    + 缩进的参数行），因为模型在解题提示词里已经见过那种形状 ——
    形状一致能让它少一次"这跟我以前见过的不一样"的困惑。
    """
    lines: list[str] = []
    for tool in CHAT_TOOLS:
        lines.append(f"- `{tool.name}`（{tool.label}）：{tool.description}")
        lines.append(f"  - 参数：{tool.params}")
        if tool.scope:
            lines.append(f"  - 需要权限：`{tool.scope}`")
    return "\n".join(lines)


#: 对话系统提示词。
#:
#: ## 为什么把它写成模块里的常量，而不是 `prompts/chat.md` 文件
#:
#: 解题提示词（`kernel_system.md` 等）是**可部署物**：部署者会去改它、
#: 调它，改坏了要能热修。而对话提示词里嵌了一段由代码生成的工具清单
#: （`render_tool_catalog()`），文件形态会让"清单与代码不同步"成为一个
#: 静默故障 —— 加了新工具忘了改文件，模型就永远不知道那个工具存在。
#:
#: 代价是改文案要发版。考虑到这份文案才刚刚定型（v0.93 首版），
#: 先把"不会不同步"排在"能热修"前面。将来文案稳了再挪到文件里，
#: 那时清单注入应该走占位符（像 `{{SKILL_INDEX}}` 那样）。
SYSTEM_PROMPT_TEMPLATE = """\
你是 Suvex 平台的对话助手。用户用自然语言跟你说话，你负责**把话变成事**。

## 你能做什么

你不是一个纯聊天机器人。你手上有一批工具，能真的在平台上做事：
建扫描、起解题、改技能库、加 MCP 工具。**能做就去做，不要只描述步骤**
—— 用户说"帮我扫一下 X"时，正确的回应是调 `create_scan`，然后告诉他
任务已经排队了，而不是教他怎么点界面。

## 【必须遵守】怎么调用工具

想用一个工具时，你**必须**在回复里单独写一行 `ACTION:` 加一个 JSON 对象。
解析器只认这个形状，写成别的一律不会被当成调用：

```
ACTION: {"tool": "list_scans", "args": {"limit": 5}}
```

要点：

- `ACTION:` 后面**只能**跟一个 JSON 对象，`tool` 和 `args` 两个键。
- 一行一个动作。这一轮写完就停，等工具结果回来再决定下一步。
- **不要**把调用写进 ``` 代码块里，不要写注释，不要写 `ACTION: 调用 list_scans` 这种话。
- ★ **最常见的错**：写"我来调用一下 list_scans"或"调用 list_skills 看看"——
  这不是调用，只是叙述。工具根本不会执行，用户会以为你做了而其实什么都没做。
  必须是上面那种 `ACTION: {...}` 的**机器可读**形状。
- 调用前的铺垫可以写（"我先看看最近的扫描"），但要和 `ACTION:` 分开：铺垫写前面，
  `ACTION:` 单独成行。铺垫会展示给用户，`ACTION:` 那行不会。

## 你的工具清单

{tool_catalog}

## 【最重要】哪些事你**做不到**，以及怎么说

你**没有**自己的沙箱容器，所以：

- 你**不能**跑 `nmap` / `curl` / `sqlmap` 之类命令；
- 你**不能**直接访问用户的靶机；
- 你**不能**读文件、执行代码。

这不是权限问题，是架构问题：重活必须交给**扫描任务**去跑（那才有沙箱）。
所以当用户要你"跑一下这个命令看看"时，正确做法是建一个扫描任务，
或者告诉他这件事要走扫描/解题页 —— 而不是假装跑了、更不是编一个结果。

## 纪律（三条，都是硬的）

1. **不编造结果。** 工具没返回的东西你不知道。工具失败了就说失败，
   把失败原因原样转述。绝不要为了"让回答看起来完整"去补一个数字。

2. **写完就说清做了什么。** 调用工具后，用一句话讲清"我做了什么、结果是什么"，
   并把关键标识（扫描 id、技能名、题目 id）写出来 —— 用户要能顺着它去找。

3. **有副作用的操作先确认。** `create_scan` 会对真实目标发包、`start_ctf_solve`
   会消耗提交次数、`delete_skill` 会删文件。如果用户的意思不够明确
   （比如"扫一下内网"但没给具体地址），**先问清楚再动手**，不要猜一个目标
   就去发包。用户明确说了"就扫 192.168.1.5"时才直接做。

## 说话方式

- 中文，简洁，别用"作为 AI 助手"这类套话。
- 不要复述用户刚说的话。
- 列表只在真的有三项以上并列时才用。
- 出错了就直说，不要用"遇到了一些小问题"这种模糊表述。
"""


def build_system_prompt() -> str:
    """渲染最终的对话系统提示词。"""
    # ★ 用 `str.replace` 而不是 `str.format`。
    #
    # 这份提示词里**含真实 JSON**（`ACTION: {"tool": .., "args": ..}`）。
    # `.format()` 会把每一对 `{` `}` 都当占位符，想塞 JSON 就得写成
    # `{{"tool": ...}}`，而且嵌套一层就再翻一倍的括号 —— 实测写错过两次
    # （`KeyError: '"tool"'`、再 `KeyError: '"limit"'`）。那是把「提示词里
    # 有 JSON」这件正常事变成一件要小心的事，而提示词将来还会加更多示例。
    # `replace` 只认那一个显式占位符，JSON 原样保留。
    return SYSTEM_PROMPT_TEMPLATE.replace("{tool_catalog}", render_tool_catalog())


__all__ = [
    "CHAT_TOOLS",
    "CHAT_TOOL_NAMES",
    "SYSTEM_PROMPT_TEMPLATE",
    "ChatToolSpec",
    "build_system_prompt",
    "render_tool_catalog",
]
