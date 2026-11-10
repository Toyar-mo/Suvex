import { describe, expect, it } from 'vitest'
import type { Asset } from '@/data/asset'
import { buildHostAssets, parseTargetParts } from './asset-host'

function asset(target: string, patch: Partial<Asset> = {}): Asset {
  return {
    target,
    scanCount: 1,
    lastScanAt: '2026-10-20T00:00:00Z',
    lastScanStatus: 'completed',
    lastCompletedAt: '2026-10-20T00:00:00Z',
    consecutiveFailures: 0,
    findingCount: 0,
    openFindingCount: 0,
    openCritical: 0,
    openHigh: 0,
    bySeverity: {},
    ...patch,
  }
}

describe('parseTargetParts', () => {
  it('splits scheme, port, path and query', () => {
    expect(parseTargetParts('http://127.0.0.1:9001/.env?x=1')).toEqual({
      host: '127.0.0.1',
      port: '9001',
      path: '/.env?x=1',
    })
  })

  it('handles bare host:port', () => {
    expect(parseTargetParts('10.20.158.101:11311 (ROS1 master)')).toEqual({
      host: '10.20.158.101',
      port: '11311',
      path: '/',
    })
  })

  it('keeps CIDR as a host instead of splitting the prefix', () => {
    expect(parseTargetParts('10.0.0.0/24')).toEqual({ host: '10.0.0.0/24', port: null, path: '' })
  })
})

describe('buildHostAssets', () => {
  it('merges paths by host and takes the worst health', () => {
    const hosts = buildHostAssets(
      [
        asset('http://127.0.0.1:9001/a', { openHigh: 1, openFindingCount: 1 }),
        asset('http://127.0.0.1:9001/b', { lastCompletedAt: null, lastScanStatus: 'failed' }),
        asset('http://127.0.0.1:8080/c'),
      ],
      Date.parse('2026-10-21T00:00:00Z'),
    )
    expect(hosts).toHaveLength(1)
    expect(hosts[0].assets).toHaveLength(3)
    expect(hosts[0].ports).toEqual(['8080', '9001'])
    expect(hosts[0].openHigh).toBe(1)
    expect(hosts[0].health).toBe('never-completed')
    expect(hosts[0].neverCompletedCount).toBe(1)
  })

  it('orders bleeding hosts first, then worst health', () => {
    const hosts = buildHostAssets([
      asset('a.example'),
      asset('z.example', { openCritical: 1, openFindingCount: 1 }),
      asset('m.example', { lastCompletedAt: null, lastScanStatus: 'failed' }),
    ])
    expect(hosts.map((host) => host.host)).toEqual(['z.example', 'm.example', 'a.example'])
  })
})
