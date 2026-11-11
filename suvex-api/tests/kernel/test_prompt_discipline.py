"""执行纪律必须真的在提示词里（不是只写在文档里）。

来源：RE5/RE6 各跑满 500 轮、`FINAL.candidates` 为空。四条纪律针对的是
已知失败模式：只给建议不执行、没把握就不交候选、关键中间结果随上下文滚掉。

扫描路径与 CTF 解题共用 `kernel_system.md`；对话是独立的
`app.chat.prompts.SYSTEM_PROMPT_TEMPLATE`，所以两处都要钉。
"""

from __future__ import annotations

from app.chat import nudge
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
    for token in (
        "交付结果怎么给（所有",
        "扫描 / 渗透",
        "文件 / 逆向 / 取证",
        "产物",
        "未完成",
        "口令 / 登录尝试不是整场任务",
        "一个策略失败不是收工信号",
        "至少完成 3 个不同方向",
        "认证旁路/越权",
        "rockyou",
    ):
        assert token in text
    assert chat_prompts.BRUTE_HARD_LIMIT <= 20
    blocked = chat_prompts.render_auth_attempt_blocked_note(chat_prompts.BRUTE_HARD_LIMIT)
    assert "失败：" in blocked and "换一个策略族" in blocked
    pivot = chat_prompts.render_strategy_pivot_reminder(chat_prompts.BRUTE_HARD_LIMIT)
    assert "不是收工信号" in pivot
    assert "已停止：本轮" not in pivot
    assert "只写一次" in text
    assert "继续 / 接着 / 往下" in text


def test_pentest_persona_carries_persistence_discipline() -> None:
    """专门渗透人格必须带上“失败不是收工”的循环，而不只是一句风格描述。"""
    prompt = chat_prompts.build_system_prompt(persona="pentest")
    for token in (
        "失败是数据，不是收工信号",
        "认证/口令是一个策略族",
        "至少覆盖 3 个不同策略族",
        "试到第三次就换",
        "只试了认证，正确答案是继续",
    ):
        assert token in prompt


def test_result_block_is_final_even_with_limitations() -> None:
    """「结果」块里的未完成/限制是边界，不是继续计划。"""
    assert nudge.looks_like_result("结果\n- 目标：x\n- 未完成：y")
    assert nudge.looks_like_result("  ## 结果\n- 结论：ok")
    assert not nudge.looks_like_result("我先继续查一下")

