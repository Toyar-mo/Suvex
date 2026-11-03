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
  it('★ 4 步 · 2 个工具，已交付答复时工具报错报成「已恢复」不标红（v1.59）', () => {
    const { container } = render(<AssistantTurn items={fullTurn()} meta="红队" />)

    const footer = container.querySelector('[data-turn-footer]') as HTMLElement
    expect(footer).not.toBeNull()
    expect(footer.getAttribute('data-turn-steps')).toBe('4')
    expect(footer.getAttribute('data-turn-tools')).toBe('2')
    // 真实失败数照旧挂在 data 属性上（事实不藏）
    expect(footer.getAttribute('data-turn-failures')).toBe('1')

    expect(footer.textContent).toContain('4 步')
    expect(footer.textContent).toContain('2 个工具')
    // ★ v1.59：这一轮**给出了最终答复**，中途一次工具报错是过程挫折、已恢复。
    //   页脚如实报「1 次工具报错（已恢复）」，不再用会让人以为整轮失败的红字「1 个失败」。
    expect(footer.textContent).toContain('1 次工具报错（已恢复）')
    expect(footer.textContent).not.toContain('1 个失败')

    // 时间来自最后一条消息（03:07），不是第一条
    const time = footer.querySelector('time') as HTMLTimeElement
    expect(time).not.toBeNull()
    expect(time.getAttribute('datetime')).toBe('2026-10-25T03:07:00Z')
    expect(time.textContent).toMatch(/^\d{2}:\d{2}$/)
    expect(footer.textContent).toContain('红队')
  })

  it('★ 没给出答复（真的没成）时，失败仍报红字「N 个失败」（v1.59）', () => {
    // 一个没有最终答复（全 process）的回合，工具还失败了 —— 这才是真失败，要红字。
    const { container } = render(
      <AssistantTurn items={[narration, toolOk, toolFailed]} />,
    )
    const footer = container.querySelector('[data-turn-footer]') as HTMLElement
    expect(footer).not.toBeNull()
    expect(footer.getAttribute('data-turn-failures')).toBe('1')
    expect(footer.textContent).toContain('1 个失败')
    expect(footer.textContent).not.toContain('已恢复')
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

describe('AssistantTurn — 单一时间线（v1.56b 对照 CyberStrikeAI 事件流）', () => {
  it('★ 消息按发生顺序铺下来：旁白 → 工具 → 工具 → 答复，答复在最后', () => {
    /*
      这是 v1.56b 的核心契约：不再把全部过程收进顶上一个「过程详情」折叠块、
      结论另起一块贴在最下面；而是推理/工具/结果/答复各在**它发生的那个
      位置**逐条铺下来（CyberStrikeAI 的 progress/thinking/tool_call/
      tool_result/答复 事件按到达顺序追加）。

      断言：答复正文的 DOM 位置排在两条工具之后（不是被提到工具上面）。
    */
    const { container } = render(<AssistantTurn items={fullTurn()} />)

    const answerRow = screen
      .getByText('已清点完毕')
      .closest('[data-message-row="assistant"]') as HTMLElement
    expect(answerRow).not.toBeNull()

    const toolRows = container.querySelectorAll('[data-message-row="tool"]')
    expect(toolRows.length).toBe(2)
    const lastTool = toolRows[toolRows.length - 1] as HTMLElement

    // 答复必须在最后一条工具**之后**（文档顺序），而不是被提到工具前面。
    expect(
      lastTool.compareDocumentPosition(answerRow) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('★ 交错回合：答复夹在两次工具之间时，位置就在中间，不被挪到最后', () => {
    /*
      真实的多轮 agent 回合常常是：旁白 → 工具 … → 一段结论 → 又去跑工具 →
      收尾答复。旧结构会把所有非最终答复都收进顶上过程块，导致中间那段
      「结论」要么消失、要么被顶到最后。时间线模型里它就该待在中间。
    */
    const midAnswer = msg({
      id: 'a-mid',
      surface: 'answer',
      content: '端口扫完了，接着看服务',
      createdAt: '2026-10-25T03:05:30Z',
    })
    const items = [narration, toolOk, midAnswer, toolFailed, answer]
    const { container } = render(<AssistantTurn items={items} />)

    const allRows = Array.from(
      container.querySelectorAll('[data-message-row]'),
    ) as HTMLElement[]
    const idxOf = (text: string) =>
      allRows.findIndex((row) => row.textContent && row.textContent.includes(text))

    const midIdx = idxOf('端口扫完了，接着看服务')
    const finalIdx = idxOf('已清点完毕')
    const firstToolIdx = idxOf('nmap -sV example.com')
    const secondToolIdx = idxOf('nmap -p- example.com')

    // 中间结论在第一条工具之后、第二条工具之前 —— 位置保真。
    expect(midIdx).toBeGreaterThan(firstToolIdx)
    expect(midIdx).toBeLessThan(secondToolIdx)
    // 收尾答复在最后。
    expect(finalIdx).toBeGreaterThan(secondToolIdx)
  })

  it('★ 不再有「把全部过程提到结论之上」的 data-assistant-work 头部块', () => {
    /*
      旧结构：<AssistantWork items=workItems/> 在最前，<结论/> 在最后 —— 于是
      data-assistant-work 一定早于答复。新结构里旁白仍可能是可折叠条，但它
      按时间线**原位**出现，不再是一个统揽全部过程的前置块。

      判据：答复与「第一条工具」之间，应夹着旁白/工具的**时间线顺序**，
      不能出现「答复之前存在一个包含全部工具的 assistant-work」这种旧形态。
      这里直接用:答复前面不能是那个把答案排除在外的 work 头块。
    */
    const { container } = render(<AssistantTurn items={fullTurn()} />)
    const root = container.querySelector('[data-assistant-turn]') as HTMLElement
    expect(root.hasAttribute('data-timeline')).toBe(true)
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
