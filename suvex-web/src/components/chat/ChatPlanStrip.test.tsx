import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { ChatPlanStrip } from './ChatPlanStrip'

vi.mock('@/data/chat', () => ({ listTodos: vi.fn() }))

import { listTodos } from '@/data/chat'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('把步骤与完成数摆出来（只读）', async () => {
  vi.mocked(listTodos).mockResolvedValue([
    { seq: 1, text: '枚举端点', status: 'done' },
    { seq: 2, text: '验证越权', status: 'in_progress' },
    { seq: 3, text: '写报告', status: 'pending' },
  ])

  render(<ChatPlanStrip sessionId="chat_1" refreshKey={0} />)

  expect(await screen.findByText('枚举端点')).toBeTruthy()
  expect(screen.getByText('计划 · 1/3 完成')).toBeTruthy()
  expect(screen.getByText('#3')).toBeTruthy()
})

it('没有计划时什么都不渲染', async () => {
  vi.mocked(listTodos).mockResolvedValue([])

  const { container } = render(<ChatPlanStrip sessionId="chat_1" refreshKey={0} />)
  await Promise.resolve()
  await Promise.resolve()

  expect(container.querySelector('[data-testid="chat-plan"]')).toBeNull()
})
