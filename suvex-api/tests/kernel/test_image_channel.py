"""守卫测试：图片到底有没有被送进对话（而不仅仅是"被取出来了"）。

## 这个文件在防什么

`view_image` 的失败模式不是抛异常，而是**静默地什么都没发生**：
工具把图取到、校验通过、返回一句"已挂到这一轮"，但调用方忘了把
`turn_images` 传给 `gateway.chat` —— 于是模型下一轮什么图也看不到，
却收到一句"图上来了"。模型于是开始编造页面上有什么。

`test_view_image.py` 覆盖的是工具本身（取文件、白名单、错误文案）。
这个文件覆盖的是**后面那一段**：从 `turn_images` 到 HTTP 请求体。

## 为什么直接测 `attach_images` 而不去跑整条 `_solve`

`_solve` 是个几千行的异步生成器，把它跑起来要沙箱、要模型、要库。
而"图挂不挂得上"是个纯函数问题：给定消息列表 + 图列表 + provider 形状，
返回什么。把它抽成模块级 `runner.attach_images()` 之后就能直接断言，
而且**单循环与多智能体共用这一份** —— 测一次，两条路径都覆盖。

真正容易漏的是"调用点忘了传"，那部分用 AST 静态断言兜（同一个文件）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.kernel import runner
from app.kernel.runner import ImagePayload, attach_images

RUNNER_SRC = Path(runner.__file__).resolve()
MULTI_SRC = RUNNER_SRC.with_name("multiagent.py")


def _png() -> ImagePayload:
    return ImagePayload(
        path="/work/shots/shot-1.png",
        media_type="image/png",
        data="iVBORw0KGgo=",
        size=8,
    )


# ── attach_images：挂法本身 ──────────────────────────────────────────────


def test_no_images_returns_the_same_list_untouched():
    """没有图时必须是**逐字节原样**返回，不能顺手复制或改形状。

    如果这里偷偷把 content 变成 list，所有纯文本请求都会带上一个
    单元素 content block —— 省钱没省到，还可能撞上不认识这个形状的端点。
    """
    msgs = [{"role": "user", "content": "ping"}]
    out = attach_images(msgs, [], anthropic=False)
    assert out is msgs, "没有图时不该新建列表"
    assert out == [{"role": "user", "content": "ping"}]


def test_image_is_merged_into_the_last_message_not_appended():
    """图并进最后一条，而不是新开一条 user 消息。

    新开一条会让这一轮出现两条连续的 user：OpenAI 兼容端点部分实现
    直接拒绝，Anthropic 侧要求 user/assistant 严格交替。
    """
    msgs = [
        {"role": "user", "content": "开始"},
        {"role": "user", "content": "工具 `view_image` 的输出：已挂上"},
    ]
    out = attach_images(msgs, [_png()], anthropic=False)

    assert len(out) == 2, "消息条数不能变 —— 图是并进去的，不是新开一条"
    assert out[0] == {"role": "user", "content": "开始"}, "前面的消息不该被动"
    assert out[1]["role"] == "user"
    assert isinstance(out[1]["content"], list)
    assert out[1]["content"][0] == {
        "type": "text",
        "text": "工具 `view_image` 的输出：已挂上",
    }, "原来那句文本要留在最前面，否则模型不知道这张图是哪来的"


def test_openai_shape_uses_image_url_with_data_prefix():
    out = attach_images([{"role": "user", "content": "看"}], [_png()], anthropic=False)
    block = out[-1]["content"][-1]
    assert block["type"] == "image_url"
    assert block["image_url"]["url"] == "data:image/png;base64,iVBORw0KGgo=", (
        "OpenAI 兼容形状要带 data: 前缀"
    )


def test_anthropic_shape_uses_source_without_data_prefix():
    """Anthropic 的 `data` 字段**不带** `data:` 前缀 —— 带了会 400 Invalid base64。"""
    out = attach_images([{"role": "user", "content": "看"}], [_png()], anthropic=True)
    block = out[-1]["content"][-1]
    assert block["type"] == "image"
    assert block["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": "iVBORw0KGgo=",
    }
    assert not block["source"]["data"].startswith("data:"), (
        "Anthropic 的 data 字段不能带 data: 前缀"
    )


def test_images_are_consumed_exactly_once():
    """★ 核心不变量：图只挂一轮。

    `messages` 每轮全量重发；一张图 base64 后 1.6 MB，留在历史里三轮
    就把上下文吃光。所以挂完必须清空来源列表。
    """
    images = [_png()]
    attach_images([{"role": "user", "content": "看"}], images, anthropic=False)
    assert images == [], "挂完必须清空，否则下一轮会重发这张图"

    # 第二轮：同一个列表（已空）再挂一次，必须不挂任何图
    out2 = attach_images([{"role": "user", "content": "第二轮"}], images, anthropic=False)
    assert out2 == [{"role": "user", "content": "第二轮"}], "第二轮不该再带图"


def test_empty_text_tail_does_not_emit_a_blank_text_block():
    """最后一条如果是空文本，不要塞一个空 text block —— 有些端点会 400。"""
    out = attach_images([{"role": "user", "content": ""}], [_png()], anthropic=False)
    kinds = [b["type"] for b in out[-1]["content"]]
    assert kinds == ["image_url"], f"不该有空 text block，实际 {kinds}"


def test_caller_messages_are_not_mutated():
    """调用方传进来的列表不能被就地改 —— 那是历史，改了下一次重发就脏了。"""
    msgs = [{"role": "user", "content": "原本的样子"}]
    out = attach_images(msgs, [_png()], anthropic=False)
    assert msgs == [{"role": "user", "content": "原本的样子"}], "原列表被就地修改了"
    assert out is not msgs


def test_multiple_images_all_ride_along():
    a = _png()
    b = ImagePayload(
        path="/work/shots/shot-2.png",
        media_type="image/jpeg",
        data="AAAA",
        size=4,
    )
    out = attach_images([{"role": "user", "content": "两张"}], [a, b], anthropic=False)
    blocks = out[-1]["content"]
    assert [x["type"] for x in blocks] == ["text", "image_url", "image_url"]
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert blocks[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")


# ── 调用点：两条循环都必须真的把图传下去 ────────────────────────────────


def _func_calls(path: Path, func_name: str) -> list[ast.Call]:
    """找出 `func_name` 内的所有调用节点（含它内部嵌套函数里的调用）。

    ⚠️ 必须同时认 `ast.FunctionDef` **和** `ast.AsyncFunctionDef`：两条
    解题循环都是 `async def`。只认前者会让本文件的断言全部假红 ——
    我自己就先踩了一次：报"没有调用 attach_images"，而源码里明明就在。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls: list[ast.Call] = []
    found_func = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            found_func = True
            calls.extend(sub for sub in ast.walk(node) if isinstance(sub, ast.Call))
    assert found_func, f"{path.name} 里没有名为 {func_name} 的函数（函数改名了？）"
    return calls


