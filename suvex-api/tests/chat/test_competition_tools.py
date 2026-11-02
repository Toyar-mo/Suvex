"""对话页的比赛平台工具（v1.46，羊城杯）。

## 这些用例守的是什么

1. **flag 外壳剥离**（`strip_flag_shell`）：赛事只收花括号内的内容，
   交整串会判错、白扣一次不可再生的提交次数。这是**比赛当天最容易踩**
   的一脚，所以用表驱动钉死"剥"与"不剥"两类。
2. **不该剥的不剥**：命令行、IP、时间戳、哈希这些**合法答案**不能被
   误剥 —— 与 `validate_answer` 的「宁可放过，不可错杀」同一方向。
3. **两个工具在"没配比赛平台"时给人话**，不抛异常、不把整轮对话打崩。
4. **提交结果的三态分流**：正确 / 判错（真错一次）/ 没受理（不扣次数，
   可重试）。这三种给用户的话必须不一样，否则模型会把"平台 429"当成
   "答案错了"然后放弃。

驱动本身（`SlabMatchDriver` 的 HTTP 解析）由 `tests/kernel/` 覆盖，
这里用假驱动替身，只测**对话层的接线与判定**。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.chat import competition
from app.kernel.platform import (
    RemoteChallenge,
    RemoteEndpoint,
    RemoteEnvironment,
    SubmitOutcome,
)

# ══════════════════════════════════════════════════════════
# strip_flag_shell
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 剥：带了外壳
        ("DASCTF{abc123}", "abc123"),
        ("flag{hello world}", "hello world"),
        ("ctf2{plain}", "plain"),
        # 前后有空白的也剥
        ("  DASCTF{abc123}  ", "abc123"),
        # 不剥：本来就没有外壳的合法答案
        ("abc123", "abc123"),
        ("cat /flag", "cat /flag"),
        ("192.168.1.1", "192.168.1.1"),
        ("2026-11-15T13:04:19Z", "2026-11-15T13:04:19Z"),
        ("deadbeef" * 8, "deadbeef" * 8),
        # 嵌套花括号：形状不符 → 原样返回（不冒险切坏）
        ("flag{a{b}}", "flag{a{b}}"),
        # 只有外壳没有正文：原样返回（不是可交的答案，但也不该变成空串）
        ("flag{}", "flag{}"),
        # 空串
        ("", ""),
        ("   ", ""),
    ],
)
def test_strip_flag_shell(raw: str, expected: str) -> None:
    assert competition.strip_flag_shell(raw) == expected


def test_strip_flag_shell_strips_nested_only_once_not_greedy() -> None:
    # 一个真实的长 flag（UUID 形状）要能完整取出来
    body = "8743fd24-a2a2-4677-a663-c1b50d53eb8f"
    assert competition.strip_flag_shell(f"DASCTF{{{body}}}") == body


# ══════════════════════════════════════════════════════════
# resolve_driver 在没配平台时的行为
# ══════════════════════════════════════════════════════════


class _FakeDriver:
    """最小驱动替身：只实现对话层会调到的几个方法。"""

    answer_max_length = 256

    def __init__(self, *, tasks=None, env=None, outcome=None):
        self._tasks = tasks or []
        self._env = env or RemoteEnvironment()
        self._outcome = outcome or SubmitOutcome(accepted=True, is_solved=True)
        self.submitted: list[tuple[dict[str, Any], str]] = []

    async def fetch_challenges(self, *, known_ids):
        return list(self._tasks)

    async def read_environment(self, handle):
        return self._env

    async def submit(self, handle, answer):
        self.submitted.append((dict(handle), answer))
        return self._outcome


def test_resolve_driver_returns_none_without_account(db_session) -> None:
    """没有账号 → None（调用方给"先去绑账号"的人话，不抛）。"""
    assert competition.resolve_driver(db_session) is None


@pytest.mark.asyncio
async def test_list_tasks_raises_lookup_error_without_account(db_session) -> None:
    with pytest.raises(LookupError):
        await competition.list_tasks(db_session)


# ══════════════════════════════════════════════════════════
# 工具层：没配平台 → 人话，不抛
# ══════════════════════════════════════════════════════════


class _Ctx:
    """`_ToolContext` 的最小替身（工具只用到 db）。"""

    def __init__(self, db):
        self.db = db
        self.session = None
        self.identity = None


@pytest.mark.asyncio
async def test_fetch_tool_returns_human_message_without_platform(db_session) -> None:
    from app.chat.engine import _tool_fetch_competition_challenges

    out = await _tool_fetch_competition_challenges(_Ctx(db_session), {})
    assert out.startswith("失败：")
    assert "比赛平台" in out and "AccessKey" in out


@pytest.mark.asyncio
async def test_submit_tool_returns_human_message_without_platform(db_session) -> None:
    from app.chat.engine import _tool_submit_flag

    out = await _tool_submit_flag(_Ctx(db_session), {"remoteId": "123", "flag": "DASCTF{x}"})
    assert out.startswith("失败：")
    assert "比赛平台" in out


# ══════════════════════════════════════════════════════════
# 工具层：接上假驱动后的路径（monkeypatch resolve_driver）
# ══════════════════════════════════════════════════════════


def _patch_driver(monkeypatch, driver):
    monkeypatch.setattr(competition, "resolve_driver", lambda db: driver)


@pytest.mark.asyncio
async def test_fetch_tool_lists_tasks(db_session, monkeypatch) -> None:
    from app.chat.engine import _tool_fetch_competition_challenges

    driver = _FakeDriver(
        tasks=[
            RemoteChallenge(remote_id="1", title="Web 入门", category="Web", score=100, is_open=True),
            RemoteChallenge(
                remote_id="2", title="Pwn 挑战", category="Pwn", score=200, is_open=False, solved=True
            ),
        ]
    )
    _patch_driver(monkeypatch, driver)
    out = await _tool_fetch_competition_challenges(_Ctx(db_session), {})
    assert "共 2 道题" in out
    assert "`1`" in out and "Web 入门" in out and "100分" in out
    assert "未开放" in out and "已解" in out


@pytest.mark.asyncio
async def test_fetch_tool_appends_endpoint_info(db_session, monkeypatch) -> None:
    from app.chat.engine import _tool_fetch_competition_challenges

    env = RemoteEnvironment(
        endpoints=[
            RemoteEndpoint(expose_ips=["10.0.0.5"], ports=["22", "80"], users=[("root", "pass123")])
        ],
        ready=True,
    )
    driver = _FakeDriver(
        tasks=[RemoteChallenge(remote_id="7", title="SSH 题")],
        env=env,
    )
    _patch_driver(monkeypatch, driver)
    out = await _tool_fetch_competition_challenges(_Ctx(db_session), {"remoteId": "7"})
    assert "10.0.0.5" in out and "pass123" in out and "22" in out


@pytest.mark.asyncio
async def test_submit_tool_strips_shell_before_submit(db_session, monkeypatch) -> None:
    from app.chat.engine import _tool_submit_flag

    driver = _FakeDriver(outcome=SubmitOutcome(accepted=True, is_solved=True))
    _patch_driver(monkeypatch, driver)
    out = await _tool_submit_flag(_Ctx(db_session), {"remoteId": "5", "flag": "DASCTF{secret}"})
    # 交出去的是**剥过壳**的内容
    assert driver.submitted == [({"exerciseId": 5}, "secret")]
    assert "正确" in out and "已解出" in out


@pytest.mark.asyncio
async def test_submit_tool_reports_wrong_answer(db_session, monkeypatch) -> None:
    from app.chat.engine import _tool_submit_flag

    driver = _FakeDriver(
        outcome=SubmitOutcome(accepted=True, is_solved=False, message="平台判定为错误答案。")
    )
    _patch_driver(monkeypatch, driver)
    out = await _tool_submit_flag(_Ctx(db_session), {"remoteId": "5", "flag": "wrong"})
    assert "错误答案" in out
    assert driver.submitted == [({"exerciseId": 5}, "wrong")]


@pytest.mark.asyncio
async def test_submit_tool_not_accepted_does_not_consume_attempt(db_session, monkeypatch) -> None:
    """平台没受理（限流/额度）→ 明确告诉模型"没扣次数、可重试"。"""
    from app.chat.engine import _tool_submit_flag

    driver = _FakeDriver(
        outcome=SubmitOutcome(accepted=False, is_solved=False, message="rate limited")
    )
    _patch_driver(monkeypatch, driver)
    out = await _tool_submit_flag(_Ctx(db_session), {"remoteId": "5", "flag": "x"})
    assert "没受理" in out and "没有消耗错误次数" in out


@pytest.mark.asyncio
async def test_submit_tool_rejects_empty_after_strip(db_session, monkeypatch) -> None:
    from app.chat.engine import _tool_submit_flag

    driver = _FakeDriver()
    _patch_driver(monkeypatch, driver)
    out = await _tool_submit_flag(_Ctx(db_session), {"remoteId": "5", "flag": "   "})
    assert out.startswith("失败：")
    assert driver.submitted == []


@pytest.mark.asyncio
async def test_submit_tool_rejects_too_long_answer(db_session, monkeypatch) -> None:
    from app.chat.engine import _tool_submit_flag

    driver = _FakeDriver()
    driver.answer_max_length = 8
    _patch_driver(monkeypatch, driver)
    out = await _tool_submit_flag(_Ctx(db_session), {"remoteId": "5", "flag": "x" * 20})
    assert out.startswith("失败：") and "太长" in out
    assert driver.submitted == []


# ══════════════════════════════════════════════════════════
# 工具与提示词登记的一致性（与 test_chat.py 的全局守护互补）
# ══════════════════════════════════════════════════════════


def test_competition_tools_are_declared_and_implemented() -> None:
    from app.chat.engine import _TOOL_IMPLS
    from app.chat.prompts import CHAT_TOOL_NAMES

    for name in ("fetch_competition_challenges", "submit_flag"):
        assert name in CHAT_TOOL_NAMES, f"{name} 没在提示词工具清单里"
        assert name in _TOOL_IMPLS, f"{name} 没在 _TOOL_IMPLS 里注册"


# ══════════════════════════════════════════════════════════
# 失败翻译：把"平台拒绝"与"链路抖动"分开
# ══════════════════════════════════════════════════════════


def test_friendly_error_flags_accesskey_rejection() -> None:
    """平台回 `无权操作` 时，提示必须指向 accesskey / 绑定，而不是网络。"""
    exc = RuntimeError("无权操作")
    msg = competition.friendly_driver_error(exc, "拉比赛平台题库")
    assert "accesskey" in msg
    assert "设置" in msg
    assert "无权操作" in msg
    assert "网络" not in msg


def test_friendly_error_flags_english_permission_words() -> None:
    exc = RuntimeError("403 Forbidden: token not allowed")
    msg = competition.friendly_driver_error(exc, "提交到比赛平台")
    assert "accesskey" in msg


def test_friendly_error_keeps_generic_failure_generic() -> None:
    exc = TimeoutError("connect timed out")
    msg = competition.friendly_driver_error(exc, "拉比赛平台题库")
    assert "拉比赛平台题库失败" in msg
    assert "connect timed out" in msg
    assert "accesskey" not in msg
