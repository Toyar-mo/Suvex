"""扫描生命周期（任务 3）：创建 / 启动 / 状态机 / 终止 / 重跑 / 并发上限。

## 这些测试真在跑子进程

`SUPEX_SCAN_RUNNER=fake` 的后端**不是 mock**：它同样 spawn 一个真的子进程
（还带一个真的孙进程），只是那个进程不做任何真实攻击。
所以队列、进程监督、杀进程树、退出码解释这些逻辑全都是真的在跑。

被替换掉的只有"那个进程里跑的是 strix 还是替身"这一件事。
用真 strix 的后果是向真实目标发包 —— 测试里绝不能那么做
（`conftest.py` 顶部有安全网，在导入任何 app 模块之前就把后端设成了 fake）。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.vocab import ScanStatus
from app.scans.models import Scan
from app.scans.runner import is_alive, popen_kwargs
from tests.conftest import SCAN_WORK_ROOT
from tests.factories import make_scan

AUTH_STATEMENT = "我有权对此目标进行安全测试"
VALID_AUTH = {"confirmed": True, "statement": AUTH_STATEMENT}
KEY = "sk-test-SCAN-3f8a1c4e9b7d30625a8c1f4e"


# ══════════════════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════════════════


@pytest.fixture()
def provider_id(ready_client: TestClient) -> str:
    created = ready_client.post(
        "/api/v1/providers",
        json={
            "label": "DeepSeek",
            "model": "deepseek-chat",
            "kind": "cloud",
            "apiKey": KEY,
            "baseUrl": "https://api.deepseek.com",
            "note": "测试用",
            "isDefault": True,
        },
    )
    assert created.status_code in (200, 201), created.text
    return created.json()["provider"]["id"]


def scan_body(provider_id: str, **overrides) -> dict:
    body = {
        "name": "测试扫描",
        "target": "https://api.example.com/",
        "targetKind": "url",
        "skills": ["web", "api"],
        "providerId": provider_id,
        "authorization": dict(VALID_AUTH),
    }
    body.update(overrides)
    return body


def create_scan(client: TestClient, provider_id: str, **overrides) -> dict:
    response = client.post("/api/v1/scans", json=scan_body(provider_id, **overrides))
    assert response.status_code in (200, 201), response.text
    return response.json()


def status_of(client: TestClient, scan_id: str) -> str:
    return client.get(f"/api/v1/scans/{scan_id}").json()["status"]


def wait_for_status(
    client: TestClient, scan_id: str, expected: set[str], *, timeout: float = 30.0
) -> str:
    """轮询到期望状态，**每一轮都驱动一次调度器**。

    两件事一起做是刻意的：

    1. **驱动调度器**：测试环境把轮询间隔设成了一小时（`SUPEX_SCAN_POLL_SECONDS`），
       这样后台线程不会去认领别的测试造出来的 `queued` 数据。
       代价是**没有人回收退出的进程** —— 所以这里替它做。
       而"调用 tick()"正是调度器在线上做的事，不是特例路径。
    2. **走 HTTP 接口读状态**：这顺便验证了前端轮询要用的那条路径
       （Scans 页就是 `GET /scans/{id}`）。用 sleep 一个固定时长的话，
       这条路径一次都没被走过。
    """
    from app.scans.scheduler import get_scheduler

    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        get_scheduler().tick()
        current = status_of(client, scan_id)
        if not seen or seen[-1] != current:
            seen.append(current)
        if current in expected:
            return current
        time.sleep(0.1)
    raise AssertionError(f"扫描 {scan_id} 在 {timeout}s 内没到 {expected}，经历的状态：{seen}")


def scan_row(db: Session, scan_id: str) -> Scan:
    return db.execute(select(Scan).where(Scan.id == scan_id)).scalar_one()


# ══════════════════════════════════════════════════════════
# 1. 授权确认（契约 §5）—— 缺失直接拒绝创建
# ══════════════════════════════════════════════════════════


def test_missing_authorization_is_rejected_with_403(ready_client: TestClient, provider_id: str) -> None:
    """★ 缺 `authorization` → **403 `TARGET_NOT_AUTHORIZED`**，不是 400。

    契约 §5.3 把这两种失败分得很清楚，而它们的码不同是有理由的：
    403 要引导用户去勾选授权，400 要提示重新确认声明。
    用错一个码，界面就会指错方向。

    这一条也是"服务端强制校验"的证明 —— 前端可以被绕过（改 JS、直接 curl），
    所以拦截必须在这里。
    """
    body = scan_body(provider_id)
    body.pop("authorization")

    response = ready_client.post("/api/v1/scans", json=body)

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "TARGET_NOT_AUTHORIZED"
    assert "授权" in error["message"]


def test_unchecked_authorization_is_rejected(ready_client: TestClient, provider_id: str) -> None:
    """`confirmed != true` 同样是 403 —— 勾了但没真勾也算。"""
    response = ready_client.post(
        "/api/v1/scans",
        json=scan_body(provider_id, authorization={"confirmed": False, "statement": AUTH_STATEMENT}),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TARGET_NOT_AUTHORIZED"


def test_wrong_statement_is_rejected_with_400(ready_client: TestClient, provider_id: str) -> None:
    """★ `statement` 与约定措辞**不完全一致** → **400** `VALIDATION_FAILED`。

    逐字校验措辞是刻意的（契约 §5.3）：**防止前端"顺手勾一个默认值"**。
    用户必须看到、并同意那**具体一句话**。这也让留痕有意义 ——
    记录的是一条明确的声明，不是一个布尔。
    """
    response = ready_client.post(
        "/api/v1/scans",
        json=scan_body(
            provider_id, authorization={"confirmed": True, "statement": "我确认过了"}
        ),
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert "authorization.statement" in error["details"]
    # 提示里给出**正确的原文** —— 那是用户能不能同意的前提
    assert AUTH_STATEMENT in error["details"]["authorization.statement"]


def test_authorization_is_recorded_forensically(
    ready_client: TestClient, provider_id: str, db_session: Session
) -> None:
    """★ 落库六列，一个都不能少（契约 §5.4）。

    其中三列只有服务端能记录，也正是留痕真正的价值：
    服务端收到的时刻、来源 IP、User-Agent —— 客户端伪造不了它们。
    另外三列客户端可以伪造，但仍然要存：留痕要能被**独立理解**，
    而不是"某个人授权了"。
    """
    created = create_scan(ready_client, provider_id)
    row = scan_row(db_session, created["id"])

    assert row.authorization_statement == AUTH_STATEMENT
    assert row.authorized_by  # 单管理员系统里恒定，但仍然要存
    assert row.authorization_received_at is not None
    assert row.authorization_ip == "testclient"
    assert row.authorization_user_agent is not None
    # 客户端没传 confirmedAt 时它是 NULL —— 权威的是上面那个服务端时刻
    assert row.authorization_confirmed_at is None


def test_rerun_also_requires_authorization(ready_client: TestClient, provider_id: str) -> None:
    """★ 重跑**同样**要求授权确认（契约 §6 专门警告过容易漏）。

    重跑在语义上是一次新扫描，目标可能已经变了（域名易主、IP 重分配）。
    不能因为"上次确认过"就免掉 —— 那等于让一次确认永久授权
    对任意时刻的同名目标发包。
    """
    first = create_scan(ready_client, provider_id)

    response = ready_client.post(f"/api/v1/scans/{first['id']}/rerun", json={})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TARGET_NOT_AUTHORIZED"


# ══════════════════════════════════════════════════════════
# 2. 创建：初始状态与归一化
# ══════════════════════════════════════════════════════════


def test_created_scan_starts_as_queued(ready_client: TestClient, provider_id: str) -> None:
    """★ 初始状态是 `queued`，**不是** `running`（契约 §3.3）。

    `running` 意味着"agent 已经在打了"，而实际要等调度器接单
    （并发满了还得排队）。**状态语义不能撒谎** ——
    用户看到 `running` 会以为已经在测了，然后困惑于为什么没有输出。
    """
    created = create_scan(ready_client, provider_id)

    assert created["status"] == ScanStatus.QUEUED.value
    assert created["finishedAt"] is None
    # ⚠️ running 时 durationSec 必须是 null 而不是 0（契约 §2.1）
    assert created["durationSec"] is None
    assert created["runId"] is None
    assert created["findings"] == {}


def test_target_is_normalized_before_storage(
    ready_client: TestClient, provider_id: str
) -> None:
    """★ 存的是**归一化后**的目标（契约 §2.8）。

    不归一化的话，`api.example.com` / `https://api.example.com` /
    `https://api.example.com/` 会在列表里变成三条看起来不同的记录，
    而它们是同一个东西。
    """
    created = create_scan(ready_client, provider_id, target="https://api.example.com/admin?x=1")

    assert created["target"] == "api.example.com"


def test_invalid_target_is_rejected(ready_client: TestClient, provider_id: str) -> None:
    response = ready_client.post(
        "/api/v1/scans", json=scan_body(provider_id, target="api", targetKind="url")
    )

    assert response.status_code == 400
    assert "target" in response.json()["error"]["details"]


def test_name_falls_back_to_target(ready_client: TestClient, provider_id: str) -> None:
    created = create_scan(ready_client, provider_id, name="", target="api.example.com")

    assert created["name"] == "api.example.com"


def test_unknown_provider_is_rejected(ready_client: TestClient) -> None:
    """provider 不存在 → 400，且提示可操作。

    ⚠️ **这里就是 `scans.provider_id` 完整性的保证** ——
    任务 2 移除了那条外键（它是历史引用，不是引用完整性约束），
    所以校验必须在写入这一刻做。
    """
    response = ready_client.post("/api/v1/scans", json=scan_body("prov_does_not_exist"))

    assert response.status_code == 400
    assert "provider" in response.json()["error"]["message"]


def test_scan_requires_scan_scope_not_just_read(ready_client: TestClient, provider_id: str) -> None:
    """只读令牌不能发起扫描（契约 §1.2）。

    这一条是 `TokenScope` 体系存在的意义：`POST /scans` **会对真实目标发包**，
    而"能访问到 API"不该等于"能发起扫描"。
    """
    token = ready_client.post(
        "/api/v1/settings/tokens",
        json={"label": "报表脚本", "scopes": ["read"], "expiresAt": None},
    ).json()["plaintext"]
    body = scan_body(provider_id)

    ready_client.cookies.clear()
    response = ready_client.post(
        "/api/v1/scans", json=body, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INSUFFICIENT_SCOPE"


# ══════════════════════════════════════════════════════════
# 3. 状态机：真的跑起来、真的收尾
# ══════════════════════════════════════════════════════════


def test_scan_runs_and_completes(ready_client: TestClient, provider_id: str) -> None:
    """queued → running → completed，且时间戳与耗时都对。"""
    created = create_scan(ready_client, provider_id)

    assert wait_for_status(ready_client, created["id"], {ScanStatus.RUNNING.value}) == "running"
    assert wait_for_status(
        ready_client, created["id"], {ScanStatus.COMPLETED.value}, timeout=30
    ) == "completed"

    final = ready_client.get(f"/api/v1/scans/{created['id']}").json()
    assert final["finishedAt"] is not None
    # 终态才有耗时，且是一个合理的正数
    assert isinstance(final["durationSec"], int)
    assert 0 <= final["durationSec"] < 60
    # 终态不该再有"进度"这个概念
    assert final["progress"] is None
    # ★ 外部工具自己的 run 标识被读回来了（任务要求 2）
    assert final["runId"] and final["runId"].startswith("fake-")


def test_exit_code_two_means_findings_not_failure(
    ready_client: TestClient, provider_id: str, scan_config
) -> None:
    """★ 退出码 2 是**成功**（发现了漏洞），不是执行失败。

    这是这一层最容易写错的一处：把"结果不干净"当成"跑挂了"，
    会让本来成功的扫描显示成失败，而用户会去重跑它。
    strix 的退出码表：0 = 完成（无人值守下表示没发现漏洞），
    1 = 致命错误，2 = 发现了漏洞。
    """
    scan_config(SUPEX_FAKE_SCAN_EXIT_CODE="2")
    created = create_scan(ready_client, provider_id)

    final = wait_for_status(
        ready_client, created["id"], {ScanStatus.COMPLETED.value, ScanStatus.FAILED.value}
    )

    assert final == ScanStatus.COMPLETED.value
    assert ready_client.get(f"/api/v1/scans/{created['id']}").json()["failureReason"] is None


def test_nonzero_exit_marks_failed_with_the_log_tail(
    ready_client: TestClient, provider_id: str, scan_config
) -> None:
    """★ 退出码 1 → failed，且**失败原因来自子进程的日志尾部**。

    这是刻意的：真正能解释问题的那句话在子进程的输出里。
    典型场景就是本机没有 Docker —— strix 会打印一句
    "DOCKER NOT INSTALLED" 并以 1 退出，用户应该在界面上直接看到它，
    而不是我们编的一句"扫描失败"。
    """
    scan_config(SUPEX_FAKE_SCAN_EXIT_CODE="1")
    created = create_scan(ready_client, provider_id)

    assert wait_for_status(ready_client, created["id"], {ScanStatus.FAILED.value}) == "failed"

    body = ready_client.get(f"/api/v1/scans/{created['id']}").json()
    assert body["failureReason"]
    # 替身脚本会把退出码打进日志，所以我们能验证"原因确实取自日志"
    assert "fake-strix" in body["failureReason"]


def test_lifecycle_events_are_emitted(ready_client: TestClient, provider_id: str) -> None:
    """状态变更要能被活动流看到（任务要求 3）。

    事件走 `activity_events`，于是 Activity 页与 SSE 流自动就有了内容 ——
    不需要为"扫描状态"再造一条通道。
    """
    created = create_scan(ready_client, provider_id)
    wait_for_status(ready_client, created["id"], {ScanStatus.COMPLETED.value}, timeout=30)

    events = ready_client.get(f"/api/v1/scans/{created['id']}/events/recent").json()["events"]
    types = [event["type"] for event in events]

    assert "completed" in types
    # 每条事件都必须挂在某个 agent 上（前端 ActivityEvent.agentId 是 string）
    assert all(event["agentId"] for event in events)
    assert all(event["agentPhase"] in {"recon", "scan", "exploit", "verify", "report"} for event in events)


def test_runner_command_never_carries_the_credential_in_argv(
    ready_client: TestClient, provider_id: str
) -> None:
    """★ 凭据**只走环境变量**，绝不进命令行参数。

    命令行参数在进程列表里对同机其他用户**可见**（`ps aux`、
    任务管理器都能看到）。一个 API key 出现在那里等于泄漏给整台机器。

    这里验的是替身后端的 argv —— 它与 strix 后端共用同一套
    "凭据走 env" 的约定，而 `StrixRunner.command()` 的实现里根本没有
    凭据这个变量可以放。
    """
    from app.core.config import get_settings
    from app.scans import service
    from app.scans.runner import get_runner

    created = create_scan(ready_client, provider_id)
    wait_for_status(ready_client, created["id"], {ScanStatus.COMPLETED.value}, timeout=30)

    from app.core.db import get_session_factory

    session = get_session_factory()()
    try:
        scan = session.get(Scan, created["id"])
        spec = service.build_spec(session, scan)
        argv = get_runner().command(spec)
        env = get_runner().env(spec)
    finally:
        session.close()

    assert KEY not in " ".join(argv)
    assert env["LLM_API_KEY"] == KEY
    assert get_settings().strix_command  # 配置项存在（真后端会用它）


# ══════════════════════════════════════════════════════════
# 4. 并发上限（任务要求 6）
# ══════════════════════════════════════════════════════════


def test_concurrency_limit_is_enforced(
    ready_client: TestClient, provider_id: str, scan_config
) -> None:
    """★ 并发上限生效：超过上限的扫描留在队列里，**不启动**。

    任务原文：「不要无限制地并发起真实攻击性质的进程」——
    这一层不是性能优化，是安全阀。
    """
    scan_config(SUPEX_MAX_CONCURRENT_SCANS="1", SUPEX_FAKE_SCAN_SECONDS="60")

    scans = [create_scan(ready_client, provider_id, name=f"扫描 {index}") for index in range(3)]

    # 一个跑起来，另外两个还在排队
    assert wait_for_status(ready_client, scans[0]["id"], {ScanStatus.RUNNING.value}) == "running"
    for scan in scans[1:]:
        assert status_of(ready_client, scan["id"]) == ScanStatus.QUEUED.value

    queue = ready_client.get("/api/v1/scans/queue").json()
    assert queue["running"] == 1
    assert queue["limit"] == 1
    assert queue["queued"] == 2
    assert queue["runner"] == "fake"
    assert queue["runnerReady"] is True


def test_aborting_the_running_scan_lets_the_next_one_start(
    ready_client: TestClient, provider_id: str, scan_config
) -> None:
    """跑完/终止一个之后，队列里的下一个会被认领 —— 而不是永远卡住。"""
    from app.scans.scheduler import get_scheduler

    scan_config(SUPEX_MAX_CONCURRENT_SCANS="1", SUPEX_FAKE_SCAN_SECONDS="60")
    first = create_scan(ready_client, provider_id, name="第一个")
    second = create_scan(ready_client, provider_id, name="第二个")
    wait_for_status(ready_client, first["id"], {ScanStatus.RUNNING.value})

    ready_client.post(f"/api/v1/scans/{first['id']}/abort")
    get_scheduler().tick()

    assert wait_for_status(ready_client, second["id"], {ScanStatus.RUNNING.value}) == "running"


def test_queue_endpoint_explains_why_a_scan_is_waiting(
    ready_client: TestClient, provider_id: str, scan_config
) -> None:
    """`/scans/queue` 要能回答"为什么一直排队"。

    没有它，界面只能显示"排队中"，而那看起来像坏了 ——
    真实原因通常是并发上限满了。
    """
    scan_config(SUPEX_MAX_CONCURRENT_SCANS="2", SUPEX_FAKE_SCAN_SECONDS="60")
    create_scan(ready_client, provider_id)

    queue = ready_client.get("/api/v1/scans/queue").json()

    assert queue["limit"] == 2
    assert {"running", "limit", "queued", "runner", "runnerReady", "runnerError"} <= set(queue)


def test_queue_endpoint_reports_an_unavailable_runner(
    ready_client: TestClient, scan_config
) -> None:
    """执行后端不可用时要**说出来**。

    否则用户点"开始扫描"只会看到扫描立刻失败，而不知道是环境缺东西
    （典型例子：strix 没装、或本机没有 Docker）。
    """
    scan_config(SUPEX_STRIX_COMMAND="definitely-not-a-real-binary-xyz", SUPEX_SCAN_RUNNER="strix")

    queue = ready_client.get("/api/v1/scans/queue").json()

    assert queue["runner"] == "strix"
    assert queue["runnerReady"] is False
    assert "找不到 strix" in (queue["runnerError"] or "")


# ══════════════════════════════════════════════════════════
# 5. 终止（任务要求 4）
# ══════════════════════════════════════════════════════════


def test_aborting_a_queued_scan_never_starts_it(
    ready_client: TestClient, provider_id: str, scan_config
) -> None:
    """排队中的直接置终态 —— 不用杀进程，调度器不会再认领它（它只认 queued）。"""
    scan_config(SUPEX_MAX_CONCURRENT_SCANS="1", SUPEX_FAKE_SCAN_SECONDS="60")
    blocker = create_scan(ready_client, provider_id, name="占位")
    queued = create_scan(ready_client, provider_id, name="排队的")
    wait_for_status(ready_client, blocker["id"], {ScanStatus.RUNNING.value})
    assert status_of(ready_client, queued["id"]) == ScanStatus.QUEUED.value

    response = ready_client.post(f"/api/v1/scans/{queued['id']}/abort")

    assert response.status_code == 200
    assert response.json()["status"] == ScanStatus.ABORTED.value
    assert response.json()["finishedAt"] is not None
    # 再 tick 一次也不会把它启动起来
    from app.scans.scheduler import get_scheduler

    get_scheduler().tick()
    assert status_of(ready_client, queued["id"]) == ScanStatus.ABORTED.value


def test_aborting_a_running_scan_kills_the_whole_process_tree(
    ready_client: TestClient, provider_id: str, scan_config, db_session: Session
) -> None:
    """★★ 「不留僵尸进程」—— 这一条是整个任务里最难用机器验证、也最值得验的。

    替身进程会起一个**孙进程**持续写心跳文件。只杀直接子进程的实现，
    会让孙进程活下来继续写 —— 而它从此再也无人认领
    （父进程没了之后会被系统收养）。

    做法：
      1. 等心跳文件出现（证明孙进程真的起来了）；
      2. 调终止接口（它会阻塞到**直接子进程**确认退出为止）；
      3. **在它返回之后**记一次心跳的修改时间；
      4. 等 1.2 秒（远大于 0.2 秒的写入间隔），修改时间**必须没变**。

    ⚠️ 第 3 步的位置是关键，第一版写错了：先记 mtime 再调接口，
    结果 `taskkill` 本身要花约 1 秒（它要依次枚举并杀掉整棵树），
    期间孙进程又写了一次 —— 于是断言在**功能正确**的情况下失败。
    实测确认过 `taskkill /PID x /T /F` 确实能杀掉孙进程
    （杀完之后 `Get-Process` 里已经找不到它）。
    """
    scan_config(SUPEX_MAX_CONCURRENT_SCANS="1", SUPEX_FAKE_SCAN_SECONDS="60")
    created = create_scan(ready_client, provider_id)
    wait_for_status(ready_client, created["id"], {ScanStatus.RUNNING.value})

    heartbeat = Path(SCAN_WORK_ROOT) / created["id"] / "grandchild.heartbeat"
    deadline = time.monotonic() + 10
    while not heartbeat.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert heartbeat.exists(), "替身的孙进程没有起来，这条测试就没测到东西"

    pid_before = scan_row(db_session, created["id"]).pid
    assert pid_before and is_alive(pid_before)

    response = ready_client.post(f"/api/v1/scans/{created['id']}/abort")
    assert response.status_code == 200
    assert response.json()["status"] == ScanStatus.ABORTED.value

    frozen = heartbeat.stat().st_mtime
    time.sleep(1.2)
    assert heartbeat.stat().st_mtime == frozen, (
        "终止返回之后心跳还在更新 —— 说明只杀了直接子进程，留下了再也无人认领的孙进程"
    )
    assert not is_alive(pid_before), "直接子进程也没有退出"


def test_aborting_a_finished_scan_is_409(ready_client: TestClient, provider_id: str) -> None:
    """已完成的扫描不能终止 → `409 SCAN_NOT_ABORTABLE`。

    前端 `abortScan` 对已完成扫描返回 false —— 它靠这个码区分。
    """
    created = create_scan(ready_client, provider_id)
    wait_for_status(ready_client, created["id"], {ScanStatus.COMPLETED.value}, timeout=30)

    response = ready_client.post(f"/api/v1/scans/{created['id']}/abort")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCAN_NOT_ABORTABLE"
    assert "已完成" in response.json()["error"]["message"]


def test_abort_does_not_overwrite_a_terminal_status(
    ready_client: TestClient, provider_id: str, scan_config
) -> None:
    """★ 终止之后，进程退出的收尾逻辑**不能**把状态改回 failed。

    进程退出码不会告诉我们"它是被杀死的还是自己失败的" ——
    如果我们杀掉它时得到的退出码被拿去定终态，
    "用户终止的"就变成了"失败的"，而那是两件不同的事。
    """
    scan_config(SUPEX_FAKE_SCAN_SECONDS="60")
    created = create_scan(ready_client, provider_id)
    wait_for_status(ready_client, created["id"], {ScanStatus.RUNNING.value})

    ready_client.post(f"/api/v1/scans/{created['id']}/abort")
    # 给调度器几次机会去错误地覆盖它
    from app.scans.scheduler import get_scheduler

    for _ in range(5):
        get_scheduler().tick()
        time.sleep(0.2)

    assert status_of(ready_client, created["id"]) == ScanStatus.ABORTED.value


def test_aborting_an_unknown_scan_is_404(ready_client: TestClient) -> None:
    assert ready_client.post("/api/v1/scans/scan_nope/abort").status_code == 404


# ══════════════════════════════════════════════════════════
# 6. 重跑（任务要求 5）
# ══════════════════════════════════════════════════════════


def test_rerun_creates_a_new_record_not_reusing_the_old_one(
    ready_client: TestClient, provider_id: str, db_session: Session
) -> None:
    """★ 重跑**建新记录**，不是把旧记录改回 running。

    扫描历史有价值（哪次发现了什么、修没修），
    把已完成的记录改回 running 会把那次历史抹掉。
    """
    original = create_scan(ready_client, provider_id, name="原始扫描")
    wait_for_status(ready_client, original["id"], {ScanStatus.COMPLETED.value}, timeout=30)
    original_finished = ready_client.get(f"/api/v1/scans/{original['id']}").json()["finishedAt"]

    response = ready_client.post(
        f"/api/v1/scans/{original['id']}/rerun", json={"authorization": dict(VALID_AUTH)}
    )

    assert response.status_code == 201
    rerun = response.json()
    assert rerun["id"] != original["id"]
    assert rerun["status"] == ScanStatus.QUEUED.value
    # 配置沿用
    assert rerun["target"] == original["target"]
    assert rerun["skills"] == original["skills"]
    assert rerun["providerId"] == original["providerId"]
    assert rerun["name"] == "原始扫描"
    # 新的 run 标识不是旧的
    assert rerun["runId"] is None

    # **旧记录一点没被动过** —— 这才是"历史有价值"的意思
    after = ready_client.get(f"/api/v1/scans/{original['id']}").json()
    assert after["status"] == ScanStatus.COMPLETED.value
    assert after["finishedAt"] == original_finished


def test_rerun_records_its_own_authorization(
    ready_client: TestClient, provider_id: str, db_session: Session
) -> None:
    """重跑要有**它自己**的授权留痕，而不是继承旧那条。"""
    original = create_scan(ready_client, provider_id)

    rerun = ready_client.post(
        f"/api/v1/scans/{original['id']}/rerun",
        json={
            "authorization": {
                "confirmed": True,
                "statement": AUTH_STATEMENT,
                "confirmedAt": "2026-09-23T15:30:42.123Z",
            }
        },
    ).json()

    row = scan_row(db_session, rerun["id"])
    assert row.authorization_statement == AUTH_STATEMENT
    assert row.authorization_received_at is not None
    # 这次客户端传了 confirmedAt，就应该存下来
    assert row.authorization_confirmed_at is not None


def test_rerun_rejects_a_deleted_provider(
    ready_client: TestClient, provider_id: str
) -> None:
    """provider 被删了就不能重跑 —— 报 400 并说明原因，而不是起一个必然失败的扫描。

    注意要先等原扫描**结束**再删 provider：它还在排队/运行时，
    删除会被 `PROVIDER_IN_USE` 拦住（那是另一条规则，见 test_providers）。
    """
    ready_client.post(
        "/api/v1/providers",
        json={
            "label": "另一个",
            "model": "x",
            "kind": "cloud",
            "apiKey": "sk-other-1234567890",
            "baseUrl": "https://api.openai.com/v1",
            "isDefault": True,
        },
    )
    original = create_scan(ready_client, provider_id)
    wait_for_status(ready_client, original["id"], {ScanStatus.COMPLETED.value}, timeout=30)
    assert ready_client.delete(f"/api/v1/providers/{provider_id}").status_code == 204

    response = ready_client.post(
        f"/api/v1/scans/{original['id']}/rerun", json={"authorization": dict(VALID_AUTH)}
    )

    assert response.status_code == 400
    assert "provider" in response.json()["error"]["message"]


# ══════════════════════════════════════════════════════════
# 7. 列表筛选与排序
# ══════════════════════════════════════════════════════════


def test_list_filters_by_status(ready_client: TestClient, db_session: Session) -> None:
    """按状态筛选。

    前置数据**直接插库**而不是走 `POST /scans`：走接口的话
    `_nudge_scheduler` 会立刻把它启动起来，于是"queued 的筛选"永远测不到。
    这里要的是一个静止的、状态已知的数据集。
    """
    make_scan(db_session, status=ScanStatus.QUEUED.value, scan_id="scan_fixture_queued")
    make_scan(db_session, status=ScanStatus.COMPLETED.value, scan_id="scan_fixture_done")

    queued = ready_client.get("/api/v1/scans?status=queued").json()
    completed = ready_client.get("/api/v1/scans?status=completed").json()

    assert [scan["id"] for scan in queued] == ["scan_fixture_queued"]
    assert [scan["id"] for scan in completed] == ["scan_fixture_done"]


def test_list_rejects_an_unknown_status(ready_client: TestClient) -> None:
    """写错的筛选值要报错，而不是静默返回全部。

    静默忽略会让前端以为筛选生效了 —— 用户看到的是一堆不符合条件的记录，
    却没有任何提示说明为什么。
    """
    response = ready_client.get("/api/v1/scans?status=nonsense")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_list_searches_name_and_target(ready_client: TestClient, provider_id: str) -> None:
    create_scan(ready_client, provider_id, name="生产环境", target="https://prod.example.com")
    create_scan(ready_client, provider_id, name="测试环境", target="https://stage.example.com")

    by_name = ready_client.get("/api/v1/scans?q=生产").json()
    by_target = ready_client.get("/api/v1/scans?q=stage").json()

    assert [scan["name"] for scan in by_name] == ["生产环境"]
    assert [scan["name"] for scan in by_target] == ["测试环境"]


def test_list_sorts_by_duration_with_nulls_last(
    ready_client: TestClient, provider_id: str, db_session: Session, scan_config
) -> None:
    """★ 按耗时排序时，`durationSec` 为 null 的必须**垫底**。

    PostgreSQL 默认 `DESC` 时 NULL 排最前 —— 那会让"正在跑的"
    排到"最慢的"位置，正是契约 §2.1 用 ⚠️ 警告过的那个现象。

    前置数据一半插库（已完成，耗时已知）、一半走接口（真的跑起来，
    `durationSec` 是 null）—— 这样两边的 null 语义都是真的。
    """
    scan_config(SUPEX_MAX_CONCURRENT_SCANS="1", SUPEX_FAKE_SCAN_SECONDS="60")
    # ⚠️ 必须复用 fixture 建的 provider：`make_scan` 默认会**新建**一个
    # `prov_deepseek` 并把它设为默认，而库里已经有一个默认了 ——
    # 撞上部分唯一索引 `uq_provider_accounts_default`。
    make_scan(
        db_session,
        provider_id=provider_id,
        status=ScanStatus.COMPLETED.value,
        scan_id="scan_fixture_done",
    )
    running = create_scan(ready_client, provider_id, name="在跑的")
    wait_for_status(ready_client, running["id"], {ScanStatus.RUNNING.value})

    listed = ready_client.get("/api/v1/scans?sort=duration&dir=desc").json()

    assert listed[0]["id"] == "scan_fixture_done"
    assert listed[-1]["id"] == running["id"]
    assert listed[-1]["durationSec"] is None


# ══════════════════════════════════════════════════════════
# 8. 孤儿回收（服务重启之后）
# ══════════════════════════════════════════════════════════


def test_orphan_with_dead_pid_becomes_failed(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 服务重启后，`running` 但进程已经没了的扫描 → `failed`，原因写"结果不可知"。

    **不猜。** 报 `completed` 是编造（我们不知道它跑完没有），
    报 `aborted` 也不对（用户没终止过它）。用户看到一条明确的、
    能解释的记录，比看到一条看起来正常但结论可能是错的记录要好。

    这里**直接往库里插**而不是走 `POST /scans`：走接口的话，
    `_nudge_scheduler` 会立刻把它启动起来，于是它就不是"孤儿"了 ——
    而要造的正是一个"上一次运行留下的、本进程不知道的行"。
    """
    from app.scans.scheduler import get_scheduler

    scan = make_scan(db_session, status=ScanStatus.RUNNING.value)
    scan.pid = 999_999  # 几乎不可能存在的 pid
    db_session.commit()

    recovered = get_scheduler().recover_orphans()

    assert recovered == 1
    body = ready_client.get(f"/api/v1/scans/{scan.id}").json()
    assert body["status"] == ScanStatus.FAILED.value
    assert "不可知" in body["failureReason"]
    assert body["finishedAt"] is not None
    assert body["durationSec"] is not None