def _callee_name(call: ast.Call) -> str:
    fn = call.func
    return fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")


def _expr_label(node: ast.expr) -> str:
    """把表达式还原成源码文本（`_messages_with_images()` 这种要能读出来）。

    不能用 `getattr(node, "id", "")`：那对 `ast.Call` 恒返回空串，
    断言消息里就只剩一个 `['']`，看不出实际传了什么 —— 排查时等于没说。
    """
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 - unparse 在极小概率下会抛，退化成类型名
        return type(node).__name__


def _call_kwargs_of(path: Path, func_name: str, callee_attr: str) -> list[str]:
    """找出 `func_name` 里对 `callee_attr(...)` 的调用实参名。"""
    found = [
        [kw.arg or "?" for kw in call.keywords]
        for call in _func_calls(path, func_name)
        if _callee_name(call) == callee_attr
    ]
    assert found, f"在 {path.name} 的 {func_name}() 里没找到对 {callee_attr}() 的调用"
    return [arg for call in found for arg in call]


@pytest.mark.parametrize(
    ("path", "func_name"),
    [
        (RUNNER_SRC, "solve_in_sandbox"),
        (MULTI_SRC, "run_agent_loop"),
    ],
    ids=["单循环", "多智能体"],
)
def test_both_loops_pass_images_into_dispatch_tool(
    path: Path, func_name: str
) -> None:
    """★ 两条解题循环都必须把 `turn_images` 传给 `dispatch_tool`。

    漏掉也不会报错 —— 工具会说"已挂上"，但图躺在没人读的列表里。
    这是本文件存在的理由：那个缺口不产生异常，只产生幻觉。
    """
    kwargs = _call_kwargs_of(path, func_name, "dispatch_tool")
    assert "images" in kwargs, (
        f"{path.name} 的 {func_name}() 调 dispatch_tool 时没传 images= —— "
        "view_image 会取到图、报告已挂上，但模型永远看不到。"
    )


