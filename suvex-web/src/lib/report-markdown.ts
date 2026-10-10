import { coverageNote, type ReportDocument, type ReportEntry } from './report-document'

/**
 * 报告文档 → Markdown。
 *
 * ★ 这个渲染器与预览组件是**同一个 IR 的两个消费者**，不是两份内容。
 *   它只做「把字段排版成 Markdown」这一件事，不重新筛选、不重新措辞、
 *   不重新排序。任何内容的增删都必须改 report-document.ts —— 那样预览会同步变。
 *   反过来说：如果哪天发现"Markdown 里少了一条"，bug 一定在 IR 构建，不在这里。
 *
 * 纯函数、零依赖：可以被脚本直接 import 跑，用来逐条比对预览与导出的内容。
 *
 * 文风约定（这是给非技术管理层看的东西）：
 *   - 不打感叹号、不用营销词、不做加粗强调轰炸。加粗只给「结论」和「要素名」。
 *   - 结论先行：摘要 → 总览 → 详情 → 附录，读者可以在任何一层停下。
 *   - 数字与原文（证据、PoC、接口路径）保持原样，不软化不改写。
 */

// ─────────────────────────────────────────────
// 表格工具
// ─────────────────────────────────────────────

/**
 * 表格单元里的 `|` 必须转义，否则整行的列会错位。
 *
 * 这不是理论问题：目标名、接口路径里出现竖线完全可能
 * （`GET /a|b`、正则片段），而一个错位的表格在 Markdown 里
 * 不会报错，只会静默地少一列 —— 读者看到的是"这个报告排版坏了"。
 */
function cell(value: string): string {
  return value.replace(/\|/g, '\\|').replace(/\n+/g, ' ')
}

/** 标题、段落里的换行会被 Markdown 折成一空格，强制断行需要两个空格 */
function inline(value: string): string {
  return value.replace(/\n/g, '  \n')
}

// ─────────────────────────────────────────────
// 主渲染
// ─────────────────────────────────────────────

export function renderReportMarkdown(doc: ReportDocument): string {
  const parts: string[] = [
    renderHeader(doc),
    renderSummary(doc),
    renderOverview(doc),
    renderEntries(doc),
    renderAppendix(doc),
  ]
  // 尾部留一个换行，且不留多余空行 —— 有些 Markdown 渲染器会把尾随空行当真
  return `${parts.join('\n\n---\n\n').trimEnd()}\n`
}

// ─────────────────────────────────────────────
// 封面 / 标题
// ─────────────────────────────────────────────

function renderHeader(doc: ReportDocument): string {
  // 草稿没有编号（生成时才分配）。正常链路下 Markdown 只在生成后产出，
  // 所以这里通常走不到；但真走到了也要给出"生成后分配"，
  // 而不是渲染成一对空的 ` ` 反引号。
  const codeLine = doc.meta.provisional ? '生成后分配' : `\`${doc.meta.code}\``

  return `# ${doc.meta.title}

|  |  |
| --- | --- |
| 报告编号 | ${codeLine} |
| 覆盖范围 | ${cell(doc.meta.scope)} |
| 生成时间 | ${doc.meta.generatedAt.slice(0, 10)} |
| 发现总数 | ${doc.stats.total} 条 |
| 涉及目标 | ${doc.method.targets.length} 个 |`
}

// ─────────────────────────────────────────────
// 摘要
// ─────────────────────────────────────────────

function renderSummary(doc: ReportDocument): string {
  const lines: string[] = ['## 一、摘要', '', doc.verdict]

  // 处置建议只在高危存在时说 —— 没有高危时喊"立即修复"是狼来了
  if (doc.stats.urgent > 0) {
    lines.push('', `其中 ${doc.stats.urgent} 条为严重或高危，建议在下一轮迭代前完成处置。`)
  }

  if (doc.stats.verified > 0) {
    lines.push(
      '',
      `本次共 ${doc.stats.verified} 条通过可复现的验证脚本确认。报告中的证据与复现步骤均来自实际执行结果，未做推测性描述。`,
    )
  }

  if (doc.meta.excludedCount > 0) {
    lines.push(
      '',
      `本报告仅收录已确认的问题，另有 ${doc.meta.excludedCount} 条待确认或已判定为误报的条目未纳入。`,
    )
  }

  return lines.join('\n')
}

