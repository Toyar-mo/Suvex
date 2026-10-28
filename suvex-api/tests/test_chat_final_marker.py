"""对话提示词里必须真的教会模型 `FINAL:` 这个终止符。

## 为什么单独测这一条

v1.19 把「什么时候该收尾」从"模型自己觉得说完了"改成"模型显式写 `FINAL:`"。
模型不认识这个记号的话，这条协议就是**静默失效**：它会继续写普通正文，
我们每次都把它当成"话没说完"推一次 —— 用户看到的是答案被追着要，
成本还翻倍。本项目已经反复吃过"配上了却没人读"的亏（`default_limitation`
只进 API 不进提示词、`__APP_VERSION__` 声明了零处使用），所以这里把
"提示词里必须有这段、且必须给出可照抄的样例"钉死。

断言刻意用**结构化特征**而不是词频：
- `FINAL:` 必须以行首形式出现（模型是照着行的形状模仿的）；
- 必须同时说明"不需要写"的场合（纯聊天），否则模型会给每句寒暄都写 FINAL，
  等于把刚省下来的那一轮又还回去。
"""

from __future__ import annotations

from app.chat.prompts import build_system_prompt


def test_system_prompt_teaches_the_final_marker() -> None:
    prompt = build_system_prompt()

    assert "\nFINAL:" in prompt, (
        "对话提示词里没有以行首 `FINAL:` 开头、可照抄的结论样例 —— 模型不会"
        "自己发明这个终止符，于是我们每轮都会误判为「话没说完」。"
    )
    assert "`FINAL:`" in prompt, "提示词里没有把 FINAL: 作为记号解释给模型"


def test_system_prompt_says_plain_chat_needs_no_marker() -> None:
    prompt = build_system_prompt()

    assert "没动过工具" in prompt, (
        "提示词没说「纯聊天不需要写 FINAL」，模型会给每句寒暄也补一个 FINAL；"
        "而我们又不能对零工具轮次强求，结果就是白多一轮。"
    )
