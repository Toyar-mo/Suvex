"""比赛平台同步（`service.sync_competition`）的接口级测试。

## 这个功能为什么需要单独的测试

CTF 页原先只显示 CTF2 老题库（407 道 BUUCTF 等），比赛平台上的题
（slab-match 协议，实测 5 道）从来没进过这张表。`sync_competition`
是唯一把它们拉进来的路径，它一旦坏了，页面会静默地只剩老题 ——
用户看不出"比赛题没同步"，只会觉得"平台没出题"。

所以这里用**假驱动**把整条链路走通，不依赖网络、不依赖真实凭据。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kernel.models import (
    KernelAccount,
    KernelChallenge,
    KernelCompetition,
    KernelPlatform,
)
from app.kernel.platform import RemoteChallenge, RemoteEnvironment, SubmitOutcome

API = "/api/v1"


class _FakeDriver:
    """一个不碰网络的比赛平台驱动，返回 3 道题（2 道未解、1 道已解）。"""

    answer_max_length = 256
    supports_environment = False

    def __init__(self) -> None:
        self.fetched_details: list[dict] = []

    async def fetch_challenges(self, *, known_ids: set[str]):
        return [
            RemoteChallenge(remote_id="501", title="WEB-题", category="WEB", handle={"exerciseId": 501}),
            RemoteChallenge(remote_id="502", title="PWN-题", category="PWN", handle={"exerciseId": 502}),
            RemoteChallenge(remote_id="503", title="MISC-题", category="MISC", handle={"exerciseId": 503}, solved=True),
        ]

    async def fetch_challenge(self, handle):
        eid = handle["exerciseId"]
        self.fetched_details.append(handle)
        return RemoteChallenge(
            remote_id=str(eid),
            title=f"题{eid}",
            category={501: "WEB", 502: "PWN", 503: "MISC"}.get(eid, "MISC"),
            description=f"{eid} 的题面",
            score=100,
            handle=handle,
            solved=(eid == 503),
        )

    async def submit(self, handle, answer):  # pragma: no cover - 本测试不提交
        return SubmitOutcome(accepted=True, is_solved=False)

    async def read_environment(self, handle):  # pragma: no cover
        return RemoteEnvironment(ready=False)

    async def start_environment(self, handle):  # pragma: no cover
        return RemoteEnvironment(ready=True)

    async def stop_environment(self, handle):  # pragma: no cover
        return None


def _bind_account(db_session: Session) -> None:
    """造一个 active 账号（有 token、有 platform_id 指向配置平台）。

    `sync_competition` 只要求"有 active 账号 + resolve_driver 能造出驱动"，
    而 resolve_driver 会被本测试 monkeypatch 掉，所以这里只要账号存在、
    token 能解就行。token_encrypted 用 service 的加密入口现造。
    """
    from app.core.crypto import encrypt_secret

    # 平台行：来源名从这里来（用户接入一个平台后，CTF 页该显示它的名字）。
    db_session.add(
        KernelPlatform(
            id="knplat_sync",
            slug="pro-dasctf-com",
            display_name="DASCTF Pro（slab-match）",
            base_url="https://example.test/slab-match/api/v1/agent",
            driver="generic",
            status="verified",
        )
    )
    acct = KernelAccount(
        id="ctf2acc_sync",
        platform_id="knplat_sync",
        label="sync-acc",
        token_encrypted=encrypt_secret("fake-access-key"),
        token_hint="****",
        status="active",
    )
    db_session.add(acct)
    db_session.commit()


def test_sync_competition_inserts_all_remote_tasks(
    ready_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次同步：3 道题全部落库，落在一个 remote_id='slab-match' 的批次里。"""
    _bind_account(db_session)
    fake = _FakeDriver()
    monkeypatch.setattr("app.chat.competition.resolve_driver", lambda db: fake)

    resp = ready_client.post(f"{API}/kernel/competition/sync")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["competitions"] == 1
    assert body["challengesCreated"] == 3
    assert body["challengesUpdated"] == 0
    assert body["errors"] == []
    # 逐题取了详情（列表端点没题面/分值）
    assert len(fake.fetched_details) == 3

    # 库里：一个 slab-match 批次，挂 3 道题
    comp = db_session.execute(
        select(KernelCompetition).where(KernelCompetition.remote_id == "slab-match")
    ).scalars().first()
    assert comp is not None
    assert comp.challenge_count == 3
    rows = db_session.execute(
        select(KernelChallenge).where(KernelChallenge.competition_id == comp.id)
    ).scalars().all()
    assert {r.remote_id for r in rows} == {"501", "502", "503"}