def test_orphan_with_live_pid_is_terminated(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 进程还活着的孤儿要**被终止**，置 `aborted`。

    为什么不是"继续跟踪它"：我们丢了 Popen 句柄，拿不到退出码、
    也不知道它跑到哪了。而一个**没人能终止的攻击进程**比一次白跑的扫描
    危险得多 —— 用户以为服务停了，其实还在替他打目标。
    """
    from app.scans.scheduler import get_scheduler

    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        **popen_kwargs(),
    )
    try:
        scan = make_scan(db_session, status=ScanStatus.RUNNING.value)
        scan.pid = sleeper.pid
        db_session.commit()
        assert is_alive(sleeper.pid)

        get_scheduler().recover_orphans()

        body = ready_client.get(f"/api/v1/scans/{scan.id}").json()
        assert body["status"] == ScanStatus.ABORTED.value
        assert "重启" in body["failureReason"]
        assert not is_alive(sleeper.pid), "孤儿进程没有被终止"
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


def test_recovery_leaves_tracked_scans_alone(
    ready_client: TestClient, provider_id: str, scan_config
) -> None:
    """★ 回归测试：孤儿回收**不能动本进程正在跟踪的扫描**。

    早先的实现把所有 `running` 的扫描都当成"上一次运行的遗留"，
    于是它会对自己刚启动的扫描下手 —— 明明跑得好好的，被自己杀掉了。
    这个 bug 是写"没有孤儿时应该什么都不做"那条测试时抓到的。
    """
    from app.scans.scheduler import get_scheduler

    scan_config(SUPEX_MAX_CONCURRENT_SCANS="1", SUPEX_FAKE_SCAN_SECONDS="30")
    created = create_scan(ready_client, provider_id)
    wait_for_status(ready_client, created["id"], {ScanStatus.RUNNING.value})

    assert get_scheduler().recover_orphans() == 0
    assert status_of(ready_client, created["id"]) == ScanStatus.RUNNING.value


def test_no_orphans_means_nothing_happens(db_session: Session) -> None:
    """没有孤儿时回收什么都不做 —— 尤其**不会**去动排队中的扫描。"""
    from app.scans.scheduler import get_scheduler

    make_scan(db_session, status=ScanStatus.QUEUED.value)

    assert get_scheduler().recover_orphans() == 0


# ══════════════════════════════════════════════════════════
# 汇总列（findings / agentCount）
# ══════════════════════════════════════════════════════════


def test_finalize_refreshes_rollup_instead_of_keeping_creation_defaults(
    db_session: Session,
) -> None:
    """★ 收尾时把汇总列刷成**真的**。

    `Scan.findings` / `Scan.agent_count` 是冗余列：建行时写的是 `{}` / `0`，
    之后必须有人负责刷新 —— 否则扫描列表永远显示"未发现漏洞"、"0 个智能体"，
    而漏洞页里明摆着有两条。两个页面互相打脸，且列表那一侧读起来像
    "这次扫描什么都没扫到"。

    这条测试盯的就是"没人调用"：`refresh_scan_rollup()` 早就写好了，
    但**一个调用点都没有**，所以它对结果毫无影响。
    """
    from app.scans import service

    # 工厂故意写了一份"看起来合理"的汇总（high: 1 / 1 个 agent），
    # 而库里既没有漏洞、也没有 agent 行 —— 刷新之后它必须变回真实的空。
    scan = make_scan(db_session, status=ScanStatus.RUNNING.value)
    assert scan.findings == {"high": 1}

    service.finalize(db_session, scan, exit_code=0)
    db_session.refresh(scan)

    assert scan.status == ScanStatus.COMPLETED.value
    assert scan.findings == {}, "库里一条漏洞都没有，汇总就不能说有一条高危"
    # 编排器 agent 是收尾事件带出来的，所以是 1 而不是 0
    assert scan.agent_count == 1


def test_rollup_counts_findings_by_severity_and_skips_false_positives(
    db_session: Session,
) -> None:
    """汇总列的 `findings` 是**按严重度分档**的计数，且**排除误报**。

    排除误报是刻意的口径（`findings_counts` 的 docstring）：
    已经判定为误报的东西还挂在扫描结果里，会让"这次扫描发现了 3 个问题"
    永远降不下来。分档而不是只给总数，是因为列表上要显示
    "高危 1 · 中危 1" 这样的小标签。
    """
    from app.core.timeutil import now_utc
    from app.findings.models import Finding
    from app.scans import service
    from tests.factories import make_agent

    scan = make_scan(db_session, status=ScanStatus.RUNNING.value)
    make_agent(db_session, scan.id)
    make_agent(db_session, scan.id)
    for index, (severity, status) in enumerate(
        [("high", "new"), ("medium", "confirmed"), ("low", "false_positive")]
    ):
        db_session.add(
            Finding(
                id=f"find_rollup_{index}",
                scan_id=scan.id,
                source_ref=f"vuln-rollup-{index}",
                severity=severity,
                category="misconfiguration",
                title=f"第 {index} 条",
                target=scan.target,
                verification="verified",
                status=status,
                discovered_at=now_utc(),
            )
        )
    db_session.commit()

    service.finalize(db_session, scan, exit_code=2)
    db_session.refresh(scan)

    assert scan.findings == {"high": 1, "medium": 1}, "误报不该被算进去，没有的档位不该出现"
    assert scan.agent_count == 3, "两个扫描 agent + 一个编排器 agent"


def test_marking_a_finding_as_false_positive_shrinks_the_scan_rollup(
    db_session: Session,
) -> None:
    """★ 漏洞状态一变，扫描列表上的计数就要跟着变。

    这是冗余列最容易腐烂的地方：状态流转走的是 findings 域，
    而汇总列挂在 scans 域 —— 两个域之间没有数据库层面的联动，
    只能靠 `change_status` 自己回头刷一次。

    不刷的表现很具体：用户把一条漏洞标成误报，回来一看扫描列表
    还是"发现 1 条高危"，而漏洞页已经变成 0 条。
    """
    from app.auth.deps import Identity
    from app.core.timeutil import now_utc
    from app.core.vocab import FindingStatus
    from app.findings import service as findings_service
    from app.findings.models import Finding
    from app.scans import service

    scan = make_scan(db_session, status=ScanStatus.COMPLETED.value)
    finding = Finding(
        id="find_rollup_fp",
        scan_id=scan.id,
        source_ref="vuln-rollup-fp",
        severity="high",
        category="misconfiguration",
        title="会被标成误报的那条",
        target=scan.target,
        verification="verified",
        status=FindingStatus.NEW.value,
        discovered_at=now_utc(),
    )
    db_session.add(finding)
    db_session.commit()

    service.refresh_scan_rollup(db_session, scan)
    db_session.refresh(scan)
    assert scan.findings == {"high": 1}

    findings_service.change_status(
        db_session,
        ids=[finding.id],
        target=FindingStatus.FALSE_POSITIVE.value,
        note="测试：规则误判，不是真实漏洞",
        identity=Identity(kind="session", label="s", scopes=frozenset({"read", "scan", "admin"})),
    )
    db_session.refresh(scan)

    assert scan.findings == {}, "标成误报之后，扫描列表不能再把它算成一条高危"
