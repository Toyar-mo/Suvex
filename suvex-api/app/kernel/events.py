"""CTF2 的进度事件：写进**现有** events 流（已定设计决策 7）。

## ★ 这个模块的全部设计都受一个约束支配

`activity_events.scan_id` 与 `agent_id` 是**非空外键**：

```
activity_events.scan_id  →  scans.id    NOT NULL
activity_events.agent_id →  agents.id   NOT NULL
```

所以"CTF2 的事件"必须挂在一条真实的 `Scan` 与 `Agent` 上 ——
这不是可以绕过的形状问题：前端 `ActivityEvent.scanId` 的 TS 类型
就是 `string`（不可空），而全局事件流 `/api/v1/events` 会把多个
扫描的 agent 按 id 索引进一个 map。

**所以每条 CTF2 解题任务都建一条真实的 Scan**（`goal=captureFlag`）。
这个决定带来三个好处，都不是巧合：

1. **前端 Activity 页面零改动**就能看到 CTF2 的进度 ——
   这正是决策 7 要的效果；
2. `scans.flag` 那一列（任务 18 加的）**直接可用**；
3. 解题过程出现在扫描列表里，用户有**一个地方**能看全部活动。

代价说清楚：`scans` 表的授权留痕六列是非空的，而这六列在 CTF2
场景下的语义与渗透测试**不同** —— 题目环境是平台分配的，
授权方是平台而不是用户。所以这里填的是**如实标注**（见
`_AUTHORIZATION_NOTE`），而不是冒充用户签过的声明。

⚠️ **同步阶段的"题目发现"没有 scan 可挂。**
那发生在用户点"同步"时，那时还没有任何解题任务。所以
`emit_challenge_discovered` 是在**建解题 scan 之后**补发的
（把它理解为"这次解题的开始"。同步本身只写日志）。
把它硬做成一条无 scan 的事件需要改 `activity_events` 的形状，
而那是一次契约修订 —— 不该由这个模块悄悄完成。

## 事件类型映射（8 种类型里挑，映射理由逐个写）

| CTF2 事件 | `type` | `agentPhase` | 为什么 |
| --- | --- | --- | --- |
| 题目发现 | `discovery` | `recon` | 它就是"发现了要打的东西" |
| 环境就绪 | `discovery` | `recon` | **不选 `completed`** —— 那是任务完成，会让活动流显示"已完成"而题还没做 |
| 候选 flag 待确认 | `poc_generated` | `exploit` | 它产出了一个**待验证的产物**，与 PoC 的地位相同 |
| 提交成功 | `verified` | `verify` | 平台确认了 —— 这是唯一能说"verified"的时刻 |
| 提交失败（被判错） | `blocked` | `exploit` | **不选 `failed`**：它受阻但不是任务失败，agent 还会换思路 |
| 提交出错（没送达） | `failed` | `verify` | 我们确定这次提交没生效，是操作层面的失败 |
| 放弃 | `failed` | `exploit` | 这道题到此为止 |

把"被判错"记成 `blocked` 而不是 `failed` 是有实际区别的：
前者在前端的投影里是"受阻"（黄色、可继续），后者是"失败"（红色）。
记错的后果是**每题都会闪一次红**（因为猜错 flag 是常态），
而满屏红色会让人不再看那个颜色。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.core.text import strip_nul, strip_nul_deep
from app.core.timeutil import now_utc
from app.core.vocab import (
    SCAN_ORIGIN_CTF2,
    TERMINAL_SCAN_STATUSES,
    ActivityEventType,
    AgentPhase,
    AgentRole,
    AgentStatus,
    ScanGoal,
    ScanStatus,
)
from app.events.models import Agent
from app.events.service import append_event, record_agent_activity
from app.kernel.client import Ctf2Error
from app.kernel.models import Ctf2ChallengeStatus
from app.scans.models import Scan

logger = logging.getLogger("suvex.ctf2")

# 解题 agent 的固定后缀。一条解题 scan 只有**一个** agent ——
# 这是刻意的：CTF2 的解题循环是单线程的对话，不是 strix 那种
# 多 agent 拓扑。伪造多个 agent 会让名册页显示一堆空壳。
SOLVER_AGENT_ID = "ctf2-solver"

# 授权留痕的措辞。
#
# ⚠️ 这句话**必须如实**。它是一条会被审计看到的记录，而"CTF2 的
# 题目环境是平台分配的"与"用户声明他有权测试这个目标"是两件事。
# 写成后者（照抄 `SUPEX_SCAN_AUTHORIZATION_STATEMENT`）会让审计
# 记录里出现一句用户从没同意过的话 —— 那比留空更糟。
_AUTHORIZATION_NOTE = "CTF2 平台分配的题目环境（授权方为平台，用完即销毁）"


def _default_provider_id(db: Session) -> str:
    """这次解题用的 provider id。

    ★ 为什么必须填它：`scans.provider_id` 是 **NOT NULL**，而它的语义是
    "这次运行当时用的是哪个 provider"（见 `scans/models.py` 里那段说明）。
    CTF2 的解题**确实**用了某个 provider（`runner.build_spec` 取的就是
    默认那个），所以填它是准确的，不是凑数。

    ⚠️ 拿不到时抛错而不是填空串：空串会往 `scans` 里写一条"用了空
    provider"的假记录，而那条记录会被当成事实读出来。宁可让调用方
    知道"现在建不出这条 scan"（并因此不发事件），也不要在数据库里
    留一条撒谎的行。

    与 `runner.build_spec` 的检查是**同一件事的两处表达**：
    那里是"没有 provider 就解不了题"，这里是"没有 provider 就写不出
    那条解题记录"。两处都有，是因为它们在不同的时机失败。
    """
    from app.providers.models import ProviderAccount

    provider = (
        db.execute(
            select(ProviderAccount).order_by(
                ProviderAccount.is_default.desc(), ProviderAccount.created_at
            )
        )
        .scalars()
        .first()
    )
    if provider is None:
        raise Ctf2Error(
            "还没有配置 LLM provider，无法为这次解题建立记录。"
            "请先在设置页添加一个 provider。"
        )
    return provider.id


def ensure_solve_scan(db: Session, challenge) -> Scan:  # noqa: ANN001
    """拿到（或建出）这道题的解题 scan。

    ## 幂等

    同一道题被解两次（重跑）时**复用同一条 scan**，不新建。
    理由：`Ctf2Challenge.scan_id` 是一个单值列，新建会让它指向新的那条，
    而旧的那条变成孤儿 —— 于是"上次解题的活动流"再也关联不到这道题。

    复用时把状态**重置为 running**（因为它又要跑了），这是对的：
    那条 scan 的语义是"这道题当前的解题过程"，不是"某一次历史运行"。
    """
    if challenge.scan_id:
        existing = db.get(Scan, challenge.scan_id)
        if existing is not None:
            # ⚠️ 必须在改写状态**之前**读它 —— 下面那句会把 status 设成
            #    running，之后再看就永远是 running 了。
            was_running = existing.status == ScanStatus.RUNNING.value
            existing.status = ScanStatus.RUNNING.value
            existing.finished_at = None
            existing.duration_sec = None
            # ★ `started_at` 只在"**从终态重新开始**"时重置。
            #
            # 为什么不能无条件重置：本函数会被**每一条事件**调用
            # （`_emit` 的第一件事就是它）。无条件写 `now_utc()` 会把
            # 起点一路往后推到最后一个事件，于是耗时变成一个
            # 荒谬的小数字 —— 实测：一次跑了 144 秒的解题，收尾时
            # `duration_sec` 记成了 **5**。
            #
            # 判据用 `was_running` 而不是"扫描表里有没有它"：
            #   · 首次开始（上一轮已结束 → 现在是终态）→ 重置起点 ✅
            #   · 本轮解题中途的每一条事件（状态已是 running）→ 不碰 ✅
            #
            # 留个旧起点的后果是"耗时"这一列系统性地说谎（曾出现
            # 一条 3.5 分钟的题显示 18 小时），而它同时喂给
            # 「疑似卡死」判定 —— 那会让界面一直挂着一条假警报。
            if not was_running:
                existing.started_at = now_utc()
            existing.goal = ScanGoal.CAPTURE_FLAG.value
            return existing

    moment = now_utc()
    scan = Scan(
        id=new_id("scan"),
        # 名字带上题目标题，让扫描列表里一眼能认出这是哪道题。
        name=f"[CTF2] {challenge.title}"[:200],
        # 目标用题目的远端 id 而不是 access_url：URL 会随环境重启变化，
        # 而"这个 scan 打的是哪道题"必须稳定。
        target=f"ctf2:{challenge.remote_id}"[:512],
        # 归类到 `service`：CTF2 的题目环境大多是一个裸 TCP 服务
        # （连上就给你交互），而不是一个网站。用 `url` 会让预检去探
        # http/https，而那对一个只讲自己协议的端口必然失败
        # —— 见 `vocab.TargetKind.SERVICE` 的说明。
        target_kind="service",
        status=ScanStatus.RUNNING.value,
        skills=[],
        goal=ScanGoal.CAPTURE_FLAG.value,
        # ★ v0.27：标明这条行是 CTF2 借的壳，不是一次真扫描。
        #
        # 为什么必须有这一列、而不能让 scans 侧"现场查关联表"：
        # 一旦有人对这条壳点了「重跑」，`rerun_scan` 会建一条**新的**
        # 同 target 的 scan，而那条新壳**没有任何 challenge 指向它** ——
        # 查关联表会把它判成真扫描，然后 strix 拒收 `ctf2:…` 这个 target、
        # argparse 退 2、被记成「已完成」。实测（2026-10-08）就这么发生了两次。
        #
        # 见 `vocab.SCAN_ORIGIN_CTF2` 的完整事故记录。
        origin=SCAN_ORIGIN_CTF2,
        # ⚠️ NOT NULL，且必须是**真实存在**的 provider —— 见
        # `_default_provider_id` 的说明。
        provider_id=_default_provider_id(db),
        started_at=moment,
        # 授权留痕：**如实标注**，不冒充用户声明。见模块常量。
        authorized_by="ctf2",
        authorization_statement=_AUTHORIZATION_NOTE,
        authorization_confirmed_at=moment,
        authorization_received_at=moment,
        authorization_ip="",
        authorization_user_agent="suvex-ctf2",
    )
    db.add(scan)
    db.flush()  # 拿到 id 给下面用，但**不 commit** —— 由调用方决定事务边界
    challenge.scan_id = scan.id
    return scan


def ensure_solver_agent(db: Session, scan_id: str) -> Agent:
    """拿到（或建出）这条 scan 的解题 agent。

    ## ★ v0.49：新建 agent 时把 `scans.agent_count` 一起刷对

    修的是用户实测的那个界面错报：「明明在跑，列表说 0 个智能体」。

    根因：`ensure_solve_scan` 建 scan 行时 `agent_count` 走模型默认值 0，
    而 `refresh_scan_rollup`（唯一刷这个数的地方）**只在收尾时**被调用
    —— 于是整个解题过程中它一直是 0，尽管 `ctf2-solver` 已经在活动流里
    干活。用户看到「解题中 · 0 个智能体」，会以为根本没启动。

    实测（2026-10-20，Kali 生产库）：`agents` 表里 `role=exploiter`、
    `status=running`、`event_count=11` 的行好端端地在那里，而列表读的是
    `scans.agent_count` = 0。更迷惑的是**重跑复用旧 scan 行**时那个数
    会留着上一轮收尾时刷出的 1 —— 于是同一个界面上的数字忽对忽错。

    ## 为什么只在**新建**分支算，不在复用分支算

    复用分支是本函数的热路径：`_emit` 每发一条事件都会调到这里，而一次
    解题能发几百条。为了一个"创建时就该对"的数，每条事件都跑一次
    `COUNT(*)` 是拿得到的收益换不划算的代价。

    新建分支一次解题只走一遍，那次 `COUNT(*)` 是必须的 —— 它同时覆盖
    「这条 scan 上除了 solver 还挂着扫描侧建的报告 agent」那种情形
    （见 `finalize_solve_scan` 里关于 `ensure_runner_agent` 的说明）。
    """
    from app.scans.service import count_scan_agents

    agent_id = f"{scan_id}:{SOLVER_AGENT_ID}"[:64]
    agent = db.get(Agent, agent_id)
    if agent is not None:
        agent.status = AgentStatus.RUNNING.value
        return agent
    agent = Agent(
        id=agent_id,
        scan_id=scan_id,
        # ⚠️ 角色的取值来自 `AgentRole`（5 个值里没有"ctf solver"）。
        # 选 `exploiter` 而不是新加一个枚举值：角色是**前端有标签的**
        # 展示字段（契约 §2.4），加值要动前端。而 `exploiter` 的语义
        # （"负责把已知的弱点变成实际效果"）与解题恰好吻合。
        role=AgentRole.EXPLOITER.value,
        status=AgentStatus.RUNNING.value,
        target=f"ctf2:{scan_id}"[:512],
        event_count=0,
        last_active_at=now_utc(),
    )
    db.add(agent)
    db.flush()

    # ★ 口径复用 `count_scan_agents`，不在这里自己写一遍 COUNT ——
    #   两处各算一遍必然漂移，那正是它被抽出来的理由。
    #
    # ⚠️ 只写列、不 commit：本函数在 `_emit` 的事务中间被调用，提交边界
    #    归调用方（与 `refresh_scan_rollup` 的区别正在这里，它自己会 commit）。
    scan = db.get(Scan, scan_id)
    if scan is not None:
        scan.agent_count = count_scan_agents(db, scan_id)
    return agent


def _emit(
    db: Session,
    challenge,  # noqa: ANN001
    *,
    type: str,
    phase: str,
    summary: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """所有事件的公共出口。**不要在别处直接调 `append_event`。**

    集中在一个函数里是为了保证两件事始终成立：
    ① scan 与 agent 一定先被建出来（否则撞非空外键）；
    ② `detail.kind` 一定被填上（`append_event` 会因为缺它而抛 400
    —— 那是它在替我们守契约 §2.6）。

    ## ★ 建不出 scan 时**记 warning 并跳过**，不往上抛

    唯一的成因是"没配 provider"（见 `_default_provider_id`）。那时
    建不出解题 scan，于是发不出事件。

    为什么不抛：**状态机不该被一条附带信息卡住**。两个真实场景：

    * `scheduler.recover_orphans()` —— 它的目的是把遗留的 `solving`
      重置掉，而"这条事件发不出去"不该让那件事失败（否则那些题会
      永远停在"解题中"，正是不回收的那个坏结果）；
    * `runner` 的失败路径 —— 它正在写"这道题失败了"，而写不进去
      会让 worker 崩掉，把一条**已经确定的结果**丢掉。

    为什么不静默：warning 里带着题目 id，运维能看到。而"事件丢了
    但状态对了"远好于"状态没写且进程崩了"。

    ⚠️ 不 commit。事务边界由调用方掌握 —— 一个事件不该让
    "写题目状态"那个还没准备好的事务提前落地。

    ## ★★ NUL 字节必须在这里挡掉（实测崩过）

    实测（2026-10-01，`flag在哪？`那类需要解二进制的题）：模型把 doc
    文件按 `utf-16le` 解出来，输出里带着 `\\x00`，`run_shell` 的
    **stdout 原文**被记进 `tool_call` 的 `detail`，于是：

        sqlalchemy.exc.DataError: (psycopg.errors.UntranslatableCharacter)
        unsupported Unicode escape sequence
        DETAIL:  \\u0000 cannot be converted to text.

    PostgreSQL 的 `text` / `jsonb` **存不了 NUL 字节**，插进去是一条硬
    错误。后果不是"少一条事件"：异常从 `_emit` 抛出去会**掀掉整个
    worker** —— 一道本来解得好好的题，因为一条日志写不进去而崩掉。

    清洗放在 `_emit` 而不是各个 `emit_*` 里：这是**唯一出口**，放在这里
    将来新增的事件类型自动被覆盖 —— 而放在各调用点，下一个加事件的人
    不会知道有这么一条规矩（这正是本次踩坑的形状）。

    工具函数在 `app/core/text.py`，那条路径（scans 导入）早就接上了，
    只有 ctf2 这条没有。
    """
    try:
        scan = ensure_solve_scan(db, challenge)
    except Ctf2Error as exc:
        logger.warning(
            "CTF2 无法为题目 %s 建立解题记录，跳过这次事件（%s）：%s",
            challenge.id,
            type,
            exc.message,
        )
        return
    agent = ensure_solver_agent(db, scan.id)
    append_event(
        db,
        scan_id=scan.id,
        agent_id=agent.id,
        agent_phase=phase,
        type=type,
        # 摘要是一行文本，detail 是嵌套 JSON（NUL 可能埋在里面）——
        # 两者都要洗，漏掉任意一边都还是插不进去。
        summary=strip_nul(summary),
        detail=strip_nul_deep(detail) if detail is not None else None,
    )
    record_agent_activity(db, agent)
    logger.debug("CTF2 事件：%s %s", type, summary[:80])


def emit_challenge_discovered(db: Session, challenge) -> None:  # noqa: ANN001
    """题目被发现 —— 在建好解题 scan 之后发。

    见模块 docstring：同步阶段没有 scan 可挂，所以这一条实际表示
    "这次解题的开始"。
    """
    _emit(
        db,
        challenge,
        type=ActivityEventType.DISCOVERY.value,
        phase=AgentPhase.RECON.value,
        summary=f"开始处理题目「{challenge.title}」（{challenge.category or '未分类'}）",
        detail={
            "kind": ActivityEventType.DISCOVERY.value,
            "subject": challenge.title,
            "challengeId": challenge.id,
            "remoteId": challenge.remote_id,
            "category": challenge.category or "",
            "sandboxProfile": challenge.sandbox_profile,
        },
    )


def emit_environment_ready(
    db: Session, challenge, *, access_urls: list[str], polls: int
) -> None:  # noqa: ANN001
    """题目环境就绪。

    ⚠️ `accessUrls` 进 detail 是**故意的**：用户要能点开题目环境
    自己去看看（很多人会想手工验证一下）。而它不是凭据 ——
    题目环境本来就是给参赛者访问的。
    """
    _emit(
        db,
        challenge,
        type=ActivityEventType.DISCOVERY.value,
        phase=AgentPhase.RECON.value,
        summary=f"题目环境已就绪（{len(access_urls)} 个入口，轮询 {polls} 次）",
        detail={
            "kind": ActivityEventType.DISCOVERY.value,
            "subject": "题目环境",
            "accessUrls": list(access_urls),
            "polls": polls,
        },
    )


def emit_candidate_ready(db: Session, challenge, *, candidate) -> None:  # noqa: ANN001
    """候选 flag 产出，等待人工确认（safe 模式）。

    ★ 这是 safe 模式**最重要的一条事件** —— 它是唯一让用户知道
    "系统在等你了"的通道。缺了它，候选 flag 会安静地躺在库里，
    而用户以为系统还在跑。

    ## flag 本身**不进 summary**，进 detail

    summary 会被 `_clip` 截断后显示在活动流的一行里（本项目在
    `scans/flag.py` 里记过这个教训：flag 正好落在截断点之后，
    于是"拿到了却看不见"）。放进 detail 让它成为结构化字段，
    界面能单独渲染 + 一键复制。
    """
    _emit(
        db,
        challenge,
        type=ActivityEventType.POC_GENERATED.value,
        phase=AgentPhase.EXPLOIT.value,
        summary="已产出候选 flag，等待确认后提交（safe 模式）",
        detail={
            "kind": ActivityEventType.POC_GENERATED.value,
            "subject": "候选 flag",
            "flag": candidate.flag,
            "evidence": candidate.evidence,
            "confidence": candidate.confidence,
            "remainingWrongSubmissions": challenge.remaining_wrong_submissions,
        },
    )


def emit_submitted_ok(db: Session, challenge, *, flag: str) -> None:  # noqa: ANN001
    """平台确认 flag 正确。**唯一能说 `verified` 的时刻。**"""
    _emit(
        db,
        challenge,
        type=ActivityEventType.VERIFIED.value,
        phase=AgentPhase.VERIFY.value,
        summary="flag 已被平台确认，题目解出",
        detail={
            "kind": ActivityEventType.VERIFIED.value,
            "subject": "flag 提交",
            "flag": flag,
            "challengeId": challenge.id,
        },
    )


def emit_submitted_failed(db: Session, challenge, *, flag: str, reason: str) -> None:  # noqa: ANN001
    """平台判定 flag 错误。

    ⚠️ 类型是 `blocked` 而不是 `failed`（见模块 docstring 的表）：
    猜错在解题里是**常态**，记成失败会让每题都闪一次红。
    同时把 `remainingWrongSubmissions` 带出去 —— 用户最需要知道的
    是"还剩几次"。
    """
    _emit(
        db,
        challenge,
        type=ActivityEventType.BLOCKED.value,
        phase=AgentPhase.EXPLOIT.value,
        summary=f"平台判定 flag 不正确（还剩 {challenge.remaining_wrong_submissions} 次机会）",
        detail={
            "kind": ActivityEventType.BLOCKED.value,
            "subject": "flag 提交",
            "flag": flag,
            "reason": reason,
            "remainingWrongSubmissions": challenge.remaining_wrong_submissions,
        },
    )


def emit_abandoned(db: Session, challenge, *, reason: str) -> None:  # noqa: ANN001
    """放弃这道题（环境起不来 / 回合用尽 / 次数用尽 / 服务重启）。

    ⚠️ `reason` **原样展示**，不做翻译。它是系统给出的具体原因
    （"轮询 200 次仍未就绪"），而把它换成一个笼统的"解题失败"
    会让用户完全不知道下一步该干什么。
    """
    _emit(
        db,
        challenge,
        type=ActivityEventType.FAILED.value,
        phase=AgentPhase.EXPLOIT.value,
        summary=f"已放弃：{reason}"[:500],
        detail={
            "kind": ActivityEventType.FAILED.value,
            "subject": challenge.title,
            "reason": reason,
            "challengeId": challenge.id,
        },
    )


# ══════════════════════════════════════════════════════════
# ★ 过程事件（`tool_call` / `reasoning`）—— 活动流里的"解题过程"
# ══════════════════════════════════════════════════════════
#
# ## 为什么必须有这两个
#
# 实测（2026-09-30）：一道题从零解出（`office2john` 提 hash →
# `hashcat` 爆出口令 → `msoffcrypto-tool` 解密 → 读到 flag），
# 而活动流里**只有 5 行状态**：
#
#     题目环境已就绪 / 已放弃：回合用尽 / flag 已被平台确认，题目解出
#
# 用户的原话是"没过程解题过程就这样？"—— 而 `ChallengeCard` 上
# 那个「查看解题过程」的链接正指向这个活动流。**承诺与实现对不上。**
#
# 根因：`ActivityEventType` 里本来就有 `tool_call` 与 `reasoning`
# （strix 走的正是这两类，前端 `EventRow` 也有完整的渲染分支与图标），
# 唯独 ctf2 这条路径**从不发**它们 —— 过程只进了
# `SolveOutcome.transcript`，最后写成一个磁盘文件。
#
# ## 载荷形状照抄 `scans/strix_events.py`
#
# 不是为了统一而统一：前端的 `EventRow` 是**按 `detail.kind` 分支渲染**的，
# 形状不一致会让过程行显示成空白（而不会有任何报错）。


def emit_tool_call(
    db: Session,  # noqa: ANN001
    challenge,
    *,
    tool: str,
    command: str,
    output: str,
) -> None:
    """一次工具调用。

    ⚠️ `exitCode` / `durationMs` 恒为 `null`：解题循环不记录单次调用的
    退出码与耗时（沙箱里的命令通过 `docker exec` 跑，我们只拿到合并后的
    文本）。契约允许 `null`，而编一个 `0` 会让界面显示一个假的成功码。
    """
    from app.scans.strix_events import MAX_SUMMARY, MAX_TEXT  # 循环导入的妥协

    _emit(
        db,
        challenge,
        type=ActivityEventType.TOOL_CALL.value,
        phase=AgentPhase.EXPLOIT.value,
        summary=_clip(f"{tool} {command}", MAX_SUMMARY),
        detail={
            "kind": ActivityEventType.TOOL_CALL.value,
            "tool": tool,
            "command": _clip(command, MAX_TEXT),
            "exitCode": None,
            "durationMs": None,
            "output": _clip(output, MAX_TEXT),
        },
    )


def emit_reasoning(db: Session, challenge, *, text: str) -> None:  # noqa: ANN001
    """模型这一轮在想什么 / 结论是什么。

    `intent` 固定为 `narration`：解题循环拿到的是模型的**对外发言**
    （它在 `ACTION:` 之前写的那段话），不是 strix 那种独立的
    `think` 工具调用。契约的 8 种类型里没有"发言"这一类，所以用
    `intent` 保住这个区别 —— 与 `strix_events._reasoning` 同一套约定。
    """
    from app.scans.strix_events import MAX_SUMMARY, MAX_TEXT

    _emit(
        db,
        challenge,
        type=ActivityEventType.REASONING.value,
        phase=AgentPhase.EXPLOIT.value,
        summary=_clip(text, MAX_SUMMARY),
        detail={
            "kind": ActivityEventType.REASONING.value,
            "text": _clip(text, MAX_TEXT),
            "intent": "narration",
        },
    )


def emit_progress(
    db: Session,  # noqa: ANN001
    challenge,
    *,
    kind: str,
    summary: str,
    payload: dict[str, str],
) -> None:
    """★ 过程事件的**统一入口** —— 直接给 `solve_in_sandbox` 当回调用。

    ## 为什么要有这一层

    `solve_in_sandbox` 是 async 且按设计不碰 DB，所以它通过回调
    把过程送出来（签名 `(kind, summary, payload)`）。回调在 worker 侧
    被调用时只拿着这三个值 —— 让它自己去 `if kind == ...` 分支、
    再逐个拼 `detail` 的话，那份"形状知识"就会跑到 worker 里去，
    而这个模块存在的意义正是把事件形状收在一处。

    所以这里做**唯一的**一处分派：`kind` 决定事件类型与载荷，
    worker 只管传。

    ⚠️ 未知 `kind` **记 warning 并丢弃**，不抛：
    它只可能来自我们自己新增的过程种类（比如将来加"思考链"），
    而为了一个展示层的新种类让整个解题崩掉是不划算的。
    但要**出声** —— 静默丢弃会让"新加的过程类型在界面上不出现"
    变成一个查不出原因的现象。
    """
    if kind == "reasoning":
        emit_reasoning(db, challenge, text=payload.get("text") or summary)
    elif kind == "tool_call":
        emit_tool_call(
            db,
            challenge,
            tool=payload.get("tool") or "",
            command=payload.get("command") or summary,
            output=payload.get("output") or "",
        )
    else:
        logger.warning("CTF2 未知的过程事件类型，已丢弃：%s", kind)


def _clip(text: str, limit: int) -> str:
    """截断到 `limit`，并**明示被截断**。

    ⚠️ 与 `scans/strix_events.py` 的同名函数保持同样的行为：截断处
    加一个标记而不是静默切掉 —— 一段被悄悄截半的输出会让人以为
    命令就产出了这么多。
    """
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（已截断，原长 {len(text)} 字符）"


def finalize_solve_scan(db: Session, challenge) -> None:  # noqa: ANN001
    """把解题 scan 收尾：状态、结束时间、耗时、汇总列。

    ## ★ 必须显式收尾，否则扫描列表永远显示"运行中"

    实测（2026-09-30，用户截图）：一道题在几分钟前就解完并提交成功，
    而扫描列表里那条 `[CTF2] hashcat` 仍然显示：

        运行中 · TCP 服务 · **0 个智能体** · ⚠ 疑似卡死 · 已静默 3 小时 58 分

    三个症状同一个根因：`ensure_solve_scan` 在**开头**把它建成 `running`，
    而这条路径上**没有任何地方把它改成终态** —— 普通扫描由
    `scans/scheduler.py` 负责收尾，而 CTF2 的解题不经过调度器
    （它直接调 `runner.run_worker`）。

    于是：
    · `status` 停在 `running` → 列表显示"运行中"，静默久了还被
      疑似卡死检测盯上（那条警告会一直挂着）；
    · `agent_count` 停在建行时的 0（`refresh_scan_rollup` 从没被调用），
      于是明明有 `ctf2-solver` 在活动流里干活，列表却说"0 个智能体"；
    · `finished_at` 为空 → 耗时永远显示"进行中"。

    按题目最终状态映射 scan 状态：解出 → `completed`；其余 → `failed`。
    这不是装饰 —— 扫描列表是全站唯一的活动总入口，它显示"运行中"
    而实际早已结束，会让人以为还有进程在跑。

    ⚠️ 计数口径不在这里重写：直接复用 `refresh_scan_rollup`。
    两处各算一遍必然漂移，而那正是它当初被抽出来的理由。

    ## ★ v0.27：solver agent 也必须在这里收尾（实测 20 个僵尸）

    同一个根因的**第二半**，上一版漏了：`ensure_solver_agent` 把 agent
    建成 `running`（`ensure_solve_scan` 每被调一次就重设一遍），而这条
    路径上没有任何地方把它改成终态 —— `scans` 那边由
    `emit_lifecycle_event` 顺带收尾（见 `scans/service.py` 里那句
    `agent.status = DONE/FAILED`），CTF2 这条**没有对应的那句**。

    实测（2026-10-08，Kali 生产库）：20 个 `…:ctf2-solver` agent 全部
    停在 `running`，其中最早的是 09-30 —— 而那 20 道题**早就跑完了**
    （16 解出、2 失败、2 被重置）。于是智能体页上它们永远转圈，
    与扫描列表显示的"已完成"自相矛盾。

    映射与 scan 状态**同源**（都不另立口径）：
    解出 → `done`；其余（失败 / 放弃 / 被重置）→ `failed`。
    """
    from app.events.models import Agent
    from app.scans.models import Scan
    from app.scans.service import refresh_scan_rollup

    if not challenge.scan_id:
        return
    scan = db.get(Scan, challenge.scan_id)
    if scan is None:
        return

    solved = challenge.status == Ctf2ChallengeStatus.SOLVED.value
    scan.status = ScanStatus.COMPLETED.value if solved else ScanStatus.FAILED.value
    scan.finished_at = now_utc()
    if scan.started_at is not None:
        delta = (scan.finished_at - scan.started_at).total_seconds()
        scan.duration_sec = max(0, int(delta))

    # ★ agent 的终态。用 id 前缀定位（`<scan_id>:ctf2-solver`）而不是
    #   `Agent.scan_id` 扫全表：这条 scan 上除了 solver 还可能挂着
    #   `scans` 模块建的 reporter agent（`ensure_runner_agent`，用于
    #   写"扫描完成"那类生命周期事件）。**那个不该由这里改** ——
    #   它的状态归 `scans` 管，两边都写会互相覆盖。
    agent = db.get(Agent, f"{scan.id}:{SOLVER_AGENT_ID}"[:64])
    if agent is not None:
        agent.status = (
            AgentStatus.DONE.value if solved else AgentStatus.FAILED.value
        )

    # 先 flush 把状态落进当前事务，再交给 `refresh_scan_rollup`
    # （它自己 commit）。顺序反了会先写下一个"已结束但计数还是 0"
    # 的中间态，随后被覆盖 —— 白写一次，且在并发读下可见。
    db.flush()
    refresh_scan_rollup(db, scan)


def promote_solve_scan_to_solved(db: Session, challenge) -> None:  # noqa: ANN001
    """解题**事后**被确认为已解出时，把那条 scan 的状态翻成 `completed`。

    ## ★ 修的是什么（实测，2026-10-20，Kali 生产库）

    `finalize_solve_scan` 只在 **worker 收尾那一刻**看一眼题目状态：

        solved = challenge.status == Ctf2ChallengeStatus.SOLVED.value
        scan.status = COMPLETED if solved else FAILED

    而 safe 模式下那一刻题目还是 `candidate_ready` —— 候选在**等人确认**。
    于是 scan 被记成 `failed`，用户随后手动确认成功、题目变 `solved`，
    **没有任何代码回头改那行 scan**。

    实测时间线（SimpleRev，`remote_id=d79788be-…`）：

        16:53:09  worker 收尾，challenge=candidate_ready → scan=failed (582s)
        16:53:50  人工提交 flag{KLDQCUDFZO} → correct → challenge=solved

    「失败」比确认早了 41 秒，而界面上它**永远**停在那里。用户看到的是
    "suvex 把这题搞失败了"，事实是这题已经解出。

    ## ★★ 只翻 `status`（和 solver agent 的终态），**不动** 时间与耗时

    `finished_at` / `duration_sec` 记的是 **worker 实际跑了多久**。把它们
    按"确认时刻"重算，会把用户**等待确认**的那段时间算进解题耗时 ——
    那是另一个错数，而且更难发现（它看上去只是一个偏大的数字）。
    收尾那一刻算出来的 582 秒是**对**的，该保留。

    同理不动 `refresh_scan_rollup`：`agent_count` / `findings` 与题目状态
    无关，重刷只会多一次 commit（它自己会 commit，见 `app/core/db.py` 里
    那条"内层函数替外层定事务边界"的告诫）。

    ## 幂等

    已经 `completed` 就什么都不做。这条会被人工确认路径反复调用
    （同一个 flag 可以再交一次），重复翻状态没有意义，但重复 commit 有代价。

    ## ★ 只在 scan **已经收尾**之后才翻

    这条同时覆盖自动提交那条路径：auto 模式在 worker **跑着的时候**就提交
    候选，那时 scan 还是 `running` —— 把它翻成 `completed` 会让界面显示
    "已完成"而进程还在干活。而正常收尾时 `finalize_solve_scan` 自己会
    按题目状态算出 `completed`，不需要这里帮忙。

    所以判据是"scan 已终态 **且** 不是 `completed`" —— 那恰好就是
    「worker 收尾时记成 failed，之后人工确认成功」这一个形状。
    """
    from app.scans.models import Scan

    scan_id = getattr(challenge, "scan_id", None)
    if not scan_id:
        return
    scan = db.get(Scan, scan_id)
    if scan is None:
        return
    if scan.status not in {s.value for s in TERMINAL_SCAN_STATUSES}:
        return
    if scan.status == ScanStatus.COMPLETED.value:
        return
    scan.status = ScanStatus.COMPLETED.value

    # solver agent 与 scan 同源（同 `finalize_solve_scan` 的映射）：
    # 它当时也被记成了 `failed`，两处不一致会在智能体页上继续显示失败。
    agent = db.get(Agent, f"{scan.id}:{SOLVER_AGENT_ID}"[:64])
    if agent is not None:
        agent.status = AgentStatus.DONE.value


__all__ = [
    "SOLVER_AGENT_ID",
    "emit_abandoned",
    "emit_candidate_ready",
    "emit_challenge_discovered",
    "emit_environment_ready",
    "emit_progress",
    "emit_reasoning",
    "emit_submitted_failed",
    "emit_submitted_ok",
    "emit_tool_call",
    "ensure_solve_scan",
    "ensure_solver_agent",
    "finalize_solve_scan",
    "promote_solve_scan_to_solved",
]
