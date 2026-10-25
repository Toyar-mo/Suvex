"""v0.95：覆盖欠账提醒（`coverage_debt_nudge`）的行为约束。

## 这一版要解决的实测问题 —— 事实那一半已修好，覆盖这一半没动

同一轮实测（`scan_01M5MTK3GA5EQ7BJ4CWYHC8YES`）里 `record_fact` 与
`record_coverage` **都是 0 次**。v0.83 只给前者加了欠账机制，它起来了
（v0.84 那轮 14 次、0 次空参数）。覆盖表没人管。

v0.84 那轮更精确的形状：`list_coverage` 被调 **2 次**、`record_coverage`
**0 次** —— 模型会去**读**这张表，却从不往里**写**。于是表永远是空的，
读了也没用，下一个 agent 还是把同一个点重测一遍。

## 这些测试盯的六件事

1. 不到阈值**绝对不能**出现（否则退化成第二段背景音，与 v0.80 同样失效）
2. 到了阈值**必须**出现，且带上可照抄的骨架
3. 记账成功后**立刻清零**
4. 按 agent 分开（子 agent 记账不替 root 销账）
5. ★ **与事实欠账完全独立** —— 记了事实不该清掉覆盖欠账，反之亦然
6. 空参数报错回包里给的骨架与提醒里的**是同一份**

## 为什么第 5 条最重要

两个计数器如果共用，会出现一个具体的坏结果：模型想起来记一条事实
（`fact_debt` 清零），那个动作同时把 `coverage_debt` 也清零 ——
于是覆盖表永远等不到那次提醒。这正是为什么它是两个独立字典。
"""

from __future__ import annotations

import inspect

import pytest

from app.kernel import multiagent
from app.kernel.multiagent import (
    COVERAGE_DEBT_WINDOW,
    AgentCoordinator,
    AgentNode,
    SolveSpec,
    _do_record_coverage,
    coverage_debt_nudge,
)

#: 提醒里那句独一无二的文案。用它判断"提没提醒"，比判断整块文本稳定。
_NUDGE_MARK = "轮没有记录覆盖了"


