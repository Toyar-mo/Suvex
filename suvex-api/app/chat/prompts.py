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

## v0.94 新增的三段可变部分

提示词从"一段固定文案"变成"固定骨架 + 三段按会话渲染的附加内容"：

1. **人格**（`render_persona`）—— 用户选的角色改变语气与关注点，
   但**不改变工具权限**。这是刻意的：人格是"怎么说"，权限是"能做
   什么"。把两者绑在一起会让人以为选了「信息收集」就真的只能扫、
   不能建扫描任务，而实际上工具集是同一套。权限只在
   `ChatToolSpec.scope` 那一层判。
2. **挂载技能**（`render_mounted_skills`）—— 用户预先指定"这个对话
   按这几篇方法论来"，正文直接注入而不是让模型自己去 `load_skill`。
3. **人工审批**（`render_approval_rule`）—— 打开时，有副作用的工具
   要先等用户点确认。这句话必须写进提示词，否则模型拿到"等待审批"
   的结果后会以为自己失败了、反复重试。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.chat.models import CHAT_PERSONAS

_log = logging.getLogger(__name__)


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
    #: 这个工具会不会改变外部世界（发包、写库、起后台任务、删文件）。
    #:
    #: ★ 与 `scope` 是**两个维度**，不是一件事：
    #:   - `scope` 回答"这个人有没有资格"（`create_scan` 要 `scan`）
    #:   - `consequential` 回答"这个动作要不要先问一声"
    #:   `list_scans` 无副作用但仍要登录；`create_scan` 既有权限要求、
    #:   又有后果。把两者合成一个字段，就没法表达"有权限但我想盯着"。
    #:
    #: 「人工审批」只拦这一类。漏标一个工具意味着用户以为开了审批
    #: 就安全，而那一个动作照样直接执行 —— 正是本项目反复在防的
    #: "配了却静默失效"。
    consequential: bool = False


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
        consequential=True,
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
        consequential=True,
    ),
    # ══════════════════════════════════════════════════════════
    # v1.46：比赛平台（羊城杯）原生工具
    # ══════════════════════════════════════════════════════════
    #
    # ★ 这两个和上面的 `list_challenges` / `start_ctf_solve` **不是一回事**，
    #   提示词必须把区别讲清楚，否则模型会混用：
    #     · `list_challenges` / `start_ctf_solve` —— 本平台 KernelChallenge
    #       题库（`/ctf2` 看板那条链路，我们自己的题）。
    #     · `fetch_competition_challenges` / `submit_flag` —— **比赛平台**
    #       （如羊城杯 ycb-test.dasctf.com）的题，比赛当天真正要刷的那批。
    ChatToolSpec(
        name="fetch_competition_challenges",
        label="列出比赛平台的题",
        description=(
            "列出**比赛平台**（羊城杯那类 CTF 比赛平台）上的题目：题号、标题、"
            "分值、分类、是否已解、是否已开放。给 `remoteId` 就顺带查那道题的"
            "靶机连接信息（IP/端口/账号密码）。"
            "⚠️ 比赛当天刷题用**这个**，不是 `list_challenges`（那个是我们自己库里的题）。"
        ),
        params="remoteId（可选，给了就查这道题的靶机环境）",
    ),
    ChatToolSpec(
        name="submit_flag",
        label="交 flag 到比赛平台",
        description=(
            "把一个答案提交到**比赛平台**。会自动剥掉 `DASCTF{}`/`flag{}` 花括号"
            "（赛事只收花括号内的内容，交整串会判错白扣次数）。"
            "平台没受理（限流/额度）时**不消耗错误次数**、可以重试；判错就是真错一次。"
            "每题最多提交 50 次，别暴力试。"
        ),
        params="remoteId（必填，题目 id）、flag（必填，答案，带不带外壳都行）",
    ),
    ChatToolSpec(
        name="start_competition_race",
        label="赛事自动托管（多模型赛跑）",
        description=(
            "把**整场比赛**交给自动托管：自动拉取当前比赛的全部题目、并发地用"
            "多个模型对每道题赛跑（先解出者胜）、卡住自动换思路重试、跳过已解/未开放的题，"
            "解出的 flag 自动提交。适合「这场比赛你帮我全跑了」。"
            "它在后台跑（一场可能几十分钟），起完立即返回，进度用 `competition_race_status` 查、"
            "要停用 `stop_competition_race`。"
            "⚠️ 只处理**比赛平台**的题（与 `fetch_competition_challenges` 同一个平台），"
            "不是本地题库。同一时刻只能有一场，已在跑会告诉你。"
        ),
        params=(
            "models（可选，模型名列表，默认就用当前默认模型）、"
            "maxConcurrent（可选，并发题数 1-8，默认 2）、"
            "maxBudgetUsd（可选，总预算上限，留空不限）、"
            "noSubmit（可选，true=只跑不提交，用于演练）、"
            "maxBumps（可选，每题卡住重试次数 0-10，默认 3）"
        ),
        scope="scan",
        consequential=True,
    ),
    ChatToolSpec(
        name="competition_race_status",
        label="查赛事托管进度",
        description="查当前赛事托管的进度快照：在跑没有、第几轮、每题状态、解出哪些 flag、累计成本。",
        params="（无）",
    ),
    ChatToolSpec(
        name="stop_competition_race",
        label="停止赛事托管",
        description="请求停止当前赛事托管（当前正在跑的工具返回后这一轮就结束）。",
        params="（无）",
        scope="scan",
        consequential=True,
    ),
    ChatToolSpec(
        name="list_skills",
        label="查看技能库",
        description=(
            "列出技能包（含内置/自定义来源、描述）。改之前先看一遍。"
            "加 includeUsage 会带出每篇被加载过几次 —— 回答"
            "「哪些技能没被用过」时用这个，不要用 run_shell 去翻目录。"
        ),
        params="category（可选，按分类过滤）、includeUsage（可选，带出被加载次数）",
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
        consequential=True,
    ),
    ChatToolSpec(
        name="delete_skill",
        label="删除技能",
        description="删一篇自定义技能。内置技能删不掉。",
        params="name（必填）、category（必填）",
        scope="admin",
        consequential=True,
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
            "加一条 MCP 连接。**加了就会尝试连上它并握手** —— "
            "失败会明确告诉你为什么（找不到命令 / 握手超时 / 返回不符协议）。"
            "支持两种类型：stdio（本地可执行文件拉起子进程）与 http（连一个远端 URL）。"
            "用户贴的 `{\"type\":\"http\",\"url\":...}` 配置就用 type=http + url，不要再塞 npx。"
        ),
        params=(
            "name（必填，唯一）、"
            "type（可选，stdio 或 http，默认 stdio）、"
            "command（stdio 必填，可执行文件）、args（可选，列表）、env（可选，对象，**凭据**）、"
            "url（http 必填，连接地址）、headers（可选，对象，**凭据**）、"
            "allowedTools（可选，列表，白名单）"
        ),
        scope="admin",
        consequential=True,
    ),
    # ══════════════════════════════════════════════════════════
    # v0.107：执行类工具 —— 让对话真的能干活
    # ══════════════════════════════════════════════════════════
    #
    # 放在最后而不是插在中间：上面 10 个是**平台操作**（读/写本平台的
    # 数据），这 4 个是**执行**（在沙箱里跑东西）。分组边界和
    # `app/chat/sandbox.py` 的存在边界一致，读者一眼能看出"从这里往下
    # 需要沙箱"。
    #
    # ★ 为什么 `run_shell` / `browser` 不是 `consequential`
    #
    # `consequential` 的判据是 `prompts.ChatToolSpec` 注释里那句
    # "要不要人工点一下同意"，而审批闸拦的是**改平台状态**的动作
    # （建扫描、改技能、加 MCP）—— 那些改的是**我们这边**的东西，
    # 改错了要人工擦屁股。
    #
    # 沙箱里跑一条命令改不了平台：它没有数据库连接、没有 API 凭据
    # （见 `runner.py` 模块 docstring 第 1 条"凭据绝不进容器"），
    # 而且容器是 `--rm` 的、删了就没了。真正的边界是**容器隔离**，
    # 不是点同意。给它加审批只会让"帮我跑一下这个"每一步都卡一次，
    # 而那正是用户在 m05387 说的"要让对话的任务都能自主"要摆脱的东西。
    #
    # ⚠️ 但 `start_ctf_solve` / `create_scan` 仍然是 consequential ——
    #    它们改的是平台数据，判据没变。
    ChatToolSpec(
        name="run_shell",
        label="执行命令",
        description=(
            "在你的专属沙箱容器里执行一条 shell 命令，返回输出。"
            "**这是你干活的主力工具。**容器里可写、能出网、是 root。"
        ),
        params=(
            "command（必填，shell 命令）、timeout_seconds（可选，默认 300，上限 300）、"
            "cwd（可选，只能是 /work 之下的路径）"
        ),
    ),
    ChatToolSpec(
        name="browser",
        label="操作浏览器",
        description=(
            "在沙箱里用真实浏览器打开网页、看页面、点击填写。"
            "适合需要登录态或 JS 渲染的页面（`run_shell` 里的 curl 拿不到的那种）。"
        ),
        params=(
            "command（必填，子命令名：open / snapshot / click / fill / type / "
            "press / get / eval / wait / screenshot / back / close / skills）、"
            "url（open 用，http:// 或 https:// 开头）、"
            "target（click/fill/type 用，@eN 或 CSS 选择器；press 用 key）、"
            "text（fill/type 要填的内容）、what（get 用：title|url|text|html|"
            "value|count|attr）、expression（eval 用）、"
            "interactive（snapshot 默认 true，只要可交互元素）"
        ),
    ),
    ChatToolSpec(
        name="load_skill",
        label="读一篇技能",
        description=(
            "读一篇技能正文（打法手册）。提示词里只有技能清单，"
            "**正文要按需读** —— 别凭名字猜里面写了什么。"
        ),
        params="name（必填，技能名）、file（可选，读技能包里的单个附件文件）",
    ),
    ChatToolSpec(
        name="load_recipe",
        label="读一条工具配方",
        description=(
            "读一条命令行工具配方的完整用法（准确命令行 + 参数 + **这条会漏什么**）。"
            "提示词里只有配方名和一句话局限，正文要按需读。"
        ),
        params="name（必填，配方名，见提示词里的配方清单）",
    ),
    # ══════════════════════════════════════════════════════════
    # v1.81：可重放验证 —— 提交发现前的硬证据
    # ══════════════════════════════════════════════════════════
    #
    # 与 `create_scan` 同级（`scan`）：它会对**授权目标**发真实请求。
    # 但不需要人工审批：它只读目标、不改平台状态；真正的边界是
    # scope guard + GuardedHTTPTransport（SSRF），不是点同意。
    ChatToolSpec(
        name="verify_http",
        label="验证一个 HTTP 发现",
        description=(
            "对**授权目标**重放请求，判断发现是否稳定、可复现，是提交前的硬证据："
            "`replay` 检查同一请求多次响应是否一致；`differential` 比较对照组与"
            "实验组的状态码/标记词/长度是否真的有差异。"
            "不要用它替代思考：它只能证明「这次请求的结果稳不稳、有没有差异」，"
            "不能证明差异一定是漏洞。"
        ),
        params=(
            "kind（replay|differential，默认 replay）、url、method（默认 GET）、"
            "headers（对象）、body（字符串）、markers（字符串列表，出现即算命中）、"
            "attempts（replay 用，默认 2）；"
            "differential 还要 control 与 experiment 两个 {url,method,headers,body} 对象"
        ),
        scope="scan",
    ),
)

