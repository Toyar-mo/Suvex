"""CTF2 环境到期：`expired` 这个状态必须真的有人写。

## 这组用例守的是什么

`KernelEnvironmentStatus.EXPIRED` 从定义那天起**没有一处设置它** —— 全仓
`app/` 零处。于是 `expires_at` 过去之后：

- 行还停在 `ready`；
- `access_url` 也还留着；
- 前端 `EnvironmentStatus.tsx` 的规则是"`accessUrl` 只在 `status === 'ready'`
  时渲染" → 界面上出现一个标着「就绪」的链接，点进去是连接断开。

用户看到的不是一个"过期的环境"，而是一个**坏掉的链接**，而来问的是
"你们的链接怎么打不开"。真库里实测有 2 条 `status='ready'` 且
`expires_at` 已过（其中一条已过 15 天），另有 2 条 `expires_at` 为 NULL
的 ready 行 —— 后者永远不会满足"到期"判据，所以还需要一条"明显陈旧"的
判据，见 `STALE_ENVIRONMENT_WITHOUT_TTL_HOURS`。

## 边界（说清楚它证不了什么）

能证：库里的过期行会转成 `expired`、死地址被清掉、题目的状态不受影响。
**不能证**平台真的会遵守它给的 `expires_at` —— 我们只是按自己的记录判断。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.core.vocab import KernelChallengeStatus, KernelEnvironmentStatus
from app.kernel.models import KernelChallenge, KernelEnvironment
from app.kernel.service import STALE_ENVIRONMENT_WITHOUT_TTL_HOURS, expire_stale_environments


def _make_environment(
    db: Session,
    challenge_id: str,
    *,
    status: str = KernelEnvironmentStatus.READY.value,
    expires_at=None,
    ready_at=None,
) -> KernelEnvironment:
    """造一条环境行。

    ⚠️ `status='ready'` 时 `access_url` 必须非空 —— 约束
    `access_url_only_when_ready` 管的是反向（非 ready 不许有 url）。
    这里按真实形状给：ready 带地址，其余不带。
    """
    ready = status == KernelEnvironmentStatus.READY.value
    environment = KernelEnvironment(
        id=new_id("ctf2env"),
        challenge_id=challenge_id,
        status=status,
        sandbox_profile="ctf2-sandbox",
        access_url="http://1.2.3.4:8080" if ready else None,
        access_urls=["http://1.2.3.4:8080", "1.2.3.4:9999"] if ready else None,
        poll_count=3,
        started_at=now_utc(),
        ready_at=ready_at if ready_at is not None else (now_utc() if ready else None),
        expires_at=expires_at,
    )
    db.add(environment)
    db.commit()
    return environment


def test_an_expired_ready_environment_becomes_expired(db_session: Session, ctf2_challenge) -> None:
    """判据 ①：`expires_at` 已过 → 转 expired，死地址清掉。

    ★ 同时断言 `access_urls` 也清了。列表接口读的是复数那一列，
      只清单数会让"全部入口"里还留着死地址 —— 那是
      `release_environment` 的历史遗留，这里两处都清。
    """
    expired = _make_environment(
        db_session, ctf2_challenge.id, expires_at=now_utc() - timedelta(minutes=1)
    )

    assert expire_stale_environments(db_session) == 1

    db_session.expire_all()
    row = db_session.get(KernelEnvironment, expired.id)
    assert row is not None
    assert row.status == KernelEnvironmentStatus.EXPIRED.value
    assert row.access_url is None
    assert row.access_urls is None


def test_a_ready_environment_with_a_future_expiry_is_left_alone(
    db_session: Session, ctf2_challenge
) -> None:
    """租期还没到 → 一个字都不动。

    这条守的是"别把还活着的环境杀掉"：误杀比不杀贵得多 —— 用户正在用的
    环境突然变成 expired，而他没有任何办法知道为什么。
    """
    alive = _make_environment(
        db_session, ctf2_challenge.id, expires_at=now_utc() + timedelta(minutes=30)
    )

    assert expire_stale_environments(db_session) == 0

    db_session.expire_all()
    row = db_session.get(KernelEnvironment, alive.id)
    assert row is not None
    assert row.status == KernelEnvironmentStatus.READY.value
    assert row.access_url == "http://1.2.3.4:8080"


def test_a_ready_environment_without_a_ttl_becomes_expired_when_stale(
    db_session: Session, ctf2_challenge
) -> None:
    """判据 ②：没有租期信息、`ready_at` 早于阈值 → 也算过期。

    ★ 这条判据存在的理由：`expires_at` 可空（写回租期是后来才补的），
      那批早先的环境**永远不会**满足判据 ①。真库里这种行有 2 条，
      `container_id` 全 NULL、`expires_at` 全 NULL —— 它们会永远停在
      ready，永远渲染一个死链接。

    ⚠️ 这里的时间是**字面量** 25 小时，不是 `STALE_ENVIRONMENT_WITHOUT_TTL_HOURS + 1`。
       用常量推导边界等于让用例跟着常量一起漂：把阈值改成 1 小时，
       "25 小时前"仍然满足，用例照样绿（反向验证实测过这一版，是绿的）。
       字面量 + 下面那条 23 小时的用例，一起把阈值**夹在** 23~25 小时之间。
    """
    old = _make_environment(
        db_session,
        ctf2_challenge.id,
        expires_at=None,
        ready_at=now_utc() - timedelta(hours=25),
    )
    assert expire_stale_environments(db_session) == 1

    db_session.expire_all()
    row = db_session.get(KernelEnvironment, old.id)
    assert row is not None
    assert row.status == KernelEnvironmentStatus.EXPIRED.value
    assert row.access_url is None


def test_a_fresh_environment_without_a_ttl_is_left_alone(
    db_session: Session, ctf2_challenge
) -> None:
    """刚就绪、只是没有租期信息 → 不动。

    ★ 这条是上面那条的**反向**，也是 24 小时阈值的守门人：阈值取得太小
      （比如 1 小时）会把一个正常环境误判成陈旧，而那正好是默认租期的
      长度 —— 于是每个环境刚起好就被判死，功能直接不可用。

    ⚠️ 23 小时是**字面量**，与上面那条的 25 小时一起把阈值夹在中间：
      任何 <23h 或 >25h 的阈值改动都会让这两条里有一条变红。
    """
    fresh = _make_environment(
        db_session,
        ctf2_challenge.id,
        expires_at=None,
        ready_at=now_utc() - timedelta(hours=23),
    )

    assert expire_stale_environments(db_session) == 0

    db_session.expire_all()
    row = db_session.get(KernelEnvironment, fresh.id)
    assert row is not None
    assert row.status == KernelEnvironmentStatus.READY.value


def test_a_stopped_environment_is_not_touched(db_session: Session, ctf2_challenge) -> None:
    """已经停掉的行即使 `expires_at` 早就过了，也不该改成 expired。

    那是**停止**，不是**到期** —— 两者在界面上给的补救动作不同
    （stop 是"你自己关的"，expired 是"重新启动环境"）。把 stopped 改成
    expired 等于抹掉"用户主动关过"这个事实。
    """
    stopped = _make_environment(
        db_session,
        ctf2_challenge.id,
        status=KernelEnvironmentStatus.STOPPED.value,
        expires_at=now_utc() - timedelta(days=3),
        ready_at=now_utc() - timedelta(days=3),
    )

    assert expire_stale_environments(db_session) == 0

    db_session.expire_all()
    row = db_session.get(KernelEnvironment, stopped.id)
    assert row is not None
    assert row.status == KernelEnvironmentStatus.STOPPED.value


def test_expiring_an_environment_does_not_touch_the_challenge(
    db_session: Session, ctf2_challenge
) -> None:
    """★ 环境到期**不改题目状态**。

    这道题解没解出、是不是还在排队，与环境到期是两件事。真库里卡在
    `environment_ready`/`solving` 的题是 0 条，没有"顺手修一下"的理由
    —— 而顺手改一个状态机字段是没有测试守得住的改动。
    """
    _make_environment(
        db_session, ctf2_challenge.id, expires_at=now_utc() - timedelta(minutes=1)
    )
    before = db_session.get(KernelChallenge, ctf2_challenge.id)
    assert before is not None
    status_before = before.status
    error_before = before.last_error

    assert expire_stale_environments(db_session) == 1

    db_session.expire_all()
    after = db_session.get(KernelChallenge, ctf2_challenge.id)
    assert after is not None
    assert after.status == status_before == KernelChallengeStatus.UNSOLVED.value
    assert after.last_error == error_before


def test_expiring_is_idempotent(db_session: Session, ctf2_challenge) -> None:
    """第二遍什么都不做 —— 判据只看 ready，改过的行不再命中。"""
    _make_environment(
        db_session, ctf2_challenge.id, expires_at=now_utc() - timedelta(minutes=1)
    )

    assert expire_stale_environments(db_session) == 1
    assert expire_stale_environments(db_session) == 0


def test_the_retention_sweep_reports_the_expired_count(
    db_session: Session, ctf2_challenge
) -> None:
    """★ 接进 `sweep_retention`，且返回值带上计数。

    没有这个字段，运维在启动日志里看不到它 —— 而"清理有没有生效"
    正是这类后台任务最该能回答的问题（与 `ctf2WorkspacesSwept`
    同一个理由）。
    """
    from app.scans import service as scans_service

    _make_environment(
        db_session, ctf2_challenge.id, expires_at=now_utc() - timedelta(minutes=1)
    )

    result = scans_service.sweep_retention(db_session)

    assert "ctf2EnvironmentsExpired" in result
    assert result["ctf2EnvironmentsExpired"] == 1


def test_the_stale_threshold_stays_at_a_day() -> None:
    """阈值本身钉死在 24 小时。

    ★ 为什么这条存在：上面两条用例把边界夹在 23~25 小时之间，但它们只
      证明"阈值落在这个区间里"，没证明它**为什么**是 24。默认租期是
      1 小时（`start_environment_for` 里写回的那个 `expires_at`），24
      是 24 倍余量 —— 这个数变了要有人解释，而不是悄悄漂。
    """
    assert STALE_ENVIRONMENT_WITHOUT_TTL_HOURS == 24


@pytest.mark.parametrize("status", ["starting", "error", "stopped"])
def test_only_ready_rows_are_ever_expired(
    db_session: Session, ctf2_challenge, status: str
) -> None:
    """非 ready 的行一律不碰，即使 `expires_at` 已过且 `ready_at` 很旧。

    判据里 `status == ready` 是**第一道**过滤，不是可有可无的前置。
    """
    _make_environment(
        db_session,
        ctf2_challenge.id,
        status=status,
        expires_at=now_utc() - timedelta(days=10),
        ready_at=now_utc() - timedelta(days=10),
    )

    assert expire_stale_environments(db_session) == 0
