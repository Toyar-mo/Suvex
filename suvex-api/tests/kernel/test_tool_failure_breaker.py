"""连续失败熔断的判据（2026-11-08）。

## 这个文件在防什么

实测那天 4 道题跑了 25 分钟、沙箱里一个文件都没有：模型反复调一个
不存在的 MCP 工具，每轮只收到一句 `unknown tool`。提示词里那句
"连续 3 次没有新信息就换方向"是**建议**，代码不认它 —— 唯一的止损
是烧完 500 轮。

熔断分成两级：连续 4 次 → 塞一条"换路子"的用户消息；连续 10 次 →
中止本题。这里只钉**判据本身**：什么算一次"工具失败"。

判据必须是**白名单式的前缀匹配**，不能去匹配 `error` 字样 ——
模型 `cat` 出来的文件里常常写着 error，按关键字判会把成功的调用
算成失败，进而误熔断一道正常的题。
"""

from __future__ import annotations

from app.kernel.runner import (
    TOOL_FAILURE_ABORT_AT,
    TOOL_FAILURE_HINT_AT,
    _looks_like_tool_error,
)


def test_the_four_dispatch_error_prefixes_count_as_failure() -> None:
    assert _looks_like_tool_error("拒绝执行：只能访问本题靶站")
    assert _looks_like_tool_error("工具执行出错（McpError）：连接超时")
    assert _looks_like_tool_error("未知工具 `ctf2_x`。可用工具只有：run_shell, ...")
    assert _looks_like_tool_error("参数错误：需要 `connection` 和 `tool`")


def test_ordinary_output_containing_error_words_is_not_a_failure() -> None:
    # 这是最容易写错的一条：文件内容里出现 error 不代表工具失败
    assert not _looks_like_tool_error("nginx: [error] open() failed while reading log")
    assert not _looks_like_tool_error("error_handler.php\n")
    assert not _looks_like_tool_error("")


def test_thresholds_are_ordered() -> None:
    assert 0 < TOOL_FAILURE_HINT_AT < TOOL_FAILURE_ABORT_AT
