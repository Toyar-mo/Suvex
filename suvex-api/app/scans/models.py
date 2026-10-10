"""`scans` 领域（扫描任务）。

这张表同时承担**法律留痕**职责（契约 §5）—— 见下面 authorization_* 六列。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.columns import JsonColumn, UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc
from app.core.vocab import ScanGoal, ScanStatus, TargetKind, values


class Scan(Base):
    """一次扫描任务。

    ## `started_at` 在 queued 状态下等于创建时刻

    契约 §2.1 把 `startedAt` 定为**非空** `ISO string`（对比 `finishedAt`
    明确可空），所以它不能是 NULL。对 `queued` 的扫描，它记录的是
    「任务被受理的时刻」。真正的开跑时刻由 `status` 变成 `running` 体现。

    刻意**不额外加一列 `actually_started_at`**：那会引入第二个时间来源，
    而「哪个才是开始时间」会立刻变成每次查数据都要重新判断的问题。
    契约里 `durationSec` 的算法也是 `finishedAt - startedAt`，多一列只会让
    这个式子在两处不一致。

    ## `duration_sec` 在 running 时必须是 NULL

    契约 §2.1 用 ⚠️ 标了这一条：前端排序依赖它区分「未结束」与「0 秒完成」。
    存 0 会把「正在跑的」排到「最快的」位置。所以它是可空列，
    且**只在终态写入**（不提供默认值 0）。
    """

    __tablename__ = "scans"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in values(ScanStatus))})", name="status_valid"
        ),
        CheckConstraint(
            f"target_kind IN ({', '.join(repr(v) for v in values(TargetKind))})",
            name="target_kind_valid",
        ),
        CheckConstraint(
            f"goal IN ({', '.join(repr(v) for v in values(ScanGoal))})",
            name="goal_valid",
        ),
        # 终止状态必须有 finished_at，非终止状态必须没有 —— 这条不变量
        # 放在数据库而不是 service 里：它是「数据不可能长成什么样」的定义。
        CheckConstraint(
            "(status IN ('completed', 'failed', 'aborted')) = (finished_at IS NOT NULL)",
            name="finished_at_matches_terminal_status",
        ),
        Index("ix_scans_status_created_at", "status", "created_at"),
        Index("ix_scans_target", "target"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # 用户可读名。可空输入 → 后端用 target 兜底（契约 §3.3），所以这一列非空。
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # ⚠️ 存的是**归一化后**的目标（契约 §2.8），不是用户原始输入。
    # 不归一化会出现「api.example.com / https://api.example.com /
    # https://api.example.com/」三条看起来不同的记录，而它们是同一个东西。
    target: Mapped[str] = mapped_column(String(512), nullable=False)
    # 预检探测到的**可执行地址**（任务 11，形如 `http://example.com`）。
    #
    # 与 `target` 分开是刻意的：`target` 是**身份**（归一化、不带协议，
    # 用来判断"是不是同一个目标"），而这一列是**当时观察到的事实**
    # （哪个协议真的通）。合成一列会让"补了协议"变成一个改变身份的动作 ——
    # 同一个目标在两次创建里会变成两条记录。
    #
    # NULL = 没探测（非 URL 目标、或探测整体失败）。
    probed_target: Mapped[str | None] = mapped_column(String(512), nullable=True)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)

    # ── 任务背景与成功判据（任务 12）──────────────────────
    #
    # ## `brief`：用户提供的任务上下文
    #
    # 起因是一个很实在的抱怨：「只给网址让他扫描太没意义了」。
    #
    # 拿 CTF 题举例：题面里有考点（"日志模板引擎的污染数据流"）、
    # 有提示（`%hhn`、varargs、边界条件）、有**诱饵**（"安全备注"），
    # 还有 flag 格式。这些信息对 agent 的价值是决定性的 ——
    # 没有它，agent 只能对着一堆接口盲扫；有了它，它能直奔考点。
    #
    # 但这一列**不是 CTF 专用的**：渗透测试同样需要交代背景
    # （"这是给客户做的第一次评估，重点是认证与越权，测试账号是 …"）。
    # 所以它是一个通用的自由文本，而不是一组 CTF 字段。
    #
    # ⚠️ **它可能含凭据**（"用 admin:xxx 登录"）。所以：
    #   · 它**不进命令行**，走 `--instruction-file`（见 runner.py）；
    #   · 它不出现在审计 detail 里；
    #   · 日志走既有的脱敏链路（`core/logging.py` 的 RedactingFormatter）。
    # 把它拼进 argv 会让同机任何用户 `ps aux` 就看到全文。
    brief: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 成功判据（见 `vocab.ScanGoal`）。**非空**，默认 `findVulns` ——
    # 一条"没有目标"的扫描在语义上是空转，给个默认值比允许 NULL 好：
    # 前端读它时不必到处写 `?? 'findVulns'`，而那种兜底写多了
    # 迟早有一处漏掉，于是那条记录在界面上没有判据可显示。
    goal: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ScanGoal.FIND_VULNS.value
    )

    # ── CTF 夺旗的结果（任务 18）────────────────────────────
    #
    # 只在 `goal == captureFlag` 时被填 —— 由 `service.finalize` 从扫描产物里
    # 抽出来（见 `scans/flag.py`）。
    #
    # ## 为什么要单独一列，而不是塞进 `findings`
    #
    # ★ **flag 不是漏洞。** 一次逆向题扫描很可能一条漏洞都没有
    # （实测那次：`findings` 是空的，而 flag 明明拿到了），
    # 硬塞成一条"critical 漏洞"会让「漏洞发现」页出现一条不是漏洞的记录，
    # 而那一页的语义是"存在什么问题"。
    #
    # 这一点在本项目里有前车之鉴：`finding.verification` 与 `finding.status`
    # 当初就是被要求"不可混为一谈"的两个维度。flag 与漏洞也是同一个道理 ——
    # 它是**这次任务达成的判据**，不是被发现的缺陷。
    #
    # ## 为什么可空且无默认值
    #
    # 三态是必须能区分的：
    #   · NULL  —— 没跑夺旗（或抽取不唯一，见 `flag.py` 的说明）；
    #   · 有值  —— 拿到了；
    #   · `""`  —— **不使用**。空串会与"没填"在界面上渲染成两种不同的话，
    #              而这里根本没有第三种含义要表达。
    flag: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ScanStatus.QUEUED.value)

    # `SkillId[]`。JSONB 而不是关联表：它是个短小集合，且查询模式是
    # 「连同扫描一起读出来」，不是「按 skill 关联查询」。
    skills: Mapped[list[str]] = mapped_column(JsonColumn, nullable=False, default=list)

    # ⚠️ `provider_id` **刻意没有外键**（任务 2 移除了它）。
    #
    # 它是一条**历史引用**，不是一条引用完整性约束：它记录的是"这次扫描
    # 当时用的是哪个 provider"。而 provider 是会被删的（用户不再用了）。
    #
    # 原来它是 `ForeignKey(..., ondelete="RESTRICT")`，后果是**任何一条历史扫描
    # 都会永久锁住那个 provider 的删除** —— 删不掉，而且用户看到的是一个
    # 数据库层的 500（英文的 foreign key violation），不是我们能解释的中文。
    # 契约 §6 只要求「有 running 扫描引用时返回 409」，也就是"只拦未结束的"，
    # 而 RESTRICT 比它严得多、且无法表达这个区分。
    #
    # 换成 SET NULL 也不行：`Scan.providerId` 在前端是 `string`（非空），
    # 置空会让旧扫描的渲染多一个它没处理过的分支。
    # 软删除倒能保住外键，但要给每一条 provider 查询都加上
    # `deleted_at IS NULL` 的过滤 —— 代价更大，且"删了但还在"这件事
    # 迟早会有人忘掉过滤。
    #
    # 那完整性靠什么保证？**写入时**：`POST /scans`（任务 3）必须校验
    # 这个 provider 存在且可用。历史引用的完整性本来就该在写入那一刻保证，
    # 而不是靠"永远不许删除被引用过的东西"。
    #
    # 如实记一条代价：provider 删掉之后，旧扫描的 `providerId` 会指向一个
    # 不存在的 id，界面上只能显示这个 id 本身。要让它一直可读，
    # 得在 scans 上快照一份 provider 的 label/model —— 而那需要在契约里
    # 给 `Scan` 加字段，属于前端类型变更，记在契约 §7 里，不在本任务做。
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)

    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # ⚠️ 可空且**无默认值**。running 时是 NULL，不是 0。
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)

    agent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 契约 §2.1 的 `findings` 字段（按严重度分档的计数）。
    # ⚠️ 它是 `Partial<Record<Severity, number>>`：**没有发现某一级别时该键不存在**，
    # 不是 0。写 0 会让前端「该键不存在」的分支永远走不到，而那是它设计过的路径。
    findings: Mapped[dict[str, int]] = mapped_column(JsonColumn, nullable=False, default=dict)

    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 0–1，仅 running/queued 有意义。
    #
    # ⚠️ **后端不填它**（任务 3 的决定）。理由：strix 不提供可靠的进度，
    # 而按"已跑时间 / 预期时长"算一个百分比是**编造** ——
    # 它会在第 30 秒显示 50%、在第 5 分钟还显示 50%，然后突然跳到 100%。
    # 与其给一个假的数字，不如留 null 让前端渲染成"进行中"（不确定态）——
    # 那个类型的可空性本来就在契约里（`number | null`）。
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── 执行后端（任务 3）──────────────────────────────────
    #
    # 这六列记录"这次扫描在**外面**是怎么跑的"。分两类：
    #
    # | 列          | 对外可见 | 用途                                         |
    # | ----------- | -------- | -------------------------------------------- |
    # | `run_id`    | ✅       | strix 侧的运行标识（`strix_runs/<run_id>/`） |
    # | `runner`    | ❌       | 用的哪个执行后端，让历史记录可被正确解读     |
    # | `run_dir`   | ❌       | 产物目录（events.jsonl、报告）的绝对路径     |
    # | `pid`       | ❌       | 子进程 pid，终止与孤儿回收靠它               |
    # | `exit_code` | ❌       | 进程退出码；判"完成"还是"失败"的依据         |
    # | `log_path`  | ❌       | stdout/stderr 落盘位置                       |
    #
    # `run_id` 对外可见是因为任务明确要求"记录 strix 侧生成的 run 标识，
    # 关联到本地的 scan 记录"—— 而"能关联"意味着人要能看见它、
    # 拿去 strix 那边对账。其余五个是服务端内部信息：
    # 路径与 pid 暴露给前端既没用又是多余的信息面。

    run_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    runner: Mapped[str | None] = mapped_column(String(32), nullable=True)
    run_dir: Mapped[str | None] = mapped_column(Text, nullable=True)
    # pid 可空：还没启动、或者进程已经回收。
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    log_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── 授权留痕（契约 §5.4）──────────────────────────────
    # ★ 这一组是本表唯一涉及法律责任的字段，**不能只在前端做**。
    #   POST /scans 会真的对目标发包，对未授权目标做这件事在很多司法管辖区
    #   是违法行为。所以「用户点过确认框」必须服务端强制校验 + 落库留痕。
    #
    # `authorized_by` 在单管理员系统里恒定，为什么还要存：
    # 留痕要能被独立理解。将来加第二个人（或机器令牌签发者）时，
    # 这一列就是「谁授权的」而不是「某个人授权了」。
    # 现在存的成本是 8 字节，将来补的成本是数据迁移。
    authorized_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authorization_statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 客户端声称的确认时刻 —— **可以伪造**。
    authorization_confirmed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # 服务端收到的时刻 —— 不可伪造。留痕真正的价值在这里。
    authorization_received_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    authorization_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authorization_user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── 归档 / 软删除 / 终止方（v0.13，任务 9）─────────────
    #
    # ★ 用**时间戳**而不是布尔。布尔少一个维度：什么时候归档的。
    #   "失败 30 天后自动归档"要能事后解释"这条为什么被归档"，
    #   回收站要按时间算到期。布尔与 `IS NOT NULL` 查询等价（可建部分索引），
    #   所以时间戳不损失任何能力，反而多出审计价值。
    #
    # ⚠️ 归档**不是**新的 `status` 取值，而是与 status 正交的一个维度 ——
    #   一条 completed 的扫描可以被归档，一条 failed 的也可以。
    #   把它塞进 status 会让"已归档的失败扫描"无处表达。
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # 软删除标记。非空 = 在回收站，默认列表不返回它。
    # 真正的删除只发生在保留期到期后的清理里（物理删）。
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # ── v0.27：这条扫描是不是 CTF2 借来挂事件流的壳 ────────────
    #
    # ★★ 为什么要一列，而不是"现场看关联表推出来"
    #
    # 判据本来可以写成 `EXISTS(SELECT 1 FROM ctf2_challenges WHERE scan_id = scans.id)`，
    # 那不用加列。**但那个判据是错的**，而且错得很隐蔽：
    # `ensure_solve_scan` 复用旧 scan 时，`challenge.scan_id` 一直指向同一条；
    # 而一旦有人从扫描页对这条壳发起「重跑」，`rerun_scan` 会**新建一条
    # 同 target 的 scan**，那条新壳**没有任何 challenge 指向它**。
    # 于是"看关联表"会把这条新壳判成真扫描 —— 而它恰恰是最需要拦住的：
    # 实测（2026-10-08）用户点了两次重跑，产生两条 `exit_code=2` +
    # `duration=0/1s` 却记为「已完成」的记录，而 CTF2 侧零动作。
    #
    # 所以标记必须在**建行时**落下，随行走，不依赖当时的关联状态。
    #
    # 取值是**建这条 scan 的那个子系统名**（当前只有 `"ctf2"`）。
    # 不用布尔：将来若有第三种子系统借壳，布尔就表达不了了 ——
    # 而"谁建的"这个问题在多子系统下必然要被问到。
    #
    # ⚠️ `authorized_by` 那几个留痕列**不能**当判据：它们记的是
    # "谁授权的"，而 CTF2 建壳时也会填一个用户 id（重跑时更是用户本人），
    # 两者在那一列上长得一模一样。
    origin: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # 谁终止的（仅 aborted/failed 可能有值）。
    #
    # ⚠️ 取值必须**诚实**：判定不出来就是 `unknown`，不许猜。
    #   历史数据里 `failure_reason` 是自由文本，分不出"用户点的终止"
    #   与"进程自己挂了"，所以老记录回填时按能确定的那部分来。
    terminated_by: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # ── 卡死检测（任务 11）────────────────────────────────
    #
    # `stalled_at` 非空 = 调度器判定"它疑似卡住了"（距上一条事件超过
    # `scan_stall_seconds`）。默认**只标记不终止** —— 见 config 里那段
    # 关于为什么"宁可让用户看到提示后自己决定"的说明。
    #
    # 它是**可自愈的标记**：一旦又有新事件进来就清掉。否则一次误判
    # 会让界面永远挂着"疑似卡死"，而那条扫描其实早就继续跑了。
    stalled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # 最近一条活动事件的时刻。冗余存一列是为了让列表**不必为每条扫描
    # 各查一次事件表**（那正是 N+1），而界面要显示"已静默 N 分钟"。
    last_event_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in (ScanStatus.COMPLETED, ScanStatus.FAILED, ScanStatus.ABORTED)

    @property
    def is_abortable(self) -> bool:
        return self.status in (ScanStatus.QUEUED, ScanStatus.RUNNING)

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Scan id={self.id!r} status={self.status!r} target={self.target!r}>"
