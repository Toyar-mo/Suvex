import type { Scan } from '@/data/scan'
import { SCAN_STATUS_LABEL, totalFindings } from '@/data/scan'

/**
 * 扫描列表导出（任务 9）。
 *
 * ## 为什么走 CSV 而不是 Markdown
 *
 * 这个导出的用途是"把选中的几次扫描拿去对账/贴进表格"，
 * 而不是"交付给客户"（那是报告页的职责，它有冻结内容与编号）。
 * CSV 能被 Excel / Sheets 直接打开，而 Markdown 表格还要再转一手。
 *
 * ## 为什么在前端生成，不新增后端端点
 *
 * 数据**全在列表里已经有了**（就是用户眼前那几列）。为它加一个
 * `GET /scans/export` 意味着后端要知道"列表显示哪几列"——
 * 那是展示层的知识，加一次列就要改两个仓库。而报告导出走后端
 * 是因为它要**冻结内容**（`detail`、PoC、时间线），那是另一回事。
 */

/** CSV 字段转义：含逗号/引号/换行的值必须用引号包起来，内部引号翻倍。 */
function cell(value: string): string {
  if (/[",\n\r]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`
  }
  return value
}

const HEADERS = ['目标', '类型', '状态', '严重度分布', '发现数', '耗时(秒)', '开始时间', '摘要']

export function scansToCsv(scans: Scan[]): string {
  const rows = scans.map((scan) => {
    const found = totalFindings(scan.findings)
    const breakdown = Object.entries(scan.findings)
      .filter(([, count]) => count > 0)
      .map(([sev, count]) => `${sev}:${count}`)
      .join(' ')

    return [
      scan.target,
      scan.targetKind,
      SCAN_STATUS_LABEL[scan.status],
      breakdown,
      String(found),
      scan.durationSec === null ? '' : String(scan.durationSec),
      scan.startedAt,
      scan.failureReason ?? '',
    ]
      .map(cell)
      .join(',')
  })

  // ★ 前缀 BOM。少了它 Excel 会用本地代码页解 UTF-8，
  //   中文列名与目标名全变成乱码 —— 而用户会以为是导出坏了。
  return '\uFEFF' + [HEADERS.map(cell).join(','), ...rows].join('\r\n') + '\r\n'
}

/**
 * 触发一次浏览器下载。
 *
 * 用 `Blob` + 临时 `<a download>`，不新增依赖。
 * 文件名带日期：同一个目标反复导出几次时，至少不会互相覆盖。
 */
export function downloadCsv(filename: string, csv: string): void {
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  // 立刻释放：不释放的话每次导出都会漏一份 blob 在内存里，
  // 而这个页面可能被反复导出。
  URL.revokeObjectURL(url)
}

/** 导出文件名。形如 `suvex-scans-20260925.csv`。 */
export function exportFilename(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
  return `suvex-scans-${stamp}.csv`
}
