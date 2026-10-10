"""报告文档 IR → Markdown。**逐字复刻前端 `lib/report-markdown.ts`。**

## 为什么是复刻

前端注释写得很直接：

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

from app.reports.document import coverage_note


def render_markdown(doc: dict[str, Any]) -> str:
    """主渲染。对应 `renderReportMarkdown`。

    五段之间用 `\\n\\n---\\n\\n` 分隔，尾部只留一个换行 ——
    有些 Markdown 渲染器会把尾随空行当真。
    """
    parts = [
        _header(doc),
        _summary(doc),
        _overview(doc),
        _entries(doc),
        _appendix(doc),
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


def _header(doc: dict[str, Any]) -> str:
    meta = doc["meta"]
    # 草稿没有编号（生成时才分配）。正常链路下 Markdown 只在生成后产出，
    # 所以这里通常走不到；但真走到了也要给出"生成后分配"，
    # 而不是渲染成一对空的反引号。
    code_line = "生成后分配" if meta["provisional"] else f"`{meta['code']}`"
    stats = doc["stats"]
    return f"""# {meta['title']}

|  |  |
| --- | --- |
| 报告编号 | {code_line} |
| 覆盖范围 | {_cell(meta['scope'])} |
| 生成时间 | {meta['generatedAt'][:10]} |
| 发现总数 | {stats['total']} 条 |
| 涉及目标 | {len(doc['method']['targets'])} 个 |"""


# ══════════════════════════════════════════════════════════
# 摘要
# ══════════════════════════════════════════════════════════


def _summary(doc: dict[str, Any]) -> str:
    stats = doc["stats"]
    meta = doc["meta"]
    lines = ["## 一、摘要", "", doc["verdict"]]

    # 处置建议只在高危存在时说 —— 没有高危时喊"立即修复"是狼来了
    if stats["urgent"] > 0:
        lines += [
            "",
            f"其中 {stats['urgent']} 条为严重或高危，建议在下一轮迭代前完成处置。",
        ]

    if stats["verified"] > 0:
        lines += [
            "",
            f"本次共 {stats['verified']} 条通过可复现的验证脚本确认。"
            "报告中的证据与复现步骤均来自实际执行结果，未做推测性描述。",
        ]

    if meta["excludedCount"] > 0:
        lines += [
            "",
            f"本报告仅收录已确认的问题，另有 {meta['excludedCount']} 条"
            "待确认或已判定为误报的条目未纳入。",
        ]

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# 风险总览
# ══════════════════════════════════════════════════════════


def _overview(doc: dict[str, Any]) -> str:
    stats = doc["stats"]
    lines = ["## 二、风险总览", ""]

    if stats["total"] == 0:
        lines.append("本次范围内未发现需要报告的安全问题。")
        return "\n".join(lines)

    lines += ["### 严重程度分布", "", "| 严重程度 | 数量 | 占比 |", "| --- | --- | --- |"]
    for row in doc["distribution"]:
        share = round((row["count"] / stats["total"]) * 100) if stats["total"] > 0 else 0
        lines.append(f"| {row['label']} | {row['count']} | {share}% |")
    lines.append(f"| **合计** | **{stats['total']}** | 100% |")

    lines += ["", "### 处置情况", "", "| 指标 | 数量 |", "| --- | --- |"]
    lines.append(f"| 已通过验证脚本确认 | {stats['verified']} |")
    lines.append(f"| 已修复 | {stats['fixed']} |")
    lines.append(f"| 待处置 | {stats['total'] - stats['fixed']} |")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# 漏洞详情
# ══════════════════════════════════════════════════════════


def _entries(doc: dict[str, Any]) -> str:
    entries = doc["entries"]
    if not entries:
        return "## 三、漏洞详情\n\n无。"
    blocks = [_entry(entry) for entry in entries]
    return "## 三、漏洞详情\n\n" + "\n\n".join(blocks)


def _entry(entry: dict[str, Any]) -> str:
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
        f"**严重程度：**{entry['severityLabel']} · **类型：**{entry['categoryLabel']} · "
        f"**目标：**`{entry['target']}`"
    )
    lines.append("")
    lines.append(
        f"**验证程度：**{entry['verificationLabel']} · **处置状态：**{entry['statusLabel']} · "
        f"**发现时间：**{entry['discoveredAt']}"
    )

    lines += ["", "#### 问题描述", "", _inline(entry["description"])]

    if entry["affected"]:
        lines += ["", "#### 受影响位置", ""]
        lines += [f"- `{location}`" for location in entry["affected"]]

    # 推理过程是可选项 —— 关掉时**整个小节不出现**，而不是留个空标题
    if entry["reasoning"]:
        lines += ["", "#### 判断依据", "", _inline(entry["reasoning"])]

    lines += ["", "#### 证据", "", _fenced(entry["evidence"], "http")]

    if entry["poc"]:
        lines += ["", "#### 复现方式", "", _fenced(entry["poc"]["code"], entry["poc"]["language"])]

    if entry["remediation"]:
        lines += ["", "#### 修复建议", "", _inline(entry["remediation"])]

    if entry["dispositions"]:
        lines += ["", "#### 处置记录", ""]
        lines += [f"- {item['at']} · {item['summary']}" for item in entry["dispositions"]]

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# 附录
# ══════════════════════════════════════════════════════════


def _appendix(doc: dict[str, Any]) -> str:
    """附录三节。

    为什么报告一定要有附录：
      正文回答"发现了什么"，附录回答"这个报告可信到什么程度、该怎么读"。
    缺了它，读者只能全盘接受结论 —— 而安全报告的结论恰恰是有条件成立的。
    """
    return "\n\n".join([_method(doc), _severity_key(doc), _content_scope(doc)])


def _method(doc: dict[str, Any]) -> str:
    """附录 A：范围与方法 —— 交代覆盖边界。"""
    method = doc["method"]
    lines = ["## 附录 A · 检测范围与方法", ""]

    if method["targets"]:
        lines += ["**覆盖目标**", ""]
        lines += [f"- `{target}`" for target in method["targets"]]
        lines.append("")

    if method["span"]:
        lines += [
            f"**发现时间跨度**：{method['span']['from']} 至 {method['span']['to']}",
            "",
        ]

    if method["skills"]:
        lines += [f"**启用的检测能力**：{'、'.join(method['skills'])}", ""]

    if method["scans"]:
        lines += ["**关联的扫描任务**", ""]
        lines += ["| 扫描名称 | 目标 | 完成时间 | 覆盖完整性 |", "| --- | --- | --- | --- |"]
        for scan in method["scans"]:
            finished = scan["finishedAt"][:10] if scan["finishedAt"] else "未完成"
            lines.append(
                f"| {_cell(scan['name'])} | `{_cell(scan['target'])}` | {finished} | "
                f"{coverage_note(scan['status'])} |"
            )
        lines.append("")

    # 中断过就必须显式提醒 —— 读者不能以为整个目标都被测过了
    interrupted = [scan for scan in method["scans"] if scan["status"] == "aborted"]
    if interrupted:
        lines += [
            f"其中 {len(interrupted)} 次扫描被提前终止，对应目标的检测覆盖不完整，"
            "相关结论仅代表已执行范围内的结果。",
            "",
        ]

    # 边界说明：这句话让报告的效力范围变得诚实
    lines.append(
        "本报告覆盖上述目标在指定范围内的检测结果。检测以自动化的黑盒与灰盒方式为主，"
        "未覆盖的目标、未启用的检测能力以及业务逻辑层面的复杂组合场景不在此次范围内。"
    )
    return "\n".join(lines)


def _severity_key(doc: dict[str, Any]) -> str:
    """附录 B：严重程度说明 —— 让"高危"对读者是可解释的。"""
    lines = ["## 附录 B · 严重程度说明", "", "| 严重程度 | 判定含义 |", "| --- | --- |"]
    for item in doc["severityDefinitions"]:
        lines.append(f"| {item['label']} | {_cell(item['definition'])} |")
    lines.append("")
    lines.append(
        "严重程度描述问题被利用的难易与影响面，不等同于业务优先级。"
        "实际处置顺序建议结合资产重要性与业务影响一并判断。"
    )
    return "\n".join(lines)


def _content_scope(doc: dict[str, Any]) -> str:
    """附录 C：内容范围 —— 报告自称包含什么，必须写清楚。"""
    options = doc["meta"]["options"]
    lines = ["## 附录 C · 内容范围", "", "本报告包含以下内容：", ""]
    lines.append(f"- 修复建议：{'包含' if options['includeRemediation'] else '未包含'}")
    lines.append(f"- 验证脚本（PoC）：{'包含' if options['includePoc'] else '未包含'}")
    lines.append(
        f"- 判断依据（智能体推理过程）：{'包含' if options['includeReasoning'] else '未包含'}"
    )
    lines.append(f"- 条目筛选：{'仅已确认的问题' if options['onlyConfirmed'] else '全部状态的问题'}")
    lines.append("")
    lines.append(
        "未包含的内容可在系统内的漏洞发现页面查看，或重新生成包含这些内容的报告。"
        "报告内容在生成时固定，不随漏洞处置状态的变化而改变。"
    )
    return "\n".join(lines)
