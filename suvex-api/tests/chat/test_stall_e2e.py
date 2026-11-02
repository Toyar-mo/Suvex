"""「原地打转」端到端：连续同一条命令 5 次 → 上下文里出现停滞提醒（v1.49）。

配套 `test_stall_detection.py`（纯函数）与 `engine._action_signature`。这里只钉
一件事：**引擎真的把提醒喂回给模型了** —— 脚本网关跑 5 次一模一样的
`ACTION`（同工具同参数），第 6 次调用模型时，喂进去的 `messages` 里必须
出现 `render_stall_reminder` 的文案，且提醒只出现一次。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.chat import engine
from app.chat.prompts import render_stall_reminder
from tests.chat.test_turn_persistence import _new_session
from tests.test_chat import ADMIN

_SAME = 'ACTION: {"tool": "list_scans", "args": {}}'


class _RecordingGateway:
    """记录每次调用时收到的 messages，用于断言"提醒被喂回去了"。"""

    def __init__(self, replies: list[tuple[str, str]]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.seen: list[list[dict[str, Any]]] = []

    async def chat(self, messages, *, system):  # noqa: ANN001, ANN201
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        text, finish = self.replies.pop(0) if self.replies else ("", "")
        return _Reply(text=text, finish_reason=finish)


class _Reply:
    def __init__(self, *, text: str, finish_reason: str = "") -> None:
        self.text = text
        self.finish_reason = finish_reason
        self.reasoning = None
        self.raw: dict[str, Any] = {}
        self.usage = None
        self.source = "content"


def _all_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


async def test_five_identical_commands_inject_stall_reminder(
    db_session: Session,
) -> None:
    session = _new_session(db_session)
    # 5 次完全相同的动作，第 6 次收尾。
    gateway = _RecordingGateway([(_SAME, "")] * 5 + [("FINAL: 换思路后放弃了。", "")])
    await engine.run_turn(
        db_session,
        session=session,
        content="解一下",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 6, "5 次动作 + 1 次收尾"
    marker = render_stall_reminder("list_scans", engine.MAX_REPEATED_ACTIONS)
    # 第 6 次调用（下标 5）应当已经带上提醒。
    sixth = _all_text(gateway.seen[5])
    assert marker in sixth, "连续 5 次相同动作后，第 6 次模型调用必须收到停滞提醒"
    # 只提醒一次：前 5 次调用的上下文里都没有这句。
    for earlier in gateway.seen[:5]:
        assert render_stall_reminder("list_scans", engine.MAX_REPEATED_ACTIONS) not in _all_text(
            earlier
        )


async def test_four_identical_commands_do_not_inject(db_session: Session) -> None:
    """4 次相同动作还不够触发（容许偶发重跑对比）。"""
    session = _new_session(db_session)
    gateway = _RecordingGateway([(_SAME, "")] * 4 + [("FINAL: 完。", "")])
    await engine.run_turn(
        db_session,
        session=session,
        content="解一下",
        identity=ADMIN,
        gateway=gateway,
    )
    for seen in gateway.seen:
        assert "原地打转" not in _all_text(seen)
