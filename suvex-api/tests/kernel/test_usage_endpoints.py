"""CTF2 题解与 token 统计的**端点**层（v0.36）。

## 为什么单独一个文件，而不是塞进 `test_usage_and_writeup.py`

那边测 service（落库与聚合口径）；这边测**路由**：状态码、响应字段名、
鉴权、以及"题不存在时是 404 还是 409"。

## ★ 三种"没有题解"必须给三种话

`read_writeup` 对三种成因都给 409 `WRITEUP_UNAVAILABLE`，但 `message`
不同 —— 因为**下一步动作不同**：

  · 题不存在 → 404（`get_challenge_or_404` 抛）；
  · 题存在但从没解过 → "点「解题」跑一次"；
  · 解过但题解没落库（v0.36 之前解的）→ "重新解一次会生成"。

分开三个码只会让前端写三段其实一样的分支。所以这里断言的是
**message 里那句指路的话**，而不是码本身。

## 契约字段名逐字核对

响应是 camelCase（`CamelModel`）。字段名写错（比如 `recorded_calls`）
不会让任何既有用例变红 —— 所以这里显式断言键存在。

## 鉴权怎么发

与全站一致：`ready_client` 走真实的 setup → 登录 → Cookie 下发。
错误体形状是 `{"error": {"code", "message"}}`（见 `ApiError` 的处理器）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.kernel import runner, service
from app.kernel.models import Ctf2SolveAttempt

API = "/api/v1"


def _attempt(db: Session, challenge_id: str):
    from app.core.vocab import Ctf2SubmitMode

    row = Ctf2SolveAttempt(
        id=new_id("ctf2try"),
        challenge_id=challenge_id,
        mode=Ctf2SubmitMode.SAFE.value,
        max_turns=40,
        sandbox_profile="ctf2-sandbox",
    )
    db.add(row)
    db.commit()
    return row


def _outcome(*, usages=None, transcript="", turns=3):
    return SimpleNamespace(
        usages=usages if usages is not None else [],
        transcript=transcript,
        turns=turns,
        candidates=[],
        error=None,
    )


# ══════════════════════════════════════════════════════════
# 一、GET /ctf2/challenges/{id}/writeup
# ══════════════════════════════════════════════════════════


def test_writeup_is_404_for_an_unknown_challenge(ready_client: TestClient) -> None:
    resp = ready_client.get(f"{API}/ctf2/challenges/chal_nope/writeup")

    assert resp.status_code == 404


def test_writeup_is_409_with_a_next_step_when_never_solved(
    ready_client: TestClient, ctf2_challenge
) -> None:
    """★ 从没发起过时的话必须**指路**，而不是只说"没有"。

    失败要说人话并给出下一步 —— 项目的设计原则之一。

    ⚠️ 这条同时是两个错误实现的反向钉子：一个"凡读不到题解就说
    v0.36 那句"的实现、以及一个"凡 `environment_ready` 就拒绝"的实现，
    都会在这里或 `test_a_challenge_in_flight_...` 那条变红。
    """
    resp = ready_client.get(f"{API}/ctf2/challenges/{ctf2_challenge.id}/writeup")

    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == "WRITEUP_UNAVAILABLE"
    assert "点「解题」跑一次" in error["message"]


def test_writeup_is_409_and_blames_the_old_version_when_missing_but_solved(
    ready_client: TestClient, db_session: Session, ctf2_challenge
) -> None:
    """★ "发起过但没题解" 的话必须与"从没发起过"**不同**。

    ⚠️ 这是最容易被"两种都给同一句话"糊弄掉的分支：码相同、只有文案不同，
    而文案才是用户唯一能看到的东西。

    ⚠️ 判据是**受理快照存不存在**，不是状态。所以这里必须造一条
    `Ctf2SolveAttempt` —— 光改 status 不够，那正是修掉的那个 bug。
    """
    _attempt(db_session, ctf2_challenge.id)

    resp = ready_client.get(f"{API}/ctf2/challenges/{ctf2_challenge.id}/writeup")

    assert resp.status_code == 409, resp.text
    message = resp.json()["error"]["message"]
    assert "v0.36" in message
    assert "重新解一次" in message


# ══════════════════════════════════════════════════════════
# ★★ 判据的第二条证据线：壳 scan（v0.28 之前的解题）
# ══════════════════════════════════════════════════════════


def test_shell_scan_alone_counts_as_already_solved(
    ready_client: TestClient, db_session: Session, ctf2_challenge
) -> None:
    """★★ **只有壳 scan、没有 attempt 行**时，不能说"还没有解过"。

    这条盯的是一个真实的数据段（2026-10-15 复核时用全库 135 道逐题查出）：

    `ctf2_solve_attempts` 是 v0.28（迁移 `d5e8f1a37c92`）才建的表。
    **在那之前解的题没有 attempt 行，但有壳 scan** —— 实测有 **22 道**
    这样的题，其中 **20 道是 `solved` 且 `confirmed_flag` 非空**
    （`hashcat`、`Url编码`、`流量！鲨鱼！`…），`solved_at` 在 09-30~10-08，
    而有 attempt 的题最早 10-12。

    只查 attempt 的判据会对这 20 道**平台判过对的题**说
    「这道题还没有解过，点「解题」跑一次」—— 一句假话。

    ⚠️ 这里刻意**只**给 `scan_id`、**不给** attempt 行：那正是要区分的
    那个数据段。测试里加一条 attempt 就把这条用例的意义抹掉了。
    """
    ctf2_challenge.scan_id = "scan_fake_shell_for_old_solve"
    ctf2_challenge.status = "solved"
    ctf2_challenge.confirmed_flag = "flag{old_solve}"
    db_session.commit()

    resp = ready_client.get(f"{API}/ctf2/challenges/{ctf2_challenge.id}/writeup")

    assert resp.status_code == 409, resp.text
    message = resp.json()["error"]["message"]
    # ★ 不能是"还没有解过" —— 这批题真的解出过
    assert "还没有解过" not in message
    assert "这次解题没有生成题解" in message


@pytest.mark.parametrize(
    "status",
    ["environment_ready", "solving", "candidate_ready", "submitting", "failed"],
)
def test_a_challenge_in_flight_never_gets_the_wrong_explanation(
    ready_client: TestClient, db_session: Session, ctf2_challenge, status: str
) -> None:
    """★★ 一道**正在跑**的题绝不能被报成"解题记录早于题解功能上线"。

    这是修掉那个 bug 的回归钉子：旧实现按 `status == unsolved` 二分，
    于是 `solving` 等五个中间态全部掉进"v0.36 之前解的"那一档 ——
    用户刚点完「解题」，却被告知"重新解一次"。

    ⚠️ 参数化五个状态而不是只测 `solving`：它们走的是同一条错分支，
    只测一个的话，哪天有人把判据改成"只认 solving"仍然会绿。
    """
    _attempt(db_session, ctf2_challenge.id)
    ctf2_challenge.status = status
    db_session.commit()

    resp = ready_client.get(f"{API}/ctf2/challenges/{ctf2_challenge.id}/writeup")

    assert resp.status_code == 409, resp.text
    message = resp.json()["error"]["message"]
    # ★ 不能出现那句话 —— 题正在跑或已受理过，"早于 v0.36" 是假话
    assert "早于" not in message
    assert "还没有解过" not in message


def test_writeup_returns_the_contract_shape(
    ready_client: TestClient, db_session: Session, ctf2_challenge
) -> None:
    """★ 字段名逐字核对 CamelModel 的输出（写错不会让别的用例变红）。"""
    attempt = _attempt(db_session, ctf2_challenge.id)
    service.save_writeup(
        db_session,
        challenge=ctf2_challenge,
        outcome=_outcome(transcript="[turn 1] result: hi", turns=4),
        attempt_id=attempt.id,
        elapsed_sec=61,
    )
    db_session.commit()

    resp = ready_client.get(f"{API}/ctf2/challenges/{ctf2_challenge.id}/writeup")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in (
        "challengeId", "title", "category", "solved", "markdown",
        "filename", "attemptId", "turns", "elapsedSec", "generatedAt", "updatedAt",
    ):
        assert key in body, f"契约字段缺失：{key}"
    assert body["challengeId"] == ctf2_challenge.id
    assert body["turns"] == 4
    assert body["elapsedSec"] == 61
    assert body["filename"].endswith(".md")
    assert "# CTF Writeup" in body["markdown"]


def test_writeup_filename_is_usable_for_download(
    ready_client: TestClient, db_session: Session, ctf2_challenge
) -> None:
    """文件名不能带路径分隔符 —— 标题来自平台，是外部数据。"""
    attempt = _attempt(db_session, ctf2_challenge.id)
    ctf2_challenge.title = "re/../etc"
    db_session.commit()
    service.save_writeup(
        db_session,
        challenge=ctf2_challenge,
        outcome=_outcome(transcript="", turns=1),
        attempt_id=attempt.id,
        elapsed_sec=None,
    )
    db_session.commit()

    resp = ready_client.get(f"{API}/ctf2/challenges/{ctf2_challenge.id}/writeup")

    assert "/" not in resp.json()["filename"]


# ══════════════════════════════════════════════════════════
# 二、GET /ctf2/challenges/{id}/usage —— 逐次明细
# ══════════════════════════════════════════════════════════


def test_challenge_usage_is_empty_for_a_fresh_challenge(
    ready_client: TestClient, ctf2_challenge
) -> None:
    """还没跑过 → 空数组（不是 404）—— 界面要显示"还没有数据"。"""
    resp = ready_client.get(f"{API}/ctf2/challenges/{ctf2_challenge.id}/usage")

    assert resp.status_code == 200
    assert resp.json() == []


def test_challenge_usage_is_404_for_an_unknown_challenge(ready_client: TestClient) -> None:
    resp = ready_client.get(f"{API}/ctf2/challenges/chal_nope/usage")

    assert resp.status_code == 404


def test_challenge_usage_lists_each_call_with_reported_flag(
    ready_client: TestClient, db_session: Session, ctf2_challenge
) -> None:
    """★ 每一行都带 `usageReported` —— 前端靠它分开显示"0"与"未记录"。"""
    attempt = _attempt(db_session, ctf2_challenge.id)
    service.record_solve_usages(
        db_session,
        attempt_id=attempt.id,
        challenge_id=ctf2_challenge.id,
        outcome=_outcome(
            usages=[
                (1, "m1", "l1", runner.TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30, reported=True)),
                (2, "m1", "l1", runner.TokenUsage(reported=False)),
            ]
        ),
    )
    db_session.commit()

    resp = ready_client.get(f"{API}/ctf2/challenges/{ctf2_challenge.id}/usage")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 2
    for key in (
        "turn", "model", "providerLabel", "promptTokens", "completionTokens",
        "totalTokens", "reasoningTokens", "usageReported", "at",
    ):
        assert key in body[0], f"契约字段缺失：{key}"
    assert [row["usageReported"] for row in body] == [True, False]
    assert [row["turn"] for row in body] == [1, 2]


# ══════════════════════════════════════════════════════════
# 三、GET /ctf2/usage —— 全局统计
# ══════════════════════════════════════════════════════════


def test_global_usage_on_an_empty_database(ready_client: TestClient) -> None:
    """一条记录都没有时也返回 200 + 零值 —— 总览页要能渲染空状态。"""
    resp = ready_client.get(f"{API}/ctf2/usage")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["calls"] == 0
    assert body["recordedCalls"] == 0
    assert body["totalTokens"] == 0
    assert body["since"] is None
    assert body["byModel"] == []
    assert body["byChallenge"] == []
    # ★ v0.37：契约字段必须真的出现（写错不会让别的用例变红）
    assert body["byAttempt"] == []


def test_global_usage_exposes_the_attempt_breakdown(
    ready_client: TestClient, db_session: Session, ctf2_challenge
) -> None:
    """★ `byAttempt` 的字段逐字核对（需求要的"细分到每个任务"）。

    这条走**真实 HTTP**，所以它同时验证了 router 的 schema 映射
    （`Ctf2UsageByAttemptOut(**a)`）不会因为字段名对不上而 500。
    """
    attempt = _attempt(db_session, ctf2_challenge.id)
    service.record_solve_usages(
        db_session,
        attempt_id=attempt.id,
        challenge_id=ctf2_challenge.id,
        outcome=_outcome(
            usages=[
                (1, "m1", "l1", runner.TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30, reported=True)),
                (2, "m1", "l1", runner.TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10, reported=True)),
            ]
        ),
    )
    db_session.commit()

    body = ready_client.get(f"{API}/ctf2/usage").json()

    assert len(body["byAttempt"]) == 1
    row = body["byAttempt"][0]
    for key in (
        "attemptId", "challengeId", "title", "mode", "model",
        "calls", "totalTokens", "startedAt",
    ):
        assert key in row, f"契约字段缺失：{key}"
    assert row["attemptId"] == attempt.id
    assert row["calls"] == 2
    assert row["totalTokens"] == 40
    assert row["mode"] == "safe"


def test_global_usage_exposes_calls_and_recorded_calls_separately(
    ready_client: TestClient, db_session: Session, ctf2_challenge
) -> None:
    """★★ 契约字段 `calls` / `recordedCalls` 都在，且能不等。

    合成一个字段会让"接入点没给用量"伪装成"消耗 0"。
    """
    attempt = _attempt(db_session, ctf2_challenge.id)
    service.record_solve_usages(
        db_session,
        attempt_id=attempt.id,
        challenge_id=ctf2_challenge.id,
        outcome=_outcome(
            usages=[
                (1, "m", "l", runner.TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30, reported=True)),
                (2, "m", "l", runner.TokenUsage(reported=False)),
            ]
        ),
    )
    db_session.commit()

    body = ready_client.get(f"{API}/ctf2/usage").json()

    assert body["calls"] == 2
    assert body["recordedCalls"] == 1
    assert body["totalTokens"] == 30
    assert body["since"] is not None


def test_global_usage_filters_really_filter(
    ready_client: TestClient, db_session: Session, ctf2_challenge, ctf2_competition
) -> None:
    """两个筛选参数都要真的生效（传了不生效比没有这个参数更坏）。"""
    attempt = _attempt(db_session, ctf2_challenge.id)
    service.record_solve_usages(
        db_session,
        attempt_id=attempt.id,
        challenge_id=ctf2_challenge.id,
        outcome=_outcome(
            usages=[(1, "m", "l", runner.TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30, reported=True))]
        ),
    )
    db_session.commit()

    by_challenge = ready_client.get(
        f"{API}/ctf2/usage?challengeId={ctf2_challenge.id}"
    ).json()
    by_competition = ready_client.get(
        f"{API}/ctf2/usage?competitionId={ctf2_competition.id}"
    ).json()
    by_other = ready_client.get(f"{API}/ctf2/usage?challengeId=chal_nope").json()

    assert by_challenge["calls"] == 1
    assert by_competition["calls"] == 1
    assert by_other["calls"] == 0
