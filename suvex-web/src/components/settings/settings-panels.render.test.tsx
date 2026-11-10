/**
 * 新增设置面板的渲染验证（v1.82）。
 *
 * 只验"渲染出来且值对得上"：数据层请求形状由各自的 *.test.ts 覆盖，
 * 这里补的是用户能不能看到。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { BenchmarkCasesPanel } from './BenchmarkCasesPanel'
import { ModelRoutesPanel } from './ModelRoutesPanel'
import { PurplePanel } from './PurplePanel'
import type { ProviderAccount } from '@/data/settings'

vi.mock('@/data/toast-store', () => ({ showToast: vi.fn() }))

vi.mock('@/data/purple', () => ({
  loadRules: async () => [
    {
      id: 'rule_1',
      name: 'CWE-89 SQL 注入',
      severity: 'high',
      cwe: 'CWE-89',
      titleContains: null,
      targetContains: null,
      enabled: true,
      createdAt: '2026-11-10T00:00:00Z',
    },
  ],
  evaluateRules: vi.fn(),
  createRule: vi.fn(),
  deleteRule: vi.fn(),
  exportHits: vi.fn(),
}))

vi.mock('@/data/benchmark', () => ({
  loadCases: async () => [
    {
      id: 'case_1',
      suite: 'smoke',
      name: '首页扫描',
      kind: 'scan',
      challengeId: null,
      scanId: 'scan_1',
      expect: { minFindings: 1 },
      enabled: true,
      createdAt: '2026-11-10T00:00:00Z',
    },
  ],
  loadRuns: async () => [
    {
      id: 'run_1',
      caseId: 'case_1',
      status: 'passed',
      detail: {},
      collectedAt: '2026-11-10T00:00:00Z',
    },
  ],
  createCase: vi.fn(),
  deleteCase: vi.fn(),
  runSuite: vi.fn(),
}))

vi.mock('@/data/modelRoutes', () => ({
  loadModelRoutes: async () => ({ routes: { skill: 'x' } }),
  saveModelRoutes: vi.fn(),
}))

const accounts = [
  {
    id: 'p1',
    label: 'DeepSeek',
    model: 'deepseek-v4.1-flash',
    kind: 'openai',
    apiKey: '',
  },
] as unknown as ProviderAccount[]

afterEach(cleanup)

describe('settings panels', () => {
  it('renders purple rules and cwe', async () => {
    render(<PurplePanel />)

    expect(await screen.findByText('紫队检测规则')).toBeTruthy()
    expect(await screen.findByText('CWE-89 SQL 注入')).toBeTruthy()
    expect(screen.getByText(/CWE-89 · high/)).toBeTruthy()
  })

  it('renders benchmark case with latest status', async () => {
    render(<BenchmarkCasesPanel />)

    expect(await screen.findByText('评测用例')).toBeTruthy()
    expect(await screen.findByText('首页扫描')).toBeTruthy()
    expect(await screen.findByText('通过')).toBeTruthy()
  })

  it('renders skill route selects', async () => {
    render(<ModelRoutesPanel accounts={accounts} />)

    expect(await screen.findByText('模型路由')).toBeTruthy()
    expect(await screen.findByText('技能 · Web')).toBeTruthy()
    expect(await screen.findByText('技能 · 云')).toBeTruthy()
  })
})
