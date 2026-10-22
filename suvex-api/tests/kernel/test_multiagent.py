"""多智能体编排（v0.58）的测试。

## 测什么 / 不测什么

测：图结构（root/child/parent）、共享状态（notes/coverage）、编排工具的
越权拦截与语义、以及**一条端到端的假跑**（root 派子 agent → 子 agent 干活
→ root 收报告 → 汇总）。

不测：真实 LLM、真实沙箱、真实并发时序。那些在 Kali 上做真跑验证
（本机没有 docker）。

## 为什么用「剧本式 gateway」而不是 mock `chat`

这个模块的全部逻辑都在「读到什么回复 → 做什么」这条链上。用一个按**调用
顺序**吐预设回复的 gateway，能精确锁住"第 N 次调用该看到什么"，
而 mock `chat` 的返回值会绕过"回复怎么被解析"这一层 —— 那一层恰恰是
最容易错的地方（文本协议）。
"""

from __future__ import annotations

import json

from app.kernel import multiagent, runner
from tests.kernel.test_runner import _FakeSandbox, _run, _spec


class _ScriptedGateway:
    """按调用顺序吐预设回复。**用完就报错**，逼测试精确。

    ⚠️ root 与 child 共用同一个 gateway 对象（真实里也是同一个 LlmGateway
    由所有 agent 复用）。所以这里按"全局第 N 次调用"给回复 —— 测试要自己
    算清 root 第 1 次、child 第 2 次…… 的顺序。
    """

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.seen_systems: list[str] = []
        self.model = "test-model"
        self.label = "test-label"

    async def chat(self, messages, *, system):  # noqa: ANN001, ANN201
        self.calls += 1
        self.seen_systems.append(system)
        if not self.replies:
            # 比"静默返回空"好：空回复会让循环继续烧轮次，测试却看不出问题。
            raise AssertionError(f"gateway 被多调用了（第 {self.calls} 次），剧本已用完")
        text = self.replies.pop(0)
        return runner.LlmReply(
            text=text,
            source="content",
            usage=runner.TokenUsage(
                prompt_tokens=10, completion_tokens=20, total_tokens=30, reported=True
            ),
        )


# ══════════════════════════════════════════════════════════
# 纯数据层：图 / 共享状态
# ══════════════════════════════════════════════════════════


def test_new_agent_ids_are_short_and_unique() -> None:
    """agent id 要短（会进提示词与工具返回，长 id 纯浪费 token）且本场唯一。"""
    coord = multiagent.AgentCoordinator()
    root = coord.add_root()
    a = coord.add_child(name="侦察", task="摸面", parent_id=root.agent_id)
    b = coord.add_child(name="验证", task="测 SQLi", parent_id=root.agent_id)
    assert root.agent_id == "root"
    assert a.agent_id == "a1" and b.agent_id == "a2"


def test_children_of_only_returns_direct_children() -> None:
    coord = multiagent.AgentCoordinator()
    root = coord.add_root()
    a = coord.add_child(name="A", task="t", parent_id=root.agent_id)
    b = coord.add_child(name="B", task="t", parent_id=root.agent_id)
    assert {n.agent_id for n in coord.children_of("root")} == {a.agent_id, b.agent_id}
    assert coord.children_of(a.agent_id) == []


def test_running_children_filters_terminal() -> None:
    coord = multiagent.AgentCoordinator()
    coord.add_root()
    a = coord.add_child(name="A", task="t", parent_id="root")
    b = coord.add_child(name="B", task="t", parent_id="root")
    a.status = multiagent.STATUS_RUNNING
    b.status = multiagent.STATUS_COMPLETED
    running = coord.running_children("root")
    assert [n.agent_id for n in running] == [a.agent_id]


