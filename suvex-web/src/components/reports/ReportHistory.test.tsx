/**
 * 历史报告的删除入口与角色（v1.79）。
 *
 * ## 为什么钉这一条
 *
 * 后端 `DELETE /reports/{id}` 要 **admin**（契约 §3.5 的 v0.10 修正），
 * 而删除按钮原来对所有人显示。只读用户看到的是一个完好的红色垃圾桶，
 * 点下去必然 403 —— **看得到却点不动**比看不到更糟：
 * 他会以为系统坏了，而不是"我没有这个权限"。
 *
 * ⚠️ 与项目其它组件测试一致：手动 `cleanup()`、不用 jest-dom 匹配器。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import ReportHistory from './ReportHistory'
import type { Report } from '@/data/report'

function makeReport(overrides: Partial<Report> = {}): Report {
  return {
    id: 'rpt_1',
    title: '示例渗透报告',
    code: 'RPT-20261109-TEST',
    findingCount: 3,
    excludedCount: 0,
    severityCounts: { critical: 1, high: 2 },
    createdAt: '2026-11-09T10:00:00.000Z',
    format: 'pdf',
    scope: { kind: 'scan', scanId: 'scan_1' },
    options: {
      includePoc: false,
      includeReasoning: false,
      onlyConfirmed: false,
      includeRemediation: true,
    },
    ...overrides,
  } as Report
}

const noop = () => {}

function renderHistory(canDelete: boolean) {
  render(
    <ReportHistory
      reports={[makeReport()]}
      onOpen={noop}
      onDownload={noop}
      onDelete={noop}
      downloadingId={null}
      describeScopeOf={() => '扫描任务「acme-web」'}
      canDelete={canDelete}
      emptyState={<p>空</p>}
    />,
  )
}

afterEach(cleanup)

describe('ReportHistory 的删除入口', () => {
  it('管理员看得到删除按钮', () => {
    renderHistory(true)
    expect(screen.getByRole('button', { name: /删除「示例渗透报告」/ })).not.toBeNull()
  })

  it('★ 只读用户看不到删除按钮（点了必然 403）', () => {
    renderHistory(false)
    expect(screen.queryByRole('button', { name: /删除「示例渗透报告」/ })).toBeNull()
    // 打开/下载不受影响 —— 只读用户的正常工作是看报告
    expect(screen.getByRole('button', { name: /打开/ })).not.toBeNull()
    expect(screen.getByRole('button', { name: /下载/ })).not.toBeNull()
  })
})
