"""黑板接进 kernel 的测试（v0.79）。

钉住两件事：

1. **没有 fact_store 时降级而不是崩** —— CTF 解题路径和大部分测试都没注入，
   这条路径必须给出可读的"本次未启用"，而不是 AttributeError；
2. **有 fact_store 时** 校验错误变成 JSON 错误、成功写入、`get_scan_fact`
   能按 key 取回全文。

用一个假 store 而不是真数据库：kernel 这层只该验证"怎么调用、怎么把结果
转成 JSON"，DB 语义归 `test_facts_store.py` 管。
"""

from __future__ import annotations

import json

import pytest

from app.kernel import multiagent


class FakeStore:
    """最小鸭子类型，行为与 `DbFactStore` 对齐但不碰 DB。"""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.raise_on_record: Exception | None = None

    def record(self, **kw) -> dict:
        self.calls.append(("record", kw))
        if self.raise_on_record is not None:
            raise self.raise_on_record
        key = kw["fact_key"]
        created = key not in self.rows
        prev = self.rows.get(key)
        version = 1 if created else prev["version"] + 1
        self.rows[key] = {**kw, "version": version, "created": created}
        return {"fact_key": key, "created": created, "version": version}

    def get(self, key: str) -> dict | None:
        self.calls.append(("get", {"fact_key": key}))
        return self.rows.get(key)

    def index_block(self, *, max_chars=None) -> str:
        self.calls.append(("index_block", {}))
        if not self.rows:
            return ""
        return "- [confirmed] endpoint/x：一句话\n\n需要完整内容时必须调用 get_scan_fact(fact_key)"


def _spec(store=None):
    return multiagent.SolveSpec(
        challenge_id="c",
        title="t",
        category="web",
        description="d",
        profile="p",
        access_urls=(),
        attachments=(),
        llm_model="m",
        llm_api_key="k",
        fact_store=store,
    )


def _coord():
    coord = multiagent.AgentCoordinator()
    root = multiagent.AgentNode(
        agent_id="root", name="root", task="", parent_id=None
    )
    coord.nodes["root"] = root
    return coord, root


async def _call(name, args, *, store):
    coord, root = _coord()
    return await multiagent.dispatch_orchestration_tool(
        name,
        args,
        coord=coord,
        me=root,
        is_root=True,
        spawner=None,
        sandbox=None,
        http=None,
        spec=_spec(store),
    )


class TestToolsAreVisible:
    def test_both_in_shared(self):
        assert multiagent.FACT_RECORD in multiagent._SHARED
        assert multiagent.FACT_GET in multiagent._SHARED

    def test_both_visible_to_root_and_child(self):
        """工具名必须在两份提示词的工具表里 —— 否则模型不知道它们存在，
        而提示词里提到的工具名如果不在表里，是 v0.72 mcp_servers 那类事故的形状。"""
        assert multiagent.FACT_RECORD in multiagent.root_tool_names()
        assert multiagent.FACT_GET in multiagent.root_tool_names()
        assert multiagent.FACT_RECORD in multiagent.child_tool_names()
        assert multiagent.FACT_GET in multiagent.child_tool_names()


class TestNoStoreDegrades:
    """没注入 fact_store 时：给出可读提示，不抛、不崩。"""

    @pytest.mark.asyncio
    async def test_record_without_store_says_disabled(self):
        out = await _call(
            multiagent.FACT_RECORD,
            {"fact_key": "endpoint/x", "summary": "一句话"},
            store=None,
        )
        data = json.loads(out)
        assert data["success"] is True
        assert data["stored"] is False
        assert "未启用" in data["note"]
        # 必须指向可用替代，否则模型会反复重试这个永远失败的调用
        assert "add_fact" in data["note"]

    @pytest.mark.asyncio
    async def test_get_without_store_returns_not_found(self):
        out = await _call(multiagent.FACT_GET, {"fact_key": "endpoint/x"}, store=None)
        data = json.loads(out)
        assert data["success"] is True
        assert data["found"] is False


