"""第三批修复的守卫：资源、边界与去重（任务 21）。

## 这批的共同点：**承诺与实现不符**

| 位置 | 承诺（注释/docstring） | 实现 |
| --- | --- | --- |
| `sweep_orphan_sandboxes` | 「前缀是判据的全部」 | `name=` 是**子串**匹配，不是前缀 |
| `_run_worker` | 「共享 client 由本函数负责，不假手他人」 | `finally` 里没有 `aclose()` |
| `_tool_http_request` | `_clip` 限制了输出 | 只截**显示**，`response.text` 全量读进内存 |
| `useActivityStream` | 实例内有 `emittedIds` 防重复 | 跨实例（切扫描）没有防线 |

前三条是后端、最后一条是前端 —— 但形状一样：
**一个机制在某一层建立了，另一层没有**。
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

from app.ctf2 import runner

_WEB_SRC = Path(__file__).resolve().parents[2] / "suvex-web" / "src"


def _code_of(func) -> str:  # noqa: ANN001
    """取函数的代码，**去掉注释与 docstring**。

    ⚠️ 必须去注释：这个项目的注释会引用被修掉的写法来解释改了什么，
        直接子串匹配会把注释当代码（本批测试的第一版就假红过两次）。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# ══════════════════════════════════════════════════════════
# 一、容器清理必须真的只清自己的
# ══════════════════════════════════════════════════════════


def test_orphan_sweep_anchors_the_name_filter() -> None:
    """★ `docker ps --filter name=` 必须锚定前缀。

    ## 为什么不锚定是危险的

    Docker 的 `name` 过滤器是**子串/正则匹配**，不是前缀匹配。
    而 `sweep_orphan_sandboxes` 的整个安全论证建立在
    「凡是叫 `ctf2-` 开头的都是上一次运行遗留的容器」上 ——
    不锚定的话，一个叫 `my-ctf2-helper` 的无关容器会被 `docker rm -f` 删掉。

    当前没有别的容器名含 `ctf2-`，所以这是**潜在**风险。
    但"论证与实现不符"本身就该修：下一个读到 docstring 的人
    会以为这里已经防住了。
    """
    source = _code_of(runner.sweep_orphan_sandboxes)

    assert "name=^" in source, (
        "`sweep_orphan_sandboxes` 的 docker filter 没有锚定前缀 —— "
        "`name=ctf2-` 是子串匹配，会误删名字里含 `ctf2-` 的其他容器。\n"
        "修法：`name=^{SANDBOX_NAME_PREFIX}`（同文件的 `ensure_ctf2_network` 就是这么写的）。"
    )


# ══════════════════════════════════════════════════════════
# 二、共享 client 必须被关掉
# ══════════════════════════════════════════════════════════


def test_worker_closes_the_shared_client() -> None:
    """★ `_run_worker` 要关掉它声明"由自己负责"的那个 client。

    注释原话：「它由本函数负责，不假手他人」—— 而原来 `finally` 里
    没有任何 `aclose()`，全文也没有第二处。

    ⚠️ 影响面**不要夸大**：CPython 的引用计数通常会在协程帧释放时回收它，
        所以这不是必然的 fd 泄漏。但每道题一个连接池、几十道题下来，
        把它变成确定的比依赖 GC 时机好。
    """
    source = _code_of(runner._run_worker)

    assert "aclose" in source, (
        "`_run_worker` 没有关掉共享 client —— 它声明了所有权却没落实，"
        "每个 worker 结束后连接池都进 GC 路径。"
    )


def test_client_is_closed_after_releasing_the_environment() -> None:
    """★★ 关闭顺序不能反：`aclose()` 必须在 `release_environment` **之后**。

    `release_environment` 还要用这个 client 发 DELETE 请求。
    放在它之前会把那次请求变成「客户端已关闭」的错误 ——
    而那正是 `release_environment` 的 docstring 里记过的那个
    `RuntimeError`（httpx 在客户端已关闭后抛的）。
    """
    source = _code_of(runner._run_worker)

    release_at = source.find("release_environment")
    close_at = source.find("aclose")
    assert release_at != -1 and close_at != -1, "两个调用都应该在"
    assert close_at > release_at, (
        "`aclose()` 出现在 `release_environment` **之前** —— "
        "而后者还要用这个 client 发请求，会撞上「客户端已关闭」的错误。"
    )


# ══════════════════════════════════════════════════════════
# 三、HTTP 工具要有读取上限
# ══════════════════════════════════════════════════════════


def test_http_tool_streams_with_a_read_cap() -> None:
    """★★ `http_request` 必须流式读 + 上限，不能 `response.text`。

    ## 为什么这是安全问题而不只是优化

    `_clip()` 只截断**显示**，不限制**读取**。而白名单里的主机是
    **题目环境**，内容由出题人可控 —— 一个返回几十 GB 的端点就能把
    宿主打爆（OOM），而宿主上跑着 API、调度器和所有正在解题的 worker。

    ## 判据

    · 用 `aiter_bytes()`（流式）而不是 `response.text`（全量）；
    · 有一个上限常量并在循环里 break。
    """
    source = _code_of(runner._tool_http_request)

    assert "aiter_bytes" in source, (
        "`http_request` 没有流式读 —— `response.text` 会把整个响应体读进宿主内存，"
        "而白名单主机的内容由出题人可控。"
    )
    assert "response.text" not in source, (
        "`http_request` 里还有 `response.text` —— 那是全量读取，没有上限。"
    )
    assert "_READ_CAP" in source, "流式读必须配一个上限，否则只是换个方式读满"


