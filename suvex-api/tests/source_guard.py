"""按名字从磁盘 AST 取源码 —— 抗「import 之后源文件被改动」的行号漂移。

## 为什么需要这个

`inspect.getsource(obj)` 的实现是：拿 `obj.__code__.co_firstlineno`，
去**当前磁盘文件**里按行号切片。它假设「磁盘文件 == 被 import 的那份」。

这个假设在两种情况下不成立：

1. **测试进程跑着，源文件被改了**（并行会话、变异测试脚本、边跑边修）。
   `co_firstlineno` 还是旧值，磁盘上的文件行号已经全变了 → 切到**别的函数**。
   实测过一次：`inspect.getsource(multiagent.run_agent_loop)` 返回的是
   `@dataclass class AgentLoopResult` —— 三条接线测试因此报红，而红的理由是
   「`coord.note_action(` 不在源码里」，与真相毫无关系。
2. `.pyc` 比 `.py` 新（stale bytecode）时同理。

## 本模块怎么做

不用行号，用**名字**：解析磁盘文件的 AST，找到同名的 def/class 节点，
按 `node.lineno` / `node.end_lineno` 切。文件被加了几百行也不影响。

- 同名有多个时用 `obj.__qualname__` 消歧（取包含关系最深的那条链）。
- 找不到（函数是动态生成的、或来自 C 扩展）时退回 `inspect.getsource`。

## 用法

在测试里用 `from tests.source_guard import getsource`，或者依赖
`tests/conftest.py` 里那个 autouse fixture —— 它把 `inspect.getsource`
整体换成本模块的实现，所以**已有的 38 处调用点不用改**。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

#: 替换发生**之前**的原始实现。
#:
#: `tests/conftest.py` 的 `_stable_getsource` fixture 会把 `inspect.getsource`
#: 整体换成 `getsource`。对照测试（「裸 inspect 会漂移」）必须用这一份，
#: 否则它测的其实是守卫自己 —— 换成守卫后对照组前提就没了，
#: 会「因为守卫工作得太好」而假红。
original_getsource = inspect.getsource


def _node_span(node: ast.AST) -> tuple[int, int]:
    """节点的起止行（1-based，含装饰器）。"""
    start = node.lineno
    decorators = getattr(node, "decorator_list", None) or []
    if decorators:
        start = min(start, *(d.lineno for d in decorators))
    return start, node.end_lineno or node.lineno


def _qualname_chain(obj: object) -> list[str]:
    """`A.B.method` → `['A', 'B', 'method']`。"""
    qualname = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", "")
    return [p for p in str(qualname).split(".") if p and p != "<locals>"]


def _find_node(tree: ast.AST, chain: list[str]) -> ast.AST | None:
    """按限定名链在 AST 里找节点。

    从右往左匹配：先找名字等于 `chain[-1]` 的节点，再要求它的祖先
    名字序列从右往左与 `chain[:-1]` 一致。这样 `A.B.method` 与 `B.method`
    都能命中，`A.method` 与 `B.method` 不会互相顶替。
    """

    if not chain:
        return None
    target = chain[-1]
    want_parents = chain[:-1]

    candidates: list[tuple[ast.AST, list[str]]] = []

    def walk(node: ast.AST, parents: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            name = getattr(child, "name", None)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and name == target:
                candidates.append((child, list(parents)))
            child_parents = parents + [name] if isinstance(name, str) else parents
            walk(child, child_parents)

    walk(tree, [])

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][0]

    # 多个同名：优先祖先链尾部匹配最长的
    best: tuple[int, ast.AST] | None = None
    for node, parents in candidates:
        score = 0
        for depth in range(1, min(len(want_parents), len(parents)) + 1):
            if want_parents[-depth] == parents[-depth]:
                score = depth
            else:
                break
        if best is None or score > best[0]:
            best = (score, node)
    return best[1] if best else None


def getsource(obj: object) -> str:
    """按名字取 `obj` 在当前磁盘文件里的源码。找不到时退回 inspect。"""
    code = getattr(obj, "__code__", None)
    if not code:
        return inspect.getsource(obj)  # type: ignore[arg-type]

    path = Path(code.co_filename)
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return inspect.getsource(obj)  # type: ignore[arg-type]

    node = _find_node(tree, _qualname_chain(obj))
    if node is None:
        return inspect.getsource(obj)  # type: ignore[arg-type]

    start, end = _node_span(node)
    lines = text.splitlines(keepends=True)
    return "".join(lines[start - 1 : end])