@pytest.mark.parametrize(
    ("path", "func_name"),
    [
        (RUNNER_SRC, "solve_in_sandbox"),
        (MULTI_SRC, "run_agent_loop"),
    ],
    ids=["单循环", "多智能体"],
)
def test_both_loops_send_images_with_the_request(path: Path, func_name: str) -> None:
    """★ 两条循环调 `gateway.chat` 时都必须用**带图**的消息。

    这条和上一条是两个不同的洞：图进了收集器 ≠ 图进了请求体。
    """
    hits = [
        _expr_label(call.args[0])
        for call in _func_calls(path, func_name)
        if _callee_name(call) == "chat" and call.args
    ]
    assert hits, f"{path.name} 的 {func_name}() 里没找到 gateway.chat(消息) 调用"
    assert any("image" in h for h in hits), (
        f"{path.name} 的 {func_name}() 发给模型的消息是 {hits}，"
        "不是带图的那份 —— 图会被无声丢掉。"
    )


def test_both_loops_share_one_implementation():
    """两条循环必须调同一个 `attach_images`，不许各写一份。

    各写一份必然分叉，而分叉的症状是"单循环能看图、多智能体看不见"——
    多智能体在开关打开时是**默认**路径，这缺口最难发现。
    """
    for path, func_name in ((RUNNER_SRC, "solve_in_sandbox"), (MULTI_SRC, "run_agent_loop")):
        used = any(
            _callee_name(call) == "attach_images" for call in _func_calls(path, func_name)
        )
        assert used, f"{path.name} 的 {func_name}() 没有调用共享的 attach_images()"


def test_multiagent_imports_the_shared_helper():
    """多智能体必须从 runner 导入 `attach_images`（而不是自己定义一份同名函数）。"""
    tree = ast.parse(MULTI_SRC.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "app.kernel.runner"
        for alias in node.names
    }
    assert "attach_images" in imported, (
        "multiagent.py 没从 runner 导入 attach_images —— "
        "是不是又抄了一份本地实现？"
    )


def test_no_image_path_does_not_require_anthropic():
    """★ 没有图的那一轮，**不许**碰 `gateway.anthropic`。

    ## 这条守的是一个我自己造出来的回归

    `anthropic` 只用来在 `_image_block()` 里二选一形状 —— 没图时它
    一个字节都不影响结果。但它写成 `anthropic=gateway.anthropic` 是
    **实参**，无图时也会被求值。

    于是一个"本来完全不必关心看图"的东西突然成了前置条件：单测里的
    假 gateway 没有 `anthropic` 属性 → `tests/kernel/test_runner.py` 与
    `tests/kernel/test_multiagent.py` **34 条**同时
    `AttributeError: '_FakeGateway' object has no attribute 'anthropic'`。

    它们是当场变红的（不是静默错误），但**症状离原因很远**：报错在
    `runner.py` 的 `attach_images(...)` 那行，而坏掉的是"看图功能的
    telemetry 泄漏进了不看图的路径"。所以单独留一条，让下一次有人想
    把 `gateway.anthropic` 挪出 if 时，这条会直接点名。

    ## 判据

    `_messages_with_images()` 的第一件事必须是 `if not turn_images: return`。
    """
    for path, func_name in ((RUNNER_SRC, "solve_in_sandbox"), (MULTI_SRC, "run_agent_loop")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        targets = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == func_name
        ]
        assert targets, f"{path.name} 里没有 {func_name}()"
        clos = [
            sub
            for sub in ast.walk(targets[0])
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
            and sub.name == "_messages_with_images"
        ]
        assert clos, f"{path.name} 的 {func_name}() 里没有 _messages_with_images()"
        body = clos[0].body
        first_if = next((n for n in body if isinstance(n, ast.If)), None)
        assert first_if is not None, (
            f"{path.name} 的 _messages_with_images() 里没有提前返回 —— "
            "无图时会去求值 gateway.anthropic，给不看图的路径加前置条件"
        )
        cond = ast.unparse(first_if.test)
        assert "turn_images" in cond, (
            f"{path.name} 的 _messages_with_images() 第一个 if 判的是 `{cond}`，"
            "不是 `not turn_images` —— 提前返回没守住无图这条路"
        )
        returns = [n for n in first_if.body if isinstance(n, ast.Return)]
        assert returns, f"{path.name} 的 `if {cond}:` 里没有 return"
        returned = ast.unparse(returns[0].value) if returns[0].value else ""
        assert returned.endswith("_messages()"), (
            f"{path.name} 无图时返回的是 `{returned}`，应当直接是 `_messages()`"
        )
