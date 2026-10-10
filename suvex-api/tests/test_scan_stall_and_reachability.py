"""卡死保护 / 终止方归因 / 协议预检（任务 11）。

这三件事来自同一个现场：一次真扫描跑了 **32 分钟零产出**。

* 用户填的目标没带协议，agent 自己补了 `https://`，而靶机只监听 http
  → 每个请求撞在 TLS 握手上 → `ffuf` 空转 → 45 次空输入轮询
  → 而 `--max-turns` 拦不住（回合数没涨，时间在走）。
* 那次扫描最终是被**服务重启**终止的，但界面上「终止方」是空的 ——
  因为 `terminated_by` **从来没有被任何代码写过**。
"""

from __future__ import annotations

from pathlib import Path
import json
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import now_utc
from app.scans import reachability
from app.scans.models import Scan
from app.scans.reachability import Reachability

API = "/api/v1"


# ══════════════════════════════════════════════════════════
# 协议预检
# ══════════════════════════════════════════════════════════


def test_no_scheme_prefers_the_protocol_that_answers(monkeypatch) -> None:
    """★ 用户没写协议时，两个都试，用**真的通的那个**。

    这正是那次白跑的场景：目标只监听 http，而 agent 自己猜了 https。
    """
    seen: list[str] = []

    def fake_probe(url: str, timeout: float) -> bool:
        seen.append(url)
        return url.startswith("http://")  # 只有 http 通

    monkeypatch.setattr(reachability, "_probe", fake_probe)
    result = reachability.check_reachability("example.com")

    assert result.effective_target == "http://example.com"
    assert result.http_ok is True and result.https_ok is False
    assert result.notice and "只在 http" in result.notice
    # https 先试（现代站点多数如此），失败再试 http
    assert seen[0].startswith("https://")


def test_no_scheme_prefers_https_when_both_work(monkeypatch) -> None:
    monkeypatch.setattr(reachability, "_probe", lambda url, timeout: True)
    result = reachability.check_reachability("example.com")

    assert result.effective_target == "https://example.com"
    assert result.notice is None, "两个都通时没什么要说的"


def test_explicit_scheme_is_not_overridden(monkeypatch) -> None:
    """★ 用户明确写了协议就**不擅自改**。

    他写 https 通常是有理由的（前面有反代），而"帮他换掉"可能让
    扫描打到一个不该打的地方。
    """
    monkeypatch.setattr(reachability, "_probe", lambda url, timeout: url.startswith("http://"))
    result = reachability.check_reachability("https://example.com")

    assert result.effective_target == "https://example.com", "不能替换成 http"
    assert result.notice and "http://" in result.notice, "但要提示另一条协议通"


def test_probe_accepts_any_http_status(monkeypatch) -> None:
    """★ 「服务应答了」就算通，**不要求 200**。

    那次的目标根路径就是 404（`Target not found`），而它明明活着。
    按 2xx 判会把一个正常可打的目标标成"不可达"，
    然后用户会去排查一个不存在的问题。
    """
    class FakeResponse:
        status_code = 404

    class FakeHttpx:
        @staticmethod
        def get(url, timeout, follow_redirects):  # noqa: ANN001
            return FakeResponse()

    import sys

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)
    assert reachability._probe("http://x", 1.0) is True


def test_unreachable_target_is_reported_but_not_blocking(monkeypatch) -> None:
    """两个协议都不通时**给出提示，但仍返回一个可用的地址**。

    不阻断创建：目标可能只在内网可达、或者此刻正在重启，
    而"我就是要在它恢复后立刻扫"必须做得到。
    """
    monkeypatch.setattr(reachability, "_probe", lambda url, timeout: False)
    result = reachability.check_reachability("offline.example.com")

    assert result.effective_target is not None
    assert result.notice and "没有应答" in result.notice


# ══════════════════════════════════════════════════════════
# 创建扫描时的接线
# ══════════════════════════════════════════════════════════


