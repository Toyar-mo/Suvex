"""把一次 CTF 扫描的产物整理成一份**题解（Writeup）**（任务 19）。

## 为什么要我们自己产，而不是让 agent 直接输出题解

这不是取舍，是**做不到** —— 三处都在 strix 里写死了：

  · `strix/report/writer.py` 里标题是硬编码的
    `f.write("# Security Penetration Test Report\\n\\n")`；
  · 分节标题（`# Executive Summary` / `# Methodology` / …）同样写死；
  · `finish_scan` 只有那四个固定字段，且 docstring 明确写着
    "for non-technical leadership. **Risk**…" —— 整套是**渗透口径**。

全库搜 `report_template` / `writeup` / `ctf` 零命中，没有任何格式开关。
所以"让 agent 直接产出一份 WP"这条路不存在，只能我们自己重排。

## 做法：**内容照用，版式重排**

agent 写的那四节内容本身是有价值的（实测那次逆向题，`Technical Analysis`
里是完整的算法还原：自定义字母表、XOR 常量、逆向求解过程）。
所以我们做两件事：

  1. 改指令，让它在夺旗任务下按**题解口径**写这四节
     （见 `runner.GOAL_CLAUSE` 里 `captureFlag` 那条）；
  2. 在这里把四节**重排成题解的版式**，并补上 flag、题目信息、我的题面。

## 数据来源：磁盘上那份报告

四节内容在 `penetration_test_report.md` 里是**已经渲染好的 markdown**，
按 `# ` 一级标题切一刀就能拿到 —— 比去解析
`activity_events.detail.command` 里那段 CLI 文本稳得多
（那段 3017 字符、形状随 strix 版本变，而文件名与标题是它代码里的常量）。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.vocab import ScanGoal, TargetKind
from app.scans.artifacts import latest_report

# strix 报告里的一级标题。
#
# ⚠️ 这条正则**不能**直接拿去扫全文 —— 必须逐行喂，且要跳过代码围栏内的行。
# 见 `_iter_headings`。
_H1 = re.compile(r"^#\s+(.+?)\s*$")

# 代码围栏的起止行：最多 3 个前导空格，然后 ``` 或 ~~~（3 个以上）。
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

# ★ 按**标题名**认节，不按位置。
#
# 位置会随 strix 版本变（它已经动过一次标题层级），而名字是它写死在
# `report/state.py` 里的常量。名字变了我们就少一节，但**不会张冠李戴** ——
# 把"方法论"当成"结论"排在错误的小节下，比少一节坏得多。
_HEADING_TO_KEY = {
    "executive summary": "executive_summary",
    "methodology": "methodology",
    "technical analysis": "technical_analysis",
    "recommendations": "recommendations",
}

# 名字全认不出来时的**位置兜底**（报告的第一个一级节恒为结论）。
_POSITIONAL_ORDER = ("executive_summary", "methodology", "technical_analysis", "recommendations")

# 我们自己的题解小节标题 —— 与 strix 的渗透口径一一对应。
_SECTION_TITLES = {
    "executive_summary": "题目与结论",
    "methodology": "解题思路",
    "technical_analysis": "解题过程",
    "recommendations": "复盘与技巧",
}

# ⚠️ 与前端 `data/scan.ts` 的 `TARGET_KIND_LABEL` **逐字一致**。
# 两边不一致的话，同一个扫描在列表里叫一个名字、在题解里叫另一个。
_TARGET_KIND_LABEL = {
    TargetKind.URL.value: "已部署的应用",
    TargetKind.REPO.value: "Git 仓库",
    TargetKind.PATH.value: "本地代码路径",
    TargetKind.SERVICE.value: "TCP 服务",
    TargetKind.ARTIFACT.value: "题目附件",
}


def _iter_headings(report: str) -> list[tuple[int, int, str]]:
    """找出一级标题，返回 `(标题行起始下标, 标题行结束下标, 标题文字)`。

    ## ★ 为什么必须逐行扫、并跟踪代码围栏（2026-10-10 实测事故）

    第一版是 `re.finditer(r"^#\\s+(.+?)\\s*$", report, re.MULTILINE)` ——
    看着完全正确，因为 strix 那四个真标题确实是 `# ` 开头的一行。
    **但代码块里的注释也长这样。**

    实测那次（`scan_01M4J5QGQHKC7YMXRCREZTBRC2`）报告 16371 字节：
    第 45 行是真标题 `# Technical Analysis`，而第 55 行是 agent 贴的
    bash 片段里的一句注释 `# 或`。于是：

      · "解题过程"被**拦腰截断**（后面 80 行全丢了）；
      · 更糟的是**留下一个未闭合的 ``` 围栏** —— 它之后的所有内容
        （含"复盘与技巧"整节）在 markdown 里全变成代码块，
        界面上是一大坨灰色等宽字。

    两个症状都不报错，而内容全是真的 —— 用户只会以为"格式乱了"。

    ## 判据

    围栏只认**行首**（最多 3 个前导空格）的 ``` 或 ~~~，与 CommonMark 一致。
    进了围栏就只找闭合围栏，里面的 `#` 一律不当标题。
    用**同种**字符才算闭合（``` 不会被 ~~~ 关掉），这是 CommonMark 的规则，
    也避免了"报告里两种围栏混用"时把结构算错。
    """
    headings: list[tuple[int, int, str]] = []
    # 开启围栏的 (字符, 长度)。`None` = 当前不在围栏里。
    fence: tuple[str, int] | None = None
    offset = 0

    for line in report.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")

        if fence is None:
            match = _FENCE.match(stripped)
            if match:
                fence = (match.group(1)[0], len(match.group(1)))
            else:
                heading = _H1.match(stripped)
                if heading:
                    # 行尾可能带 \r\n，所以结束下标按**去掉换行后**的长度算，
                    # 这样 `body` 不会以换行开头（原来的实现也是这个效果）。
                    end = offset + len(stripped)
                    headings.append((offset, end, heading.group(1).strip()))
        else:
            match = _FENCE.match(stripped)
            # 闭合围栏：**同种**字符、且长度不短于开启的那个（CommonMark 规则）。
            # 不校验长度的话，开 ``` 而用 ````` 关（或反过来）会被算成没关，
            # 于是后面真正的标题全被当成代码块内容 —— 与要修的 bug 同形。
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= fence[1]:
                fence = None

        offset += len(line)

    return headings


def split_sections(report: str) -> dict[str, str]:
    """把 strix 的报告按一级标题切成小节。认不出来就少一节，不硬凑。"""
    if not report:
        return {}

    matches = _iter_headings(report)
    if not matches:
        return {}

    bodies: list[str] = []
    named: dict[str, str] = {}
    for index, (_, start, title) in enumerate(matches):
        end = matches[index + 1][0] if index + 1 < len(matches) else len(report)
        body = report[start:end].strip()
        bodies.append(body)
        key = _HEADING_TO_KEY.get(title.lower())
        if key and key not in named:
            named[key] = body

    if named:
        return named

    # ★ 位置兜底：只在**一个名字都没认出来**时启用。
    #
    # 报告的第一个一级标题是它自己那份文档的题名（`# Security Penetration
    # Test Report`），所以正文四节从**第二个**开始。
    # 这条只在 strix 把标题全改了的时候走到 —— 那时按位置排至少还能给出
    # 一份内容正确的题解，只是小节名未必贴切；而空白则什么都给不了。
    return {
        key: bodies[index + 1]
        for index, key in enumerate(_POSITIONAL_ORDER)
        if index + 1 < len(bodies) and bodies[index + 1]
    }


