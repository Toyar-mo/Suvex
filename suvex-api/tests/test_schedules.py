"""定时扫描计划（v0.85）。

## 这个功能的失败模式，按危害排序

1. **重复对目标发包** —— 推进 `next_run_at` 与建扫描的顺序搞反，
   一次建失败会让同一轮被反复认领。这是唯一"会造成外部后果"的一条。
2. **停机后补跑一串** —— 服务停 6 小时、计划每 15 分钟，恢复后
   连发 24 次。那不是"补齐"，那是对目标的一次突发流量。
3. **静默停用** —— 建扫描连续失败就自动停掉计划，用户以为在定期复查，
   实际早就不跑了。这比"一直在失败"危险得多（后者界面上能看见）。
4. **启用即开扫** —— 停了三个月再启用，`next_run_at` 还在三个月前，
   下一次 tick 立刻扫一次。用户点的是"启用"，不是"现在跑"。
5. **权限绕过** —— 写端点若只要 `read`，只读账号就能排一条
   每天扫十次的任务，而它自己一次扫描都发起不了。

每一组测试对应上面一条。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.timeutil import now_utc
from app.schedules import service
from app.schedules.models import MAX_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES, ScanSchedule
from tests.conftest import GOOD_USERNAME  # noqa: F401 - 让测试能按用户名断言留痕

API = "/api/v1"

#: 与 `settings.scan_authorization_statement` 的默认值一致。
#: 测试里直接写这句而不是 `get_settings().scan_authorization_statement`：
#: 前者更严格 —— 如果默认值被改了，这里会红，提醒我们去想
#: "契约里那句声明是不是也该改"。
STATEMENT = "我确认已获得对该目标进行安全测试的授权"


@pytest.fixture()
def statement(settings) -> str:
    """从真实配置取那句声明。

    ⚠️ 这里**必须**读配置而不是写死：`check_statement` 比对的是
    `settings.scan_authorization_statement`，测试若写死一个不同的字符串，
    每个用例都会 400，而失败原因看起来像"接口坏了"。
    """
    return settings.scan_authorization_statement


# ══════════════════════════════════════════════════════════
# 造数据
# ══════════════════════════════════════════════════════════


def _make(
    db: Session,
    *,
    name: str = "每日复查",
    target: str = "api.acme.internal",
    interval_minutes: int = 60,
    enabled: bool = True,
    next_run_at=None,
    skills: list[str] | None = None,
):
    """直接插一条计划（不走接口），用于纯 service 层的测试。

    `next_run_at` 显式传 None 与"不传"是不同的：前者在启用状态下会撞
    CHECK 约束（那正是约束要拦的），所以这里用默认值 `now + interval`。
    """
    moment = now_utc()
    if enabled:
        effective_next = (
            next_run_at if next_run_at is not None else moment + timedelta(minutes=interval_minutes)
        )
    else:
        # ★ 停用与"没有下次时间"必须同时成立 —— 数据库 CHECK 约束
        # `schedule_disabled_has_no_next_run` 会拦住别的组合。
        # 造数据时也遵守它，而不是绕过它：那条约束是本文件
        # `test_disable_clears_next_run` 要验的东西。
        effective_next = None
    schedule = ScanSchedule(
        id=f"sched_{name}",
        name=name,
        target=target,
        target_kind="url",
        goal="findVulns",
        interval_minutes=interval_minutes,
        enabled=enabled,
        next_run_at=effective_next,
        last_scan_id=None,
        last_run_at=None,
        failure_count=0,
        last_failure_reason=None,
        provider_id="prov_deepseek",
        provider_label="DeepSeek",
        skills=skills or ["web"],
        brief=None,
        authorized_by="user_test",
        authorization_statement="我有权对此目标进行安全测试",
        authorization_confirmed_at=moment,
        created_at=moment,
        updated_at=moment,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


# ══════════════════════════════════════════════════════════
# _advance：停机期间错过的轮次不补跑
# ══════════════════════════════════════════════════════════


def test_advance_returns_first_future_slot_without_drift() -> None:
    """落后 24 个周期时，推进结果是"第一个未来时刻"，不是 now+interval。

    ★ 这条是"不漂移"的核心断言。若实现成 `now + interval`，
    结果虽然也在未来，但它**不在基准的整数倍网格上** ——
    「每周一 3 点」的计划会永久漂到重启时刻。
    """
    interval = timedelta(minutes=15)
    now = now_utc().replace(second=0, microsecond=0)
    past = now - timedelta(hours=6)  # 落后 24 个周期

    result = service._advance(past, interval, moment=now)

    assert result > now, "推进后必须在未来，否则下一轮立刻又 due"
    assert (result - past) % interval == timedelta(0), (
        f"推进结果没落在基准的整数倍网格上：{result - past} 不是 {interval} 的整数倍"
    )
    # 只前进一个周期（不是 24 个）—— 错过的只跑一次。
    assert result - now <= interval


def test_advance_never_returns_a_past_moment() -> None:
    """落后 1 秒也要推到下一个周期 —— 否则同一轮里会被反复认领。"""
    interval = timedelta(minutes=15)
    now = now_utc().replace(second=0, microsecond=0)
    past = now - timedelta(seconds=1)

    result = service._advance(past, interval, moment=now)

    assert result > now
    assert result - now < interval


def test_advance_on_future_returns_unchanged() -> None:
    """还没到点时不动它。

    ★ 这条防的是死循环：负数喂给 `ceil` 会得 0 → `next == past` →
    下一轮立刻又 due → 再算一次 ……
    """
    interval = timedelta(minutes=15)
    now = now_utc()
    future = now + timedelta(hours=1)

    assert service._advance(future, interval, moment=now) == future


# ══════════════════════════════════════════════════════════
# claim_due：认领 + 推进（不建扫描）
# ══════════════════════════════════════════════════════════


def test_claim_due_advances_next_run_in_the_same_commit(db_session: Session) -> None:
    """认领之后，**库里**的 `next_run_at` 已经是未来时刻。

    ★ 这是"不重复发包"的落点：调用方拿到快照去建扫描，
    中间崩了也不会让下一轮重新认领同一条 —— 因为它已经不是 due 了。
    """
    moment = now_utc()
    schedule = _make(db_session, next_run_at=moment - timedelta(minutes=1))

    claimed = service.claim_due(db_session, now=moment)

    assert [s.id for s in claimed] == [schedule.id]
    db_session.expire_all()
    fresh = db_session.get(ScanSchedule, schedule.id)
    assert fresh is not None
    assert fresh.next_run_at is not None
    assert fresh.next_run_at > moment


def test_claim_due_ignores_disabled(db_session: Session) -> None:
    """停用的计划不会被认领（哪怕 `next_run_at` 在过去的垃圾数据里）。"""
    moment = now_utc()
    _make(db_session, enabled=False, next_run_at=None)

    assert service.claim_due(db_session, now=moment) == []


def test_claim_due_respects_limit(db_session: Session) -> None:
    """一次最多认领 `limit` 条 —— 防止所有计划同时到点把队列灌满。"""
    moment = now_utc()
    for i in range(5):
        _make(db_session, name=f"计划{i}", target=f"t{i}.example", next_run_at=moment - timedelta(minutes=1))

    claimed = service.claim_due(db_session, now=moment, limit=2)

    assert len(claimed) == 2


def test_claim_due_returns_a_snapshot_not_the_orm_object(db_session: Session) -> None:
    """返回的是快照，不是仍挂在 session 上的 ORM 对象。

    ★ 防的是我在实现时踩过的坑：`commit()` 之后对象 expired，
    调用方下一次读属性会**重新开一个事务** —— 而它接下来要做
    I/O 密集的建扫描，两个事务交叠是死锁温床。
    """
    moment = now_utc()
    _make(db_session, next_run_at=moment - timedelta(minutes=1))

    claimed = service.claim_due(db_session, now=moment)

    assert len(claimed) == 1
    # 读一个属性不会抛（若是 expired 且已 detach 的 ORM 对象，这里会抛）。
    assert claimed[0].target == "api.acme.internal"
    assert claimed[0].provider_id == "prov_deepseek"


# ══════════════════════════════════════════════════════════
# set_enabled：启用即开扫的那个坑
# ══════════════════════════════════════════════════════════


def test_enable_does_not_fire_immediately_after_long_pause(db_session: Session) -> None:
    """停了三个月再启用，`next_run_at` 必须在未来 —— **不能**立刻扫一次。

    用户点的是「启用」，不是「现在跑一次」。
    """
    moment = now_utc()
    # 造一条"停了很久"的计划：停用状态、没有下次时间（CHECK 约束要求），
    # 但有 `last_run_at` 在三个月前 —— 那正是真实世界里它的样子。
    schedule = _make(db_session, enabled=False)
    schedule.last_run_at = moment - timedelta(days=90)
    db_session.commit()

    service.set_enabled(db_session, schedule.id, True, now=moment)

    db_session.expire_all()
    fresh = db_session.get(ScanSchedule, schedule.id)
    assert fresh is not None
    assert fresh.enabled is True
    assert fresh.next_run_at is not None
    assert fresh.next_run_at > moment, (
        f"启用后 next_run_at 仍在过去（{fresh.next_run_at}），下一轮 tick 会立刻扫一次"
    )


def test_enabling_an_already_enabled_schedule_does_not_drift(db_session: Session) -> None:
    """对一条已经在跑的启用计划再点一次「启用」，下次时间不能被推远。

    ★ 这条防的是"界面重复提交把计划越推越远"：每点一次就 +1 个周期，
    点几次之后它就再也不跑了，而界面上看起来一切正常。
    """
    moment = now_utc()
    schedule = _make(db_session, interval_minutes=60, next_run_at=moment + timedelta(minutes=30))
    before = schedule.next_run_at

    service.set_enabled(db_session, schedule.id, True, now=moment)

    db_session.expire_all()
    fresh = db_session.get(ScanSchedule, schedule.id)
    assert fresh is not None
    assert fresh.next_run_at == before


def test_disable_clears_next_run(db_session: Session) -> None:
    """停用必须把 `next_run_at` 清空（数据库 CHECK 约束要求）。"""
    schedule = _make(db_session)

    service.set_enabled(db_session, schedule.id, False)

    db_session.expire_all()
    fresh = db_session.get(ScanSchedule, schedule.id)
    assert fresh is not None
    assert fresh.enabled is False
    assert fresh.next_run_at is None


# ══════════════════════════════════════════════════════════
# record_failure：不停用
# ══════════════════════════════════════════════════════════


def test_repeated_failures_never_disable_the_schedule(db_session: Session) -> None:
    """连续失败 20 次，计划仍然启用。

    ★ 静默停用一个用户以为在跑的计划是最坏的形态 ——
    它会让人相信"这块有人管"。
    """
    schedule = _make(db_session)

    for i in range(20):
        service.record_failure(db_session, schedule.id, f"第 {i} 次失败")

    db_session.expire_all()
    fresh = db_session.get(ScanSchedule, schedule.id)
    assert fresh is not None
    assert fresh.enabled is True
    assert fresh.failure_count == 20
    assert fresh.last_failure_reason == "第 19 次失败"


def test_failure_reason_is_truncated(db_session: Session) -> None:
    """失败原因截断到 2000 字 —— 它可能是整段 SQL 报错，而界面只显示一行。"""
    schedule = _make(db_session)

    service.record_failure(db_session, schedule.id, "x" * 5000)

    db_session.expire_all()
    fresh = db_session.get(ScanSchedule, schedule.id)
    assert fresh is not None
    assert fresh.last_failure_reason is not None
    assert len(fresh.last_failure_reason) == 2000


def test_record_dispatch_clears_failures(db_session: Session) -> None:
    """成功建出扫描后，失败计数归零、原因清空。"""
    schedule = _make(db_session)
    service.record_failure(db_session, schedule.id, "连不上 provider")
    service.record_dispatch(db_session, schedule.id, scan_id="scan_x")

    db_session.expire_all()
    fresh = db_session.get(ScanSchedule, schedule.id)
    assert fresh is not None
    assert fresh.failure_count == 0
    assert fresh.last_failure_reason is None
    assert fresh.last_scan_id == "scan_x"
    assert fresh.last_run_at is not None


# ══════════════════════════════════════════════════════════
# 间隔校验
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("minutes", [MIN_INTERVAL_MINUTES, 60, 1440, MAX_INTERVAL_MINUTES])
def test_valid_intervals_are_accepted(minutes: int) -> None:
    service.validate_interval(minutes)  # 不抛即通过


@pytest.mark.parametrize(
    "minutes",
    [MIN_INTERVAL_MINUTES - 1, 0, -5, MAX_INTERVAL_MINUTES + 1],
)
def test_out_of_range_intervals_are_rejected(minutes: int) -> None:
    with pytest.raises(service.ScheduleError):
        service.validate_interval(minutes)


def test_bool_is_not_an_acceptable_interval() -> None:
    """`True` 是 int 的子类 —— 不加这个判断，`intervalMinutes=true` 会变成 1 分钟。"""
    with pytest.raises(service.ScheduleError):
        service.validate_interval(True)


# ══════════════════════════════════════════════════════════
# 接口层
# ══════════════════════════════════════════════════════════


def _payload(statement: str, **overrides) -> dict:
    body = {
        "name": "每日复查",
        "target": "https://api.acme.internal/admin",
        "targetKind": "url",
        "goal": "findVulns",
        "intervalMinutes": 60,
        "providerId": "prov_default",
        "skills": ["web"],
        "brief": None,
        "authorizationStatement": statement,
    }
    body.update(overrides)
    return body


def test_create_schedule_stores_normalized_target(
    ready_client: TestClient, provider_id: str, statement: str
) -> None:
    """目标要**归一化后**入库（与 `POST /scans` 同一口径：剥协议、去路径）。

    ★ 计划的目标会在**未来很多次**扫描里复用 —— 这一次存错，
    错的就不止一次。
    """
    response = ready_client.post(f"{API}/schedules", json=_payload(statement, providerId=provider_id))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["target"] == "api.acme.internal"
    assert body["enabled"] is True
    assert body["intervalMinutes"] == 60
    assert body["nextRunAt"] is not None


def test_create_schedule_rejects_wrong_statement(ready_client: TestClient, provider_id: str) -> None:
    """授权声明措辞不对 → 400，且**不回显**用户输入。"""
    response = ready_client.post(f"{API}/schedules", json=_payload("我随便写了一句", providerId=provider_id))

    assert response.status_code == 400, response.text
    raw = response.text
    assert "我随便写了一句" not in raw, "错误响应回显了用户输入（契约 §1.3 禁止）"


def test_create_schedule_rejects_unknown_provider(ready_client: TestClient, statement: str) -> None:
    response = ready_client.post(
        f"{API}/schedules", json=_payload(statement, providerId="prov_does_not_exist")
    )

    assert response.status_code == 400, response.text


def test_create_schedule_rejects_bad_target(
    ready_client: TestClient, provider_id: str, statement: str
) -> None:
    response = ready_client.post(
        f"{API}/schedules", json=_payload(statement, providerId=provider_id, target="")
    )

    assert response.status_code == 400, response.text


def test_list_schedules_reports_due_count(ready_client: TestClient, db_session: Session) -> None:
    """`dueCount` 只数"启用且已到点"的那些。"""
    moment = now_utc()
    _make(db_session, name="到期", target="a.example", next_run_at=moment - timedelta(minutes=5))
    _make(db_session, name="未到期", target="b.example", next_run_at=moment + timedelta(hours=1))
    _make(db_session, name="停用", target="c.example", enabled=False)

    response = ready_client.get(f"{API}/schedules")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3
    assert body["dueCount"] == 1


def test_disabled_schedules_sort_last(ready_client: TestClient, db_session: Session) -> None:
    """停用的（`nextRunAt` 为 null）排在后面，不打断"接下来要跑什么"的顺序。"""
    moment = now_utc()
    _make(db_session, name="以后", target="b.example", next_run_at=moment + timedelta(hours=5))
    _make(db_session, name="马上", target="a.example", next_run_at=moment + timedelta(minutes=5))
    _make(db_session, name="停用", target="c.example", enabled=False)

    body = ready_client.get(f"{API}/schedules").json()

    assert [item["name"] for item in body["items"]] == ["马上", "以后", "停用"]


def test_update_only_accepts_name_and_interval(
    ready_client: TestClient, provider_id: str, statement: str
) -> None:
    """PATCH 传 `target` 会被**严格模式拒绝**（400）而不是静默忽略。

    ★ 静默忽略的症状是"我改了目标、界面也说成功了，但它还是扫老目标" ——
    那是本项目最不喜欢的一类 bug（不报错、只出错）。
    """
    created = ready_client.post(f"{API}/schedules", json=_payload(statement, providerId=provider_id))
    assert created.status_code == 201, created.text
    schedule_id = created.json()["id"]

    response = ready_client.patch(f"{API}/schedules/{schedule_id}", json={"target": "evil.example"})

    assert response.status_code == 400, response.text


def test_update_interval_recomputes_next_run(
    ready_client: TestClient, provider_id: str, statement: str
) -> None:
    created = ready_client.post(
        f"{API}/schedules", json=_payload(statement, providerId=provider_id, intervalMinutes=1440)
    )
    assert created.status_code == 201, created.text
    schedule_id = created.json()["id"]
    before = created.json()["nextRunAt"]

    updated = ready_client.patch(f"{API}/schedules/{schedule_id}", json={"intervalMinutes": 10})

    assert updated.status_code == 200, updated.text
    assert updated.json()["intervalMinutes"] == 10
    assert updated.json()["nextRunAt"] != before, (
        "改了间隔但下次时间没重算 —— 用户要等旧的 24 小时周期走完才生效"
    )


def test_toggle_roundtrip(ready_client: TestClient, provider_id: str, statement: str) -> None:
    created = ready_client.post(f"{API}/schedules", json=_payload(statement, providerId=provider_id))
    schedule_id = created.json()["id"]

    off = ready_client.post(f"{API}/schedules/{schedule_id}/enabled", json={"enabled": False})
    assert off.status_code == 200, off.text
    assert off.json()["enabled"] is False
    assert off.json()["nextRunAt"] is None

    on = ready_client.post(f"{API}/schedules/{schedule_id}/enabled", json={"enabled": True})
    assert on.status_code == 200, on.text
    assert on.json()["enabled"] is True
    assert on.json()["nextRunAt"] is not None


def test_delete_removes_only_the_schedule(
    ready_client: TestClient, provider_id: str, statement: str, db_session: Session
) -> None:
    """删计划不删它建出来的扫描。"""
    created = ready_client.post(f"{API}/schedules", json=_payload(statement, providerId=provider_id))
    schedule_id = created.json()["id"]

    deleted = ready_client.delete(f"{API}/schedules/{schedule_id}")

    assert deleted.status_code == 204, deleted.text
    assert db_session.get(ScanSchedule, schedule_id) is None


def test_viewer_cannot_create_schedule(viewer_client: TestClient, provider_id: str, statement: str) -> None:
    """只读账号不能排定时计划。

    ★ 这是权限模型中"最容易被漏掉的一条"：写端点看起来只是改配置，
    实际上是在授予"以后定期对目标发包"的能力 —— 门槛必须与
    `POST /scans` 一致（`require_scan`）。
    """
    response = viewer_client.post(f"{API}/schedules", json=_payload(statement, providerId=provider_id))

    assert response.status_code == 403, response.text


def test_viewer_can_read_schedules(viewer_client: TestClient) -> None:
    assert viewer_client.get(f"{API}/schedules").status_code == 200


def test_missing_schedule_gives_404(ready_client: TestClient) -> None:
    assert ready_client.delete(f"{API}/schedules/sched_nope").status_code == 404


def test_404_message_does_not_double_say_deleted(ready_client: TestClient) -> None:
    """404 文案不能出现「不存在……可能已被删除。……可能已被删除」这种重复。

    ★ 这条是我实现时踩到的：`ScheduleError` 的正文已经带了「可能已被删除」，
    而 `ApiError.not_found` 又追加一次。
    """
    response = ready_client.delete(f"{API}/schedules/sched_nope")

    assert response.status_code == 404
    assert response.text.count("可能已被删除") == 1, response.text


# ══════════════════════════════════════════════════════════
# `artifact` 不能成为定时计划的目标（v0.91）
# ══════════════════════════════════════════════════════════


def test_artifact_kind_is_rejected_with_an_actionable_message(
    ready_client: TestClient, provider_id: str, statement: str
) -> None:
    """「题目附件」不能排定时计划 —— 必须 400，且说清下一步该做什么。

    ## 这条测试固定的是「两道门都必须关掉」

    `artifact` 在定时计划里不是"暂时不支持"，而是**两种输入都立不起来**：

    * `target=""` → `min_length=1` 让它 422（根本进不到 handler）；
    * `target="tryre.zip"` → schema 放行，但 `targets.normalize_and_validate`
      对任何非空输入都返回「这类题目不需要目标地址」→ 400。

    于是旧实现了两种写法都失败，但**报错都指错了地方**：前者说
    "字段太短"，后者说"不需要目标地址"，没有任何一句话提到
    "定时计划存不下附件"。用户会以为是自己的写法问题。

    这里断言的是**非空输入这一条**（能走到 handler 的那条），
    因为它是真正被这次改动改掉的行为。空串那条由 schema 兜着，
    见下面的 `test_artifact_with_empty_target_still_fails_schema`。
    """
    response = ready_client.post(
        f"{API}/schedules",
        json=_payload(statement, providerId=provider_id, targetKind="artifact", target="tryre.zip"),
    )

    assert response.status_code == 400, response.text
    body = response.text
    # 报错必须指向**真正的原因**，而不是"目标不合法"
    assert "附件" in body, body
    assert "定时计划" in body, body


def test_artifact_with_empty_target_still_fails_schema(
    ready_client: TestClient, provider_id: str, statement: str
) -> None:
    """空 target 在 schema 层就被拒，永远到不了 handler 的 artifact 判断。

    ★ 这条存在的意义是防止有人"顺手把 min_length 放宽"来让 artifact 可用 ——
    那样虽然能建出一条计划，但派发时 `attachmentStagingId=None`，
    每一轮都会失败。放宽 schema 不是解法，改派发链路才是。

    ⚠️ 断言 **400 而不是 422**：本项目把 `RequestValidationError` 统一翻成
    400 + `VALIDATION_FAILED`（`app/core/errors.py`，契约 §1.3 的口径）。
    两种 artifact 写法的状态码相同，真正的区别在**文案**：这一条说
    "长度不足"（看起来像用户漏填），上面那条说"定时计划存不下附件"
    （说清了真正原因）。靠文案区分，不是靠状态码。
    """
    response = ready_client.post(
        f"{API}/schedules",
        json=_payload(statement, providerId=provider_id, targetKind="artifact", target=""),
    )

    assert response.status_code == 400, response.text
    # 这一条是"字段太短" → 说明 pydantic 先拦住了，没走到附件那条分支
    assert "长度不足" in response.text, response.text
    assert "附件" not in response.text, response.text


def test_other_kinds_are_still_accepted(ready_client: TestClient, provider_id: str, statement: str) -> None:
    """拒绝 artifact 不能误伤别的形态 —— 这是防守型改动最容易犯的错。

    ★ 每加一条"拒绝某某"的规则，就要有一条"别人还能用"的测试，
      否则规则写宽了（比如判成 `not in {url, repo}`）也没人发现。
    """
    for kind, target in [
        ("url", "https://api.acme.internal"),
        ("repo", "https://github.com/acme/backend.git"),
        ("path", "/srv/app"),
        ("service", "1.2.3.4:9999"),
    ]:
        response = ready_client.post(
            f"{API}/schedules",
            json=_payload(statement, providerId=provider_id, targetKind=kind, target=target),
        )
        assert response.status_code == 201, f"{kind} 被误伤了：{response.text}"
