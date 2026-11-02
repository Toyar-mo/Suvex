// ⚠️ 必须手动 import `cleanup`：本仓库没开 vitest 的 `globals: true`。
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { MessageRow, ReasoningBlock } from './MessageRow'
import type { ChatMessage } from '@/data/chat'

/*
  ══════════════════════════════════════════════════════════════
  MessageRow：一条消息的四种皮，不能互相冒充
  ══════════════════════════════════════════════════════════════

  这一层最贵的错是**角色画错**：

  · 用户消息没有气泡 → 分不清谁说的；
  · 助手正文套了气泡 → 长回答变成一大块灰砖（v1.25 的老皮，v1.29 已拆掉）；
  · 工具消息没走工具卡 → 结果糊成一段正文；
  · 待审批的消息按 role 画成"已跑完" → 用户以为它做完了，其实在等人点头。

  所以四条分支各钉一遍，顺带钉住「思考过程」默认折叠。
*/

afterEach(cleanup)

function msg(over: Partial<ChatMessage> & { id: string }): ChatMessage {
  return {
    seq: 1,
    role: 'assistant',
    content: '',
    createdAt: '2026-10-25T00:00:00Z',
    ...over,
  }
}

describe('MessageRow — 用户消息', () => {
  it('★ 用户消息是右对齐的气泡（唯一该有气泡皮的角色）', () => {
    const { container } = render(
      <MessageRow message={msg({ id: 'u1', role: 'user', content: '扫一下 10.0.0.5' })} />,
    )

    const row = container.querySelector('[data-message-row="user"]') as HTMLElement
    expect(row).not.toBeNull()
    expect(row.className).toContain('justify-end')
    expect(row.textContent).toContain('扫一下 10.0.0.5')
    // 气泡皮只在这一支上出现
    const bubble = row.firstElementChild as HTMLElement
    expect(bubble.className).toContain('rounded-[10px]')
    expect(bubble.className).toContain('bg-surface')
  })
})

describe('MessageRow — 助手正文', () => {
  it('★ 助手正文是纯文本：没有气泡皮、没有灰底', () => {
    const { container } = render(
      <MessageRow message={msg({ id: 'a1', content: '已清点完毕' })} />,
    )

    const row = container.querySelector('[data-message-row="assistant"]') as HTMLElement
    expect(row).not.toBeNull()
    expect(row.textContent).toContain('已清点完毕')
    expect(row.className).not.toContain('rounded-[10px]')
    expect(row.className).not.toContain('bg-surface')
    // 没有 reasoning 就不该冒出「思考过程」按钮
    expect(screen.queryByRole('button', { name: '思考过程' })).toBeNull()
  })

  it('★ 有 reasoning 时「思考过程」默认折叠，点开才看到全文', () => {
    const { container } = render(
      <MessageRow
        message={msg({ id: 'a2', content: '结论', reasoning: '先看端口，再下结论' })}
      />,
    )

    const toggle = screen.getByRole('button', { name: '思考过程' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(container.textContent).not.toContain('先看端口，再下结论')

    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(container.textContent).toContain('先看端口，再下结论')
  })

  it('★ 独立的 ReasoningBlock 也能自己开合（导出给别处用）', () => {
    const { container } = render(<ReasoningBlock text="先跑 passive 扫描" />)
    expect(container.textContent).not.toContain('先跑 passive 扫描')
    fireEvent.click(screen.getByRole('button', { name: '思考过程' }))
    expect(container.textContent).toContain('先跑 passive 扫描')
  })
})

describe('MessageRow — 工具消息', () => {
  it('★ 工具消息走工具卡，而不是一段正文', () => {
    const { container } = render(
      <MessageRow
        message={msg({
          id: 't1',
          role: 'tool',
          toolName: 'run_shell',
          toolArgs: { command: 'ls' },
          content: '{"ok":true}',
        })}
      />,
    )

    const row = container.querySelector('[data-message-row="tool"]') as HTMLElement
    expect(row).not.toBeNull()
    expect(row.getAttribute('data-tool-name')).toBe('run_shell')
    expect(container.querySelector('[data-message-row="assistant"]')).toBeNull()
  })
})

describe('MessageRow — 待审批优先于角色', () => {
  it('★ 即使 role=tool，pending 也画审批卡而不是「已跑完」的工具卡', () => {
    const onDecide = vi.fn()
    const { container } = render(
      <MessageRow
        message={msg({
          id: 'p1',
          role: 'tool',
          approvalStatus: 'pending',
          toolName: 'create_scan',
          toolArgs: { target: 'example.com' },
          content: '等待确认',
        })}
        onDecide={onDecide}
      />,
    )

    expect(screen.getByText('要执行一个操作，等你确认')).not.toBeNull()
    expect(screen.getByText('create_scan')).not.toBeNull()
    // ★ 关键：没有画成工具卡 —— 那会让人以为它跑完了
    expect(container.querySelector('[data-message-row="tool"]')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '执行' }))
    expect(onDecide).toHaveBeenCalledWith(true)
    fireEvent.click(screen.getByRole('button', { name: '拒绝' }))
    expect(onDecide).toHaveBeenCalledWith(false)
  })
})
