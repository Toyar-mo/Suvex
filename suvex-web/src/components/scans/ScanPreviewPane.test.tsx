import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it } from 'vitest'

import { ScanPreviewPane } from './ScanPreviewPane'
import type { Scan } from '@/data/scan'

afterEach(cleanup)

function makeScan(): Scan {
  return {
    id: 'scan_1',
    name: '内网资产复测',
    target: '10.0.0.5:8080',
    targetKind: 'url',
    status: 'completed',
    skills: ['web'],
    providerId: 'p1',
    startedAt: '2026-11-11T10:00:00Z',
    finishedAt: '2026-11-11T10:10:00Z',
    durationSec: 600,
    agentCount: 2,
    findings: { critical: 1, high: 2 },
    failureReason: null,
    progress: 1,
    runId: null,
    archivedAt: null,
    deletedAt: null,
    terminatedBy: null,
  } as Scan
}

it('没选中时给空态，不渲染一个空白栏', () => {
  render(
    <MemoryRouter>
      <ScanPreviewPane scan={null} />
    </MemoryRouter>,
  )

  expect(screen.getByText('从左侧选择一个任务')).toBeTruthy()
})

it('选中后显示目标、发现数与详情入口', () => {
  render(
    <MemoryRouter>
      <ScanPreviewPane scan={makeScan()} />
    </MemoryRouter>,
  )

  expect(screen.getByText('内网资产复测')).toBeTruthy()
  expect(screen.getByText('10.0.0.5:8080')).toBeTruthy()
  expect(screen.getByText('3')).toBeTruthy()
  expect(screen.getByRole('link', { name: /查看执行详情/ }).getAttribute('href')).toBe(
    '/activity?scan=scan_1',
  )
})
