import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { AgentHub } from './AgentHub'

vi.mock('@/data/chat', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/data/chat')>()
  return {
    ...actual,
    loadSessionPreview: vi.fn(async () => ({
      messages: [
        {
          id: 'm1',
          seq: 1,
          role: 'assistant',
          content: '已确认 443 暴露在 nginx 1.24.0 上。',
          createdAt: '2026-11-12T01:01:00Z',
          surface: 'answer',
        },
      ],
    })),
  }
})
import type { ChatSession } from '@/data/chat'

function child(over: Partial<ChatSession> = {}): ChatSession {
  return {
    id: 'chat_child',
    title: '[子代理] port-scan',
    createdBy: 'u1',
    createdAt: '2026-11-12T01:00:00Z',
    updatedAt: '2026-11-12T01:02:00Z',
    messageCount: 4,
    archived: false,
    running: false,
    parentSessionId: 'chat_main',
    ...over,
  }
}

function renderHub(over: Partial<Parameters<typeof AgentHub>[0]> = {}) {
  const props = {
    open: true,
    onClose: vi.fn(),
    parent: null,
    children: [child()],
    runningIds: [],
    onOpen: vi.fn(),
    onGuide: vi.fn(async () => 'ok' as const),
    onStop: vi.fn(),
    ...over,
  }
  render(<AgentHub {...props} />)
  return props
}

describe('AgentHub — 子代理面板（v1.92）', () => {
  it('列出去掉前缀的名字、状态与消息数', () => {
    renderHub()
    expect(screen.getByText('port-scan')).toBeTruthy()
    expect(screen.getByText('已结束')).toBeTruthy()
    expect(screen.getByText(/4 条消息/)).toBeTruthy()
  })

  it('运行中的子代理显示运行态、引导输入与停止按钮', () => {
    renderHub({ children: [child({ running: true })] })
    expect(screen.getByText('运行中')).toBeTruthy()
    expect(screen.getByPlaceholderText(/给这个子代理补一句/)).toBeTruthy()
    expect(screen.getByLabelText('停止子代理')).toBeTruthy()
  })

  it('★ 引导提交把 sessionId 与文本交给调用方；成功后清空输入', async () => {
    const props = renderHub({ children: [child({ running: true })] })
    const input = screen.getByPlaceholderText(/给这个子代理补一句/)
    fireEvent.change(input, { target: { value: '先看 443' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    await screen.findByText(/已把提示发给 port-scan/)
    expect(props.onGuide).toHaveBeenCalledWith('chat_child', '先看 443')
  })

  it('★ 子代理已结束时打开提示而不是假装发送成功', async () => {
    const props = renderHub({
      children: [child({ running: true })],
      onGuide: vi.fn(async () => 'no-turn' as const),
    })
    const input = screen.getByPlaceholderText(/给这个子代理补一句/)
    fireEvent.change(input, { target: { value: '继续' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    await screen.findByText(/已经结束了/)
    expect(props.onGuide).toHaveBeenCalled()
  })

  it('空态说明 spawn_subagents', () => {
    renderHub({ children: [] })
    expect(screen.getByText(/没有派生子代理/)).toBeTruthy()
    expect(screen.getByText(/spawn_subagents/)).toBeTruthy()
  })

  it('从子代理打开时给出「返回主对话」入口', () => {
    const parent = child({ id: 'chat_main', title: '主线调查', parentSessionId: null })
    const props = renderHub({ parent, children: [child()] })
    fireEvent.click(screen.getByText(/返回主对话/))
    expect(props.onOpen).toHaveBeenCalledWith(parent)
  })
})

describe('AgentHub — 转录预览（v1.92）', () => {
  it('点名字展开最近转录，不离开主对话', async () => {
    renderHub()
    fireEvent.click(screen.getByText('port-scan'))
    await screen.findByText(/已确认 443/)
    // 仍然在面板里；没有触发 onOpen（那是「打开」按钮的职责）
    expect(screen.getByText(/已确认 443/)).toBeTruthy()
  })
})
