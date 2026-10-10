"""读取一次扫描写出来的产物文件（任务 18 / 19）。

## 为什么单独一个模块

`flag.py` 要找 flag，`writeup.py` 要拼题解 —— 两者都要做同一件事：
在 `strix_runs/` 下认出**这一次**写出来的那些文件，然后把它们读进来。

各写一份的话，"最新的那个目录"这条判断迟早会分叉，
而分叉的症状很隐蔽：**flag 找到了、题解里却是空的**（或者反过来）。
同一件事实只能有一个出处，这是本项目一贯的取向。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("suvex.scans")

# 单个文件最多读多少字节。报告与 notes 都是几十 KB 级别，
# 给到 2 MiB 是"绝不可能截到正文"的量级，同时挡住意外指向一个巨大文件的情况。
MAX_READ = 2 * 1024 * 1024

# strix 那份人读的报告固定叫这个名字。
REPORT_NAME = "penetration_test_report.md"


def read_text(path: Path) -> str:
    """读一个文本文件。**读不到就当空串** —— 缺文件是正常情况，不是错误。"""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(MAX_READ)
    except OSError:
        return ""


def run_output_dirs(run_dir: str | Path | None) -> list[Path]:
    """`strix_runs/` 下的各次运行目录，**最新的在前**。

    正常一次扫描只有一个；取最新是防御性的 —— 万一续跑或重试留下了多个，
    至少先看的是这一次真正写出来的那份。

    目录不存在时返回空列表，不抛：调用方（续跑、收尾、读题解）都在
    "这一次可能什么都没产出"的语境里，抛异常只会让一次正常结束的扫描
    被标成失败 —— 而原因是"读不到一个可选文件"。
    """
    if not run_dir:
        return []
    root = Path(run_dir) / "strix_runs"
    if not root.is_dir():
        return []
    dirs = [entry for entry in root.iterdir() if entry.is_dir()]
    return sorted(dirs, key=lambda path: path.stat().st_mtime, reverse=True)


def latest_report(run_dir: str | Path | None) -> str:
    """读出这次扫描的**人读报告**全文。没有就返回空串。"""
    for directory in run_output_dirs(run_dir):
        text = read_text(directory / REPORT_NAME)
        if text:
            return text
    return ""


__all__ = ["latest_report", "MAX_READ", "read_text", "REPORT_NAME", "run_output_dirs"]