class TestWithStore:
    @pytest.mark.asyncio
    async def test_record_writes_and_reports_created(self):
        store = FakeStore()
        out = await _call(
            multiagent.FACT_RECORD,
            {"fact_key": "endpoint/x", "summary": "一句话", "confidence": "confirmed"},
            store=store,
        )
        data = json.loads(out)
        assert data["success"] is True
        assert data["created"] is True
        assert data["fact_key"] == "endpoint/x"
        assert "新条目" in data["note"]

    @pytest.mark.asyncio
    async def test_record_twice_reports_update_and_version(self):
        store = FakeStore()
        args = {"fact_key": "endpoint/x", "summary": "一句话"}
        await _call(multiagent.FACT_RECORD, args, store=store)
        out = await _call(multiagent.FACT_RECORD, args, store=store)
        data = json.loads(out)
        assert data["created"] is False
        assert data["version"] == 2
        assert "第 2 版" in data["note"]

    @pytest.mark.asyncio
    async def test_validation_error_becomes_json_error(self):
        """store 抛 ValueError（来自 validate_fact）→ 必须是 JSON 错误，
        不能冒泡（冒泡会终止整场编排）。"""
        store = FakeStore()
        store.raise_on_record = ValueError("fact_key 必须带分类前缀")
        out = await _call(
            multiagent.FACT_RECORD, {"fact_key": "x", "summary": "s"}, store=store
        )
        data = json.loads(out)
        assert data["success"] is False
        assert "分类前缀" in data["error"]

    @pytest.mark.asyncio
    async def test_unexpected_error_is_swallowed_not_raised(self):
        """DB 抖一下不该让整场编排停摆 —— 返回 ok+stored=False 让模型继续。"""
        store = FakeStore()
        store.raise_on_record = RuntimeError("connection reset")
        out = await _call(
            multiagent.FACT_RECORD, {"fact_key": "endpoint/x", "summary": "s"}, store=store
        )
        data = json.loads(out)
        assert data["success"] is True
        assert data["stored"] is False

    @pytest.mark.asyncio
    async def test_get_returns_full_detail(self):
        store = FakeStore()
        await _call(
            multiagent.FACT_RECORD,
            {"fact_key": "endpoint/x", "summary": "一句话", "detail": "细节在这里"},
            store=store,
        )
        out = await _call(multiagent.FACT_GET, {"fact_key": "endpoint/x"}, store=store)
        data = json.loads(out)
        assert data["success"] is True
        assert data["found"] is True
        assert data["summary"] == "一句话"

    @pytest.mark.asyncio
    async def test_get_missing_suggests_record(self):
        store = FakeStore()
        out = await _call(multiagent.FACT_GET, {"fact_key": "endpoint/nope"}, store=store)
        data = json.loads(out)
        assert data["found"] is False
        assert "record_fact" in data["note"]

    @pytest.mark.asyncio
    async def test_recorded_by_is_agent_id(self):
        """谁写的要记住 —— 同一条事实被两个 agent 写过不同结论时靠它分辨。"""
        store = FakeStore()
        await _call(
            multiagent.FACT_RECORD, {"fact_key": "endpoint/x", "summary": "s"}, store=store
        )
        assert store.calls[0][1]["recorded_by"] == "root"


class TestIndexBlockReachesPrompt:
    def test_index_block_is_passed_into_live_state(self):
        # 用源码断言：注入点必须把 fact_index 传进去
        import inspect

        text = inspect.getsource(multiagent.run_agent_loop)
        assert "fact_index=_fact_index" in text
        assert "index_block()" in text

    def test_empty_index_keeps_state_empty(self):
        """落库黑板为空时不能凭空空吐一段 —— 与空状态返回空串同一条纪律。"""
        s = multiagent.SharedState()
        assert s.render_live_state(turn=1, max_turns=10, fact_index="") == ""

    def test_index_alone_is_enough(self):
        """只有落库事实、没有任何内存事实时也必须出内容 ——
        那是别的扫描留下的结论，正是最该被看见的一类。"""
        s = multiagent.SharedState()
        out = s.render_live_state(turn=5, max_turns=100, fact_index="- [confirmed] endpoint/x：一句话")
        assert out != ""
        assert "endpoint/x" in out
        assert "第 5/100 轮" in out
