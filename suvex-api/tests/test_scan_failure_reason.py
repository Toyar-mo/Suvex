"""失败原因要**能读、且可行动** —— 别把用户推去读 Python 栈帧。

## 这次修的是什么（2026-09-27 的真实失败）

供应商返回 403 余额不足：

    openai.PermissionDeniedError: Error code: 403 - {'code': 'insufficient_user_quota'}

strix 在被拒的请求上**重试了几次**，于是日志末尾 25 行全是重复的栈帧

    File "/usr/lib/python3.13/asyncio/tasks.py", line 507, in wait_for

而唯一有信息量的那一行在几百行之前 —— **正好落在 `FAILURE_REASON_MAX`
（600 字）的截断之外**。

原来的 `_failure_reason` 直接取日志末尾，于是界面上是
「⚠️ 基础设施中断」+ 一堆 Python 文件名，还给了个展开按钮让人读栈帧。
而用户真正要做的事是**去充值**。

这不是排版问题：**那段堆栈把可行动的信息换成了不可行动的信息。**
"""

from __future__ import annotations

from pathlib import Path

from app.scans import service
from app.scans.models import Scan


def _log_path(scan: Scan) -> Path:
    return Path(scan.run_dir) / "scan.log"


def _frames(count: int) -> str:
    """一段逼真的重试栈帧 —— 长度足以把前面那句错误挤出截断范围。"""
    return "\n".join(
        f'  File "/usr/lib/python3.13/asyncio/tasks.py", line {500 + i}, in wait_for\n'
        f"    return await fut"
        for i in range(count)
    )


def test_known_quota_failure_is_explained_in_plain_words(scan: Scan) -> None:
    """★★ 余额不足要说成"去充值"，而不是让用户读 403 的栈帧。

    错误行刻意放在**最前面**、后面接 60 段栈帧 —— 复刻真实形态：
    真正的那句话被重试日志挤到截断线之外。
    """
    _log_path(scan).write_text(
        "openai.PermissionDeniedError: Error code: 403 - {'error': {'message': "
        "'预扣费额度失败, 用户剩余额度: ¥0.093782', 'code': 'insufficient_user_quota'}}\n"
        "Traceback (most recent call last):\n" + _frames(60) + "\n",
        encoding="utf-8",
    )
    scan.log_path = str(_log_path(scan))

    reason = service._failure_reason(scan, 1)

    # 第一句就必须是可行动的那句（会被截断截掉的话，放后面等于没放）
    assert reason.startswith("模型服务额度不足")
    assert "充值" in reason
    # 并且明确排除"目标/沙箱的锅" —— 否则用户会去排查错的方向
    assert "不是目标或沙箱的问题" in reason


def test_unknown_error_falls_back_to_the_last_error_line(scan: Scan) -> None:
    """认不出的错误：至少把 `某某Error: 说明` 那一行拎出来。

    取**最后一条** —— 同一次失败会被重试重复打印，最后那条才是终止它的。
    """
    _log_path(scan).write_text(
        "Traceback (most recent call last):\n"
        "RuntimeError: 第一次尝试失败\n" + _frames(30) + "\n"
        "ValueError: 最终把它打下来的那个\n",
        encoding="utf-8",
    )
    scan.log_path = str(_log_path(scan))

    reason = service._failure_reason(scan, 1)

    assert "ValueError: 最终把它打下来的那个" in reason


def test_log_without_any_error_line_still_shows_the_tail(scan: Scan) -> None:
    """没有错误行时退回日志末尾 —— 不能因为"没认出错误"就什么都不给。"""
    _log_path(scan).write_text(
        "docker: command not found\n请检查执行后端配置。\n", encoding="utf-8"
    )
    scan.log_path = str(_log_path(scan))

    reason = service._failure_reason(scan, 1)

    assert "command not found" in reason


