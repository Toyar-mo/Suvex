"""报告文档 IR → Markdown。**前端旧实现 `lib/report-markdown.ts` 已移除，这里是唯一实现。**

## 为什么是复刻

当时的约定写得很直接（该文件已随死代码清理删除，理由仍然成立）：

> 这个渲染器与预览组件是**同一个 IR 的两个消费者**，不是两份内容。
> 它只做「把字段排版成 Markdown」这一件事，不重新筛选、不重新措辞、
> 不重新排序。任何内容的增删都必须改 `report-document.ts` —— 那样预览会同步变。
> 反过来说：如果哪天发现"Markdown 里少了一条"，bug 一定在 IR 构建，不在这里。

后端出 Markdown 时适用同一条：**它只排版，不判断**。
两边的 Markdown 一旦不同，就是"预览与导出不一致"——
而那是任务 6 的验收标准之一。

## 文风约定（这是给非技术管理层看的东西）

* 不打感叹号、不用营销词、不做加粗强调轰炸。加粗只给「结论」和「要素名」。
* 结论先行：摘要 → 总览 → 详情 → 附录，读者可以在任何一层停下。
* 数字与原文（证据、PoC、接口路径）保持原样，不软化不改写。
"""

from __future__ import annotations

from typing import Any

from app.reports import l10n
from app.reports.document import coverage_note


def render_markdown(doc: dict[str, Any], locale: str = "zh-CN") -> str:
    """主渲染。对应 `renderReportMarkdown`。

    五段之间用 `\\n\\n---\\n\\n` 分隔，尾部只留一个换行 ——
    有些 Markdown 渲染器会把尾随空行当真。
    """
    parts = [
        _header(doc, locale),
        _summary(doc, locale),
        _overview(doc, locale),
        _entries(doc, locale),
        _appendix(doc, locale),
    ]
    return ("\n\n---\n\n".join(parts)).rstrip() + "\n"


# ══════════════════════════════════════════════════════════
# 转义工具 —— 两个防"静默损坏"的细节
# ══════════════════════════════════════════════════════════


def _cell(value: str) -> str:
    """表格单元里的 `|` 必须转义，否则整行列会错位。

    这不是理论问题：目标名、接口路径里出现竖线完全可能
    （`GET /a|b`、正则片段），而一个错位的表格在 Markdown 里
    **不会报错**，只会静默地少一列 —— 读者看到的是"这个报告排版坏了"。
    """
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def _inline(value: str) -> str:
    """标题、段落里的换行会被 Markdown 折成一个空格，强制断行需要两个空格。"""
    return str(value or "").replace("\n", "  \n")


def _fenced(code: str, language: str) -> str:
    """代码块。

    ⚠️ 围栏在**内容含三反引号时换成四个** —— 证据里出现 ``` 的概率不低
    （HTTP 响应里嵌着 JSON 示例、PoC 里带着另一个代码块），
    三反引号会让围栏提前闭合，**后面的内容全变成正文**。
    这是 Markdown 生成器最典型的一个静默损坏点。
    """
    text = str(code or "")
    fence = "````" if "```" in text else "```"
    return f"{fence}{language}\n{text.rstrip()}\n{fence}"


# ══════════════════════════════════════════════════════════
# 封面
# ══════════════════════════════════════════════════════════


def _header(doc: dict[str, Any], locale: str = "zh-CN") -> str:
    meta = doc["meta"]
    # 草稿没有编号（生成时才分配）。正常链路下 Markdown 只在生成后产出，
    # 所以这里通常走不到；但真走到了也要给出"生成后分配"，
    # 而不是渲染成一对空的反引号。
    code_line = (
        l10n.t(locale, "md.codePending")
        if meta["provisional"]
        else f"`{meta['code']}`"
    )
    stats = doc["stats"]
    return f"""# {meta['title']}

|  |  |
| --- | --- |
| {l10n.t(locale, "md.reportCode")} | {code_line} |
| {l10n.t(locale, "md.scope")} | {_cell(meta['scope'])} |
| {l10n.t(locale, "md.generatedAt")} | {meta['generatedAt'][:10]} |
| {l10n.t(locale, "md.total")} | {l10n.t(locale, "md.countUnit", n=stats['total'])} |
| {l10n.t(locale, "md.targetCount")} | {l10n.t(locale, "md.targetUnit", n=len(doc['method']['targets']))} |"""


# ══════════════════════════════════════════════════════════
# 摘要
# ══════════════════════════════════════════════════════════