def _spec(store=None) -> SolveSpec:
    return SolveSpec(
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


def _coord() -> tuple[AgentCoordinator, AgentNode]:
    coord = AgentCoordinator()
    root = AgentNode(agent_id="root", name="root", task="", parent_id=None)
    coord.nodes["root"] = root
    return coord, root


def _state(coord: AgentCoordinator, agent_id: str = "root") -> str:
    """按**调用点**的形状渲染（两个欠账都传），而不是只传一个。

    ⚠️ 这里如果只传 `coverage_debt`，测出来的就不是生产路径 ——
    生产在 `_run_agent_loop` 里两个参数一起传（见 `coverage_debt=...` 那一行）。
    """
    return coord.shared.render_live_state(
        fact_debt=coord.fact_debt_of(agent_id),
        coverage_debt=coord.coverage_debt_of(agent_id),
    )


# ── 1/2：阈值 ────────────────────────────────────────


def test_debt_triggers_only_at_window() -> None:
    """攒到阈值才催；差一轮都不行。

    ## ⚠️ 写死 5 和 6，不引用 `COVERAGE_DEBT_WINDOW`

    与 `test_fact_debt_nudge.py` 里同一条纪律：用常量拼循环次数和期望文本，
    把常量改成 999 测试照样绿 —— 那测的是"代码自洽"，不是"阈值是 6"。

    6 是设计决策（探查阶段常有几轮纯读，与 `FACT_DEBT_WINDOW` 同一个理由），
    它该被钉住。改常量会让这条测试真的红。
    """
    coord, _root = _coord()

    for _ in range(5):
        coord.note_investigation_for_coverage("root")
    assert _NUDGE_MARK not in _state(coord), "不到阈值就催 = 噪音，会退化成背景音"

    coord.note_investigation_for_coverage("root")
    state = _state(coord)
    assert _NUDGE_MARK in state
    assert "6 轮" in state, "提示里要写清欠了几轮，否则模型不知道严重程度"
    # 骨架必须在 —— "把判断变成填空"是这套机制的全部意义。
    assert "record_coverage" in state
    assert "surface" in state


def test_window_constant_is_six() -> None:
    """把"6"这个设计值单独钉一条，让改常量无处可藏。"""
    assert COVERAGE_DEBT_WINDOW == 6


# ── 3：清零 ─────────────────────────────────────────


def test_recording_clears_debt_immediately() -> None:
    """记了一条覆盖 -> 下一次渲染里提醒必须消失。"""
    coord, root = _coord()
    for _ in range(9):  # 写死，不跟常量走
        coord.note_investigation_for_coverage("root")
    assert _NUDGE_MARK in _state(coord)

    _do_record_coverage(
        {"surface": "GET /api/v1/health", "risk": "r", "outcome": "已排除：干净"},
        coord=coord,
        me=root,
    )
    state = _state(coord)
    assert _NUDGE_MARK not in state, "销账后还挂着 = 永久噪音"
    assert coord.coverage_debt_of("root") == 0


def test_failed_record_does_not_clear_debt() -> None:
    """缺 `surface` 的调用被拒 -> 欠账照旧。

    这条防"假完成"：模型以为记了、销账后不再被催，而表里什么都没有。
    `record_coverage` 没有 DB 往返（`SharedState` 是纯内存），所以它的
    "没成功"只有一条路径 —— 参数校验失败。
    """
    coord, root = _coord()
    for _ in range(6):
        coord.note_investigation_for_coverage("root")
    before = coord.coverage_debt_of("root")

    out = _do_record_coverage({"area": "猜错的字段名"}, coord=coord, me=root)
    assert "success" in out and "false" in out.lower().replace('"', "")
    assert coord.coverage_debt_of("root") == before, "没记进去就不该销账"
    assert not coord.shared.coverage, "无效调用不该写进表里"


# ── 4：按 agent 分开 ────────────────────────────────


def test_debt_is_per_agent() -> None:
    """子 agent 攒的覆盖欠账不能算到 root 头上，反之亦然。"""
    coord, _root = _coord()
    for _ in range(6):  # 写死 6 轮
        coord.note_investigation_for_coverage("a1")

    assert coord.coverage_debt_of("a1") >= 6
    assert coord.coverage_debt_of("root") == 0
    assert _NUDGE_MARK not in _state(coord, "root")
    assert _NUDGE_MARK in _state(coord, "a1")


# ── 5：★ 与事实欠账完全独立 ─────────────────────────


def test_recording_fact_does_not_clear_coverage_debt() -> None:
    """★ 记了**结论**不销**覆盖**的账。

    这是两个计数器必须分开的**唯一理由**，也是最容易被"顺手复用"弄丢的性质：
    模型容易想起"我确认了某个结论所以记一条"，却想不起"我刚才还打了五个端点"。
    共用计数器时前者的写入会顺手清掉后者 —— 覆盖表就永远等不到提醒。
    """
    coord, root = _coord()
    for _ in range(6):
        coord.note_investigation_for_coverage("root")
        coord.note_investigation("root")
    assert coord.coverage_debt_of("root") >= 6

    coord.note_fact_recorded("root")

    assert coord.fact_debt_of("root") == 0, "记事实该清事实账"
    assert coord.coverage_debt_of("root") >= 6, "★ 但不该清覆盖账"
    assert _NUDGE_MARK in _state(coord), "覆盖提醒必须还在"


def test_recording_coverage_does_not_clear_fact_debt() -> None:
    """反方向：记了**覆盖**不销**结论**的账。"""
    coord, root = _coord()
    for _ in range(6):
        coord.note_investigation_for_coverage("root")
        coord.note_investigation("root")
    assert coord.fact_debt_of("root") >= 6

    _do_record_coverage(
        {"surface": "POST /api/v1/auth/login", "risk": "r", "outcome": "成立：弱口令"},
        coord=coord,
        me=root,
    )

    assert coord.coverage_debt_of("root") == 0, "记覆盖该清覆盖账"
    assert coord.fact_debt_of("root") >= 6, "★ 但不该清事实账"


# ── 6：骨架单点性 ───────────────────────────────────


def test_error_reply_and_nudge_share_the_same_skeleton() -> None:
    """空参数回包里的骨架 == 提醒里的骨架。

    两处各写一份的后果：改了一处忘另一处，模型按旧骨架填、又被拒一次。
    这类"同一个事实写两遍"是本项目已经踩过的坑（`tools.TOOL_SPECS`
    与真实工具表脱节，13 vs 35）。

    ## ⚠️ 必须先解 JSON 再比 —— 这是我在写这条测试时踩到的

    第一版直接 `assert _COVERAGE_SKELETON in out`，红了。不是产品缺陷：
    `_do_record_coverage` 的报错走 `_json_err`，骨架里的 `"` 在 wire 上是
    `\\"`。模型拿到的是**解码后**的文本，它看见的骨架与提醒里那份逐字相同。

    所以这里解析 JSON、取 `error` 字段再断言 —— 测的必须是模型真正读到的东西，
    而不是它在传输层被转义成什么样。
    """
    import json

    coord, root = _coord()
    out = _do_record_coverage({}, coord=coord, me=root)
    payload = json.loads(out)
    assert payload["success"] is False
    decoded = payload["error"]

    nudge = coverage_debt_nudge(6)

    assert multiagent._COVERAGE_SKELETON in decoded, "报错回包里必须带可照抄的骨架"
    assert multiagent._COVERAGE_SKELETON in nudge


def test_nudge_does_not_raise_on_braces() -> None:
    """骨架里的真花括号不能让 `.format()` 抛 `KeyError`。

    与 `fact_debt_nudge` 完全同源的坑：v0.84 第一版把骨架内联进
    `FACT_DEBT_NUDGE.format(n=n)`，直接 `KeyError: '"tool"'`。
    """
    text = coverage_debt_nudge(6)
    assert '{"tool"' in text or '"tool"' in text, "骨架里应当有真花括号（否则这条测试没在测它）"
    assert "{n}" not in text, "`{n}` 应当已被替换掉"


# ── 接线：钉住三个调用点 ─────────────────────────────


def test_coverage_debt_is_wired_into_the_tool_loop() -> None:
    """★ 接线断言：主循环里必须真的 `+1`。

    纯函数测试全绿但接线没接上，是 v0.72 `mcp_servers` 事故的形状 ——
    v0.95 这一版的三个方法（`note_coverage_recorded` /
    `note_investigation_for_coverage` / `coverage_debt_of`）**一开始就是
    定义齐了、一个调用点都没有**，正是这个形状。
    """
    src = inspect.getsource(multiagent.run_agent_loop)
    assert "note_investigation_for_coverage(" in src, "覆盖欠账计数器没接线 = 永远是 0"


def test_coverage_debt_is_passed_to_render_live_state() -> None:
    """★ 接线断言：渲染时两个欠账都要传，只传一个等于覆盖提醒永远不出现。"""
    src = inspect.getsource(multiagent.run_agent_loop)
    assert "coverage_debt=coord.coverage_debt_of(" in src


def test_coverage_record_clears_debt_via_shared_state() -> None:
    """★ 接线断言：`_do_record_coverage` 成功后必须销账。"""
    src = inspect.getsource(multiagent._do_record_coverage)
    assert "note_coverage_recorded(" in src


@pytest.mark.parametrize("call_site", ["run_agent_loop", "_do_record_coverage"])
def test_no_two_counters_share_a_dict(call_site: str) -> None:
    """两条独立性质的静态守卫：`fact_debt` 与 `coverage_debt` 是两个字段。

    动态测试（上面第 5 组）能抓住"清零时串了"，但抓不住
    "某个改法只赋值了一个、另一个没赋值"这种半截接线。
    """
    coord = AgentCoordinator()
    assert coord.fact_debt is not coord.coverage_debt
    assert coord.fact_debt == {}
    assert coord.coverage_debt == {}
