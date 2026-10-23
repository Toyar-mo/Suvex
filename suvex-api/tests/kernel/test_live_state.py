"""给 v0.79 实验 A（强制注入 live state）加测试。

钉住四件事，任何一件被改回去都要变红：
1. 空状态返回**空串**（不是只有回合数的壳子）；
2. 有状态时包含三段（当前方向 / 已确认事实 / 已测位置）；
3. negative 事实排在 positive 前面（截断时优先保留已排除方向）；
4. **接线**：root 的 system prompt 真的每轮带上了它，且子 agent 不带。
"""

from __future__ import annotations

import inspect

from app.kernel import multiagent
from app.kernel.multiagent import (
    LIVE_STATE_MAX_CHARS,
    SharedState,
)


def _populated() -> SharedState:
    s = SharedState()
    s.add_idea("测 BOLA：用 viewer 身份跑 admin 写接口", by="root")
    s.resolve_idea("测 BOLA", "dropped", by="root", why="401 先于对象存在性")
    s.add_fact("鉴权是 Cookie 会话", by="root", kind="positive", evidence="set-cookie")
    s.add_fact("settings token 是前端 mock", by="root", kind="negative", evidence="bundle xxxx")
    s.record_coverage("/api/v1/scans", "高", "401 未认证", by="root")
    return s


class TestEmptyState:
    def test_empty_state_returns_empty_string(self):
        """空状态必须一个字节都不加。

        ⚠️ 这条是第一版真实漏掉的：回合位置那行首轮就有，让它进 `parts`
        会让 `if not parts` 永远为假，于是空状态返回 48 字符的壳子。
        """
        assert SharedState().render_live_state(turn=0, max_turns=120) == ""

    def test_empty_state_is_empty_even_without_turn_info(self):
        assert SharedState().render_live_state() == ""


class TestPopulatedState:
    def test_all_three_sections_present(self):
        r = _populated().render_live_state(turn=5, max_turns=120)
        assert "## 已确认事实" in r
        assert "## 已测位置" in r

    def test_idea_section_only_when_live(self):
        s = SharedState()
        s.add_fact("只记一条事实", by="root")
        r = s.render_live_state(turn=1, max_turns=10)
        assert "## 当前方向" not in r
        assert "## 已确认事实" in r

    def test_turn_position_present_but_not_alone(self):
        r = _populated().render_live_state(turn=7, max_turns=120)
        assert "第 7/120 轮" in r and "剩 113 轮" in r

    def test_header_says_it_is_pushed_by_system(self):
        """必须说清"这不是你的记忆" —— 否则模型以为是它自己记的。"""
        r = _populated().render_live_state(turn=3, max_turns=10)
        assert "系统每轮推送" in r


class TestNegativeFirst:
    def test_negative_facts_precede_positive(self):
        r = _populated().render_live_state(turn=2, max_turns=10)
        block = r.split("## 已确认事实")[1]
        assert block.index("[已排除]") < block.index("[成立]")

    def test_truncation_keeps_negative(self):
        """撑爆上限后，最先被留下的仍应是已排除方向。"""
        s = SharedState()
        s.add_fact("已排除的第一条", by="root", kind="negative", evidence="e")
        for i in range(80):
            s.add_fact(f"很长的成立事实 {i} " + "填充" * 40, by="root", kind="positive")
        r = s.render_live_state(turn=1, max_turns=10)
        assert len(r) <= LIVE_STATE_MAX_CHARS + 80
        assert "已排除的第一条" in r
        assert "更多记录请调用 get_memory" in r


class TestWellFormed:
    def test_respects_budget(self):
        s = SharedState()
        for i in range(200):
            s.add_fact(f"事实 {i} " + "x" * 100, by="root")
        r = s.render_live_state(turn=1, max_turns=10)
        assert len(r) <= LIVE_STATE_MAX_CHARS + 80


class TestWiring:
    def test_live_state_is_injected_into_root_system_prompt(self):
        src = inspect.getsource(multiagent.run_agent_loop)
        assert "render_live_state(" in src
        # 必须是拼进 system= 的那份，而不是只塞进 recent
        assert "_sys = system_prompt" in src
        assert "system=_sys" in src

    def test_only_root_gets_it(self):
        """注入必须挂在 `is_root` 分支里 —— 子 agent 不该收到全场状态。

        断言方式：取 `render_live_state(` 往前到最近的 `for turn` 循环体开头，
        确认这段里 `if is_root:` 是它的**直接**守卫（比"全文某处有 is_root"严）。
        """
        src = inspect.getsource(multiagent.run_agent_loop)
        i = src.index("render_live_state(")
        # 往前找最近的 if is_root: —— 中间不能有别的同级 if
        head = src[:i]
        last_guard = head.rfind("if is_root:")
        assert last_guard != -1
        guard_block = head[last_guard:i]
        # 守卫到注入点之间不应再出现"另一个 if"（否则说明隔着别的分支）
        assert guard_block.count("if ") == 1, guard_block
