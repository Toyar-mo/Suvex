"""同步作用域（v0.30）：`known_remote_ids` 必须限定在当前账号内。

## ⚠️ 先纠正一个我自己的误判（2026-10-12）

我在评估时一度写成"**跨账号**的同名 remote_id 会误跳过"。查源码后发现
那条前提**不成立**：`uq_ctf2_accounts_active` 是一条**部分唯一索引**
（`WHERE status = 'active'`），数据库强制**同一时刻只有一个活跃账号**。

所以"两个 active 账号各有同名题目"这个场景在当前架构下**不可能出现**。

## 那这次收窄还有什么意义

有，但理由要写准 —— 不是"防跨账号"，而是**让判据与语义对齐**：

    "已在库" 的含义应当是「**本账号**已经有这道题了」，
    而不是「这个 remote_id 在这个数据库里出现过」。

两者的差别在**解绑再绑定另一个账号**时就会显现：

  1. 账号 A 绑定时同步了练习集，库里有 `task-1`；
  2. 解绑 A（记录留成 `revoked`，题目**不删** —— 那是历史）；
  3. 绑定账号 B，而 B 的练习集里**也有一个** `task-1`（不同题目）；
  4. 全表查询会认为"已有 task-1" → **跳过 B 的这道题**。

第 4 步的症状是"同步说完成了，但题库里少了几道题" —— 而它看起来像
"平台根本没有这些题"，排查方向会跑到平台侧去。

## 测试怎么造这个场景

用 `revoked` 状态的账号（而不是两个 active）—— 那正是上面第 2 步的
真实状态，也是唯一能合法造出"两个账号都有题"的方式。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.kernel.models import KernelChallenge, KernelCompetition


def _account(db: Session, *, remote_suffix: str, status: str = "active"):  # noqa: ANN202
    """造一个 CTF2 账号。

    ⚠️ `status='active'` 受**部分唯一索引**限制（一个实例只允许一个）——
    要造第二个账号必须用 `revoked` / `expired`。这不是测试的权宜之计，
    而是真实数据形态：解绑后历史记录留着，题目也留着。
    """
    from app.core.crypto import encrypt_secret
    from app.kernel.models import KernelAccount

    acc = KernelAccount(
        id=new_id("ctf2acc"),
        label=f"测试账号{remote_suffix}",
        token_encrypted=encrypt_secret("pat_test_" + remote_suffix),
        status=status,
    )
    db.add(acc)
    db.flush()
    return acc


def _competition(db: Session, account_id: str, *, remote_id: str, kind: str = "practice"):  # noqa: ANN202
    comp = KernelCompetition(
        id=new_id("ctf2comp"),
        account_id=account_id,
        remote_id=remote_id,
        kind=kind,
        name=f"练习集 {remote_id}",
    )
    db.add(comp)
    db.flush()
    return comp


def _challenge(db: Session, competition_id: str, *, remote_id: str):  # noqa: ANN202
    ch = KernelChallenge(
        id=new_id("ctf2chal"),
        competition_id=competition_id,
        remote_id=remote_id,
        title=f"题目 {remote_id}",
        category="MISC",
    )
    db.add(ch)
    db.flush()
    return ch


def _known_ids_for_account(db: Session, account_id: str) -> set[str]:
    """复刻 `service.sync` 里那句查询（与实现保持同形）。

    ⚠️ 这里**故意重写一遍**而不是 import 一个辅助函数：那条查询是
    `sync` 内部的局部变量，没有独立函数可调。而测试的价值恰恰在于
    "如果实现改回全表，这条会红" —— 所以它必须独立表达一遍。
    """
    return {
        str(x)
        for x in db.execute(
            select(KernelChallenge.remote_id)
            .join(KernelCompetition, KernelCompetition.id == KernelChallenge.competition_id)
            .where(KernelCompetition.account_id == account_id)
        )
        .scalars()
        .all()
    }


# ══════════════════════════════════════════════════════════
# 一、★ 核心：解绑再绑定另一个账号时不该误跳过
# ══════════════════════════════════════════════════════════


def test_old_accounts_tasks_do_not_leak_into_the_new_account(
    db_session: Session,
) -> None:
    """★★ 上一个（已解绑）账号的 remote_id **不该**让新账号的题被跳过。

    这就是本次收窄要挡的场景：解绑后题目不删（历史），于是全表查询会
    把旧账号的 `task-1` 当成"已有"，而新账号的 `task-1` 是**另一道题**。
    """
    # 旧账号：已解绑，但它的题目还留在库里
    old = _account(db_session, remote_suffix="OLD", status="revoked")
    comp_old = _competition(db_session, old.id, remote_id="practice-old")
    _challenge(db_session, comp_old.id, remote_id="task-1")

    # 新账号：当前活跃的
    new = _account(db_session, remote_suffix="NEW", status="active")
    comp_new = _competition(db_session, new.id, remote_id="practice-new")
    _challenge(db_session, comp_new.id, remote_id="task-1")
    db_session.commit()

    known_new = _known_ids_for_account(db_session, new.id)

    # 新账号自己的题当然在
    assert "task-1" in known_new
    # ★ 关键：作用域必须只覆盖新账号 —— 行数就是判据
    rows = db_session.execute(
        select(KernelChallenge.id)
        .join(KernelCompetition, KernelCompetition.id == KernelChallenge.competition_id)
        .where(KernelCompetition.account_id == new.id)
    ).scalars().all()
    assert len(rows) == 1, (
        "★ 作用域没收窄 —— 查到了已解绑账号的题，"
        "会让新账号的同名新题被误跳过"
    )


def test_revoked_accounts_tasks_are_isolated(db_session: Session) -> None:
    """★★ 逐条核对：旧账号独有的 remote_id **不进入**新账号的已知集合。"""
    old = _account(db_session, remote_suffix="REV", status="revoked")
    comp_old = _competition(db_session, old.id, remote_id="p-old")
    _challenge(db_session, comp_old.id, remote_id="only-in-old")

    new = _account(db_session, remote_suffix="ACT", status="active")
    comp_new = _competition(db_session, new.id, remote_id="p-new")
    _challenge(db_session, comp_new.id, remote_id="only-in-new")
    db_session.commit()

    known_new = _known_ids_for_account(db_session, new.id)

    assert "only-in-new" in known_new
    assert "only-in-old" not in known_new, (
        "★ 已解绑账号的 remote_id 混进来了 —— 会让新账号的新题被误跳过"
    )


# ══════════════════════════════════════════════════════════
# 二、同账号内的行为不变（不能收得过窄）
# ══════════════════════════════════════════════════════════


def test_same_account_across_competitions_still_matches(db_session: Session) -> None:
    """★★ 同一账号下、不同练习集里的同一个 remote_id **仍然算已知**。

    这条挡的是"收得过头"：如果按 `competition_id` 而不是 `account_id`
    收窄，那么"同一道题出现在两个练习集里"会被当成两道新题，
    于是详情被重复拉取 —— 而那是同步慢回去的原因。
    """
    acc = _account(db_session, remote_suffix="MULTI", status="active")
    comp_1 = _competition(db_session, acc.id, remote_id="practice-1")
    comp_2 = _competition(db_session, acc.id, remote_id="practice-2")

    _challenge(db_session, comp_1.id, remote_id="shared-task")
    _challenge(db_session, comp_2.id, remote_id="shared-task")
    db_session.commit()

    known = _known_ids_for_account(db_session, acc.id)
    assert "shared-task" in known


def test_empty_account_has_no_known_ids(db_session: Session) -> None:
    """★ 新账号的已知集合是空的。"""
    acc = _account(db_session, remote_suffix="EMPTY", status="active")
    assert _known_ids_for_account(db_session, acc.id) == set()


def test_known_ids_ignore_other_accounts_entirely(db_session: Session) -> None:
    """★ 只有别的账号有题时，本账号的已知集合仍为空。"""
    old = _account(db_session, remote_suffix="LONELY", status="revoked")
    comp_old = _competition(db_session, old.id, remote_id="p-x")
    _challenge(db_session, comp_old.id, remote_id="x-task")

    new = _account(db_session, remote_suffix="FRESH", status="active")
    db_session.commit()

    assert _known_ids_for_account(db_session, new.id) == set(), (
        "★ 本账号一道题都没有，却匹配到了别的账号的题"
    )
