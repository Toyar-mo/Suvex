"""黑板索引块非空时，root 每轮拿到的那句「怎么用」引导（v0.80 补齐）。

## 这一组在钉什么

v0.77 的实测推翻了"把规矩写进静态提示词就够了"：`add_fact` 被调 **0 次**，
`record_coverage` **2 次**。模型每轮都看得到对话历史，所以不觉得自己需要
记账 —— 直到历史被裁掉已经走远。

所以 v0.80 改成「系统替它记 + 每轮推给它」。而"推给它"如果只是一段没人读的
索引，就等于没推。这一句引导是**每轮都在、紧挨着黑板内容**的那句，它的
位置就是它的全部价值 —— 所以它必须由 `render_live_state` 出，不能只写在
`kernel_pentest.md` 里。

## 反面同样重要

索引块为空时**一个字节都不能多**。`test_live_state.py` 里那组空气状态断言
已经钉着这条，这里再钉一次是因为新加的文本最容易犯的就是这个错
（把"提醒"写成无条件追加）。
"""

from __future__ import annotations

import inspect

from app.kernel import multiagent
from app.kernel.multiagent import FACT_INDEX_NUDGE, SharedState

#: 任意的非空索引块 —— 内容不重要，只要"有"。
INDEX = "## 项目黑板（持久，含上次扫描的结论）\n- [confirmed] ruled-out/x-y：试过不行"


class TestNudgePresence:
    def test_absent_when_index_empty(self):
        """空索引块 → 结果里不许出现引导的任何一句。

        逐句检查而不是只查 "record_fact"：以后有人把这句改短/改写，
        这条断言仍然有效（只要它还在提这两个工具之一就算漏）。
        """
        out = SharedState().render_live_state(turn=1, max_turns=50, fact_index="")
        assert FACT_INDEX_NUDGE not in out
        for needle in ("record_fact", "get_scan_fact", "换参数"):
            assert needle not in out, out

    def test_empty_index_renders_nothing_at_all(self):
        """空黑板 + 空状态 → 空串（连"第 1/50 轮"都不许有）。"""
        assert SharedState().render_live_state(turn=1, max_turns=50, fact_index="") == ""

    def test_present_when_index_non_empty(self):
        out = SharedState().render_live_state(turn=1, max_turns=50, fact_index=INDEX)
        assert FACT_INDEX_NUDGE in out

    def test_names_both_next_actions(self):
        """两个动作必须都提到，缺一个这个功能就断了。

        - `record_fact`：不写进来，下一轮的黑板永远是空的
          （v0.77 的 `add_fact=0` 正是这个形状）；
        - `get_scan_fact`：不提它，模型只能拿 80 字摘要当全文，
          那正是 `INDEX_FOOTER` 里"禁止凭摘要臆造细节"要防的。

        ## 为什么断言 `FACT_INDEX_NUDGE` 而不是整份渲染结果

        第一版写的是 `assert "record_fact" in out`（out = 整份渲染）—— 结果
        反向验证把它改成 `record` 之后**测试照样绿**：因为别处也提到这个名字，
        整份输出里那个单词并未消失。**那钉的是"整份提示词里出现过这个词"，
        不是"引导句点了两个动作"**。所以钉常量本身。
        """
        assert "record_fact" in FACT_INDEX_NUDGE
        assert "get_scan_fact" in FACT_INDEX_NUDGE

    def test_tells_how_to_interpret_ruled_out(self):
        """提醒的核心语义：换了参数不算新尝试（v0.77 bb29546 那条纪律）。"""
        assert "换参数" in FACT_INDEX_NUDGE


class TestNudgeIsPerTurn:
    def test_present_on_every_turn(self):
        """**每轮**都在 —— 只在第一轮说一次等于没说过。

        这条是这一版的全部理由：静态提示词（第 1 轮就有、之后每轮也一样）
        被当成背景音，模型看不见就想不到。
        """
        for turn in (1, 2, 5, 40, 50):
            out = SharedState().render_live_state(turn=turn, max_turns=50, fact_index=INDEX)
            assert FACT_INDEX_NUDGE in out, f"第 {turn} 轮缺引导"

    def test_unchanged_when_state_grows(self):
        """引导是常量，不随状态变化 —— 否则同一轮内两次渲染不同，
        提示词前缀会抖（长扫描里那是真金白银）。"""
        st1 = SharedState()
        st2 = SharedState()
        st2.write_note("一条自由笔记", "内容", by="root")
        a = st1.render_live_state(turn=2, max_turns=50, fact_index=INDEX)
        b = st2.render_live_state(turn=2, max_turns=50, fact_index=INDEX)
        assert FACT_INDEX_NUDGE in a
        assert FACT_INDEX_NUDGE in b
        # 引导那一整句在两份渲染里逐字节相同
        tail = FACT_INDEX_NUDGE.splitlines()[0]
        assert a.count(tail) == b.count(tail) == 1


class TestWiring:
    def test_nudge_computed_after_empty_check(self):
        """源码级守卫：引导必须加在 `if not parts: return ""` 之后。

        为什么用源码断言而不是跑一遍：`render_live_state` 有两个调用方，
        空状态那条由 `test_empty_index_renders_nothing_at_all` 覆盖；
        这里钉的是"以后有人把这段挪到前面去"这种改动 —— 那种改动会让
        空黑板凭空多出几百字节，而单测很可能恰好没覆盖到那个分支。
        """
        src = inspect.getsource(multiagent.SharedState.render_live_state)
        i_empty = src.index("if not parts:")
        i_nudge = src.index("parts.append(FACT_INDEX_NUDGE)")
        assert i_empty < i_nudge, "引导必须在 `if not parts: return` 之后追加"