// ─────────────────────────────────────────────
// 风险总览
// ─────────────────────────────────────────────

function renderOverview(doc: ReportDocument): string {
  const lines: string[] = ['## 二、风险总览', '']

  if (doc.stats.total === 0) {
    lines.push('本次范围内未发现需要报告的安全问题。')
    return lines.join('\n')
  }

  lines.push('### 严重程度分布', '')
  lines.push('| 严重程度 | 数量 | 占比 |')
  lines.push('| --- | --- | --- |')
  for (const row of doc.distribution) {
    const share = doc.stats.total > 0 ? Math.round((row.count / doc.stats.total) * 100) : 0
    lines.push(`| ${row.label} | ${row.count} | ${share}% |`)
  }
  lines.push(`| **合计** | **${doc.stats.total}** | 100% |`)

  // 覆盖情况。放在分布之后，因为它回答的是"这些数字覆盖了多大面"
  lines.push('', '### 处置情况', '')
  lines.push('| 指标 | 数量 |')
  lines.push('| --- | --- |')
  lines.push(`| 已通过验证脚本确认 | ${doc.stats.verified} |`)
  lines.push(`| 已修复 | ${doc.stats.fixed} |`)
  lines.push(`| 待处置 | ${doc.stats.total - doc.stats.fixed} |`)

  return lines.join('\n')
}

// ─────────────────────────────────────────────
// 漏洞详情
// ─────────────────────────────────────────────

function renderEntries(doc: ReportDocument): string {
  if (doc.entries.length === 0) return '## 三、漏洞详情\n\n无。'

  const blocks = doc.entries.map(renderEntry)
  return `## 三、漏洞详情\n\n${blocks.join('\n\n')}`
}

/**
 * 单条漏洞。
 *
 * 小节顺序是刻意的，按读者**决策所需**排列，不按数据字段顺序：
 *   基本信息 → 问题描述 → 影响位置 → 判断依据 → 证据 → 复现 → 修复建议 → 处置记录
 *
 * 修复建议排在复现之后：管理层读到这里时已经理解了问题，
 * 建议才有落点；反过来先给建议，读者不知道该修的是什么。
 */
function renderEntry(entry: ReportEntry): string {
  const lines: string[] = []

  lines.push(`### ${entry.index}. ${entry.title}`)
  lines.push('')
  lines.push(
    `**严重程度：**${entry.severityLabel} · **类型：**${entry.categoryLabel} · **目标：**\`${entry.target}\``,
  )
  lines.push('')
  lines.push(
    `**验证程度：**${entry.verificationLabel} · **处置状态：**${entry.statusLabel} · **发现时间：**${entry.discoveredAt}`,
  )

  lines.push('', '#### 问题描述', '', inline(entry.description))

  if (entry.affected.length > 0) {
    lines.push('', '#### 受影响位置', '')
    for (const location of entry.affected) lines.push(`- \`${location}\``)
  }

  // 推理过程是可选项 —— 关掉时整个小节不出现，而不是留个空标题
  if (entry.reasoning) {
    lines.push('', '#### 判断依据', '', inline(entry.reasoning))
  }

  lines.push('', '#### 证据', '', fenced(entry.evidence, 'http'))

  if (entry.poc) {
    lines.push('', '#### 复现方式', '', fenced(entry.poc.code, entry.poc.language))
  }

  if (entry.remediation) {
    lines.push('', '#### 修复建议', '', inline(entry.remediation))
  }

  if (entry.dispositions.length > 0) {
    lines.push('', '#### 处置记录', '')
    for (const item of entry.dispositions) {
      lines.push(`- ${item.at} · ${item.summary}`)
    }
  }

  return lines.join('\n')
}

/**
 * 代码块。
 *
 * 围栏用 4 个反引号而不是 3 个：证据里出现 ``` 的概率不低
 * （HTTP 响应里嵌着 JSON 示例、PoC 里带着另一个代码块），
 * 三反引号会让围栏提前闭合，后面的内容全变成正文。
 * 这是 Markdown 生成器最典型的一个静默损坏点。
 */
function fenced(code: string, language: string): string {
  const fence = code.includes('```') ? '````' : '```'
  // 语言标识规整化：PoC 的 language 是 'python' / 'bash' 这类，
  // 证据块用 http —— 不认识的语言标识会让高亮器退化，不影响可读性
  return `${fence}${language}\n${code.trimEnd()}\n${fence}`
}

