"""本地复盘任务（`kind='local'`）的测试（v0.43）。

## 这个文件守什么

`kind='local'` 是应急响应能不能**从界面发起**的关键：它让一批本机上传
的证据包变成一个可开跑的任务，而**不需要绑 CTF2 账号**。

它是三处配合的结果，每一处单独看都"很合理"，只有一起改才成立：

1. `Ctf2Competition.account_id` 可空 + `kind_valid` 接受 `'local'`
   （迁移 `d5a9e3c72f81`）；
2. `service._requires_platform()` 对 local 返回 False，于是 `start_solve`
   不再要求账号；
3. `runner._run_worker` 按 `platform_backed` 跳过三次平台调用。

## ★ 为什么每条测试都要"证明另一侧没被顺手放开"

这次改动的风险方向是**过度放开**：为了让复盘任务跑起来，很容易把
"必须绑账号"这条对**平台题**仍然成立的约束一起改松。那不会报错，
症状是"某道需要平台的题进到了一个没有凭据的流程里"，而它会在很深
的地方才炸出来（实测过：错误信息说"同步可能没完成"）。

所以下面每一条正向断言旁边，都有一条反向断言。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.ctf2 import service
from app.ctf2.models import Ctf2Challenge, Ctf2Competition
from app.core.vocab import Ctf2CompetitionKind


# ══════════════════════════════════════════════════════════
# 一、批次：本地复盘有自己的批次，且只有一个
# ══════════════════════════════════════════════════════════


def test_local_batch_has_no_account(db_session: Session) -> None:
    """★ 本地批次 `account_id` 是 NULL —— 这正是把那一列改成可空的理由。

    如果它挂一个账号，`ondelete=CASCADE` 会在解绑时把复盘记录连带删掉 ——
    而复盘记录指向的是唯一一份证据包。
    """
    batch = service.ensure_local_batch(db_session)

    assert batch.kind == Ctf2CompetitionKind.LOCAL
    assert batch.account_id is None


def test_local_batch_is_reused_not_recreated(db_session: Session) -> None:
    """反复调用拿到**同一行**。

    用 `new_id()` 现造的话，界面上"本地复盘"会越堆越多，而每个里面
    只有一道题 —— 那是很难收拾的体验问题（而它不会报错）。
    """
    first = service.ensure_local_batch(db_session)
    second = service.ensure_local_batch(db_session)

    assert first.id == second.id


def test_local_batch_remote_id_is_fixed_literal(db_session: Session) -> None:
    """★ `remote_id` 必须是**固定字面量**，不能现造。

    `uq_ctf2_competitions_account_remote` 建在 `(account_id, remote_id)` 上，
    而本地批次的 `account_id` 是 NULL —— PostgreSQL 里 NULL 不等于 NULL，
    所以这条唯一约束对它**形同虚设**。唯一性只能靠 `remote_id` 自己。
    """
    batch = service.ensure_local_batch(db_session)

    assert batch.remote_id == "local-reviews"


def test_local_batch_is_not_practice(db_session: Session) -> None:
    """★ local 与 practice 必须是两个 kind。

    `practice` 的语义是"平台上某个练习集"，`_remote_ids()` 会拿它的
    `remote_id` 当 `practice_id` 去打平台。把本地任务塞进一个 practice
    批次里，worker 会拿一个自造 id 去请求不存在的路径 → 404 → 题失败，
    而错误信息说的是"同步可能没完成"。
    """
    batch = service.ensure_local_batch(db_session)

    assert batch.kind != "practice"


# ══════════════════════════════════════════════════════════
# 二、建任务：分类与镜像由服务端定，不由调用方挑
# ══════════════════════════════════════════════════════════


def test_create_local_review_uses_ir_category(db_session: Session) -> None:
    challenge = service.create_local_review(db_session, title="一次复盘")

    assert challenge.category == "ir-root-cause"


def test_create_local_review_picks_ir_image(db_session: Session) -> None:
    """★★ 镜像必须是 `ctf2-sandbox-ir`，不能落列默认值 `ctf2-sandbox`。

    这条是 2026-10-21 真跑踩出来的：`run_ir_closed_loop.py` 最初没显式
    写 `sandbox_profile`，于是落了列默认值（基础镜像），容器里没有
    `tshark` / `fls` 一类命令 —— 而模型的反应是"我没有工具"，不是
    "镜像选错了"。这个错误的全部代价是白跑一轮。
    """
    challenge = service.create_local_review(db_session, title="镜像要对")

    assert challenge.sandbox_profile == "ctf2-sandbox-ir"


def test_create_local_review_status_is_unsolved(db_session: Session) -> None:
    """建成时是"未开始"，不是"解题中" —— 开跑是另一步。"""
    challenge = service.create_local_review(db_session, title="状态")

    assert challenge.status == "unsolved"


def test_create_local_review_starts_with_no_candidate(db_session: Session) -> None:
    """复盘任务永远没有 flag —— `candidate_flag` 必须是 None。

    不显式写的话，如果将来列默认值变了，一道复盘题会带着一个候选
    flag 出现，而 `ck_ctf2_challenges_candidate_never_with_confirmed`
    只挡"同时有候选与确认"，挡不住"不该有候选却有"。
    """
    challenge = service.create_local_review(db_session, title="无 flag")

    assert challenge.candidate_flag is None
    assert challenge.confirmed_flag is None


def test_create_local_review_title_is_stripped(db_session: Session) -> None:
    """标题去首尾空白 —— 它会进报告标题与工单文件名。"""
    challenge = service.create_local_review(db_session, title="  带空白  ")

    assert challenge.title == "带空白"


def test_create_local_review_counts_challenges(db_session: Session) -> None:
    """每建一道题，批次的 `challenge_count` 加一（界面靠它显示"几个复盘"）。"""
    batch = service.ensure_local_batch(db_session)
    before = batch.challenge_count

    service.create_local_review(db_session, title="第一道")
    service.create_local_review(db_session, title="第二道")

    db_session.refresh(batch)
    assert batch.challenge_count == before + 2


def test_create_local_review_remote_id_is_unique_per_challenge(db_session: Session) -> None:
    """★ 两道本地题的 `remote_id` 不能相同。

    `uq_ctf2_challenges_competition_remote` 是 `(competition_id, remote_id)`
    上的唯一约束，而本地题都在同一个批次里 —— 现造 id 时若用了固定值，
    第二道题会直接撞唯一约束，报错指向一个与"我要建第二道复盘"无关的
    约束名。
    """
    first = service.create_local_review(db_session, title="甲")
    second = service.create_local_review(db_session, title="乙")

    assert first.remote_id != second.remote_id


# ══════════════════════════════════════════════════════════
# 三、平台门禁：local 放开，其余收紧
# ══════════════════════════════════════════════════════════


def test_local_review_does_not_require_platform(db_session: Session) -> None:
    """★ 本地复盘不需要 CTF2 账号 —— 它一次都不碰平台域名。"""
    challenge = service.create_local_review(db_session, title="不需要账号")

    assert service._requires_platform(db_session, challenge) is False


def test_practice_challenge_still_requires_platform(
    db_session: Session, ctf2_challenge: Ctf2Challenge
) -> None:
    """★★ 反向断言：**平台题照旧要求账号**。

    这是本次改动最危险的失败模式 —— 为了让复盘任务跑起来，很容易把
    "必须绑账号"一起改松。那不会报错，症状是"某道需要平台的题进到了一个
    没有凭据的流程里"，而错误会在很深的地方才出现。
    """
    assert service._requires_platform(db_session, ctf2_challenge) is True


def test_competition_challenge_requires_platform(
    db_session: Session, ctf2_competition: Ctf2Competition
) -> None:
    """赛事题同样要求账号（它不是 practice，走的是同一条收紧分支）。"""
    challenge = Ctf2Challenge(
        id="ctf2chal_comp_only",
        competition_id=ctf2_competition.id,
        remote_id="comp-1",
        title="赛事题",
        category="web",
        status="unsolved",
        sandbox_profile="ctf2-sandbox",
    )
    db_session.add(challenge)
    db_session.flush()

    assert service._requires_platform(db_session, challenge) is True


def test_unknown_competition_defaults_to_requiring_platform(
    db_session: Session,
) -> None:
    """★ 查不到赛事行时**按需要平台处理**（收紧）。

    外键保证赛事行平时一定在，真查不到说明数据坏了。这时多要一个账号
    不会造成错的后果；而放过一个本该有账号的题，会让 worker 在后面某个
    位置拼出一个空凭据去打平台，报错与真实原因相距很远。

    ⚠️ 注意这与 `_assert_submission_supported` 的"查不到就放行"方向
       相反，两处理由不同（见两个函数的 docstring）。
    """
    orphan = Ctf2Challenge(
        id="ctf2chal_orphan",
        # 一个不存在的 competition_id。外键在 flush 时才校验，
        # 而这里要测的正是"查不到"这个分支 —— 用一个真实存在但
        # 已删的行无法构造，所以直接 new 一个对象不写库。
        competition_id="ctf2comp_does_not_exist",
        remote_id="orphan",
        title="孤儿题",
        category="web",
        status="unsolved",
        sandbox_profile="ctf2-sandbox",
    )

    assert service._requires_platform(db_session, orphan) is True


def test_local_review_passes_submission_gate(db_session: Session) -> None:
    """★ 本地复盘要能过 `_assert_submission_supported`。

    那个函数的放行条件是 `kind == 'practice'` 或查不到赛事行。local
    两者都不满足，所以它必须被显式放行 —— 否则建完任务点"开始复盘"
    会得到一句 `CTF2_COMPETITION_SUBMIT_UNSUPPORTED`，而那句话说
    "赛事题目仍可同步与查看"，与用户正在做的事毫无关系。
    """
    challenge = service.create_local_review(db_session, title="过门禁")

    # 不抛就是通过
    service._assert_submission_supported(db_session, challenge)


def test_local_review_remote_ids_are_non_empty(db_session: Session) -> None:
    """★★ 本地题的 `practice_id` **不是空串** —— 反直觉但关键的事实。

    `_remote_ids()` 直接拿 `Ctf2Competition.remote_id` 当 `practice_id`，
    而本地批次的 `remote_id` 是 `local-reviews`。所以
    `start_environment_for()` 开头那道"缺远端标识就提前抛"的闸**不会拦住
    它** —— 它会带着一个自造的 id 真的去请求 CTF2 平台，然后 404。

    ★ 这条测试的价值：它证伪了"本地题因为没有远端 id 所以不会打平台"
      这个**看起来很合理**的推理。正确的结论相反 —— 正因为它有（自造的）
      远端 id，才**必须**在 worker 里按 `platform_backed` 显式跳过三次
      平台调用。少跳一次的表现是"复盘任务跑两秒就失败，错误说同步可能
      没完成"，一句与真实原因毫无关系的话。
    """
    challenge = service.create_local_review(db_session, title="有自造远端")

    remote = service._remote_ids(db_session, challenge.id)

    assert remote["practice_id"] == "local-reviews"
    assert remote["challenge_id"] == challenge.remote_id


# ══════════════════════════════════════════════════════════
# 四、审计动作：建复盘与解题是两个问题
# ══════════════════════════════════════════════════════════


def test_ir_task_create_audit_action_exists() -> None:
    """★ 建复盘有**专用**审计动作，不复用 `CTF2_SOLVE_START`。

    审计日志是给事后复盘的人看的，而"这次应急响应是谁建的"与
    "这道 CTF 题是谁点了解题"在追责时是两个不同的问题 —— 前者问
    "谁把一批生产日志交给了智能体"，后者问"谁在打靶"。
    """
    from app.core.vocab import AUDIT_ACTION_LABELS, AuditAction

    assert AuditAction.IR_TASK_CREATE == "ir.task_create"
    assert AuditAction.IR_TASK_CREATE in AUDIT_ACTION_LABELS
    # 标签必须是中文人话，不是 key 原样
    assert AUDIT_ACTION_LABELS[AuditAction.IR_TASK_CREATE] != AuditAction.IR_TASK_CREATE


def test_ir_task_create_is_not_a_ctf2_action() -> None:
    """★ 动作名不能挂在 `ctf2.` 前缀下。

    它是 `ir.` 族：这条动作写的是"把一批**本机日志**交给智能体"，
    而不是"在平台上打一道题"。挂在 ctf2 前缀下会让审计列表按域筛选时
    把应急响应混进 CTF2。
    """
    from app.core.vocab import AuditAction

    assert not AuditAction.IR_TASK_CREATE.startswith("ctf2.")


# ══════════════════════════════════════════════════════════
# 五、schema 对账：新增的 kind 必须三处一致
# ══════════════════════════════════════════════════════════


def test_competition_kind_literal_covers_enum() -> None:
    """★ `CompetitionKindLiteral` 必须与 `Ctf2CompetitionKind` 逐值一致。

    漏改的症状是 `GET /ctf2/competitions` 直接 500，报
    `Input should be 'competition' or 'practice'` —— 而数据库、worker、
    界面全都对，唯一错的是那一个联合类型。
    """
    from typing import get_args

    from app.ctf2.schemas import CompetitionKindLiteral
    from app.core.vocab import Ctf2CompetitionKind

    literal_values = set(get_args(CompetitionKindLiteral))
    enum_values = {member.value for member in Ctf2CompetitionKind}

    assert literal_values == enum_values


def test_competition_out_allows_null_account(db_session: Session) -> None:
    """★ `Ctf2CompetitionOut.accountId` 必须接受 None。

    local 批次没有账号。类型坚持 `str` 的话，序列化一条本地批次
    会抛 `ValidationError` —— 而那个错误发生在**返回响应时**，
    表现是整个 `GET /ctf2/competitions` 500，页面全白。
    """
    from app.ctf2.schemas import Ctf2CompetitionOut

    out = Ctf2CompetitionOut(
        id="ctf2comp_local",
        accountId=None,
        remoteId="local-reviews",
        kind="local",
        name="本地复盘",
        remoteStatus="",
        startAt=None,
        endAt=None,
        challengeCount=0,
        syncedAt="2026-10-21T10:00:00Z",
    )

    assert out.accountId is None
    assert out.kind == "local"


@pytest.mark.parametrize("kind", ["competition", "practice", "local"])
def test_all_kinds_are_accepted_by_schema(kind: str) -> None:
    """三个 kind 都能进 `Ctf2CompetitionOut`。"""
    from app.ctf2.schemas import Ctf2CompetitionOut

    out = Ctf2CompetitionOut(
        id="x",
        accountId=None,
        remoteId="r",
        kind=kind,  # type: ignore[arg-type]
        name="n",
        remoteStatus="",
        startAt=None,
        endAt=None,
        challengeCount=0,
        syncedAt="2026-10-21T10:00:00Z",
    )

    assert out.kind == kind
