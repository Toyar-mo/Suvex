import { describe, expect, it } from 'vitest'

import type { Finding, FindingStatus } from '@/data/finding'
import { DEFAULT_REPORT_OPTIONS } from '@/data/report'
import { applyOptions } from './report-build'

/*
  ══════════════════════════════════════════════════════════════
  「只含已确认」的判据
  ══════════════════════════════════════════════════════════════

  ## 为什么单独钉这一条

  这个开关同时决定两件用户能直接看见的事：

    1. 报告正文里放哪些条目；
    2. 「生成报告」按钮是否可用 —— 页面用 `applyOptions(inScope, options)
       .length` 算条数，算成 0 就直接拦住。

  它原来写成白名单 `confirmed + fixed`，而状态从 4 个涨到 6 个之后，
  `retesting`（复测中）与 `closed`（已关闭）被静默漏掉 —— 这两类
  只能从 `confirmed` 之后流转过来，明明都是确认过的。症状是
  "我筛出 3 条，页面说 0 条，按钮点不动"，而报告里也会少放真问题。

  后端 `app/reports/document.py: apply_options` 早就是排除式。
  两处口径不一致时，前端算的条数与导出的正文对不上 —— 所以这里
  对齐的是**后端已有的语义**，不是新发明一套。
*/

function makeFinding(status: FindingStatus): Finding {
  return {
    id: `fnd_${status}`,
    scanId: 'scan_a',
    title: '示例漏洞',
    severity: 'high',
    status,
    category: 'injection',
    target: 'api.example.com',
    verification: 'verified',
    discoveredAt: '2026-10-01T10:00:00.000Z',
    detail: { affected: [] },
  } as unknown as Finding
}

const onlyConfirmed = { ...DEFAULT_REPORT_OPTIONS, onlyConfirmed: true }

describe('applyOptions / 只含已确认', () => {
  it('开关关掉时原样返回（一条都不筛）', () => {
    const findings = [makeFinding('new'), makeFinding('false_positive')]
    expect(applyOptions(findings, DEFAULT_REPORT_OPTIONS)).toHaveLength(2)
  })

  it('★ 排除待确认（new）与误报（false_positive）', () => {
    const findings = [makeFinding('new'), makeFinding('false_positive'), makeFinding('confirmed')]
    const kept = applyOptions(findings, onlyConfirmed)

    expect(kept.map((f) => f.status)).toEqual(['confirmed'])
  })

  it('★ retesting / closed 必须保留 —— 白名单会静默漏掉它们', () => {
    const findings = [
      makeFinding('confirmed'),
      makeFinding('fixed'),
      makeFinding('retesting'),
      makeFinding('closed'),
    ]

    expect(applyOptions(findings, onlyConfirmed)).toHaveLength(4)
  })

  it('★ 全是 retesting/closed 时不能算成 0 条（那会让生成按钮禁用）', () => {
    const findings = [makeFinding('retesting'), makeFinding('closed')]
    expect(applyOptions(findings, onlyConfirmed).length).toBe(2)
  })
})
