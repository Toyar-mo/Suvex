"""平台能力断言：赛事侧的题目**不能**解题/提交。

## 为什么这是一个需要测试的"功能"，而不是一段防御性代码

这里拒绝的不是"不该做的事"，而是**平台上根本不存在的东西**。

对 `https://ctf2.dasctf.com/api/open/v1` 逐条探测过（未鉴权请求：
401/400 说明路由存在，`404 + "resource":"route"` 说明不存在）：
练习侧的提交与起环境端点存在，而**赛事侧的同名端点全部 404**
（`competitions/{id}/challenges/{cid}/submit/`、
`stages/{sid}/challenges/{cid}/environment/start/` 等 8 条）。

所以"赛事题目也能解题"这个功能不是没做完，而是**平台没开放**。
代码能做的最好的事是：在**受理之前**给出明确的 409，而不是发一个
注定 404 的请求（那会被翻译成"平台接口可能变更了"，排查方向全错），
更不是猜一条对称的路径（猜错的代价是我们把一次没送达的提交记成
"已提交"，用户据此以为这题交过了）。

## 这个文件钉住的是"拒绝的**时机**"

断言里除了状态码，还要断言**没落库的状态变化**：一道被拒的题不该
变成 `solving`（那会让用户以为已经在跑了）。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.core.vocab import KernelChallengeStatus, KernelSandboxProfile


@pytest.fixture()
def competition_challenge(db_session: Session, ctf2_account):
    """**赛事**（`kind='competition'`）下的一道题。

    ⚠️ 与 `ctf2_challenge`（练习集）刻意分开：这两条路径的行为**不同**，
    而它们长得几乎一样 —— 正是那种"改了一处忘了另一处"的地方。
    """
    from app.kernel.models import KernelChallenge, KernelCompetition

    competition = KernelCompetition(
        id=new_id("ctf2comp"),
        account_id=ctf2_account.id,
        remote_id="9001",
        kind="competition",
        name="2026 秋季赛",
        remote_status="ongoing",
        challenge_count=1,
        synced_at=now_utc(),
    )
    db_session.add(competition)
    db_session.commit()

    challenge = KernelChallenge(
        id=new_id("ctf2chal"),
        competition_id=competition.id,
        remote_id="c-9001",
        stage_id="stage-1",
        title="赛事题",
        category="web",
        status=KernelChallengeStatus.UNSOLVED.value,
        candidate_flag=None,
        confirmed_flag=None,
        wrong_submission_count=0,
        max_wrong_submissions=3,
        candidate_count=0,
        sandbox_profile=KernelSandboxProfile.BASE.value,
    )
    db_session.add(challenge)
    db_session.commit()
    return challenge


def test_solve_is_refused_for_a_competition_challenge(
    db_session: Session, competition_challenge
) -> None:
    """★ 受理阶段就拒绝，而且**不改题目状态**。

    只断言"抛了 409"是不够的：如果拒绝发生在状态改成 `solving` 之后，
    用户会在界面上看到这道题"正在解题" —— 而它永远不会动。
    """
    from app.kernel import service

    with pytest.raises(ApiError) as excinfo:
        service.start_solve(db_session, competition_challenge)

    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "CTF2_COMPETITION_SUBMIT_UNSUPPORTED"
    # 拒绝必须说得像人话：用户要知道"这不是我的错，也不是等一会就好"
    assert "赛事" in excinfo.value.message

    db_session.refresh(competition_challenge)
    assert competition_challenge.status == KernelChallengeStatus.UNSOLVED.value
    assert competition_challenge.scan_id is None


def test_submit_is_refused_for_a_competition_challenge(
    db_session: Session, competition_challenge
) -> None:
    """提交路径同样拒绝，而且**在格式校验之前**。

    先做 flag 格式校验再去拒绝，会让用户以为"改一下 flag 就能交" ——
    于是他会一遍遍改，而这条路根本没有终点。
    """
    from app.kernel import service

    with pytest.raises(ApiError) as excinfo:
        # 故意给一个**形状不合法**的 flag：如果实现先校验格式，
        # 这里拿到的会是 422/格式错误，而不是平台能力那条 409。
        service.confirm_submit(db_session, competition_challenge, flag="not-a-flag")

    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "CTF2_COMPETITION_SUBMIT_UNSUPPORTED"


def test_practice_challenge_is_not_affected(db_session: Session, ctf2_challenge) -> None:
    """练习集的题**不受影响**（这条断言挡的是"一刀切拒绝"）。

    ⚠️ 这里只走到"账号/LLM 检查"那一步之前就足够了 —— 重点是
    **不抛平台能力那条错**。所以断言的是"错误码不是它"，
    而不是"调用成功"（成功需要一个真的 provider 与账号，那是别的用例的事）。
    """
    from app.kernel import service

    try:
        service.start_solve(db_session, ctf2_challenge)
    except ApiError as exc:
        assert exc.code != "CTF2_COMPETITION_SUBMIT_UNSUPPORTED", (
            "练习集的题被平台能力断言误伤了"
        )


def test_unknown_kind_is_refused_conservatively(
    monkeypatch, db_session: Session, competition_challenge
) -> None:
    """★ 认不出的 `kind` 一律**拒绝**（fail-closed）。

    平台将来可能加一种新的赛事类型。那时我们不知道它有没有提交端点 ——
    而"不知道"的正确处理是不发请求：猜一条路径去提交，最坏的结果是把
    一次没送达的提交记成"已提交"，用户据此以为这题交过了。

    ⚠️ 这条**只能靠 monkeypatch 造**：数据库上有一条
    `ck_ctf2_competitions_kind_valid`（`kind IN ('competition','practice')`），
    所以库里写不进 `mystery`。那说明这个分支平时不可达 —— 但它仍然
    值得测，因为那条 CHECK 会在某次迁移里被放宽，而放宽的那一刻
    这个分支就变成活的了。
    """
    from app.kernel import service

    class _AlienCompetition:
        kind = "mystery"

    monkeypatch.setattr(db_session, "get", lambda *args, **kwargs: _AlienCompetition())

    with pytest.raises(ApiError) as excinfo:
        service._assert_submission_supported(db_session, competition_challenge)

    assert excinfo.value.code == "CTF2_COMPETITION_SUBMIT_UNSUPPORTED"


# ══════════════════════════════════════════════════════════════════════
# v1.72：赛事题在**配置驱动平台**上要能解（不再一刀切拒绝）
# ══════════════════════════════════════════════════════════════════════
#
# 旧断言依据的是 **CTF2 旧开放 API** 的实测（赛事端点全 404）。但它不认识
# 我们自建的 GenericDriver 平台（如 slab-match：官方 api_doc.md 明确给了
# `POST /answer-panel/answer` 提交 + `POST /ctf/reset-endpoint` 起环境）。
# 这类平台上赛事题是**能解**的，所以闸门要放行。

_SPEC_WITH_SUBMIT_AND_ENV = {
    "display_name": "比赛平台",
    "base_url": "https://example.test/slab-match/api/v1/agent",
    "auth": {"style": "header", "name": "X-Agent-AccessKey", "prefix": ""},
    "envelope": {
        "success_path": "code",
        "success_value": "00000",
        "data_path": "data",
        "message_path": "message",
    },
    "list_challenges": {
        "request": {"method": "GET", "path": "/ctf/exercise-list"},
        "items_path": "",
        "children_path": "corpus",
        "category_from_group": True,
    },
    "challenge_detail": {
        "method": "GET",
        "path": "/ctf/exercise",
        "params": {"exerciseId": "{id}"},
    },
    "submit": {
        "method": "POST",
        "path": "/answer-panel/answer",
        "body": {"exerciseId": "{id}", "flag": "{answer}"},
    },
    "start_env": {"method": "POST", "path": "/ctf/reset-endpoint", "body": {"exerciseId": "{id}"}},
    "environment": {"from_detail": True, "endpoints_path": "endpoints", "ready_path": "isNeedCheck", "ready_negate": True},
    "fields": {"id": "id", "title": "name", "description": "description", "score": "score", "solved": "hasSolved"},
}


def _backed_competition(db_session: Session, *, spec: dict | None):
    """造一个「账号→平台→赛事」链，供放行断言用。"""
    from app.core.crypto import encrypt_secret, mask_secret
    from app.kernel.models import KernelAccount, KernelCompetition, KernelPlatform

    platform = KernelPlatform(
        id=new_id("knplat"),
        slug=f"test-src-{new_id('x')[:8]}",
        display_name="比赛平台",
        base_url="https://example.test/slab-match/api/v1/agent",
        driver="generic",
        spec=spec,
        status="verified",
    )
    db_session.add(platform)
    db_session.commit()

    account = KernelAccount(
        id=new_id("knacc"),
        label="比赛号",
        token_encrypted=encrypt_secret("ak_test_key"),
        token_hint=mask_secret("ak_test_key"),
        status="active",
        platform_id=platform.id,
    )
    db_session.add(account)
    db_session.commit()

    competition = KernelCompetition(
        id=new_id("kncomp"),
        account_id=account.id,
        remote_id="c-1",
        kind="competition",
        name="比赛赛",
        remote_status="ongoing",
        challenge_count=1,
        synced_at=now_utc(),
    )
    db_session.add(competition)
    db_session.commit()
    return competition


def test_competition_on_generic_platform_with_submit_env_is_allowed(db_session: Session) -> None:
    """配了 submit + 起环境的 GenericDriver 平台上的赛事题：**放行**。"""
    from app.kernel import service

    competition = _backed_competition(db_session, spec=_SPEC_WITH_SUBMIT_AND_ENV)
    assert service._competition_backed_by_generic_driver(db_session, competition) is True
    # 直接调闸门，不抛即放行。
    from app.kernel.models import KernelChallenge

    challenge = KernelChallenge(
        id=new_id("knchal"),
        competition_id=competition.id,
        remote_id="c-1",
        title="赛事题",
        category="web",
        status=KernelChallengeStatus.UNSOLVED.value,
        sandbox_profile=KernelSandboxProfile.BASE.value,
    )
    db_session.add(challenge)
    db_session.commit()
    service._assert_submission_supported(db_session, challenge)  # 不抛 = 通过


def test_competition_on_generic_platform_without_submit_still_refused(db_session: Session) -> None:
    """平台没配 submit（或根本不是 generic 配置）→ 仍按旧断言拒绝。"""
    from app.kernel import service

    bad_spec = dict(_SPEC_WITH_SUBMIT_AND_ENV)
    bad_spec["submit"] = {"method": "POST", "path": ""}  # 没有 path
    competition = _backed_competition(db_session, spec=bad_spec)
    assert service._competition_backed_by_generic_driver(db_session, competition) is False

    from app.kernel.models import KernelChallenge

    challenge = KernelChallenge(
        id=new_id("knchal"),
        competition_id=competition.id,
        remote_id="c-1",
        title="赛事题",
        category="web",
        status=KernelChallengeStatus.UNSOLVED.value,
        sandbox_profile=KernelSandboxProfile.BASE.value,
    )
    db_session.add(challenge)
    db_session.commit()
    with pytest.raises(ApiError) as excinfo:
        service._assert_submission_supported(db_session, challenge)
    assert excinfo.value.code == "CTF2_COMPETITION_SUBMIT_UNSUPPORTED"