def test_spawn_capped_at_max_children() -> None:
    """★ 子 agent 有硬上限 —— 否则一个"全面看看"能开二十个、烧光 token。"""
    coord = multiagent.AgentCoordinator(total_budget=1000)
    coord.add_root()
    for i in range(multiagent.MAX_CHILDREN):
        coord.add_child(name=f"c{i}", task="t", parent_id="root")
    assert coord.can_spawn("root") is False, "到达上限后不该再允许 spawn"


def test_spawn_blocked_when_budget_exhausted() -> None:
    coord = multiagent.AgentCoordinator(total_budget=5)
    coord.add_root()
    coord.turns_used = 5
    assert coord.can_spawn("root") is False


def test_total_budget_scales_with_max_turns_for_parallel_agents() -> None:
    """★ 多智能体的总预算必须**大于**单循环的 max_turns。

    回归线（2026-10-22）：原来 `TOTAL_TURN_RATIO = 1.0` —— 总预算 == max_turns。
    但那是给**一条**循环的数；多智能体是 root + 最多 6 个子 agent 共 7 条循环
    来分这 80，实测一次真实渗透 6 个 agent **恰好跑满 80 轮就断**，4 个专职
    子 agent 每条只分到十几二十轮，全卡在侦察阶段、一条 confirmed 都没收敛到。
    开 4 个子 agent 本为并行覆盖更多面，结果总预算没跟着涨，比单循环还浅。

    这里钉死：同样 max_turns 下，多智能体总预算 = max_turns * TOTAL_TURN_RATIO，
    且严格大于 max_turns（否则并行等于白开）。
    """
    # 复刻 run_multi_agent 里的算法 `total_budget=max(12, int(max_turns * RATIO))`，
    # 不构造 SolveSpec（它有 9 个必填位置参数，为一条算式去拼它不划算）。
    ratio = multiagent.TOTAL_TURN_RATIO
    assert ratio > 1.0, "总预算必须比单循环大，否则并行无意义"
    for max_turns in (80, 150):
        total = max(12, int(max_turns * ratio))
        assert total > max_turns, f"多智能体总预算 {total} 不该 <= max_turns {max_turns}"


def test_shared_notes_overwrite_by_title() -> None:
    """同名标题**覆盖**写（去重靠标题）—— 这是"别写重复笔记"的实现。"""
    shared = multiagent.SharedState()
    shared.write_note("入口", "v1 内容", by="A")
    shared.write_note("入口", "v2 内容", by="B")
    assert list(shared.notes.keys()) == ["入口"]
    assert "v2 内容" in shared.notes["入口"]
    assert "by B" in shared.notes["入口"]


def test_shared_coverage_records_chain_of_ownership() -> None:
    """同一 surface 被两人测过 → `by` 记成 "A → B"，因为后者的结论更可信。"""
    shared = multiagent.SharedState()
    shared.record_coverage("/api/login", "认证绕过", "干净", by="A")
    shared.record_coverage("/api/login", "认证绕过", "确认弱口令", by="B")
    rec = shared.coverage["/api/login"]
    assert rec["outcome"] == "确认弱口令"
    assert rec["by"] == "A → B"


def test_render_graph_marks_you() -> None:
    coord = multiagent.AgentCoordinator()
    coord.add_root()
    coord.add_child(name="侦察", task="t", parent_id="root")
    text = coord.render_graph(me="root")
    assert "← you" in text
    assert "root" in text and "侦察" in text


# ══════════════════════════════════════════════════════════
# 编排工具：越权拦截 + 语义
# ══════════════════════════════════════════════════════════


def _dispatch_orch(name, args, *, coord, me, is_root=True, spawner=None):  # noqa: ANN001
    return _run(
        multiagent.dispatch_orchestration_tool(
            name,
            args,
            coord=coord,
            me=me,
            is_root=is_root,
            spawner=spawner,
            sandbox=_FakeSandbox(),
            http=None,
            spec=_spec(),
        )
    )


