/**
 * 验证：预览与导出内容一致。
 *
 * 这是本轮验收标准里"预览效果和导出内容一致"的机器化检查。
 *
 * 做法不是"人眼看两遍"，而是利用架构上的一个事实：
 *   预览组件（ReportSheet）与 Markdown 渲染器（renderReportMarkdown）
 *   读的是**同一个 ReportDocument**。所以真正要验的是：
 *     IR 里的每一条内容，是否**都出现在了** Markdown 里。
 *   任何一条缺失，都说明两个消费者对同一份 IR 的解释不一致 —— 那才是 bug。
 *
 * 为什么能这样验：因为 ReportSheet 是"IR 的直接投影"，
 * 它不做任何筛选/措辞/排序（这些都发生在 buildReportDocument）。
 * 所以只要 Markdown 覆盖了 IR 的全部内容字段，预览就不可能比它多或少。
 *
 * 运行方式：`npm run verify:report`
 * 由 `scripts/run-report-parity.mjs` 借 Vite 的 ssrLoadModule 加载本文件，
 * 因此这里可以直接 `import '@/...'`（别名与浏览器里走同一套解析规则），
 * **不复制任何业务逻辑** —— 检查的就是真实源码本身。
 */
import { FIXTURE_FINDINGS, FIXTURE_SCANS } from './report-parity-fixtures.mts'
import { buildReportDocument } from '@/lib/report-document'
import { renderReportMarkdown } from '@/lib/report-markdown'
import type { ReportOptions } from '@/data/report'

const NOW = '2026-02-14T15:30:00.000Z'

let failures = 0
function check(label: string, condition: boolean, detail = '') {
  if (condition) {
    console.log(`  ✓ ${label}`)
  } else {
    failures++
    console.log(`  ✗ ${label}${detail ? `  ← ${detail}` : ''}`)
  }
}

/**
 * 多行文本的包含判定。
 *
 * ★ 为什么要逐行而不是整串 includes：
 *   Markdown 渲染器对正文类字段走的是 `inline()`，它把每个 `\n`
 *   换成「两个空格 + 换行」—— 那是 Markdown 的**硬换行**语法，
 *   用来保留原文的分行（否则 Markdown 会把换行折成一个空格）。
 *   因此导出里的文本与 IR 的原文**逐字符并不相等**，差着那些空格。
 *
 *   第一版脚本用整串 includes 比对，于是把「修复建议」「判断依据」
 *   全部报成缺失 —— 实际内容一条没少，是**比对方式错了**。
 *   这正是不能拿"测试红了"直接当"代码坏了"的原因：
 *   得先看清红的是被测对象还是测试本身。
 *
 *   逐行判定的语义也更贴近真正要验的事：
 *   **原文的每一行都出现在了导出里**。
 */
function containsText(haystack: string, text: string): boolean {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
    .every((line) => haystack.includes(line))
}

/** 用各种选项组合生成文档，逐一比对 */
const optionMatrix: { name: string; options: ReportOptions }[] = [
  {
    name: '默认（仅修复建议）',
    options: {
      includePoc: false,
      includeReasoning: false,
      onlyConfirmed: false,
      includeRemediation: true,
    },
  },
  {
    name: '全开（PoC + 推理 + 建议）',
    options: {
      includePoc: true,
      includeReasoning: true,
      onlyConfirmed: false,
      includeRemediation: true,
    },
  },
  {
    name: '只含已确认 + 全开',
    options: {
      includePoc: true,
      includeReasoning: true,
      onlyConfirmed: true,
      includeRemediation: true,
    },
  },
  {
    name: '全关（最干净的对外版本）',
    options: {
      includePoc: false,
      includeReasoning: false,
      onlyConfirmed: false,
      includeRemediation: false,
    },
  },
]

/** 三个范围各验一遍 */
const scopes = [
  { name: '某次扫描 scan_01J8X2', scope: { kind: 'scan' as const, scanId: 'scan_01J8X2' } },
  { name: '某个目标 acme-web', scope: { kind: 'target' as const, target: 'acme-web' } },
  {
    name: '时间区间（全部）',
    scope: { kind: 'range' as const, from: '2020-01-01', to: '2030-01-01' },
  },
]

