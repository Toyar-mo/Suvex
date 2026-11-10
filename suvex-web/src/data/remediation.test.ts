import { describe, expect, it, vi } from 'vitest'

import { api } from '@/lib/api'

import { loadRemediation } from './remediation'

vi.mock('@/lib/api', () => ({ api: { get: vi.fn() } }))

describe('loadRemediation', () => {
  it('encodes the finding id in the path', async () => {
    const plan = {
      findingId: 'f 1',
      cwe: 'CWE-89',
      category: 'injection',
      readiness: 'ready',
      title: 'SQL 注入：参数化查询',
      steps: ['a'],
      verification: ['b'],
      note: 'n',
    }
    vi.mocked(api.get).mockResolvedValue(plan)

    await expect(loadRemediation('f 1')).resolves.toEqual(plan)
    expect(api.get).toHaveBeenCalledWith('/findings/f%201/remediation')
  })
})
