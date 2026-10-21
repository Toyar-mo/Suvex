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
