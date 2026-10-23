"""Idea / Memory 二分（v0.75）的回归测试。

保护的核心是**上下文信噪比**：一个还活着的方向和一个已经废弃的方向，
在提示里长得一样，模型就会反复去试死路。

两条最贵的不变量，各有一个反向验证点：
1. 废弃必须给原因（`why`）——没有原因的废弃等于没记；
2. `render_ideas()` 只列活着的 —— 死掉的必须离开提示。
"""

from __future__ import annotations

import pytest

from app.kernel import multiagent as ma


def _state() -> ma.SharedState:
    return ma.SharedState()


class TestAddIdea:
    def test_new_idea_is_open(self) -> None:
        st = _state()
        rec = st.add_idea("试试 SPA chunk 里的路由表", by="root")
        assert rec["status"] == "open"
        assert rec["by"] == "root"

    def test_same_text_does_not_duplicate(self) -> None:
        st = _state()
        st.add_idea("同一句话", by="root")
        st.add_idea("同一句话", by="a1")
        assert len(st.ideas) == 1

    def test_blank_rejected(self) -> None:
        st = _state()
        with pytest.raises(ValueError):
            st.add_idea("   ", by="root")

    def test_long_text_truncated_but_id_usable(self) -> None:
        st = _state()
        rec = st.add_idea("x" * 1000, by="root")
        assert len(rec["text"]) == 400
        # id 是前 60 字，仍可用前缀匹配
        assert len(next(iter(st.ideas))) == 60


class TestResolveIdea:
    def test_dropped_requires_why(self) -> None:
        """★★ 没有原因的废弃等于没记。

        反向验证：把 `if status == "dropped" and not why.strip()` 整段删掉，
        本条立刻 FAILED —— 而"我们试过了不行"正是最容易失真的记忆
        （下次没人知道到底试了什么，会重走一遍）。
        """
        st = _state()
        st.add_idea("试试 SPA chunk", by="root")
        with pytest.raises(ValueError, match="why"):
            st.resolve_idea("试试 SPA chunk", "dropped", by="root")
        # 给了原因就能过
        rec = st.resolve_idea("试试 SPA chunk", "dropped", by="root", why="chunk 里只有 i18n")
        assert rec["status"] == "dropped"
        assert rec["why"] == "chunk 里只有 i18n"

    def test_done_requires_evidence(self) -> None:
        st = _state()
        st.add_idea("验证越权", by="root")
        with pytest.raises(ValueError, match="evidence"):
            st.resolve_idea("验证越权", "done", by="root")
        rec = st.resolve_idea("验证越权", "done", by="root", evidence="GET /users → 200")
        assert rec["evidence"] == "GET /users → 200"

    def test_prefix_match_must_be_unique(self) -> None:
        st = _state()
        st.add_idea("试试 SPA chunk A", by="root")
        st.add_idea("试试 SPA chunk B", by="root")
        with pytest.raises(ValueError, match="唯一"):
            st.resolve_idea("试试 SPA chunk", "doing", by="root")
        # 唯一前缀可以
        st.resolve_idea("试试 SPA chunk A", "doing", by="root")

    def test_unknown_status_rejected(self) -> None:
        st = _state()
        st.add_idea("x", by="root")
        with pytest.raises(ValueError, match="状态"):
            st.resolve_idea("x", "maybe", by="root")

    def test_unknown_id_rejected(self) -> None:
        st = _state()
        with pytest.raises(ValueError, match="没有匹配"):
            st.resolve_idea("不存在的方向", "doing", by="root")

    def test_by_chain_records_history(self) -> None:
        # "谁改成谁" 比 "最后一个是谁" 有用（不抹历史的最小形式）。
        st = _state()
        st.add_idea("x", by="root")
        st.resolve_idea("x", "doing", by="a1")
        assert st.ideas["x"]["by"] == "root → a1"


class TestRenderIdeas:
    def test_only_live_ideas_are_rendered(self) -> None:
        """★★ 死掉的 Idea 必须离开提示。

        反向验证：把 `render_ideas` 里的过滤条件改成"全部"，本条立刻 FAILED。
        这条是 Idea/Memory 二分的**全部收益**所在：上下文不再被已完成的
        和已废弃的方向稀释。
        """
        st = _state()
        st.add_idea("活着的方向", by="root")
        st.add_idea("做完的方向", by="root")
        st.add_idea("废弃的方向", by="root")
        st.resolve_idea("做完的方向", "done", by="root", evidence="拿到了")
        st.resolve_idea("废弃的方向", "dropped", by="root", why="行不通")

        rendered = st.render_ideas()
        assert "活着的方向" in rendered
        assert "做完的方向" not in rendered
        assert "废弃的方向" not in rendered

    def test_doing_is_still_live(self) -> None:
        st = _state()
        st.add_idea("在做的", by="root")
        st.resolve_idea("在做的", "doing", by="root")
        assert "在做的" in st.render_ideas()

    def test_empty_message(self) -> None:
        assert "没有" in _state().render_ideas()


