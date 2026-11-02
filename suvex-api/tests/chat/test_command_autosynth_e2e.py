"""贴了命令没写 ACTION → 引擎直接当 run_shell 执行（v1.51 端到端）。

配套 `test_command_autosynth.py`（纯函数）。这里只钉**引擎真的把那段命令
变成了一次工具调用**：脚本网关第一轮回一段"接下来 dump" + 裸命令块的正文
（没有 ACTION），引擎应当把这段命令翻译成一个 `run_shell` 动作执行掉，而
不是再纠偏一次让模型重发。第二轮收尾。

用 `monkeypatch` 把 `tool_run_shell` 换成一个记录调用的假实现，断言它被
以那段命令为参数调用了一次。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.chat import engine
from tests.chat.test_turn_persistence import _new_session
from tests.test_chat import ADMIN

# 第一轮：人话 + 裸命令块，没有 ACTION。
_PASTED = (
    "这批 chunk 我已经定位到了，接下来 dump 整个区域。\n\n"
    "cd /work && python3 -c \"print('autosynth-ok')\""
)


class _Reply:
    def __init__(self, *, text: str, finish_reason: str = "") -> None:
        self.text = text
        self.finish_reason = finish_reason
        self.reasoning = None
        self.raw: dict[str, Any] = {}
        self.usage = None
        self.source = "content"


class _Gateway:
    def __init__(self, replies):
        self.replies = list(replies)

    async def chat(self, messages, *, system):  # noqa: ANN001, ANN201
        text, finish = self.replies.pop(0) if self.replies else ("", "")
        return _Reply(text=text, finish_reason=finish)


async def test_pasted_command_is_executed_without_a_nudge(
    db_session: Session, monkeypatch: Any
) -> None:
    calls: list[dict[str, Any]] = []

    async def _fake_run_shell(ctx, args):  # noqa: ANN001, ANN202
        calls.append(dict(args))
        return "ok"

    monkeypatch.setitem(engine._TOOL_IMPLS, "run_shell", _fake_run_shell)

    session = _new_session(db_session)
    gateway = _Gateway([(_PASTED, ""), ("FINAL: 看完了。", "")])
    await engine.run_turn(
        db_session,
        session=session,
        content="dump 一下",
        identity=ADMIN,
        gateway=gateway,
    )

    assert len(calls) == 1, "贴的命令必须被执行一次"
    assert calls[0]["command"].startswith("cd /work && python3 -c")
    assert "autosynth-ok" in calls[0]["command"]
    # 只用了两次模型调用（拿到命令 + 收尾），没有额外那次纠偏往返。
    assert len(gateway.replies) == 0


async def test_pasted_command_without_intent_cue_is_not_executed(
    db_session: Session, monkeypatch: Any
) -> None:
    """没有「接下来/现在」提示，只是陈述 + 贴命令 → 不该自动执行。"""
    calls: list[dict[str, Any]] = []

    async def _fake_run_shell(ctx, args):  # noqa: ANN001, ANN202
        calls.append(dict(args))
        return "ok"

    monkeypatch.setitem(engine._TOOL_IMPLS, "run_shell", _fake_run_shell)
    session = _new_session(db_session)
    gateway = _Gateway([("global.utoc 我没拆过。\n\ncd /work && ls", ""), ("FINAL: 完。", "")])
    await engine.run_turn(
        db_session,
        session=session,
        content="看看",
        identity=ADMIN,
        gateway=gateway,
    )
    assert calls == [], "没有下一步提示时不自动执行"
