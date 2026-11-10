import { describe, expect, it, vi } from 'vitest'

import { api } from '@/lib/api'

import {
  captureSnapshot,
  compareSnapshots,
  createCase,
  loadRuns,
  loadSnapshots,
  runSuite,
} from './benchmark'

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

describe('benchmark cases client', () => {
  it('creates a case', async () => {
    const created = {
      id: 'case_1',
      suite: 'smoke',
      name: '首页扫描',
      kind: 'scan' as const,
      challengeId: null,
      scanId: 'scan_1',
      expect: { minFindings: 1 },
      enabled: true,
      createdAt: '2026-11-10T00:00:00Z',
    }
    vi.mocked(api.post).mockResolvedValue(created)

    await expect(
      createCase({
        suite: 'smoke',
        name: '首页扫描',
        kind: 'scan',
        scanId: 'scan_1',
        expect: { minFindings: 1 },
      }),
    ).resolves.toEqual(created)
    expect(api.post).toHaveBeenCalledWith('/benchmark/cases', {
      suite: 'smoke',
      name: '首页扫描',
      kind: 'scan',
      scanId: 'scan_1',
      expect: { minFindings: 1 },
    })
  })

  it('runs a suite and loads latest runs', async () => {
    const suiteRun = {
      suite: 'smoke',
      runs: [],
      passed: 1,
      failed: 0,
      pending: 0,
      error: 0,
    }
    vi.mocked(api.post).mockResolvedValue(suiteRun)
    vi.mocked(api.get).mockResolvedValue([])

    await expect(runSuite('smoke')).resolves.toEqual(suiteRun)
    expect(api.post).toHaveBeenCalledWith('/benchmark/suites/smoke/run')

    await expect(loadRuns('smoke')).resolves.toEqual([])
    expect(api.get).toHaveBeenCalledWith('/benchmark/runs?suite=smoke')
  })
})
