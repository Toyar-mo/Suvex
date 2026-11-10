import { describe, expect, it, vi } from 'vitest'

import { api } from '@/lib/api'

import { loadModelRoutes, saveModelRoutes } from './modelRoutes'

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), put: vi.fn() },
}))

describe('model routes client', () => {
  it('loads the routes snapshot', async () => {
    const snapshot = { routes: { findVulns: 'p1' } }
    vi.mocked(api.get).mockResolvedValue(snapshot)

    await expect(loadModelRoutes()).resolves.toEqual(snapshot)
    expect(api.get).toHaveBeenCalledWith('/settings/model-routes')
  })

  it('saves routes with the documented body shape', async () => {
    const snapshot = { routes: { captureFlag: 'p2' } }
    vi.mocked(api.put).mockResolvedValue(snapshot)

    await expect(saveModelRoutes({ captureFlag: 'p2' })).resolves.toEqual(snapshot)
    expect(api.put).toHaveBeenCalledWith('/settings/model-routes', {
      routes: { captureFlag: 'p2' },
    })
  })
})