#: 名字集合，供 engine 做"模型是不是在编工具名"的快速判断。
CHAT_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in CHAT_TOOLS)

#: 名字 → 规格。给"用户 @ 了某个工具"那条提示词查 label 用。
#:
#: ★ 用 dict 而不是每次 `next(t for t in CHAT_TOOLS if ...)`：后者是
#:   线性扫描，而这段在**每一轮**的系统提示词渲染里都会跑到。
TOOL_BY_NAME: dict[str, ChatToolSpec] = {t.name: t for t in CHAT_TOOLS}

#: 「有副作用」的工具名集合。人工审批只拦这些。
CONSEQUENTIAL_TOOL_NAMES: frozenset[str] = frozenset(
    t.name for t in CHAT_TOOLS if t.consequential
)


# ══════════════════════════════════════════════════════════
# 原生工具调用（v1.64）—— 从文本协议迁到 provider 原生 tool-calling
# ══════════════════════════════════════════════════════════
#
# ## 为什么要这一步（而不是继续打补丁）
#
# 改造前对话走「文本协议」：系统提示词教模型写一行 `ACTION: {json}`，引擎再用
# 一整套正则（`_XML_*` / `parse_actions` / `looks_like_plan` …）去解析与纠偏。
# 模型每发明一种新写法（`<result>` / `<result_note>` / `<analysis>` / DSML /
# `<parameter>` / 代码块里的命令 …）就要加一条正则 —— 这正是「bug 反复」的
# 结构性根源（见 v1.44/v1.52/v1.57/v1.63 的修法记录）。
#
# 原生 tool-calling 把「工具名 + 结构化参数」交给 provider 在协议层保证，
# 我们不再解析任何自由文本，也不再教模型「写 ACTION」。（生产接入点
# `deepseek-v4.1-flash` 实测支持：单选/纯答/多轮/并行四类全部返回结构化
# `tool_calls`；见 docs/native-tool-calling-20261104.md。）
#
# ## 这份表为什么手写、不解析 `ChatToolSpec.params`
#
# `params` 是给人读的散文（"limit（可选，默认 10）"），拿正则拆成 JSON schema
# 会在某个措辞上崩掉。这里按 `engine._execute_tool` 实际读取的键名，逐字写死
# 每个工具的参数，**保证 schema 与执行侧同源**：读的是 `args.get("command")`，
# 这里就声明 `command`。少一个键 = 模型不知道能传它；多一个键 = 模型乱填却
# 被忽略。二者都不是我们想要的。

#: 工具名 → 参数 JSON schema（`{"type":"object","properties":{...},"required":[...]}`）。
#: 与 `CHAT_TOOLS` 一一对应；`build_tools_schema()` 按 `CHAT_TOOLS` 顺序取用，
#: 缺一条会 KeyError（宁可启动即炸，也不要某天某个工具静默消失）。
_TOOL_PARAM_SCHEMAS: dict[str, dict[str, object]] = {
    "list_scans": {
        "type": "object",
        "properties": {"limit": {"type": "integer", "description": "最多返回几条，默认 10"}},
        "required": [],
    },
    "create_scan": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "目标地址/仓库/路径"},
            "targetKind": {
                "type": "string",
                "enum": ["url", "repo", "path", "service"],
                "description": "目标类型，默认 url",
            },
            "skills": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "web", "api", "container", "cloud",
                        "dependencies", "secrets", "protocol",
                    ],
                },
                "description": "要用的扫描技能（必填，至少一项）",
            },
            "goal": {
                "type": "string",
                "enum": ["findVulns", "captureFlag", "verifyFix"],
                "description": "扫描目标，默认 findVulns",
            },
            "brief": {"type": "string", "description": "任务背景（可选）"},
        },
        "required": ["target", "skills"],
    },
    "list_challenges": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "默认 10"},
            "status": {
                "type": "string",
                "enum": ["unsolved", "solved", "failed"],
                "description": "按状态过滤（可选）",
            },
        },
        "required": [],
    },
    "start_ctf_solve": {
        "type": "object",
        "properties": {
            "challengeId": {"type": "string", "description": "题目 id（库里已有的题）"},
            "mode": {"type": "string", "enum": ["safe", "auto"], "description": "解题模式，可选"},
            "brief": {"type": "string", "description": "人工提示（可选）"},
        },
        "required": ["challengeId"],
    },
    "fetch_competition_challenges": {
        "type": "object",
        "properties": {
            "remoteId": {"type": "string", "description": "给这道题的 id 就顺带查靶机环境（可选）"},
        },
        "required": [],
    },
    "submit_flag": {
        "type": "object",
        "properties": {
            "remoteId": {"type": "string", "description": "题目 id"},
            "flag": {"type": "string", "description": "答案，带不带 DASCTF{}/flag{} 外壳都行"},
        },
        "required": ["remoteId", "flag"],
    },
    "start_competition_race": {
        "type": "object",
        "properties": {
            "models": {
                "type": "array",
                "items": {"type": "string"},
                "description": "赛跑用的模型名列表（可选，默认当前默认模型）",
            },
            "maxConcurrent": {
                "type": "integer",
                "description": "并发题数 1-8，默认 2",
            },
            "maxBudgetUsd": {"type": "number", "description": "总预算上限（美元，可选，留空不限）"},
            "noSubmit": {"type": "boolean", "description": "true=只跑不提交（演练）"},
            "maxBumps": {"type": "integer", "description": "每题卡住重试次数 0-10，默认 3"},
        },
        "required": [],
    },
    "competition_race_status": {"type": "object", "properties": {}, "required": []},
    "stop_competition_race": {"type": "object", "properties": {}, "required": []},
    "list_skills": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "按分类过滤（可选）"},
            "includeUsage": {
                "type": "boolean",
                "description": "带出每篇被 load_skill 加载过几次（可选，默认 false）",
            },
        },
        "required": [],
    },
    "read_skill": {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "技能名"}},
        "required": ["name"],
    },
    "save_skill": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "小写字母数字与连字符"},
            "category": {"type": "string", "description": "分类"},
            "description": {"type": "string", "description": "一句话描述"},
            "body": {"type": "string", "description": "SKILL.md 正文（markdown）"},
        },
        "required": ["name", "category", "description", "body"],
    },
    "delete_skill": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名"},
            "category": {"type": "string", "description": "分类"},
        },
        "required": ["name", "category"],
    },
    "list_mcp_servers": {"type": "object", "properties": {}, "required": []},
    "add_mcp_server": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "唯一名字"},
            "type": {"type": "string", "enum": ["stdio", "http"], "description": "默认 stdio"},
            "command": {"type": "string", "description": "stdio 必填，可执行文件"},
            "args": {"type": "array", "items": {"type": "string"}, "description": "stdio 参数（可选）"},
            "env": {"type": "object", "description": "stdio 环境变量/凭据（可选）"},
            "url": {"type": "string", "description": "http 必填，连接地址"},
            "headers": {"type": "object", "description": "http 请求头/凭据（可选）"},
            "allowedTools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "工具白名单（可选）",
            },
        },
        "required": ["name"],
    },
    "run_shell": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "shell 命令"},
            "timeout_seconds": {
                "type": "integer",
                "description": "超时秒数，默认 300，上限 300",
            },
            "cwd": {"type": "string", "description": "工作目录，只能是 /work 之下（可选）"},
        },
        "required": ["command"],
    },
    "browser": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": [
                    "open", "snapshot", "click", "fill", "type", "press",
                    "get", "eval", "wait", "screenshot", "back", "close", "skills",
                ],
                "description": "子命令",
            },
            "url": {"type": "string", "description": "open 用，http:// 或 https:// 开头"},
            "target": {"type": "string", "description": "click/fill/type 用，@eN 或 CSS 选择器"},
            "text": {"type": "string", "description": "fill/type 要填的内容"},
            "what": {
                "type": "string",
                "enum": ["title", "url", "text", "html", "value", "count", "attr"],
                "description": "get 取什么",
            },
            "expression": {"type": "string", "description": "eval 用的 JS 表达式"},
            "interactive": {"type": "boolean", "description": "snapshot 是否只要可交互元素，默认 true"},
            "full": {"type": "boolean", "description": "snapshot 整页 / screenshot 全屏（可选）"},
            "path": {"type": "string", "description": "screenshot 输出路径（可选）"},
            "timeout_seconds": {"type": "integer", "description": "超时秒数（可选）"},
        },
        "required": ["command"],
    },
    "load_skill": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名"},
            "file": {"type": "string", "description": "读技能包里的单个附件文件（可选）"},
        },
        "required": ["name"],
    },
    "load_recipe": {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "配方名（见提示词里的配方清单）"}},
        "required": ["name"],
    },
    # v1.81：可重放验证。differential 的 control / experiment 各是一组请求形状。
    "verify_http": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["replay", "differential"],
                "description": "默认 replay",
            },
            "url": {"type": "string", "description": "replay 的目标地址"},
            "method": {"type": "string", "description": "默认 GET"},
            "headers": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "请求头（可选）",
            },
            "body": {"type": "string", "description": "请求体（可选）"},
            "markers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "出现即算命中的标记词",
            },
            "attempts": {
                "type": "integer",
                "minimum": 2,
                "maximum": 10,
                "description": "replay 重放次数，默认 2",
            },
            "control": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string"},
                    "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                    "body": {"type": "string"},
                },
                "required": ["url"],
            },
            "experiment": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string"},
                    "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                    "body": {"type": "string"},
                },
                "required": ["url"],
            },
        },
        "required": [],
    },
}


def build_tools_schema() -> list[dict[str, object]]:
    """把 `CHAT_TOOLS` 渲染成 OpenAI 风格的 `tools=` 数组（原生工具调用用）。

    形状（OpenAI / 兼容网关统一认）::

        [{"type": "function",
          "function": {"name": ..., "description": ..., "parameters": {...}}}]

    Anthropic 需要在 `LlmGateway._body` 里换成它自己的
    `{"name","description","input_schema"}` 形态（见那边的说明）。
    """
    schema: list[dict[str, object]] = []
    for tool in CHAT_TOOLS:
        params = _TOOL_PARAM_SCHEMAS[tool.name]
        schema.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": f"{tool.label}：{tool.description}",
                    "parameters": params,
                },
            }
        )
    return schema


def may_call_tool(name: str, identity: object | None) -> bool:
    """这个身份能不能调这个工具。

    ## 为什么必须只有这一份实现

    判据在**两处**被用到，而它们端的场景不同：

    · `engine._need_scope` —— 执行期，模型真的点了这个工具；
    · `router.decide_approval` —— 人工审批期，用户点"确认执行"。

    两处原本各写了一遍 `spec.scope is not None and not identity.can(...)`。
    那种重复不会当场出错，但会**漂**：以后有人给 `ChatToolSpec` 加一个
    `scopes: tuple[str, ...]`（比如要求同时具备两个权限），只会改其中
    一处 —— 于是出现"审批放行但执行被拒"（用户点了确认，什么都没发生），
    或者更糟的"审批按旧规则放行、执行按新规则又被拦"。

    ## 为什么不认识的工具名在这里返回 False

    返回 True 的话，任何拼写错误、任何降版后遗留的工具名都是一次绕过：
    "我不认识它，所以它不需要权限，所以放行"。默认拒绝才对 ——
    调用方拿到 False 才能说清"这个工具已经不在列表里了"。

    ## 为什么 `identity` 标成 `object | None` 而不是 `Identity`

    调用方只需要它有 `.can(scope)` 这一个方法。用具体类做类型会让测试
    必须构造一整个身份（scopes 集合、user_id、role……），而那些字段在
    这个判断里一个都不用。
    """
    spec = TOOL_BY_NAME.get(name)
    if spec is None:
        return False
    if spec.scope is None:
        # 没声明 scope = 读操作，任何能进这个页面的身份都可以。
        return True
    if identity is None:
        return False
    can = getattr(identity, "can", None)
    if can is None:
        return False
    return bool(can(spec.scope))