# 正文里任意级别的标题（`#` 到 `######`）。
_ANY_HEADING = re.compile(r"^(#{1,6})(\s+.*)?$")


def _demote_headings(body: str) -> str:
    """把 agent 正文里的标题**整体降一级**，让它们落在我们的小节之下。

    ## 为什么必须降（2026-10-10 实测）

    实测那份题解里同时出现了 `## 解题过程`（我们生成的）与
    `## 完整解题过程`（agent 在报告正文里自己写的）—— **同级并列**。
    于是 markdown 的目录里是：

        ## 解题过程
        ## 完整解题过程
        ## 解题思路
        ## 解题思路（怎么想到的、试过什么、如何排除）

    读者会以为这是四个并列的章节，而真相是后一个在讲前一个的内容。
    "解题过程"这个词重复两遍还会让人怀疑是不是渲染错了。

    降一级之后层级就正确了：我们的小节是父，agent 的小节是它的内容。

    ## 为什么不动正文里的其他东西

    只改**标题行的井号数量**，一个字都不动正文。降级失败（比如正文里
    已经用到 `######`）时保持原样 —— 六号已是 markdown 的上限，
    再加就渲染不出来了，而"层级差一级"远好过"标题直接变成普通段落"。

    ⚠️ 同样要跳过代码围栏：bash/python 注释里的 `#` 不是标题
    （`_iter_headings` 那次事故的同一个坑，这里不能再踩一遍）。
    """
    out: list[str] = []
    fence: tuple[str, int] | None = None

    for line in body.splitlines():
        if fence is None:
            opening = _FENCE.match(line)
            if opening:
                fence = (opening.group(1)[0], len(opening.group(1)))
                out.append(line)
                continue
            heading = _ANY_HEADING.match(line)
            if heading and len(heading.group(1)) < 6:
                out.append("#" + line)
                continue
        else:
            closing = _FENCE.match(line)
            if closing and closing.group(1)[0] == fence[0] and len(closing.group(1)) >= fence[1]:
                fence = None
        out.append(line)

    return "\n".join(out)


