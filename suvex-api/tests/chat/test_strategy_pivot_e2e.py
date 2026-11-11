"""认证尝试到上限后必须换策略族，而不是交卷（v1.87）。

线上会话标题「用子代理并行渗透…」里，模型在第 10 次登录/口令尝试后被引擎
直接落一句「已停止…」并结束整轮；用户看到的是“试了一个策略就交给用户”。

这里用脚本网关 + 假工具执行钉住三件事：
1. 第 11 次认证调用被拒绝，但不结束整轮；
2. 拒绝之后上下文里出现“换策略族”的提醒；
3. 只有认证尝试、没有 3 个不同方向的结果块会被打回，不会落成最终答复。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.chat import engine, service
from app.chat.prompts import render_strategy_pivot_reminder
from tests.chat.test_turn_persistence import _new_session
from tests.test_chat import ADMIN

_API = 'ACTION: {"tool": "run_shell", "args": {"command": "curl -X POST http://target/login -d password=admin"}}'
_PROBE_1 = 'ACTION: {"tool": "run_shell", "args": {"command": "nmap -sV 10.0.0.5"}}'
_PROBE_2 = 'ACTION: {"tool": "run_shell", "args": {"command": "curl -s http://10.0.0.5/robots.txt"}}'
_PROBE_3 = 'ACTION: {"tool": "run_shell", "args": {"command": "curl -s http://10.0.0.5/.git/HEAD"}}'
_SPAWN = 'ACTION: {"tool": "spawn_subagents", "args": {"tasks": [{"name": "recon", "prompt": "枚举 10.0.0.5"}]}}'
_RESULT = "结果\n- 目标：10.0.0.5\n- 结论：仅测试到认证入口\n- 证据：见覆盖记录\n- 产物：无\n- 未完成/限制：其他方向未验证"


class _Gateway:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.seen: list[list[dict[str, Any]]] = []

    async def chat(self, messages, *, system):  # noqa: ANN001, ANN201
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        text = self.replies.pop(0) if self.replies else ""
        return _Reply(text=text)


class _Reply:
    def __init__(self, *, text: str) -> None:
        self.text = text
        self.finish_reason = ""
        self.reasoning = None
        self.raw: dict[str, Any] = {}
        self.usage = None
        self.source = "content"


async def _fake_execute(_ctx, _name, _args):  # noqa: ANN001, ANN201
    return "ok", False


def _all_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(
        m["content"] for m in messages if isinstance(m.get("content"), str)
    )


async def test_auth_limit_blocks_the_family_and_continues(
    db_session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(engine, "_execute_tool", _fake_execute)
    session = _new_session(db_session)
    # 10 次认证 + 第 11 次（应被拒绝）+ 3 个不同方向 + 结果块。
    gateway = _Gateway([_API] * 11 + [_PROBE_1, _PROBE_2, _PROBE_3, _RESULT])

    await engine.run_turn(
        db_session,
        session=session,
        content="开始评估",
        identity=ADMIN,
        gateway=gateway,
    )

    assert gateway.calls == 15, "第 11 次认证应被拒绝，然后继续执行 3 个探测 + 收尾"
    assert "换策略族" in _all_text(gateway.seen[10]), "达到上限时就必须把换策略提醒喂回去"
    assert "达到该策略族上限" in _all_text(gateway.seen[11]), "下一次模型调用必须看到拒绝原因"

    stored = service.list_messages(db_session, session=session)
    assert not any("已停止：本轮" in (m.content or "") for m in stored), "不能再落旧的停止语"
    assert any("结果" in (m.content or "") for m in stored if m.role == "assistant")


async def test_plan_bounce_quota_resets_after_real_tool_progress(
    db_session: Session, monkeypatch
) -> None:
    """长任务里正常的“接下来…”不能累计吃光计划打回额度。"""
    monkeypatch.setattr(engine, "_execute_tool", _fake_execute)
    session = _new_session(db_session)
    actions = [
        f'ACTION: {{"tool": "run_shell", "args": {{"command": "echo step-{i}"}}}}'
        for i in range(5)
    ]
    plan = "接下来继续检查下一个入口"
    replies: list[str] = []
    for action in actions:
        replies.extend([action, plan])
    replies.append(_RESULT)
    gateway = _Gateway(replies)

    await engine.run_turn(
        db_session,
        session=session,
        content="开始评估",
        identity=ADMIN,
        gateway=gateway,
    )

    assert gateway.calls == 11, "5 个工具 + 5 句计划 + 最终结果，不能在第 5 句计划处收尾"
    stored = service.list_messages(db_session, session=session)
    assert not any("这一轮停在了下一步上" in (m.content or "") for m in stored)


async def test_parent_can_summarize_delegated_subagent_work(
    db_session: Session, monkeypatch
) -> None:
    """父代理把工作交给子代理后，不应被自己的 3 探测闸门卡住。"""
    monkeypatch.setattr(engine, "_execute_tool", _fake_execute)
    session = _new_session(db_session)
    session.persona = "pentest"
    db_session.commit()
    gateway = _Gateway([_SPAWN, _RESULT])

    await engine.run_turn(
        db_session,
        session=session,
        content="开始评估",
        identity=ADMIN,
        gateway=gateway,
    )

    assert gateway.calls == 2
    assert "结果" in _all_text(gateway.seen[1])


async def test_single_probe_final_is_bounced_for_pentest_persona(
    db_session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(engine, "_execute_tool", _fake_execute)
    session = _new_session(db_session)
    session.persona = "pentest"
    db_session.commit()
    # 只做了 1 个方向就交卷：打回；补到 3 个方向后才接受。
    gateway = _Gateway([_PROBE_1, _RESULT, _PROBE_2, _PROBE_3, _RESULT])

    await engine.run_turn(
        db_session,
        session=session,
        content="开始评估",
        identity=ADMIN,
        gateway=gateway,
    )

    assert gateway.calls == 5
    assert "现在还不能交付" in _all_text(gateway.seen[2])
    assert "只完成了 1 个" in _all_text(gateway.seen[2])


async def test_auth_only_final_is_bounced(
    db_session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(engine, "_execute_tool", _fake_execute)
    session = _new_session(db_session)
    # 6 次认证后马上交卷：必须打回；补 3 个方向后才接受。
    gateway = _Gateway([_API] * 6 + [_RESULT, _PROBE_1, _PROBE_2, _PROBE_3, _RESULT])

    await engine.run_turn(
        db_session,
        session=session,
        content="开始评估",
        identity=ADMIN,
        gateway=gateway,
    )

    assert gateway.calls == 11, "结果块应被打回一次，补完 3 个方向后才收尾"
    assert "现在还不能交付" in _all_text(gateway.seen[7])
    assert "覆盖不足" in _all_text(gateway.seen[7])
    stored = service.list_messages(db_session, session=session)
    assert not any("已停止：本轮" in (m.content or "") for m in stored)


def test_pivot_reminder_text_is_not_a_stop_message() -> None:
    text = render_strategy_pivot_reminder(10)
    assert "不是收工信号" in text
    assert "至少选" in text


def test_bearer_token_is_not_mistaken_for_password_bruteforce() -> None:
    """认证后的正常 API 请求不能被认证停损器当成爆破。"""
    assert not engine._looks_like_auth_attempt(
        "run_shell",
        {"command": 'curl -H "Authorization: Bearer svx_testtoken" http://127.0.0.1:8080/api/v1/settings'},
    )
    assert not engine._looks_like_auth_attempt(
        "run_shell",
        {"command": "curl -H 'X-Auth-Token: svx_testtoken' http://127.0.0.1:8080/api/v1/credentials"},
    )
    assert engine._looks_like_auth_attempt(
        "run_shell",
        {"command": "curl -X POST http://127.0.0.1:8080/api/v1/auth/login -d 'password=x'"},
    )
    assert engine._looks_like_auth_attempt(
        "verify_http",
        {"url": "http://127.0.0.1:8080/api/v1/login", "body": "user=a&password=b"},
    )
