import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it } from 'vitest'

import { I18nProvider } from '@/i18n'
import { PriorityBoard } from './PriorityBoard'

afterEach(() => {
  cleanup()
  localStorage.clear()
})

it('P0–P4 每级一行，数量与"查看"链接指向过滤后的发现列表', () => {
  // jsdom 的 navigator.language 是 en-US，先钉住中文，断言才不看环境。
  localStorage.setItem('suvex.locale', 'zh-CN')
  render(
    <I18nProvider>
      <MemoryRouter>
        <PriorityBoard
          data={[
            { severity: 'critical', count: 2 },
            { severity: 'high', count: 5 },
            { severity: 'medium', count: 0 },
            { severity: 'low', count: 1 },
            { severity: 'info', count: 3 },
          ]}
        />
      </MemoryRouter>
    </I18nProvider>,
  )

  const board = screen.getByTestId('priority-board')
  expect(board.textContent).toContain('共 11 条未处置')
  // 五个级别都在，且 0 值保留一行（缺行会被读成"漏了"）
  for (const level of ['P0', 'P1', 'P2', 'P3', 'P4']) {
    expect(board.querySelector(`[data-priority="${level}"]`)).not.toBeNull()
  }
  const p0 = board.querySelector('[data-priority="P0"]')
  expect(p0?.textContent).toContain('2')

  const link = screen.getAllByRole('link').at(0)
  expect(link?.getAttribute('href')).toBe('/findings?severity=critical')
})
