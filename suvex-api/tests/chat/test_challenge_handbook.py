"""绑定题目的对话要把题型手册正文注入提示词。

与解题侧同一条实测：RE5/RE6 各 500 轮、`load_skill` 0 次 —— 只给清单，
模型不会主动拉。对话绑定题目时同样直接给正文（前 8KB），普通对话不受影响。
"""

from __future__ import annotations

from app.chat.prompts import build_system_prompt


def test_challenge_category_injects_handbook() -> None:
    prompt = build_system_prompt(challenge_category="REVRESE")
    assert "本题型手册：ctf-reverse" in prompt
    assert "# CTF Reverse Engineering" in prompt


def test_plain_chat_has_no_handbook() -> None:
    assert "本题型手册" not in build_system_prompt()
