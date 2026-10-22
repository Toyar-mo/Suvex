"""`ctf2.schemas` 与 `core.vocab` 的对账。

## 为什么这几条值得单独一个文件

`schemas.py` 里的 `Literal[...]` 是**第二份**枚举定义（第一份在 vocab）。
第二份必然漂移 —— 而漂移的症状是**静默**的：

* vocab 加一个状态 → Literal 没跟 → 新状态在 API 层被 **400 拒掉**，
  而模型层、数据库层都是对的（它们由 `values()` 动态生成）。
  本项目在 `findings/schemas.py` 的 `StatusLiteral` 上真栽过这一次。

`tests/test_scan_brief_and_goal.py` 已经为 `GoalLiteral` 写了一条同样的
对账测试 —— 这里是同一个套路，只是覆盖 CTF2 的四个字面量。

⚠️ 它与 `scripts/verify_ctf2_parity.py` 的**分工**：
* 这里比的是**后端内部**（vocab ↔ schemas），跑在 `pytest` 里；
* 那个脚本比的是**跨仓库**（vocab ↔ 前端 `data/ctf2.ts`），
  要在两个仓库都在的机器上跑。
"""

from __future__ import annotations

from typing import get_args

from app.core import vocab
from app.kernel import schemas


def test_challenge_status_literal_matches_the_vocab_enum() -> None:
    """★ 题目状态：Literal 必须等于 vocab 枚举。

    这条规矩本项目栽过：`findings/schemas.py` 的 `StatusLiteral`
    当初漏掉两个状态，于是新状态在 API 层被 400 拒掉 ——
    而模型层、数据库层都是对的，只有一处字面量落后。
    """
    assert set(get_args(schemas.ChallengeStatusLiteral)) == {
        member.value for member in vocab.Ctf2ChallengeStatus
    }


def test_submission_status_literal_matches_the_vocab_enum() -> None:
    assert set(get_args(schemas.SubmissionStatusLiteral)) == {
        member.value for member in vocab.Ctf2SubmissionStatus
    }


def test_environment_status_literal_matches_the_vocab_enum() -> None:
    assert set(get_args(schemas.EnvironmentStatusLiteral)) == {
        member.value for member in vocab.Ctf2EnvironmentStatus
    }


def test_submit_mode_literal_matches_the_vocab_enum() -> None:
    assert set(get_args(schemas.SubmitModeLiteral)) == {
        member.value for member in vocab.Ctf2SubmitMode
    }


def test_sandbox_profile_literal_matches_the_vocab_enum() -> None:
    assert set(get_args(schemas.SandboxProfileLiteral)) == {
        member.value for member in vocab.Ctf2SandboxProfile
    }


def test_models_generate_their_checks_from_the_vocab() -> None:
    """模型层的 CHECK 表达式由 `values()` 生成，所以它自动跟着 vocab ——
    这条断言是那个"自动"的证据（万一有人把它改成手写字面量就会红）。
    """
    from app.kernel.models import Ctf2Challenge, Ctf2Environment, Ctf2Submission

    for model, enum_cls in (
        (Ctf2Challenge, vocab.Ctf2ChallengeStatus),
        (Ctf2Environment, vocab.Ctf2EnvironmentStatus),
        (Ctf2Submission, vocab.Ctf2SubmissionStatus),
    ):
        rendered = " ".join(
            str(constraint.sqltext)
            for constraint in model.__table__.constraints
            if constraint.name and "status_valid" in constraint.name
        )
        assert rendered, f"{model.__name__} 上没有 status_valid 约束"
        for value in vocab.values(enum_cls):
            assert f"'{value}'" in rendered, (
                f"{model.__name__} 的 CHECK 里少了取值 {value!r} —— "
                "它大概被改成手写字面量了"
            )


def test_submit_mode_check_covers_every_value() -> None:
    """`submit_mode` 的 CHECK 同理，但它在**另一张表**上（配置单例）。

    单独一条是因为它容易漏：`Ctf2Config` 住在 `settings/models.py`，
    而"加一个提交策略"的人多半只想到 `ctf2_challenges` 那张表。
    """
    from app.settings.models import Ctf2Config

    rendered = " ".join(
        str(constraint.sqltext)
        for constraint in Ctf2Config.__table__.constraints
        if constraint.name and "submit_mode_valid" in constraint.name
    )
    assert rendered
    for value in vocab.values(vocab.Ctf2SubmitMode):
        assert f"'{value}'" in rendered


def test_wrong_submission_ceiling_matches_between_code_and_database() -> None:
    """★ 上限的天花板在两处：代码常量与表上的 CHECK。

    CHECK 写的是**字面量**（`<= 10`），因为表达式一旦建好就固化在库里
    —— 写常量会让"改了常量但没写迁移"表现为"应用层通过、数据库拒绝"。
    那正是本项目在 `findings.status` 上踩过的那类漂移。

    所以这条断言是那个提醒的**可执行版本**：改了任一处，另一处没跟就红。
    """
    from app.kernel.service import MAX_WRONG_SUBMISSIONS_CEILING
    from app.settings.models import Ctf2Config

    rendered = " ".join(
        str(constraint.sqltext)
        for constraint in Ctf2Config.__table__.constraints
        if constraint.name and "max_wrong_submissions_within_ceiling" in constraint.name
    )
    assert rendered, "Ctf2Config 上少了 max_wrong_submissions_within_ceiling"
    assert f"<= {MAX_WRONG_SUBMISSIONS_CEILING}" in rendered, (
        f"代码里的上限是 {MAX_WRONG_SUBMISSIONS_CEILING}，而表上的 CHECK 是："
        f"{rendered} —— 改了一处没改另一处"
    )
