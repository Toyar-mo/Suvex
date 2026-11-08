"""看板「查看」→ 这道题的那个对话（CTF 页重做第 2 步）。

## 这个文件在防什么

「查看」是一个**回访式**入口：点它应当回到这道题的那条时间线，而不是
每点一次多一个空会话。三条判据都不会报错，只会让人慢慢失去信任：

1. 同一个题目**再点一次必须拿到同一个会话** —— 否则侧栏会被空对话冲垮，
   而真正的历史还不一定在里面。
2. 已归档的那个会话要被**捞回来并取消归档**：归档只表示"从侧栏消失"，
   不代表这段记录不该再打开。
3. 详情里要带**题目投影**（标题/状态/分值/候选）—— 题目条靠它渲染；
   题目被删时是 `null`，前端据此说"题目已不存在"，而不是把条藏掉。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.crypto import encrypt_secret, mask_secret
from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.core.vocab import KernelChallengeStatus, KernelSandboxProfile
from app.kernel.models import KernelAccount, KernelChallenge, KernelCompetition

API = "/api/v1"


@pytest.fixture()
def challenge(db_session: Session) -> KernelChallenge:
    account = KernelAccount(
        id=new_id("ctf2acc"),
        label="测试号",
        token_encrypted=encrypt_secret("pat_test_123"),
        token_hint=mask_secret("pat_test_123"),
        status="active",
        remote_user_id="u_1001",
        remote_username="tester",
        scopes=[],
        last_synced_at=now_utc(),
    )
    db_session.add(account)
    competition = KernelCompetition(
        id=new_id("ctf2comp"),
        account_id=account.id,
        remote_id="1234",
        kind="practice",
        name="2026 秋季练习赛",
        remote_status="ongoing",
        challenge_count=0,
        synced_at=now_utc(),
    )
    db_session.add(competition)
    # ★ 先落库再插题：三张表之间只有裸 FK 列、没有 relationship，
    #   一次 flush 里的插入顺序不保证 —— 实测曾因此撞 FK 约束。
    db_session.flush()
    row = KernelChallenge(
        id=new_id("ctf2chal"),
        competition_id=competition.id,
        remote_id="9001",
        stage_id=None,
        title="[DASCTF] 示例题",
        category="crypto",
        description=None,
        attachments=[],
        score=190,
        status=KernelChallengeStatus.CANDIDATE_READY.value,
        candidate_flag="DASCTF{maybe}",
        confirmed_flag=None,
        wrong_submission_count=0,
        max_wrong_submissions=3,
        candidate_count=1,
        sandbox_profile=KernelSandboxProfile.BASE.value,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _open(client: TestClient, challenge_id: str):
    return client.post(f"{API}/chat/sessions/for-challenge/{challenge_id}")


def test_opens_a_session_titled_after_the_challenge(
    ready_client: TestClient, challenge: KernelChallenge
) -> None:
    resp = _open(ready_client, challenge.id)
    assert resp.status_code == 200, resp.text

    payload = resp.json()
    assert payload["title"] == challenge.title
    assert payload["challengeId"] == challenge.id


def test_second_open_returns_the_same_session(
    ready_client: TestClient, challenge: KernelChallenge
) -> None:
    """★★ 幂等：连点两次不该多出一个空对话。"""
    first = _open(ready_client, challenge.id).json()
    second = _open(ready_client, challenge.id).json()

    assert first["id"] == second["id"]
    listed = ready_client.get(f"{API}/chat/sessions").json()
    assert sum(1 for s in listed if s["challengeId"] == challenge.id) == 1


def test_unknown_challenge_is_404(ready_client: TestClient) -> None:
    """不存在的题目 → 404，而不是建一个挂空的会话。"""
    assert _open(ready_client, "ctf2chal_does_not_exist").status_code == 404


def test_archived_session_is_reopened(
    ready_client: TestClient, challenge: KernelChallenge
) -> None:
    """★ 归档的会话要被捞回来（并取消归档）—— 历史不该被藏起来。"""
    session_id = _open(ready_client, challenge.id).json()["id"]
    assert ready_client.delete(f"{API}/chat/sessions/{session_id}").status_code == 200

    reopened = _open(ready_client, challenge.id).json()

    assert reopened["id"] == session_id
    assert reopened["archived"] is False


def test_detail_carries_the_challenge_projection(
    ready_client: TestClient, challenge: KernelChallenge
) -> None:
    """详情带题目投影：标题、状态、分值、候选 flag —— 题目条就靠这几项。"""
    session_id = _open(ready_client, challenge.id).json()["id"]

    detail = ready_client.get(f"{API}/chat/sessions/{session_id}").json()

    ref = detail["challenge"]
    assert ref is not None
    assert ref["id"] == challenge.id
    assert ref["title"] == challenge.title
    assert ref["status"] == KernelChallengeStatus.CANDIDATE_READY.value
    assert ref["score"] == 190
    assert ref["candidateFlag"] == "DASCTF{maybe}"


def test_plain_session_has_no_challenge_ref(ready_client: TestClient) -> None:
    """普通对话的 `challenge` 是 null —— 题目条不该出现在每个会话上。"""
    created = ready_client.post(f"{API}/chat/sessions", json={"title": "随便聊聊"}).json()

    detail = ready_client.get(f"{API}/chat/sessions/{created['id']}").json()

    assert detail["challenge"] is None
