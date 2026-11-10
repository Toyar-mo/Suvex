import { describe, expect, it, vi } from 'vitest'

import { api, apiBlob } from '@/lib/api'

import { createRule, evaluateRules, exportHits, loadRules } from './purple'

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), del: vi.fn() },
  apiBlob: vi.fn(),
}))

const rule = {
  id: 'rule_1',
  name: 'CWE-89 SQL 注入',
  severity: 'high',
  cwe: 'CWE-89',
  titleContains: null,
  targetContains: null,
  enabled: true,
  createdAt: '2026-11-10T00:00:00Z',
}

describe('purple client', () => {
  it('loads rules', async () => {
    vi.mocked(api.get).mockResolvedValue([rule])

    await expect(loadRules()).resolves.toEqual([rule])
    expect(api.get).toHaveBeenCalledWith('/purple/rules')
  })

  it('creates a rule', async () => {
    vi.mocked(api.post).mockResolvedValue(rule)

    await expect(createRule({ name: 'SQL', cwe: 'CWE-89', severity: 'high' })).resolves.toEqual(
      rule,
    )
    expect(api.post).toHaveBeenCalledWith('/purple/rules', {
      name: 'SQL',
      cwe: 'CWE-89',
      severity: 'high',
    })
  })

  it('evaluates via POST', async () => {
    const result = { totalFindings: 3, hits: [] }
    vi.mocked(api.post).mockResolvedValue(result)

    await expect(evaluateRules()).resolves.toEqual(result)
    expect(api.post).toHaveBeenCalledWith('/purple/evaluate')
  })

  it('exports hits as a blob', async () => {
    const out = { blob: new Blob(['x']), filename: 'purple-hits.jsonl', mime: 'application/x-ndjson' }
    vi.mocked(apiBlob).mockResolvedValue(out)

    await expect(exportHits('jsonl')).resolves.toEqual(out)
    expect(apiBlob).toHaveBeenCalledWith('/purple/export?format=jsonl')
  })
})
