"""v0.83：记账欠费提醒（`FACT_DEBT_NUDGE`）的行为约束。

## 这一版要解决的实测问题

v0.80 上线后第一轮真实扫描（`scan_01M5MTK3GA5EQ7BJ4CWYHC8YES`）：
42 次工具调用里 `record_fact` **0 次**。工具在注册表里、提示词写着用法、
`FACT_INDEX_NUDGE` 每轮跟着黑板一起推 —— **全都到位，模型一次都没写**。

所以 v0.83 换了一个机制：**计数 -> 到阈值才催，且催的时候给可照抄的骨架**。

这些测试盯的就是"机制真的按设计走"：

1. 不到阈值**绝对不能**出现（否则它退化成第二段背景音，和 v0.80 一样失效）
2. 到了阈值**必须**出现（否则新机制等于没接）
3. 记账成功后**立刻清零**（否则一次欠费会永久挂在那，变成噪音）
4. 写库失败**不销账**（"调了但没存进去"不算记了）
5. 按 agent 分开（子 agent 的记账不替 root 销账）
6. 读黑板不算欠账（消费不是生产）
"""

from __future__ import annotations

import pytest

from app.kernel import multiagent
from app.kernel.multiagent import (
    FACT_DEBT_WINDOW,
    AgentCoordinator,
    AgentNode,
    SolveSpec,
    _counts_toward_fact_debt,
    _do_record_fact,
)

#: 提醒里那句独一无二的文案。用它判断"提没提醒"，比判断整块文本稳定。
_NUDGE_MARK = "轮没有把结论写进项目黑板了"


class FakeStore:
    """最小鸭子类型，与 `DbFactStore` 对齐但不碰 DB。

    与 `test_facts_wiring.py` 里的同名类语义一致（那边不 import 这边，
    因为两个文件各自要调 `raise_on_record` 的不同时间点，合体会互相牵制）。
    """

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.raise_on_record: Exception | None = None

    def record(self, **kw) -> dict:
        if self.raise_on_record is not None:
            raise self.raise_on_record
        key = kw["fact_key"]
        created = key not in self.rows
        prev = self.rows.get(key)
        version = 1 if created else prev["version"] + 1
        self.rows[key] = {**kw, "version": version, "created": created}
        return {"fact_key": key, "created": created, "version": version}

    def get(self, key: str) -> dict | None:
        return self.rows.get(key)

    def index_block(self, *, max_chars=None) -> str:
        if not self.rows:
            return ""
        return "- [confirmed] endpoint/x：一句话"


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


def _coord(store=None) -> tuple[AgentCoordinator, AgentNode]:
    coord = AgentCoordinator()
    root = AgentNode(agent_id="root", name="root", task="", parent_id=None)
    coord.nodes["root"] = root
    return coord, root


# ── 1/2/3/5：计数与渲染 ──────────────────────────────


def test_debt_accumulates_and_triggers_at_window() -> None:
    """攒到阈值才催；差一轮都不行。

    ## ⚠️ 这里故意写死数字，不引用 `FACT_DEBT_WINDOW`

    第一版用 `for _ in range(FACT_DEBT_WINDOW - 1)` 再用 f-string 拼期望值，
    被反向验证当场抓出问题：**把常量改成 999，测试照样绿** ——
    因为循环次数和期望文本都跟着常量一起变，断言恒成立。
    那测的是"代码自洽"，不是"阈值是 6"。

    6 轮是**设计决策**（v0.80 那轮 42 次调用 0 条事实，3-4 轮纯读属正常），
    它该被钉住。所以这里写死 5（不催）和 6（催）；
    改常量会让这条测试真的红 —— 那正是它存在的意义。
    """
    coord, _root = _coord()

    for _ in range(5):
        coord.note_investigation("root")
    state = coord.shared.render_live_state(
        turn=5, max_turns=100, fact_debt=coord.fact_debt_of("root")
    )
    assert _NUDGE_MARK not in state, "不到阈值就催 = 噪音，会退化成背景音"

    coord.note_investigation("root")
    state = coord.shared.render_live_state(
        turn=6, max_turns=100, fact_debt=coord.fact_debt_of("root")
    )
    assert _NUDGE_MARK in state
    assert "6 轮" in state, "提示里要写清欠了几轮，否则模型不知道严重程度"
    # 骨架必须在 —— 这是"把判断变成填空"的全部意义。
    assert "record_fact" in state
    assert "fact_key" in state


def test_window_constant_is_six() -> None:
    """把"6"这个设计值单独钉一条，让改常量这件事无处可藏。"""
    assert FACT_DEBT_WINDOW == 6


