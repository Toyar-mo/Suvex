"""从扫描产物里认出 CTF 的 flag（任务 18）。

## 为什么这件事需要一个专门的模块

对一次 `captureFlag` 扫描来说，**flag 就是全部交付物**。而它此前只活在两个
地方：报告文件里的一个代码块，以及 `finish_scan` 事件 `detail` 的 JSON 里 ——
而那条事件的 `summary` 是被 `_clip` 截断的 JSON 原文
（`finish_scan {"executive_summary":"本次评估的目标是…`），
**flag 正好落在截断点之后**。

于是实测撞到的现象是：任务跑完、agent 明确说"flag 已验证"，
而用户在界面上**看不到那个 flag**。他能做的只有去展开 JSON 或者上机器读文件。

这个模块把那件事变成一次抽取，结果存进 `scans.flag`，由界面直接显示。

## 两条来源，优先级明确

1. **agent 自己写的标记行**（`FLAG: xxx`）—— 最可靠。
   它是被协议**明确要求**的（见 `runner.EXPLORATION_PROTOCOL`），
   所以它不是"我们猜出来的形状"，而是"它按约定告诉我们的"。
2. **报告里的花括号串** —— 兜底。只在**不歧义**时才采纳（见下）。

## ★ 宁可返回 `None`，也绝不猜

匹配到**多个不同**的花括号串时返回 `None`，而不是"挑一个最像的"。

理由是用户会**直接拿这个值去提交答案**。把"我猜的"和"它确实找到了"混在
一起，这个字段就再也不能信了 —— 而他看到空白至少知道"得自己去报告里找"。
一个看起来确定的错值，比一块诚实的空白坏得多（与 `runner.can_resume`
里"给它一句能解释的话，而不是让它自己去撞"是同一条原则）。

## 只在 `captureFlag` 上用

真实渗透报告里 `{...}` 形状的东西太多了（payload、模板占位符、示例令牌），
对 `findVulns` 跑这套抽取会稳定地产出假 flag。调用方负责按 goal 分支，
见 `service.finalize`。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from app.scans.artifacts import read_text, run_output_dirs

logger = logging.getLogger("suvex.scans")

# ── 来源 1：agent 明确写的标记行 ────────────────────────────
#
# `FLAG: xxx` / `flag：xxx`（中英文冒号都收），整行独占。
# 要求独占一行是为了避开"正文里顺口提到 FLAG: 这个词"那种误命中。
_FLAG_LINE = re.compile(r"^[ \t]*(?:FLAG|Flag)\s*[:：][ \t]*(\S+)[ \t]*$", re.MULTILINE)

# 明确表示"没找到"。它同样匹配 `_FLAG_LINE`，所以必须**先**判掉 ——
# 否则 `FLAG: NOT_FOUND` 会被当成一个值为 "NOT_FOUND" 的 flag 存下来。
_FLAG_ABSENT = re.compile(
    r"^[ \t]*(?:FLAG|Flag)\s*[:：][ \t]*(?:NOT_FOUND|NONE|N/?A|未找到|无)[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)

# ── 来源 2：花括号串 ───────────────────────────────────────
#
# 形状：一段字母开头的标识符紧跟 `{...}`。覆盖 `DASCTF{...}`、`flag{...}`、
# `HTB{...}`、`CTF{...}` 这些常见写法。
#
# ⚠️ 前面那条 lookbehind 不是装饰：JSON 里换行是转义过的 `\n`，
# 于是 `...\nDASCTF{...}` 的原文里 `D` 前面是一个字母 `n`。
# 没有 lookbehind 的话，"从 D 开始"和"从 n 开始"会各产生一个候选 ——
# 而后者是 `nDASCTF{...}` 这种一眼假的形状，却会让"是否唯一"的判定失败。
_FLAG_TOKEN = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]{1,19}\{[^{}\n]{3,200}\}")

# 报告的固定文件名（strix 产出）。
_REPORT_NAME = "penetration_test_report.md"

# ── 占位符形状的"答案" ─────────────────────────────────────
#
# ★ 这一条是**回填时真实撞到**的，不是设想出来的：
#
#   一条失败的扫描，报告里留着模板占位符 `flag{...}`，
#   于是它被当成"拿到了一个 flag{...}"记进了库。
#
#   那正是这个字段最不能出的错 —— 用户会**照抄它去提交**。
#   而它看起来还挺像回事（前缀是 `flag`，形状也对）。
#
# 判据只收"正文里只有省略号 / 星号 / 下划线 / 问号 / 连字符"这一种。
#
# ⚠️ 刻意**不做**"单词黑名单"（`xxx` / `test` / `example`）：
#   黑名单一定会漏，而漏掉的代价是假 flag；同时它还会误伤形状古怪的
#   真题 flag —— 而误伤的代价是**真 flag 被丢掉**。
#   标点这条没有这两种风险：真正的 flag 正文里不可能只有标点。
_PLACEHOLDER_BODY = re.compile(r"^[\s.。*＊_?？\-—]+$")


def _is_placeholder(value: str) -> bool:
    """这个值是不是一个占位符（`flag{...}` 这种），而不是真的 flag。"""
    start = value.find("{")
    if start < 0 or not value.endswith("}"):
        return False
    return bool(_PLACEHOLDER_BODY.match(value[start + 1 : -1]))


# 兜底来源里**刻意不包括** `run.json`：它和 agents.json 都是 strix 的内部
# 快照，里面同一句话会被转义、被重复存好几份，是假候选的主要产地。
# 报告与 notes 是人读的文本，形状干净得多。
_FALLBACK_NAMES = (".state/notes.json", "coverage.json")


def _read_text(path: Path) -> str:
    """读一个文本文件。读不到就当空串 —— 缺文件是正常情况，不是错误。"""
    return read_text(path)


def _run_output_dirs(run_dir: Path) -> list[Path]:
    """`strix_runs/` 下的各次运行目录，**最新的在前**。"""
    return run_output_dirs(run_dir)


def _from_marker(text: str) -> str | None:
    """从标记行里取 flag。没有标记行、或标记为"没找到"时返回 `None`。"""
    if not text:
        return None
    # ⚠️ 顺序：先判"明确没找到"。反过来的话 `FLAG: NOT_FOUND`
    # 会被当成一个值为 "NOT_FOUND" 的 flag 存下来 —— 而它在界面上
    # 长得就像"拿到了一个叫 NOT_FOUND 的 flag"。
    if _FLAG_ABSENT.search(text):
        return None
    found = _FLAG_LINE.search(text)
    if not found:
        return None
    # 标记行里也可能是占位符（`FLAG: flag{...}`）—— 同样不能收
    return None if _is_placeholder(found.group(1)) else found.group(1)


def _from_tokens(text: str) -> str | None:
    """从花括号串里取 flag。**不唯一就返回 `None`**（见模块 docstring）。"""
    if not text:
        return None
    candidates = {
        match.group(0)
        for match in _FLAG_TOKEN.finditer(text)
        # 占位符先剔掉：留下它会让"是否唯一"的判定一起失效 ——
        # 一份真 flag + 一个 `flag{...}` 占位符会被判成"有歧义"，
        # 于是真 flag 也一起丢了。
        if not _is_placeholder(match.group(0))
    }
    if len(candidates) == 1:
        return candidates.pop()
    if len(candidates) > 1:
        # 记一条日志而不是静默放弃：这是"本该有 flag 但没能确认"的信号，
        # 排查时能看到"抽到了哪几个候选"，比只看到空白有用得多。
        logger.info("报告里有 %d 个不同的花括号串，无法确认哪个是 flag，留空", len(candidates))
    return None


def extract_flag(run_dir: str | Path | None) -> str | None:
    """从一次扫描的产物里认出 flag。认不出来返回 `None`。

    搜索顺序（**先可靠、后兜底**）：

    1. 报告里的标记行；
    2. 报告里的花括号串（唯一时）；
    3. notes / coverage 里的标记行，再是花括号串（唯一时）。

    报告排在最前，因为它是 agent 写给**人**看的最终结论 ——
    形状最干净，也最不容易出现"顺手提到"的噪音。
    """
    if not run_dir:
        return None
    base = Path(run_dir)
    if not base.is_dir():
        return None

    dirs = _run_output_dirs(base)
    if not dirs:
        return None

    reports = [_read_text(directory / _REPORT_NAME) for directory in dirs]
    fallbacks = [
        _read_text(directory / name) for directory in dirs for name in _FALLBACK_NAMES
    ]

    # ① 报告里的标记行
    for text in reports:
        found = _from_marker(text)
        if found:
            return found

    # ② 报告里的花括号串
    for text in reports:
        found = _from_tokens(text)
        if found:
            return found

    # ③ 兜底文件
    for text in fallbacks:
        found = _from_marker(text)
        if found:
            return found
    for text in fallbacks:
        found = _from_tokens(text)
        if found:
            return found

    return None


__all__ = ["extract_flag"]
