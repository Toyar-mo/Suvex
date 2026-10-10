import { SEVERITY_ORDER } from '@/data/findings'
import { totalFindings, type Scan, type ScanStatus } from '@/data/scan'

/**
 * 扫描列表的筛选与排序 —— 纯函数，放在 lib 而不是组件文件里。
 *
 * 为什么单独一个文件：一是这些逻辑需要被测试（纯函数可以独立验证），
 * 二是组件文件里混导出非组件会让 Fast Refresh 失效（改一个常量就整页重挂载）。
 */

export type SortKey = 'recent' | 'severity' | 'duration'
export type SortDir = 'asc' | 'desc'

export const SORT_LABEL: Record<SortKey, string> = {
  recent: '按时间',
  severity: '按严重度',
  duration: '按耗时',
}

export interface ScanFilter {
  status: ScanStatus | 'all'
  /** 目标关键字，大小写不敏感 */
  query: string
}

/**
 * 排序。
 *
 * 「按严重度」用的是**这次扫描发现的最高严重度**，不是漏洞总数 ——
 * 用户问的是"哪次扫描问题最严重"，不是"哪次找得最多"。
 * 一个 critical 比十个 low 更该排前面。
 */
export function sortScans(scans: Scan[], key: SortKey, dir: SortDir): Scan[] {
  /** 返回最高严重度在 SEVERITY_ORDER 里的索引；没有发现则返回末尾 */
  const severityRank = (scan: Scan): number => {
    for (const [index, severity] of SEVERITY_ORDER.entries()) {
      if ((scan.findings[severity] ?? 0) > 0) return index
    }
    return SEVERITY_ORDER.length
  }

  const compare = (a: Scan, b: Scan): number => {
    switch (key) {
      case 'recent':
        // 新→旧。ISO 串的字符串比较等价于时间比较
        return b.startedAt.localeCompare(a.startedAt)
      case 'severity': {
        const diff = severityRank(a) - severityRank(b)
        // 同级时用总数作次级排序，否则同级别的相对顺序不稳定
        return diff !== 0 ? diff : totalFindings(b.findings) - totalFindings(a.findings)
      }
      case 'duration': {
        // 未结束的扫描没有耗时，一律排到最后 —— **不能当成 0**，
        // 那会让"正在跑的"出现在"最快的"位置，是明显的错误排序
        const ad = a.durationSec
        const bd = b.durationSec
        if (ad === null && bd === null) return 0
        if (ad === null) return 1
        if (bd === null) return -1
        return ad - bd
      }
      default:
        return 0
    }
  }

  const sorted = [...scans].sort(compare)
  // recent / severity 的"降序"语义已在 compare 里定好，只有 duration 需要按方向反转
  if (key === 'duration') {
    return dir === 'desc' ? sorted.reverse() : sorted
  }
  return dir === 'asc' ? sorted.reverse() : sorted
}

/** 筛选。状态与关键字是 AND 关系 */
export function filterScans(scans: Scan[], filter: ScanFilter): Scan[] {
  let result = scans

  if (filter.status !== 'all') {
    result = result.filter((scan) => scan.status === filter.status)
  }

  const query = filter.query.trim().toLowerCase()
  if (query) {
    result = result.filter(
      (scan) =>
        scan.target.toLowerCase().includes(query) || scan.name.toLowerCase().includes(query),
    )
  }

  return result
}
