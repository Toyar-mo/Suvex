import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { AssetMap } from './AssetMap'
import type { HostAsset } from '@/data/asset-host'

afterEach(cleanup)

function host(name: string, overrides: Partial<HostAsset> = {}): HostAsset {
  return {
    host: name,
    assets: [],
    ports: ['80', '443'],
    openFindingCount: 3,
    openCritical: 1,
    openHigh: 0,
    lastCompletedAt: null,
    health: 'ok',
    ...overrides,
  } as HostAsset
}

it('每台主机一个节点，点节点把主机名交回调用方', () => {
  const onSelectHost = vi.fn()
  render(
    <AssetMap
      hosts={[host('10.0.0.5'), host('10.0.0.6', { health: 'failing', openHigh: 2 })]}
      onSelectHost={onSelectHost}
    />,
  )

  expect(screen.getByText('2 台主机')).toBeTruthy()
  expect(screen.getByText('2 台有未处置高危')).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: /10\.0\.0\.6/ }))
  expect(onSelectHost).toHaveBeenCalledWith('10.0.0.6')
})

it('缩放按钮改变百分比，重置回到 100%', () => {
  render(<AssetMap hosts={[host('10.0.0.5')]} />)

  expect(screen.getByText('100%')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '放大' }))
  expect(screen.getByText('115%')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '重置视图' }))
  expect(screen.getByText('100%')).toBeTruthy()
})
