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

from dataclasses import dataclass

from app.chat.models import CHAT_PERSONAS


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
            "加一条 MCP 连接。**加了就会尝试拉起它并握手** —— "
            "失败会明确告诉你为什么（找不到命令 / 握手超时 / 返回不符协议）。"
        ),
        params=(
            "name（必填，唯一）、command（必填，可执行文件）、"
            "args（可选，列表）、env（可选，对象，**凭据**）、"
            "allowedTools（可选，列表，白名单）"
        ),
        scope="admin",
        consequential=True,
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
`crypto` / `取证` / `逆向` 是 `--network none`，任何联网手法都不成立。""",
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


def build_system_prompt(
    *,
    persona: str | None = None,
    require_approval: bool = False,
    mounted_skills: list[tuple[str, str]] | None = None,
    hinted_tools: list[str] | None = None,
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
    prompt += render_persona(persona)
    prompt += render_approval_rule(require_approval)
    prompt += render_tool_hint(hinted_tools)
    prompt += render_mounted_skills(mounted_skills or [])
    return prompt


__all__ = [
    "CHAT_PERSONAS_SPEC",
    "CHAT_TOOLS",
    "CHAT_TOOL_NAMES",
    "CONSEQUENTIAL_TOOL_NAMES",
    "DEFAULT_PERSONA_ID",
    "PERSONA_BY_ID",
    "SYSTEM_PROMPT_TEMPLATE",
    "TOOL_BY_NAME",
    "ChatPersona",
    "ChatToolSpec",
    "build_system_prompt",
    "render_approval_rule",
    "render_mounted_skills",
    "render_persona",
    "render_tool_catalog",
    "render_tool_hint",
]
