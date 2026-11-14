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
    const bubble = row.querySelector('.rounded-lg') as HTMLElement
    expect(bubble).not.toBeNull()
    expect(bubble.className).toContain('rounded-lg')
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
    expect(row.className).not.toContain('rounded-lg')
    expect(row.className).not.toContain('bg-surface')
    // 没有 reasoning 就不该冒出「思考过程」按钮
    expect(screen.queryByRole('button', { name: /思考过程/ })).toBeNull()
  })

  it('★ 有 reasoning 时「思考过程」默认折叠，点开才看到全文', () => {
    const { container } = render(
      <MessageRow
        message={msg({ id: 'a2', content: '结论', reasoning: '先看端口，再下结论' })}
      />,
    )

    const toggle = screen.getByRole('button', { name: /思考过程/ })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(container.textContent).not.toContain('先看端口，再下结论')

    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(container.textContent).toContain('先看端口，再下结论')
  })

  it('★ 有模型耗时时按 Tern 的 Thought for Ns 显示', () => {
    render(
      <MessageRow
        message={msg({
          id: 'a3',
          content: '结论',
          reasoning: '先看端口',
          durationMs: 1400,
        })}
      />,
    )

    expect(screen.getByRole('button', { name: /思考了 1 秒|Thought for 1s/ })).toBeTruthy()
  })

  it('★ 独立的 ReasoningBlock 也能自己开合（导出给别处用）', () => {
    const { container } = render(<ReasoningBlock text="先跑 passive 扫描" />)
    expect(container.textContent).not.toContain('先跑 passive 扫描')
    fireEvent.click(screen.getByRole('button', { name: /思考过程/ }))
    expect(container.textContent).toContain('先跑 passive 扫描')
  })
})

describe('MessageRow — 助手正文渲染 Markdown（v1.75）', () => {
  it('★ 加粗/列表/代码块被渲染，而不是显示星号原文', () => {
    const { container } = render(
      <MessageRow
        message={msg({
          id: 'md1',
          content: '**结论**\n\n- 第一项\n- 第二项\n\n```bash\nls -la\n```',
        })}
      />,
    )

    const row = container.querySelector('[data-markdown-answer]') as HTMLElement
    expect(row).not.toBeNull()
    expect(row.querySelector('strong')?.textContent).toBe('结论')
    expect(row.querySelectorAll('li').length).toBe(2)
    expect(row.querySelector('pre')).not.toBeNull()
    expect(row.textContent).not.toContain('**结论**')
  })

  it('★ /work/persist 路径（正文与行内代码）都变成下载链接', () => {
    const { container } = render(
      <MessageRow
        sessionId="chat_1"
        message={msg({
          id: 'md2',
          content: '结果在 `/work/persist/report.md`，副本 /work/persist/copy.txt',
        })}
      />,
    )

    const links = Array.from(
      container.querySelectorAll('a[download]'),
    ) as HTMLAnchorElement[]
    expect(links.length).toBe(2)
    const hrefs = links.map((link) => decodeURIComponent(link.getAttribute('href') ?? ''))
    expect(hrefs[0]).toContain('/chat/sessions/chat_1/files/download')
    expect(hrefs[0]).toContain('path=report.md')
    expect(hrefs[1]).toContain('path=copy.txt')
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

describe('MessageRow — 工具输出折叠', () => {
  it('★ 长输出默认收起，点击「展开输出」才铺开', () => {
    const long = Array.from({ length: 20 }, (_, i) => `line ${i}`).join('\n')
    const { container } = render(
      <MessageRow
        message={msg({ id: 't-long', role: 'tool', toolName: 'run_shell', content: long })}
      />,
    )

    const card = container.querySelector('[data-message-row="tool"]') as HTMLElement
    expect(card.getAttribute('data-tool-collapsed')).toBe('true')
    const expand = screen.getByRole('button', { name: /展开输出/ })
    fireEvent.click(expand)
    expect(card.getAttribute('data-tool-collapsed')).toBe('false')
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

  it('★ v1.71：start_competition_race 审批卡带「会自动交 flag / 有费用」的人话说明', () => {
    render(
      <MessageRow
        message={msg({
          id: 'p2',
          role: 'tool',
          approvalStatus: 'pending',
          toolName: 'start_competition_race',
          toolArgs: { models: ['deepseek-v4.1-flash'], maxConcurrent: 2 },
          content: '等待确认',
        })}
        onDecide={vi.fn()}
      />,
    )
    expect(screen.getByText('要执行一个操作，等你确认')).not.toBeNull()
    expect(screen.getByText(/自动提交/)).not.toBeNull()
    expect(screen.getByText(/费用/)).not.toBeNull()
  })

  it('未登记说明的工具（如 create_scan）不显示多余的人话说明', () => {
    render(
      <MessageRow
        message={msg({
          id: 'p3',
          role: 'tool',
          approvalStatus: 'pending',
          toolName: 'create_scan',
          toolArgs: { target: 'example.com' },
          content: '等待确认',
        })}
        onDecide={vi.fn()}
      />,
    )
    expect(screen.queryByText(/自动提交/)).toBeNull()
  })
})
