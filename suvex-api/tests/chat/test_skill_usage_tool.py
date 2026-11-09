"""`list_skills(includeUsage=true)` 与「问事实别做项目」的提示词（v1.79）。

## 为什么值得单独测

2026-11-09 的一条真实会话：用户问「现在手上有哪些技能？哪几篇没被用过？」，
模型先调了两次 `list_skills`（不含用量），只好转去用 `run_shell` 翻沙箱目录、
扫事件表、自己写解析脚本 —— 一轮跑了 **70 步 / 53 次 shell / 32 分钟**，
最后撞上模型接入点的 500。

两处修复各自钉一条：

1. `list_skills(includeUsage=true)` 必须直接在清单里给出「被加载 N 次」，
   并有"多少篇从未被加载"的汇总 —— 这样这个问题一次调用就能答。
2. 系统提示词必须明确「问现状用一段话回答，不要产出交付物」，
   否则模型会把"回答一个问题"扩写成"替用户完成一个项目"。

⚠️ 默认（不带 includeUsage）的输出**不能**变：它是"我有哪些技能"的清单，
顺带扫 2 万条事件是纯浪费。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from app.chat import engine
from app.chat import service as chat_service
from app.chat.prompts import build_system_prompt
from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.core.vocab import AgentPhase
from app.events.models import ActivityEvent
from app.kernel import skills as skills_mod
from tests.factories import make_agent, make_scan
from tests.test_chat import ADMIN


def _emit_load(db: Session, *, scan_id: str, agent_id: str, name: str) -> None:
    db.add(
        ActivityEvent(
            id=new_id("evt"),
            scan_id=scan_id,
            agent_id=agent_id,
            agent_phase=AgentPhase.SCAN.value,
            type="tool_call",
            summary=f"加载技能 {name}",
            detail={"kind": "tool_call", "tool": "load_skill", "command": json.dumps({"name": name})},
            at=now_utc(),
        )
    )
    db.commit()


def _ctx(db: Session):  # noqa: ANN202 - 测试内部小工具
    session = chat_service.create_session(db, created_by="user_admin")
    db.commit()
    return engine._ToolContext(db=db, session=session, identity=ADMIN)


async def test_list_skills_can_report_usage_in_one_call(db_session: Session) -> None:
    """★ 一次调用就能回答「哪些没用过」，不需要 run_shell 翻目录。"""
    scan = make_scan(db_session)
    agent = make_agent(db_session, scan.id)
    names = [skill.name for skill in skills_mod.discover_skills()]
    assert names, "测试环境里读不到任何技能包"
    loaded = names[0]
    _emit_load(db_session, scan_id=scan.id, agent_id=agent.id, name=loaded)
    _emit_load(db_session, scan_id=scan.id, agent_id=agent.id, name=loaded)

    out = await engine._tool_list_skills(_ctx(db_session), {"includeUsage": True})

    assert f"`{loaded}`" in out
    assert "被加载 2 次" in out
    # 汇总必须说清"多少篇从未被加载"——那正是用户问的第二半
    assert "从未被加载" in out
    assert "统计口径" in out


async def test_list_skills_without_usage_does_not_scan_events(db_session: Session) -> None:
    """默认输出保持原样：不带用量、不提统计。"""
    out = await engine._tool_list_skills(_ctx(db_session), {})
    assert "被加载" not in out
    assert "统计口径" not in out


def test_system_prompt_separates_questions_from_deliverables() -> None:
    """★ 提示词必须写明：问现状就一段话回答，不要产出文件/报告。"""
    prompt = build_system_prompt()
    assert "问事实，还是做任务" in prompt
    assert "不要为此创建文件" in prompt
    assert "/work/persist/" in prompt
    # 还要指路到能一次答完的工具，否则模型又会去 run_shell 翻目录
    assert "includeUsage=true" in prompt


@pytest.mark.parametrize("native", [False, True])
def test_qa_rule_survives_both_tool_protocols(native: bool) -> None:
    """原生工具调用与 ACTION: 文本协议共用外层模板 —— 两种模式都要带上。"""
    prompt = build_system_prompt(native_tools=native)
    assert "问事实，还是做任务" in prompt
