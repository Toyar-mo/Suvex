"""执行纪律必须真的在提示词里（不是只写在文档里）。

来源：RE5/RE6 各跑满 500 轮、`FINAL.candidates` 为空。四条纪律针对的是
已知失败模式：只给建议不执行、没把握就不交候选、关键中间结果随上下文滚掉。

扫描路径与 CTF 解题共用 `kernel_system.md`；对话是独立的
`app.chat.prompts.SYSTEM_PROMPT_TEMPLATE`，所以两处都要钉。
"""

from __future__ import annotations

from app.chat import prompts as chat_prompts
from app.kernel import runner


def test_kernel_prompt_has_authorization_and_submission_discipline() -> None:
    text = runner.PROMPT_PATH.read_text(encoding="utf-8")
    assert "授权与执行纪律" in text
    assert "不要自行判死" in text
    assert "/work/persist" in text


def test_chat_prompt_has_task_discipline() -> None:
    text = chat_prompts.SYSTEM_PROMPT_TEMPLATE
    assert "执行任务时的纪律" in text
    assert "不自行判死" in text
    assert "/work/persist" in text


def test_all_deliverable_tasks_must_end_with_a_result_block() -> None:
    """所有"做事"任务都必须先给可核对的结果块，不能只补登录场景。"""
    text = chat_prompts.SYSTEM_PROMPT_TEMPLATE
    for token in ("交付结果怎么给（所有", "扫描 / 渗透", "文件 / 逆向 / 取证", "产物", "未完成"):
        assert token in text