@dataclass(frozen=True)
class ChatPersona:
    """一个"人格"：用户可选的角色，改变语气与关注点。

    ## 为什么人格不改变工具集

    这是 v0.94 最容易被误解的一点，所以写在代码里而不是只写在文档里：
    人格只往提示词里加一段**关注点**，`CHAT_TOOLS` 一个不增一个不减。

    理由：把人设与权限绑在一起，会造出两个都要维护的真相。比如
    「信息收集」限制成只能扫 —— 那用户开这个角色想问"帮我看看刚才那个
    扫描结果"都会被拒。而真正的权限边界在 `ChatToolSpec.scope` 与
    路由层，那两处已经够了。

    ## `id` 与 `label`

    `id` 入库存库（受 CHECK 约束限制），`label` 给人看。两者分开是为了
    将来能改中文名而不动数据 —— 反过来（存中文名）会让改名变成一次
    数据迁移。
    """

    id: str
    label: str
    #: 侧栏/弹窗上的一行说明，告诉用户"选它会发生什么"。
    summary: str
    #: 注入系统提示词的那段。空串 = 与默认人格只差语气。
    guidance: str


#: 全部人格。⚠️ `id` 必须与 `app/chat/models.py::CHAT_PERSONAS` 逐字一致，
#: 也与迁移 `e8f3c2a71b54` 里的 CHECK 约束一致。文件末尾有断言钉住。
#:
#: 顺序 = 界面上卡片/下拉的顺序。`default` 必须排第一：它是 `None` 的
#: 呈现形态，排在中间会让人以为"默认"是某个具体角色。
CHAT_PERSONAS_SPEC: tuple[ChatPersona, ...] = (
    ChatPersona(
        id="default",
        label="通用助手",
        summary="不偏向任何方向，按你说的做。",
        guidance="",
    ),
    ChatPersona(
        id="recon",
        label="信息收集",
        summary="先把面摸清：资产、端口、指纹、可见的服务。",
        guidance="""\
关注点：先建立**面**，再谈缺陷。你倾向于先把目标暴露了什么摸清楚
——哪些端口、什么框架、有没有备份文件/管理入口/默认页 —— 而不是
一开始就去猜某个 CVE。

说话方式：列事实优先于下结论。做了几次采集就说几次，不要合并成
"我做了全面排查"这种含糊总结。发现的目标资产要给出具体地址/路径。""",
    ),
    ChatPersona(
        id="pentest",
        label="渗透测试",
        summary="按攻击路径推进：入口 → 提权 → 影响。",
        guidance="""\
关注点：一条**能走通的路径**比十个孤立发现更有用。你会主动把发现
串起来（这个未授权接口 + 那个内网地址 = 能进多深），并明确说出
"还差哪一步"。

说话方式：每讲一个缺陷都要带上"怎么利用"和"影响是什么"。没有利用
路径的发现要标出来是"待验证"，不要和已验证的混在一起说。""",
    ),
    ChatPersona(
        id="binary",
        label="二进制分析",
        summary="逆向、漏洞点定位、利用原语。",
        guidance="""\
关注点：控制流与数据流。你关心输入从哪进、有没有边界检查、
能不能控到返回地址或函数指针。

说话方式：给地址、给偏移、给具体指令，不要只说"存在缓冲区溢出"。
不确定的地方明说是推测。""",
    ),
    ChatPersona(
        id="postex",
        label="后渗透",
        summary="拿到立足点之后：横向、持久化、凭据。",
        guidance="""\
关注点：从"进去了"到"能待多久、能走多远"。凭据复用、信任关系、
可达网段。

说话方式：严格区分"我看到了"与"我推测能"。涉及横向的动作必须
先说清它会在目标上留下什么痕迹。""",
    ),
    ChatPersona(
        id="container",
        label="容器安全",
        summary="镜像、编排、逃逸面、供应链。",
        guidance="""\
关注点：镜像里装了什么、以什么身份跑、挂了哪些宿主资源。
逃逸面（特权容器、挂载的 docker.sock、hostPath）优先于应用漏洞。

说话方式：区分"镜像层的问题"与"运行时配置的问题"，两者的修法不同。""",
    ),
    ChatPersona(
        id="forensics",
        label="数字取证",
        summary="还原发生了什么：时间线、痕迹、证据链。",
        guidance="""\
关注点：时间顺序与因果关系。你会先排时间线，再解释每个痕迹
能支持什么结论、不能支持什么结论。

说话方式：给时间戳、给来源（哪个日志/哪个文件）。**不要**把
推测写成事实 —— 取证结论的可信度就是它的全部价值。""",
    ),
    # ── v0.99：从 CyberStrikeAI 的 roles/*.yaml 借鉴方向、重写措辞 ──
    #
    # ⚠️ 它那 13 个角色每个只有一句 `user_prompt`（"你是一个专业的 XX 专家，
    #   请使用专业工具…"），照抄过来等于让人格退化成一句自我介绍。
    #   我们既有的 7 个都是「关注点 + 说话方式」两段，所以这四个也照那个形状写。
    ChatPersona(
        id="api",
        label="API 安全",
        summary="接口契约哪里缺了检查：鉴权、越权、注入、批量赋值。",
        guidance="""\
关注点：**每个接口的参数与返回**，而不是某个已知 CVE。你会先把接口清单
列出来（路径、方法、参数、返回字段），再逐个问：这个参数服务端信了吗？
这个 id 换成别人的会怎样？这个字段前端没传但后端会读吗？

说话方式：给接口路径与具体请求。说"越权"时必须写出**换了哪个值、
返回了什么**；只说"可能存在越权"等于没说。区分"未授权能访问"与
"授权后能越权访问"—— 这是两种严重程度差很远的结论。

覆盖顺序：鉴权缺失 → 对象级越权（IDOR）→ 功能级越权 → 批量赋值 →
注入（SQL/NoSQL/命令）→ GraphQL 特有面（内省、批量查询）。""",
    ),
    ChatPersona(
        id="cloud",
        label="云与控制面",
        summary="元数据、IAM、编排 RBAC —— 身份能走多远。",
        guidance="""\
关注点：**身份与权限**，而不是单台机器的漏洞。拿到一组凭据之后，
先问它属于谁、能做什么、能不能换个身份 —— 而不是先找提权 exploit。

说话方式：把权限画成可达范围（能读哪些资源、能写哪些、能不能建新身份）。
区分"配置错"与"设计错"：前者一条命令能修，后者要改架构。

⚠️ 我们的沙箱里**没有** aws / gcloud / kubectl / trivy 这些云 CLI
（已实测）。所以这一人格的产出以**分析与判据**为主：
看配置、看凭据形状、看元数据接口能不能访问、看 RBAC 规则写成了什么，
而不是"跑一个云扫描器"。命令要能落地的才算数。""",
    ),
    ChatPersona(
        id="ctf",
        label="CTF 解题",
        summary="先判题型、先拿稳的分、时间盒。",
        guidance="""\
关注点：**在有限回合内拿到 flag**。你会先花一两轮把题型判清楚
（web / pwn / reverse / crypto / misc），再选一条最可能通的路走，
而不是把每种手法都试一遍。

说话方式：先说"这是什么题、我打算怎么走、大概几步"，再动手。
一条路走三轮没进展就明确说"这条路我放弃了，换 X"，并说明为什么。
**不要**在同一个方向上反复微调参数超过三轮 —— 那是回合耗尽的最常见原因。

判据：拿到 flag 才算解出。中途的任何"应该可以"都不是结论。

⚠️ 注意：只有 `web` / `pwn` / 网络类题的容器有网；
`crypto` / `取证` / `逆向` 是 `--network none`，任何联网手法都不成立。

**赛跑 / 整场托管**：用户说"帮我全跑了 / 整场比赛交给你 / 多模型一起跑"
这类话时，用 `start_competition_race` 起**赛事自动托管**（多模型对每道题
赛跑、卡住自动重试、跳过已解、自动交 flag），而不是自己一题一题地用
`fetch_competition_challenges` + `run_shell` 手搓。起完用
`competition_race_status` 汇报进度；用户要停就用 `stop_competition_race`。
如果只想解**一道**题、或想自己一步步来，那就别开赛跑，正常解题即可。""",
    ),
    ChatPersona(
        id="aillm",
        label="AI / LLM 应用",
        summary="提示注入、工具滥用、RAG 投毒、系统提示词提取。",
        guidance="""\
关注点：**模型分不清"指令"与"数据"** 这条缝。输入不只来自用户，
还来自文档、网页、工具返回值、检索结果 —— 攻击面从那里长出来。

说话方式：验证注入类问题时，判据是**实际的副作用**（发出去的请求、
读到的文件、被调用的工具），不是模型的措辞。模型说"我执行了"经常是幻觉。
系统提示词提取要**逐字吻合**才算命中，拿到大意不算。

覆盖顺序：直接注入 → 间接注入（把指令藏进会被它读的东西里）→
工具滥用（fetch 工具能 SSRF 吗？代码执行工具能 RCE 吗？）→
过度授权/跨租户 → 输出处理不当（模型输出进 HTML/SQL/eval）→
资源成本攻击。

如果这道题要的是「判定考察点是否成立」（`agentsec-*`），那产出是
结论与证据分级（confirmed / needs_follow_up / ruled_out），不是打穿。""",
    ),
)

#: id → 人格。engine 用它把会话上的 `persona` 列解析成具体人格。
PERSONA_BY_ID: dict[str, ChatPersona] = {p.id: p for p in CHAT_PERSONAS_SPEC}

#: 没有选人格时用哪个。
DEFAULT_PERSONA_ID = "default"

#: ★ v1.71：人格 → 默认注入的技能包名字。
#:
#: 「默认调用」的含义：用户选了某个人格，就自动把对应技能包的正文塞进
#: 系统提示词，而不必他再手动在「挂载技能」里一篇篇勾。这样模型一进这个
#: 方向就带着那套打法，不需要先自己 `load_skill` 摸索。
#:
#: 只挑**入口/分诊**级的那篇进去（`solve-challenge` 就是 CTF 的派发入口：
#: 它自己讲 triage + 按题型路由到各 `ctf-*` 子技能的流程，模型读它就知道
#: 该 `load_skill("ctf-web")` 还是 `ctf-pwn`）。把 12 篇全塞进去会把上下文
#: 撑爆（ctf-pwn 单篇 + 70 个附件），而且大部分和当前这道题无关 —— 那正
#: 违背了「清单 + 按需拉」的设计。所以默认只给这一篇「路由图」，具体题型
#: 手册仍由模型按需 `load_skill`。
PERSONA_DEFAULT_SKILLS: dict[str, tuple[str, ...]] = {
    "ctf": ("solve-challenge",),
}

#: ★ 断言：`models.CHAT_PERSONAS`（进库 CHECK 的那份）与本文件这份
#: 必须逐字一致。
#:
#: 为什么这条断言值得存在：两处不一致时**没有任何地方会报错**。
#: 一个只在 models 那份里有的 id 能存进库，而 `PERSONA_BY_ID` 查不到
#: → 静默退回默认人格 → 用户以为自己选了「数字取证」，实际得到的是
#: 通用助手。那是最糟的一类故障：界面一切正常。
assert tuple(p.id for p in CHAT_PERSONAS_SPEC) == CHAT_PERSONAS, (
    "prompts.CHAT_PERSONAS_SPEC 与 models.CHAT_PERSONAS 不一致："
    f"{tuple(p.id for p in CHAT_PERSONAS_SPEC)} != {CHAT_PERSONAS}"
)


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