def _summary(doc: dict[str, Any], locale: str = "zh-CN") -> str:
    stats = doc["stats"]
    meta = doc["meta"]
    lines = [l10n.t(locale, "md.summary"), "", doc["verdict"]]

    # 处置建议只在高危存在时说 —— 没有高危时喊"立即修复"是狼来了
    if stats["urgent"] > 0:
        lines += [
            "",
            l10n.t(locale, "md.urgent", n=stats["urgent"]),
        ]

    if stats["verified"] > 0:
        lines += [
            "",
            l10n.t(locale, "md.verified", n=stats["verified"]),
        ]

    if meta["excludedCount"] > 0:
        lines += [
            "",
            l10n.t(locale, "md.excluded", n=meta["excludedCount"]),
        ]

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# 风险总览
# ══════════════════════════════════════════════════════════


def _overview(doc: dict[str, Any], locale: str = "zh-CN") -> str:
    stats = doc["stats"]
    lines = [l10n.t(locale, "md.overview"), ""]

    if stats["total"] == 0:
        lines.append(l10n.t(locale, "md.noFindings"))
        return "\n".join(lines)

    lines += [
        l10n.t(locale, "md.severityDist"),
        "",
        f"| {l10n.t(locale, 'md.severityCol')} | {l10n.t(locale, 'md.countCol')} | {l10n.t(locale, 'md.shareCol')} |",
        "| --- | --- | --- |",
    ]
    for row in doc["distribution"]:
        share = round((row["count"] / stats["total"]) * 100) if stats["total"] > 0 else 0
        lines.append(f"| {row['label']} | {row['count']} | {share}% |")
    lines.append(f"| **{l10n.t(locale, 'md.totalRow')}** | **{stats['total']}** | 100% |")

    lines += [
        "",
        l10n.t(locale, "md.handling"),
        "",
        f"| {l10n.t(locale, 'md.metricCol')} | {l10n.t(locale, 'md.countCol')} |",
        "| --- | --- |",
    ]
    lines.append(f"| {l10n.t(locale, 'md.verifiedCount')} | {stats['verified']} |")
    lines.append(f"| {l10n.t(locale, 'md.fixed')} | {stats['fixed']} |")
    lines.append(f"| {l10n.t(locale, 'md.pending')} | {stats['total'] - stats['fixed']} |")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# 漏洞详情
# ══════════════════════════════════════════════════════════


def _entries(doc: dict[str, Any], locale: str = "zh-CN") -> str:
    entries = doc["entries"]
    if not entries:
        return f"{l10n.t(locale, 'md.entries')}\n\n{l10n.t(locale, 'md.noEntries')}"
    blocks = [_entry(entry, locale) for entry in entries]
    return f"{l10n.t(locale, 'md.entries')}\n\n" + "\n\n".join(blocks)


def _entry(entry: dict[str, Any], locale: str = "zh-CN") -> str:
    """单条漏洞。

    小节顺序是刻意的，按读者**决策所需**排列，不按数据字段顺序：
      基本信息 → 问题描述 → 影响位置 → 判断依据 → 证据 → 复现 → 修复建议 → 处置记录

    修复建议排在复现之后：管理层读到这里时已经理解了问题，
    建议才有落点；反过来先给建议，读者不知道该修的是什么。
    """
    lines: list[str] = []
    lines.append(f"### {entry['index']}. {entry['title']}")
    lines.append("")
    lines.append(
        f"**{l10n.t(locale, 'md.severity')}:** {entry['severityLabel']} · "
        f"**{l10n.t(locale, 'md.category')}:** {entry['categoryLabel']} · "
        f"**{l10n.t(locale, 'md.target')}:** `{entry['target']}`"
    )
    lines.append("")
    lines.append(
        f"**{l10n.t(locale, 'md.verification')}:** {entry['verificationLabel']} · "
        f"**{l10n.t(locale, 'md.status')}:** {entry['statusLabel']} · "
        f"**{l10n.t(locale, 'md.discoveredAt')}:** {entry['discoveredAt']}"
    )

    lines += ["", l10n.t(locale, "md.description"), "", _inline(entry["description"])]

    if entry["affected"]:
        lines += ["", l10n.t(locale, "md.affected"), ""]
        lines += [f"- `{location}`" for location in entry["affected"]]

    # 推理过程是可选项 —— 关掉时**整个小节不出现**，而不是留个空标题
    if entry["reasoning"]:
        lines += ["", l10n.t(locale, "md.reasoning"), "", _inline(entry["reasoning"])]

    lines += ["", l10n.t(locale, "md.evidence"), "", _fenced(entry["evidence"], "http")]

    if entry["poc"]:
        lines += [
            "",
            l10n.t(locale, "md.poc"),
            "",
            _fenced(entry["poc"]["code"], entry["poc"]["language"]),
        ]

    if entry["remediation"]:
        lines += ["", l10n.t(locale, "md.remediation"), "", _inline(entry["remediation"])]

    if entry["dispositions"]:
        lines += ["", l10n.t(locale, "md.dispositions"), ""]
        lines += [f"- {item['at']} · {item['summary']}" for item in entry["dispositions"]]

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# 附录
# ══════════════════════════════════════════════════════════


