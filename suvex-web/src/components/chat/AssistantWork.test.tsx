// ⚠️ 必须手动 import `cleanup`：本仓库没开 vitest 的 `globals: true`。
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AssistantWork } from './AssistantWork'
import type { ChatMessage } from '@/data/chat'

/*
  ══════════════════════════════════════════════════════════════
  AssistantWork：判据是「过程被收好，但该露的一件都不能少」
  ══════════════════════════════════════════════════════════════

  这一层做的是**聚合**，聚合最容易犯的错有两种：

  ① 把待审批的工具也收进折叠块 —— 用户看不到"它在等我点一下"，
     整轮就停在那儿，而界面上看起来很平静；
  ② 顺序被打乱 —— 旁白 A、旁白 B、工具、旁白 C 被压成"工具 + 三条旁白"，
     读起来因果就反了。

  所以断言三件事：分段（2 步 / 1 步）、单条工具不套组、审批卡永远在外面。
*/

afterEach(cleanup)

function msg(over: Partial<ChatMessage> & { id: string }): ChatMessage {
  return {
    seq: 1,
    role: 'assistant',
    content: '',
    createdAt: '2026-10-25T03:04:00Z',
    ...over,
  }
}

const narrationA = msg({ id: 'n1', surface: 'process', content: '我先看看端口' })
const narrationB = msg({ id: 'n2', surface: 'process', content: '顺手看一眼证书' })
const narrationC = msg({ id: 'n3', surface: 'process', content: '证书快过期了' })
const oneTool = msg({
  id: 't1',
  role: 'tool',
  toolName: 'run_shell',
  toolArgs: { command: 'nmap -sV example.com' },
  content: '{"ok":true}',
})

describe('AssistantWork — 分段保持真实顺序', () => {
  it('★ 旁白A、旁白B、tool、旁白C → 两个旁白组（2 步 / 1 步）+ 一条裸工具卡', () => {
    const { container } = render(
      <AssistantWork items={[narrationA, narrationB, oneTool, narrationC]} />,
    )
    const work = container.querySelector('[data-assistant-work]') as HTMLElement
    expect(work).not.toBeNull()
    // 三段：旁白组、工具、旁白组 —— 顺序就是消息顺序
    expect(work.children.length).toBe(3)

    const groups = container.querySelectorAll('[data-narration-group]')
    expect(groups.length).toBe(2)
    expect(groups[0].getAttribute('data-narration-steps')).toBe('2')
    expect(groups[1].getAttribute('data-narration-steps')).toBe('1')

    expect(screen.getByRole('button', { name: /过程详情 · 2 步/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /过程详情 · 1 步/ })).toBeTruthy()

    // ★ 单条工具**不**套 "1 个工具" 的组（那会让工具名藏进第二层）
    expect(container.querySelector('[data-tool-group]')).toBeNull()
    const toolRow = container.querySelector('[data-message-row="tool"]')
    expect(toolRow).not.toBeNull()
    expect(work.children[1]).toBe(toolRow)

    // 段内文字是真实的旁白内容，不是占位。旁白默认收起，所以先展开两段。
    fireEvent.click(screen.getByRole('button', { name: /过程详情 · 2 步/ }))
    fireEvent.click(screen.getByRole('button', { name: /过程详情 · 1 步/ }))
    expect(work.children[0].textContent).toContain('我先看看端口')
    expect(work.children[0].textContent).toContain('顺手看一眼证书')
    expect(work.children[2].textContent).toContain('证书快过期了')
  })
})

describe('AssistantWork — 旁白默认开合', () => {
  it('★ 跑着时默认展开；一轮结束后（live=false）自动收起', () => {
    const items = [narrationA, narrationB]
    const { container, rerender } = render(<AssistantWork items={items} live />)

    expect(container.querySelectorAll('[data-narration-line]').length).toBe(2)
    const open = screen.getByRole('button', { name: /正在处理 · 2 步/ })
    expect(open.getAttribute('aria-expanded')).toBe('true')

    rerender(<AssistantWork items={items} live={false} />)

    const closed = screen.getByRole('button', { name: /过程详情 · 2 步/ })
    expect(closed.getAttribute('aria-expanded')).toBe('false')
    // ★ 收起意味着旁白真的不在 DOM 里，而不是只把高度压成 0
    expect(container.querySelectorAll('[data-narration-line]').length).toBe(0)
  })

  it('★ 用户手动开合过之后，回合结束不再抢回去', () => {
    /*
      原实现是 `useEffect(() => setOpen(live), [live])`：`live` 一变就把
      用户的选择覆盖掉 —— 而这句“手动点过之后不再自动抢回去”正是写在
      组件注释里的承诺。

      序列：跑着（默认展开）→ 点一下收起（用户要专心看结论）→ 再点一下
      展开（用户想核对证据）→ 回合结束（live=false）。最后一个动作是用户的，
      必须保留。
    */
    const items = [narrationA, narrationB]
    const { container, rerender } = render(<AssistantWork items={items} live />)
    const toggle = screen.getByRole('button', { name: /正在处理 · 2 步/ })

    fireEvent.click(toggle)
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')

    rerender(<AssistantWork items={items} live={false} />)

    const after = screen.getByRole('button', { name: /过程详情 · 2 步/ })
    expect(after.getAttribute('aria-expanded')).toBe('true')
    expect(container.querySelectorAll('[data-narration-line]').length).toBe(2)
  })
})

describe('AssistantWork — 待审批的消息永远不折叠', () => {
  it('★ pending 的消息即使 surface=process 也画审批卡，且收不进旁白组', () => {
    const onDecide = vi.fn()
    const pending = msg({
      id: 'p1',
      role: 'tool',
      surface: 'process',
      approvalStatus: 'pending',
      toolName: 'create_scan',
      toolArgs: { target: 'example.com' },
      content: '等待确认',
    })

    const { container } = render(
      <AssistantWork items={[narrationA, pending, narrationB]} onDecide={onDecide} />,
    )

    const heading = screen.getByText('要执行一个操作，等你确认')
    // 不在任何旁白组里 —— 否则收起旁白时"等我确认"就消失了
    expect(heading.closest('[data-narration-group]')).toBeNull()
    expect(container.querySelectorAll('[data-narration-line]').length).toBe(0)

    const work = container.querySelector('[data-assistant-work]') as HTMLElement
    // 三段：旁白组、审批卡、旁白组（审批卡把连续旁白切开了）
    expect(work.children.length).toBe(3)
    expect(work.children[1].textContent).toContain('要执行一个操作')
    expect(work.children[1].textContent).toContain('create_scan')
  })

  it('★「执行」回调 true、「拒绝」回调 false，且都带这条消息的 id', () => {
    const onDecide = vi.fn()
    const pending = msg({
      id: 'p1',
      role: 'tool',
      approvalStatus: 'pending',
      toolName: 'create_scan',
      toolArgs: { target: 'example.com' },
      content: '等待确认',
    })

    render(<AssistantWork items={[pending]} onDecide={onDecide} />)

    fireEvent.click(screen.getByRole('button', { name: '执行' }))
    expect(onDecide).toHaveBeenCalledWith('p1', true)

    fireEvent.click(screen.getByRole('button', { name: '拒绝' }))
    expect(onDecide).toHaveBeenCalledWith('p1', false)
  })
})
