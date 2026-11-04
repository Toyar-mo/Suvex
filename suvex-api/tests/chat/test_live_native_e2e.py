r"""真实模型 E2E（手动运行，不进默认套件 —— 需要网络+密钥）。

跑法: PYTHONPATH=. ./.venv/Scripts/python.exe -m pytest tests/chat/test_live_native_e2e.py -q -o addopts= -p no:cacheprovider -s
密钥从 C:\Users\Administrator\Desktop\新项目\abase_wsl.txt 读（base|model|key）。
"""
from __future__ import annotations
import json, os
import pytest
from sqlalchemy.orm import Session

from app.chat import engine, service as chat_service
from app.kernel.runner import LlmGateway
from tests.test_chat import ADMIN

KEYFILE = r"C:\Users\Administrator\Desktop\新项目\abase_wsl.txt"

def _gw():
    base, model, key = open(KEYFILE, encoding="utf-8").read().split("|")
    return LlmGateway(model=model, api_key=key, base_url=base)

@pytest.mark.skipif(not os.path.exists(KEYFILE), reason="无本地密钥文件")
async def test_live_run_turn_executes_native_tools_and_ends_cleanly(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()

    executed: list[tuple] = []

    async def fake_exec(ctx, name, args):  # noqa: ANN001, ANN202
        executed.append((name, dict(args)))
        return "written: /tmp/a.txt (13 bytes)", False

    monkeypatch.setattr(engine, "_execute_tool", fake_exec)

    result = await engine.run_turn(
        db_session,
        session=session,
        content="用 run_shell 两步：先 echo hi > /tmp/a.txt，再 cat /tmp/a.txt。分步做。",
        identity=ADMIN,
        gateway=_gw(),
    )

    print("\n[E2E] executed tool calls:", executed)
    print("[E2E] message roles:", [(m.role, m.surface) for m in result.messages])
    for m in result.messages:
        joined = m.content or ""
        assert "result_note" not in joined, f"泄漏 result_note: {joined[:120]}"
        assert "</parameter>" not in joined, f"泄漏 parameter: {joined[:120]}"
    # 至少跑过一次 run_shell（原生调用被真的执行）
    assert any(n == "run_shell" for n, _ in executed), f"没执行 run_shell: {executed}"
    # 最后一条是 assistant 可见答案
    assert result.messages[-1].role == "assistant"
    print("[E2E] final answer head:", (result.messages[-1].content or "")[:200])
