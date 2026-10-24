"""`ctf2` 领域的出入参。

两个基类的分工照抄 `core/schemas.py` 的定义，不另立一套：

* `StrictModel` —— **请求体**。多传一个字段直接 400，早报早改；
* `CamelModel` —— **响应体**。多返回一个字段不该让前端解析失败。

⚠️ **本模块的任何 `*Out` 里都没有 token 字段，也永远不会有。**
`KernelAccountOut` 只有 `tokenMask`（形如 `pat_****9f2a`）——
这不是靠自觉：模型上根本没有那个字段，想漏也漏不出去。

## ★ 这里有一组必须与 `core/vocab.py` 逐字一致的字面量

`ChallengeStatusLiteral` / `SubmitModeLiteral` / `SandboxProfileLiteral`。
它们是 `findings/schemas.py` 的 `StatusLiteral` 当初漏掉两个状态、
以及 `scans/schemas.py` 的 `TargetKindLiteral` 需要一条对账测试盯着
—— 同一类隐患。所以这里配了 `tests/kernel/test_schemas.py` 的
模块级对账用例，断言它们与 `values(...)` 相等。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.core.schemas import CamelModel, StrictModel
from app.core.timeutil import IsoDateTime

# ⚠️ 与 `core.vocab.KernelChallengeStatus` 的**全部**取值一致。
# 改 vocab 必须改这里，而 `tests/kernel/test_schemas.py` 有一条对账测试盯着。
ChallengeStatusLiteral = Literal[
    "unsolved",
    "environment_ready",
    "solving",
    "candidate_ready",
    "submitting",
    "solved",
    "failed",
]

SubmissionStatusLiteral = Literal["pending", "correct", "wrong", "rejected", "failed"]

EnvironmentStatusLiteral = Literal["stopped", "starting", "ready", "error", "expired"]

SubmitModeLiteral = Literal["safe", "auto"]

#: 沙箱档案取值（v0.65 起新名为 `suvex-sandbox*`）。
#:
#: ★ 同时保留旧名 `ctf2-sandbox*`：数据库里 v0.65 之前建的行可能还是旧名，
#:   响应体若只接受新名，会在一批历史题上抛 ValidationError（表现为整个
#:   列表 500、页面全白）。校验/展示侧再各自 `normalize_sandbox_profile`
#:   映射到新名，不会把旧名漏给用户做选项。
SandboxProfileLiteral = Literal[
    "suvex-sandbox",
    "suvex-sandbox-re",
    "suvex-sandbox-ir",
    "ctf2-sandbox",
    "ctf2-sandbox-re",
    "ctf2-sandbox-ir",
]

# ⚠️ 与 `core.vocab.KernelCompetitionKind` 的**全部**取值一致。
#
# `local`（v0.43）= 本地复盘案例，不属于任何平台账号。加它的时候漏改
# 这里的症状是：`GET /ctf2/competitions` 直接 500，报
# `Input should be 'competition' or 'practice'` —— 而数据库、worker、
# 界面全都对，唯一错的是这一个联合类型。
#
# 对账测试在 `tests/kernel/test_schemas.py`（与 `ChallengeStatusLiteral`
# 的那条同族）。
CompetitionKindLiteral = Literal["competition", "practice", "local"]

# ★ 平台接入（v0.25）。与 `models.KernelPlatform` 的 CHECK 约束是**同一对** ——
#   加一个新的驱动或状态时两处都要改。CHECK 表达式建好就固化在数据库里，
#   而这两个 Literal 能改：只改这里会让"应用层放行、数据库拒绝"，
#   表现为一个看起来毫无道理的 500。
PlatformDriverLiteral = Literal["ctf2", "generic"]
PlatformStatusLiteral = Literal["building", "draft", "verified", "failed"]


# ══════════════════════════════════════════════════════════
# 账号
# ══════════════════════════════════════════════════════════


class KernelAccountBindIn(StrictModel):
    """`POST /ctf2/accounts` —— 绑定一个 CTF2 账号。

    ## ★ 为什么 `label` 有默认值而 `token` 没有

    `token` 没有默认值，因为"绑一个没有凭据的账号"不是一个有意义的动作。
    `label` 有默认值，因为用户常常只想赶紧绑上、之后再改名 ——
    而逼他先想个名字是一道纯摩擦。

    ## 关于 `token` 可能进日志

    它**不会**：请求体从来不被打印（见 `core/logging.py` 的审计结论），
    且 bind 之后这个对象就离开作用域。但如实记一条做不到的事：
    Python 的 str 不可变、无法主动清零，所以明文在内存里存活的那段时间
    无法被擦除，只能等 GC。与 `providers/service.py` 里记的那条一样。
    """

    # CTF2 的 Personal Access Token。形态校验只做"非空、长度合理"——
    # 具体格式是**平台的**约定，我们硬编码一个正则，平台一改就误伤。
    # 真正的校验是拿它去调一次 API（见 service.bind_account）。
    token: str = Field(min_length=8, max_length=512)
    label: str = Field(default="", max_length=120)


class KernelAccountOut(CamelModel):
    """一个绑定账号的投影。

    ⚠️ **没有 `token` 字段，也永远不会有。** 只有 `tokenMask` ——
    它的用途是"让用户确认绑的是哪把钥匙"，不是"把钥匙给出去"。
    """

    id: str
    label: str
    status: str
    remoteUserId: str | None
    remoteUsername: str | None
    # 平台授予的 scope 列表。前端据此提前提示"少一个权限"，
    # 而不是等用户点了启动环境才吃到 403。
    scopes: list[str]
    tokenMask: str
    hasToken: bool
    lastSyncedAt: IsoDateTime | None
    lastError: str | None
    createdAt: IsoDateTime
    updatedAt: IsoDateTime


# ══════════════════════════════════════════════════════════
# 配置（提交策略）
# ══════════════════════════════════════════════════════════


class KernelSettingsOut(CamelModel):
    """`GET /kernel/settings` —— 也是 `PUT` 的响应。"""

    submitMode: SubmitModeLiteral
    maxWrongSubmissions: int
    defaultSandboxProfile: SandboxProfileLiteral
    # ★ v0.22：解题并发配额。它**不再借用**扫描那个上限 ——
    #   借用会让两个调度器各自拿到同一个上限，实际跑出 2 倍
    #   （见 `settings/models.py` 的 `KernelConfig.max_concurrent_solves`）。
    maxConcurrentSolves: int
    # ★ v0.24：自动作业（抢首杀）。四者的取舍见
    #   `settings/models.py` 的 `KernelConfig`（自动开解单独默认关，
    #   判据是可逆性）。
    solvePollSeconds: float
    autoSyncSeconds: int
    autoPriorityNew: bool
    autoStartNew: bool
    # ★ v0.26：每题最多跑多少回合。**这个数没有全局最优** ——
    #   实测同一批题里，简单题 6 轮出结果、.NET 逆向题跑满 40 轮
    #   发出 37 条各不相同的命令仍是"没做完"。所以它是配置项。
    maxTurns: int
    # ★ v0.35：换模 fallback。默认 provider 调不动时改用哪个 provider；
    #   空串 / null = 不启用（默认）。见 `settings/models.py` 的说明。
    fallbackProviderId: str | None = None
    # ★ v0.40：铸网-2026 智能体安全场景包开关。默认 **false**。
    #   打开后 `agentsec-*` 那四类题型才被校验层放行、且命中时会在
    #   system prompt 末尾追加"判定考察点是否成立"的评测块。
    #   ⚠️ 它不是"多几种题型"，是**换一套行为纪律** —— 见
    #   `settings/models.py` 的 `KernelConfig.enable_agentsec_scenarios`。
    enableAgentsecScenarios: bool = False
    # ★ v0.41：应急响应场景包开关。同样**可选、默认 false**。
    #   打开后 `ir-*` 那三类题型才被校验层放行、且命中时会在 system
    #   prompt 末尾追加"复盘过去发生了什么"的场景块。
    #
    #   ⚠️ 与 `enableAgentsecScenarios` **各自独立**：共用开关会让
    #   一族的分类在另一族开着时通过校验，而对应 prompt 块不注入 ——
    #   分类合法、纪律没注入。理由见 `settings/models.py` 的
    #   `KernelConfig.enable_ir_scenarios`。
    enableIrScenarios: bool = False
    # ★ v0.58：多智能体编排开关。默认 **false**。打开后解题走
    #   root 调度 + 子 agent 并行执行；关着时单循环逐字节不变。
    #   ⚠️ 它改变**执行拓扑**、不改变行为纪律（与两个场景包开关不同维），
    #   但会把一次任务的 LLM 调用量放大约 N 倍 —— 见
    #   `settings/models.py` 的 `KernelConfig.enable_multi_agent`。
    enableMultiAgent: bool = False
    updatedAt: IsoDateTime


class KernelSettingsIn(StrictModel):
    """`PUT /kernel/settings`。

    ⚠️ 字段**全部必填**（与 `SandboxIn` / `ScanRetentionIn` 同一条理由）：
    配置类端点漏传一项是调用方 bug，而给默认值会让它**静默改回默认** ——
    用户以为自己只改了一项，回头发现自动提交被关掉了（或更糟：被打开了）。

    ★ 唯一带默认值的是 `fallbackProviderId`（`None` = 不启用换模）。
    它是**可空**字段，`None` 与"不传"语义完全等价，所以默认值不构成
    "替调用方做决定"。两个场景包开关不同 —— 它们没有那个等价关系，
    详见下面那段说明。

    ## 为什么这里没有"来源"字段

    `SandboxOut` 有一个 `executionSource`（标出哪几项来自环境变量默认值）。
    这里**刻意没有**：CTF2 的这几项配置**不读环境变量** ——
    见 `app/settings/models.py` 里 `KernelConfig` 的说明
    （提交策略是安全取向的选择，不该由一个部署参数静默决定）。
    没有第二个来源，就没有来源可标。
    """

    submitMode: SubmitModeLiteral
    maxWrongSubmissions: int = Field(ge=0, le=10)
    defaultSandboxProfile: SandboxProfileLiteral
    # 区间与 `vocab.MIN/MAX_CTF2_MAX_CONCURRENT_SOLVES` 是同一对数。
    # 上限 16 高于扫描的 8：比赛规则写明题量"明显超过人工手工完成能力"，
    # 解题的并发诉求天然更高。
    maxConcurrentSolves: int = Field(ge=1, le=16)
    # 区间与 `vocab.MIN/MAX_CTF2_SOLVE_POLL_SECONDS` 同一对。
    # ⚠️ 下界 0.5 是护栏而不是建议值：填 0 会让调度线程变成忙等，
    #    症状是 CPU 打满而解题**反而变慢**。
    solvePollSeconds: float = Field(ge=0.5, le=30)
    # ⚠️ **0 是合法值**（= 关闭自动同步）。不要把它并进"非法值"里：
    #    "我不想让它自动同步"是一个正当诉求（PAT 有配额、比赛已结束）。
    autoSyncSeconds: int = Field(ge=0, le=3600)
    autoPriorityNew: bool
    autoStartNew: bool
    # 区间与 `vocab.MIN/MAX_CTF2_MAX_TURNS` 是同一对数。
    # 上界 500 是"别手滑填 100000"的护栏，**不是**成本护栏 ——
    # 真正的成本该由人在现场按题权衡。
    maxTurns: int = Field(ge=5, le=500)
    # ★ v0.35：换模 fallback。**可选**（有默认值）—— 与上面"全部必填"
    #   不同，理由是它是一个**新增**字段：老的客户端不带它时应当按
    #   "不启用"处理，而不是报 422。空串与 null 都表示"不启用"。
    fallbackProviderId: str | None = Field(default=None, max_length=64)
    # ══════════════════════════════════════════════════════
    # ★ v0.40 / v0.41 的两个场景包开关：**必填，没有默认值**
    # ══════════════════════════════════════════════════════
    #
    # 它们原本各带一个 `= False`，理由是"`false` 是唯一安全的那一侧，
    # 老客户端不带它时行为与加这个功能之前完全一致"。**那个理由只在
    # 「这个功能还没被打开过」时成立**，打开之后就反过来了：
    #
    #   `PUT /kernel/settings` 是**全量提交**语义 —— `update_ctf2_settings`
    #   逐字段赋值，没传的字段按形参默认值落库。所以用户把开关打开
    #   之后，只要有任何一次保存**没有**带上这个字段，它就会被静默
    #   改回 `false`。而界面上看不出发生过什么：那一次保存改的是别的
    #   项、返回 200、页面正常刷新。
    #
    # 换句话说，默认值在这里不是"安全的兜底"，而是**静默关掉一个已经
    # 开着的功能的唯一途径**。`false` 安全，只在"用户从没打开过"时
    # 成立；而服务端无法区分"从没打开过"和"上次打开了、这次调用方
    # 忘了传"。
    #
    # ⚠️ 所以判据不是"哪个值更安全"，而是**能不能区分"没传"与"传了
    # false"**。区分不了，给默认值就是在替调用方做决定 —— 这与本类
    # 开头"字段全部必填"那条纪律是同一件事，此前把这两个列为例外错了。
    #
    # ⚠️ `fallbackProviderId` 保留默认值 `None` 不是同一种情况：它是
    # **可空**字段，`None` 与"不传"语义完全等价（都表示"不启用换模"），
    # 默认值不构成替调用方做决定。（v0.35 的历史契约，本次不动。）
    enableAgentsecScenarios: bool
    # ★ v0.41：应急响应场景包开关。**必填**，理由同上。
    enableIrScenarios: bool
    # ★ v0.58：多智能体编排开关。**必填**，理由同上（全量提交语义，
    #   给默认值等于"漏传 = 静默关掉一个已经开着的功能"）。
    enableMultiAgent: bool


# ══════════════════════════════════════════════════════════
# MCP 连接（v0.81）
# ══════════════════════════════════════════════════════════
#
# ## 为什么单独一套出入参，不塞进 `KernelSettingsIn/Out`
#
# `KernelSettingsIn` 是**全量提交**语义（漏传 = 静默改回默认），所以它的
# 每个字段都必填 —— 这是 `enableMultiAgent` 那段血泪说明的纪律。
# 但 MCP 连接是**一张清单**，而且需要两件 `KernelSettingsIn` 做不了的事：
#
#   1. **单独保存**：加一条 MCP 连接不该连带提交"提交策略是否 safe"；
#   2. **单独试连**：填完 command 后必须先知道"拉得起来吗、有哪些工具"，
#      而不是跑一次真实解题才发现 —— 那次失败不会说清原因。
#
# 所以它是独立端点 + 独立出入参。`KernelSettingsOut` 里**不出现**它：
# 设置页的四块聚合快照里，这一块有自己的加载/失败状态。

#: 单条 MCP 连接的入参。
#:
#: ⚠️ `env` **只进子进程环境**，不回显（见 `McpServerConfig.public_dict`
#: 与 `settings/models.py` 的 `mcp_servers` 说明）。所以这个类只做入参，
#: 对应的 `*Out` 里**没有** `env` 字段 —— 与 `KernelAccountOut` 不返回
#: token 是同一件事：模型上根本没有那个字段，想漏也漏不出去。
class McpServerIn(StrictModel):
    """要保存的一条 MCP 连接。

    ## 为什么 `env` 允许传、但回执里读不到

    凭据（API token、数据库口令）属于 `env`。如果回显，它就进了浏览器、
    进了前端 store、进了任何截图。而用户需要的是"我知道我配过它"，
    不是"我要看见它" —— 所以回执只给 `hasEnv: true`。
    """

    #: 连接名 = 模型在 `list_mcps`/`call_mcp` 里用的 id。必填、唯一。
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    #: 拉起 server 的可执行文件（`npx` / `python3` / 绝对路径）。
    command: str = Field(min_length=1, max_length=512)
    #: 传给 `command` 的参数。
    args: list[str] = Field(default_factory=list, max_length=64)
    #: 追加进子进程的环境变量。**不回显**。
    env: dict[str, str] = Field(default_factory=dict)
    #: ★ 保留已保存的 `env`，忽略本次提交的 `env` 字段（v0.82）。
    #:
    #: ## 为什么必须有这个开关（否则编辑一次就会丢凭据）
    #:
    #: `env` 是**只进不出**的（见 `McpServerOut` 没有该字段）。于是前端
    #: 打开"编辑"时**拿不到原值**，输入框只能留空。而 `PUT /kernel/mcp-servers`
    #: 是全量替换语义 —— 前端把空 `env` 提交上来，后端就真的把凭据清掉了。
    #:
    #: 那个失效形状正是本项目最忌讳的一类：**用户只改了一下 notes，
    #: 保存成功、界面正常，而凭据已经没了**。而它要等到下一次真的调用
    #: 那条连接时才会暴露（认证失败），届时没人会想到是三天前那次编辑。
    #:
    #: 所以给前端一个明确的"我这次不碰 env"的表示法，而不是让它
    #: 在"清空"和"保留"之间靠猜。命名用 `preserveEnv` 而不是
    #: `envUnchanged`：前者说的是后端要做什么，后者说的是一个模糊的状态。
    preserveEnv: bool = False
    #: 用途说明（进 `list_mcps` 的描述位，帮模型判断该不该用）。
    notes: str = Field(default="", max_length=500)
    #: 白名单：只允许调这些工具名（`None` = 全放）。
    #:
    #: ⚠️ **必须手写 camelCase**，与 `McpServerOut` 同一条理由。
    #:
    #: 入参是 `StrictModel`（`extra="forbid"`），而 `CamelModel` 的
    #: `alias_generator` 只给了 `validation_alias`、**没有** `serialization_alias`，
    #: 所以字段名写成 `allowed_tools` 时，**请求体里就必须也叫 `allowed_tools`**。
    #:
    #: 这个错误实测抓到过（2026-10-23）：前端按契约发 `allowedTools`，
    #: 后端回 422「不认识的字段」，于是**新增/编辑 MCP 连接在界面上根本
    #: 存不进去** —— 而报错说的是字段名，看起来像前端传错了，实际是
    #: 两端对字段命名的口径不一致。`preserveEnv` 一开始就写成 camelCase，
    #: 所以没踩到；这一行是漏改的那个。
    allowedTools: list[str] | None = None
    #: ★ v0.83：单次工具调用的超时（秒）。`None` = 用内核默认（60 秒）。
    #:
    #: ## 为什么必须让它可配（实测踩到的）
    #:
    #: 全局写死 60 秒时，慢工具**必然失败**，而失败长得像"MCP 连接坏了"
    #: （模型看到的是一句读取超时），于是它会去改配置、换参数，
    #: 而不是意识到"这个操作本来就慢"。实测两类：
    #:
    #:   · 需要先批量预处理再执行的：光展开上万个模板文件就要 **102 秒**；
    #:   · 网段扫描：一个 /24 约 **85 秒**。
    #:
    #: ⚠️ 上限 1800 秒是"别手滑填 99999"的护栏，不是建议值。
    #:    **它应当与 server 自己的内部超时对齐**：外层比内层短 → 外层先超时、
    #:    内层还在跑；外层比内层长 → 内层先把子进程杀掉并返回错误，外层再等
    #:    也没用。哪个 server 有内部超时，就在它的文档里写清并成对配置。
    callTimeout: float | None = Field(default=None, gt=0, le=1800)


class McpServerOut(CamelModel):
    """已保存的一条 MCP 连接（**不含 `env`**）。

    ## ⚠️ camelCase 字段名是**手写**的，这不是多余劳动

    `CamelModel` 的 `alias_generator` 只给了 `validation_alias`
    （入方向：允许用 snake_case 构造），**没有** `serialization_alias`
    —— 所以字段名写成 `allowed_tools` 时，JSON 里就是 `allowed_tools`。

    而本项目的契约（`docs/api-contract.md`）与全部既有出入参都是
    camelCase（`submitMode` / `hasEnv`…）。v0.81 这里写成了 snake_case，
    于是 `GET /kernel/mcp-servers` 回的键与契约**对不上**：前端按
    `allowedTools` 读会拿到 `undefined`，而 `undefined` 在界面上表现为
    "白名单是空的"——一个看起来完全正常、实际读错了字段的状态。

    ⚠️ 与 `KernelSettingsOut` 里那些**本来就是 camelCase 的字段名**
    （`submitMode` 等）做法一致：手写 camelCase 名字，靠对账测试钉住。
    """

    name: str
    command: str
    args: list[str] = []
    notes: str = ""
    #: 白名单：只允许调这些工具名（`None` = 全放）。
    allowedTools: list[str] | None = None
    #: 是否配了 `env` —— 只说"有没有"，不说是**什么**。
    hasEnv: bool = False
    #: ★ v0.83：单次工具调用超时（秒）。`None` = 用内核默认 60 秒。
    #:
    #: ★ 这个字段**必须**回显（它不像 `env` 那样是凭据）：部署者需要
    #:   能看到这条连接实际生效的超时是多少。不回显的话，
    #:   "我明明配了 300 秒"与"其实还在用 60 秒"在界面上无法区分。
    callTimeout: float | None = None


class McpServersIn(StrictModel):
    """`PUT /kernel/mcp-servers` 的整体清单（全量替换）。

    ⚠️ **全量替换**而不是增量：清单只有几条，逐个 PATCH 会引入"顺序"
    "删除"两类新状态。前端把整份清单提交上来，后端整份写回 —— 这与
    设置页其他面板的"这一块整体保存"是同一个心智模型。
    """

    servers: list[McpServerIn] = Field(default_factory=list, max_length=32)


class McpServersOut(CamelModel):
    """`GET /kernel/mcp-servers` —— 已保存的 MCP 连接清单（**不含 `env`**）。

    ⚠️ 这里刻意**不做**任何"某个具体工具是否就绪"的判定。
    曾经有一对 `gogoReady` / `gogoReason` 字段，是给某个特定外部工具定制的；
    那个工具已移除，而"某工具是否就绪"这件事现在统一由
    `GET /kernel/tools` 的 `ready` / `reason` 逐条回答 ——
    判据按 `requires_connection` 查，不再写死某个连接名。
    """

    servers: list[McpServerOut] = []


class McpServerTestIn(StrictModel):
    """`POST /kernel/mcp-servers/test` —— 试连**尚未保存**的一条配置。

    ★ 接受未保存的草稿：用户填完就想知道对不对，让他先保存再试连
    会把"验证配置"变成"先污染配置再回滚"。
    """

    server: McpServerIn


class McpServerTestOut(CamelModel):
    """试连结果。

    ⚠️ 失败时 `ok=false` **且** `reason` 带可执行的原因（"command 不存在"
    "握手超时"…），而不是一个笼统的"连接失败"。这个区分很重要：
    前者用户能自己改，后者只能来问我们。
    """

    ok: bool
    #: 失败原因（中文）。成功时为空串。
    reason: str = ""
    #: server 报上来的工具名清单（成功时才有）。
    tools: list[str] = []
    #: server 自报的名字与版本（成功时才有）。
    #:
    #: ⚠️ camelCase 手写，理由见 `McpServerOut` 的说明 —— 这一组三个类
    #: 在 v0.81 都写成了 snake_case，与契约对不上（前端读 `serverName`
    #: 会拿到 `undefined`，界面显示"未知"而实际是读错了键）。
    serverName: str = ""
    serverVersion: str = ""
    #: 握手耗时（毫秒）。用来回答"这个 server 是慢还是挂了"。
    elapsedMs: int = 0


# ══════════════════════════════════════════════════════════
# 内置工具清单与开关（v0.82）
# ══════════════════════════════════════════════════════════
#
# ## 为什么这一组与 MCP 那一组分开
#
# MCP 连接是**用户添加的外部服务**（可以任意多条、带凭据、要试连）；
# 内置工具是**代码里固定的 13 个**（不可增删、无凭据、只需要开关）。
# 两者的 CRUD 语义完全不同，混成一个端点会让请求体里出现
# "一半字段对另一半不适用"的形状 —— 那种接口最终的归宿是
# 一大串 `if kind == ...`。
#
# ## 为什么 `enabled` 与 `disabled` 两个方向都要
#
# 只给 `disabled` 会让前端必须自己算差集才能渲染一个开关列表，而
# 差集算法是"前端猜后端"的经典入口（后端加一个工具，前端不显示它，
# 而界面上看不出少了什么）。所以回执里把**每一个工具的当前状态**
# 直接给出，前端不需要做集合运算。


class KernelToolOut(CamelModel):
    """一个内置工具在界面上的完整样子。

    ⚠️ `name` 是**标识符**，界面要用等宽字体原样显示 —— 模型在
    `ACTION: {"tool": "<name>"}` 里写的就是它，所以它不能被翻译。
    `label` 才是给人看的中文名。
    """

    name: str
    label: str
    group: str
    summary: str
    params: str = ""
    #: ★ v0.86：**默认参数下它做不到什么**（空串 = 默认即全功能）。
    #:
    #: 它的用途不是"写文档"，而是防一类**结论被写错**的失真：某个工具在默认
    #: 参数下只做基础那一层，返回"命中 0"；而模型会把那个 0 读成
    #: "目标本来就没有"。实测过同一目标、同一工具、只补一个参数就从 0 → 2 命中。
    #:
    #: 所以这句话要给**模型**看（进提示词）也要给**人**看（进界面）。
    defaultLimitation: str = ""
    #: 当前是否启用（= 不在 disabled_tools 里）。
    enabled: bool = True
    #: 关掉它会明显废掉主力能力。界面据此给出更重的提示。
    core: bool = False
    #: 依赖的 MCP 连接名（非空表示"配了那条连接才真的能用"）。
    requiresConnection: str = ""
    #: 那条依赖是否**真的就绪**（按 `requiresConnection` 逐条查，不写死连接名）。
    #:
    #: ⚠️ 这个字段与 `enabled` 是**两件事**，界面必须分开显示：
    #:   · `enabled=false` → 你把它关了（去开关里打开）；
    #:   · `enabled=true` 但 `ready=false` → 它没配好（去 MCP 连接里配）。
    #: 合成一个"不可用"会让用户不知道去哪修。
    ready: bool = True
    #: 未就绪的原因（中文，可直接展示）。
    reason: str = ""


class KernelToolsOut(CamelModel):
    """`GET /kernel/tools` —— 内置工具清单 + 当前开关 + 逐工具就绪状态。"""

    tools: list[KernelToolOut] = []
    #: 分组顺序（界面按它排栏，不从 tools 里二次推导 —— 同 `TOOL_GROUPS`）。
    groups: list[str] = []
    #: 被显式关掉的工具名（前端"重置为全开"要用它判断有没有改动）。
    disabled: list[str] = []


class KernelToolsIn(StrictModel):
    """`PUT /kernel/tools` —— 整份替换"关掉的工具"清单。

    ## 为什么收"关掉的"而不是"启用的"

    与数据库列同一条取舍（见 `settings/models.py::KernelConfig.disabled_tools`）：
    这样前端**不需要知道**全部工具名就能表达"我只要关这一个"，
    而且后端新增工具时老客户端提交上来的清单不会把它误关掉。

    ## ⚠️ 全量替换语义

    与 `PUT /kernel/mcp-servers` 一致：提交什么就是什么。空列表 = 全开。
    部分更新（PATCH 语义）会引入"这条没传 = 保持原样还是关掉"的歧义，
    而那正是本项目在 `KernelSettingsIn` 上记过的教训。
    """

    disabled: list[str] = Field(default_factory=list, max_length=64)


# ══════════════════════════════════════════════════════════
# 平台接入（v0.25）
# ══════════════════════════════════════════════════════════


class KernelPlatformOut(CamelModel):
    """一个已接入的平台。

    ⚠️ `id` **可空**，而且 `None` 有确切含义：内置的 CTF2 **不是数据库里
    的一行**（见 `models.KernelPlatform` 那段"为什么不插初始数据"）。
    列表接口会合成一个 `id=None, source='builtin'` 的条目给界面展示 ——
    那是展示层的事，不该沉淀成一行需要维护的数据。
    """

    id: str | None
    slug: str
    displayName: str
    baseUrl: str
    driver: PlatformDriverLiteral
    source: Literal["builtin", "generated"]
    status: PlatformStatusLiteral
    #: 失败原因（中文，可直接展示）。成功时为 `None`。
    lastError: str | None = None
    #: 生成跑了几轮。界面显示"试了 2 次"用。
    attempts: int = 0
    #: ★ 界面用它决定"要不要继续轮询" —— 比让前端自己比较
    #: `status == 'building'` 更明确：以后加状态时不会漏改前端。
    isBuilding: bool = False
    #: ★ 当前账号**正在用**这个平台。内置条目在这一项上为真是常态
    #: （没配过任何平台时，账号用的就是内置驱动）。
    active: bool = False
    #: 模型最后一次的原始输出。**只在失败时有意义** —— 它是排查
    #: "为什么它总也生成不对"的唯一证据。
    lastRaw: str | None = None
    createdAt: IsoDateTime | None = None
    updatedAt: IsoDateTime | None = None


class KernelPlatformDraftIn(StrictModel):
    """`POST /ctf2/platforms/draft` —— 让 agent 去识别一个平台。

    ⚠️ 这里**收明文 key**。它只在内存里过一遍（后台线程用完即弃），
    **不落库** —— 见 `models.KernelPlatform` 的说明。要真正启用这个平台，
    用户还得再点一次"启用"，那时才写进账号表的密文列。
    """

    baseUrl: str = Field(min_length=4, max_length=500)
    accessKey: str = Field(min_length=1, max_length=500)
    #: 接口文档原文（Markdown / OpenAPI / curl 示例都行）。
    #: 可空 —— 没有文档时只能靠实测推断，成功率会低一些，但仍然能试。
    document: str = Field(default="", max_length=200_000)


class KernelPlatformActivateIn(StrictModel):
    """`POST /ctf2/platforms/{id}/activate`。"""

    accessKey: str = Field(min_length=1, max_length=500)
    label: str = Field(default="", max_length=120)


# ══════════════════════════════════════════════════════════
# 赛事 / 题目 / 环境
# ══════════════════════════════════════════════════════════


class KernelCompetitionOut(CamelModel):
    id: str
    # ★ v0.43：可空 —— `kind == 'local'` 的本地复盘案例不属于任何平台账号。
    accountId: str | None
    remoteId: str
    kind: CompetitionKindLiteral
    name: str
    # 平台侧状态原文。**不枚举**（那是别人家的取值空间，见 models 的说明）。
    remoteStatus: str
    startAt: IsoDateTime | None
    endAt: IsoDateTime | None
    challengeCount: int
    syncedAt: IsoDateTime


class KernelEnvironmentOut(CamelModel):
    """题目环境。

    ⚠️ `accessUrl` 只在 `status == 'ready'` 时非空 —— 数据库层有一条
    CHECK 约束（`access_url_only_when_ready`）守着这件事，而前端
    `EnvironmentStatus` 组件再判一次。两处都写是刻意的：一个非 ready
    状态下出现的可点链接会被用户报告成"你们的链接坏了"。
    """

    id: str
    challengeId: str
    status: EnvironmentStatusLiteral
    sandboxProfile: SandboxProfileLiteral
    accessUrl: str | None
    accessUrls: list[str]
    containerId: str | None
    pollCount: int
    startedAt: IsoDateTime | None
    readyAt: IsoDateTime | None
    expiresAt: IsoDateTime | None
    lastError: str | None


class KernelChallengeOut(CamelModel):
    """一道题。

    ## 三个"由服务端算好"的字段

    `remainingWrongSubmissions` / `wrongSubmissionCount` / `maxWrongSubmissions`
    三个都给，而不是只给前两个让前端做减法。理由见
    `KernelChallenge.remaining_wrong_submissions` 的 docstring：
    `max - count` 这条规则有两份实现迟早会分叉，而分叉的症状是
    "界面显示还剩 2 次、实际提交时被拒"。

    ## ★ `candidateFlag` 与 `confirmedFlag` **是两个字段**

    一个 `flag` 字段装不下"它猜的"与"平台认的"这两种意思 ——
    `candidate_ready` 状态下它出现，`solved` 之后它还在，
    而界面无法据此判断该不该让人确认。见 `KernelChallenge` 里
    那两列的说明：分开之后这个字段组合本身就能回答
    「现在有没有东西等着我确认」。
    """

    id: str
    competitionId: str
    remoteId: str
    stageId: str | None
    title: str
    category: str
    description: str | None
    attachments: list[dict[str, Any]]
    score: int | None

    status: ChallengeStatusLiteral
    # LLM 提出、**等人工确认**的候选（`candidate_ready` 时有值）。
    candidateFlag: str | None
    # **平台判过对**的 flag（`solved` 时有值）。两者不会同时有值。
    confirmedFlag: str | None
    remainingWrongSubmissions: int
    wrongSubmissionCount: int
    maxWrongSubmissions: int
    candidateCount: int

    sandboxProfile: SandboxProfileLiteral
    scanId: str | None
    solvedAt: IsoDateTime | None
    lastError: str | None
    # 最新的那一条环境（可空：还没起过）。列表页会**批量**填充它，
    # 避免前端为每道题单独请求一次（那是 N+1）。
    environment: KernelEnvironmentOut | None
    # 调度优先级。**越大越先做**，默认 0。
    #
    # ⚠️ 队列里同级按 FIFO（`updated_at`），所以**全 0 时的行为与
    #    v0.23 之前完全一致** —— 不下发这个字段也不会改变任何排序。
    priority: int
    createdAt: IsoDateTime
    updatedAt: IsoDateTime


class KernelRemoteCompetitionOut(CamelModel):
    """`GET /ctf2/competitions/remote` —— **平台侧**的赛事列表（不落库）。

    ## 为什么它不是 `KernelCompetitionOut`

    那个是**本地**行（有 id、kind、challenge_count、同步时间…），
    而这个是"平台说它有哪些赛事"的即时快照 —— 它可能包含我们
    还没同步过的赛事。两者的字段集合本来就不一样，硬合成一个会让
    前端在两种语义之间猜（`id` 到底是本地 id 还是平台 id？）。

    这里的 `remoteId` 是**平台 id**，前端把它原样回传给
    `POST /ctf2/sync` 的 `competitionIds`。
    """

    remoteId: str
    name: str
    # 起止时间原样下发（平台给的是字符串）。**不解析成时间戳**：
    # 解析失败的后果是"这场赛事的时间显示成空白"，而空白不会告诉用户
    # "我们没读懂它的格式"—— 原样传至少前端还能显示点什么。
    startAt: str | None
    endAt: str | None
    # 平台侧的状态原文（不枚举：那是别人家的取值空间）。
    status: str


class KernelPullIn(StrictModel):
    """`POST /ctf2/challenges/pull` —— 按链接/ID 拉**一道**题。

    ★ 为什么要有这条路：CTF2 的开放 API **没有**"列出练习集题目"的接口
    （实测 11 条候选路径全部 `resource=route`），而"按 id 取单题详情"
    **是活的**。所以拿到题目的唯一可行方式是用户把平台上的题目链接粘过来，
    我们按 id 精确拉取 —— 一次一道，但确定能成。
    """

    #: 任意文本：整条链接、或一段 UUID 都行（服务端自己抠 id）。
    input: str = Field(min_length=1, max_length=2000)


# ══════════════════════════════════════════════════════════
# 本地复盘任务（v0.43，应急响应）
# ══════════════════════════════════════════════════════════


class IrTaskIn(StrictModel):
    """`POST /ctf2/ir/tasks` —— 从界面建一个**本地复盘任务**。

    ## 为什么这个入口必须存在

    在这之前，本地复盘案例只能由 `scripts/run_ir_closed_loop.py` 直接
    写库创建 —— 也就是说**用户无法从界面上发起一次应急响应复盘**。
    侧栏放一个入口却建不出任务，那个入口就只是个"这里本该有功能"的
    提示牌。

    ## 字段为什么这么少

    只有"叫什么"和"想查什么"。其余全部由服务端定：

    · `category` 固定 `ir-root-cause` —— 建任务时用户还不知道这次
      复盘会走到哪（是根因、时间线还是范围），让他先选一个等于让
      他猜。分类可以在跑完之后按实际结论调整，而**建任务时选错**
      会让提示词从第一轮就偏。
    · `sandbox_profile` 由 `profile_for_category` 从分类推出来，
      不由用户挑 —— 挑错的表现是"命令找不到"，而不是一句看得懂的错。
    · `competition` 由服务端找（或建）那个隐藏的本地批次。

    ## 附件不在这里

    附件走**先暂存、后认领**（与扫描附件同一条路子，见
    `scans/uploads.py` 的 `store_staged` / `claim`）。原因是同一个：
    任务一创建就会被调度器毫秒级认领，"建完再传"的窗口不存在。
    所以流程是 `POST /scans/attachments/staging` → 拿到 stagingId
    → 连同本结构的字段一起 `POST /ctf2/ir/tasks` → 认领。
    """

    #: 任务名。会显示在列表与报告标题里，也参与工单文件名。
    title: str = Field(min_length=1, max_length=200)
    #: 想查什么 —— 会原样进提示词的"本次复盘目标"那一段。
    #: 可空：空的时候提示词用"还原这次入侵的完整过程并给出处置建议"。
    brief: str = Field(default="", max_length=2000)
    #: 附件暂存批次 id（`POST /scans/attachments/staging` 的返回值）。
    #: 可空 —— 但空的时候这次复盘没有证据可看，服务端会照建不误
    #: 并在回执里说明，而不是拒绝（"先想起来再补附件"是常见流程）。
    attachmentStagingId: str | None = None


class IrTaskOut(CamelModel):
    """建任务的结果：直接给出可以立刻开跑的那道题。"""

    challenge: KernelChallengeOut
    #: 认领到的附件（名字与字节数）。为空说明这次没传附件。
    #:
    #: ⚠️ 形状与 `KernelChallengeOut.attachments` 用同一个
    #:   （`list[dict[str, Any]]`）而不是另立一个类型：它们描述的是
    #:   同一件事（这道题有哪些附件），两处各定一个类只会分叉。
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    #: 人类可读的提示，比如"附件暂存批次已过期，本次没有证据包"。
    notice: str | None = None


class KernelPullOut(CamelModel):
    """拉取结果。"""

    challenge: KernelChallengeOut
    #: 这次是新建还是更新已有的（重复拉同一道题时是更新）。
    created: bool
    #: 认出来的题目 id（回显给用户，便于核对是不是他想要的那道）。
    challengeId: str
    #: 命中的练习集名字（让用户知道这道题被挂在哪儿了）。
    competitionName: str


class KernelSnapshotOut(CamelModel):
    """`GET /ctf2/snapshot` —— 一次拿到账号 + 赛事 + 题目。

    为什么不拆成三个请求：这三层在任何一次有意义的展示里都会**同时**
    被需要，而分三次请求会让界面出现"题目出来了、账号还没出来"的
    中间闪烁 —— 那个闪烁看起来就像 bug。
    """

    account: KernelAccountOut | None
    competition: KernelCompetitionOut | None
    challenges: list[KernelChallengeOut]


# ══════════════════════════════════════════════════════════
# 同步
# ══════════════════════════════════════════════════════════


class KernelSyncIn(StrictModel):
    """`POST /ctf2/sync` —— 同步赛事/题目。

    `competitionIds` 为空 = 同步练习集（平台没有"列出全部赛事"的端点，
    见 client 的说明）。

    **上限 50**：一次同步是一串外部请求，没有上限时
    "同步全部"会变成一个几十秒的请求，而它在超时后是**部分成功**的 ——
    那是最难解释的状态。
    """

    competitionIds: list[str] = Field(default_factory=list, max_length=50)


class KernelSyncOut(CamelModel):
    """同步的账。

    ★ 三个计数**必须分开**：`challengesCreated` / `challengesUpdated`。
    只给一个"同步了 N 个"会让用户无法判断"我改了题面它有没有同步过来" ——
    而那正是他会反复点同步的原因（每次都得等一遍完整往返）。
    """

    competitions: int
    challengesCreated: int
    challengesUpdated: int
    # 逐条失败的原因。**不因为一条失败就整次失败**（见 service.sync），
    # 但失败的必须能被看见 —— 否则"少了几道题"是无声的。
    errors: list[str]
    # 附件是**独立的一笔账**：题面同步成功而附件全失败是一种真实状态，
    # 用户需要据此判断"解题失败是不是因为 agent 根本没拿到附件"。
    attachmentsDownloaded: int
    attachmentsSkipped: int
    attachmentsFailed: int
    # 一句话结论，可直接展示。
    message: str


# ══════════════════════════════════════════════════════════
# 解题 / 提交
# ══════════════════════════════════════════════════════════


class KernelSolveIn(StrictModel):
    """`POST /ctf2/challenges/{id}/solve` —— 起环境 + 调度 LLM 解题。"""

    # 提交策略。不填就是设置里的值。
    mode: SubmitModeLiteral | None = None
    # 本题的沙箱镜像。不填则由 `category` 推导（见 runner.profile_for_category）
    # —— 推导规则是**一处**，不散在调用点上。
    sandboxProfile: SandboxProfileLiteral | None = None
    # 覆盖本题的错误提交上限。不填沿用题目行上已存的值。
    #
    # ⚠️ 允许覆盖但**不允许无限**：上限被 service 夹在
    # `MAX_WRONG_SUBMISSIONS_CEILING` 之内，见那里的说明。
    maxWrongSubmissions: int | None = Field(default=None, ge=0, le=10)
    # 传给 agent 的额外提示（比如"题目的提示说考点在反调试"）。
    # 与 `scans.brief` 同一条口径：**上限 8000**，且不进命令行。
    brief: str | None = Field(default=None, max_length=8000)
    # ★ 续解（v0.26）：接着上一次的进度跑，而不是从零开始。
    #
    # ## 为什么它值得一个参数，而不是"自动"
    #
    # 实测（2026-10-01，`[FlareOn3]GUI`）：一道 .NET 逆向题跑满 40 轮、
    # 发出 **37 条各不相同的命令**（一路分析到 `decryptBuffer` /
    # `DecompressBuffer` 的资源流结构），最后因为预算耗尽而停。
    #
    # 那 37 步是真金白银换来的，而"再解一次"会让模型把它们再做一遍。
    # 但**反过来也不行**：用户点那个按钮时，有时恰恰是想换个思路重来 ——
    # 替他决定"接着上次走"会让"重来"这个动作消失。所以做成显式选择。
    #
    # ⚠️ 找不到上一次的过程文件时**不是错误**：退回从头开始并记一条日志。
    resume: bool = False


class KernelPriorityIn(StrictModel):
    """`PUT /ctf2/challenges/{id}/priority`。

    只有一个字段，但仍然用 `StrictModel`：多余字段会被拒，
    而"我以为传了 priority、其实键名拼错了"会静默不生效。
    """

    priority: int


class KernelPrioritiesByScoreIn(StrictModel):
    """`POST /ctf2/priorities/by-score` —— 按分值批量设优先级。

    ⚠️ `competitionId` 可空表示"全部排队中的题"。做成可选而不是必填：
        比赛时通常只有一个赛事，强制指定只会多一次选择。

    ⚠️ `limit` 有上限：一次改几千行的优先级会在事务里拿一把长锁，
        而排队中的题本来就只有几十道。给个护栏。
    """

    competitionId: str | None = None
    limit: int = Field(default=50, ge=1, le=500)


class KernelPriorityBatchOut(CamelModel):
    """`POST /ctf2/priorities/by-score` 的响应。

    只回"改了几道"，不回被改的题目列表：调用方紧接着就会重新拉
    列表（那本来就是它要的数据），把整份列表塞进这个响应等于让
    同一份数据在一个往返里走两遍。

    ⚠️ `changed` 可能小于请求里的 `limit` —— 排队中的题就那么几道。
        **不回 0 就报错**：0 是一个合法且有用的结果，它说明"当前
        没有排队中的题"，而那是用户该知道的事实。
    """

    changed: int


class KernelSolveOut(CamelModel):
    """`POST /ctf2/challenges/{id}/solve` 的响应。

    ## ★ 它**不同步等解题结束**

    解题要跑几分钟到几十分钟（LLM 在沙箱里干活）。同步等它意味着
    一个几十分钟的 HTTP 请求 —— 中间任何一层（浏览器、反代、uvicorn
    的 keep-alive）都会先超时，而超时之后用户**不知道任务到底起没起**。

    所以这个端点只做"受理"：起环境、建记录、把任务丢给后台，
    然后立刻返回。真正的进展走 **events 流**（已定设计决策 7：
    前端 Activity 页面不改代码就能看到）。

    ⚠️ 因此它**不会**返回 flag。它返回的是"受理凭据"。
    """

    challenge: KernelChallengeOut
    environmentId: str | None
    scanId: str | None
    # 一句话说明当前状态，可直接展示。与报告 verdict 同一个套路。
    message: str


class KernelSubmitIn(StrictModel):
    """`POST /ctf2/challenges/{id}/submit` —— **人工确认**提交候选 flag。

    ## ★ 这个端点是 safe 模式的落点

    从这里进来的提交**已经经过人的确认**（有人点了按钮）。`mode`
    字段在这里记录的是"这次提交属于哪条策略路径"，用于审计与统计，
    **不改变是否提交** —— 提交已经决定了。

    允许 `flag` 与题目上存的候选不同：用户经常需要手工修正
    （模型可能把尾随的引号带进来、或把两个候选拼在一起）。
    """

    flag: str = Field(min_length=1, max_length=512)
    mode: SubmitModeLiteral | None = None


class KernelSubmitOut(CamelModel):
    """提交的即时结果。

    ⚠️ `message` 是**后端写的**中文原话（平台回显或本地拦截原因），
    前端**原样展示、不加工** —— 它常常含平台的具体提示
    （"flag 格式应为 flag{...}"），而那正是用户唯一能采取行动的线索。
    """

    status: SubmissionStatusLiteral
    message: str
    challenge: KernelChallengeOut


# ══════════════════════════════════════════════════════════
# 队列
# ══════════════════════════════════════════════════════════


class KernelQueueOut(CamelModel):
    """`GET /ctf2/queue` —— 调度器状态。

    与 `GET /scans/queue` 同一条思路：它回答一个前端**必须**能回答的
    问题：「为什么我点了开始解题，却一直排在队列里？」
    答案通常是"并发上限满了"，而那需要能看到当前在跑几个、上限几个。
    只给一个"排队中"的状态，用户会以为是坏了。
    """

    running: int
    limit: int
    queued: int
    # 正在跑的题目 id 列表（前端可以把那几张卡标出来）
    challenges: list[str]


class KernelWriteupOut(CamelModel):
    """`GET /ctf2/challenges/{id}/writeup` —— 一道题的题解（v0.36）。

    ## ★ 为什么 CTF2 要自己的题解端点

    CTF2 解题**不走 strix**（由 `runner.solve_in_sandbox` 自己的循环驱动），
    所以 `GET /scans/{id}/writeup` 对它**永远**找不到报告文件 ——
    而它报的"产物目录可能被手工清理过"在误导用户。详见契约 §3.19.5。

    ## ⚠️ `markdown` **含 flag 原文**

    与 `KernelSolveNote`（方法卡）相反：卡片进**另一道题**的 prompt，
    必须白名单式脱敏；而题解是**赛后提交给人看**的，flag 正是要展示的。
    判据是这份内容的**去向**，不是"flag 是不是秘密"。
    """

    challengeId: str
    title: str
    category: str
    #: 产出时这道题解没解出。未解出的题解同样保留（"试过什么"也是材料）。
    solved: bool
    markdown: str
    #: 下载用的文件名（前端 `downloadMarkdown` 直接拿去用）。
    filename: str
    #: 出自哪一次尝试。可空（尝试记录被清掉时 `SET NULL`）。
    attemptId: str | None = None
    turns: int
    elapsedSec: int | None = None
    generatedAt: IsoDateTime
    updatedAt: IsoDateTime


class KernelWorkOrderOut(CamelModel):
    """`GET /ctf2/challenges/{id}/workorder` —— 处置建议工单（v0.41，会话 C）。

    ## ★ 这是一个**独立端点**，不塞进题解里

    题解是一份叙事文档（人会通读），工单是**待办清单**（人要逐条批）。
    两者的用法不同：工单会被复制进工单系统、打印出来批、作为附件发出去。
    所以它得能被**单独下载**，而不是让人从一篇 markdown 里手工摘。

    ## ⚠️ `markdown` 里没有任何「已执行」的动作

    这是设计约束不是文案选择：本系统**没有**执行通道，工单只到"建议"
    为止。文件头第一句就是否定句（见 `workorder.render_workorders`）。
    """

    challengeId: str
    title: str
    #: 工单全文（markdown）。四段齐全的在前、缺段的标注缺什么。
    markdown: str
    #: 下载用的文件名。
    filename: str
    #: 工单条数。0 是**正常结果**（本次复盘没有需要处置的事项），
    #: 与"取不到工单"是两件事 —— 后者是 409。
    count: int


class KernelUsageEntryOut(CamelModel):
    """**一次 LLM 调用**的用量（"细分到每一次任务"最直接的视图）。"""

    turn: int
    model: str
    providerLabel: str
    promptTokens: int
    completionTokens: int
    totalTokens: int
    reasoningTokens: int
    #: ★ 接入点**到底给没给**用量。
    #:
    #: False 时上面四个 token 值都是 0，而那个 0 **不代表没消耗** ——
    #: 所以前端必须分开显示"0"与"未记录"。
    usageReported: bool
    at: IsoDateTime


class KernelUsageByModelOut(CamelModel):
    """按模型分组的用量。

    换模（v0.35 的 fallback）之后**同一次解题**会用两个模型，
    而"换模前后各花多少、值不值"是这里最有价值的读数。
    """

    model: str
    providerLabel: str
    calls: int
    totalTokens: int


class KernelUsageByChallengeOut(CamelModel):
    """按题分组的用量（总览页看"哪道题最烧钱"）。"""

    challengeId: str
    title: str
    calls: int
    totalTokens: int


class KernelUsageByAttemptOut(CamelModel):
    """★ 按**每次解题**分组的用量（v0.37）。

    ## 为什么在 `byChallenge` 之外还要这个

    需求原话是"细分到**每个任务**的消耗"。而"任务"在 CTF2 里指的是
    **一次解题**，不是"一道题" —— 一道题可以解很多次。

    实测（2026-10-15）：`[ACTF2020 新生赛]Upload` 有 **4 次** attempt。
    只按题聚合会把"哪一次跑得贵、哪一次在兜圈子"糊成一行，
    而那正是复盘时最想知道的东西。

    ## 为什么不需要新表

    `ctf2_solve_attempts` 从 v0.28 起就带 `mode` / `created_at` /
    `llm_model` / `max_turns`，所以这只是**多一个聚合维度**，
    不是新数据。本字段纯增，`byModel` / `byChallenge` 保持不变
    （前端已有的渲染不受影响）。

    ## ⚠️ `attemptId` 可能是空串

    v0.36 之前的解题**没有 usage 记录**，所以不会出现在这里；
    但 usage 行的 `attempt_id` 是 NOT NULL 外键，理论上不会空。
    留 `attemptId` 而非只给 `challengeId`：前端要靠它跳回那一次解题。
    """

    attemptId: str
    challengeId: str
    title: str
    #: 这次解题用的提交策略（`safe` / `auto`）—— 与"花了多少"同屏才看得出代价。
    mode: str
    #: 这次解题用的模型快照。换模后同一道题的不同 attempt 会不同。
    model: str
    calls: int
    totalTokens: int
    #: 这次解题的受理时刻（= 这次"任务"的起点）。
    startedAt: IsoDateTime


class KernelTokenUsageOut(CamelModel):
    """`GET /ctf2/usage` —— token 消耗统计（v0.36）。

    ## ★★ `calls` 与 `recordedCalls` 必须分开看

    · `calls` = 调用次数（**永远准确**，从行数数得出来）；
    · `recordedCalls` = 其中**接入点真的返回了用量**的次数。

    两者不等时 `totalTokens` 是**下限**（有几次没算进去）。
    把"没记录"显示成"消耗 0"会与"没跑过"混淆，那正是这一对字段
    要消除的歧义 —— 与 `hasKey`/`apiKey` 分两列是同一条纪律。

    ## `since`：统计从哪一刻开始

    v0.36 **之前**的解题没有 usage 记录。不给这个字段的话，用户会
    以为"总共就花了这么点"，而实际是**没记**。所以界面要显示它。
    """

    calls: int
    recordedCalls: int
    promptTokens: int
    completionTokens: int
    #: ★ 它是 `total_tokens` 与 `prompt+completion` 里较大者之和
    #: （有的接入点只给分项），所以与逐题相加能对上。
    totalTokens: int
    byModel: list[KernelUsageByModelOut]
    byChallenge: list[KernelUsageByChallengeOut]
    #: ★ v0.37：按**每次解题**分组 —— 需求要的"细分到每个任务"。
    #: 纯增字段，`byModel` / `byChallenge` 语义不变。
    byAttempt: list[KernelUsageByAttemptOut] = Field(default_factory=list)
    #: 最早一条记录的时间。为空表示一条都没有。
    since: IsoDateTime | None = None


__all__ = [
    "ChallengeStatusLiteral",
    "CompetitionKindLiteral",
    "KernelAccountBindIn",
    "KernelAccountOut",
    "KernelChallengeOut",
    "KernelCompetitionOut",
    "KernelEnvironmentOut",
    "KernelQueueOut",
    "KernelPullIn",
    "KernelPullOut",
    "KernelRemoteCompetitionOut",
    "KernelSettingsIn",
    "KernelSettingsOut",
    "KernelSnapshotOut",
    "KernelSolveIn",
    "KernelSolveOut",
    "KernelSubmitIn",
    "KernelSubmitOut",
    "KernelSyncIn",
    "KernelSyncOut",
    "KernelTokenUsageOut",
    "KernelUsageByChallengeOut",
    "KernelUsageByModelOut",
    "KernelUsageEntryOut",
    "KernelWriteupOut",
    "EnvironmentStatusLiteral",
    "IrTaskIn",
    "IrTaskOut",
    "PlatformDriverLiteral",
    "PlatformStatusLiteral",
    "SandboxProfileLiteral",
    "SubmissionStatusLiteral",
    "SubmitModeLiteral",
]
