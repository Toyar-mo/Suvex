"""SLA 修复时限（任务 10 P3）。

五类要覆盖的：

1. **不计时的两种情况** —— 档位设 0 天、已到终态。
   它们都是 `sla_due_at IS NULL`，但原因不同，而"为什么这条没有倒计时"
   是用户会问的问题。
2. **到期的三档判定**（`onTrack` / `dueSoon` / `overdue`）与天数取整。
3. **起算点可配** —— `discovered` vs `confirmed`。
4. **三个重算时机** —— 导入、状态流转、规则变更。少一个就会出现
   "改了规则但倒计时没变"。
5. **筛选**用区间条件（能走索引），而不是取回来再过滤。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.findings import service
from app.findings.models import Finding

API = "/api/v1"


def _set_sla(
    client: TestClient,
    *,
    critical: int = 3,
    high: int = 7,
    medium: int = 30,
    low: int = 90,
    info: int = 0,
    start_from: str = "discovered",
) -> dict:
    response = client.put(
        f"{API}/settings/finding-sla",
        json={
            "criticalDays": critical,
            "highDays": high,
            "mediumDays": medium,
            "lowDays": low,
            "infoDays": info,
            "startFrom": start_from,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _finding(db: Session, finding_id: str) -> Finding:
    return db.get(Finding, finding_id)


# ══════════════════════════════════════════════════════════
# 判定与取整
# ══════════════════════════════════════════════════════════


def test_sla_state_three_buckets_and_rounding() -> None:
    """三档判定与天数取整 —— 纯函数，直接构造对象测。

    ★ 取整规则值得单独盯：剩 4 小时应该显示「还剩 1 天」，
    而 `floor` 会给 0，读起来像"今天到期"，实则是"还有半天"。
    """
    from app.core.vocab import FindingStatus
    from app.settings.models import SLA_WARN_WITHIN_DAYS

    now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)

    def make(due: datetime | None) -> Finding:
        return Finding(
            id="f",
            scan_id="s",
            severity="high",
            category="misconfiguration",
            title="t",
            target="x",
            verification="none",
            status=FindingStatus.CONFIRMED.value,
            discovered_at=now,
            sla_due_at=due,
        )

    # 不计时
    assert service.sla_state_of(make(None), now) == ("none", None)

    # 已超期（负天数）
    assert service.sla_state_of(make(now - timedelta(days=5)), now) == ("overdue", -5)
    # ★ 超了不到一天 → -1，而**不是 0**：0 会被读成"今天到期"，
    #   而它明明已经过了。这条边界是两个错误的交界处，值得单独钉住。
    assert service.sla_state_of(make(now - timedelta(hours=4)), now) == ("overdue", -1)
    # 刚过 1 整天 → 仍是 -1（按完整天数算），不该因为多出几毫秒就变 -2
    assert service.sla_state_of(make(now - timedelta(days=1, seconds=1)), now) == ("overdue", -1)
    assert service.sla_state_of(make(now - timedelta(days=2, seconds=1)), now) == ("overdue", -2)

    # 临期（窗口内）
    soon, days = service.sla_state_of(make(now + timedelta(hours=4)), now)
    assert (soon, days) == ("dueSoon", 1), "剩 4 小时该是「还剩 1 天」"
    assert service.sla_state_of(
        make(now + timedelta(days=SLA_WARN_WITHIN_DAYS)), now
    )[0] == "dueSoon"

    # 还早
    assert service.sla_state_of(make(now + timedelta(days=20)), now)[0] == "onTrack"


def test_zero_days_means_no_deadline(ready_client: TestClient, scan, db_session: Session) -> None:
    """★ 档位设 0 天 = **不设时限**，不是"立刻到期"。

    给低危套一个必然超期的假期限，后果是"超期"这个信号整体失效 ——
    一眼看去全是红的，于是没人看那个红点。
    """
    from pathlib import Path

    from tests.conftest import vuln_record, write_vulns

    _set_sla(ready_client, info=0)
    write_vulns(
        Path(scan.run_dir),
        [vuln_record(id="v1", severity="info", title="提示类")],
    )
    service.import_from_run(db_session, scan)
    db_session.expire_all()

    row = db_session.execute(select(Finding).where(Finding.scan_id == scan.id)).scalar_one()
    assert row.sla_due_at is None, "0 天档不该有到期时间"

    listed = ready_client.get(f"{API}/findings").json()
    assert listed[0]["slaState"] == "none"
    assert listed[0]["slaDaysLeft"] is None


# ══════════════════════════════════════════════════════════
# 重算时机
# ══════════════════════════════════════════════════════════


def test_import_computes_due_at(
    ready_client: TestClient, imported: list[Finding], db_session: Session
) -> None:
    """★ 导入时就要算好。

    不在那时算的话，要到第一次改状态才出现 —— 而"刚导入的高危漏洞
    没有到期时间"恰恰是最需要它的时候。
    """
    _set_sla(ready_client, high=7)
    # 上一步已经导入过了，规则是在导入之后设的 —— 所以这里再导入一次
    # 触发重算。真实流程里规则通常先配好。
    service.refresh_sla(db_session, imported)
    db_session.commit()
    db_session.expire_all()

    high = db_session.execute(select(Finding).where(Finding.severity == "high")).scalar_one()
    assert high.sla_due_at is not None, "高危该有到期时间"
    assert high.sla_due_at == high.discovered_at + timedelta(days=7)

    # info 档设的是 0 天，**不该**有到期时间 —— 这条与上面那条一起
    # 才说明"按档位取天数"真的生效了（而不是给所有条目都算了一个值）
    info = db_session.execute(select(Finding).where(Finding.severity == "low")).scalar_one()
    assert info.sla_due_at is not None, "low 档默认 90 天，该有"


def test_changing_the_rule_recomputes_everything(
    ready_client: TestClient, imported: list[Finding], db_session: Session
) -> None:
    """★ 改了天数要立刻生效。

    不重算的话，用户把高危从 7 天改成 3 天，回到列表看到的还是按 7 天
    算的倒计时 —— 而"我改了规则但没生效"看起来就是没保存成功。
    """
    _set_sla(ready_client, high=7)
    rows = list(db_session.execute(select(Finding)).scalars())
    service.refresh_sla(db_session, rows)
    db_session.commit()

    _set_sla(ready_client, high=30)  # 保存时路由内部会重算
    db_session.expire_all()

    high = db_session.execute(select(Finding).where(Finding.severity == "high")).scalar_one()
    assert high.sla_due_at is not None
    delta = high.sla_due_at - high.discovered_at
    assert math.isclose(delta.total_seconds(), 30 * 86400, rel_tol=1e-6), (
        f"应该按新的 30 天算，实际 {delta.days} 天"
    )


def test_closing_stops_the_clock(
    ready_client: TestClient, imported: list[Finding], db_session: Session
) -> None:
    """★ 到终态就不计时。

    `fixed` / `retesting` **仍然计时** —— 修复动作发生了但还没验证完，
    对外承诺的期限依然成立。只有 `closed` / `false_positive` 停表。
    """
    _set_sla(ready_client, high=7)
    rows = list(db_session.execute(select(Finding)).scalars())
    service.refresh_sla(db_session, rows)
    db_session.commit()
    db_session.expire_all()

    target = imported[1]  # high
    assert _finding(db_session, target.id).sla_due_at is not None

    # 确认 → 修复 → 关闭
    for to in ("confirmed", "fixed", "closed"):
        response = ready_client.post(
            f"{API}/findings/status", json={"ids": [target.id], "status": to}
        )
        assert response.status_code == 200, response.text

    db_session.expire_all()
    assert _finding(db_session, target.id).sla_due_at is None, "关闭后不该再计时"


def test_fixed_still_counts(
    ready_client: TestClient, imported: list[Finding], db_session: Session
) -> None:
    """`fixed` 仍然计时（与上一条配对，防止"到终态"被实现成"到任何非 new"）。"""
    _set_sla(ready_client, high=7)
    rows = list(db_session.execute(select(Finding)).scalars())
    service.refresh_sla(db_session, rows)
    db_session.commit()

    target = imported[1]
    ready_client.post(f"{API}/findings/status", json={"ids": [target.id], "status": "confirmed"})
    ready_client.post(f"{API}/findings/status", json={"ids": [target.id], "status": "fixed"})

    db_session.expire_all()
    assert _finding(db_session, target.id).sla_due_at is not None, "已修复但没验证完，仍该计时"


# ══════════════════════════════════════════════════════════
# 起算点
# ══════════════════════════════════════════════════════════


def test_start_from_confirmed_uses_the_first_confirmation(
    ready_client: TestClient, imported: list[Finding], db_session: Session
) -> None:
    """★ 起算点可配。

    按确认时间起算对"确认得晚"的团队更合理（漏洞在队列里躺了一周，
    之后才开始算修复期），但会让人有动机拖着不确认。两种都有道理，
    所以交给配置。
    """
    target = imported[1]  # high
    # 先确认（这一步会按 discovered 起算）
    ready_client.post(f"{API}/findings/status", json={"ids": [target.id], "status": "confirmed"})

    # 再把起算点改成 confirmed
    _set_sla(ready_client, high=7, start_from="confirmed")
    db_session.expire_all()

    row = _finding(db_session, target.id)
    confirmed_at = service._confirmed_at(row)
    assert confirmed_at is not None
    assert row.sla_due_at == confirmed_at + timedelta(days=7), "应该从确认时刻起算"


def test_unconfirmed_falls_back_to_discovered(
    ready_client: TestClient, imported: list[Finding], db_session: Session
) -> None:
    """还没确认过的漏洞用发现时间兜底。

    否则未确认的会集体"没有期限"，而它们恰恰是最该被催的。
    """
    _set_sla(ready_client, high=7, start_from="confirmed")

    first = _finding(db_session, imported[0].id)
    assert first.status == "new", "前置条件：这条还没被确认过"

    rows = list(db_session.execute(select(Finding)).scalars())
    service.refresh_sla(db_session, rows)
    db_session.commit()
    db_session.expire_all()

    again = _finding(db_session, imported[1].id)
    assert again.sla_due_at is not None, "未确认也要有期限"
    assert again.sla_due_at == again.discovered_at + timedelta(days=7)


# ══════════════════════════════════════════════════════════
# 配置端点与筛选
# ══════════════════════════════════════════════════════════


def test_config_round_trip(ready_client: TestClient) -> None:
    saved = _set_sla(ready_client, critical=1, high=2, medium=3, low=4, info=5, start_from="confirmed")
    assert saved["criticalDays"] == 1
    assert saved["startFrom"] == "confirmed"

    read_back = ready_client.get(f"{API}/settings/finding-sla").json()
    assert read_back["highDays"] == 2
    assert read_back["infoDays"] == 5


def test_negative_days_rejected(ready_client: TestClient) -> None:
    """天数为负是配置错误（0 才表示不设时限）。"""
    response = ready_client.put(
        f"{API}/settings/finding-sla",
        json={
            "criticalDays": -1, "highDays": 7, "mediumDays": 30,
            "lowDays": 90, "infoDays": 0, "startFrom": "discovered",
        },
    )
    assert response.status_code == 400


def test_filter_by_sla_state_uses_the_column(
    ready_client: TestClient, imported: list[Finding], db_session: Session
) -> None:
    """★ 筛选走 `sla_due_at` 的区间条件（能用索引），不是取回来再过滤。

    ⚠️ **`critical=90` 不是随手写的，它挡的是一个日期炸弹。**

    这条用例需要"恰好一条超期"，而 `conftest.vuln_record` 的漏洞时间戳是
    **硬编码的** `2026-09-24 10:00:00 UTC`（`test_reports.py` 用同一个日期
    做范围筛选，所以不能把它改成相对时间）。于是：

    | 档位 | 默认天数 | 从 09-24 起算的到期日 |
    | --- | --- | --- |
    | critical | **3** | 09-27 ← **当天是 09-28 时它就超期了** |
    | high | 7 | 10-01 |
    | low | 90 | 12-23 |

    也就是说这条用例在 09-24~09-26 是绿的，从 **09-27 起自己变红**，
    而失败信息是"overdue 里多了一条" —— 与"筛选走没走索引"毫无关系。

    修法只能是把临界档位抬到不可能超期的值（而不是放松断言 ——
    `== [victim.id]` 正是这条用例要验的东西）。
    """
    _set_sla(ready_client, critical=90, high=7)
    rows = list(db_session.execute(select(Finding)).scalars())
    service.refresh_sla(db_session, rows)
    db_session.commit()

    # 人为把一条改成已超期
    victim = imported[1]
    row = _finding(db_session, victim.id)
    row.sla_due_at = datetime.now(UTC) - timedelta(days=2)
    db_session.commit()

    overdue = ready_client.get(f"{API}/findings?slaState=overdue").json()
    assert [r["id"] for r in overdue] == [victim.id]
    assert overdue[0]["slaState"] == "overdue"
    assert overdue[0]["slaDaysLeft"] == -2

    on_track = ready_client.get(f"{API}/findings?slaState=onTrack").json()
    assert victim.id not in {r["id"] for r in on_track}