class TestAddFact:
    def test_negative_fact_accepted(self) -> None:
        # "已排除"和"成立"同等重要 —— 它防止重走死路。
        st = _state()
        st.add_fact("alg:none 被拒", by="a1", evidence="401", kind="negative")
        assert st.facts["alg:none 被拒"]["kind"] == "negative"

    def test_unknown_kind_rejected(self) -> None:
        st = _state()
        with pytest.raises(ValueError, match="事实类型"):
            st.add_fact("x", by="a1", kind="maybe")

    def test_blank_rejected(self) -> None:
        st = _state()
        with pytest.raises(ValueError):
            st.add_fact("  ", by="a1")

    def test_same_text_overwrites_with_new_evidence(self) -> None:
        st = _state()
        st.add_fact("CORS 反射", by="a1", evidence="旧证据")
        st.add_fact("CORS 反射", by="a2", evidence="新证据")
        assert len(st.facts) == 1
        assert st.facts["CORS 反射"]["evidence"] == "新证据"

    def test_render_shows_kind_and_evidence(self) -> None:
        st = _state()
        st.add_fact("alg:none 被拒", by="a1", evidence="401", kind="negative")
        st.add_fact("CORS 反射任意 Origin", by="root", evidence="ACAO 回显")
        rendered = st.render_facts()
        assert "[已排除]" in rendered
        assert "[成立]" in rendered
        assert "ACAO 回显" in rendered


class TestToolsAreWired:
    """工具名必须真的注册进 root/子 agent 的可见集合。

    这一组专门防 v0.72 那个 `mcp_servers` 事故的形状：
    **配了但不生效**。新工具写好了、handler 也写了，但只要没进 `_SHARED`，
    模型就永远调不到它 —— 而单测如果只测 handler，会全绿。
    """

    @pytest.mark.parametrize(
        "name", [ma.IDEA_ADD, ma.IDEA_RESOLVE, ma.MEMORY_ADD, ma.MEMORY_GET]
    )
    def test_registered_in_shared(self, name: str) -> None:
        assert name in ma._SHARED, f"{name} 没进 _SHARED，模型调不到"

    @pytest.mark.parametrize(
        "name", [ma.IDEA_ADD, ma.IDEA_RESOLVE, ma.MEMORY_ADD, ma.MEMORY_GET]
    )
    def test_visible_to_root_and_child(self, name: str) -> None:
        assert name in ma.root_tool_names()
        assert name in ma.child_tool_names()


class TestDispatch:
    """三个新工具走一遍真实分派入口（含错误分支要返回 JSON 而不是抛异常）。"""

    def _node(self) -> ma.AgentNode:
        return ma.AgentNode(agent_id="root", name="root", task="t", parent_id=None)

    def _coord(self) -> ma.AgentCoordinator:
        return ma.AgentCoordinator(total_budget=80)

    def test_add_idea_via_dispatch(self) -> None:
        import asyncio

        coord, node = self._coord(), self._node()
        out = asyncio.run(
            ma.dispatch_orchestration_tool(
                ma.IDEA_ADD,
                {"text": "试试 /mcp"},
                coord=coord,
                me=node,
                is_root=True,
                spawner=None,
                sandbox=None,
                http=None,
                spec=None,
            )
        )
        assert '"status": "open"' in out

    def test_resolve_dropped_without_why_returns_error_json(self) -> None:
        """错误分支必须返回 `_json_err` 而不是抛 —— 抛出去会让整轮崩掉。"""
        import asyncio

        coord, node = self._coord(), self._node()
        coord.shared.add_idea("x", by="root")
        out = asyncio.run(
            ma.dispatch_orchestration_tool(
                ma.IDEA_RESOLVE,
                {"idea_id": "x", "status": "dropped"},
                coord=coord,
                me=node,
                is_root=True,
                spawner=None,
                sandbox=None,
                http=None,
                spec=None,
            )
        )
        assert '"success": false' in out
        assert "why" in out

    def test_get_memory_returns_two_separate_columns(self) -> None:
        """★★ 两栏必须分开返回，不能拼成一段文本。

        拼在一起就退回成了 notes —— Idea/Memory 二分的意义就是让读的人
        （模型）一眼分辨"哪条是猜想、哪条是事实"。
        """
        import asyncio

        coord, node = self._coord(), self._node()
        coord.shared.add_idea("待试 X", by="root")
        coord.shared.add_fact("确认 Y", by="root")
        out = asyncio.run(
            ma.dispatch_orchestration_tool(
                ma.MEMORY_GET,
                {},
                coord=coord,
                me=node,
                is_root=True,
                spawner=None,
                sandbox=None,
                http=None,
                spec=None,
            )
        )
        assert '"ideas"' in out
        assert '"facts"' in out
        payload = __import__("json").loads(out)
        assert "待试 X" in payload["ideas"]
        assert "确认 Y" in payload["facts"]


class TestPromptMentionsTools:
    """提示词里必须提到这几个工具。

    这一组防的是"代码写好了、模型不知道有它"。工具注册进 `_SHARED` 只是
    "可以调"，提示词里出现才是"知道该调"。v0.72 那次 `mcp_servers` 事故
    就是这个形状的另一面：**接了线但没告诉使用方**。
    """

    def _prompts(self) -> tuple[str, str]:
        from app.kernel import runner

        spec = runner.SolveSpec(
            challenge_id="x",
            title="t",
            category="web",
            description="d",
            profile="base",
            access_urls=(),
            attachments=(),
            llm_model="m",
            llm_api_key="k",
            llm_api_base="b",
            max_turns=5,
            multi_agent=True,
        )
        return runner.build_multi_agent_prompts(spec)

    @pytest.mark.parametrize(
        "name", [ma.IDEA_ADD, ma.IDEA_RESOLVE, ma.MEMORY_ADD, ma.MEMORY_GET]
    )
    def test_both_prompts_mention(self, name: str) -> None:
        root, child = self._prompts()
        assert name in root, f"root 提示词没提 {name}"
        assert name in child, f"child 提示词没提 {name}"

    def test_dropped_requires_why_is_in_prompt(self) -> None:
        # 硬约束必须写进提示词，否则模型只会收到一个看不懂的报错。
        root, child = self._prompts()
        for text in (root, child):
            assert "why" in text
