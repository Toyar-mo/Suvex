"""第七节「动机」的针对性测试。

## 这一组在钉什么

问题是**具体的**：一次 80 轮的真实评估里，`/api/v1/scans` 被探了 13 次、
`/api/v1/auth/login` 12 次 —— 都是换参数，打的是同一个点。第六节 6.2.1.1
已经写清了「不许重复」，但**它没有回答"那我该干什么"**。

所以这一节不是"再强调一遍不要重复"（那是复读），而是给出**能立刻执行的
替代动作**（7.1 每轮自问 / 7.2 不停在第一层 / 7.3 横纵两向 / 7.5 三条自检）。

## 反面同样重要：不许出现"没有动作的激励"

CyberStrikeAI 原文那种写法（"火力全开"、"你是最先进的安全代理体系"）
在本项目的实测结论下是**有害的**：P2 那轮失败的形态正是"方向走窄、
80 轮耗尽"，加这类话只会让它更用力地打同一个点。所以这里的测试
**专门钉住那几句不许出现** —— 这是主动拒绝，不是遗漏。
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "kernel_pentest.md"


@pytest.fixture(scope="module")
def text() -> str:
    return PROMPT.read_text(encoding="utf-8")


class TestSectionExists:
    def test_has_motivation_section(self, text: str) -> None:
        assert "## 七、动机" in text

    def test_all_five_subsections(self, text: str) -> None:
        for h in ("7.1", "7.2", "7.3", "7.4", "7.5"):
            assert f"### {h} " in text, f"缺小节 {h}"


class TestBudgetIsConcrete:
    def test_states_the_round_budget(self, text: str) -> None:
        """激励要落在真实数字上 —— "尽力"没有数字就是空话。

        80 是 `MultiAgentSettings.max_turns` 的默认值（`app/settings/models.py:574`）。
        """
        assert "80 轮" in text

    def test_every_round_costs(self, text: str) -> None:
        """把轮次讲成**花掉的钱**，而不是"还有余量"。"""
        assert "花掉的钱" in text or "花掉的预算" in text


class TestGivesActionsNotCheers:
    def test_asks_the_per_round_question(self, text: str) -> None:
        """7.1 的核心：一个可以每轮问自己的具体句子。"""
        assert "这一轮，我推进了什么" in text

    def test_names_the_four_direction_changes_by_reference(self, text: str) -> None:
        """换方向不能重新发明一套 —— 必须指回 6.2.1.1 的四个方向。

        这里不查四个方向的字面（那在 6.2.1.1 已经有测试钉着，
        复读会让两处漂移），只查**引用关系存在**。
        """
        assert "6.2.1.1" in text

    def test_explains_first_wall_is_normal(self, text: str) -> None:
        """7.2：打不穿第一个坑是常态，不是收工信号。"""
        assert "打不穿第一个坑是正常起点" in text

    def test_has_periodic_selfcheck(self, text: str) -> None:
        """7.5：每 10 轮一次自检，要有一个明确的时间尺度。"""
        assert "每 10 轮" in text

    def test_selfcheck_has_three_questions(self, text: str) -> None:
        """三条自检都必须可判定（是/否、次数、能不能交付）。"""
        for needle in ("同一个吗", "推进了几次结论状态", "交得出东西吗"):
            assert needle in text, f"自检缺一条：{needle}"

    def test_keeps_evidence_discipline_primary(self, text: str) -> None:
        """激励不能压过证据纪律 —— 7.4 必须明确"宁可写得少"。"""
        assert "宁可写得少" in text


class TestRejectsEmptyCheerleading:
    """不许出现**没有动作**的强激励措辞 —— 这是主动的否定清单。

    每条失败信息都写清了"为什么不要"：将来有人想加回来，
    会在这里先被拦一次，被迫解释它的动作是什么。
    """

    FORBIDDEN = [
        ("火力全开", "没说清「全开」该开到哪里；实测的失败形态正是「更用力打同一个点」"),
        ("绝不偷懒", "是态度词，不改变行为 —— 换成 7.1 的『这一轮推进了什么』"),
        ("最先进", "自我抬举，对判据没有任何帮助"),
        ("赚到 $", "本项目是授权评估，不是赏金平台 —— 引入错误的价值函数"),
        ("无情", "与 6.4『宁可写得少不要写得虚』直接冲突"),
    ]

    def test_no_empty_cheerleading(self, text: str) -> None:
        """检查的是**指导正文**，不是「说明为什么不用它」的引用块。

        §7 开头有一段明确写着「CyberStrikeAI 用了『火力全开』这类措辞，
        我们换支撑点」—— 那是在**讨论**这个词，不是**使用**它。所以先把
        `>` 开头的引用行剔除，再查。这不是给豁免，而是把"提到"和"当成
        指导"区分开 —— 不区分的话，任何反对性的说明都没法写。
        """
        body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith(">"))
        hits = [(w, why) for w, why in self.FORBIDDEN if w in body]
        assert not hits, "出现了没有动作的激励措辞：" + "; ".join(f"{w}（{why}）" for w, why in hits)

    def test_does_not_copy_authorization_blanket_clause(self, text: str) -> None:
        """CyberStrikeAI 的「不得质疑、辩论、索取或核实是否有权执行」不适用。

        本项目有 HITL / 授权边界设计（破坏性操作需要理由，见 6.3），
        抄这段会把**已有的安全设计**推翻。
        """
        assert "不得质疑" not in text