def render_persona(persona_id: str | None) -> str:
    """渲染人格那一段。默认人格返回空串（不加任何东西）。

    ★ 未知 id 返回空串而不是抛异常，**但有前提**：调用方已经把
      这个 id 过了一遍 pydantic 校验与数据库 CHECK 约束，正常路径下
      到这里不可能未知。返回空串是为了让"库里有一条脏数据"退化成
      「这个对话是通用助手」，而不是让整轮对话 500 —— 一个坏字段
      不该让用户连话都说不成。

      但这不意味着可以不管：`_resolve_persona` 会把未命中的 id 记一条
      warning。静默降级 + 可观测的日志，比抛异常或纯静默都好。
    """
    if not persona_id or persona_id == DEFAULT_PERSONA_ID:
        return ""
    persona = PERSONA_BY_ID.get(persona_id)
    if persona is None or not persona.guidance:
        return ""
    return f"\n## 你在这次对话里扮演的角色：{persona.label}\n\n{persona.guidance}\n"


def render_approval_rule(require_approval: bool) -> str:
    """渲染人工审批那一段。关闭时返回空串。"""
    if not require_approval:
        return ""
    # ★ 这段必须说清"等待审批不是失败"。
    #
    #   真实的坏情况：模型拿到 `status=awaiting_approval` 的结果，把
    #   它当成"工具挂了"，然后换个参数再试一次 —— 用户会看到弹了三个
    #   确认框，每个都是同一件事。这段话就是为了让模型知道该停手。
    return """
## 【重要】这次对话打开了「人工审批」

你调用下面这些会改变外部世界的工具时，**不会立刻执行** ——
会先弹给用户确认。它们是：
{names}

拿到「等待用户确认」的结果时：

- 这是**正常状态**，不是你失败了，也不是工具坏了。
- **不要**换个参数重试，也不要重复调用同一个工具 —— 用户已经看到
  你的请求了，他在决定。
- 用一句话告诉用户你在等什么被确认，然后停下。等他回话。

其他工具（只读的那些）不受影响，照常直接执行。
""".replace("{names}", "\n".join(f"- `{n}`" for n in sorted(CONSEQUENTIAL_TOOL_NAMES)))


def render_tool_hint(hinted: list[str] | None) -> str:
    """渲染"用户点名要用这些工具"那一段。没点名返回空串。

    ## 为什么这个函数只写"倾向"而不写"只准"

    用户 @ 一个工具，通常意思是"别再绕了，直接用它"。但如果那个工具
    其实不适用（比如点了 `run_shell` 却问"我上次扫了什么"），强制
    会让模型硬着头皮跑一条不相干的命令，然后把结果编成一个答案。
    写明"优先"、"不适用就说明原因"给了它正确的退路。

    ## 为什么要校验名字

    `hinted` 来自用户输入，可能是任意字符串。不校验的话，一个
    `@不存在的工具` 会被原样贴进提示词，模型会以为**那个工具存在**
    并试图调用它 —— 于是每一轮都消耗一次"工具不存在"的失败往返。
    未命中的名字在这里就被丢掉，同时明确告诉模型"这些名字没有对应工具"。
    """
    if not hinted:
        return ""

    known = [name for name in hinted if name in CHAT_TOOL_NAMES]
    unknown = [name for name in hinted if name not in CHAT_TOOL_NAMES]

    lines = [
        "",
        "## 用户在输入里点名了工具",
        "",
        "这一轮用户希望优先使用下面这些工具。直接朝它们去，不要先绕一圈：",
        "",
    ]
    for name in known:
        spec = TOOL_BY_NAME.get(name)
        label = spec.label if spec else name
        lines.append(f"- `{name}`（{label}）")

    if unknown:
        lines.append("")
        lines.append(
            "⚠️ 另外这些名字**没有对应的工具**，不要尝试调用它们："
            + "、".join(f"`{name}`" for name in unknown)
            + "。如果用户说的就是它们，直接说明你没有这个工具。"
        )

    lines.append("")
    lines.append(
        "如果点名的工具明显不适合这个问题，**说明理由再用合适的那个** —— "
        "硬用一个不对的工具会把结论带偏。"
    )
    return "\n".join(lines) + "\n"


def render_format_reminder(code_block_only: bool = False) -> str:
    """模型只写了人话、没写动作时，喂回去的一句纠偏（v1.13）。

    ## 为什么需要它（这是真实缺陷的修法）

    改造前：模型回「文件到了。先判型：看包概况、协议分层、流量最大的会话。」
    —— 一句**正确但停在半路**的叙述，没有 `ACTION:`。主循环看到
    `actions` 为空就地 `return`，整轮结束。用户看到的就是"输出一次就停了"，
    只能自己打「继续」把它推一下。

    模型并不是拒绝干活：它已经想好了下一步，只是把"打算做"写成了人话。
    所以要做的不是换个说法安慰用户，而是**把它推回协议里**。

    ## 为什么把可用工具名一起列出来

    只有"请用 ACTION 格式"这种抽象要求时，模型容易再写一段叙述 ——
    它不知道**能调什么**。列真名让它有一个可照抄的落点。
    这与内核侧 `runner._format_reminder()` 是同一个理由（那处也是
    把 `TOOL_NAMES` 拼进去）。

    ## 与 `render_truncation_reminder` 必须分开

    见那个函数的 docstring：病因不同，药也不同。用同一句话会让被切断的
    模型试图把断掉的话写完，于是又超一次。
    """
    sample = 'ACTION: {"tool": "run_shell", "args": {"command": "ls -la /work"}}'
    pasted = (
        "## ⚠️ 你把命令写进了代码块/正文，但它没有被执行\n\n"
        "我看到了你贴的 shell 命令，但**那只是文字**——没有 `ACTION:` 行，"
        "工具一个都没有运行，命令结果也不会回来。\n\n"
        if code_block_only
        else "## ⚠️ 你上一条回复没有包含动作\n\n"
        "那一轮里没有 `ACTION:` 行 —— 所以工具一个都没有执行，"
        "如果没有这句提醒，这一轮就到此结束了。\n\n"
    )
    return (
        pasted
        + "如果你还要继续做事，**现在直接写下一步的 ACTION**，不要重复刚才的叙述：\n\n"
        f"```\n{sample}\n```\n\n"
        f"可用工具：{'、'.join(sorted(CHAT_TOOL_NAMES))}\n\n"
        "如果你已经全部做完了，就用一段话给出结论，不要再写 ACTION。"
    )


def render_truncation_reminder(reply_chars: int) -> str:
    """模型被输出上限切断时，喂回去的是**另一句**（v1.13）。

    ## 为什么必须与上面那条分开

    两件事的病因不同，药也不同：

    | 病因 | 现象 | 该怎么办 |
    |---|---|---|
    | 格式错 | 想好了但没写 ACTION | 写 ACTION |
    | 被切断 | 写了但话没说完 | **先只写 ACTION 一行** |

    用同一句话的后果是：被切断的模型被要求"写下一步的 ACTION"，
    它会试图接着把那句被切断的话写完 —— 于是又超一次。

    `reply_chars` 只是告诉模型"你写了多少字就断了"，给它一个具体的量感
    （内核 `runner._truncation_reminder` 同样带这个数）。
    """
    sample = 'ACTION: {"tool": "run_shell", "args": {"command": "ls -la /work"}}'
    return (
        "## ⚠️ 你的上一条回复达到输出长度上限被切断了\n\n"
        f"你写了大约 {reply_chars} 个字符就断了，最后那段内容丢失。\n\n"
        "这次请**先只写 ACTION 那一行**，不要在动作前后写长段说明 —— "
        "说明可以等拿到工具结果之后再写：\n\n"
        f"```\n{sample}\n```\n\n"
        f"可用工具：{'、'.join(sorted(CHAT_TOOL_NAMES))}"
    )


def render_stall_reminder(tool_name: str, times: int) -> str:
    """同一条命令反复跑、毫无进展时喂回去的话（v1.49）。

    ## 为什么需要这一句

    线上实测（会话 `chat_01M6BWQ69MZJNBAB7C7QMVXPV2`，turn_01M6E99VZ…）：
    模型在同一个死胡同里连跑几十轮 —— 反复写「我意识到一个从未做过的测试…
    现在真正运行」，但跑的是**同一条**命令（或同一族永远失败的命令），
    结果一模一样。这不是"慢慢解"，是**原地打转**：`round_index` 涨到 75、
    `tools_used` 到 101，可没有任何新信息进账，纯烧 token 与时间。

    判据只认**最硬的信号**：连续 N 次执行了**完全相同**的（工具 + 参数）。
    同一条命令连跑 N 次而模型还在期待不同结果，只可能是它没看到/没理解
    上次的结果，或者在赌运气 —— 两种情况都该中止这个策略，而不是继续。

    ## 为什么把次数写进去

    给模型一个明确的"你已经这样干了 N 次"，比抽象地说"别打转"有效得多 ——
    它得看到量级才会承认这条路走不通。
    """
    return (
        "## ⚠️ 你已经在原地打转了\n\n"
        f"你把 `{tool_name}` 这一条命令（参数完全相同）连续执行了 {times} 次，"
        "每次拿到的是**一模一样**的结果，但还在期待不同的输出。\n\n"
        "这说明这个策略是死路。请停下来做三件事之一：\n"
        "1. **换一个完全不同的思路**（换工具、换参数、换切入口），不要再跑这条命令；\n"
        "2. 承认这条线索走不通，把**已经确认的事实**和**还缺什么**讲清楚，交给用户；\n"
        "3. 如果确实没别的办法，就用一段话给出当前的诚实结论，别再空跑。\n\n"
        "继续重复同一条命令不会有任何新结果。"
    )


def render_final_confirm_reminder(narration: str) -> str:
    """模型写了 `FINAL:`，但我们认为它还没做完时，喂回去的话（v1.29）。

    ## 为什么需要这句话

    实测（会话 `chat_01M62X9DDC52RM95NKQW8P5DY8`）：模型的 `FINAL` 内容是
    「现在查 libc 版本。」—— 那是**下一步计划**，不是结论。而 `FINAL:` 是
    终止符，写了就收尾，于是用户看到「又是中途停下，继续都没用」。

    所以这一句要做的只有一件事：**要求它把「做完了」讲清楚**。要么补上
    真正的结论（拿到的东西、证据在哪），要么承认还要继续、改写成 `ACTION:`
    去接着做。

    ## 为什么把它的原话引回去

    不引原话时，模型经常把这段提醒理解为「格式又错了」，然后原样再写一遍
    `FINAL:` 同一句话。引出来它才能看出自己写的是个计划，而不是结论。
    """
    sample = 'ACTION: {"tool": "run_shell", "args": {"command": "ls -la /work"}}'
    quoted = " ".join((narration or "").split())[:200]
    lines = [
        "## ⚠️ 你写了 `FINAL:`，但那看起来不是结论",
        "",
        f"你上一次回复的 `FINAL:` 内容是：「{quoted}」",
        "",
        "`FINAL:` 是**终止符** —— 写了它就结束这一轮，用户拿到的就是这句话。上面这句是在说**接下来要做什么**，不是已经拿到了什么。",
        "",
        "所以只有两种正确的写法：",
        "",
        "1. **还没做完** → 把这一句改写成一行 `ACTION:`，直接去做：",
        "",
        "```",
        sample,
        "```",
        "",
        "2. **真的做完了** → 用 `FINAL:` 写**结果**：拿到了什么（地址、flag、命令输出里的关键行）、证据在哪个文件。不要写「我打算…」。",
        "",
        f"可用工具：{'、'.join(sorted(CHAT_TOOL_NAMES))}",
    ]
    return "\n".join(lines)
