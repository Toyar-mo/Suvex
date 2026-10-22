"""`ctf2.autosync` 的行为：间隔、关闭、抢锁、只碰新题、逐题容错。

## 这个模块为什么值得单独一组测试

它是**唯一一个不看界面就会自己动**的东西：自动去平台拉题、自动给新题
发优先级、还可能自动开解。它出错的形态全都是"安静"的：

* 线程静默死掉 → 新题再也不会自动出现（用户不会怀疑一个他没见过的线程）
* 间隔读成 0 之后不退让 → 循环变成忙等，CPU 打满
* 抢不到同步锁却排队 → 一次手点之后积压一串重复同步，白烧请求预算
* 自动开解在第一道题上抛异常 → 后面几道**全部**没开，而日志里只有一行

所以下面每一条都对着其中一种失败写。
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy.orm import Session

from app.kernel import autosync as autosync_module
from app.kernel import service as ctf2_service
from app.kernel.autosync import Ctf2AutoSync
from app.kernel.service import SyncResult
from tests.ctf2.conftest import _make_challenge


@pytest.fixture(autouse=True)
def _reset_singleton():
    """单例是全局状态，留着会让用例互相影响（与 test_scheduler 同一个做法）。"""
    autosync_module.reset_autosync()
    yield
    autosync_module.reset_autosync()


def _set_config(db: Session, **overrides) -> None:
    row = ctf2_service.get_ctf2_config(db)
    for key, value in overrides.items():
        setattr(row, key, value)
    db.commit()


# ══════════════════════════════════════════════════════════
# 间隔
# ══════════════════════════════════════════════════════════


def test_interval_zero_means_disabled(db_session: Session) -> None:
    """★ 0 = 关闭，而且它必须是**合法值**。

    把 0 混进"非法值"里会逼用户去填一个最小值，而"我不想让它自动同步"
    是一个正当诉求（PAT 有配额、比赛结束了想让系统安静下来）。
    """
    _set_config(db_session, auto_sync_seconds=0)
    assert Ctf2AutoSync()._interval() == 0


def test_interval_comes_from_its_own_setting(db_session: Session) -> None:
    """间隔读的是 CTF2 自己的设置，不是扫描那个旋钮。"""
    _set_config(db_session, auto_sync_seconds=120)
    assert Ctf2AutoSync()._interval() == 120


def test_interval_is_clamped_into_range(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """越界值被夹回合法区间（**读侧**兜底）。

    ⚠️ 这里不写库：`auto_sync_seconds` 有 CHECK 约束
    （`auto_sync_seconds_within_range`），写 99999 会被数据库直接拒绝。
    而这条要验的恰恰是**读侧**的兜底 —— 万一约束将来被放开（或者换了
    存储），读侧仍然不该把一个 99999 秒的间隔当成合法值：那意味着用户
    改了设置之后，要等 27 小时才知道它有没有生效。
    """
    from dataclasses import replace

    fake = replace(ctf2_service.effective_ctf2_settings(db_session), auto_sync_seconds=99999)
    monkeypatch.setattr(ctf2_service, "effective_ctf2_settings", lambda db: fake)
    assert Ctf2AutoSync()._interval() <= 3600


# ══════════════════════════════════════════════════════════
# 没有账号：什么都不做，而且**不是错误**
# ══════════════════════════════════════════════════════════


def test_without_an_account_it_quietly_does_nothing(db_session: Session) -> None:
    """没绑账号是**正常状态**（先启动服务、后绑 PAT），不该记成错误。

    记成错误会让界面上一片红，而用户此刻唯一该做的事就是去绑 PAT ——
    一个"你还没绑账号"的红字并不能告诉他这一点。
    """
    sync = Ctf2AutoSync()
    assert sync.run_once() == 0
    assert sync.stats()["lastError"] is None


# ══════════════════════════════════════════════════════════
# 抢锁：手动同步在跑就跳过
# ══════════════════════════════════════════════════════════


def test_it_skips_while_a_manual_sync_is_running(db_session: Session, ctf2_account) -> None:
    """★ 手动同步占着锁时**跳过这一轮**，而不是排队。

    排队会让"用户手点一次同步"之后积压一串自动同步 —— 它们做的是
    完全一样的事（把平台读一遍），而比赛里的请求预算和解题共享同一个
    账号，重复打只会推高撞 429 的概率。

    这里用**真的 RLock**（而不是 monkeypatch `try_sync`）：`try_sync`
    的非阻塞语义、以及"RLock 可重入"这条前提，都要被真实地走一遍。
    """
    holder_ready = threading.Event()
    release = threading.Event()

    def hold_the_lock() -> None:
        with ctf2_service._SYNC_LOCK:
            holder_ready.set()
            release.wait(10)

    holder = threading.Thread(target=hold_the_lock, daemon=True)
    holder.start()
    assert holder_ready.wait(10), "占锁线程没起来"
    try:
        assert Ctf2AutoSync().run_once() == 0
    finally:
        release.set()
        holder.join(10)


# ══════════════════════════════════════════════════════════
# 新题：只碰这一批，绝不重排全表
# ══════════════════════════════════════════════════════════


def test_new_challenges_get_auto_priority_and_old_ones_are_untouched(
    db_session: Session, ctf2_account, ctf2_competition, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★★ 新题拿到自动优先级，**既有题一个字节都不动**。

    这是整个自动作业里最容易做错的一处。如果为了"让新题排最前"去调
    `set_priorities_by_score` 重排全库，用户的人工优先级会被整片覆盖 ——
    那种"我明明调过"的丢失比不自动更糟。

    所以这里同时断言两件事：新题**拿到了** 1000+ 的值，
    而用户手工设过的那道题**保持原值**。
    """
    from app.core.vocab import CTF2_AUTO_PRIORITY_BASE

    manual = _make_challenge(db_session, ctf2_competition, remote_id="manual", score=10)
    manual.priority = 7  # 用户手工设的（约定 1..999）
    low = _make_challenge(db_session, ctf2_competition, remote_id="new-low", score=50)
    high = _make_challenge(db_session, ctf2_competition, remote_id="new-high", score=500)
    db_session.commit()

    result = SyncResult()
    result.new_challenge_ids = [low.id, high.id]
    monkeypatch.setattr(ctf2_service, "try_sync", lambda db, **kw: result)

    assert Ctf2AutoSync().run_once() == 2

    db_session.expire_all()
    assert db_session.get(type(manual), manual.id).priority == 7, "人工优先级被覆盖了"
    assert db_session.get(type(low), low.id).priority == CTF2_AUTO_PRIORITY_BASE + 50
    assert db_session.get(type(high), high.id).priority == CTF2_AUTO_PRIORITY_BASE + 500