def test_missing_log_is_not_an_error(scan: Scan) -> None:
    """日志文件不在（被清了 / 权限不对）也不该炸 —— 给一句能解释的话即可。"""
    scan.log_path = str(Path(scan.run_dir) / "does-not-exist.log")

    reason = service._failure_reason(scan, 1)

    assert "退出码" in reason
    assert reason  # 不能是空串 —— 空的原因等于没有原因


def test_agent_tool_output_429_is_not_read_as_rate_limit(scan: Scan) -> None:
    """★★ 目标自己回 429，不能被当成「模型服务限流」。

    2026-10-22 真跑撞到的形态：一次渗透里 agent 探测目标平台的
    `/auth/forgot-password`，对**存在**的用户它回 429（这是模型发现的
    "用户枚举"信号）。这行工具输出落在 scan.log 里，而 `_find_failure_line`
    扫的是日志尾部 256KB —— 其中混着 agent 自己的工具输出与推理，于是被
    `\\b429\\b` 这个宽正则一口咬住，把一个跑了 88 次真实工具的扫描报成
    「模型服务限流」。
    低报方向还恰好让人去查一个根本不存在的模型问题 —— 比不报更坏。
    """
    _log_path(scan).write_text(
        "POST /auth/forgot-password returned 429 for existing user\n"
        "目标对存在用户返回 429，可用于用户枚举。\n",
        encoding="utf-8",
    )
    scan.log_path = str(_log_path(scan))

    reason = service._failure_reason(scan, 1)

    # 不能是「模型服务限流」那句
    assert "模型服务限流" not in reason
    # 认不出已知失败 → 退回日志末尾，如实把工具输出给用户看
    assert "forgot-password" in reason


def test_real_vendor_429_is_still_explained_as_rate_limit(scan: Scan) -> None:
    """反过来：**真**的供应商 429 仍要说成限流（不能一刀切掉这颗子弹）。

    供应商的限流都带上下文短语（`Error code: 429` / `rate_limit_exceeded`
    / `429 Too Many Requests`），与 agent 的工具输出可区分。
    """
    for line in (
        "openai.RateLimitError: Error code: 429 - rate_limit_exceeded\n",
        "HTTP 429 Too Many Requests\n",
    ):
        _log_path(scan).write_text(line, encoding="utf-8")
        scan.log_path = str(_log_path(scan))
        reason = service._failure_reason(scan, 1)
        assert reason.startswith("模型服务限流"), f"应识别为限流：{line!r} -> {reason!r}"


def test_budget_exhaustion_is_explained_not_dumped(scan: Scan) -> None:
    """★ 轮次预算用尽是「没跑到收敛」，不是「坏了」—— 要说清下一步动作。

    多智能体尤其容易撞上（总预算被多个子 agent 分）。
    """
    _log_path(scan).write_text(
        "error: 总轮次预算已用尽（240 轮）。\n", encoding="utf-8"
    )
    scan.log_path = str(_log_path(scan))

    reason = service._failure_reason(scan, 1)

    assert "回合预算" in reason
    assert "不是执行失败" in reason
    # 给出唯一能做的事：调大回合上限或收窄目标
    assert "调大" in reason or "收窄" in reason


def test_did_not_start_names_strix_for_strix_backend(scan: Scan) -> None:
    """未启动的文案：strix 后端就说 strix。"""
    scan.log_path = str(Path(scan.run_dir) / "scan.log")
    _log_path(scan).write_text(
        "usage: strix [-h] --target TARGET\n"
        "strix: error: unrecognized arguments: --bogus\n",
        encoding="utf-8",
    )

    reason = service._failure_reason(scan, 2, strix_ran=False, runner_name=None)

    assert "strix 没能启动" in reason
    assert "完全没有对目标做任何事" in reason


