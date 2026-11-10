import { describe, expect, it, vi } from 'vitest'

import { api } from '@/lib/api'

import { createAgent, deleteAgent, loadAgents } from './agents'

vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), post: vi.fn(), del: vi.fn() } }))

const profile = {
  id: 'agp_1',
  key: 'planner',
  name: '规划',
  description: '',
  builtin: true,
  enabled: true,
  mcpServers: [],
  skills: [],
  tools: [],
  createdAt: '2026-11-10T00:00:00Z',
  updatedAt: '2026-11-10T00:00:00Z',
}

describe('agents client', () => {
  it('loads profiles', async () => {
    vi.mocked(api.get).mockResolvedValue([profile])
    await expect(loadAgents()).resolves.toEqual([profile])
    expect(api.get).toHaveBeenCalledWith('/agents')
  })

  it('creates and deletes', async () => {
    vi.mocked(api.post).mockResolvedValue({ ...profile, builtin: false })
    vi.mocked(api.del).mockResolvedValue({ deleted: true })

    await createAgent({ key: 'x', name: 'X' })
    expect(api.post).toHaveBeenCalledWith('/agents', { key: 'x', name: 'X' })

    await deleteAgent('agp 1')
    expect(api.del).toHaveBeenCalledWith('/agents/agp%201')
  })
})