def _appendix(doc: dict[str, Any], locale: str = "zh-CN") -> str:
    """附录三节。

    为什么报告一定要有附录：
      正文回答"发现了什么"，附录回答"这个报告可信到什么程度、该怎么读"。
    缺了它，读者只能全盘接受结论 —— 而安全报告的结论恰恰是有条件成立的。
    """
    return "\n\n".join(
        [_method(doc, locale), _severity_key(doc, locale), _content_scope(doc, locale)]
    )


def _method(doc: dict[str, Any], locale: str = "zh-CN") -> str:
    """附录 A：范围与方法 —— 交代覆盖边界。"""
    method = doc["method"]
    lines = [l10n.t(locale, "md.method"), ""]

    if method["targets"]:
        lines += [l10n.t(locale, "md.coverageTargets"), ""]
        lines += [f"- `{target}`" for target in method["targets"]]
        lines.append("")

    if method["span"]:
        lines += [
            l10n.t(
                locale,
                "md.span",
                **{"from": method["span"]["from"], "to": method["span"]["to"]},
            ),
            "",
        ]

    if method["skills"]:
        lines += [
            l10n.t(
                locale,
                "md.skills",
                skills=l10n.t(locale, "md.listSep").join(method["skills"]),
            ),
            "",
        ]

    if method["scans"]:
        lines += [l10n.t(locale, "md.scans"), ""]
        lines += [
            f"| {l10n.t(locale, 'md.scanName')} | {l10n.t(locale, 'md.target')} | "
            f"{l10n.t(locale, 'md.finishedAt')} | {l10n.t(locale, 'md.completeness')} |",
            "| --- | --- | --- | --- |",
        ]
        for scan in method["scans"]:
            finished = (
                scan["finishedAt"][:10]
                if scan["finishedAt"]
                else l10n.t(locale, "md.notFinished")
            )
            lines.append(
                f"| {_cell(scan['name'])} | `{_cell(scan['target'])}` | {finished} | "
                f"{coverage_note(scan['status'], locale)} |"
            )
        lines.append("")

    # 中断过就必须显式提醒 —— 读者不能以为整个目标都被测过了
    interrupted = [scan for scan in method["scans"] if scan["status"] == "aborted"]
    if interrupted:
        lines += [
            l10n.t(locale, "md.interrupted", n=len(interrupted)),
            "",
        ]

    # 边界说明：这句话让报告的效力范围变得诚实
    lines.append(
        "本报告覆盖上述目标在指定范围内的检测结果。检测以自动化的黑盒与灰盒方式为主，"
        "未覆盖的目标、未启用的检测能力以及业务逻辑层面的复杂组合场景不在此次范围内。"
    )
    return "\n".join(lines)


def _severity_key(doc: dict[str, Any], locale: str = "zh-CN") -> str:
    """附录 B：严重程度说明 —— 让"高危"对读者是可解释的。"""
    lines = [
        l10n.t(locale, "md.severityKey"),
        "",
        f"| {l10n.t(locale, 'md.severityCol')} | {l10n.t(locale, 'md.severityMeaning')} |",
        "| --- | --- |",
    ]
    for item in doc["severityDefinitions"]:
        lines.append(f"| {item['label']} | {_cell(item['definition'])} |")
    lines.append("")
    lines.append(
        "严重程度描述问题被利用的难易与影响面，不等同于业务优先级。"
        "实际处置顺序建议结合资产重要性与业务影响一并判断。"
    )
    return "\n".join(lines)


def _content_scope(doc: dict[str, Any], locale: str = "zh-CN") -> str:
    """附录 C：内容范围 —— 报告自称包含什么，必须写清楚。"""
    options = doc["meta"]["options"]
    lines = [l10n.t(locale, "md.contentScope"), "", l10n.t(locale, "md.contentIntro"), ""]
    included = l10n.t(locale, "md.included")
    excluded = l10n.t(locale, "md.excludedOption")
    lines.append(
        f"- {l10n.t(locale, 'md.includeRemediation')}: "
        f"{included if options['includeRemediation'] else excluded}"
    )
    lines.append(
        f"- {l10n.t(locale, 'md.includePoc')}: "
        f"{included if options['includePoc'] else excluded}"
    )
    lines.append(
        f"- {l10n.t(locale, 'md.includeReasoning')}: "
        f"{included if options['includeReasoning'] else excluded}"
    )
    lines.append(
        f"- {l10n.t(locale, 'md.filter')}: "
        f"{l10n.t(locale, 'md.onlyConfirmed') if options['onlyConfirmed'] else l10n.t(locale, 'md.allStatuses')}"
    )
    lines.append("")
    lines.append(
        "未包含的内容可在系统内的漏洞发现页面查看，或重新生成包含这些内容的报告。"
        "报告内容在生成时固定，不随漏洞处置状态的变化而改变。"
    )
    return "\n".join(lines)