for (const { name: scopeName, scope } of scopes) {
  for (const { name: optName, options } of optionMatrix) {
    const doc = buildReportDocument({
      allFindings: FIXTURE_FINDINGS,
      scans: FIXTURE_SCANS,
      scope,
      options,
      title: '一致性验证报告',
      now: NOW,
    })
    const md = renderReportMarkdown(doc)

    console.log(`\n── ${scopeName} · ${optName}（${doc.entries.length} 条）──`)

    // ══ 1. 封面与元信息 ══
    check('报告编号出现在导出中', md.includes(doc.meta.code), doc.meta.code)
    check('报告标题出现在导出中', md.includes(doc.meta.title))
    check('覆盖范围出现在导出中', md.includes(doc.meta.scope))

    // ══ 2. 摘要 ══
    check('风险结论出现在导出中', md.includes(doc.verdict))

    // ══ 3. 逐条比对（核心）══
    let entryFails = 0
    for (const entry of doc.entries) {
      const missing: string[] = []

      // 每条必须出现的字段 —— 与 ReportSheet 渲染的字段一一对应。
      // 一律走 containsText：正文类字段在导出里带 Markdown 硬换行，整串比对必假红。
      if (!containsText(md, entry.title)) missing.push('标题')
      if (!containsText(md, entry.severityLabel)) missing.push('严重度')
      if (!containsText(md, entry.categoryLabel)) missing.push('类型')
      if (!containsText(md, entry.target)) missing.push('目标')
      if (!containsText(md, entry.verificationLabel)) missing.push('验证程度')
      if (!containsText(md, entry.statusLabel)) missing.push('处置状态')
      if (!containsText(md, entry.discoveredAt)) missing.push('发现时间')
      if (!containsText(md, entry.description)) missing.push('描述')
      for (const location of entry.affected) {
        if (!containsText(md, location)) missing.push(`受影响位置 ${location}`)
      }
      // 证据原文必须完整出现（渲染时会 trimEnd，所以比 trim 后的内容）
      if (!containsText(md, entry.evidence.trimEnd())) missing.push('证据原文')

      // 可选字段：**出现与否必须与选项一致**，多和少都是 bug
      if (entry.reasoning !== null && !containsText(md, entry.reasoning)) missing.push('判断依据')
      if (entry.poc !== null && !containsText(md, entry.poc.code.trimEnd()))
        missing.push('PoC 代码')
      if (entry.remediation !== null && !containsText(md, entry.remediation))
        missing.push('修复建议')
      for (const d of entry.dispositions) {
        if (!containsText(md, d.summary)) missing.push(`处置记录 ${d.summary}`)
      }

      if (missing.length > 0) {
        entryFails++
        console.log(`    ✗ 条目 ${entry.index}「${entry.title}」缺失：${missing.join('、')}`)
      }
    }
    check(
      `${doc.entries.length} 条漏洞的全部字段都出现在导出中`,
      entryFails === 0,
      `${entryFails} 条有缺失`,
    )

    // ══ 4. 反向检查：不该出现的内容必须不出现 ══
    const anyPoc = FIXTURE_FINDINGS.filter((f) => doc.entries.some((e) => e.findingId === f.id)).some(
      (f) => f.detail.poc !== null,
    )

    if (!options.includePoc && anyPoc) {
      // 取一条范围内确实有 PoC 的，确认它的代码没被导出。
      // 同样用 containsText —— 用整串比会在有换行的脚本上假绿或假红。
      const withPoc = FIXTURE_FINDINGS.find(
        (f) => f.detail.poc !== null && doc.entries.some((e) => e.findingId === f.id),
      )
      const leaked = withPoc ? containsText(md, withPoc.detail.poc!.code.trimEnd()) : false
      check('未勾选 PoC 时，脚本代码未泄露到导出中', !leaked)
    }

    if (!options.includeReasoning) {
      const withReasoning = FIXTURE_FINDINGS.find(
        (f) => f.detail.reasoning.length > 20 && doc.entries.some((e) => e.findingId === f.id),
      )
      const leaked =
        withReasoning && md.includes(withReasoning.detail.reasoning.trim().slice(0, 40))
      check('未勾选判断依据时，推理内容未泄露到导出中', !leaked)
    }

    // ══ 5. 附录 ══
    check('附录 A 检测范围与方法', md.includes('附录 A'))
    check('附录 B 严重程度说明', md.includes('附录 B'))
    check('附录 C 内容范围', md.includes('附录 C'))

    // 内容范围要如实反映选项
    check(
      '附录 C 的选项声明与实际一致',
      md.includes(`验证脚本（PoC）：${options.includePoc ? '包含' : '未包含'}`) &&
        md.includes(`判断依据（智能体推理过程）：${options.includeReasoning ? '包含' : '未包含'}`),
    )

    // ══ 6. 排除声明 ══
    if (doc.meta.excludedCount > 0) {
      check(
        '排除条数写在报告里',
        md.includes(`${doc.meta.excludedCount} 条`),
        `${doc.meta.excludedCount}`,
      )
    }
  }
}