def test_sync_competition_is_idempotent(
    ready_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """再同步一次：不重复建题，全部走"更新"。"""
    _bind_account(db_session)
    fake = _FakeDriver()
    monkeypatch.setattr("app.chat.competition.resolve_driver", lambda db: fake)

    assert ready_client.post(f"{API}/kernel/competition/sync").status_code == 200
    second = ready_client.post(f"{API}/kernel/competition/sync")
    assert second.status_code == 200
    body = second.json()
    assert body["challengesCreated"] == 0
    assert body["challengesUpdated"] == 3

    # 库里仍然只有一个 slab-match 批次、3 道题
    comps = db_session.execute(
        select(KernelCompetition).where(KernelCompetition.remote_id == "slab-match")
    ).scalars().all()
    assert len(comps) == 1
    rows = db_session.execute(
        select(KernelChallenge).where(KernelChallenge.competition_id == comps[0].id)
    ).scalars().all()
    assert len(rows) == 3


def test_synced_tasks_report_their_true_source_on_the_api(
    ready_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 端到端：同步后从题目接口看到来源是「羊城杯 …」（批次名），不是别的平台。"""
    _bind_account(db_session)
    monkeypatch.setattr("app.chat.competition.resolve_driver", lambda db: _FakeDriver())

    assert ready_client.post(f"{API}/kernel/competition/sync").status_code == 200

    comp = db_session.execute(
        select(KernelCompetition).where(KernelCompetition.remote_id == "slab-match")
    ).scalars().first()
    assert comp is not None
    ch = db_session.execute(
        select(KernelChallenge).where(KernelChallenge.competition_id == comp.id)
    ).scalars().first()
    assert ch is not None

    body = ready_client.get(f"{API}/kernel/challenges/{ch.id}").json()
    # 来源 = 平台自己的名字（用户接入新平台后，CTF 页就该显示那个平台名）
    assert body["platformName"] == "DASCTF Pro（slab-match）"
    assert body["platformSlug"].startswith("src-")


def test_sync_competition_without_account_is_409(ready_client: TestClient) -> None:
    """没绑定账号 → 409 + 可行动提示，而不是 500。"""
    resp = ready_client.post(f"{API}/kernel/competition/sync")
    assert resp.status_code == 409, resp.text
    # 错误信封是 {"error": {"code": ..., "message": ...}}
    assert resp.json()["error"]["code"] in ("CTF2_NOT_BOUND", "COMPETITION_NOT_BOUND")


def test_activating_a_platform_kicks_off_a_sync(
    ready_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 用户要的效果：新接入一个平台并启用后，CTF 页立刻开始拉题。

    这里验证的是"接线"：`activate_platform` 端点启用成功后，后台线程真的
    调了 `service.sync_competition`（且用的是这个平台自己的名字）。
    真实网络那一步由 `resolve_driver` 的假驱动替身完成。
    """
    import threading

    # 造一个已经"识别完（verified）"的平台行，省掉识别流程。
    db_session.add(
        KernelPlatform(
            id="knplat_activate",
            slug="pro-dasctf-com",
            display_name="DASCTF Pro（slab-match）",
            base_url="https://example.test/slab-match/api/v1/agent",
            driver="generic",
            status="verified",
        )
    )
    db_session.commit()

    calls: list[dict] = []

    # 本测试只验证"启用 → 触发同步"这条接线，不验证凭据在线校验
    # （那是 platforms.verify_spec 的事，需要真网络）。把 activate 本身
    # 换成直接返回账号，把焦点收窄到我们改的那一段。
    from app.kernel.models import KernelAccount

    def _fake_activate(db, *, platform_id, access_key, label=""):
        acct = db.execute(
            __import__("sqlalchemy").select(KernelAccount).where(KernelAccount.status == "active")
        ).scalars().first()
        if acct is None:
            acct = KernelAccount(id="knacc_test", status="active")
            db.add(acct)
        acct.label = label or "比赛号"
        acct.platform_id = platform_id
        db.flush()
        return acct

    monkeypatch.setattr("app.kernel.router.platforms.activate_platform", _fake_activate)

    def _fake_sync(db, *, competition_name="x"):
        from app.kernel.service import SyncResult

        calls.append({"name": competition_name})
        return SyncResult(competitions=1, challenges_created=5)

    # 端点里的线程体 import 的是 `service.sync_competition`，patch 它即可。
    monkeypatch.setattr("app.kernel.router.service.sync_competition", _fake_sync)
    # 让线程里的 db 会话也走测试库（get_session_factory 已被 conftest 指向测试库）。
    # 用一个小屏障等线程把 calls 填上，避免竞态。
    done = threading.Event()
    monkeypatch.setattr(
        "app.kernel.router.threading.Thread",
        lambda target, name=None, daemon=None: _FakeThread(target, done),
    )

    resp = ready_client.post(
        f"{API}/kernel/platforms/knplat_activate/activate",
        json={"accessKey": "ak_test_key", "label": "比赛号"},
    )
    assert resp.status_code == 200, resp.text
    assert done.wait(timeout=5), "后台同步线程没有被触发"
    assert calls == [{"name": "DASCTF Pro（slab-match）"}]


class _FakeThread:
    """同步执行 target（在本线程里跑），跑完 set event —— 让断言确定。"""

    def __init__(self, target, done):
        self._target = target
        self._done = done

    def start(self):
        try:
            self._target()
        finally:
            self._done.set()