def render_plan_exhausted_note() -> str:
    """打回额度用尽时，替模型给用户的一句实话（v1.30）。

    ## 为什么需要这句话

    线上会话 `chat_01M640EY2P54QC1E2QS54R0R3Z`：模型先把「checksec 没输出，
    我用 readelf 和 pwn 自己看防护」当成 `FINAL:`（被打回一次），下一次又写
    「典型栈溢出。看主逻辑。」。每轮的打回额度只有 1 次，于是第二句被当作
    结论落进对话流（`surface=answer`），用户看到的仍是「解题到一半就停」。

    ## 为什么不是继续打回

    打回要花一次真实模型调用，无限问下去会把钱和时间都烧在一个不肯收尾的
    模型上。所以额度用完就停 —— 但**不能假装那是结论**：补一句实话，让
    用户知道下一步还没执行，也知道该说什么才能接着跑。
    """
    return "（这一轮停在了下一步上：「继续」我接着执行 —— 上面那句是计划，不是结果。）"


def render_turn_wall_note(max_seconds: int) -> str:
    """单轮墙钟预算（`TURN_WALL_SECONDS`）到点时，替模型给用户的一句实话（v1.68）。

    ## 为什么需要这句话

    会话 `ctf` 的 turn_01M6N2JEY1FZ9HERVADHSQHVZ3 单轮连跑 6.3 小时、629 次工具，
    每步都是不同的真实命令（真在做一道极难的堆 pwn），所以"连续重复动作"检测
    永不触发、"纠偏额度"也管不到"动作" —— 于是一个合法但极难的活会无限烧下去，
    用户点「中断并继续」只是让它接着跑，观感就是"一直不停"。对应 CyberStrikeAI
    的 `agent.tool_timeout_minutes`：一次任务必须有总时长上界。

    ## 为什么不是硬砍

    到点**优雅收尾**并如实说明"到了本轮时长上限、点继续可以接着跑"，把控制权
    交回用户，而不是静默中断或假装是结论。这样既不失控，也不一刀切死真正的长任务。
    """
    minutes = max(1, int(round(max_seconds / 60)))
    return (
        f"（这一轮已经连续跑了超过 {minutes} 分钟，为了不让它无上限地一直跑，"
        "我先停下、释放了环境。任务还没结束的话，点「继续」我再接着干——"
        "已经做过的步骤不会重来。）"
    )


def render_repeated_conclusion_note() -> str:
    """连续多次给出同一结论时，替模型给用户的一句实话（v1.69）。

    ## 为什么需要这句话（真实事故）

    会话 `chat_01M6P1TAYTASS6P64P85PED8MB`（「千千的咲.zip解题」）单会话跑了
    12+ 小时、429 次 run_shell、127 轮，最后反复给出同一句结论（「没拿到 flag——
    这题需要动态分析，本沙箱没有 KVM/虚拟化」），却每次换个算法"再穷尽一下"。
    每步都是不同命令，重复动作检测不触发；墙钟到点停下，用户点「继续」又续。
    正确行为：判定它已经到底，收尾并讲清楚"这是阻塞、不是没试够"，别让用户
    以为再点一次「继续」就有转机。对应 CyberStrikeAI「已授权不得再核实 /
    结论要给证据而不是无限重试」的纪律。
    """
    return (
        "（我注意到刚才已经多次给出同一个结论，就不再重复跑下去了——"
        "这不是「还差一点没试到」，而是当前环境/信息确实卡住了。"
        "如果要继续，请先补齐缺失的条件（例如换到满足要求的环境、或提供关键信息），"
        "然后告诉我，我再接着做。）"
    )


def render_native_tool_note(names: list[str] | None = None) -> str:
    """上游只回了原生工具调用、平台没能执行时，替模型给用户的一句实话。

    ## 为什么必须有一句可见的话（这是真实缺陷的修法）

    线上会话 `chat_01M6556ES13EGH8NN712252SPB`：本地 OpenAI 兼容网关
    回的是 `delta.tool_calls` + `finish_reason="tool_calls"`，正文为空。
    引擎拿到空正文、空动作，纠偏额度用尽后直接 `return` —— turn_run
    正常 `done`，界面上什么都不动，用户看到的就是「对话跑到一半就结束」。

    所以额度用尽时**不能静默收尾**：必须明确告诉用户这一轮的失败形态，
    并给出可行动作（重发 / 换接入点）。

    ## 为什么点名是哪个工具

    「只回了原生工具调用」是内部协议层的事，用户看到会一头雾水。带上
    工具名（`run_shell`、`http_request`…）至少能对上界面上的工具卡，
    也能在排障时区分「网关给的是空壳」与「网关给了一个我们没实现的工具」。
    未知名字用「工具调用」—— 不编造一个不存在的工具名。
    """
    label = "、".join(name for name in (names or []) if name) or "工具调用"
    return (
        f"（上游这一轮只回了原生工具调用（{label}），平台没能执行，"
        "因此本轮没有结果；可以重发或换接入点。）"
    )


def render_mounted_skills(mounted: list[tuple[str, str]]) -> str:
    """渲染"用户挂载的技能"那一段。

    `mounted` 是 `(技能名, 正文)` 列表。空列表返回空串。

    ★ 为什么把正文整段注入，而不是只给名字让模型自己 `load_skill`：
      用户勾选技能的意思就是"这个对话就按这几篇来"。如果还要模型
      自己判断该不该读，那他勾的东西有一半时候不会生效 —— 而用户
      无从知道是模型没读还是技能没用。注入把这件事变成确定的。

    ★ 但注入有上限：`engine` 那一侧已经把总量卡在
      `MAX_MOUNTED_SKILL_CHARS` 以内（超出会按顺序截）。
      这里的 docstring 只是提醒，真正的闸门在调用方 —— 一个提示词
      构造器不该承担"总共能有多大"这个决策。
    """
    if not mounted:
        return ""
    blocks: list[str] = [
        "\n## 用户在这次对话里挂载了这些方法论文档\n",
        "**它们的优先级高于你的一般经验**：当你的一般做法与这里写的不一致时，"
        "按这里的来。但**不要**主动宣布「我正在遵循某某技能」—— 按它做就行。\n",
    ]
    for name, body in mounted:
        blocks.append(f"### 技能：`{name}`\n")
        blocks.append(body.strip())
        blocks.append("")
    return "\n".join(blocks)


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
#: 先把"不会不同步"排在"能修"前面。将来文案稳了再挪到文件里，
#: 那时清单注入应该走占位符（像 `{{SKILL_INDEX}}` 那样）。
#:
#: ⚠️ v1.64：`SYSTEM_PROMPT_TEMPLATE` 里「## 【必须遵守】怎么调用工具」到
#:   「…正常说完即可。」这一段被一对 `####` 包住，作为**文本协议**版本。
#:   当接入点支持原生工具调用时，`build_system_prompt(native_tools=True)` 会把
#:   这一段（含两端的 `####`）整段换成 `_NATIVE_TOOLS_SECTION`，让模型用结构化
#:   调用而不是写 `ACTION:`/`FINAL:`。不支持时原样保留（向后兼容/降级网关）。
_NATIVE_TOOLS_SECTION = """\
## 【必须遵守】怎么调用工具

工具**已经作为原生函数提供给你了**（function calling）。想用一个工具时，直接
发起一个工具调用（tool call），把工具名和参数填进去即可 —— **不要**再在正文里
手写 `ACTION:` 那行 JSON，也**不要**写 `FINAL:`。正文只写给人看的话（叙述/结论）。

要点：

- 工具调用由协议层传送，参数结构见每个工具的 schema。填错参数名系统会拒绝，
  按报错改正再调。
- 一轮里可以发起一个或多个工具调用；系统把执行结果回给你，你再决定下一步。
- ★ **工具结果只能由系统返回，你不能自己编**：绝不要在正文里写
  `<result>…</result>`、"结果：xxx" 之类假装工具已经跑完了。你写下的任何
  "结果"都不是真的。**要么发起工具调用真去调，要么老实说还缺什么。**
- ★ **最常见的错**：正文里写"我来调用一下 run_shell"或贴一段 shell 命令，
  却**没有真正发起调用**。那只是叙述，工具不会执行。要它执行就发起一次工具调用。

### 什么时候算"这一轮真的做完了"

- 还要继续做 → 继续发起工具调用；
- 全部做完了 → **直接用一段话给出结论**（拿到了什么、证据在哪个文件），
  正文即最终答复，不需要任何终止符。

★ 只有**真的拿到的东西**才算结论，绝不能把"接下来要做什么"当成结论文本写出来
  —— 那会让这一轮提前结束、用户只能再打「继续」。凡是有「先…」「接下来…」
  「然后…」「现在去…」这类话，就去把它做成一次工具调用。

### 交付结果怎么给（登录 / 口令 / 凭据类任务）

用户让你「登录后台」「爆破口令」「验证凭据」时，最终答复**必须先给结果块**，
再谈过程或环境问题。格式固定：

登录信息
- 目标：http://...
- 用户名：admin
- 密码：...
- 结果：成功 / 失败（POST /api/v1/auth/login → 200 / 401，带证据）
- 备注：权限、后续建议

规则：
- 结果已经拿到时，**不要**用「沙箱没恢复，等你修好再继续」收尾 —— 那会让用户
  以为任务没完成，而实际上他问的那件事已经有答案了。
- 不要在长段推理中间夹一句「登录成功」；用户要的是**可复制、可核对**的字段。
- 确实还有依赖环境的未完成项，另起「未完成」小节，放在结果块之后，
  不要混进结果里，更不要盖住结果。
"""

