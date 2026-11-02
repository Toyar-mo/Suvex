// ⚠️ 必须手动 import `cleanup`：本仓库没开 vitest 的 `globals: true`。
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AssistantTurn } from './AssistantTurn'
import type { ChatMessage } from '@/data/chat'

/*
  ══════════════════════════════════════════════════════════════
  AssistantTurn：判据是「结论是主角，过程是证据」
  ══════════════════════════════════════════════════════════════

  这个组件的整篇注释都在讲同一件事：最终答复不包气泡、过程收进折叠。
  所以断言的落点不是"元素存在"，而是**两种消息长得不一样**：

  · 助手正文外面没有 `rounded-[10px]` / `bg-surface`（那是用户气泡的皮）；
  · 过程在结论之前，且中间有 24px 的呼吸（`pt-3`）；
  · 页脚上的步数 / 工具数 / 失败数是真的数出来的，不是常量。
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

const narration = msg({
  id: 'n1',
  surface: 'process',
  content: '我先跑一遍端口扫描',
  createdAt: '2026-10-25T03:04:00Z',
})
const toolOk = msg({
  id: 't1',
  role: 'tool',
  toolName: 'run_shell',
  toolArgs: { command: 'nmap -sV example.com' },
  content: '{"ok":true}',
  createdAt: '2026-10-25T03:05:00Z',
})
const toolFailed = msg({
  id: 't2',
  role: 'tool',
  toolName: 'run_shell',
  toolArgs: { command: 'nmap -p- example.com' },
  toolFailed: true,
  content: 'timeout',
  createdAt: '2026-10-25T03:06:00Z',
})
const answer = msg({
  id: 'a1',
  surface: 'answer',
  content: '已清点完毕',
  reasoning: '先看端口，再下结论',
  createdAt: '2026-10-25T03:07:00Z',
})

function fullTurn(): ChatMessage[] {
  return [narration, toolOk, toolFailed, answer]
}

describe('AssistantTurn — 最终答复是纯文本', () => {
  it('★ 结论可见、不是气泡皮，且过程排在结论之前', () => {
    const { container } = render(<AssistantTurn items={fullTurn()} meta="红队" />)

    const paragraph = screen.getByText('已清点完毕')
    const answerRow = paragraph.closest('[data-message-row="assistant"]') as HTMLElement
    expect(answerRow).not.toBeNull()
    // ★ 用户气泡才有这两样；助手正文套上它们，长回答就变成一大块灰砖
    expect(answerRow.className).not.toContain('rounded-[10px]')
    expect(answerRow.className).not.toContain('bg-surface')

    // 过程（data-assistant-work）必须出现在结论之前，否则读起来像"先有结论后找证据"
    const work = container.querySelector('[data-assistant-work]') as HTMLElement
    expect(work).not.toBeNull()
    expect(work.compareDocumentPosition(answerRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    // 思考过程跟着正文那条消息，默认折叠、点开才出现
    const reasoning = screen.getByRole('button', { name: '思考过程' })
    expect(reasoning.getAttribute('aria-expanded')).toBe('false')
    expect(container.textContent).not.toContain('先看端口，再下结论')
    fireEvent.click(reasoning)
    expect(reasoning.getAttribute('aria-expanded')).toBe('true')
    expect(container.textContent).toContain('先看端口，再下结论')
  })
})

describe('AssistantTurn — 回合页脚', () => {
  it('★ 4 步 · 2 个工具 · 1 个失败，时间取最后一条消息，右侧带 meta', () => {
    const { container } = render(<AssistantTurn items={fullTurn()} meta="红队" />)

    const footer = container.querySelector('[data-turn-footer]') as HTMLElement
    expect(footer).not.toBeNull()
    expect(footer.getAttribute('data-turn-steps')).toBe('4')
    expect(footer.getAttribute('data-turn-tools')).toBe('2')
    expect(footer.getAttribute('data-turn-failures')).toBe('1')

    expect(footer.textContent).toContain('4 步')
    expect(footer.textContent).toContain('2 个工具')
    // ★ 失败数要在页脚上露出来，不能只活在收起的分组里
    expect(footer.textContent).toContain('1 个失败')

    // 时间来自最后一条消息（03:07），不是第一条
    const time = footer.querySelector('time') as HTMLTimeElement
    expect(time).not.toBeNull()
    expect(time.getAttribute('datetime')).toBe('2026-10-25T03:07:00Z')
    expect(time.textContent).toMatch(/^\d{2}:\d{2}$/)
    expect(footer.textContent).toContain('红队')
  })

  it('★ 时间戳解析不了时退成空串，而不是渲染「Invalid Date」', () => {
    const { container } = render(
      <AssistantTurn items={[msg({ id: 'x', content: 'hi', createdAt: 'not-a-date' })]} />,
    )
    const footer = container.querySelector('[data-turn-footer]') as HTMLElement
    expect(footer).not.toBeNull()
    const time = footer.querySelector('time') as HTMLTimeElement
    expect(time.textContent).toBe('')
  })
})

describe('AssistantTurn — 过程与结论之间的间距', () => {
  it('★ 有工作过程时结论容器带 pt-3（24px），没有时不带', () => {
    /*
      正文 <p> → MessageRow 的 div → 结论容器。pt-3 在**最外层**那个容器上：
      它管的是「过程 → 结论」这一整段之间的呼吸，不是单条消息内部的间距。
    */
    const withWork = render(<AssistantTurn items={fullTurn()} />)
    const answerWithWork = screen.getByText('已清点完毕')
    const containerWithWork = answerWithWork.parentElement!.parentElement!
    expect(containerWithWork.className).toContain('pt-3')
    withWork.unmount()

    render(<AssistantTurn items={[answer]} />)
    const answerAlone = screen.getByText('已清点完毕')
    const containerAlone = answerAlone.parentElement!.parentElement!
    expect(containerAlone.className).not.toContain('pt-3')
  })
})