def _duration(seconds: int | None) -> str:
    """秒数 → 人读的时长。`None` 表示还没结束。"""
    if not seconds or seconds <= 0:
        return "—"
    minutes, rest = divmod(int(seconds), 60)
    if minutes and rest:
        return f"{minutes} 分 {rest} 秒"
    if minutes:
        return f"{minutes} 分"
    return f"{rest} 秒"


def filename_for(scan: Any) -> str:
    """下载用的文件名。带上题目名，一次下好几份时才分得清。

    ⚠️ 字符类里写 `\\s` 是**错的**（我第一版就这么写的）：在字符类中
    `\\` 是转义后的反斜杠、紧跟的 `s` 只是普通字母，于是这条正则变成
    "反斜杠或 s 或 …" —— 结果是空格没被替换、而题目名里的字母 `s`
    被吃掉（`scan` → `can`）。两个症状都很隐蔽，是测试抓出来的。
    """
    safe = re.sub(r'[\\/:*?"<>|\s]+', "-", (scan.name or "").strip())
    safe = safe.strip("-")[:60] or "scan"
    return f"CTF-Writeup-{safe}.md"


def build_writeup(scan: Any, *, report: str | None = None, now: datetime | None = None) -> str:
    """拼出题解的 markdown 全文。

    `report` 不传时自己去磁盘上读（`artifacts.latest_report`）。
    显式传入是为了让这个函数能被独立验证 —— 不必为了测排版造一个目录树。
    """
    if report is None:
        report = latest_report(getattr(scan, "run_dir", None))

    sections = split_sections(report)
    lines: list[str] = []

    lines.append(f"# CTF Writeup · {scan.name}")
    lines.append("")
    lines.append("| 项目 | 内容 |")
    lines.append("| --- | --- |")
    lines.append(f"| 目标 | `{scan.target}` |")
    lines.append(f"| 形态 | {_TARGET_KIND_LABEL.get(scan.target_kind, scan.target_kind)} |")
    lines.append(f"| 用时 | {_duration(getattr(scan, 'duration_sec', None))} |")
    if getattr(scan, "agent_count", None):
        lines.append(f"| 智能体 | {scan.agent_count} 个 |")
    started = getattr(scan, "started_at", None)
    if started is not None:
        lines.append(f"| 开始于 | {started.strftime('%Y-%m-%d %H:%M UTC')} |")
    lines.append("")

    # ── 结果：flag 放最前 ─────────────────────────────────
    #
    # ★ 版式上这是刻意的：题解里唯一"一眼要看"的就是它。
    #   放进围栏代码块 —— 用户要整段复制去平台提交，代码块一点即可全选，
    #   而夹在句子中间的那串字符得用鼠标小心地划。
    lines.append("## 结果")
    lines.append("")
    flag = getattr(scan, "flag", None)
    if flag:
        lines.append("```")
        lines.append(flag)
        lines.append("```")
    else:
        # ⚠️ 措辞留余地：没拿到、和"拿到了但我们没能确认"是两回事
        #   （见 `scans/flag.py`：报告里的形状不唯一时我们拒绝猜）。
        lines.append("本次没有确认到 flag。")
    lines.append("")

    # ── 我提供的题面 ──────────────────────────────────────
    #
    # 单独一节、用引用块：它是**用户的原话**，与 agent 的结论要能分开看。
    # 题解发到比赛平台上时，这一段往往正是别人最需要的"题面"。
    brief = (getattr(scan, "brief", None) or "").strip()
    if brief:
        lines.append("## 题目背景")
        lines.append("")
        lines.extend(f"> {row}" if row.strip() else ">" for row in brief.splitlines())
        lines.append("")

    for key in _POSITIONAL_ORDER:
        body = sections.get(key)
        if not body:
            continue
        lines.append(f"## {_SECTION_TITLES[key]}")
        lines.append("")
        # ★ 降一级再拼：agent 正文里的 `## xxx` 与我们的 `## 小节名` 原本同级，
        #   拼出来是并列的两个章节（实测出现过"解题过程 / 完整解题过程"）。
        #   理由见 `_demote_headings`。
        lines.append(_demote_headings(body))
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        f"*由 Suvex 生成 · 扫描 `{scan.id}` · 判据：「CTF 夺旗」*"
        if scan.goal == ScanGoal.CAPTURE_FLAG.value
        else f"*由 Suvex 生成 · 扫描 `{scan.id}`*"
    )
    lines.append("")

    return "\n".join(lines)


def writeup_path(scan: Any) -> Path | None:
    """题解落盘的位置（与扫描产物同级）。

    保留给"把题解也存一份到磁盘"的用法；当前端点是在线拼的，
    所以这个函数暂时只有测试与排查在用。
    """
    run_dir = getattr(scan, "run_dir", None)
    return Path(run_dir) / "writeup.md" if run_dir else None


__all__ = ["build_writeup", "filename_for", "split_sections", "writeup_path"]