SYSTEM_PROMPT_TEMPLATE = """\
你是 Suvex 平台的对话助手。用户用自然语言跟你说话，你负责**把话变成事**。

## 你能做什么

你不是一个纯聊天机器人。你手上有两类工具，都能真的做事：

1. **平台操作**：建扫描、起解题、改技能库、加 MCP。**能做就去做，不要只描述
   步骤** —— 用户说"帮我扫一下 X"时，正确的回应是调 `create_scan`，然后告诉
   他任务已经排队了，而不是教他怎么点界面。
2. **执行**：`run_shell` 在你的沙箱里跑任意命令，`browser` 操作真实浏览器。
   写代码、装包、跑工具、抓网页、调 API —— 都走这两个。

用户说"帮我写个脚本试试"、"搜一下 CVE-2024-XXXX"、"这个接口返回什么"时，
**直接做**，不要回"我不能执行代码"。你不能做的是"替用户点他屏幕上的按钮"，
不是"执行"。

## 问事实，还是做任务

先分清用户要的是**一句话**，还是一件**交付物**：

- 问现状 / 事实（"我有哪些技能""哪几篇没被用过""上次扫描结果如何"
  "这个配置是什么"）：用已有工具查到，就**用一段话回答**。不要为此创建文件、
  写交接文档、产出报告或顺手改配置 —— 用户没有要那些东西。
- 明确要交付物（"写份报告""把这个脚本存下来""生成证据包"）：才把东西写进
  `/work/persist/`，并在结论里说清文件路径。

不要把"回答一个问题"自动扩展成"替用户完成一个项目"：那会花掉几十轮模型调用
和几十分钟，而用户等的是一句话。平台自己的数据优先用平台工具查
（例如 `list_skills(includeUsage=true)`、`list_scans`），不要用 `run_shell`
去翻内部目录冒充查询。

## 执行任务时的纪律（只在"做交付物 / 解题 / 排查"时适用）

- **授权范围**：目标由用户明确给出（题目靶机、他让你操作的服务）。在沙箱内
  可以直接执行验证，不要只给步骤、不要因为"双用途"自我审查；但不要越界到
  用户没给的目标或宿主环境。
- **不自行判死**：拿到形似答案（flag / 密钥 / 口令 / 关键偏移）时，**即使不确定**
  也要在结论里给出并说明证据来源；不要因为"没把握"就不说、或只交一句"没找到"。
  是否提交、是否继续验证由用户决定。
- **关键中间结果落盘**：写进 `/work/persist/`，并在结论里给出路径。聊天上下文
  会滚动，文件不会。

####
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
- ★ **工具结果只能由系统返回，你不能自己编**：绝对不要在你的回复正文里写
  `<result>...</result>`、"结果：xxx" 之类假装工具已经跑完了。你写下的任何
  "结果"都不是真的，系统不会执行它，只会让你以为自己做过而陷入空转。
  **要么写一行 `ACTION:` 真去调工具，要么老老实实说还缺什么。**
- 调用前的铺垫可以写（"我先看看最近的扫描"），但要和 `ACTION:` 分开：铺垫写前面，
  `ACTION:` 单独成行。铺垫会展示给用户，`ACTION:` 那行不会。

### 什么时候算"这一轮真的做完了"

如果你这一轮**已经动过工具**（跑过命令、看过文件），结束时必须二选一：

- 还要继续做 → 写一行 `ACTION: {...}`；
- 全部做完了 → **以一行 `FINAL:` 开头写你的结论**，例如：

```
FINAL: 解出来了，flag 是 flag{...}。证据是 /work/out.txt 里的第 3 行。
```

★ **`FINAL:` 只能写"已经拿到的东西"，绝对不能写"接下来要做什么"。**
  反例（真实出现过）：「`FINAL: 现在查 libc 版本。`」—— 这是一句计划，
  写进 `FINAL:` 就等于告诉系统"我做完了"，这一轮立刻结束，用户只能再打一次
  「继续」。凡是有「先…」「接下来…」「然后…」「现在去…」这类话，一律写成
  `ACTION:` 去做，不要写 `FINAL:`。

为什么要这一行：只写一段像结论的话时，系统无法区分"做完了"和"话说到一半
忘了写动作"—— 后者会让整轮停在那半句话上，用户只能自己再打一次「继续」。
`FINAL:` 是**明确的终止符**，写了它就一定收尾，不写就会被要求接着说下去。

- `FINAL:` 之后的正文会原样展示给用户（`FINAL:` 这三个词本身不会）。
- 纯聊天、查资料这类**没动过工具**的回答不需要写 `FINAL:`，正常说完即可。
####

## 你的工具清单

{tool_catalog}

## 沙箱里有哪些命令行工具

**先看清单再动手**。清单里每条都带一句「这个工具默认会漏什么」——
那句话比工具名本身重要，因为它决定你会不会拿着默认结果下错结论。

{recipe_index}

用法：`load_recipe` 拉完整命令行与参数；不确认装没装就先 `command -v <名字>`。
**清单里没有的工具并不代表一定没有** —— 你可以 `command -v` 或直接跑。

### 交互式程序（gdb / nc / python REPL）用 tmux

`run_shell` 每条命令都是**新 shell**，进程不会保留。需要跨轮交互时用镜像里的 `tmux`：

```
tmux new-session -d -s dbg 'gdb -q /work/a'
tmux send-keys -t dbg 'b main' Enter
tmux capture-pane -pt dbg | tail -40
tmux kill-session -t dbg
```

`nc`、python REPL、`sqlmap --os-shell` 同理。不要每条命令重新起 gdb/nc。

### 缺工具就自己装，别回头说「没装」

沙箱是容器内 **root**、且**能出公网**，装包是正常操作，不是特殊许可：

- 系统工具缺了：`apt-get update && apt-get install -y --no-install-recommends <包名>`；
- Python 库缺了：`pip install <包名>`（或 `pip3`）；
- 装完**再跑一次确认**（`<命令> --version` / 直接跑小输入），别装完就假设成了。

典型例子：拿到图片要 OCR，`command -v tesseract` 为空或报错 —— 就
`apt-get install -y tesseract-ocr tesseract-ocr-chi-sim`（中文图必须带
`-chi-sim`，否则中文是乱码）再 `tesseract img.png stdout -l chi_sim+eng`。
**不要**因为"没装 tesseract"就停在这里让用户自己去装 —— 那是在浪费你自己
的执行环境。

只有在**确实装不上**（仓库没有该包 / 网络被她国限制 / 编译依赖搞不定）时，
才如实告诉用户"这个工具我装不上，原因是……"，并说明你试过什么。

### 配置类错误：说清楚，不要绕过

平台返回「未绑定账号」「没有权限」「未配置」这类话时，那是**配置问题**，
重试和换参数都不会让它变好（2026-11-08 实测：账号解绑后，模型先重试了一
次、又从沙箱里 `getent hosts ctf2` 并逐个 `/dev/tcp` 探端口，想"直接连靶机"
绕开平台 —— 全是白跑）。

遇到这类错误就停下来说三件事：缺什么、在哪里补、补完之后还要做什么。
**不要**：

- 反复重试同一个工具（第 2 次的结果会和第 1 次一模一样）；
- 在沙箱里直连题目靶机或内网地址去"绕过平台"——靶机与附件**只能**通过
  平台接口拿到，直连既拿不到题也证明不了任何事；
- 把 `ctf2` / `198.18.x.x` 这类地址的"端口开放"当成线索：沙箱里它们是
  代理的 fake-ip，`/dev/tcp` 显示 open 并不代表对面真的在监听。

### Windows 专用工具？找 Linux 的等价方案，别一句"环境不支持"就放弃

沙箱是 Linux。很多教程、工具（尤其是安全/取证类）是 **Windows GUI 程序**：
RouterPassView、WinHex、Process Hacker、各类厂商的"配置解密器"GUI……你在
Linux 里当然**跑不了那个 exe**。但"跑不了这个 exe" **不等于"这件事做不了"**。

放弃前，按这个顺序至少试过三件事（**不要**只报告一句"需要 Windows 工具，
无法完成，我主动放弃"——那是把一个可解的题让给对手）：

1. **找 Linux 原生的等价工具**：先 `apt-get install` / `pip install` 试这些名字。
   - 路由器/设备配置解密：`routerpassview` 的替代是按**格式**找开源实现
     （如各厂商固件解析脚本、`binwalk` 拆出配置段、GitHub 上同名格式的
     Python 解析器）。
   - 十六进制/结构分析：`xxd`/`hexdump`/`radare2`/`imhex-cli`/`010-editor` 都
     有 Linux 版或 CLI。
   - 抓包/解密：`tshark`/`wireshark`/`mitmproxy`。
   - 取证/隐写：`binwalk`/`foremost`/`steghide`/`zsteg`/`exiftool`/`photorec`。
2. **自己写解析器**：如果这个 Windows 工具本质就是"按某格式解密一段字节"，
   格式往往是公开的（甚至就是 AES/XOR/固定 key 的变体）。用 Python 照格式
   实现一遍 —— 这是 CTF/取证里最常见的正解，不是"自己搓暴力扫描"。
3. **用公开资料反推算法**：`web_search`/抓文档，查这个"Windows 工具"到底
   做了什么变换（很多工具的算法在逆向博客里有说明）。

只有当**这三条都试过、确实走不通**（格式闭源且未知、加密依赖硬件/联网授权、
样本本身就需要 Windows 运行时）时，才向用户说明"这个具体步骤在 Linux 下需要
X，我尝试了 A/B/C 都不行，建议……"。**给的是可执行的替代路径，不是放弃。**

典型反例（不要再犯）：用户给了一个路由器备份配置，让你解出里面的宽带账号。
你回"需要 RouterPassView，这是 Windows 工具，Linux 无法解密，我主动放弃。"
—— 正确做法是 `binwalk` 拆包 → 认出配置段格式 → 用 Python 按该格式解密，
这段配置的算法公开且简单。**"跑不了那个 GUI"从来不是终点。**

### 需要「动态分析」不等于「必须起 Android 模拟器」——userspace 模拟不走 KVM

分析 Android 原生 `.so`（尤其加壳/加密/运行时才算密钥的题）时，常见错误是：
一听说「要动态分析」，就想「起一个 Android 模拟器跑起来」——然后发现沙箱/本机
没有 `/dev/kvm`（没有硬件虚拟化），于是断定「动态分析做不了，我放弃」。
**这是把唯一一条路当成全部。** 动态分析有两条路：

1. **整机模拟**（Android 模拟器 / 真机）：需要 `/dev/kvm` 或真机。环境缺 KVM 时这条走不通。
2. **userspace 指令级模拟**（**不需要 KVM，沙箱里通常就能跑**）——把 `.so` 里的
   **目标函数**单独放进 CPU 模拟器执行，只求算出一个值（密钥/明文/判定结果），
   不需要整个系统起来：
   - **unicorn**（`pip install unicorn`，本沙箱通常已自带，`python3 -c "import unicorn"` 先确认）——最灵活：
     自己用 `Uc(UC_ARCH_ARM64, UC_MODE_ARM)` `mem_map` 映射 `.so` 的代码段，`emu_start(函数地址)`
     执行，hook 读寄存器拿返回值。配 `capstone`（也常自带）反汇编定位函数。能做「只跑解密函数、
     喂密文、读明文」。已验证：用 unicorn 直接 `mem_map` arm64 `.so` 的 PT_LOAD 可执行段并
     `emu_start` 执行真实函数是可行的（`bti c`/`adrp`/`b` 这些指令都能正常解码执行）。
   - **qemu-user**（`qemu-aarch64`，`qemu-aarch64-static` 更常见）——直接 `qemu-aarch64 ./arm64_binary`，
     或 `qemu-aarch64 -L <sysroot> 程序`；能跑单个 ELF，不需要模拟整个 Android。
   - **unidbg**（Java，专门做 Android `.so` 的 JNI 级模拟；`apt install default-jdk` 后
     从 GitHub 拉 unidbg，`emulatorForJni`/`emulateFunction` 直接调 `.so` 里的函数）——
     Android 原生破解的首选，缺 KVM 也能用。

**做之前先确认一件事**：这个「加密/解密」到底发生在**哪个函数**、`p.bin` 这类数据到底
**有没有被程序读**。别假设「一定有解密函数」：先 `strings -n 6 lib.so | grep -iE '<文件名>|Data/|Decrypt|AES'`
确认二进制里真的引用了目标数据；如果 `strings` 里 `p.bin`/`Data/` 全 0 命中，说明**这个
文件根本没被程序读取**（可能是纯拼图/图形题、或数据在别处），那就不存在「解密函数」可
模拟——此时应转去分析**判题/校验逻辑**（hook 或模拟 `Submit*`/`IsComplete` 这类函数看它
怎么判对错），而不是硬啃一个不存在的解密。

**只有当**：目标函数确实存在、且 unicorn/qemu-user/unidbg 三条**都试过确实跑不通**
（函数依赖未实现的 JNI/系统调用且无法 stub、或需要真实 GPU/输入）时，才说「这一步在
无 KVM 环境需要 X，我试了 A/B/C」。**「没有 KVM」不是「做不了动态分析」。**

### 先找现成工具，别自己搓暴力扫描（这条能省你几分钟到几十分钟）

分析二进制/大文件时，**先想"有没有现成工具能干这件事"，不要一上来写 Python
`for` 循环逐字节扫**。线上真实事故：一个 2.7 MB 的 chunk，模型写了

```python
for i in range(len(d) - 16):   # ~5000 万次迭代，纯 Python
    v = struct.unpack_from('<16i', d, i)
    ...
```

一条命令占了容器 98% CPU 好几分钟、每轮还反复跑，用户看直播流就像卡死。
而同样的目标，现成工具是**毫秒级**的：

| 要做的事 | 用这个，别自己写循环 |
| --- | --- |
| 抽可打印字符串（找硬编码口令/路径/URL） | `strings -n 6`；UTF-16 加 `-e l` |
| 找某个字节串/魔数的偏移 | `grep -oba '<pattern>' file` 或 `grep -aboP` |
| 十六进制查看附近字节 | `xxd -s <offset> -l 512 file` |
| ELF/PE 节区、符号、重定位 | `objdump -h/-t/-R`、`readelf -a` |
| 反汇编一段代码 | `objdump -d`（数据节区用 `-D`）、`radare2 -A -c ...` |
| 拆包/嵌套/固件 | `binwalk -e` |
| 提取已知格式 | 对应工具：`unzip`/`tar`/`ffmpeg`/`retoc`（UE5 pak）、`7z` |

**只有当现成工具确实不适用**（比如要按自定义结构解析二进制、要算相关性/
相似度）时，才自己写代码 —— 而且写的也应该是**向量化/numpy** 或**用工具先
缩小范围再精读**，不是对整份文件做 O(n) 的逐字节 Python 循环。

判断标准很简单：**如果一段脚本要跑超过 10 秒，先停下来想"是不是有工具能替代"。**


## 打法手册（技能包）

{skill_index}

用法：`load_skill` 按名字拉正文（`file` 可拉篇内附件）。清单只有一句话描述，
**别凭名字猜正文写了什么**。

## 【最重要】你**有**执行环境，别说自己做不到

你有一个**专属沙箱容器**，通过 `run_shell` 和 `browser` 驱动。它是：

- **能跑命令**：`nmap` / `curl` / `python3` / `gcc` / `sqlmap` 等都能用
  （具体装了哪些见下面的配方清单）。
- **能读写文件**：容器内 root，`/work` 及整个文件系统可写；
- **能出网**：能访问互联网（搜索、装包、拉工具）；
- **能写代码**：写脚本、编译、跑起来、改到对为止 —— 这是正经用法，
  不要因为"这看起来像开发任务"就推给用户。

⚠️ **绝对不要**说"我没有执行环境"、"我不能运行命令"、"请你自己在终端跑"。
这句话曾经是对的（v0.107 之前确实没有执行环境），现在是**错的**，
说出口就等于在骗用户。用户会看到你说完这句之后什么都没发生。

### 用户传的附件在 `/work/attachments`

用户上传的文件**挂在 `/work/attachments/`（只读）**。这条很关键，因为：

- 文本附件（日志、配置）的正文已经贴在你收到的消息里了，直接读就行；
- **二进制附件**（pcapng、zip、ELF、图片）的正文只有一句"读不了" ——
  文件本身没消失，它在那个目录里。用 `run_shell` 去分析：
  `file` 看类型、`xxd` 看字节、`binwalk` 拆包、`tcpdump -r` 读抓包、
  `strings` 捞可打印串。
- 文件名带附件 id 前缀（形如 `a8f7c9d1-xxx.pcapng`）。要精确路径的话
  先 `ls /work/attachments/`，**别按你记忆里的原文件名直接 `cat`** ——
  名字对不上会得到 file not found，而文件其实好好地在。

⚠️ 如果 `ls /work/attachments/` 是空的，而用户说"我传了文件"：先别
下结论。用 `find / -name '*.<扩展名>' 2>/dev/null` 再确认一次，然后
如实告诉用户"文件没到我这儿"——**不要假装分析过**，也不要读了几句
"读不了"就编一个结论。

### 给用户下载的产物：放 `/work/persist/`

`/work/persist/` 是**会话级持久工作区**，挂的是宿主目录：写进去的文件
**不随沙箱回收消失**，平台能直接列出来、给你下载（对话页里点链接即可）。
所以凡是**要交付给用户留存/下载**的东西 —— 报告、脚本、证据、生成的文件
—— 都写到 `/work/persist/` 下，别留在 `/work` 的其它位置。

- `/work` 其它位置是容器可写层：容器一回收（归档 / 空闲 30 分钟）就没了，
  平台也下载不到。
- 写完后在回复里写**相对路径**，例如"报告在 `reports/pwn-分析.md`"。
  平台会把 `/work/persist/<相对路径>` 渲染成可点的下载链接。
- 例：`mkdir -p /work/persist/reports && mv /work/report.md /work/persist/reports/`。

### 下载大附件（几十 MB ~ 数 GB）：必须断点续传 + 直接落到 `/work/persist/`

**一条 `run_shell` 最多跑 300 秒**（`timeout_seconds` 上限就是 300，超了会被容器内
`timeout` 杀掉）。所以：

- **一条命令下不完的大文件，别指望一次 `curl -o` 下完** —— 超时被杀的后果是
  **只留下半截文件**（比如 1.9GB 的包下到 480MB 就被砍），而你很可能以为它下好了。
- **断点续传（关键）**：用 `curl -C -`（offset 续传）。中断/超时后**再跑同一条命令
  会从断点接着下**，而不是从 0 重来：

  ```bash
  # 目标：把 1.9GB 的包下到持久目录（容器回收也不丢）
  mkdir -p /work/persist/dl
  curl -C - -sS --retry 5 --retry-delay 2 -m 280 \
       -o /work/persist/dl/misc.zip 'https://…/Be_Powerful.zip'
  ```

- **一条命令只给一次机会**：`-m 280` 让 curl 在 300 秒上限前自己先停（留 20s 余量），
  这样是 curl 正常退出、文件是完整可续的，而不是被 `timeout` 强杀。
  下到一半没完？**再发一次同样的命令**即可续上（`-C -` 会读已有文件大小）。循环几次直到下完：

  ```bash
  cd /work/persist/dl && for i in 1 2 3 4 5 6 7 8; do
    curl -C - -sS --retry 3 -m 280 -o misc.zip 'https://…/Be_Powerful.zip' && break
    echo "retry $i"; sleep 1
  done; ls -l misc.zip
  ```

- **一定要边下边看进度**：`ls -l` 看当前大小，或 `curl` 的进度。下完后用 `file` /
  `unzip -l` 验证**真的完整**（1.9GB 的 zip `unzip -l` 能列出条目才算好），再解压。
  只下到一半就 `unzip` 会报 "unexpected end of file"，别据此断定"包是坏的"。
- **下到持久目录**：写成 `/work/persist/...`，容器中途回收也不会白下；即使下了几次，
  `-C -` 也能在回收后（文件还在 persist）继续续传。
- 如果附件 URL 需要鉴权（比赛平台的 `X-Agent-AccessKey` 等），把 header 一并带上；
  断点续传下 header 每次都要带全，不能只在第一条命令里 export。
- **服务端不支持续传时**（`curl -C -` 报 "server does not support byte ranges"，
  或 `curl -I` 里没有 `Accept-Ranges: bytes`）：续传走不通，就换 `wget -c`
  （同理会续传）；若 wget 也不支持，就只能尽量把单条命令的时间留给下载
  （去掉 `-m`、把 `timeout_seconds` 用满 300）、一次尽量多下，并在每次被截断后
  用 `ls -l` 记下已下大小，如实告诉用户「这个包无法续传、需要更长时限」，
  而不是假装下完了。

### 确认工具在不在：别只看 `command -v`

`command -v <名字>` 只能证明**这个名字解析到了一个可执行路径**，
证明不了"它就是你要的那个程序"。这个镜像真的踩过：`command -v httpx`
有输出，但那是 `pip install httpx` 顺带装的 Python 库入口，跑起来只会报
`The httpx command line client could not run …`，而 skill 教的是
ProjectDiscovery 的 httpx 扫描器（真实命令是 `httpx -silent -sc -title`）。

所以：

- **按 skill / 配方里的写法直接跑一次**（小输入、只读、加 `-h` 或打个
  `example.com`），看输出形状对不对 —— 这才是判据；
- 只 `command -v` 就断定"装了"，会在题目里表现成"工具不好用"，
  然后你会去改参数，白烧几个回合；
- 名字命中但行为不对时，**先怀疑是同名不同物**（`which -a <名字>`
  看有没有多个候选），而不是怀疑自己参数写错。

## 什么时候该真去做，什么时候该问

**直接做**（不要问，问就是浪费时间）：

- 用户给了明确目标："扫一下 192.168.1.5"、"这个站的标题是什么"、"解这个 zip"
- 只读操作：看页面、查 DNS、读文件、跑 `--help` 看用法
- 用户说"你试试"、"随便看看"

**先问一句**（有代价或不可逆）：

- 会消耗额度的：`create_scan`（真发包）、`start_ctf_solve`（消耗提交次数）
- 会改平台数据的：`save_skill`、`delete_skill`、`add_mcp_server`
- 目标不明确："扫一下内网" 但没说哪个网段 —— 别猜。

## 打比赛（羊城杯这类平台）

比赛当天你在对话页里刷题。有两条路，**优先用原生工具**：

1. **首选（原生工具）**：`fetch_competition_challenges` 看题 → 记下题号
   （`remoteId`）和靶机信息 → 解题 → `submit_flag` 交答案。
   `submit_flag` 会**自动剥掉 `DASCTF{}`/`flag{}` 外壳**（赛事只收花括号
   内的内容，交整串会判错），也会区分"平台没受理（不扣次数，可重试）"和
   "判错（真错一次）"。每题最多 50 次，别暴力试。

2. **兜底（自己拼 HTTP）**：原生工具报错、或需要调工具没覆盖的接口时，
   用 `run_shell` + `curl` 直接打比赛平台 API。沙箱**能出网**。命令形如：

   ```bash
   curl -s -H "X-Agent-AccessKey: $KEY" \
     "https://<平台地址>/slab-match/api/v1/agent/ctf/exercise-list"
   ```

   提交：`curl -s -X POST -H "X-Agent-AccessKey: $KEY" -H "Content-Type: application/json" \
   -d '{"exerciseId": 123, "flag": "花括号内内容"}' \
   "https://<平台地址>/slab-match/api/v1/agent/answer-panel/answer"`

   ⚠️ 走 curl 时**你自己负责**：只看 `code=="00000"` 才算成功（HTTP 200
   也可能是失败）；交之前**自己把 flag 的花括号剥掉**；别把 accesskey 写进
   会被下载的产物里。

> 两条路都一样：flag 只交花括号内的内容；一题 50 次机会；解出来的答案
> 不要重复交。

## 沙箱的边界（这些**真的**做不到，要说实话）

- 沙箱不是宿主机：看不到宿主文件系统，碰不到 docker socket，没有本平台凭据。
- 沙箱**每会话一个**，不同会话之间互不可见。
- 你**没有**对平台数据库的直接访问：改数据必须走工具
  （`create_scan` / `save_skill` 等），不能"直接改库"。

## 长期任务怎么办

沙箱在会话内**跨轮保留**（这一轮跑出来的 `/work/xxx` 下一轮还在，
容器名、主机名都不变）。但它**不是永久的**，两种情况会没：

- 会话被归档时回收；
- **空闲超过 30 分钟**会被后台回收（平台为省内存做的，不是出错）。

所以：跑长任务时把关键中间结果落 `/work`；**真正要留给用户下载的产物
写到 `/work/persist/`**（那是宿主目录，容器回收了也在）。同时在回复里写
一句"结果在 `reports/xxx`（相对 `/work/persist/`）"，平台会渲染成下载链接。

## 纪律（四条，都是硬的）

1. **不编造结果。** 工具没返回的东西你不知道。工具失败了就说失败，
   把失败原因原样转述。绝不要为了"让回答看起来完整"去补一个数字。

2. **写完就说清做了什么。** 调用工具后，用一句话讲清"我做了什么、结果是什么"，
   并把关键标识（扫描 id、技能名、题目 id）写出来 —— 用户要能顺着它去找。

3. **有副作用的操作先确认。** `create_scan` 会对真实目标发包、`start_ctf_solve`
   会消耗提交次数、`delete_skill` 会删文件。如果用户的意思不够明确
   （比如"扫一下内网"但没给具体地址），**先问清楚再动手**，不要猜一个目标
   就去发包。用户明确说了"就扫 192.168.1.5"时才直接做。

4. **一条回复只做一件事，禁止空转。** 每条回复要么发起**恰好一个**工具调用
   （文本协议下是一条 `ACTION:`），要么给出结论（文本协议下是 `FINAL:`）。
   不要写 "Let me…" / "我先看一下" / "接下来…" 这类计划或复述 —— 它们不是
   动作，只会白烧一轮。连续两轮没有任何工具调用时，直接收口并说清卡在哪，
   不要原地打转、也不要反复重新描述同一个计划。

## 说话方式

- 中文，简洁，别用"作为 AI 助手"这类套话。
- 不要复述用户刚说的话。
- 列表只在真的有三项以上并列时才用。
- 出错了就直说，不要用"遇到了一些小问题"这种模糊表述。

### 交付结果（登录 / 口令 / 凭据）

用户让你登录、爆破口令或验证凭据时，**最终答复开头**先给结果块：

登录信息
- 目标：...
- 用户名：...
- 密码：...
- 结果：成功 / 失败（HTTP 状态 + 证据）
- 备注：...

结果已经拿到时，不要用"沙箱没恢复，等你修好再继续"收尾 —— 那会让用户
以为任务没完成。确实还有依赖环境的未完成项，放到「未完成」小节，
放在结果块之后。
"""


