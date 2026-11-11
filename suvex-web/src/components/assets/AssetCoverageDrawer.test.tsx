import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AssetCoverageDrawer from './AssetCoverageDrawer'
import { loadCoverage, loadSurfaceMap } from '@/data/assetCoverage'

vi.mock('@/data/assetCoverage', () => ({
  loadCoverage: vi.fn(),
  loadSurfaceMap: vi.fn(),
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AssetCoverageDrawer', () => {
  it('用标题给 dialog 命名，并把两个截断都明确说出来', async () => {
    vi.mocked(loadCoverage).mockResolvedValue({
      target: 'http://t.test',
      tested: [],
      ruledOut: [],
      gaps: [],
      truncated: true,
      stats: { tested: 0, ruledOut: 0 },
    })
    vi.mocked(loadSurfaceMap).mockResolvedValue({
      target: 'http://t.test',
      nodes: [],
      edges: [],
      truncated: true,
    })

    render(<AssetCoverageDrawer target="http://t.test" onClose={() => {}} />)

    expect(await screen.findByRole('dialog', { name: '覆盖度与事实图' })).toBeTruthy()
    expect(await screen.findByText(/覆盖度记录已截断/)).toBeTruthy()
    expect(await screen.findByText(/事实图已截断/)).toBeTruthy()
  })
})