def test_root_cannot_call_agent_finish() -> None:
    """★ 越权拦截：root 用 `agent_finish` 会被告知去用 `finish_scan`。"""
    coord = multiagent.AgentCoordinator()
    root = coord.add_root()
    out = _dispatch_orch("agent_finish", {"result_summary": "x"}, coord=coord, me=root, is_root=True)
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "finish_scan" in parsed["error"]


def test_child_cannot_call_finish_scan() -> None:
    coord = multiagent.AgentCoordinator()
    coord.add_root()
    child = coord.add_child(name="c", task="t", parent_id="root")
    out = _dispatch_orch("finish_scan", {}, coord=coord, me=child, is_root=False)
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "agent_finish" in parsed["error"]


def test_create_agent_requires_name_and_task() -> None:
    coord = multiagent.AgentCoordinator()
    root = coord.add_root()
    out = _dispatch_orch("create_agent", {"name": "x"}, coord=coord, me=root)
    assert json.loads(out)["success"] is False


def test_create_agent_blocked_without_spawner() -> None:
    coord = multiagent.AgentCoordinator()
    root = coord.add_root()
    out = _dispatch_orch("create_agent", {"name": "x", "task": "y"}, coord=coord, me=root, spawner=None)
    assert json.loads(out)["success"] is False
    assert "spawner" in json.loads(out)["error"]


def test_write_note_then_list_notes_visible_to_other_agent() -> None:
    """共享草稿纸：A 写的，B 能读到（这是跨 agent 传中间结论的机制）。"""
    coord = multiagent.AgentCoordinator()
    root = coord.add_root()
    child = coord.add_child(name="A", task="t", parent_id="root")
    _dispatch_orch(
        "write_note",
        {"title": "发现", "body": "/admin 免认证"},
        coord=coord,
        me=child,
        is_root=False,
    )
    out = _dispatch_orch("list_notes", {}, coord=coord, me=root, is_root=True)
    assert "/admin 免认证" in json.loads(out)["notes"]


def test_record_coverage_surfaces_in_list() -> None:
    coord = multiagent.AgentCoordinator()
    root = coord.add_root()
    _dispatch_orch(
        "record_coverage",
        {"surface": "/api/users", "risk": "IDOR", "outcome": "确认越权"},
        coord=coord,
        me=root,
    )
    out = _dispatch_orch("list_coverage", {}, coord=coord, me=root)
    assert "/api/users" in json.loads(out)["coverage"]


