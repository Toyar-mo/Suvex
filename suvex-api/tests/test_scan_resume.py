"""续跑（任务 16）的守卫测试。

## 为什么这件事值得单独一套测试

「续跑」是本项目里**唯一一个把已有记录改回非终态**的动作。
其他每个改变状态的动作（重跑、终止、删除）都不需要"回到过去"，
而续跑必须把上一次尝试的**结论**清干净：`finished_at` / `duration_sec` /
`failure_reason` / `progress` / `exit_code`。

`scans` 上有一条 CHECK 约束 `ck_scans_finished_at_matches_terminal_status`
（终态必须有 `finished_at`，非终态必须没有），所以漏清 `finished_at`
会**撞约束报错** —— 那是好事，是响的。真正危险的是另一种漏法：
清了 `finished_at` 却忘了清 `duration_sec`。那不会报错，
只会让这条扫描以"半小时"的成绩排进"最快扫描"里，
而它其实跑了半小时才倒下。

## 与 strix 硬约束的对齐只能在 argv 上测

`--resume` 与 `--target` **互斥**，strix 的原话是

    Cannot combine --resume with --target/--target-list.

叠上去它直接 `parser.error` 退出，于是这次续跑变成"启动即失败"，
而用户看到的是又一条失败记录。这件事在 HTTP 层完全看不出来 ——
只能测拼出来的 argv。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.vocab import ScanStatus
from app.scans import runner, service
from tests.factories import make_scan

API = "/api/v1"
AUTH = {"confirmed": True, "statement": "我有权对此目标进行安全测试"}

RUN_ID = "35b162d9cea6ede7ab1ee2cc-tcp-ctf_8255"


# ══════════════════════════════════════════════════════════
# 夹具：一条"跑过、然后倒了"的扫描
# ══════════════════════════════════════════════════════════


def _make_failed(
    db: Session,
    tmp_path: Path,
    *,
    status: str = ScanStatus.FAILED.value,
    with_state: bool = True,
    run_id: str = RUN_ID,
):
    """造一条可以续跑的扫描。

    `with_state=False` 时**故意不写** strix 的两个状态文件 ——
    那模拟"会话文件被清理过"，是必须能给出替代动作的那条分支。
    """
    scan = make_scan(db, status=status)
    scan.run_dir = str(tmp_path / "scan")
    scan.runner = "fake"
    scan.run_id = run_id
    scan.progress = 0.4
    scan.exit_code = 1
    db.commit()

    base = Path(scan.run_dir)
    base.mkdir(parents=True, exist_ok=True)
    if with_state:
        # strix 的两个硬前提，见 `runner.can_resume`
        state = base / "strix_runs" / run_id / ".state"
        state.mkdir(parents=True, exist_ok=True)
        (base / "strix_runs" / run_id / "run.json").write_text(
            json.dumps({"targets_info": {"targets": [scan.target]}}), encoding="utf-8"
        )
        (state / "agents.json").write_text(json.dumps({"agents": []}), encoding="utf-8")
    return scan


def _spec(**overrides) -> runner.LaunchSpec:
    base = dict(
        scan_id="scan_test",
        target="api.example.com",
        target_kind="url",
        skills=("web",),
        run_dir=Path("/tmp/irrelevant"),
        log_path=Path("/tmp/irrelevant/scan.log"),
        llm_model="openai/gpt-x",
        llm_api_key="sk-not-a-real-key",
    )
    base.update(overrides)
    return runner.LaunchSpec(**base)


# ══════════════════════════════════════════════════════════
# 一、能不能续：判定必须在**启动前**
# ══════════════════════════════════════════════════════════


def test_can_resume_needs_both_state_files(tmp_path: Path) -> None:
    """两个文件缺一个就不算能续 —— strix 不会优雅退回，它会直接退出。

    所以判定要前置到"能给出解释的时刻"（用户点按钮时），
    而不是留给子进程去撞一个 `parser.error`。
    """
    run_dir = tmp_path / "scan"
    state = run_dir / "strix_runs" / RUN_ID
    state.mkdir(parents=True)

    # 一个都没有
    assert runner.can_resume(run_dir, RUN_ID) is False

    # 只有 run.json
    (state / "run.json").write_text("{}", encoding="utf-8")
    assert runner.can_resume(run_dir, RUN_ID) is False

    # 补上 agents.json 才算数
    (state / ".state").mkdir()
    (state / ".state" / "agents.json").write_text("{}", encoding="utf-8")
    assert runner.can_resume(run_dir, RUN_ID) is True


def test_can_resume_is_false_without_a_run_id(tmp_path: Path) -> None:
    """没跑过（`run_id` 为空 / `run_dir` 为空）就没什么可续的。

    这一条挡的是"对一条**从未启动过**的扫描点续跑"——
    那时 `run_dir` 还不存在，`Path(None)` 会抛 TypeError。
    """
    assert runner.can_resume(tmp_path, None) is False
    assert runner.can_resume(None, RUN_ID) is False


# ══════════════════════════════════════════════════════════
# 二、续跑的语义：**同一条记录**，不是新建
# ══════════════════════════════════════════════════════════


def test_resume_reuses_the_same_record(
    ready_client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    """★ 续跑**就地**回到排队态，不新建记录。

    新建一条会把一份连续的会话历史劈成两条，而
    `activity_events` 的唯一键是 `(scan_id, source_row_id)` ——
    同一次 strix run 的事件会在两条记录下**各导一遍**，
    活动流里出现两份一模一样的轨迹。数据模型上就不该那么做。

    （对照：重跑**必须**新建，见 `test_scans.py` 的同名用例。）
    """
    scan = _make_failed(db_session, tmp_path)
    original_run_dir = scan.run_dir

    response = ready_client.post(f"{API}/scans/{scan.id}/resume", json={"authorization": AUTH})
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["id"] == scan.id, "续跑建出了一条新记录 —— 历史被劈成两条了"
    assert body["status"] == ScanStatus.QUEUED.value

    # ★ `run_id` / `run_dir` 必须留着 —— 它们正是续跑的凭据
    assert body["runId"] == RUN_ID
    db_session.expire_all()
    row = db_session.get(type(scan), scan.id)
    assert row.run_id == RUN_ID
    assert row.run_dir == original_run_dir


def test_resume_clears_the_previous_attempts_conclusions(
    ready_client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    """★ 上一次的"结论"必须清干净，否则它会被当成这次的成绩。

    尤其是 `durationSec`：只清 `finished_at` 而不清它不会违反任何约束，
    于是这条跑了半小时才倒下的扫描会以 120 秒排进"最快扫描"。
    """
    scan = _make_failed(db_session, tmp_path)

    response = ready_client.post(f"{API}/scans/{scan.id}/resume", json={"authorization": AUTH})
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["finishedAt"] is None, "非终态还挂着结束时刻"
    assert body["durationSec"] is None, "上次的耗时会污染'最快扫描'"
    assert body["failureReason"] is None, "已经要接着跑了，还挂着上次的失败原因"
    assert body["progress"] is None
    assert body["startedAt"] is not None


def test_resume_does_not_touch_the_session_files(
    ready_client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    """续跑**不能**顺手把 strix 的状态文件清掉 —— 那正是它要接回去的东西。

    这条防的是一种很自然的"清理"冲动：既然要重跑，先把旧状态删了？
    删了就没得续了，而失败方式是"启动即失败"。
    """
    scan = _make_failed(db_session, tmp_path)

    ready_client.post(f"{API}/scans/{scan.id}/resume", json={"authorization": AUTH})

    assert runner.can_resume(scan.run_dir, scan.run_id) is True


def test_resume_records_a_fresh_authorization(
    ready_client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    """★ 续跑要**重新**确认授权，不能沿用上次的留痕。

    续跑会继续对真实目标发包。而"上次确认过"不该永久授权 ——
    目标可能已经易主，题面也可能换了一版。
    """
    scan = _make_failed(db_session, tmp_path)
    scan.authorization_statement = "上一轮的措辞"
    db_session.commit()

    response = ready_client.post(f"{API}/scans/{scan.id}/resume", json={"authorization": AUTH})
    assert response.status_code == 201, response.text

    db_session.expire_all()
    row = db_session.get(type(scan), scan.id)
    assert row.authorization_statement == AUTH["statement"], "授权留痕还是上一轮的"
    assert row.authorization_received_at is not None


# ══════════════════════════════════════════════════════════
# 三、不能续的时候：409 + 一句**可行动**的话
# ══════════════════════════════════════════════════════════


def test_resume_refuses_a_completed_scan(
    ready_client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    """已完成的扫描不在续跑范围内，且消息要**点名当前状态**。

    它下面的 agent 都已经终结，续上去会立刻又结束 ——
    那是"再挖一轮"的另一个功能，不是续跑。
    """
    scan = _make_failed(db_session, tmp_path, status=ScanStatus.COMPLETED.value)

    response = ready_client.post(f"{API}/scans/{scan.id}/resume", json={"authorization": AUTH})
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "SCAN_NOT_RESUMABLE"
    # 说清是哪一条不满足 —— 否则用户只知道"不行"，不知道"为什么不行"
    assert ScanStatus.COMPLETED.value in error["message"]


def test_resume_refuses_a_running_scan(
    ready_client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    """正在跑的没什么可续 —— 而且续跑会把它的 pid 抹掉，等于把它变成孤儿。"""
    scan = _make_failed(db_session, tmp_path, status=ScanStatus.RUNNING.value)

    response = ready_client.post(f"{API}/scans/{scan.id}/resume", json={"authorization": AUTH})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SCAN_NOT_RESUMABLE"


def test_missing_session_files_offer_the_alternative(
    ready_client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    """★ 会话文件没了：不能只说"不能续"，要给出**替代动作**。

    用户要的是一个结果，"不能续"本身不解决问题，"可以重跑"才解决。
    """
    scan = _make_failed(db_session, tmp_path, with_state=False)

    response = ready_client.post(f"{API}/scans/{scan.id}/resume", json={"authorization": AUTH})
    assert response.status_code == 409, response.text
    message = response.json()["error"]["message"]
    assert "重跑" in message, "只说不能续，没说那该怎么办"


def test_resume_requires_authorization(
    ready_client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    """★★ 缺授权确认就**不能**续 —— 否则它是一条绕过 §5.3 的捷径。

    续跑会继续对真实目标发包。少了这条检查，
    一次陈旧的授权确认就等于对任意时刻的同名目标永久授权。
    """
    scan = _make_failed(db_session, tmp_path)

    # 完全不带 authorization → 403（授权缺失，不是内容不合法）
    missing = ready_client.post(f"{API}/scans/{scan.id}/resume", json={})
    assert missing.status_code == 403, missing.text

    # 带了但措辞不对 → 400
    wrong = ready_client.post(
        f"{API}/scans/{scan.id}/resume",
        json={"authorization": {"confirmed": True, "statement": "我确认过了"}},
    )
    assert wrong.status_code == 400, wrong.text
    assert "authorization.statement" in wrong.json()["error"]["details"]

    # ★ 被拒之后**什么都没变** —— 状态机不能因为一次失败的请求而前进
    db_session.expire_all()
    row = db_session.get(type(scan), scan.id)
    assert row.status == ScanStatus.FAILED.value
    assert row.finished_at is not None


def test_resume_is_audited_as_its_own_action(
    ready_client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    """续跑要留下**自己**的审计动作，不能混进 `scan.rerun`。

    两者对用户是两件不同的事（"接着跑" vs "重开一轮"），
    审计里混在一起的话，"这条扫描为什么又跑了一次"就查不清了。
    """
    from sqlalchemy import select

    from app.audit.models import AuditLog

    scan = _make_failed(db_session, tmp_path)

    ready_client.post(f"{API}/scans/{scan.id}/resume", json={"authorization": AUTH})

    rows = (
        db_session.execute(select(AuditLog).where(AuditLog.action == "scan.resume"))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].target == scan.id
    assert (rows[0].detail or {}).get("runId") == RUN_ID


# ══════════════════════════════════════════════════════════
# 四、启动参数：`--resume` 与 `--target` 互斥
# ══════════════════════════════════════════════════════════


def test_command_never_combines_resume_with_target() -> None:
    """★★ strix 的硬约束，只能在 argv 上验证。

    原话：`Cannot combine --resume with --target/--target-list.`
    续跑时它自己从 `run.json` 读回 `targets_info`，再给一个 `--target`
    是自相矛盾的（以哪个为准？），而它选择直接报错退出 ——
    于是这次续跑变成"启动即失败"，用户看到的是又一条失败记录。
    """
    argv = runner.StrixRunner().command(_spec(resume_run_id=RUN_ID))

    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == RUN_ID
    assert "--target" not in argv, "续跑又叠了 --target，strix 会直接退出"


def test_command_targets_normally_when_not_resuming() -> None:
    """不续跑时仍然是 `--target`，且**没有** `--resume`（反向也要成立）。"""
    argv = runner.StrixRunner().command(_spec())

    assert "--target" in argv
    assert argv[argv.index("--target") + 1] == "api.example.com"
    assert "--resume" not in argv


def test_command_takes_scan_depth_from_the_spec_not_from_env(
    monkeypatch,
) -> None:
    """扫描深度与回合上限取自 `spec`（设置页覆盖后的生效值），不是直接读 env。

    从 env 读的话，设置页上的改动**永远不会生效** ——
    而界面上还写着"已生效"。这类"界面说谎"是最难被发现的一种 bug。
    """
    from app.core.config import get_settings

    monkeypatch.setenv("SUPEX_SCAN_MODE", "deep")
    monkeypatch.setenv("SUPEX_SCAN_MAX_TURNS", "1999")
    get_settings.cache_clear()
    try:
        argv = runner.StrixRunner().command(_spec(scan_mode="quick", scan_max_turns=42))
    finally:
        get_settings.cache_clear()

    assert argv[argv.index("--scan-mode") + 1] == "quick"
    assert argv[argv.index("--max-turns") + 1] == "42"


# ══════════════════════════════════════════════════════════
# 五、排队 → 启动之间：状态文件消失了怎么办
# ══════════════════════════════════════════════════════════


def test_build_spec_derives_resume_run_id(db_session: Session, tmp_path: Path) -> None:
    """`build_spec` 是"这一次启动到底续不续"的唯一判定点。

    判据只看"有没有 `run_id` + 状态文件还在不在"，这是**无歧义**的：
    重跑走 `rerun_scan()`，它建的是新记录（`run_id` 为空），
    所以"有 `run_id` 且能续"只可能来自 `resume_scan()`。
    """
    scan = _make_failed(db_session, tmp_path)

    spec = service.build_spec(db_session, scan)
    assert spec.resume_run_id == RUN_ID


def test_build_spec_falls_back_to_a_fresh_scan_when_state_vanished(
    db_session: Session, tmp_path: Path
) -> None:
    """★ 排队与启动之间状态文件被清了：**退回全新扫描**，不要报错。

    入口处 `resume_scan()` 已经挡过一次，走到这里说明文件是在
    "排队"与"启动"之间消失的（被手工清理）。那时报错不如照跑 ——
    用户的意图是"把这次跑完"，而一个 `parser.error` 只会再给他一条失败记录。
    """
    scan = _make_failed(db_session, tmp_path)

    state = Path(scan.run_dir) / "strix_runs" / RUN_ID / ".state" / "agents.json"
    state.unlink()

    spec = service.build_spec(db_session, scan)
    assert spec.resume_run_id is None, "状态文件都没了还去续，等于启动即失败"


# ══════════════════════════════════════════════════════════
# 六、run 标识丢了：从磁盘救回来
# ══════════════════════════════════════════════════════════
#
# ## 这一节来自一次真实的 409（2026-09-27）
#
# 用户点「续跑」，界面上**什么都没发生**，服务端日志里是三次 409。
# 查下来：那条扫描跑了 828 秒、起了 3 个 agent，被一次服务重启打断，
# `recover_orphans()` 把它标成 failed —— 而平时负责补录 `run_id` 的
# `_reap_finished()` 遍历的是**内存里的进程句柄表**，重启后那张表是空的，
# 所以那条路径永远轮不到它。
#
# 于是磁盘上 `strix_runs/challenge_7f94/run.json`（36 KB）与
# `.state/agents.json` 一应俱全、**完全能续**，而 `can_resume` 因为
# `run_id` 是 NULL 直接判否，错误信息还说"会话文件已不在"—— 与事实**正好相反**。
#
# 两条测试分别盯治标与治本：
#   · 治标：`resume_scan` 从磁盘把标识找回来（救已经坏掉的记录）；
#   · 治本：`recover_orphans` 当场记下来（不再产生新的坏记录）。


def test_resume_recovers_a_missing_run_id_from_disk(
    ready_client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    """★★ 记录里没有 `run_id`，但磁盘上会话完整 → **仍然能续**。

    构造方式就是那次事故的形状：**状态文件都在，`run_id` 是 NULL**。

    断言两件事，缺一不可：
      1. 能续（`201`）—— 能续本身还不够；
      2. `run_id` 被**写回记录** —— 否则判据每被问一次就要重扫一遍磁盘，
         而且下一次启动依然会认为它"没跑过"。
    """
    scan = _make_failed(db_session, tmp_path)
    scan.run_id = None  # ← 模拟"从没被写进库"（服务重启带走的那一种）
    db_session.commit()

    response = ready_client.post(f"{API}/scans/{scan.id}/resume", json={"authorization": AUTH})

    assert response.status_code == 201, response.text
    db_session.expire_all()
    assert db_session.get(type(scan), scan.id).run_id == RUN_ID


def test_a_genuinely_lost_session_still_refuses(
    ready_client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    """★ 反向：会话文件**真的**不在时，照样要拒。

    没有这一条的话，"从磁盘找回来"这段逻辑可以退化成"随便编一个" ——
    而那时用户会得到一次**启动即失败**的续跑（strix 直接报
    `missing .../agents.json`），比一句清楚的 409 难查得多。
    """
    scan = _make_failed(db_session, tmp_path, with_state=False)
    scan.run_id = None
    db_session.commit()

    response = ready_client.post(f"{API}/scans/{scan.id}/resume", json={"authorization": AUTH})

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SCAN_NOT_RESUMABLE"


def test_recover_orphans_records_the_run_id(
    ready_client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    """★★ 治本的那一处：孤儿回收时**顺手把 `run_id` 记下来**。

    为什么必须在源头补、而不是只靠续跑时自愈：自愈只发生在用户**点了续跑**
    那一刻。在那之前，界面上看不出这条记录其实能续，而且每问一次就要
    重扫一遍磁盘。

    构造"上一次运行留下的行"：**直接插库**，不走 `POST /scans` ——
    走接口的话 `_nudge_scheduler` 会立刻把它启动起来，它就不是孤儿了
    （与 `test_scans.py` 里同名用例同一条理由）。
    """
    from app.scans.scheduler import get_scheduler

    scan = _make_failed(db_session, tmp_path, status=ScanStatus.RUNNING.value)
    scan.run_id = None
    scan.pid = 999_999  # 几乎不可能存在的 pid → 走"进程已不在"那条分支
    db_session.commit()

    assert get_scheduler().recover_orphans() == 1

    db_session.expire_all()
    row = db_session.get(type(scan), scan.id)
    assert row.status == ScanStatus.FAILED.value
    # ★ 这一条就是本次修复的核心断言
    assert row.run_id == RUN_ID, "run_id 没被补上 —— 这条记录永远续不了"