def test_created_scan_keeps_normalized_target_and_stores_probe_separately(
    ready_client: TestClient, provider_id: str, monkeypatch
) -> None:
    """★ 归一化后的 `target` **不带协议**，探测结果存在另一列。

    这是被 `test_target_is_normalized_before_storage` 抓出来的：
    我第一版把探测到的地址写进了 `target`，于是
    `example.com` 与 `https://example.com` 会变成两条不同的记录 ——
    而那条测试要防的正是"同一目标被当成两个"。
    """
    monkeypatch.setattr(
        "app.scans.service.check_reachability",
        lambda target, **kw: Reachability("http://example.com", "只在 http 下有应答", True, False),
    )
    response = ready_client.post(
        f"{API}/scans",
        json={
            "target": "example.com",
            "targetKind": "url",
            "skills": ["web"],
            "providerId": provider_id,
            "authorization": {"confirmed": True, "statement": "我有权对此目标进行安全测试"},
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["target"] == "example.com", "展示字段保持归一化（不带协议）"
    # 预检结论通过响应带回来，界面才能提示
    assert body["notice"] and "只在 http" in body["notice"]


# ══════════════════════════════════════════════════════════
# 卡死检测
# ══════════════════════════════════════════════════════════


def _running_scan(db: Session, *, started_ago_seconds: int) -> Scan:
    from tests.factories import make_scan

    scan = make_scan(db, status="running")
    scan.started_at = now_utc() - timedelta(seconds=started_ago_seconds)
    db.commit()
    return scan


def test_stalled_scan_is_flagged(db_session: Session, scan_config) -> None:
    """★ 静默超过阈值就标记（默认只标记、不终止）。

    那次 32 分钟里，事件流上停着不动，而系统毫无反应 ——
    用户只能看着一个转圈的界面。
    """
    scan_config(SUPEX_SCAN_STALL_SECONDS="60")
    from app.scans.scheduler import get_scheduler

    scan = _running_scan(db_session, started_ago_seconds=600)
    get_scheduler()._detect_stalls()

    db_session.expire_all()
    row = db_session.get(Scan, scan.id)
    assert row.stalled_at is not None, "超过阈值该被标记"
    assert row.status == "running", "默认只标记，不终止"


def test_stall_flag_clears_when_activity_resumes(db_session: Session, scan_config) -> None:
    """★ 标记是**可自愈**的。

    否则一次误判（某个工具确实安静了 11 分钟才输出）会让界面永远
    挂着"疑似卡死"，而它其实早就继续跑了。
    """
    scan_config(SUPEX_SCAN_STALL_SECONDS="60")
    from app.scans.scheduler import get_scheduler

    scan = _running_scan(db_session, started_ago_seconds=600)
    scheduler = get_scheduler()
    scheduler._detect_stalls()
    db_session.expire_all()
    assert db_session.get(Scan, scan.id).stalled_at is not None

    # 来了一条新事件 → 标记该被清掉
    #
    # ⚠️ 必须先建 agent：`activity_events.agent_id` 有外键指向 `agents`。
    #    直接插一条带假 agent_id 的事件会撞约束 —— 而那正是第一次跑
    #    这个测试时的报错。
    from tests.factories import make_agent, make_event

    agent = make_agent(db_session, scan.id)
    make_event(
        db_session,
        scan_id=scan.id,
        agent_id=agent.id,
        type="reasoning",
        summary="又动起来了",
    )

    scheduler._detect_stalls()
    db_session.expire_all()
    assert db_session.get(Scan, scan.id).stalled_at is None, "有新事件就该清掉标记"


def test_stall_detection_can_be_disabled(db_session: Session, scan_config) -> None:
    """阈值设 0 = 关掉检测。"""
    scan_config(SUPEX_SCAN_STALL_SECONDS="0")
    from app.scans.scheduler import get_scheduler

    scan = _running_scan(db_session, started_ago_seconds=99_999)
    get_scheduler()._detect_stalls()

    db_session.expire_all()
    assert db_session.get(Scan, scan.id).stalled_at is None


# ══════════════════════════════════════════════════════════
# 终止方归因
# ══════════════════════════════════════════════════════════


def test_user_abort_records_terminated_by_user(
    ready_client: TestClient, db_session: Session, scan_config
) -> None:
    """★ 用户点的终止 → `terminated_by = "user"`。

    这个字段一直存在于模型与契约里、界面也在显示，但**从来没有任何
    代码写过它** —— 于是「终止方」永远是空的，看不出是谁停的。
    """
    from tests.factories import make_scan

    scan = make_scan(db_session, status="running")

    response = ready_client.post(
        f"{API}/scans/{scan.id}/abort",
        json={"authorization": {"confirmed": True, "statement": "我有权对此目标进行安全测试"}},
    )
    assert response.status_code == 200, response.text

    db_session.expire_all()
    row = db_session.get(Scan, scan.id)
    assert row.status == "aborted"
    assert row.terminated_by == "user"


def test_service_stop_records_terminated_by_system(db_session: Session) -> None:
    """★ 服务停止导致的中断 → `terminated_by = "system"`。

    这条特别重要：strix 那边会把同一件事记成
    `Scan ... interrupted by the user`（它只能看到"父进程叫我停"，
    看不到是谁叫的）。写清 `system` 之后，界面上的「终止方」才能
    回答那个真问题：**是我停的，还是服务重启顺手带走的**。
    """
    from app.scans.scheduler import get_scheduler

    scan = _running_scan(db_session, started_ago_seconds=30)

    # 让调度器"跟踪"它，模拟一个正在跑的扫描
    class FakeProc:
        pid = 999_999

        @staticmethod
        def poll():  # noqa: ANN205
            return None  # 还在跑

    scheduler = get_scheduler()
    scheduler._procs[scan.id] = FakeProc()  # type: ignore[assignment]

    import app.scans.scheduler as sched_mod

    # 不真去杀进程：这里只验状态与归因
    original = sched_mod.terminate_tree
    sched_mod.terminate_tree = lambda *a, **kw: True  # type: ignore[assignment]
    try:
        scheduler._terminate_tracked(reason="服务停止，扫描已终止")
    finally:
        sched_mod.terminate_tree = original  # type: ignore[assignment]
        scheduler._procs.pop(scan.id, None)

    db_session.expire_all()
    row = db_session.get(Scan, scan.id)
    assert row.status == "aborted"
    assert row.terminated_by == "system"
    assert row.failure_reason == "服务停止，扫描已终止"


def test_explicit_scheme_is_not_rewritten_end_to_end(
    ready_client: TestClient, provider_id: str, monkeypatch
) -> None:
    """★ 走**完整 create_scan 链路**验证「用户写的协议不被改写」。

    ⚠️ 这条测试是补写的，因为上面那条直接调 `check_reachability` 的版本
    **在被修复前也是绿的** —— 它绕过了 `create_scan` 的接线，
    而 bug 恰恰就在那一行的参数上（传了归一化值当 origin）。

    所以这里只 mock **网络层**（`_probe`），其余全走真实代码：
    `create_scan` → `check_reachability` → 响应。
    判据用"探过哪些 URL"，因为那才是接线错没错的直接证据。
    """
    import app.scans.reachability as reach_mod

    probed: list[str] = []

    def fake_probe(url: str, timeout: float) -> bool:
        probed.append(url)
        return url.startswith("https://")

    monkeypatch.setattr(reach_mod, "_probe", fake_probe)

    response = ready_client.post(
        f"{API}/scans",
        json={
            # 用户**显式**写了 https，而且 https 是通的
            "target": "https://example.com/admin",
            "targetKind": "url",
            "skills": ["web"],
            "providerId": provider_id,
            "authorization": {"confirmed": True, "statement": "我有权对此目标进行安全测试"},
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["target"] == "example.com", "归一化照旧（剥掉协议与路径）"
    assert not any(u.startswith("http://") for u in probed), (
        f"用户显式写了 https，就不该再去探 http —— 实际探了：{probed}"
    )
    assert probed and probed[0] == "https://example.com", (
        f"该拿用户的原始输入判协议、用归一化值构造地址（不带路径）：{probed}"
    )
    assert body["notice"] is None, "协议是通的，没有任何要提示的"


def test_stall_action_abort_terminates_the_scan(db_session: Session, scan_config) -> None:
    """★ `scan_stall_action="abort"` 时**真的终止**，并归因给 `system`。

    默认是 `flag`（只标记），但运维可能确实需要自动清理卡死的扫描 ——
    这条路径必须被测到，否则它只是在配置里承诺了一个做不到的事。

    这里句柄**不在**调度器手里（重启后的孤儿就是这种形态），
    所以还要验措辞诚实：不能说"已终止"却不提进程可能还活着。
    """
    scan_config(SUPEX_SCAN_STALL_SECONDS="60", SUPEX_SCAN_STALL_ACTION="abort")
    from app.scans.scheduler import get_scheduler

    scan = _running_scan(db_session, started_ago_seconds=600)
    get_scheduler()._detect_stalls()

    db_session.expire_all()
    row = db_session.get(Scan, scan.id)
    assert row.status == "aborted", "abort 模式该真的终止"
    assert row.terminated_by == "system", "是服务按规则终止的，不是用户"
    assert row.finished_at is not None, "终态必须有 finished_at（CHECK 约束）"
    assert row.stalled_at is not None, "标记与终止是两件事，两个都要落库"
    assert "手工检查" in (row.failure_reason or ""), (
        "句柄不在手上时不能谎称已终止 —— 进程可能还在跑"
    )


def test_stall_action_rejects_unknown_value(scan_config) -> None:
    """拼错的动作名要在**启动时**报错，不能静默退化成 flag。

    静默退化的话，运维会以为"自动终止已经开着"，而它从来没生效过 ——
    等到真有扫描卡死一整天才会发现。
    """
    import pytest

    with pytest.raises(Exception):
        scan_config(SUPEX_SCAN_STALL_ACTION="abortt")


# ══════════════════════════════════════════════════════════
# instruction 的中文要求
# ══════════════════════════════════════════════════════════

def test_instruction_asks_for_chinese_but_keeps_commands_verbatim() -> None:
    """★ 要求中文输出，但**命令/URL/payload 保持原样**。

    实测时活动页的"推理"全是英文，而这套界面是中文的 ——
    那种混搭让人以为"是不是坏了"。

    但不能要求"全部翻译"：把 `ffuf -w ... -u ...` 译成中文
    会让命令直接跑不起来。
    """
    from app.scans.runner import LaunchSpec, StrixRunner

    # ★ 用**真的** LaunchSpec，而不是一个只带两个属性的替身
    #   （任务 12）：替身会在 `_instruction` 新增字段访问时炸，
    #   而那种失败看起来像"指令生成坏了"，其实是测试的假对象过期了。
    #   用真类型意味着**加字段时这里不用改**。
    spec = LaunchSpec(
        scan_id="scan_x",
        target="http://example.com",
        target_kind="url",
        skills=("web",),
        run_dir=Path("_unused"),
        log_path=Path("_unused/scan.log"),
        llm_model="openai/test",
        llm_api_key="sk-test",
    )

    text = StrixRunner._instruction(spec)
    assert "中文" in text
    assert "不要翻译" in text or "保持原样" in text
    # 原始的技能范围说明还在
    assert "Web 应用层漏洞" in text