def test_http_tool_tells_the_model_when_truncated() -> None:
    """截断要**说出来**，不能静默。

    静默截断会让模型以为"响应就这么长"，从而得出错误结论 ——
    而它没有任何线索去怀疑。
    """
    source = _code_of(runner._tool_http_request)

    assert "truncated" in source, (
        "`http_request` 截断后没有告诉模型 —— 它会以为响应就这么长，"
        "据此得出错误结论而毫无线索。"
    )


# ══════════════════════════════════════════════════════════
# 四、前端：跨实例的事件去重
# ══════════════════════════════════════════════════════════


def _read_web(relative: str) -> str:
    path = _WEB_SRC / relative
    assert path.is_file(), f"找不到前端文件：{path}"
    return path.read_text(encoding="utf-8")


def test_activity_flush_dedupes_by_id() -> None:
    """★★ `useActivityStream` 的 flush 必须按 id 去重。

    ## 为什么需要（两道防线各管一层）

    `activity-stream.ts` 里的 `emittedIds` 防的是**同一个实例**内的重复
    （StrictMode 双挂载 —— 那个 bug 已经修过，见它的 `epoch` 注释）。
    但它是**闭包内变量**，实例一换就全没了。

    而 `ActivityPage` 在 `scanFilter` 变化时会 `createSseEventSource`
    建**新实例** —— 于是：

        `?scan=A` → `?scan=B` → 回 `?scan=A`

    新实例的 `emittedIds` 是空的，seed 把同一批历史事件**再 emit 一遍**，
    与 `events` 里旧条目重复，`key={event.id}` 也会撞车。

    ## 判据

    flush 里必须**真的用那个 Set 过滤批次**。

    ⚠️ 第一版这条断言写的是 `"new Set(prev.map" in source or "seen.has" in source`
        —— 而把 `fresh = batch.filter(...)` 改成 `fresh = batch`（等于去掉去重）
        之后它**仍然通过**：因为 `new Set(prev.map(...))` 那行还在。
        也就是说那条断言对"去重是否真的生效"是**空洞的**。
        是变异测试（拆掉去重后它没变红）把这个漏洞抓出来的。

    现在断言的是那个**过滤表达式本身** —— 只建 Set 不用它过滤，等于没去重。
    """
    source = _read_web("hooks/useActivityStream.ts")

    assert ".filter((e) => !seen.has(e.id))" in source, (
        "`useActivityStream` 的 flush 没有真的用 Set 过滤批次 —— "
        "切换扫描（A→B→A）时同一批历史事件会出现两遍，且 React key 撞车。\n"
        "（注意：只建了 Set 但没用它过滤，等于没去重。）"
    )


def test_activity_stream_resets_state_on_source_change() -> None:
    """★★ `source` 变化时要清空事件与派生状态。

    这是上一条的**根治**：光去重的话，旧流的事件仍然留在列表里
    （它们不属于当前视图）。`source` 变了就意味着"看的不是同一条流了"。

    派生状态有三个要一起清：
      · `newWhilePaused`（属于旧流的暂停计数）；
      · `latestBatchIds`（旧 id 在新流里没有意义）；
      · `hasSeededRef`（新流要重新经历"首批不播动画"）。
    """
    source = _read_web("hooks/useActivityStream.ts")

    # effect 的清理函数里应当有这几行
    for needle, why in (
        ("setEvents([])", "事件列表"),
        ("setLatestBatchIds(new Set())", "入场动画批次"),
        ("setNewWhilePaused(0)", "暂停期间新增计数"),
        ("hasSeededRef.current = false", "seed 标记"),
    ):
        assert needle in source, (
            f"`source` 变化时没有重置{why}（缺 `{needle}`）—— "
            "切扫描后会残留上一条流的状态，表现为重复条目或错误的角标数字。"
        )


def test_activity_page_total_uses_filtered_events() -> None:
    """★ 名册的「全部活动 N 条」要用**过滤后**的事件。

    `totalEvents={events.length}` 依赖"订阅源恰好已经按 scanId 过滤过"
    这个隐含前提。一旦改成"订阅全局流 + 前端过滤"（`scanEvents` 那个
    useMemo 已经在了，这是很自然的一步演进），它就会显示**全站**事件
    总数，而用户以为是这次扫描的 —— 数字偏大且看起来完全合理。
    """
    source = _read_web("pages/ActivityPage.tsx")

    assert "totalEvents={scanEvents.length}" in source, (
        "名册的 totalEvents 用的是未过滤的 events —— 它依赖"
        "「订阅源恰好已过滤」这个隐含前提，改成全局流后会显示错误的数字。"
    )
