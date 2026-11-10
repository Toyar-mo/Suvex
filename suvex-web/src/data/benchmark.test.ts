import { describe, expect, it, vi } from 'vitest'

import { api } from '@/lib/api'

import { captureSnapshot, compareSnapshots, loadSnapshots } from './benchmark'

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn() },
}))

const snapshot = {
  id: 'snap_1',
  label: '基线',
  capturedAt: '2026-11-10T00:00:00Z',
  payload: {
    scans: { total: 0, completed: 0, failed: 0, aborted: 0, avgDurationSec: null },
    findings: {
      total: 0,
      verified: 0,
      needsFollowUp: 0,
      none: 0,
      falsePositive: 0,
      falsePositiveRate: null,
    },
    challenges: {
      total: 0,
      solved: 0,
      solveRate: null,
      candidateCount: 0,
      wrongSubmissions: 0,
    },
    coverage: { surfaces: 0, targets: 0 },
  },
}

describe('benchmark client', () => {
  it('unwraps the snapshot list', async () => {
    vi.mocked(api.get).mockResolvedValue({ items: [snapshot] })

    await expect(loadSnapshots()).resolves.toEqual([snapshot])
    expect(api.get).toHaveBeenCalledWith('/benchmark/snapshots?limit=50')
  })

  it('captures a snapshot', async () => {
    vi.mocked(api.post).mockResolvedValue(snapshot)

    await expect(captureSnapshot('基线')).resolves.toEqual(snapshot)
    expect(api.post).toHaveBeenCalledWith('/benchmark/snapshots', { label: '基线' })
  })

  it('encodes compare ids', async () => {
    const compare = { before: snapshot, after: snapshot, delta: { solveRate: 0 } }
    vi.mocked(api.get).mockResolvedValue(compare)

    await expect(compareSnapshots('a b', 'c&d')).resolves.toEqual(compare)
    expect(api.get).toHaveBeenCalledWith('/benchmark/compare?before=a%20b&after=c%26d')
  })
})
