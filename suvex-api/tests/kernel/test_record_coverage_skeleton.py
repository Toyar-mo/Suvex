"""v0.84 #4：`record_coverage` 的空/错字段回包必须带骨架。

## 实地证据（这条测试的由来）

`scan_01M5PGMX9ADDWV0V42B1W48ZF9` 的 scan.log：

    652: [tool_call] record_coverage {"area": "api-v1-unauthenticated-endpoint-fuzz", ...}
    654: [reasoning] The record_coverage needs `surface` field. Let me redo that and run the logout test.
    655: [tool_call] record_coverage {"surface": "/api/v1/FUZZ (69-word API dictionary)", ...}

模型把字段名猜成了 `area`，拿到报错才改对。**格式是对的（ACTION + args），
只是不知道字段叫什么** —— 而这恰恰是"回包里给骨架"能省掉的半轮。

同一轮里 `record_fact` 一次空参数都没有，因为它早就有骨架回包了。
两个工具的待遇应当一致。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.kernel import multiagent
from app.kernel.multiagent import (
    AgentCoordinator,
    AgentNode,
    SolveSpec,
    dispatch_orchestration_tool,
)
from app.kernel.runner import parse_actions


def _call(coord: AgentCoordinator, me: AgentNode, args: dict) -> str:
    """走真实分发入口，而不是直接调 `_do_record_coverage` ——
    这样连"工具名是否登记"一起验了。"""
    return asyncio.run(
        dispatch_orchestration_tool(
            "record_coverage",
            args,
            coord=coord,
            me=me,
            is_root=True,
            spawner=None,
            sandbox=None,  # type: ignore[arg-type]
            http=None,
            spec=_spec(),
        )
    )


def _spec() -> SolveSpec:
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
    )


@pytest.fixture
def coord() -> AgentCoordinator:
    return AgentCoordinator()


@pytest.fixture
def me(coord: AgentCoordinator) -> AgentNode:
    root = AgentNode(agent_id="root", name="root", task="", parent_id=None)
    coord.nodes["root"] = root
    return root


def test_skeleton_is_valid_json() -> None:
    """骨架本身得是合法 JSON —— 模型会原样复制。"""
    obj = json.loads(multiagent._COVERAGE_SKELETON)
    assert obj["tool"] == "record_coverage"
    assert set(obj["args"]) == {"surface", "risk", "outcome"}


def test_skeleton_survives_parse_actions() -> None:
    """★ 模型照抄骨架时 `parse_actions` 必须解析出非空 args。

    缺 `"args"` 包装会回落成 `{}` —— 那就是**又一个空参数**，
    和 v0.83 那 3 次 `record_fact` `{}` 同一个失败模式。
    """
    parsed = parse_actions("ACTION: " + multiagent._COVERAGE_SKELETON)
    assert len(parsed) == 1
    tool, args = parsed[0]
    assert tool == "record_coverage"
    assert args, 'args 是空的 —— 骨架缺了 `"args"` 包装'
    assert set(args) == {"surface", "risk", "outcome"}


def _error_text(out: str) -> str:
    """回包是 JSON，骨架里的引号被转义了 —— 解出来再查，别对原始串断言。

    （第一版就是直接 `assert '"surface"' in out`，被转义后的 `\\"surface\\"`
    挡红。测试自己写错了会掩盖被测行为，所以这里显式做一次解包。）
    """
    payload = json.loads(out)
    return str(payload.get("error") or "")


def test_missing_surface_returns_skeleton(coord, me) -> None:
    """空参数时的回包必须含骨架全文，而不只是一句"需要 surface"。

    只回一句话时，模型知道缺什么但**不知道正确形状** —— 它可能再猜一次
    字段名（实测就是猜成了 `area`），又白花半轮。
    """
    out = _call(coord, me, {})
    text = _error_text(out)
    assert '"surface"' in text, "回包里没有 surface 字段的写法"
    assert "ACTION:" in text, "回包里没有可照抄的 ACTION 行"
    assert "record_coverage" in text
    assert json.loads(out)["success"] is False


def test_wrong_field_name_also_returns_skeleton(coord, me) -> None:
    """★ 实地那个 bug 的形状：字段名猜成 `area` 时也要给骨架。"""
    out = _call(coord, me, {"area": "api-v1-fuzz", "outcome": "已排除"})
    text = _error_text(out)
    assert '"surface"' in text
    assert "ACTION:" in text
    assert json.loads(out)["success"] is False


def test_valid_args_records_and_returns_ok(coord, me) -> None:
    """正常路径不受影响：写进去、回 success、带上那条"干净也要记"的提醒。"""
    out = _call(
        coord,
        me,
        {
            "surface": "GET /api/v1/health",
            "risk": "信息泄露",
            "outcome": "已排除：只有 status",
        },
    )
    payload = json.loads(out)
    assert payload["success"] is True
    assert "GET /api/v1/health" in out
    assert coord.shared.coverage, "没有真的写进共享状态"


def test_outcome_defaults_to_placeholder(coord, me) -> None:
    """`risk` / `outcome` 省略时填「未标」，不报错 —— surface 是唯一必填。"""
    out = _call(coord, me, {"surface": "POST /api/v1/login"})
    payload = json.loads(out)
    assert payload["success"] is True
    entry = coord.shared.coverage["POST /api/v1/login"]
    assert entry["risk"] == "未标"
    assert entry["outcome"] == "未标"
