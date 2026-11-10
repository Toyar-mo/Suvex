import { describe, expect, it, vi } from 'vitest'

import { api } from '@/lib/api'

import { loadCoverage, loadSurfaceMap } from './assetCoverage'

vi.mock('@/lib/api', () => ({ api: { get: vi.fn() } }))

describe('asset coverage client', () => {
  it('passes target through the query string', async () => {
    const snapshot = {
      target: 'http://t.test',
      tested: [],
      ruledOut: [],
      truncated: false,
      stats: { tested: 0, ruledOut: 0 },
    }
    vi.mocked(api.get).mockResolvedValue(snapshot)

    await expect(loadCoverage('http://t.test')).resolves.toEqual(snapshot)
    expect(api.get).toHaveBeenCalledWith('/assets/coverage', {
      query: { target: 'http://t.test' },
    })
  })

  it('loads the surface map for the same target', async () => {
    const map = { target: 'http://t.test', nodes: [], edges: [], truncated: false }
    vi.mocked(api.get).mockResolvedValue(map)

    await expect(loadSurfaceMap('http://t.test')).resolves.toEqual(map)
    expect(api.get).toHaveBeenCalledWith('/assets/surface', {
      query: { target: 'http://t.test' },
    })
  })
})
