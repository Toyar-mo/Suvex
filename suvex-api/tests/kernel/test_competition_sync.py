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

from app.kernel.models import KernelAccount, KernelChallenge, KernelCompetition
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
    # 来源 = 比赛平台批次的名字（默认「羊城杯 AI Agent 解题赛」）
    assert body["platformName"] == "羊城杯 AI Agent 解题赛"
    assert body["platformSlug"].startswith("src-")


def test_sync_competition_without_account_is_409(ready_client: TestClient) -> None:
    """没绑定账号 → 409 + 可行动提示，而不是 500。"""
    resp = ready_client.post(f"{API}/kernel/competition/sync")
    assert resp.status_code == 409, resp.text
    # 错误信封是 {"error": {"code": ..., "message": ...}}
    assert resp.json()["error"]["code"] in ("CTF2_NOT_BOUND", "COMPETITION_NOT_BOUND")
