import { describe, expect, it } from 'vitest'
import { sarifFallbackFilename, findingsSarifUrl } from './sarif-export'
import { findingsExportUrl } from '@/data/findings-store'

/**
 * SARIF 导出 URL 的守卫。
 *
 * 存在的理由：`findingsSarifUrl` 是在 CSV 那个 URL 上**换路径段**得来的
 * （`/export` → `/export/sarif`）。这种"改写别人的输出"的写法最容易
 * 在对方改了 URL 形状时静默坏掉 —— 换不到就原样返回，用户拿到的是
 * **CSV 内容的 .sarif 文件**，而没有人会发现。
 */
describe('findingsSarifUrl', () => {
  it('把 /export 换成 /export/sarif', () => {
    expect(findingsSarifUrl({})).toBe('/api/v1/findings/export/sarif')
  })

  it('筛选参数原样保留', () => {
    const url = findingsSarifUrl({ severity: 'critical,high', scanId: 'scan_01ABC' })
    expect(url).toContain('/export/sarif?')
    expect(url).toContain('severity=critical%2Chigh')
    expect(url).toContain('scanId=scan_01ABC')
    // 查询串必须在路径段之后 —— 换错位置的表现是参数跑到 /export 和 /sarif 之间
    expect(url.indexOf('?')).toBeGreaterThan(url.indexOf('/export/sarif'))
  })

  it('多值维度与 CSV 表达一致', () => {
    const params = { status: 'new,confirmed', category: 'xss,sqli' } as const
    const sarif = findingsSarifUrl(params)
    /*
      ★ 这条判据是本次反向验证逼出来的（v0.85）。

      原来的写法是 `expect(sarif).toBe(findingsSarifUrl(params))` ——
      **完全自指**，恒真。反向验证里把正则换成不带锚点的
      `replace('/export', '/export/sarif')` 之后测试照样绿，
      因为真实 URL 的形状是 `/api/v1/findings/export?...`，
      第一个 `export` 恰好就是要换的那个 —— 只要前缀永不变
      `export`，这个 bug 就永远测不出来。

      改成「与被替换的那段**真正不同**」：从 CSV 的 URL 取路径段，
      断言 SARIF 的路径段是它的父路径 + `export/sarif`。
      这样"没换"、"换错位置"、"多换一刀"都会露馅。
    */
    const csvPath = new URL(findingsExportUrl(params), 'http://x').pathname
    const sarifPath = new URL(sarif, 'http://x').pathname
    expect(sarifPath).toBe(csvPath.replace(/\/export$/, '/export/sarif'))
    expect(sarifPath.endsWith('/findings/export/sarif')).toBe(true)
  })

  it('false 与空串不写进查询串（与 CSV 同规则）', () => {
    const url = findingsSarifUrl({ unassigned: false, deleted: false, q: '' })
    expect(url).toBe('/api/v1/findings/export/sarif')
  })
})

describe('sarifFallbackFilename', () => {
  it('形如 suvex-findings-YYYYMMDD.sarif', () => {
    // 用固定时刻，避免跨零点时这个用例偶发失败
    expect(sarifFallbackFilename(new Date(2026, 9, 23))).toBe('suvex-findings-20261023.sarif')
  })

  it('月份与日期补零', () => {
    expect(sarifFallbackFilename(new Date(2026, 0, 5))).toBe('suvex-findings-20260105.sarif')
  })
})
