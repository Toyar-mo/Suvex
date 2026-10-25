"""`test_source_guard.py` 的被测夹具模块 —— 故意保持极小、无 import 依赖。

不要在这里加业务逻辑。它的唯一用途是提供各种形状的定义
（普通函数 / 同名方法 / 装饰器 / 嵌套函数），供漂移测试切片。
"""

from __future__ import annotations

from functools import wraps


def alpha() -> str:
    """第一个函数。漂移测试的主角。"""
    return "alpha"


def beta() -> str:
    """第二个函数。当文件被插行后，裸 inspect 会漂到这里。"""
    return "beta"


class First:
    """与 `Second` 有同名方法，用于验证限定名消歧。"""

    def run(self) -> str:
        return "first"


class Second:
    """见 `First`。"""

    def run(self) -> str:
        return "second"


def _tag(fn):  # type: ignore[no-untyped-def]
    @wraps(fn)
    def wrapper(*a, **kw):  # type: ignore[no-untyped-def]
        return fn(*a, **kw)

    return wrapper


@_tag
def decorated() -> str:
    """带装饰器的函数：切片必须从 `@` 开始。"""
    return "decorated"


def outer():  # type: ignore[no-untyped-def]
    """返回一个嵌套函数，用于验证 `<locals>` 的取源码。"""

    def inner() -> str:
        return "inner"

    return inner


# ──────────────────────────────────────────────────────────────
# 下面两个类**故意都**含一个同名嵌套函数 `build`。
#
# 只有「限定名链里过滤掉 `<locals>`」实现正确时，才能把那两份
# `build` 区分开：
#   - 过滤后：`Beta.make.build` 的 want_parents = ['Beta','make']
#             对 Beta 节点可匹配 2 层 → 得分 2；对 Alpha 节点只匹配 1 层 → 得分 1
#   - 不过滤：want_parents = ['Beta','make','<locals>']
#             两边都只能匹配 1 层（`<locals>` 不是 AST 节点，parents 里没有）
#             → 同分 → 取「第一个候选」→ 可能返回 Alpha 的 build
#
# `Alpha` 必须在 `Beta` 之前定义，这样「取第一个候选」会明确落到 Alpha 上，
# 漂移才可观察。顺序颠倒的话两者同分但先命中 Beta，反而看不出来。
# ──────────────────────────────────────────────────────────────
class Alpha:
    """同名嵌套函数的前一个（见 `Beta`）。"""

    def make(self):  # type: ignore[no-untyped-def]
        def build() -> str:
            return "alpha-build"

        return build


class Beta:
    """同名嵌套函数的后一个（见 `Alpha`）。"""

    def make(self):  # type: ignore[no-untyped-def]
        def build() -> str:
            return "beta-build"

        return build
