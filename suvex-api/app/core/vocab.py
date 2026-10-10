"""契约词汇表：所有枚举取值与状态流转表的**唯一实现处**。

放在一个文件里是刻意的。这些取值是前后端的共同语言，它们的**事实来源**
是前端 `src/data/*.ts`（契约 §2 反复强调「后端照抄命名，不要另起一套风格」）。
集中在一处的好处是：契约改了，改这一个文件；有人怀疑漂移了，看这一个文件。

⚠️ **`AgentRole` 与 `AgentPhase` 是两套值，不要合并**（契约 §2.4 专门论证过）：
`recon` 在两边同名，但 `scanner` vs `scan`、`exploiter` vs `exploit`、
`verifier` vs `verify`、`reporter` vs `report` 拼写不同。
一个 `exploiter` 角色的 agent 完全可能产生一个 `scan` 阶段的事件 ——
共用一个字段会让前端两处标签必有一处显示错。

⚠️ **`verification` 与 `status` 是两个轴，不是一件事**（契约 §2.2）：
一条漏洞可以是「已用 PoC 证明存在（verified），但判定不影响业务（false_positive）」。
表结构上必须是两列 —— 合成一列，这个真实的争议场景就无处表达。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


# 排序权重：前端按严重度降序排（`lib/finding-query.ts`），
# 而字符串序是 c/h/i/l/m 这种无意义顺序，必须在应用层映射。
# 数值越大越严重。
SEVERITY_RANK: Final[dict[str, int]] = {
    Severity.CRITICAL: 5,
    Severity.HIGH: 4,
    Severity.MEDIUM: 3,
    Severity.LOW: 2,
    Severity.INFO: 1,
}


class ScanStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


# 只有这两个状态可以终止（契约 §3.3）。放在这里而不是 service 里，
# 因为「能不能终止」是状态机的属性，不是某个函数的局部判断。
ABORTABLE_SCAN_STATUSES: Final[frozenset[str]] = frozenset({ScanStatus.QUEUED, ScanStatus.RUNNING})

# 终态。`durationSec` 只在终态有值 —— running 时必须是 null 而不是 0，
# 前端排序逻辑依赖这个区分（契约 §2.1）。
TERMINAL_SCAN_STATUSES: Final[frozenset[str]] = frozenset(
    {ScanStatus.COMPLETED, ScanStatus.FAILED, ScanStatus.ABORTED}
)


class TargetKind(StrEnum):
    """目标的**形态** —— 它决定"怎么测"。

    ⚠️ 加取值时必须同时改三处，否则会静默分叉（本项目在 `findings.status`
    上真踩过：改了枚举、漏了 CHECK 约束，于是新状态写不进去）：
      1. 这里；
      2. `scans/schemas.py` 的 `TargetKindLiteral`；
      3. **一条 alembic 迁移**更新 `scans.target_kind_valid` 这个 CHECK
         （`--autogenerate` **不比对约束内容**，不会替你发现）。
    前端 `data/scan.ts` 的 `TargetKind` 是第四处。
    """

    URL = "url"
    REPO = "repo"
    PATH = "path"
    # ★ 任务 15：裸 TCP 服务（Pwn / 网络 Misc / Crypto 预言机）。
    #
    # 为什么必须是**独立的一类**而不是塞进 `url`：它的"测法"根本不同 ——
    # 没有 HTTP 层，要发的是二进制协议。把它当成 url 会让预检去探
    # http/https（对裸端口必然全失败），也会让 agent 以为目标是网站。
    SERVICE = "service"
    # ★ 任务 17：**只有附件、没有目标地址**（逆向 / 纯文件 Misc / 密码学）。
    #
    # ## 为什么它必须存在，而不是"让用户随便填个占位地址"
    #
    # CTF 里有一大类题目根本没有可连的东西：给你一个 ELF、一个流量包、
    # 一段密文，flag 就藏在**文件本身**里。而向导里「目标地址」是必填的 ——
    # 于是这类题**建不出来**，用户只能去填一个假地址，然后看着 agent
    # 对着一个不存在的主机白跑一轮。那不是"绕过去"，那是把一次必然失败
    # 的扫描伪装成一次正常的扫描。
    #
    # ## ★ 它在执行层**没有对应物** —— 这是刻意的
    #
    # `artifact` 不是一个"新的测法"。后端把附件解压成一个目录，然后
    # **按 `path`（本地代码路径）的形态**交给执行后端（见
    # `service.build_spec`）。也就是说：
    #
    #   · strix 那边什么都不用教它 —— `local_code` 本来就是它支持的类型；
    #   · `runner.py` 一行都不用改 —— `--target <目录>` 早已是走通的路。
    #
    # 与 `service` 那一类对比就看得很清楚：`service` 必须新增一类，是因为
    # strix **没有** TCP 这个类型（会被误判成 web_application，还得写一段
    # 指令去纠正它）；而 `artifact` 只是**我们自己**在"谁来准备那个目录"
    # 上的差别 —— 用户传附件、我们来解压，而不是用户手工铺一个目录。
    #
    # 换句话说：`artifact` 是**建扫描时**的形态，不是**执行时**的形态。
    # 这正是不必动执行层的原因。
    ARTIFACT = "artifact"


class ScanGoal(StrEnum):
    """这次扫描**要达成什么**（任务 12）。

    ## ★ 为什么它是独立于 `TargetKind` 的一个维度

    `TargetKind` 回答的是「目标是**什么形态**」（一个网站 / 一个仓库 /
    一个本地目录），它决定**怎么测**（黑盒发包还是白盒读代码）。

    而 `ScanGoal` 回答的是「**怎样算成功**」。两者正交，不能合并：

    · 一个 CTF 靶机是 `url` 形态，扫法和普通网站**完全一样** ——
      区别只在于"必须拿到 flag 才算解出"。
    · 同一个 `url` 目标，可以是"例行找漏洞"，也可以是"验证上个版本的
      修复有没有效"。**测法相同、判据不同。**

    合并进 `TargetKind` 会让"目标形态"与"任务目标"互相污染：
    `ctf` 既不是一种形态（它就是个网址），也没说清扫法有什么不同。

    ## 取值

    · `findVulns`（默认）—— 例行渗透测试：找出问题、定级、给修复建议。
    · `captureFlag` —— CTF 夺旗：**核心判据是拿到 flag**，
      过程记录是次要的。这会显著改变 agent 的取舍（见 `runner.py`
      里往 instruction 注入的那段说明）。
    · `verifyFix` —— 复测：目标是确认**已知的**那几条问题修没修好，
      而不是再发现一堆新问题。没有这个取值的话，用户复测时会被
      一堆无关的新发现淹掉，而他要的答案是"那条修了没有"。
    """

    FIND_VULNS = "findVulns"
    CAPTURE_FLAG = "captureFlag"
    VERIFY_FIX = "verifyFix"


class SkillId(StrEnum):
    WEB = "web"
    API = "api"
    CONTAINER = "container"
    CLOUD = "cloud"
    DEPENDENCIES = "dependencies"
    SECRETS = "secrets"
    # ★ 任务 20：裸 TCP 服务的协议分析（Pwn / 网络 Misc / Crypto 预言机）。
    #
    # ## 为什么必须新增一个技能，而不是把某个已有技能挂到 `service` 上
    #
    # `service`（裸 TCP）加进来时**漏了技能映射** —— 前六个技能里没有一个
    # 的 `applicableKinds` 含 `service`，于是向导第 2 步「检测范围」
    # 六项全部置灰、一项都勾不了，而下一步又要求"至少勾一项"：
    # **整个目标类型是一条死路**，用户卡在那里出不去。
    #
    # ⚠️ 这个坑 `artifact` 当初已经踩过一次，注释里也写了
    #    「否则用户在第 2 步勾不出任何东西，整个目标类型就是一条死路」——
    #    `service` 又踩了一遍。所以这里不只是补一个名字，见下。
    #
    # 那为什么不干脆给 `web` 或 `api` 加上 `service`？因为它们映射出去的
    # 侧重是「Web 应用层漏洞（注入、XSS、越权、SSRF）」「API 接口（认证、
    # 授权、参数校验）」—— 而裸 TCP 上**没有 HTTP 层、没有接口描述**，
    # 那句话会把 agent 往一个不存在的方向带。技能的意义就是**改变 agent 的
    # 侧重**（见 `scans/runner.py` 的 `SKILL_FOCUS`），配错了比不配更糟。
    #
    # 所以补的是一个**语义上真的存在**的能力：对着一个裸端口做协议逆向、
    # 构造二进制输入、定位溢出与逻辑缺陷。这正是 `TCP_TARGET_CLAUSE`
    # 里描述的那类题目（Pwn / 网络 Misc / Crypto 预言机）需要的。
    PROTOCOL = "protocol"


class ProviderKind(StrEnum):
    CLOUD = "cloud"
    LOCAL = "local"


class FindingCategory(StrEnum):
    INJECTION = "injection"
    BROKEN_ACCESS_CONTROL = "broken_access_control"
    AUTH = "auth"
    XSS = "xss"
    SSRF = "ssrf"
    MISCONFIGURATION = "misconfiguration"
    INFO_DISCLOSURE = "info_disclosure"
    CRYPTO = "crypto"


class Verification(StrEnum):
    """轴一：证据强度。**由 agent 产出**，人不改它。"""

    NONE = "none"
    PARTIAL = "partial"
    VERIFIED = "verified"


class FindingStatus(StrEnum):
    """轴二：处置状态。**由人工流转**，agent 不改它。"""

    NEW = "new"
    CONFIRMED = "confirmed"
    FIXED = "fixed"
    FALSE_POSITIVE = "false_positive"
    # ── 任务 10 新增：把"修复完成"与"确认关闭"分开 ──
    # 这两个状态让流程有了收尾：修完不等于事情结束，还得有人确认它真的好了。
    RETESTING = "retesting"
    CLOSED = "closed"


# 契约 §2.3：这是**业务规则**，不是 UI 细节。
# 后端必须用同一张表校验；非法流转返回 400 VALIDATION_FAILED。
# 不要依赖前端不显示非法按钮 —— 那是双保险里的第二道，不是唯一一道。
FINDING_STATUS_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    FindingStatus.NEW: frozenset({FindingStatus.CONFIRMED, FindingStatus.FALSE_POSITIVE}),
    FindingStatus.CONFIRMED: frozenset(
        {FindingStatus.FIXED, FindingStatus.FALSE_POSITIVE, FindingStatus.NEW}
    ),
    # 已修复有**三个**出口（任务 10 扩展）：
    #   · `retesting` —— 提交复测，走严谨流程；
    #   · `closed`    —— 直接关闭，用于一眼就能确认修好的情况；
    #   · `confirmed` —— 退回已确认。
    #
    # 为什么保留"直连关闭"：需求原文是「已修复 → 复测中 → 已关闭
    # （**如需要**复测环节）」。"如需要"三个字说明复测不该是唯一路径 ——
    # 强制经过它的代价是一条明显修好的漏洞（比如改了个响应头）也要多走
    # 一次流转，用户会开始乱点。`retesting` 的语义是"**正在**验证修复效果"，
    # 它是一个进行中的标记，不是一个审批节点。
    FindingStatus.FIXED: frozenset(
        {
            FindingStatus.RETESTING,
            FindingStatus.CLOSED,
            FindingStatus.CONFIRMED,
        }
    ),
    # 复测：通过则关闭，不通过则退回已确认。
    #
    # 不能直接判误报 —— 沿用下面 `false_positive` 那段的原则：
    # 一条已经"修复"过的漏洞如果要变成误报，必须先退回确认，
    # 让"修复"这个已经发生的动作保持可追溯。
    FindingStatus.RETESTING: frozenset({FindingStatus.CLOSED, FindingStatus.CONFIRMED}),
    # ★ 已关闭**可以重新打开**。
    #
    # 漏洞会复发，修复也会不彻底。没有回路的终态会逼用户把"重新打开"
    # 记成一条新记录 —— 而那会切断历史，"这条漏洞反复出现过几次"
    # 就再也查不出来了。
    FindingStatus.CLOSED: frozenset({FindingStatus.CONFIRMED}),
    # 已修复只能「退回已确认」，不能直接判误报 ——
    # 一条修复过的漏洞如果其实是误报，应该先退回确认，让「修复」这个动作可追溯。
    FindingStatus.FALSE_POSITIVE: frozenset({FindingStatus.NEW, FindingStatus.CONFIRMED}),
}


class TimelineActor(StrEnum):
    AGENT = "agent"
    USER = "user"


class TimelineKind(StrEnum):
    DETECTED = "detected"
    POC_GENERATED = "poc_generated"
    VERIFIED = "verified"
    STATUS_CHANGED = "status_changed"
    NOTE = "note"
    # 指派（任务 10 P2）。**独立的一类，不混进 `status_changed`** ——
    # 时间线上"谁把它交给谁"和"状态怎么变的"是两个不同的问题，
    # 而混在一起以后就没法只筛出指派历史（比如复盘"这条漏洞换过几次手"）。
    ASSIGNED = "assigned"


class AgentRole(StrEnum):
    """agent **长期承担的角色**（名册上显示「信息收集」）。"""

    RECON = "recon"
    SCANNER = "scanner"
    EXPLOITER = "exploiter"
    VERIFIER = "verifier"
    REPORTER = "reporter"


class AgentStatus(StrEnum):
    RUNNING = "running"
    IDLE = "idle"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"


class AgentPhase(StrEnum):
    """**某个事件所处的阶段**（活动流里显示「利用中」）。与 AgentRole 是两套值。"""

    RECON = "recon"
    SCAN = "scan"
    EXPLOIT = "exploit"
    VERIFY = "verify"
    REPORT = "report"


class ActivityEventType(StrEnum):
    """完整事件的 8 种类型（契约 §4.3）。

    注意 Overview 的投影只取其中 6 种（少了 `reasoning` 和 `failed`）——
    但后端**只发完整事件**，投影由前端派生。理由：投影是展示层视图模型，
    让后端产出它等于把「一行摘要怎么截」的规则搬到后端，
    UI 一改摘要就要动后端（契约 §2.5）。
    """

    TOOL_CALL = "tool_call"
    REASONING = "reasoning"
    DISCOVERY = "discovery"
    POC_GENERATED = "poc_generated"
    VERIFIED = "verified"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"


class EventDetailKind(StrEnum):
    """`detail` 的判别式（契约 §2.6）。取值与事件类型同名，但它们是两个概念：

    `type` 是**这一行怎么渲染**（图标、颜色），`detail.kind` 是**载荷长什么样**。
    目前一一对应，但保持分开 —— 将来若出现「同一个类型两种载荷」，
    合并过的字段就没法表达。
    """

    TOOL_CALL = "tool_call"
    REASONING = "reasoning"
    DISCOVERY = "discovery"
    POC_GENERATED = "poc_generated"
    VERIFIED = "verified"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"


class TokenScope(StrEnum):
    READ = "read"
    SCAN = "scan"
    ADMIN = "admin"


class NotifyChannelKind(StrEnum):
    EMAIL = "email"
    WEBHOOK = "webhook"
    SLACK = "slack"


class SandboxStatus(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


class SandboxEngine(StrEnum):
    DOCKER = "docker"


class ScanMode(StrEnum):
    """传给 strix 的 `--scan-mode` —— 扫描深度。

    三档是 strix 自己定义的，不另立一套：

    * `quick` —— 明确"跳过穷举、只打高价值面"；
    * `standard` —— 结构化方法，覆盖与深度平衡；
    * `deep` —— "尽可能彻底"，代价是更长的运行时间与更高的费用。

    默认取 `standard`（见 `core/config.py` 的说明）：把最贵的选项设成默认
    等于替用户做了花钱的决定。
    """

    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


class ReportFormat(StrEnum):
    PDF = "pdf"
    MARKDOWN = "markdown"


class ReportScopeKind(StrEnum):
    SCAN = "scan"
    TARGET = "target"
    RANGE = "range"
    # ★ 按**选中的那几条漏洞**出报告（任务 10 P5）。
    #
    # 它打通的是"漏洞发现"与"报告"两个模块：用户在漏洞页勾了 5 条，
    # 点「生成报告」直接带着它们跳到报告页，而不是让他去报告页重新勾一遍。
    #
    # ⚠️ 与另外三种 kind 有一处结构差异：成员是**一串 id**，不是一列能装下的。
    # 所以 `reports` 表上**没有**对应的 scope 列 —— id 列表存在既有的
    # `report_findings` 关联表里（那本来就是"这份报告冻结了哪些漏洞"）。
    FINDINGS = "findings"


class SlaStartFrom(StrEnum):
    """SLA 从哪个时刻起算（任务 10 P3）。

    按发现时间起算是常见做法，但它有个已知的不公平：**确认得晚的团队
    会被罚** —— 漏洞在队列里躺了一周，之后才开始算它的修复期。
    按确认时间起算对那类团队更合理，但会让人有动机**拖着不确认**。

    两种都有道理，所以做成配置而不是替用户决定。
    """

    DISCOVERED = "discovered"
    CONFIRMED = "confirmed"


class AuditAction(StrEnum):
    """审计日志记录的动作（契约 §3.9，任务 8）。

    ⚠️ **只记"有后果"的操作**，不记读。理由在契约 §3.9 的注里：
    审计表要保留的年限远长于日志，把每一次读都记进去，
    等于用"可观测性"换一个必然的容量问题 —— 而读操作本来就没有可追责的后果。

    这六个的共同点是：**它们改变了系统状态或把数据带出了系统**。
    `report.download` 属于后者 —— 报告一旦被下载就不受控了，
    "谁把哪一份带出去了"是事后必须能回答的问题。
    """

    SCAN_CREATE = "scan.create"
    SCAN_RERUN = "scan.rerun"
    # 续跑（任务 16）。与重跑分开记：两者都是"又发了一次包"，
    # 但续跑会**接着上次的会话**跑，审计上要能区分"从零开始"与"接着跑"。
    SCAN_RESUME = "scan.resume"
    SCAN_ABORT = "scan.abort"
    FINDING_STATUS_CHANGE = "finding.status_change"
    FINDING_ASSIGN = "finding.assign"
    # ── 处理人名单（任务 10 P2）──
    # 它决定"漏洞归谁"，而"谁把人加进来/谁停用了他"是事后追溯职责边界时
    # 必须能回答的问题。
    MEMBER_CREATE = "member.create"
    MEMBER_UPDATE = "member.update"
    MEMBER_DELETE = "member.delete"
    REPORT_DOWNLOAD = "report.download"
    REPORT_DELETE = "report.delete"
    # v0.13（任务 9）：归档与**永久删除**。
    # 归档要留痕是因为它是"把东西从默认视图里移走"—— 事后要能回答
    # "那次扫描怎么不见了"。而永久删除是**不可逆**的，必须有痕。
    SCAN_ARCHIVE = "scan.archive"
    SCAN_PURGE = "scan.purge"
    # ── v0.18（任务 10 P6）：漏洞的删除与附件 ──
    #
    # 软删除**也要留痕**：它把条目从默认视图里移走，事后要能回答
    # "那条漏洞怎么不见了" —— 与 `scan.archive` 同一条理由。
    # 永久删除是不可逆的，必须有痕（同 `scan.purge`）。
    #
    # 附件留痕的理由与读操作不同：上传**把数据带进了系统**，
    # 而附件可能含敏感信息（内网截图、凭据抓包）。"谁在什么时候
    # 往这条记录里放了什么"是事后必须能回答的问题。
    FINDING_DELETE = "finding.delete"
    FINDING_RESTORE = "finding.restore"
    FINDING_PURGE = "finding.purge"
    FINDING_ATTACH = "finding.attach"
    FINDING_ATTACH_DELETE = "finding.attach_delete"
    # ── v0.19（任务 12）：任务的背景说明与题目附件 ──
    #
    # 附件要留痕的理由与读操作不同：上传**把数据带进了系统**，
    # 而它会被送进沙箱交给会执行任意命令的 agent ——
    # "谁在什么时候把哪个文件交给了哪次扫描"必须可回答。
    SCAN_ATTACH = "scan.attach"
    SCAN_ATTACH_DELETE = "scan.attach_delete"
    # ── v0.20（任务 17）：CTF2 平台对接 ──
    #
    # ★ 这一组里**每一条都能对应一个"事后必须能回答的问题"**，
    #   而不是"能记就记"：
    #
    # · `ACCOUNT_BIND` / `ACCOUNT_UNBIND`：PAT 是一把代表用户去操作
    #   外部平台的钥匙。"谁在什么时候把这把钥匙放进系统"与"谁撤掉了它"
    #   是安全事件复盘的第一问（与 `finding.attach` 同一条理由，
    #   但更强 —— 附件只能被读，凭据能被用来发包）。
    #   ⛔ 记录里**只放账号标签与 id，绝不放 token**（`record()` 还会
    #   再过一次 `redact`，但那是最后一道，不是第一道）。
    # · `SYNC`：一次同步会以我们的身份对平台发出几十次请求，并写入本地。
    # · `SOLVE_START` / `SOLVE_CANCEL`：起一个环境并在容器里执行任意命令，
    #   这些动作有真实代价（平台额度、CPU）。
    # · `SUBMIT`：**把 flag 交给外部平台**。CTF 平台的错误提交次数
    #   通常有限且不可再生 —— 这是本组里唯一不可逆的一条。
    # · `SETTINGS_UPDATE`：`safe → auto` 是拆掉"提交前必须人工确认"
    #   这块刹车片，改它本身就是一次有后果的决定。
    #
    # ⚠️ **刻意不记** `release_environment`：它幂等、可重建、没有不可逆
    #   后果（见本类的规矩：只记有后果的操作）。加进来只会让审计表
    #   里多出一堆"我又点了一次释放"的噪音，而噪音会让真正的记录难找。
    CTF2_ACCOUNT_BIND = "ctf2.account_bind"
    CTF2_ACCOUNT_UNBIND = "ctf2.account_unbind"
    CTF2_SYNC = "ctf2.sync"
    CTF2_SOLVE_START = "ctf2.solve_start"
    CTF2_SOLVE_CANCEL = "ctf2.solve_cancel"
    CTF2_SUBMIT = "ctf2.submit"
    CTF2_SETTINGS_UPDATE = "ctf2.settings_update"
    # ── 平台接入（v0.25）────────────────────────────────────
    #
    # ★ 四个动作全部留痕，因为它们的共同后果是"**服务端以后要去打哪个
    #   地址**"。那与"改一个提交策略"不同：它改变的是数据从哪来。
    CTF2_PLATFORM_DRAFT = "ctf2.platform_draft"
    CTF2_PLATFORM_ACTIVATE = "ctf2.platform_activate"
    CTF2_PLATFORM_USE_BUILTIN = "ctf2.platform_use_builtin"
    CTF2_PLATFORM_DELETE = "ctf2.platform_delete"


# 动作 → 可直接展示的中文标签。由 `audit.service.action_label()` 使用
# （`GET /audit` 的读接口把标签下发给调用方）。
#
# ⚠️ 这里的注释曾经写着"与前端 `AUDIT_ACTION_LABEL` 逐字一致（跨仓库对账点）"。
# **那是错的**：核实过，`suvex-web/src` 里没有任何审计展示页，也没有
# 那张表（grep `AUDIT_ACTION_LABEL` / `audit` 全无命中）。所以它现在
# **不是**一个跨仓库对账点 —— 把它当成对账点会让人去改一个不存在的地方。
#
# 它真正的守卫是 `tests/test_audit.py::test_every_action_has_a_display_label`：
# 漏一个标签**不会报错**（`action_label` 回退成原始动作串），
# 于是那一行在界面上显示成 `ctf2.account_bind` 而没人会当它是 bug。
AUDIT_ACTION_LABELS: Final[dict[str, str]] = {
    AuditAction.SCAN_CREATE: "发起扫描",
    AuditAction.SCAN_RERUN: "重跑扫描",
    AuditAction.SCAN_RESUME: "续跑扫描",
    AuditAction.SCAN_ABORT: "终止扫描",
    AuditAction.FINDING_STATUS_CHANGE: "变更漏洞状态",
    AuditAction.FINDING_ASSIGN: "指派漏洞负责人",
    AuditAction.MEMBER_CREATE: "新增处理人",
    AuditAction.MEMBER_UPDATE: "更新处理人",
    AuditAction.MEMBER_DELETE: "删除处理人",
    AuditAction.REPORT_DOWNLOAD: "下载报告",
    AuditAction.REPORT_DELETE: "删除报告",
    AuditAction.SCAN_ARCHIVE: "归档扫描",
    AuditAction.SCAN_PURGE: "永久删除扫描",
    AuditAction.FINDING_DELETE: "删除漏洞",
    AuditAction.FINDING_RESTORE: "恢复漏洞",
    AuditAction.FINDING_PURGE: "永久删除漏洞",
    AuditAction.FINDING_ATTACH: "上传附件",
    AuditAction.FINDING_ATTACH_DELETE: "删除附件",
    AuditAction.SCAN_ATTACH: "上传任务附件",
    AuditAction.SCAN_ATTACH_DELETE: "删除任务附件",
    AuditAction.CTF2_ACCOUNT_BIND: "绑定 CTF2 账号",
    AuditAction.CTF2_ACCOUNT_UNBIND: "解绑 CTF2 账号",
    AuditAction.CTF2_SYNC: "同步 CTF2 赛事与题目",
    AuditAction.CTF2_SOLVE_START: "发起 CTF2 解题",
    AuditAction.CTF2_SOLVE_CANCEL: "取消 CTF2 解题",
    AuditAction.CTF2_SUBMIT: "提交 CTF2 flag",
    AuditAction.CTF2_SETTINGS_UPDATE: "更新 CTF2 配置",
    AuditAction.CTF2_PLATFORM_DRAFT: "发起平台自动识别",
    AuditAction.CTF2_PLATFORM_ACTIVATE: "启用一个平台",
    AuditAction.CTF2_PLATFORM_USE_BUILTIN: "切回内置 CTF2",
    AuditAction.CTF2_PLATFORM_DELETE: "删除平台配置",
}


def values(enum_cls: type[StrEnum]) -> tuple[str, ...]:
    """给 CHECK 约束与 OpenAPI 枚举用。"""
    return tuple(member.value for member in enum_cls)


# ══════════════════════════════════════════════════════════
# CTF2 平台对接（app/ctf2/，v0.20）
# ══════════════════════════════════════════════════════════
#
# ⚠️ 这一段里的每个枚举**都有一处必须同步改的地方**，与 `TargetKind`
# 的 docstring 里那张清单同一条规矩：
#   1. 这里；
#   2. `ctf2/schemas.py` 的对应 Literal（有对账测试盯着）；
#   3. **一条 alembic 迁移**更新 `ctf2_*` 表上的 CHECK 约束；
#   4. `ctf2/models.py` 里 `values()` 生成的模型层约束。
# 第 3 条最容易漏：`--autogenerate` 不比对 CHECK 的**内容**，
# 本项目在 `findings.status` 上已经真踩过一次（迁移 `b1f7c0a25e38`）。
#
# ★ 另外还有第五处，是 CTF2 特有的：**前端 `src/data/ctf2.ts`**。
#   那边的标签表键集合必须与这里逐字一致，否则状态在界面上显示成空白。
#   对账由 `scripts/verify_ctf2_parity.py` 做（机器比对，不靠人眼）。


class Ctf2ChallengeStatus(StrEnum):
    """一道 CTF2 题目在**我们这边**的推进状态。

    ## ★ 为什么不直接抄 CTF2 的题目状态

    CTF2 侧的题目状态回答的是「这个用户在**平台**上做没做出来」——
    它只有"未解出 / 已解出"两种，因为平台不关心你是自己在做还是
    在这个系统里做。而我们要回答的是「**我们的流水线**走到哪一步了」：
    环境起没起来、LLM 在不在解、候选 flag 有没有、提交成不成。

    把两者合成一个字段，会出现「平台说未解出、而我们正在跑」这种
    无法表达的状态 —— 而它恰恰是最需要显示的中间态。

    ## 取值

    · `unsolved` —— 刚同步下来，什么都还没做。
    · `environment_ready` —— 题目环境已起（`access_url` 可用），还没开解。
    · `solving` —— 已受理：在队列里、或 LLM 正在沙箱里跑。
    · `candidate_ready` —— **已产出候选 flag，等人工确认（safe 模式）**。
      这是 safe 模式（默认）的核心状态：它把"要不要提交"这个
      **不可撤销的外部动作**停在一个必须由人推一下的位置。
    · `submitting` —— 正在提交（含重试）。单独一档而不是并进
      `solving`：提交请求可能重复送达，界面必须能显示"正在提交"，
      否则用户会重复点。
    · `solved` —— 平台确认 flag 正确。
    · `failed` —— 环境起不来、或错误提交次数用尽、或沙箱异常退出。
    """

    UNSOLVED = "unsolved"
    ENVIRONMENT_READY = "environment_ready"
    SOLVING = "solving"
    CANDIDATE_READY = "candidate_ready"
    SUBMITTING = "submitting"
    SOLVED = "solved"
    FAILED = "failed"


# 可以往里推的状态（"能不能再解一次"）。放在 vocab 里因为它是**状态机的属性**，
# 不是某个 service 函数的局部判断 —— 与 `ABORTABLE_SCAN_STATUSES` 同一条理由。
#
# ⚠️ `solved` **不在**里面：已经解出的题再解一次没有意义，
# 而允许它会让"重复提交 flag"变成一条可走通的路径 —— CTF 平台的
# 重复提交要计入错误次数的（有些平台直接判违规），那是在拿用户账号冒险。
RESOLVABLE_CTF2_CHALLENGE_STATUSES: Final[frozenset[str]] = frozenset(
    {
        Ctf2ChallengeStatus.UNSOLVED,
        Ctf2ChallengeStatus.ENVIRONMENT_READY,
        Ctf2ChallengeStatus.CANDIDATE_READY,
        Ctf2ChallengeStatus.FAILED,
    }
)

# 终态。`solved` 是成功终态，`failed` 是失败终态（可由人工重置回 `unsolved`）。
TERMINAL_CTF2_CHALLENGE_STATUSES: Final[frozenset[str]] = frozenset(
    {Ctf2ChallengeStatus.SOLVED, Ctf2ChallengeStatus.FAILED}
)


class Ctf2SubmissionStatus(StrEnum):
    """一次 flag 提交的结果。

    ## ★ `rejected` 与 `wrong` 是两件事，必须分开

    · `wrong` —— 请求**真的到了平台**，平台判定这个 flag 不对。
      这件事有外部后果：它消耗错误提交额度。
    · `rejected` —— **我们自己在本地就拦下了**，请求根本没发出去
      （超出该题错误提交上限、flag 形状不符、或 safe 模式下无人确认）。
      它不消耗任何外部额度。

    合并的代价是实打实的：`rejected` 如果记成 `wrong`，
    "这道题我还剩几次机会"会被本地拦截白白扣掉 —— 而用户看到的是
    次数莫名其妙变少，且无法解释。

    · `pending` —— 请求已发出，还没拿到最终判定（网络中断或进程重启）。
      **这个状态必须存在**：宁可留一条待确认的记录让人去看，
      也不要把它猜成 `failed`（那会误导成"没提交过"，用户就会再提交一次）。
    · `failed` —— 传输层失败（超时、5xx 用尽重试）。与 `pending` 的区别
      是"我们确定请求没有生效"。
    """

    PENDING = "pending"
    CORRECT = "correct"
    WRONG = "wrong"
    REJECTED = "rejected"
    FAILED = "failed"


# 会**消耗平台错误提交额度**的状态。计数上限只该盯这几个 ——
# 见上面 `rejected` 的说明。
CTF2_CREDITED_WRONG_STATUSES: Final[frozenset[str]] = frozenset(
    {Ctf2SubmissionStatus.WRONG}
)


class Ctf2EnvironmentStatus(StrEnum):
    """题目环境（容器）的生命周期。

    ⚠️ `expired` 与 `error` 分开：CTF2 侧的环境**有租期**，
    到期后 `access_url` 会变成连不上的死地址。那是正常老化，
    不是故障 —— 补救动作是"重新启动环境"，而 `error` 的补救动作是
    "看日志、可能是镜像或平台侧的问题"。混成一档，界面就只能给一句
    含糊的"环境异常"，用户不知道该点重启还是该找人。
    """

    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    ERROR = "error"
    EXPIRED = "expired"


class Ctf2SubmitMode(StrEnum):
    """提交策略（已定设计决策 4）。

    ## ★ 默认必须是 `safe`，且这是安全取向不是体验取向

    flag 的提交是**不可撤销的外部动作**，而且有**错误次数上限** ——
    烧完额度这道题就只能等下一年（或换账号）。让一个 LLM 在无人确认的
    情况下连续对外提交，赌的是"它猜的都对"，而它猜错是常态。

    所以 `safe` 是默认，`auto` 必须在设置里**显式打开**，且即便打开
    也仍受每题错误提交次数上限约束 ——
    "自动"降低的是**人工确认**这一步，不是安全边界本身。
    """

    SAFE = "safe"
    AUTO = "auto"


class Ctf2SandboxProfile(StrEnum):
    """用哪个沙箱镜像（已定设计决策 1）。

    ## 为什么 reverse 要**单独一个镜像**

    逆向题的解题过程本身就是"运行一个可能是恶意的未知二进制"。
    基础镜像为了 web/crypto/misc 的通用性装了浏览器、代理、一堆
    解析库，攻击面比逆向所需的（gdb / objdump / python）大得多，
    而逆向**恰好是唯一一类会主动执行目标给的代码**的题型。

    按题型收窄镜像 = 收窄"题目利用解题者工具链"这条路径。
    代价是两个镜像要各自维护，这一点如实记在
    `docker/ctf2-sandbox-re/Dockerfile` 的注释里。

    取值就是**镜像名本身**，不做"逻辑名 → 镜像名"的第二层映射 ——
    两层映射会让"这个字段到底存的是什么"变成一个每次都要查的问题。
    """

    BASE = "ctf2-sandbox"
    REVERSE = "ctf2-sandbox-re"


# 每题默认的错误提交次数上限（已定设计决策 4）。
#
# 默认 3 而不是"不限"：错误次数是**不可再生资源**，
# 给一个保守的数字，人还有机会在耗尽前介入。
DEFAULT_CTF2_MAX_WRONG_SUBMISSIONS: Final[int] = 3

# ── 解题并发配额（v0.22）────────────────────────────────────
#
# ★ 放在 `vocab` 而不是 `ctf2/service.py`，理由是**依赖方向**：
#   `ctf2/scheduler.py` 要用它，而 scheduler ↔ service 之间是**有环的**
#   （service 在 `_enqueue` 里延迟导入 scheduler）。常量若住在 service，
#   scheduler 就得在模块顶层 import service —— 今天能跑（因为 service
#   那侧是延迟导入），但将来有人把 service 那行 import 提到模块级，
#   就会炸成循环导入。`vocab` 是叶子模块，没有这个风险。
#
# 语义：**CTF2 自己的**解题并发配额。它不再借用扫描那个
# （见 `settings/models.py` 的 `Ctf2Config.max_concurrent_solves`：
# 借用会让两个调度器各自拿到同一个上限，实际跑出 2 倍 —— 那是一处
# 真实存在过的超卖，决赛两个模式同时跑时会打爆单机）。
#
# 总并发 = 扫描子配额 + 解题子配额。各守自己那份，总和天然有界。
DEFAULT_CTF2_MAX_CONCURRENT_SOLVES: Final[int] = 3
MIN_CTF2_MAX_CONCURRENT_SOLVES: Final[int] = 1
# 上限 16 而不是 8（扫描那个的上限）：比赛规则写明「题量设计明显超过
# 人工手工完成能力」，解题的并发诉求天然高于扫描。真能开多大取决于
# 机器与模型服务的并发额度，所以这只是一个"别让人手滑填 1000"的护栏。
MAX_CTF2_MAX_CONCURRENT_SOLVES: Final[int] = 16

# ── 解题队列的轮询间隔（v0.24）──────────────────────────────
#
# ## ★ 为什么 CTF2 不再借用 `scan_poll_seconds`
#
# 原来 `ctf2/scheduler.py` 的循环写的是：
#
#     interval = max(1.0, float(get_settings().scan_poll_seconds))
#
# 那是**扫描**的旋钮。后果不是"少了个设置项"那么轻：用户在设置页把
# 扫描轮询从 2 秒调到 10 秒（完全合理的省机器操作），CTF2 的认领延迟
# 会**一起**变成 10 秒 —— 而他改的是另一个领域的设置，界面上没有任何
# 地方提示这两件事连着。
#
# 这与 v0.22 的 `max_concurrent_solves` 是同一类病：**两个独立的东西
# 共用一个旋钮**。配额那次表现为超卖，这次表现为"改了 A 动了 B"。
#
# ## 取值：1.0 秒
#
# 这个间隔只决定"一个槽位空出来之后，下一道题多久被认领"。
# 比赛规则写明「每道题目前三名成功解题的团队额外加分」，所以认领延迟
# 直接换分；而一个 tick 的成本是一次带行锁的单表 SELECT，1 秒一次
# 在单机上可以忽略。
#
# 为什么不更低：`MIN` 定 0.5 秒是护栏，不是建议值。低于 1 秒的收益
# 会被"认领本身也要启动容器、起 LLM 会话"这一步吃掉，而空转查询
# 是**恒定**成本 —— 那是在给数据库做无意义的功。
DEFAULT_CTF2_SOLVE_POLL_SECONDS: Final[float] = 1.0
MIN_CTF2_SOLVE_POLL_SECONDS: Final[float] = 0.5
MAX_CTF2_SOLVE_POLL_SECONDS: Final[float] = 30.0

# ── 自动增量同步（v0.24，抢首杀）────────────────────────────
#
# ## 它解决的是什么
#
# 比赛规则里那条「前三名额外获得 15%/10%/5% 奖励分」意味着**发现新题的
# 时刻**比解题速度更早地决定得分：一道题在平台上挂出来之后的头几分钟
# 是抢前三的窗口，而在此之前系统里连这道题都不存在 —— 用户得先手动
# 点一次同步才看得见它。
#
# ## ★ 默认 30 秒，而不是 1 秒或"关"
#
# 增量同步一轮的请求量是 **1 次 `writeups` + 每个新题 1 次探测**（已在
# 库的题按 remote_id 跳过，见 `service.sync` 的说明），所以成本不随题目
# 总数增长。真正的约束有两条，都不在我们这边：
#
#   ① 平台**不会更快地出新题** —— 出题人的节奏是分钟级，不是秒级；
#   ② 请求预算和解题**共享同一个账号**的限额。客户端那个 5 req/s 的
#      令牌桶是**每个 `Ctf2Client` 实例**一个（见 `client.py` 的
#      `__init__`），不是每个 PAT 一个 —— 于是 N 个并发解题就是
#      N × 5 req/s 打同一个账号。同步再挤进去只会推高撞 429 的概率，
#      而 429 的代价落在**提交**上（重试耗尽 → 题目落到 failed）。
#
# 30 秒 = 每分钟 2 次请求，相对解题流量可以忽略，同时"新题最晚 30 秒
# 内可见"。要更激进就把这个值调小，但要连同 `max_concurrent_solves`
# 一起想 —— 那是同一份额度。

# ⚠️ 0 表示**关闭**，这是默认之外的合法取值（用户可以随时关掉这个
#    后台网络行为，而不必卸载功能）。
DEFAULT_CTF2_AUTO_SYNC_SECONDS: Final[int] = 30
MIN_CTF2_AUTO_SYNC_SECONDS: Final[int] = 0
MAX_CTF2_AUTO_SYNC_SECONDS: Final[int] = 3600

# ── 解题回合预算（v0.26）────────────────────────────────────
#
# ## ★ 为什么默认是 80 而不是原来的 40
#
# 实测（2026-10-01，`[FlareOn3]GUI`，一道 .NET 逆向题）：跑满 40 轮、
# 发出 **37 条各不相同的命令**（一路分析到 `decryptBuffer` /
# `DecompressBuffer` 的资源流结构），最后因为预算耗尽而停。
#
# 那次实测顺带**推翻**了一个更省事的猜测 —— "跑满是因为模型在兜圈子
# 或者失忆"。37 条命令全不相同，它一直在做实事。真实原因是**这类题
# 本身就需要更多次迭代**。
#
# ## 为什么做成可配置
#
# 同一批实测里，简单题 6 轮就出结果（`[ACTF2020 新生赛]Exec`），
# 而难题 80 轮可能还不够。这个数**没有全局最优**，只有"按题权衡" ——
# 所以它必须是一个能改的值，而不是一个写死的数字。
#
# ⚠️ 它与 `runner.DEFAULT_MAX_TURNS` 是**同一个数**（那边从这里导入）。
#    两处各写一份的话，改了一处就会得到"界面上显示 200、实际跑 80"
#    那种谁也查不出来的错位。
DEFAULT_CTF2_MAX_TURNS: Final[int] = 80
# 下界 5：再少就不是"预算不足"，而是"根本没开始"（一道题光看附件就要 2-3 轮）。
MIN_CTF2_MAX_TURNS: Final[int] = 5
# 上界 500：它只是一个"别手滑填 100000"的护栏。
#
# ⚠️ 这个上限**不是**成本护栏 —— 真正的成本是"每轮一次 LLM 调用"，
#    而那该由用户在比赛现场自己权衡（难题给 200 轮可能是完全理性的决定）。
#    拦得太紧，等于我们替他决定"这道题能不能解出来"。
MAX_CTF2_MAX_TURNS: Final[int] = 500

# ── 优先级的取值分层（v0.24）────────────────────────────────
#
# `ctf2_challenges.priority` 现在有两个写入方：**人**（题目卡片上的
# 提前/延后、"按分值排队"）和**系统**（新题自动加成）。两者共用一个
# 整数列，就必须约定谁大谁小，否则"自动"会把人的判断盖掉。
#
# 约定：
#
#     0           默认（没人管过，纯 FIFO）
#     1 .. 999    人工设置。`set_priorities_by_score` 按分值从高到低
#                 发 `total - index`，题量在 999 以内都不会越界。
#     1000 起     自动新题加成，值 = 基准 + 分值（封顶见下）
#
# 这样三条性质同时成立：新题排在所有旧题前面（抢首杀）、新题内部
# **分值高的先做**（那是同一份时间里的最优解）、而人只要设 1000 以上
# 就能压过自动判断（**人永远在环里**，这也是赛事方明确允许的操作）。
CTF2_PRIORITY_MANUAL_MAX: Final[int] = 999
CTF2_AUTO_PRIORITY_BASE: Final[int] = 1000
# 新题加成的封顶：基准 + 999。留出这一段是为了让"分值"这个维度
# 只在自动档内部比较，不会溢出到 2000 以上而挤掉人工的插队空间。
CTF2_AUTO_PRIORITY_MAX: Final[int] = CTF2_AUTO_PRIORITY_BASE + CTF2_PRIORITY_MANUAL_MAX

# ── 人工插队的起点（v0.24）──────────────────────────────────
#
# ## ★ 为什么需要这个数：不加它，"提前"按钮会**事实上失效**
#
# 比赛时新题一律被自动加成到 1000..1999。而题目卡片上的「提前」按钮
# 是**每次 +10** —— 用户点十几次也追不上一道 1500 的新题。那个按钮
# 会从"有用"变成"点了没反应"，而这是**自动优先引入的回归**，不是原有
# 缺陷。
#
# 所以「提前」的语义定成"一步插到最前"：
#
#     priority < 2000  →  设为 2000（压过所有自动加成）
#     priority >= 2000 →  +10（人工档内部再微调）
#
# 「延后」对称：2000 → 0（退回默认），> 2000 → -10。
#
# 为什么是 2000 而不是"最大值"：它必须**大于** `CTF2_AUTO_PRIORITY_MAX`
# （1999），否则自动题里分值最高那道会压过人工插队的题。
CTF2_PRIORITY_MANUAL_BASE: Final[int] = CTF2_AUTO_PRIORITY_MAX + 1


# ══════════════════════════════════════════════════════════
# 扫描的"建行者"（v0.27）
# ══════════════════════════════════════════════════════════
#
# ## ★ 为什么需要它：一次实测的误操作
#
# 2026-10-08 用户报「点了重跑为什么没有开始解」。查下来是：
# 他在**扫描列表**里对一条 `[CTF2] WEB5[中等]` 点了「重跑」——
# 而那个按钮属于**扫描模块**（strix 渗透测试），与 CTF2 的解题链路
# 毫无关系。于是：
#
#     rerun_scan 忠实复制了 target `ctf2:1cbe06be-…`
#     → strix 收到后当场拒绝（Invalid target）
#     → argparse 用法错误，退出码 2
#     → 而 `EXIT_FINDINGS` **也是 2**，被记成「已完成 · 0 秒」
#     → CTF2 侧零动作，题目状态一个字没变
#
# 三个后果里最坏的不是那条假记录，而是**用户没有任何反馈**：
# 他点了按钮，界面显示"已完成"，而题没动。
#
# ## 这个常量的作用
#
# 让 `scans` 侧能认出"这条行不是我建的、别拿它当我自己的事"，
# 从而在重跑/续跑/终止三个入口处**明确拒绝并指路**。
#
# ⚠️ **不能**用 `authorized_by` 当判据：CTF2 建壳时也填用户 id
#    （重跑那条更是用户本人 id），两者在那一列上完全一样。
SCAN_ORIGIN_CTF2: Final[str] = "ctf2"
