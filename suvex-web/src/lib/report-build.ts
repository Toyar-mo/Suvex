import { countBySeverity } from './finding-query'
import { SEVERITY_ORDER, type Severity } from '@/data/findings'
import type { Finding } from '@/data/finding'
import type { ReportOptions, ReportScope } from '@/data/report'

/**
 * 报告的纯逻辑 —— 范围筛选、内容裁剪、统计。
 * 全部是纯函数，便于独立验证（不依赖 store，不依赖 React）。
 */

// ─────────────────────────────────────────────
// 范围
// ─────────────────────────────────────────────

/**
 * 按范围挑出发现。
 *
 * 时间区间用**闭区间** [from, to]：用户说"2月1日到2月14日"时，
 * 心里想的是包含这两天的。开区间会让"我明明选了今天"发现少一条。
 */
export function findingsInScope(findings: Finding[], scope: ReportScope): Finding[] {
  switch (scope.kind) {
    case 'scan':
      return findings.filter((f) => f.scanId === scope.scanId)
    case 'target':
      return findings.filter((f) => f.target === scope.target)
    case 'findings': {
      // 用户手选的那几条。用 Set 而不是 `includes`：选了几十条时
      // 后者是 O(n·m)，而这个函数在预览的每次输入变化时都会跑。
      const picked = new Set(scope.findingIds)
      return findings.filter((f) => picked.has(f.id))
    }
    case 'range': {
      const from = new Date(scope.from).getTime()
      const to = new Date(scope.to).getTime()
      if (Number.isNaN(from) || Number.isNaN(to)) return []
      // 让 to 覆盖当天最后一毫秒，避免"选了今天却没包含今天下午的发现"
      const endOfDay = new Date(to)
      endOfDay.setHours(23, 59, 59, 999)
      const end = Math.max(endOfDay.getTime(), to)
      return findings.filter((f) => {
        const t = new Date(f.discoveredAt).getTime()
        return t >= from && t <= end
      })
    }
  }
}

/**
 * 应用内容选项。
 *
 * 「只含已确认」的语义要说清楚：它保留 **confirmed + fixed**。
 *   - confirmed = 已确认存在
 *   - fixed     = 曾经确认过、现在修了 —— 从交付角度它仍然发生并处理过，
 *                 把它剔掉会让报告显示"什么都没发现"，那是失真。
 *   - new（待确认）和 false_positive（误报）才是该排除的：
 *     前者还没定论，后者已判定不是问题。给客户的报告里放这两种是失职。
 */
export function applyOptions(findings: Finding[], options: ReportOptions): Finding[] {
  if (!options.onlyConfirmed) return findings
  return findings.filter((f) => f.status === 'confirmed' || f.status === 'fixed')
}

// ─────────────────────────────────────────────
// 排序
// ─────────────────────────────────────────────

/**
 * 报告内漏洞的排序：**永远按严重度降序**。
 *
 * 不提供排序选项 —— 报告不是列表视图，它有一个天然正确的顺序：
 * 最严重的在最前面。给管理层看的文档里出现"按时间排序"只会让人漏掉严重问题。
 *
 * 同级时按发现时间**升序**（先发现的在前）：报告叙述的是"我们依次发现了什么"，
 * 和列表中"最近的在前"是相反的需求。
 */
export function sortForReport(findings: Finding[]): Finding[] {
  return [...findings].sort((a, b) => {
    const diff = SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity)
    return diff !== 0 ? diff : a.discoveredAt.localeCompare(b.discoveredAt)
  })
}

// ─────────────────────────────────────────────
// 统计
// ─────────────────────────────────────────────

export interface ReportStats {
  total: number
  bySeverity: Partial<Record<Severity, number>>
  /** 需要优先处置的：严重 + 高危 */
  urgent: number
  /** 已通过 PoC 验证的条数 */
  verified: number
  /** 已修复的条数 */
  fixed: number
  /** 涉及的目标数 */
  targets: number
  /** 涉及的扫描数 */
  scans: number
}