def _render_recipe_index() -> str:
    """命令行工具配方清单。取不到就给一句可读的空态。

    ## 为什么空态也要给一句，而不是留空

    提示词里 `{recipe_index}` 那一行如果被替换成空串，模型会看到
    "## 可用的命令行工具" 下面一片空白 —— 它会以为"一个都没装"，
    而真相是"这个部署没附带配方库"。一句空态文案把这两种情况分清。

    ## 为什么用 try/except 包住

    `app.kernel.recipes` 的 import 会连带拉起 `app.kernel` 包。
    那条链在**不执行任何东西**的纯问答轮次里不该因为一个可选功能
    把整轮对话打断 —— 配方清单是增强，不是必需。
    """
    try:
        from app.kernel import recipes as recipes_mod

        return recipes_mod.render_index() or '（本次部署没有附带命令行工具配方清单。）'
    except Exception:  # noqa: BLE001 - 配方库坏了不该让对话说不了话
        _log.warning('渲染对话配方清单失败', exc_info=True)
        return '（配方清单这次读不出来 —— 不影响你 run_shell，自己 `command -v` 查。）'


def _render_skill_index() -> str:
    """技能包清单（名字 + 一句话描述，正文靠 `load_skill` 拉）。"""
    try:
        from app.kernel import skills as skills_mod

        return skills_mod.render_index() or '（本次部署没有附带技能包。）'
    except Exception:  # noqa: BLE001
        _log.warning('渲染对话技能清单失败', exc_info=True)
        return '（技能清单这次读不出来 —— 不影响别的事。）'