describe('AssistantTurn — 插话只属于正在跑的那一轮', () => {
  const interjection = msg({ id: 'i1', role: 'user', content: '顺便看看 443', seq: 9 })

  it('★ live 且传了 interjections 才画', () => {
    const { container } = render(<AssistantTurn items={fullTurn()} live interjections={[interjection]} />)
    const note = container.querySelector('[data-interjection]')
    expect(note).not.toBeNull()
    expect(note!.textContent).toContain('顺便看看 443')
  })

  it('★ 非 live 即使传了也不画（那一轮已经结束，插话已经落成下一条用户消息）', () => {
    const { container } = render(<AssistantTurn items={fullTurn()} interjections={[interjection]} />)
    expect(container.querySelector('[data-interjection]')).toBeNull()
  })
})

describe('AssistantTurn — 跑着的时候', () => {
  it('★ 流式正文 + 阶段行都在回合里，但没有回合页脚', () => {
    const { container } = render(
      <AssistantTurn
        items={[narration, toolOk]}
        live
        streamingText="半截"
        phaseText="第 7 轮 · 模型在推理…"
      />,
    )

    const bubble = container.querySelector('[data-streaming-bubble]') as HTMLElement
    expect(bubble).not.toBeNull()
    expect(bubble.textContent).toContain('半截')

    const status = container.querySelector('[role="status"]') as HTMLElement
    expect(status).not.toBeNull()
    expect(status.textContent).toContain('第 7 轮')

    // 这一轮还没结束，页脚（步数/时间）现在出现就是在说谎
    expect(container.querySelector('[data-turn-footer]')).toBeNull()
  })
})

describe('AssistantTurn — 待审批的消息透传回调', () => {
  it('★ 最终答复位的待审批消息：回合把 id 补回 callback，不只交一个 bool', () => {
    const onDecide = vi.fn()
    const pendingAnswer = msg({
      id: 'a9',
      surface: 'answer',
      approvalStatus: 'pending',
      toolName: 'create_scan',
      toolArgs: { target: 'example.com' },
      content: '等待确认',
    })

    render(<AssistantTurn items={[pendingAnswer]} onDecide={onDecide} />)

    // 它落在「最终答复」位，但 MessageRow 依然优先画审批卡
    fireEvent.click(screen.getByRole('button', { name: '执行' }))
    expect(onDecide).toHaveBeenCalledWith('a9', true)
    fireEvent.click(screen.getByRole('button', { name: '拒绝' }))
    expect(onDecide).toHaveBeenCalledWith('a9', false)
  })

  it('★ 工作过程里的待审批工具卡也同样带 id', () => {
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

    render(<AssistantTurn items={[narration, pending]} onDecide={onDecide} />)

    // 待审批的消息不是"最终答复"，所以它留在工作过程里；但也绝不能因为
    // surface=process 就被折进旁白组（AssistantWork.test.tsx 管这一条）。
    fireEvent.click(screen.getByRole('button', { name: '执行' }))
    expect(onDecide).toHaveBeenCalledWith('p1', true)

    fireEvent.click(screen.getByRole('button', { name: '拒绝' }))
    expect(onDecide).toHaveBeenCalledWith('p1', false)
  })
})
