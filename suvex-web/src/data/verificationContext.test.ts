import { describe, expect, it, vi } from 'vitest'

import { api } from '@/lib/api'

import { loadVerificationContext } from './verificationContext'

vi.mock('@/lib/api', () => ({ api: { get: vi.fn() } }))

describe('verification context client', () => {
  it('loads with an encoded finding id', async () => {
    const payload = {
      finding: {
        id: 'f 1',
        title: 't',
        severity: 'high',
        cwe: 'CWE-89',
        target: 'http://x',
        verification: 'verified',
        status: 'new',
      },
      facts: [],
      relatedFindings: [],
      checks: { class: 'sqli', general: [], specific: [] },
      note: 'n',
    }
    vi.mocked(api.get).mockResolvedValue(payload)

    await expect(loadVerificationContext('f 1')).resolves.toEqual(payload)
    expect(api.get).toHaveBeenCalledWith('/findings/f%201/verification-context')
  })
})