// ─────────────────────────────────────────────
// 附录
// ─────────────────────────────────────────────

/**
 * 附录三节。
 *
 * 为什么报告一定要有附录：
 *   正文回答"发现了什么"，附录回答"这个报告可信到什么程度、该怎么读"。
 *   缺了它，读者只能全盘接受结论 —— 而安全报告的结论恰恰是有条件成立的。
 */
function renderAppendix(doc: ReportDocument): string {
  return [renderMethod(doc), renderSeverityKey(doc), renderContentScope(doc)].join('\n\n')
}

/** 附录 A：范围与方法 —— 交代覆盖边界 */
function renderMethod(doc: ReportDocument): string {
  const m = doc.method
  const lines: string[] = ['## 附录 A · 检测范围与方法', '']

  if (m.targets.length > 0) {
    lines.push('**覆盖目标**', '')
    for (const target of m.targets) lines.push(`- \`${target}\``)
    lines.push('')
  }

  if (m.span) {
    lines.push(`**发现时间跨度**：${m.span.from} 至 ${m.span.to}`, '')
  }

  if (m.skills.length > 0) {
    lines.push(`**启用的检测能力**：${m.skills.join('、')}`, '')
  }

  if (m.scans.length > 0) {
    lines.push('**关联的扫描任务**', '')
    lines.push('| 扫描名称 | 目标 | 完成时间 | 覆盖完整性 |')
    lines.push('| --- | --- | --- | --- |')
    for (const scan of m.scans) {
      lines.push(
        `| ${cell(scan.name)} | \`${cell(scan.target)}\` | ${scan.finishedAt ? scan.finishedAt.slice(0, 10) : '未完成'} | ${coverageNote(scan.status)} |`,
      )
    }
    lines.push('')
  }

  // 中断过就必须显式提醒 —— 读者不能以为整个目标都被测过了
  const interrupted = m.scans.filter((s) => s.status === 'aborted')
  if (interrupted.length > 0) {
    lines.push(
      `其中 ${interrupted.length} 次扫描被提前终止，对应目标的检测覆盖不完整，相关结论仅代表已执行范围内的结果。`,
      '',
    )
  }

  // 边界说明：这句话让报告的效力范围变得诚实
  lines.push(
    '本报告覆盖上述目标在指定范围内的检测结果。检测以自动化的黑盒与灰盒方式为主，未覆盖的目标、未启用的检测能力以及业务逻辑层面的复杂组合场景不在此次范围内。',
  )

  return lines.join('\n')
}

/** 附录 B：严重程度说明 —— 让"高危"对读者是可解释的 */
function renderSeverityKey(doc: ReportDocument): string {
  const lines: string[] = ['## 附录 B · 严重程度说明', '']
  lines.push('| 严重程度 | 判定含义 |')
  lines.push('| --- | --- |')
  for (const item of doc.severityDefinitions) {
    lines.push(`| ${item.label} | ${cell(item.definition)} |`)
  }
  lines.push('')
  lines.push(
    '严重程度描述问题被利用的难易与影响面，不等同于业务优先级。实际处置顺序建议结合资产重要性与业务影响一并判断。',
  )
  return lines.join('\n')
}

/** 附录 C：内容范围 —— 报告自称包含什么，必须写清楚 */
function renderContentScope(doc: ReportDocument): string {
  const o = doc.meta.options
  const lines: string[] = ['## 附录 C · 内容范围', '', '本报告包含以下内容：', '']
  lines.push(`- 修复建议：${o.includeRemediation ? '包含' : '未包含'}`)
  lines.push(`- 验证脚本（PoC）：${o.includePoc ? '包含' : '未包含'}`)
  lines.push(`- 判断依据（智能体推理过程）：${o.includeReasoning ? '包含' : '未包含'}`)
  lines.push(`- 条目筛选：${o.onlyConfirmed ? '仅已确认的问题' : '全部状态的问题'}`)
  lines.push('')
  lines.push(
    '未包含的内容可在系统内的漏洞发现页面查看，或重新生成包含这些内容的报告。报告内容在生成时固定，不随漏洞处置状态的变化而改变。',
  )
  return lines.join('\n')
}