// ═════════════════════════════════════════════
// 边界情形
// ═════════════════════════════════════════════
console.log('\n══ 边界情形 ══')

// 空范围：不能崩，且必须给出"未发现问题"而不是空白
const emptyDoc = buildReportDocument({
  allFindings: FIXTURE_FINDINGS,
  scans: FIXTURE_SCANS,
  scope: { kind: 'scan', scanId: 'scan_不存在' },
  options: {
    includePoc: true,
    includeReasoning: true,
    onlyConfirmed: false,
    includeRemediation: true,
  },
  title: '空报告',
  now: NOW,
})
const emptyMd = renderReportMarkdown(emptyDoc)
check('空范围不崩溃', emptyMd.length > 0)
check('空范围给出明确结论', emptyMd.includes('未发现'), emptyDoc.verdict)
check('空范围仍带附录', emptyMd.includes('附录 A'))

// Markdown 围栏：证据里含 ``` 时必须用更长的围栏，否则表格/正文会被吞
const trickyDoc = buildReportDocument({
  allFindings: FIXTURE_FINDINGS,
  scans: FIXTURE_SCANS,
  scope: { kind: 'scan', scanId: 'scan_01J8X2' },
  options: {
    includePoc: true,
    includeReasoning: true,
    onlyConfirmed: false,
    includeRemediation: true,
  },
  title: '围栏测试',
  now: NOW,
})
const trickyMd = renderReportMarkdown(trickyDoc)
// 统计 ``` 的出现次数：必须是偶数（成对），否则说明围栏错乱
const fenceCount = (trickyMd.match(/^```/gm) || []).length
check('代码围栏成对闭合', fenceCount % 2 === 0, `出现 ${fenceCount} 次`)

/*
  表格竖线转义。

  ★ 这一项原来写的是
      check('表格单元转义了竖线', !/\| [^|]*\|[^|]*\|/.test('') || true)
    —— `|| true` 让它**永远为真**。那比没有检查更糟：它会在日志里
    给出一个绿色的 `✓`，而真正的回归（表格因为目标名里的竖线错列）
    不会有任何人发现。

  现在用一个含 `|` 的**目标名**真跑一遍。目标名会进封面的
  「覆盖范围」表格单元（`renderHeader` 里的 `cell(doc.meta.scope)`），
  所以渲染器必须把它转义成 `\|` —— 否则那一行会多出一列，整张表错位。

  ⚠️ 别拿**标题**来测这个：标题渲染成 `### N. …`（不是表格单元），
     里面的竖线无害，用它会得到一个永远为假的检查。
*/
const PIPE_TARGET = 'a|b.test'
const pipeDoc = buildReportDocument({
  allFindings: [
    { ...FIXTURE_FINDINGS[2], id: 'fnd_pipe', title: '竖线转义测试', target: PIPE_TARGET },
  ],
  scans: FIXTURE_SCANS,
  scope: { kind: 'target', target: PIPE_TARGET },
  options: {
    includePoc: false,
    includeReasoning: false,
    onlyConfirmed: false,
    includeRemediation: true,
  },
  title: '转义测试',
  now: NOW,
})
const pipeMd = renderReportMarkdown(pipeDoc)
const escapedScope = `目标 ${PIPE_TARGET.replace('|', '\\|')}`
check(
  '表格单元里的竖线被转义',
  pipeDoc.entries.length === 1 && pipeMd.includes(escapedScope),
  `entries=${pipeDoc.entries.length}，导出里没有出现 ${escapedScope}`,
)

console.log(
  failures === 0 ? '\n══ 全部通过：预览与导出内容一致 ══' : `\n══ ${failures} 项未通过 ══`,
)
process.exit(failures === 0 ? 0 : 1)