def test_recording_clears_debt_immediately() -> None:
    """记了一条 -> 下一次渲染里提醒必须消失。"""
    coord, _root = _coord()
    for _ in range(9):  # 写死，不跟常量走（理由同上一条）
        coord.note_investigation("root")
    assert _NUDGE_MARK in coord.shared.render_live_state(fact_debt=coord.fact_debt_of("root"))

    coord.note_fact_recorded("root")
    state = coord.shared.render_live_state(fact_debt=coord.fact_debt_of("root"))
    assert _NUDGE_MARK not in state, "销账后还挂着 = 永久噪音"
    assert coord.fact_debt_of("root") == 0


def test_debt_is_per_agent() -> None:
    """子 agent 攒的欠账不能算到 root 头上，反之亦然。

    与 `recent_actions` 同一个理由：判定对象与判定依据必须是同一个 agent。
    """
    coord, _root = _coord()
    for _ in range(6):  # 写死 6 轮，不跟常量走
        coord.note_investigation("a1")

    assert coord.fact_debt_of("a1") >= 6
    assert coord.fact_debt_of("root") == 0

    # root 的 prompt 不该看到 a1 的欠账。
    assert _NUDGE_MARK not in coord.shared.render_live_state(
        fact_debt=coord.fact_debt_of("root")
    )
    # a1 自己的 prompt 该看到。
    assert _NUDGE_MARK in coord.shared.render_live_state(
        fact_debt=coord.fact_debt_of("a1")
    )


def test_empty_state_stays_empty() -> None:
    """空状态一个字节都不能多（v0.79 起就钉着的约束）。"""
    coord, _root = _coord()
    assert coord.shared.render_live_state(turn=0, max_turns=100) == ""


# ── 4：写库失败不销账 ────────────────────────────────


def test_failed_store_does_not_clear_debt() -> None:
    """调了 `record_fact` 但没存进去 -> 欠费照旧。

    这条防的是"假完成"：模型以为自己记了，销账后不再被催，
    而实际上那条结论谁也没有。宁可多催一次。
    """
    store = FakeStore()
    store.raise_on_record = RuntimeError("db down")
    coord, root = _coord(store)

    for _ in range(6):
        coord.note_investigation("root")
    before = coord.fact_debt_of("root")

    out = _do_record_fact(
        {"fact_key": "endpoint/health", "summary": "s"},
        coord=coord,
        me=root,
        spec=_spec(store),
    )
    assert '"stored": false' in out.replace("'", '"')
    assert coord.fact_debt_of("root") == before, "没存进去就不该销账"


def test_successful_store_clears_debt() -> None:
    """写进去了 -> 销账。这是 4 的另一面，缺了它 4 会退化成恒真。"""
    store = FakeStore()
    coord, root = _coord(store)
    for _ in range(6):
        coord.note_investigation("root")
    assert coord.fact_debt_of("root") > 0

    _do_record_fact(
        {"fact_key": "endpoint/health", "summary": "s"},
        coord=coord,
        me=root,
        spec=_spec(store),
    )
    assert coord.fact_debt_of("root") == 0
    assert "endpoint/health" in store.rows


# ── 6：哪些调用算欠账 ────────────────────────────────


@pytest.mark.parametrize(
    "tool",
    [
        multiagent.FACT_RECORD,
        multiagent.FACT_GET,
        multiagent.MEMORY_ADD,
        multiagent.MEMORY_GET,
        multiagent.COVERAGE_RECORD,
        multiagent.COVERAGE_LIST,
        multiagent.NOTE_WRITE,
        multiagent.NOTE_LIST,
        multiagent.THINK,
        multiagent.THREAT_GET,
        multiagent.IDEA_ADD,
        multiagent.IDEA_RESOLVE,
        multiagent.TODO_CREATE,
        multiagent.TODO_LIST,
        multiagent.TODO_UPDATE,
        multiagent.VIEW_GRAPH,
        multiagent.WAIT_FOR_AGENTS,
        multiagent.SEND_MESSAGE,
    ],
)
def test_producing_or_reading_tools_do_not_owe(tool: str) -> None:
    """记账/读黑板/思考/协作**都不算**欠账。

    记账类算欠账会形成一个荒谬的不动点（越记账欠账越多）；
    读黑板是**消费**，模型连着读十个事实而不产出一条新结论，
    恰恰是我们在等的行为，不该催。
    """
    assert _counts_toward_fact_debt(tool_name=tool) is False


@pytest.mark.parametrize("tool", ["run_shell", "http_request"])
def test_investigation_tools_owe(tool: str) -> None:
    """探查类调用才会产生新结论 -> 算欠账。

    注意 `run_shell` 里只是 grep 本地 bundle 也算 —— 那同样可能读出
    端点清单，是**该记的东西**（与 `_looks_like_acting` 的判定口径不同，
    那个判的是"碰没碰目标"）。
    """
    assert _counts_toward_fact_debt(tool_name=tool) is True