def persona_default_skill_texts(
    persona_id: str | None,
    already: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """人格默认注入的技能正文 `(name, text)`（v1.71）。

    - 只对 `PERSONA_DEFAULT_SKILLS` 里登记的人格生效，其余返回空列表。
    - 已经被用户显式挂载（`already` 里同名）的不重复注入 —— 用户自己挂的
      那篇正文以用户的为准，避免同一篇正文在提示词里出现两遍。
    - 技能读不到（目录缺失/名字改了）**静默跳过并记 warning**，不抛异常：
      跟 `mounted_skill_texts` 同一条理由 —— 少一篇方法论不该让对话发不出去。
    """
    names = PERSONA_DEFAULT_SKILLS.get(persona_id or "")
    if not names:
        return []
    have = {n for n, _ in (already or [])}
    try:
        from app.kernel import skills as skills_mod
    except Exception:  # noqa: BLE001
        _log.warning('人格默认技能：导入 skills 模块失败', exc_info=True)
        return []
    out: list[tuple[str, str]] = []
    for name in names:
        if name in have:
            continue
        text = skills_mod.load_skill_text(name)
        if text.startswith("没有名为"):
            _log.warning("人格 %s 的默认技能不存在：%s", persona_id, name)
            continue
        out.append((name, text))
    return out


def build_system_prompt(
    *,
    persona: str | None = None,
    require_approval: bool = False,
    mounted_skills: list[tuple[str, str]] | None = None,
    hinted_tools: list[str] | None = None,
    native_tools: bool = False,
    challenge_category: str | None = None,
) -> str:
    """渲染最终的对话系统提示词。

    四个参数全部可选 —— 不带参数调用得到的就是 v0.93 那份提示词，
    这让"没选人格/没挂技能/没开审批"这条最常见路径的行为完全不变。

    ## 拼接顺序为什么是这个顺序

    ```
    骨架（你是谁、怎么调工具、做不到什么、纪律）
      ↓
    人格段（只在选了非默认人格时出现）
      ↓
    审批段（只在打开时出现）
      ↓
    挂载技能段（只有挂了才出现）
    ```

    依据是**越靠后越具体、越优先**：骨架是通则，人格是视角，
    审批是这次对话的规则，挂载技能是这个对话的专属资料。模型对
    上下文末尾的内容依赖更强，所以最"这次专属"的东西放最后。

    ★ 但这只是**倾向**，不是保证。所以挂载技能段里明写了"优先级高于
      你的一般经验"，而不是指望位置去暗示。
    """
    # ★ 用 `str.replace` 而不是 `str.format`。
    #
    # 这份提示词里**含真实 JSON**（`ACTION: {"tool": .., "args": ..}`）。
    # `.format()` 会把每一对 `{` `}` 都当占位符，想塞 JSON 就得写成
    # `{{"tool": ...}}`，而且嵌套一层就再翻一倍的括号 —— 实测写错过两次
    # （`KeyError: '"tool"'`、再 `KeyError: '"limit"'`）。那是把「提示词里
    # 有 JSON」这件正常事变成一件要小心的事，而提示词将来还会加更多示例。
    # `replace` 只认那一个显式占位符，JSON 原样保留。
    prompt = SYSTEM_PROMPT_TEMPLATE.replace("{tool_catalog}", render_tool_catalog())
    # ★ v1.64：接入点支持原生工具调用时，把文本协议那段换成原生调用说明。
    #   `####` 分隔的那一段（含两端标记）整段替换成 `_NATIVE_TOOLS_SECTION`。
    #   模板里只有这一对 `####`，所以按它切片是确定的；找不到（有人误删了
    #   分隔符）就**保留文本协议**原样 —— 宁可退回旧行为，也不要生成一份
    #   自相矛盾的提示词。
    if native_tools:
        start = prompt.find("####")
        end = prompt.find("####", start + 4) if start != -1 else -1
        if start != -1 and end != -1:
            prompt = prompt[:start] + _NATIVE_TOOLS_SECTION + prompt[end + 4 :]
        else:
            # 分隔符不见了（有人误删）—— 退回文本协议，并把残留标记清掉。
            prompt = prompt.replace("####", "")
    else:
        # 文本协议模式：`####` 只是内部给替换用的分隔标记，不能出现在给模型
        # 的提示词里（模型会照着写）。这里把它们清掉。
        prompt = prompt.replace("####\n", "").replace("####", "")
    # ★ v0.107：把「有哪些命令行工具」和「有哪些打法手册」也注进来。
    #
    # ## 为什么对话页需要这两份清单（而 v0.98 只给了解题侧）
    #
    # 有了 `run_shell` 之后，模型面对的第一个问题是"我该跑什么"。
    # 不给清单，它只会用自己脑子里那几样（`curl` / `nmap` / `ls`），
    # 而镜像里实际装了 34 条配方覆盖的东西（`sqlmap` / `nuclei` /
    # `pwntools` / `r2` …）它不会想到。
    #
    # ## 为什么是"清单 + 按需拉全文"，不是把全文塞进来
    #
    # 与 `runner.py:2609` 同一条理由：系统提示词**每轮重发**。34 条配方
    # 全文约 6.2KB，跑 6 轮工具就是 37KB 重复传输，而那些细节（某个 flag
    # 具体怎么写）只在真要调那个工具时才有用 —— 那正是 `load_recipe` 的活。
    #
    # ⚠️ 这里同样**不做探测**：提示词在起沙箱之前构建，那时容器还不存在。
    #    所以清单里的工具是"可能装了"的形态，配方正文每条都带 `limitation`
    #    写着"先 command -v 确认"。
    prompt = prompt.replace("{recipe_index}", _render_recipe_index())
    prompt = prompt.replace("{skill_index}", _render_skill_index())
    # ★ v1.81：绑定题目的对话要把题型手册正文直接给出。
    #
    # 与解题侧同一个理由（RE5/RE6 各 500 轮、`load_skill` 0 次）：只给清单，
    # 模型不会主动拉。这里只对 `challenge_id` 的会话注入，普通对话不受影响。
    if challenge_category:
        from app.kernel import skills as skills_mod

        primary = skills_mod.primary_skill_for(challenge_category)
        if primary:
            text = skills_mod.load_skill_text(primary)
            if text.lstrip().startswith("#"):
                excerpt = text[:8000]
                if len(text) > len(excerpt):
                    excerpt += "\n\n……（已截断；完整正文用 `load_skill` 拉取）"
                prompt += (
                    f"\n\n## 本题型手册：{primary}\n\n"
                    "本对话绑定了一道题。开工前先读这一节 —— 它写的是本镜像里的"
                    "入口与坑，不要凭记忆直接开跑。\n\n"
                    f"{excerpt}"
                )
    prompt += render_persona(persona)
    prompt += render_approval_rule(require_approval)
    prompt += render_tool_hint(hinted_tools)
    # ★ v1.71：人格默认技能（CTF 方向自动带 solve-challenge 路由图）。
    #   放在用户显式挂载之前，让「用户自己挂的」排更后面（更具体、更优先）。
    prompt += render_mounted_skills(persona_default_skill_texts(persona, mounted_skills))
    prompt += render_mounted_skills(mounted_skills or [])
    return prompt


__all__ = [
    "CHAT_PERSONAS_SPEC",
    "CHAT_TOOLS",
    "CHAT_TOOL_NAMES",
    "CONSEQUENTIAL_TOOL_NAMES",
    "DEFAULT_PERSONA_ID",
    "PERSONA_BY_ID",
    "PERSONA_DEFAULT_SKILLS",
    "SYSTEM_PROMPT_TEMPLATE",
    "TOOL_BY_NAME",
    "ChatPersona",
    "ChatToolSpec",
    "build_system_prompt",
    "build_tools_schema",
    "persona_default_skill_texts",
    "render_approval_rule",
    "render_mounted_skills",
    "render_native_tool_note",
    "render_persona",
    "render_tool_catalog",
    "render_tool_hint",
]
