"""批量开始 / 取消（CTF 页重做第 3 步）。

## 这个文件在防什么

比赛现场会一次开十几道题。批量端点有三件事必须成立，而且错了都不报错：

1. **部分成功**：一条失败不能把前面已受理的也回滚 —— 那些 worker 已经在
   队列里了，数据库却当作没发生，会出现"界面说没开始、实际在跑"。
2. **去重**：前端"全选"很容易把同一道题带两遍，重复入队等于同一道题
   被跑两次（双倍成本）。
3. **逐条结果**：整体 4xx 会让前端丢掉"哪几道没起来"，而那正是用户
   唯一需要知道的事。

⚠️ `_never_enqueue` 把 `service._enqueue` 换成记录器 —— 绝不让真 worker
   起来（与 `test_ir_task_router.py` 同一个理由，那里有完整说明）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.vocab import KernelChallengeStatus
from app.kernel.models import KernelChallenge

API = "/api/v1"


@pytest.fixture(autouse=True)
def _never_enqueue(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []

    def _record(challenge_id: str, *, attempt_id: str | None = None) -> None:
        calls.append((challenge_id, attempt_id))

    monkeypatch.setattr("app.kernel.service._enqueue", _record)
    return calls


def _make(db: Session, template: KernelChallenge, **overrides) -> KernelChallenge:
    from app.core.ids import new_id

    kwargs = {
        "id": new_id("ctf2chal"),
        "competition_id": template.competition_id,
        "remote_id": new_id("r"),
        "stage_id": None,
        "title": "批量题",
        "category": "web",
        "description": None,
        "attachments": [],
        "score": 100,
        "status": KernelChallengeStatus.UNSOLVED.value,
        "candidate_flag": None,
        "confirmed_flag": None,
        "wrong_submission_count": 0,
        "max_wrong_submissions": 3,
        "candidate_count": 0,
        "sandbox_profile": template.sandbox_profile,
    }
    kwargs.update(overrides)
    row = KernelChallenge(**kwargs)
    db.add(row)
    db.commit()
    return row


def _batch(client: TestClient, action: str, ids: list[str]):
    return client.post(
        f"{API}/kernel/challenges/batch", json={"action": action, "challengeIds": ids}
    )


def test_solve_starts_every_selected_challenge(
    ready_client: TestClient,
    db_session: Session,
    ctf2_challenge: KernelChallenge,
    provider_id: str,
    _never_enqueue: list[tuple],
) -> None:
    second = _make(db_session, ctf2_challenge, title="第二道")

    resp = _batch(ready_client, "solve", [ctf2_challenge.id, second.id])
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert (body["requested"], body["succeeded"], body["failed"]) == (2, 2, 0)
    assert {item["id"] for item in body["results"]} == {ctf2_challenge.id, second.id}
    assert all(item["ok"] for item in body["results"])
    # 真的走到了入队 —— 只看 200 可能来自别处的提前返回
    assert {call[0] for call in _never_enqueue} == {ctf2_challenge.id, second.id}


def test_duplicate_ids_are_collapsed(
    ready_client: TestClient,
    ctf2_challenge: KernelChallenge,
    provider_id: str,
    _never_enqueue: list[tuple],
) -> None:
    """★★ 同一道题带两遍只处理一次 —— 重复入队等于跑两遍，双倍成本。"""
    resp = _batch(ready_client, "solve", [ctf2_challenge.id, ctf2_challenge.id])

    body = resp.json()
    assert body["requested"] == 1
    assert len(_never_enqueue) == 1


def test_one_failure_does_not_block_the_rest(
    ready_client: TestClient,
    db_session: Session,
    ctf2_challenge: KernelChallenge,
    provider_id: str,
    _never_enqueue: list[tuple],
) -> None:
    """★★ 部分成功：已解出的那道报失败，另一道照常受理。"""
    solved = _make(
        db_session,
        ctf2_challenge,
        title="已经解出的题",
        status=KernelChallengeStatus.SOLVED.value,
        confirmed_flag="flag{done}",
    )

    resp = _batch(ready_client, "solve", [solved.id, ctf2_challenge.id])
    assert resp.status_code == 200, resp.text

    body = resp.json()
    by_id = {item["id"]: item for item in body["results"]}
    assert by_id[solved.id]["ok"] is False
    assert by_id[solved.id]["message"], "失败必须带一句给人看的话"
    assert by_id[ctf2_challenge.id]["ok"] is True
    assert body["succeeded"] == 1 and body["failed"] == 1
    # 失败的那条**没有**被入队
    assert [call[0] for call in _never_enqueue] == [ctf2_challenge.id]


def test_cancel_batch_moves_them_back_to_unsolved(
    ready_client: TestClient,
    db_session: Session,
    ctf2_challenge: KernelChallenge,
) -> None:
    running = _make(
        db_session, ctf2_challenge, title="正在跑", status=KernelChallengeStatus.SOLVING.value
    )

    resp = _batch(ready_client, "cancel", [running.id])
    assert resp.status_code == 200, resp.text
    assert resp.json()["succeeded"] == 1

    db_session.expire_all()
    assert db_session.get(KernelChallenge, running.id).status == KernelChallengeStatus.UNSOLVED.value


def test_unknown_id_is_reported_not_fatal(ready_client: TestClient) -> None:
    resp = _batch(ready_client, "solve", ["ctf2chal_does_not_exist"])

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["failed"] == 1
    assert body["results"][0]["ok"] is False
