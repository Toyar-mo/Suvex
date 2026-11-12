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

  · 助手正文外面没有 `rounded-lg` / `bg-surface`（那是用户气泡的皮）；
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
    expect(answerRow.className).not.toContain('rounded-lg')
    expect(answerRow.className).not.toContain('bg-surface')

    // 过程摘要（data-turn-work）在结论之前，结论不被工具记录淹没
    const work = container.querySelector('[data-turn-work]') as HTMLElement
    expect(work).not.toBeNull()
    expect(work.compareDocumentPosition(answerRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    // 思考过程跟着正文那条消息，默认折叠、点开才出现
    const reasoning = screen.getByRole('button', { name: /思考过程/ })
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

    // v1.92：页脚只留时间与人格标签；步数/工具数在 data 属性里（供端到端
    // 断言），不再用一行密集文字重复工具卡已有的信息。
    expect(footer.textContent).not.toContain('步')
    expect(footer.textContent).not.toContain('个工具')
    // 已交付答复：过程中的一次工具报错不再在页脚重复标红。
    expect(footer.textContent).not.toContain('失败')

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
    expect(footer.textContent).toContain('1 个工具失败')
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

describe('AssistantTurn — 过程直接铺开（对齐 omp，v1.92）', () => {
  it('★ 默认就能看到每一次工具调用与旁白，而不是一条折叠摘要', () => {
    const { container } = render(<AssistantTurn items={fullTurn()} />)

    const work = container.querySelector('[data-turn-work]') as HTMLElement
    expect(work).not.toBeNull()
    expect(work.getAttribute('data-turn-work-steps')).toBe('3')
    expect(work.getAttribute('data-turn-work-tools')).toBe('2')

    // 没有「工作过程」折叠按钮：过程本身就在转录里
    expect(screen.queryByRole('button', { name: /工作过程/ })).toBeNull()

    // 两条工具都在 DOM 里，且旁白在第一条工具之前（真实发生顺序）
    const toolRows = Array.from(
      container.querySelectorAll('[data-message-row="tool"]'),
    ) as HTMLElement[]
    expect(toolRows.length).toBe(2)
    const narrationLine = container.querySelector('[data-narration-line]') as HTMLElement
    expect(narrationLine.textContent).toContain('我先跑一遍端口扫描')
    expect(
      narrationLine.compareDocumentPosition(toolRows[0]) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('★ 失败在折叠/展开两种状态下都看得出来', () => {
    const { container } = render(<AssistantTurn items={[toolFailed]} />)
    const row = container.querySelector('[data-message-row="tool"]') as HTMLElement
    expect(row.textContent).toContain('失败')
    expect(row.querySelector('.text-signal-risk')).not.toBeNull()
  })

  it('★ 工具参数摘要始终在标题行上', () => {
    const { container } = render(<AssistantTurn items={[toolOk]} />)
    expect(container.textContent).toContain('nmap -sV example.com')
  })

  it('★ 交错回合的中间结论也始终露在过程之外', () => {
    const midAnswer = msg({
      id: 'a-mid',
      surface: 'answer',
      content: '端口扫完了，接着看服务',
      createdAt: '2026-10-25T03:05:30Z',
    })
    render(<AssistantTurn items={[narration, toolOk, midAnswer, toolFailed, answer]} />)
    expect(screen.getByText('端口扫完了，接着看服务')).toBeTruthy()
    expect(screen.getByText('已清点完毕')).toBeTruthy()
  })
})

describe('AssistantTurn — 只看回答', () => {
  it('★ hideWork 时不渲染任何过程，只留结论', () => {
    const { container } = render(<AssistantTurn items={fullTurn()} hideWork />)
    expect(container.querySelector('[data-message-row="tool"]')).toBeNull()
    expect(container.querySelector('[data-narration-line]')).toBeNull()
    expect(screen.getByText('已清点完毕')).toBeTruthy()
  })

  it('★ hideWork 且本轮没有回答时给一行说明，而不是空白', () => {
    const { container } = render(<AssistantTurn items={[narration, toolOk]} hideWork />)
    expect(container.querySelector('[data-answers-only-placeholder]')).not.toBeNull()
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

describe('AssistantTurn — live 阶段行上的「已 N 次工具」计数（v1.30）', () => {
  /*
    这个计数由 ChatPage 从 `turnToolCount(turn.items)` 算好、经 `progress`
    prop 传进来（ChatPage 那边数对，这里负责渲染）。钉两条最容易骗人的：

    ① live 且 tools>0 时，阶段行要显示「（已 N 次工具）」；
    ② tools 为 0 或没传 progress 时，**不显示**——不能在还没执行工具时
       就蹦出「已 0 次工具」让用户以为卡住了。
  */
  it('★ live 且传了 tools，阶段行显示「（已 N 次工具）」', () => {
    const { container } = render(
      <AssistantTurn items={[narration, toolOk]} live phaseText="第 3 轮 · 正在执行 run_shell…" progress={{ tools: 3 }} />,
    )
    const status = container.querySelector('[role="status"]') as HTMLElement
    expect(status).not.toBeNull()
    expect(status.textContent).toContain('（已 3 次工具）')
  })

  it('★ tools=0 时不显示计数（别在没跑工具时就写「已 0 次」）', () => {
    const { container } = render(
      <AssistantTurn items={[narration]} live phaseText="第 1 轮 · 模型在推理…" progress={{ tools: 0 }} />,
    )
    const status = container.querySelector('[role="status"]') as HTMLElement
    expect(status.textContent).not.toContain('次工具')
  })

  it('没传 progress 时不显示计数', () => {
    const { container } = render(<AssistantTurn items={[narration]} live phaseText="第 1 轮 · 模型在推理…" />)
    const status = container.querySelector('[role="status"]') as HTMLElement
    expect(status.textContent).not.toContain('次工具')
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

describe('AssistantTurn — 运行中的工具（v1.92）', () => {
  it('live + liveTool 时画一张「执行中」的卡片', () => {
    const { container } = render(<AssistantTurn items={[]} live liveTool="run_shell" />)
    const running = container.querySelector('[data-live-tool]') as HTMLElement
    expect(running).not.toBeNull()
    expect(running.textContent).toContain('run_shell')
    expect(running.textContent).toContain('执行中')
  })

  it('live 但没有正在执行的工具时不画运行卡', () => {
    const { container } = render(<AssistantTurn items={[]} live />)
    expect(container.querySelector('[data-live-tool]')).toBeNull()
  })
})

describe('AssistantTurn — MCP 真实进度（v1.92）', () => {
  it('有 progress/total 时显示百分比而不是秒数', () => {
    const { container } = render(
      <AssistantTurn
        items={[]}
        live
        liveTool="mcp__x__scan"
        liveToolProgress={{ seconds: 0, progress: 3, total: 10, message: '扫描中' }}
      />,
    )
    const running = container.querySelector('[data-live-tool]') as HTMLElement
    expect(running.textContent).toContain('30%')
    expect(running.textContent).toContain('扫描中')
  })
})
