"""`scans` 领域的出入参（契约 §2.1 / §3.3 / §5）。

`ScanOut` 是契约 §2.1 那张表加**一个**新字段 `runId`（任务 3 要求：
「记录 strix 侧生成的 run 标识，关联到本地的 scan 记录」——
而"能关联"意味着人要能看见它、拿去 strix 那边对账）。

服务端内部还有 `run_dir` / `pid` / `exit_code` / `log_path` 四列，
**刻意不出现在这里**：路径与 pid 对前端没有用处，只是多余的信息面。
判"完成还是失败"的结论已经落在 `status` 与 `failureReason` 上了。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from app.core.schemas import CamelModel, StrictModel
from app.core.timeutil import IsoDateTime
from app.core.vocab import SkillId

TargetKindLiteral = Literal["url", "repo", "path", "service", "artifact"]

# ⚠️ 与 `core.vocab.ScanGoal` 的**全部**取值一致（任务 12）。
#
# 这里又是一个"抄了一份"的字面量 —— 与 `findings/schemas.py` 里
# `StatusLiteral` 当初漏掉两个状态是同一类隐患。所以：
# **改 vocab 就必须改这里**，且 `tests/test_scan_brief_and_goal.py`
# 有一条对账测试盯着两者相等（不靠人眼）。
GoalLiteral = Literal["findVulns", "captureFlag", "verifyFix"]


class AuthorizationIn(StrictModel):
    """扫描授权确认（契约 §5.2）。

    ★ **必填，缺失直接拒绝创建。** 这是本契约里唯一涉及法律责任的设计：
    `POST /scans` 会真的对目标发包，对未授权目标做这件事在很多司法管辖区
    是违法行为。

    服务端会：
    1. 校验 `confirmed is True`（否则 `403 TARGET_NOT_AUTHORIZED`）；
    2. **逐字校验 `statement`**（不一致则 `400 VALIDATION_FAILED`）；
    3. 落库六列留痕（用户、措辞、客户端时刻、**服务端收到的时刻**、IP、UA）。

    逐字校验措辞是刻意的：**防止前端"顺手勾一个默认值"**。用户必须看到、
    并同意那**具体一句话**。这也让留痕有意义 —— 记录的是一条明确的声明，
    不是一个布尔。
    """

    confirmed: bool
    # 原样回存用户同意的措辞。服务端按 SUPEX_SCAN_AUTHORIZATION_STATEMENT 逐字比对。
    statement: str = ""
    # 客户端声称的确认时刻。**可以伪造** —— 所以它只是留痕的一部分，
    # 权威的是服务端另外记的 `authorizationReceivedAt`。可选：
    # 一个用 curl 建扫描的人不该被迫自己算一个时间戳。
    confirmedAt: IsoDateTime | None = None


class ScanCreate(StrictModel):
    """`POST /scans`（契约 §3.3）。"""

    # 可空，后端用 target 推导（契约 §3.3：`name` 可空，后端用 target 兜底）
    name: str = Field(default="", max_length=200)
    # ⚠️ 契约说送上来的是**已归一化**的值。服务端仍然自己归一化一遍 ——
    # 前端可以被绕过，而"同一个目标存成三条记录"的代价是列表里出现
    # 看起来不同的重复项。
    #
    # ★ 默认空串、**不再在 schema 层要求非空**（任务 17）。
    #
    #   起因是 `artifact`（只有附件的逆向 / 纯文件 Misc 题）：它**根本没有
    #   目标地址**，而 `min_length=1` 会让这类题在建扫描的第一步就被 pydantic
    #   打回 —— 报错还说不出"这类题不需要地址"。
    #
    #   ⚠️ 但"必填"这件事**不能就这么算了**，它只是**换了地方**：
    #   校验挪到了 `service.create_scan` → `targets.validate()`，按 kind 分支。
    #   除 `artifact` 外的四种仍然必须非空（`validate` 的第一条早退就是
    #   "请填写目标"）。也就是说 schema 放行了空串，而**业务层不放行** ——
    #   这正是本项目一贯的分工：schema 管形状，service 管语义。
    #
    #   为什么不用 pydantic 的 model_validator 在这一层按 kind 判：
    #   那样"合法的目标"就有了**两份规则**（这里一份、targets.py 一份），
    #   而两份规则迟早会分叉。目标的合法性只该有一个出处。
    target: str = Field(default="", max_length=512)
    targetKind: TargetKindLiteral
    skills: list[str] = Field(min_length=1)
    providerId: str = Field(min_length=1)
    # ── 任务背景与成功判据（任务 12）──────────────────────
    #
    # `brief` 是**用户提供的任务上下文**：CTF 的题面与提示、
    # 渗透测试的范围与测试账号、复测时"要确认哪几条修好了"。
    #
    # ⚠️ **上限 8000 字符**，不是随手定的：
    #   · 它最终会进 agent 的提示词，无限长会吃掉上下文预算；
    #   · 8000 字足够写一份很详细的题面/测试说明；
    #   · 有上限才能让"太长"变成一个**明确的 400**，而不是
    #     悄悄截断（截断掉的可能正好是最后那句 flag 格式）。
    brief: str | None = Field(default=None, max_length=8000)
    # 成功判据。默认 `findVulns` —— 契约把它定为可选，
    # 不填就是例行找漏洞（绝大多数场景）。
    goal: GoalLiteral = "findVulns"
    # ── 题目附件的暂存批次（任务 12 修正）────────────────
    #
    # ★ 为什么是"暂存 id"而不是"建完再传"
    #
    # 向导里的动作顺序是：填目标 → 贴题面 → **选附件** → 点启动。
    # 而扫描一旦创建就**毫秒级**被调度器认领（`_nudge_scheduler`），
    # 所以"建完再往 `/{id}/attachments` 传"这个窗口根本不存在 ——
    # 那条路会稳定地拿到 409。我第一版就是那么设计的，被测试当场抓住。
    #
    # 所以附件走**先暂存、创建时认领**：
    #   1. 选文件 → `POST /scans/attachments/staging` → 拿 `stagingId`；
    #   2. 点启动 → 带上这个 id；
    #   3. 后端把暂存目录移到该扫描名下。
    attachmentStagingId: str | None = Field(default=None, max_length=64)
    # ★ 类型上可空，但**语义上必填** —— 契约 §5.3 要求缺它时返回
    # `403 TARGET_NOT_AUTHORIZED`，而不是 pydantic 的 400 VALIDATION_FAILED。
    #
    # 所以刻意**不用** pydantic 的必填来拦：那样拦出来的错误码是 400，
    # 而前端对这两者的处理不同 —— 403 要引导用户去勾选授权，
    # 400 要提示重新确认声明。用错一个码，界面就会指错方向。
    authorization: AuthorizationIn | None = None

    @field_validator("skills")
    @classmethod
    def _known_skills(cls, value: list[str]) -> list[str]:
        valid = {member.value for member in SkillId}
        unknown = [item for item in value if item not in valid]
        if unknown:
            raise ValueError(f"未知的技能：{'、'.join(unknown)}")
        seen: list[str] = []
        for item in value:
            if item not in seen:
                seen.append(item)
        return seen


class ScanRerun(StrictModel):
    """`POST /scans/{id}/rerun`（契约 §3.3 / §6）。

    ★ **同样要求授权确认。** 这条容易漏：重跑在语义上是一次**新扫描**，
    目标可能已经变了（域名易主、IP 重分配）。不能因为"上次确认过"就免掉。
    """

    # ★ 同样可空、同样语义必填 —— 与 `ScanCreate` 同一个理由。
    authorization: AuthorizationIn | None = None
    # 可空：不填就沿用原扫描的名字。
    name: str | None = Field(default=None, max_length=200)


class ScanOut(CamelModel):
    """一次扫描。字段名与前端 `Scan` 类型逐字一致。"""

    id: str
    name: str
    target: str
    targetKind: str
    status: str
    skills: list[str]
    providerId: str
    # ── 任务背景与成功判据（任务 12）──────────────────────
    #
    # `brief` 回显给前端：用户需要确认"我写的那段背景确实传上去了"
    # （否则他无处验证，只能猜）。详情页也用它显示原始任务说明。
    brief: str | None = None
    goal: str = "findVulns"
    startedAt: IsoDateTime
    finishedAt: IsoDateTime | None
    # ⚠️ running 时必须是 null，不能是 0（契约 §2.1：存 0 会把"正在跑的"
    # 排到"最快的"位置）。
    durationSec: int | None
    agentCount: int
    # `Partial<Record<Severity, number>>`：**没有发现某一级别时该键不存在**。
    findings: dict[str, int]
    failureReason: str | None
    # ⚠️ 后端**不填它** —— strix 不提供可靠进度，算一个百分比是编造。
    # 前端渲染成不确定态。见 `scans/models.py` 的说明。
    progress: float | None
    # 任务 3 新增：strix 侧的运行标识（`strix_runs/<runId>/`）。
    # 未启动或未能识别时是 null。
    runId: str | None

    # ── CTF 夺旗的结果（任务 18）──────────────────────────
    #
    # 只在 `goal == captureFlag` 时可能有值，由 `service.finalize` 从扫描产物
    # 里抽出来（见 `scans/flag.py`）。
    #
    # ⚠️ **null 有三种可能的含义**，界面上的措辞不能把它们说成同一件事：
    #   · 这次不是夺旗任务；
    #   · 是夺旗任务但没拿到；
    #   · 拿到了，但报告里的形状不唯一、我们**拒绝猜**（见 `flag.py`）。
    # 所以前端渲染时必须**结合 `goal` 与状态**来判断，不能看到 null 就说
    # "没找到 flag" —— 那是把第三种（我们不确定）说成了第二种（确实没有）。
    flag: str | None = None

    # ── v0.13（任务 9）：归档 / 软删除 / 终止方 ──────────────
    #
    # ⚠️ 用**时间戳**不用布尔：布尔丢掉"什么时候归档的"，
    #   而"失败 30 天后自动归档"这条规则要能事后解释。
    #   见契约 §2.1 的说明。
    archivedAt: IsoDateTime | None
    deletedAt: IsoDateTime | None
    # 谁终止的。判定不出来就是 "unknown" —— **不猜**。
    terminatedBy: str | None

    # ── v0.27：这条记录是不是别的子系统借来挂事件流的 ──────────
    #
    # ★ 它不是"内部实现细节"，**必须下发** —— 前端要靠它藏掉
    #   「重跑 / 续跑 / 终止」三个入口。
    #
    #   起因是一次实测的误操作：用户对 `[CTF2] WEB5[中等]` 点了「重跑」，
    #   而那个按钮属于扫描模块 —— strix 拒收 `ctf2:…` 这个 target、
    #   argparse 退 2、被记成「已完成 · 0 秒」，而题目一个字没动。
    #   他点了两次，得到两条假记录，而**界面没有任何提示**。
    #
    # 取值是建这条 scan 的子系统名（当前只有 `"ctf2"`）；
    # `None` = 一次正常的扫描。见 `vocab.SCAN_ORIGIN_CTF2`。
    #
    # ⚠️ **不能**用 `authorizedBy` 代替它：CTF2 建壳时也填用户 id
    #   （重跑那条更是用户本人），两者在那一列上完全一样。
    origin: str | None = None
    # ── 卡死检测（任务 11）────────────────────────────────
    #
    # `stalledAt` 非空 = 调度器判定"它很久没有新事件了"。
    # 界面上据此显示「疑似卡死 · 已静默 N 分钟」，让用户知道
    # "是它卡住了"而不是"我的网慢"。
    #
    # `lastEventAt` 一并给出：那个"静默了多久"由前端从它算的话，
    # 就要和客户端的时钟对齐 —— 而**服务端与客户端钟差**会让
    # 它显示成负数（"静默 -2 分钟"）。所以给时刻、由前端算相对时长
    # 是可以的（相对时长不敏感绝对值），但**判定**必须在后端
    # （见 `stalledAt` 由谁设）。
    stalledAt: IsoDateTime | None
    lastEventAt: IsoDateTime | None
    # 目标可达性预检的一句人话（任务 11）。**只有创建扫描的响应会带它**，
    # 其余端点一律为 null —— 它是"刚刚那次创建观察到的事实"，
    # 不是一个可以事后查询的属性。
    #
    # 放在响应里而不是只在日志里：用户填 `example.com` 时心里想的
    # 可能是 https，而真相可能相反；不告诉他，他会看着一次
    # 毫无产出的扫描猜半天。
    notice: str | None = None

    # ── v0.44：这条记录对应的 CTF2 题分类（只读派生，不落库）──────────
    #
    # ★ 它存在的原因只有一个：**列表里的一行怎么称呼自己**。
    #
    #   `origin` 只能说明"这条 scan 是 ctf2 借的壳"，它区分不出
    #   一道 CTF 题和一次应急响应复盘 —— 而这两者在那张表里长得一样：
    #   `target_kind="service"`（所以显示成「TCP 服务」）、
    #   `goal="captureFlag"`（所以零条目时的措辞在说"夺旗"）、
    #   名字带 `[CTF2] ` 前缀。一次日志复盘被渲染成"TCP 服务 · 夺旗"
    #   不是措辞不好看，是**说错了**：那次任务压根没有对任何地址发过包。
    #
    # ★ 为什么下发原始分类值、而不是直接下发一句中文标签：
    #   中文标签属于展示层（`suvex-web/src/data/scan.ts` 的
    #   `SCAN_GOAL_LABEL` / `TARGET_KIND_LABEL` 都在那边），把它搬到
    #   后端等于让"同一件事实有两个出处"—— 而这条正是本项目的
    #   三条设计原则之一。后端只说事实（分类是什么），前端的表里查词。
    #
    # ⚠️ **不要**用它的有无来判断"是不是 IR"：判前缀要用
    #   `vocab.IR_CATEGORY_PREFIX`（`ir-`），不是相等比较。将来加
    #   `ir-timeline` / `ir-scope` 时这里不需要改，这也正是下发分类
    #   而不是下发布尔值的原因。
    #
    # ⚠️ 只有 `origin == "ctf2"` 时可能有值；普通扫描恒为 `None`。
    #   历史记录里这条壳与题目的关联可能缺失（题目被删、scan_id 为
    #   NULL），那时也是 `None` —— 前端必须按"就当它是普通 CTF2 题"
    #   降级，不能因为拿不到分类就不渲染这一行。
    ctf2Category: str | None = None

    @classmethod
    def from_model(cls, scan, *, ctf2Category: str | None = None) -> ScanOut:  # noqa: ANN001
        """把 ORM 行转成响应。

        `ctf2Category` 由调用方**预取后传入**，不从 `scan` 上取：
        `Scan` 表里没有这一列（分类在 `ctf2_challenges` 上），而
        `from_model` 是个纯函数、没有 session。列表端点一次要序列化
        几十条，逐条去查库会变成 N+1 —— 所以取数是调用方的事，
        批量查一次再按 id 分派（见 `router._ctf2_categories`）。
        """
        return cls(
            id=scan.id,
            name=scan.name,
            target=scan.target,
            targetKind=scan.target_kind,
            status=scan.status,
            skills=list(scan.skills or []),
            providerId=scan.provider_id,
            startedAt=scan.started_at,
            finishedAt=scan.finished_at,
            durationSec=scan.duration_sec,
            agentCount=scan.agent_count,
            # `Partial` 的语义：空字典就是"一档都没有"，不是"全是 0"。
            findings=dict(scan.findings or {}),
            failureReason=scan.failure_reason,
            progress=scan.progress,
            runId=scan.run_id,
            archivedAt=scan.archived_at,
            deletedAt=scan.deleted_at,
            terminatedBy=scan.terminated_by,
            # v0.27：`None` = 正常扫描；`"ctf2"` = CTF2 借的壳。
            # 前端据此藏掉「重跑 / 续跑 / 终止」—— 三个都必然失败。
            origin=scan.origin,
            stalledAt=scan.stalled_at,
            lastEventAt=scan.last_event_at,
            notice=None,
            # 任务背景与判据（任务 12）。历史记录里 `brief` 是 NULL
            # （这一列是后加的），前端按"没有背景说明"渲染即可。
            brief=scan.brief,
            goal=scan.goal,
            flag=scan.flag,
            ctf2Category=ctf2Category,
        )


class ScanAttachmentOut(CamelModel):
    """一个任务附件（任务 12 阶段 4）。

    ⚠️ **不含存储路径**。这里连"文件名"本身就是存储名（两者合一），
    所以它当然要给出 —— 没有路径可隐瞒，文件就在
    `scan_root/_uploads/<scanId>/` 下，而客户端不需要知道那个位置。
    """

    # 文件名（已消毒）。agent 在沙箱里就是按这个名字找它
    name: str
    sizeBytes: int


class StagedAttachmentsOut(CamelModel):
    """暂存结果（任务 12）。

    `stagingId` 要原样放进 `POST /scans` 的 `attachmentStagingId` ——
    前端在向导里拿到它、点启动时带上。
    """

    stagingId: str
    files: list[ScanAttachmentOut]


class WriteupOut(CamelModel):
    """CTF 题解（任务 19）。

    ## 只给 markdown 原文，**不在后端渲染**

    渲染器在前端（`react-markdown`）。后端再渲染一遍就等于多一套要跟着
    前端一起维护的样式 —— 而两边一定会分叉，症状是"下载下来的 `.md`
    和屏幕上看到的不是同一个东西"。

    给原文还顺带解决了另一件事：用户要把它贴到比赛平台上，
    而**原文才是他要的**，渲染后的 HTML 不是。
    """

    markdown: str
    """题解全文（markdown）。"""
    filename: str
    """下载用的文件名，带题目名 —— 一次下好几份时才分得清。"""


class BatchArchiveIn(StrictModel):
    """`POST /scans/batch/archive`。

    ★ `archived` 是**必填**的，不设默认值。
    归档与取消归档是同一端点的两个方向，默认值会让
    "我只想取消归档"这种请求在漏传字段时**默默执行归档** ——
    一个破坏性动作绝不能有默认方向。
    """

    ids: list[str] = Field(min_length=1, max_length=200)
    archived: bool


class BatchIdsIn(StrictModel):
    """`POST /scans/batch/delete` 与 `/restore` —— 只有一组 id。"""

    # 上限 200：一次批量操作不该是"把全表选中"。
    # 前端当前也不分页，但接口不该假设调用方一定友好。
    ids: list[str] = Field(min_length=1, max_length=200)


class BatchRerunIn(StrictModel):
    """`POST /scans/batch/rerun`。

    ⚠️ **必须带授权确认**，而且服务端对**每一条**都校验 ——
    批量重跑是 N 次真的发包，不能因为"批量"就绕过 §5.3 的法律要求。
    """

    ids: list[str] = Field(min_length=1, max_length=50)
    authorization: AuthorizationIn
    # ⚠️ 与 `ScanRerun` **同形**，缺了它 `rerun_scan` 会在 `payload.name`
    # 上炸（AttributeError，而不是一条可读的 400）。
    # 批量重跑复用的就是单条那条服务函数 —— 形状必须一致，
    # 这也正是"批量不是另一套逻辑"的体现。
    # 可空：不填就各自沿用原扫描的名字。
    name: str | None = Field(default=None, max_length=200)


class BatchResultOut(CamelModel):
    """三个批量端点共用的响应 —— 与 `POST /findings/status` **同形**。

    不发明新形状的理由：前端只有一套处理逻辑（提示改了几条、
    哪几条没生效）。每个操作自造一个响应，等于把那段逻辑抄三遍。
    """

    changed: int
    # 已经是目标状态、因此被幂等跳过的条数
    skipped: int
    # 变更后的完整 Scan[]，**仅含 changed 的**
    scans: list[ScanOut]


class BatchRerunOut(CamelModel):
    """批量重跑的响应。

    与上面三个**刻意不同**：重跑不是"改状态"，是"新建 N 条记录"，
    所以没有 `changed`/`skipped` 可言。硬套那个形状会得到
    `changed = 新建了几条`这种读了要想一下的字段。
    失败项单独列出来 —— 批量里有一条没起来，用户必须知道是**哪一条**。
    """

    created: list[ScanOut]
    failed: list[BatchRerunFailure]


class BatchRerunFailure(CamelModel):
    scanId: str
    error: str


class CompareOut(CamelModel):
    """`GET /scans/compare?a=&b=` —— 两条扫描的发现增减。"""

    a: ScanOut
    b: ScanOut
    # 只在 a 里出现、只在 b 里出现、两边都有
    onlyInA: list[str]
    onlyInB: list[str]
    common: list[str]
    # 一句话结论，可直接展示（与报告 verdict 同一个套路）
    verdict: str


class ScanStatsOut(CamelModel):
    """`GET /scans/queue` —— 调度器状态。

    做成独立端点是因为它回答一个前端**必须**能回答的问题：
    「为什么我点了开始，扫描却一直排在"排队中"？」
    答案通常是"并发上限满了"，而那需要能看到当前在跑几个、上限几个。
    只给一个"排队中"的状态，用户会以为是坏了。
    """

    # 正在运行的扫描数（本进程实际持有的子进程数）
    running: int
    # 配置的并发上限
    limit: int
    # 还有几个在排队
    queued: int
    # 执行后端名字：strix | fake
    runner: str
    # 后端自检是否通过；不通过时 `runnerError` 说明原因
    runnerReady: bool
    runnerError: str | None


# Pydantic 要一次显式重建才能解析前向引用（`BatchRerunOut` 引用了
# 在它之后定义的 `BatchRerunFailure`）。不重建的话，
# 第一次拿它做校验时会报 "not fully defined"，而那是个运行期才炸的错。
BatchRerunOut.model_rebuild()

__all__ = [
    "AuthorizationIn",
    "BatchArchiveIn",
    "BatchIdsIn",
    "BatchRerunFailure",
    "BatchRerunIn",
    "BatchRerunOut",
    "BatchResultOut",
    "CompareOut",
    "ScanCreate",
    "ScanOut",
    "ScanRerun",
    "ScanStatsOut",
]
