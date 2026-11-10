import { describe, expect, it, vi } from 'vitest'

import { api } from '@/lib/api'

import { loadQuality, type QualitySnapshot } from './quality'

vi.mock('@/lib/api', () => ({ api: { get: vi.fn() } }))

const snapshot: QualitySnapshot = {
  scans: { total: 3, completed: 2, failed: 1, aborted: 0, avgDurationSec: 120 },
  findings: {
    total: 4,
    verified: 3,
    needsFollowUp: 1,
    none: 0,
    falsePositive: 1,
    falsePositiveRate: 0.25,
  },
  challenges: {
    total: 10,
    solved: 4,
    solveRate: 0.4,
    candidateCount: 6,
    wrongSubmissions: 2,
  },
  coverage: { surfaces: 12, targets: 3 },
  generatedAt: '2026-11-10T00:00:00Z',
}

describe('loadQuality', () => {
  it('reads /overview/quality and returns the snapshot', async () => {
    vi.mocked(api.get).mockResolvedValue(snapshot)

    await expect(loadQuality()).resolves.toEqual(snapshot)
    expect(api.get).toHaveBeenCalledWith('/overview/quality')
  })
})