/**
 * 汇总统计。
 *
 * 复用 finding-query 的 countBySeverity —— 报告的分布数字必须和
 * 总览图表、漏洞列表用的是同一套口径，各写一份是"数字对不上"的起点。
 */
export function summarize(findings: Finding[]): ReportStats {
  const bySeverity = countBySeverity(findings)
  return {
    total: findings.length,
    bySeverity,
    urgent: (bySeverity.critical ?? 0) + (bySeverity.high ?? 0),
    verified: findings.filter((f) => f.verification === 'verified').length,
    fixed: findings.filter((f) => f.status === 'fixed').length,
    targets: new Set(findings.map((f) => f.target)).size,
    scans: new Set(findings.map((f) => f.scanId)).size,
  }
}

/**
 * 一句话风险结论。
 *
 * 报告开头需要一句**能被直接引用**的判断，而不是让读者自己对数字。
 * 措辞逐级收敛，且刻意避免"可能存在"这类无信息量的模糊表述 ——
 * 有 PoC 就说有，没有就说没有。
 */
export function riskVerdict(stats: ReportStats): string {
  if (stats.total === 0) return '本次范围内未发现安全问题。'
  if (stats.urgent === 0) {
    return `本次范围内共发现 ${stats.total} 个问题，均不构成直接可利用风险，建议按常规加固计划处理。`
  }
  if (stats.verified > 0) {
    return `本次范围内共发现 ${stats.total} 个安全问题，其中 ${stats.urgent} 个为严重或高危，已通过可复现的验证脚本确认其中 ${stats.verified} 个，建议优先处置。`
  }
  return `本次范围内共发现 ${stats.total} 个安全问题，其中 ${stats.urgent} 个为严重或高危，建议优先处置。`
}

// ─────────────────────────────────────────────
// 时间区间预设
// ─────────────────────────────────────────────

export type RangePreset = '7d' | '30d' | 'all'

export const RANGE_PRESET_LABEL: Record<RangePreset, string> = {
  '7d': '最近 7 天',
  '30d': '最近 30 天',
  all: '全部时间',
}

/**
 * 把预设换算成区间。
 *
 * `now` 作为参数传入而不是内部取 Date.now()：
 * 纯函数才好测，也避免同一次渲染里多次取时间导致边界抖动。
 */
export function presetToRange(
  preset: RangePreset,
  findings: Finding[],
  now: number,
): { from: string; to: string } {
  const to = new Date(now).toISOString()

  if (preset === 'all') {
    // 全部时间：从最早一条开始，而不是某个魔法日期
    if (findings.length === 0) {
      return { from: new Date(now - 86_400_000).toISOString(), to }
    }
    const earliest = findings.reduce(
      (min, f) => (f.discoveredAt < min ? f.discoveredAt : min),
      findings[0].discoveredAt,
    )
    return { from: earliest, to }
  }

  const days = preset === '7d' ? 7 : 30
  return { from: new Date(now - days * 86_400_000).toISOString(), to }
}

// ─────────────────────────────────────────────
// 可选项
// ─────────────────────────────────────────────

/**
 * 报告里实际能提供的可选内容 —— 决定界面上哪些开关该被禁用。
 *
 * 为什么要有这个：如果范围内一条 PoC 都没有，还让用户勾"包含 PoC 细节"，
 * 勾完发现报告里没有变化，用户会怀疑功能坏了。
 * 直接禁用并说明"本范围内没有可复现脚本"，才是诚实的做法。
 */
export function availableContent(findings: Finding[]) {
  return {
    poc: findings.some((f) => f.detail.poc !== null),
    reasoning: findings.some((f) => f.detail.reasoning.trim().length > 0),
    remediation: findings.some((f) => f.detail.remediation !== null),
    /** 存在未被处置的条目时才谈得上"只导出已确认" */
    unconfirmed: findings.some((f) => f.status === 'new' || f.status === 'false_positive'),
  }
}