def test_priority_uses_the_score_not_the_batch_position(
    db_session: Session, ctf2_competition
) -> None:
    """★ 排序键**只由题目自己的分值决定**，与它在批次里的位置无关。

    如果按"批次内排名"赋值（第 1 名 1000+50、第 2 名 1000+49），先到的
    低分题会占住高位，30 秒后新来的高分题反而排在它们后面 —— 而抢首杀
    要的恰恰是"新出现的高分题插到最前面"。

    这条测试**故意把低分题放在前面**：批次位置与分值顺序相反，
    只有"分值决定一切"的实现才能通过。
    """
    from app.core.vocab import CTF2_AUTO_PRIORITY_BASE

    cheap = _make_challenge(db_session, ctf2_competition, remote_id="cheap", score=10)
    pricey = _make_challenge(db_session, ctf2_competition, remote_id="pricey", score=900)
    db_session.commit()

    assert ctf2_service.apply_new_challenge_priority(db_session, [cheap.id, pricey.id]) == 2

    db_session.expire_all()
    assert db_session.get(type(pricey), pricey.id).priority > db_session.get(
        type(cheap), cheap.id
    ).priority
    assert db_session.get(type(cheap), cheap.id).priority == CTF2_AUTO_PRIORITY_BASE + 10


def test_null_score_still_counts_as_a_new_challenge(
    db_session: Session, ctf2_competition
) -> None:
    """分值为 NULL 的题**仍然要**拿到加成（按 0 算）。

    练习集常常不给分值。如果 NULL 直接导致跳过，那些题会掉回"和从没调过
    的题一样"的 0 —— 新题加成白给，而抢首杀恰恰最怕"新题没排在前面"。
    """
    from app.core.vocab import CTF2_AUTO_PRIORITY_BASE

    unscored = _make_challenge(
        db_session, ctf2_competition, remote_id="unscored", score=None
    )
    db_session.commit()

    assert ctf2_service.apply_new_challenge_priority(db_session, [unscored.id]) == 1
    db_session.expire_all()
    assert db_session.get(type(unscored), unscored.id).priority == CTF2_AUTO_PRIORITY_BASE