def test_did_not_start_names_own_engine_for_agent_backend(scan: Scan) -> None:
    """★★ 自研引擎失败时**不能**说成 strix。

    2026-10-21 真跑撞到的形态：用户从「新建扫描」建的任务走 agent 后端，
    引擎没起来，界面上却显示「strix 没能启动」—— 一个他根本没在用的工具名。
    这里锁死：`runner_name="agent"` 时主语是「自研引擎」，且全文不出现 strix。
    """
    scan.log_path = str(Path(scan.run_dir) / "scan.log")
    _log_path(scan).write_text("Traceback (most recent call last):\n", encoding="utf-8")

    reason = service._failure_reason(scan, 2, strix_ran=False, runner_name="agent")

    assert "自研引擎 没能启动" in reason
    assert "完全没有对目标做任何事" in reason
    assert "strix" not in reason


def test_did_not_start_reads_runner_from_scan_when_not_passed(scan: Scan) -> None:
    """不显式传 runner_name 时退回 `scan.runner` —— finalize 传的就是它。"""
    scan.runner = "agent"
    scan.log_path = str(Path(scan.run_dir) / "scan.log")
    _log_path(scan).write_text("", encoding="utf-8")

    reason = service._failure_reason(scan, 2, strix_ran=False)

    assert "自研引擎 没能启动" in reason
    assert "strix" not in reason


def test_reasoning_that_mentions_rate_limiting_is_not_a_429(scan: Scan) -> None:
    """★★★ v0.69：模型在推理里**否定**限流（"No lockout or rate limiting confirmed"），
    不能被读成「模型服务限流」。

    2026-10-22 P0 重跑真跑暴露：旧的宽正则 `rate[_ ]?limit(?:...)?` 里后缀是**可选**的，
    于是裸的 `rate limiting` 也命中。一次跑了 351 条事件、1802 秒、6 个智能体的真实
    评估，日志尾部只有这一句推理带 "rate limiting"，整条扫描就被报成「模型服务限流」。
    """
    _log_path(scan).write_text(
        "[reasoning] No lockout or rate limiting confirmed. Let me test the login.\n"
        "[tool_call] run_shell curl -sS http://target/api/v1/users/\n",
        encoding="utf-8",
    )
    scan.log_path = str(_log_path(scan))

    reason = service._failure_reason(scan, 1)

    assert "模型服务限流" not in reason, f"推理里的 rate limiting 不能被当限流：{reason!r}"


def test_engine_terminal_error_line_wins_over_earlier_keyword_hits(scan: Scan) -> None:
    """★★★ v0.69：引擎写在**最后一行**的 `error: <真实原因>` 必须压过更靠前的
    关键词命中（工具输出/推理里命中已知错误短语的行）。

    真跑形态：日志中部有一行命中已知错误短语（例如某次请求真的撞到
    `rate_limit_exceeded`，或任何已知失败关键词），而末尾引擎写的是
    `error: 扫描超过设置页指定的单次超时，已被引擎停止。`。
    旧实现**从头往尾扫、命中即返回**，于是更早的命中盖掉真正的超时原因。
    这里的更早命中用**严格版仍会命中**的供应商短语，确保测试锁的是
    「顺序」而不是「正则宽严」。
    """
    _log_path(scan).write_text(
        "[tool_call] run_shell curl -sS http://target/api/v1/users/\n"
        "HTTP 429 Too Many Requests\n"  # 更早的、严格正则仍命中的一行
        "[tool_call] run_shell curl -sS http://target/api/v1/system/health\n"
        "error: 扫描超过设置页指定的单次超时，已被引擎停止。\n",
        encoding="utf-8",
    )
    scan.log_path = str(_log_path(scan))

    reason = service._failure_reason(scan, 1)

    assert "超时" in reason, f"应取引擎的终止行（超时），而不是更早的假命中：{reason!r}"
    assert "模型服务限流" not in reason


def test_engine_terminal_error_line_still_maps_known_failure(scan: Scan) -> None:
    """引擎终止行本身若命中已知错误（如额度不足），仍要给出那句更可操作的话 ——
    不能让「先取终止行」的优先级把已知失败的映射短路掉。"""
    _log_path(scan).write_text(
        "error: insufficient_user_quota: 剩余额度不足\n", encoding="utf-8"
    )
    scan.log_path = str(_log_path(scan))

    reason = service._failure_reason(scan, 1)

    assert "额度" in reason