def test_send_message_rejects_when_target_terminal() -> None:
    """★ 我们的子 agent 跑完就结束、不能唤醒 —— 发消息给已结束的会明确报错，
    而不是静默丢弃让 root 空等。"""
    coord = multiagent.AgentCoordinator()
    root = coord.add_root()
    child = coord.add_child(name="c", task="t", parent_id="root")
    child.status = multiagent.STATUS_COMPLETED
    out = _dispatch_orch(
        "send_message_to_agent", {"target_agent_id": child.agent_id, "message": "hi"},
        coord=coord, me=root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "completed" in parsed["error"]


def test_unknown_orchestration_tool_lists_available() -> None:
    coord = multiagent.AgentCoordinator()
    root = coord.add_root()
    out = _dispatch_orch("frobnicate", {}, coord=coord, me=root)
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "create_agent" in parsed["error"]


# ══════════════════════════════════════════════════════════
# 端到端：root 派子 agent → 子 agent 干活 → root 汇总
# ══════════════════════════════════════════════════════════


def test_end_to_end_root_spawns_child_and_merges_findings() -> None:
    """★★ 完整路径：root 派一个子 agent，子 agent 跑出自己的发现，
    root 收报告后用 FINAL 汇总 —— 最终 outcome 里能看到**两边的发现合并**。

    调用顺序（共用 gateway）：
      1. root  → create_agent（派"SQLi 验证"）
      2. child → FINAL（子 agent 报一条发现）
      3. root  → FINAL（root 收口，报自己一条发现）
    """
    root_final = 'FINAL: {"summary": "完成", "findings": [{"title": "root 发现的未授权读取", "severity": "low"}]}'
    child_final = 'FINAL: {"summary": "子 agent 完成", "findings": [{"title": "SQLi 注入确认", "severity": "high"}]}'
    spawn_action = (
        "ACTION: "
        + json.dumps(
            {
                "tool": "create_agent",
                "args": {"name": "SQLi 验证", "task": "验证 /api/login 是否有 SQL 注入"},
            },
            ensure_ascii=False,
        )
    )
    gateway = _ScriptedGateway([spawn_action, child_final, root_final])
    sandbox = _FakeSandbox()
    spec = _spec(multi_agent=True, max_turns=6)

    # on_progress 不应改变结果（这里给 None，走"没有回调"分支）。
    outcome = _run(
        runner.solve_in_sandbox(spec, sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )

    # 两个 agent 都跑过（root 至少 2 次：派活 + 收口；child 至少 1 次：FINAL）。
    assert gateway.calls >= 3, f"应当 root 与 child 都跑过，实际调用 {gateway.calls} 次"
    titles = [f.get("title") for f in (outcome.findings or []) if isinstance(f, dict)]
    assert "SQLi 注入确认" in titles, "子 agent 的发现必须被汇总进来"
    assert "root 发现的未授权读取" in titles, "root 自己的发现也要在"


def test_multi_agent_flag_off_is_byte_identical_single_loop() -> None:
    """★★ 默认（multi_agent=False）**完全走单循环** —— 这是本版最硬的回归守卫。

    证据：单循环里 `create_agent` 不是合法工具，模型调它只会拿到
    "未知工具" 一句话，然后正常用 run_shell 干活并 FINAL。
    """
    sandbox = _FakeSandbox(outputs={"ls": "hint.txt"})
    gateway = _ScriptedGateway(
        [
            'ACTION: {"tool": "run_shell", "args": {"command": "ls /work"}}',
            'FINAL: {"candidates": [{"flag": "flag{x}", "evidence": "看到了"}]}',
        ]
    )
    spec = _spec()  # multi_agent 默认 False
    outcome = _run(
        runner.solve_in_sandbox(spec, sandbox=sandbox, gateway=gateway, system_prompt="sys")
    )
    assert [c.flag for c in outcome.candidates] == ["flag{x}"]
    assert gateway.calls == 2, "单循环：一次工具 + 一次 FINAL，不该有多余调用"


def test_child_cannot_spawn_grandchild() -> None:
    """★ 单层编排：子 agent 的 spawner 是 None —— 它调 create_agent 会失败。
    防"子 agent 再开子 agent"指数爆炸。"""
    coord = multiagent.AgentCoordinator()
    coord.add_root()
    child = coord.add_child(name="c", task="t", parent_id="root")
    out = _dispatch_orch(
        "create_agent", {"name": "gc", "task": "x"},
        coord=coord, me=child, is_root=False, spawner=None,
    )
    # 越权拦截先挡（create_agent 是 root-only）
    assert json.loads(out)["success"] is False


def test_root_prompt_and_child_prompt_differ() -> None:
    """两份提示词必须不同 —— 否则"root 只调度、child 只动手"的分工不成立。"""
    root_p = runner.load_root_prompt()
    child_p = runner.load_child_prompt()
    assert root_p != child_p
    assert "调度" in root_p
    assert "执行者" in child_p


def test_build_multi_agent_prompts_includes_scenario_blocks() -> None:
    """★ 多智能体的两份 prompt **都**带上本题场景块（与单循环同一份 base），
    所以渗透/IR 纪律不会因为走多智能体就丢。"""
    spec = _spec(category="pentest-evaluation", pentest=True)
    root_p, child_p = runner.build_multi_agent_prompts(spec)
    # 渗透块里的一句话，两份都该有。
    marker = "已验证的安全发现"
    assert marker in root_p
    assert marker in child_p
    # 且各自再叠了角色层。
    assert "调度者" in root_p
    assert "执行者" in child_p