def test_apply_priority_with_no_ids_is_a_noop(db_session: Session) -> None:
    """空批次不写库、不报错（自动同步绝大多数轮次都是这个分支）。"""
    assert ctf2_service.apply_new_challenge_priority(db_session, []) == 0


# ══════════════════════════════════════════════════════════
# 自动开解：逐题容错
# ══════════════════════════════════════════════════════════


def test_auto_start_survives_a_failure_on_one_challenge(
    db_session: Session, ctf2_account, ctf2_competition, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 一道题开解失败，不该让后面几道**全部**没开。

    "批量操作做到一半停住"是这类代码最常见的失败形态，而且它失败得
    很安静：日志里只有一行异常，界面上什么都没发生，用户以为自动开解
    没生效。所以这里让第一道题抛异常，断言第二道**仍然**被开了。
    """
    first = _make_challenge(db_session, ctf2_competition, remote_id="first")
    second = _make_challenge(db_session, ctf2_competition, remote_id="second")
    db_session.commit()
    _set_config(db_session, auto_start_new=True, auto_priority_new=False)

    result = SyncResult()
    result.new_challenge_ids = [first.id, second.id]
    monkeypatch.setattr(ctf2_service, "try_sync", lambda db, **kw: result)

    started: list[str] = []

    def fake_start_solve(db: Session, challenge, **kwargs):  # noqa: ANN001, ANN003
        if challenge.id == first.id:
            raise RuntimeError("没有可用的 LLM provider")
        started.append(challenge.id)
        return challenge

    monkeypatch.setattr(ctf2_service, "start_solve", fake_start_solve)

    assert Ctf2AutoSync().run_once() == 2
    assert started == [second.id], "一道题失败影响了另一道"


def test_auto_start_is_off_by_default(
    db_session: Session, ctf2_account, ctf2_competition, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 默认**不**自动开解（判据是可逆性，见 `Ctf2Config` 的说明）。

    它默认关是因为它会真的消耗模型额度和解题并发名额 —— 在一场"题量
    明显超过人工能力"的比赛里，它可能把 3 个名额全占在几道根本解不出
    的题上。所以"默认值站在错不起的那一侧"这条要能被测出来。
    """
    challenge = _make_challenge(db_session, ctf2_competition, remote_id="only")
    db_session.commit()

    settings = ctf2_service.effective_ctf2_settings(db_session)
    assert settings.auto_start_new is False
    assert settings.auto_priority_new is True

    result = SyncResult()
    result.new_challenge_ids = [challenge.id]
    monkeypatch.setattr(ctf2_service, "try_sync", lambda db, **kw: result)

    started: list[str] = []
    monkeypatch.setattr(
        ctf2_service,
        "start_solve",
        lambda db, challenge, **kw: started.append(challenge.id),
    )

    Ctf2AutoSync().run_once()
    assert started == [], "默认配置下不该自动开解"


# ══════════════════════════════════════════════════════════
# 状态
# ══════════════════════════════════════════════════════════


def test_stats_reports_what_the_last_round_did(
    db_session: Session, ctf2_account, ctf2_competition, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`stats()` 是"查看运行状态"那条需求的落点（比赛规则里明确列了它）。"""
    challenge = _make_challenge(db_session, ctf2_competition, remote_id="s1")
    db_session.commit()

    result = SyncResult()
    result.new_challenge_ids = [challenge.id]
    monkeypatch.setattr(ctf2_service, "try_sync", lambda db, **kw: result)

    sync = Ctf2AutoSync()
    sync.run_once()
    stats = sync.stats()
    assert stats["lastNewChallenges"] == 1
    assert stats["lastRunAt"] is not None
    assert stats["lastError"] is None
    # 线程没 start 过，所以 running 是 False —— 这条顺带说明 stats 不撒谎。
    assert stats["running"] is False
