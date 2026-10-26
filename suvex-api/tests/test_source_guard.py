"""`tests/source_guard.py` 的守卫测试。

核心要证明的只有一件事：**源文件被改行之后，`getsource` 仍能取到正确的函数**，
而 `inspect.getsource` 会漂移到别处。

## 怎么造出真实的漂移局面（第一版造错了，记下来）

漂移的定义是：**代码对象带着旧的行号，但磁盘文件已经变了**。

第一版把改过的内容写进 `tmp_path`，却忘了模块的 `__code__.co_filename`
仍指向真实文件 —— 于是 `getsource` 和 `inspect` 读的都是**没被改的真实文件**，
四条测试全是空转（两条「通过」是因为断言写得太松）。

正确配方（本文件 `drifted` fixture 用的就是它）：
1. 把原源码复制到 `tmp_path`，`importlib` 从那里 import → `co_filename` 指向副本；
2. **就地覆盖**那份副本，在第一个定义之前插入 N 行；
3. 此时代码对象的 `co_firstlineno` 是旧值，而文件内容整体下移了 N 行。

`getsource` 按名字在 AST 里找 → 正确。
`inspect.getsource` 按旧行号切片 → 切到填充行/别的函数。
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

from tests.source_guard import getsource, original_getsource

INSERTED_LINES = 40


def _insert_before_first_definition(text: str, count: int) -> str:
    """在第一个 def/class 之前插入 `count` 行填充注释。"""
    lines = text.splitlines(keepends=True)
    first_def = next(
        i for i, ln in enumerate(lines) if ln.lstrip().startswith(("def ", "class ", "async def "))
    )
    padding = "".join(f"# 插入的填充行 {i}\n" for i in range(count))
    return "".join(lines[:first_def]) + padding + "".join(lines[first_def:])


@pytest.fixture()
def drifted(tmp_path: Path) -> ModuleType:
    """import 一份副本 → 就地给它插行 → 返回那个「行号已过期」的模块。"""
    origin = Path(__file__).resolve().parent / "_drift_fixture.py"
    original_text = origin.read_text(encoding="utf-8")

    copy_path = tmp_path / "_drift_fixture.py"
    copy_path.write_text(original_text, encoding="utf-8")

    name = "_drift_fixture_copy"
    spec = importlib.util.spec_from_file_location(name, copy_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)

    stale_lineno = mod.alpha.__code__.co_firstlineno

    # ★ 就地覆盖：代码对象保留旧行号，磁盘文件整体下移
    shifted = _insert_before_first_definition(original_text, INSERTED_LINES)
    copy_path.write_text(shifted, encoding="utf-8")

    # 断言这个局面真的造出来了 —— 否则整组测试是空转
    assert mod.__file__ == str(copy_path), "co_filename 没指向副本"
    assert mod.alpha.__code__.co_firstlineno == stale_lineno
    fresh = copy_path.read_text(encoding="utf-8").splitlines()
    assert "def alpha" not in fresh[stale_lineno - 1], "填充没生效：旧行号处仍是 def alpha，造不出漂移"

    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


def test_guard_survives_inserted_lines(drifted: ModuleType) -> None:
    """★ 核心：文件被插了 40 行，按名字仍取到正确函数。"""
    src = getsource(drifted.alpha)

    assert "def alpha" in src, f"取到的不是 alpha：{src[:80]!r}"
    assert '"alpha"' in src
    assert "def beta" not in src, "漂移到 beta 了"
    # 取到的是完整、可解析的定义
    assert ast.parse(textwrap.dedent(src)).body


def test_guard_distinguishes_same_name_in_different_classes(drifted: ModuleType) -> None:
    """同名方法在两个类里 —— 按限定名链消歧。"""
    a = getsource(drifted.First.run)
    b = getsource(drifted.Second.run)
    assert '"first"' in a, a
    assert '"second"' not in a, f"First.run 漂到了 Second.run：{a!r}"
    assert '"second"' in b, b
    assert '"first"' not in b, f"Second.run 漂到了 First.run：{b!r}"


def test_guard_decorated_function_keeps_decorators(drifted: ModuleType) -> None:
    """装饰器属于定义的一部分，切片必须从 `@` 开始。"""
    src = getsource(drifted.decorated)
    assert src.lstrip().startswith("@"), f"装饰器丢了：{src[:60]!r}"
    assert "def decorated" in src


def test_guard_nested_function(drifted: ModuleType) -> None:
    """嵌套函数（`<locals>`）也要能取到。"""
    fn = drifted.outer()
    src = getsource(fn)
    assert "def inner" in src
    assert '"inner"' in src


def test_guard_disambiguates_same_nested_name_across_classes(drifted: ModuleType) -> None:
    """★ 两个类各有一个同名嵌套函数 —— 必须靠限定名链分辨。

    这条专门盯「`_qualname_chain` 是否过滤 `<locals>`」：
    不过滤的话两边都只能匹配 1 层、同分，于是固定取第一个候选
    （`Alpha.build`），`Beta.build` 就取错。
    """
    a = getsource(drifted.Alpha().make())
    b = getsource(drifted.Beta().make())

    assert '"alpha-build"' in a, a
    assert '"beta-build"' not in a, f"Alpha.build 漂到了 Beta.build：{a!r}"
    assert '"beta-build"' in b, f"Beta.build 取错了：{b!r}"
    assert '"alpha-build"' not in b, f"Beta.build 漂到了 Alpha.build：{b!r}"


def test_plain_inspect_drifts_under_same_conditions(drifted: ModuleType) -> None:
    """对照组：证明守卫**不是白写的** —— 裸 inspect 在同样条件下会错。

    ⚠️ 必须用 `source_guard.original_getsource`（替换前抓下的那份），
    不能用 `inspect.getsource` —— conftest 的 `_stable_getsource` fixture
    已经把后者换成守卫本身了，直接调它测的是守卫，前提不存在。

    断言用「**不等于**正确答案」而不是「必然切到别的函数」：
    实测漂移形态取决于旧行号落在哪 —— 这里落在模块 docstring 里，
    `getblock` 会一路吞到下一个 dedent，把填充行和 alpha 一起带出来。
    那是**污染**，不是正确切片。这条红掉的唯一含义是
    「`inspect.getsource` 本身变稳了，守卫可以删」。
    """
    good = getsource(drifted.alpha)
    bad = original_getsource(drifted.alpha)

    assert bad.strip() != good.strip(), (
        f"裸 inspect 竟然和正确答案一字不差，说明本测试构造失效：{bad[:120]!r}"
    )
    # 污染必须看得见：填充行或别的定义被卷进来了
    assert "# 插入的填充行" in bad, f"漂移形态与预期不同（没卷进填充行），检查构造：{bad[:160]!r}"


def test_fallback_does_not_recurse_when_the_source_is_wrapped() -> None:
    """★ 退回分支不得递归 —— 这是 2026-10-26 全量测试红 6 条的真正原因。

    ## 那条 bug 的形状

    `tests/conftest.py` 的 autouse fixture 把 `inspect.getsource` **整体
    换成了 `getsource` 本身**。而 `getsource` 在「AST 里按名字找不到定义」
    时会退回 `inspect.getsource(obj)` —— 于是变成调自己，无限递归。
    堆栈里全是 `ast.py:274`，完全看不出是自家守卫的问题。

    当时六条一起红，而它们与本轮改动毫无关系：
    `test_lifespan_ensures_the_network_after_sweeping_orphans`、
    `test_history_does_not_touch_verdict`、两条
    `test_run_worker_passes_*_to_the_async_body`、
    `test_worker_crash_fallback_covers_environment_ready`、
    `test_autosync_records_its_last_error`。

    ## 为什么不是假想情形

    `app.main.lifespan` 上有 `@asynccontextmanager`，装饰后
    `__code__.co_filename` 是 `contextlib.py`（装饰器内部的 wrapper），
    于是「去那个文件里找叫 lifespan 的定义」必然失败 → 一定走退回分支。
    **任何一个被装饰的函数**都可能是这个形状。

    ## 这条测试怎么钉住它

    直接把 `inspect.getsource` 换成守卫（与 conftest 做的一样），
    然后取一个**装饰过**的函数 —— 递归的话这里会抛 RecursionError；
    修好之后应当有限地返回一段源码。
    """
    import inspect

    from app import main

    wrapped = main.lifespan

    # 先确认前提：它确实被装饰过、__code__ 指向的不是 main.py。
    # 前提变了（比如 python 版本换了对 asynccontextmanager 的实现），
    # 这条测试就失效了，要换一个被装饰的函数重新构造。
    assert wrapped.__code__.co_filename.endswith("contextlib.py"), (
        f"前提变了：lifespan 的 __code__ 来自 {wrapped.__code__.co_filename}，"
        "不再是 contextlib.py —— 这条测试构造失效，需换一个被装饰的函数"
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(inspect, "getsource", getsource)
        # 递归的话这里抛 RecursionError；正常运行返回 wrapper 的源码
        src = getsource(wrapped)

    assert src.strip(), "取到空源码"
    # 退回分支给的是 contextlib 里的 wrapper，不是 lifespan 本体 ——
    # 这不理想，但它是**有限**的、可解释的退化；递归不是。
    assert "def " in src or "@" in src, f"退回结果不像源码：{src[:120]!r}"
