// ⚠️ 必须手动 import `cleanup`：本仓库没开 vitest 的 `globals: true`，
//    RTL 的自动清理不会注册。漏了它的症状是「Found multiple elements」。
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { ToolGroup } from './ToolGroup'
import type { ChatMessage } from '@/data/chat'

/*
  ══════════════════════════════════════════════════════════════
  ToolGroup：判据是「收起时也不能把失败藏起来」
  ══════════════════════════════════════════════════════════════

  这一层折叠的**全部风险**是信息丢失：收起来之后用户只能看到一行摘要，
  如果失败数没冒到那一行上，"工具挂了"就和"工具跑完了"长得一样。
  所以断言集中在三件事：

  ① 摘要行的文案与数值（几个工具、几个失败）必须是真的；
  ② 收起态**确实**没有明细（否则"折叠"只是视觉上变窄，DOM 里还全都在）；
  ③ 展开后的列表是限高的（一组十几条会把结论顶出屏幕）。
*/

afterEach(cleanup)

function tool(id: string, over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id,
    seq: 1,
    role: 'tool',
    content: '{"ok":true}',
    toolName: 'run_shell',
    toolArgs: { command: 'ls -la' },
    toolFailed: null,
    createdAt: '2026-10-25T03:04:00Z',
    ...over,
  }
}

/** 3 条工具，其中 2 条失败（第二条显式失败，第三条连工具名都没有）。 */
function threeTools(): ChatMessage[] {
  return [
    tool('t1', { toolName: 'list_scans' }),
    tool('t2', { toolName: 'run_shell', toolFailed: true, content: 'denied' }),
    tool('t3', { toolName: null, toolFailed: true, toolArgs: null, content: 'boom' }),
  ]
}

function groupOf(container: HTMLElement): HTMLElement {
  const group = container.querySelector('[data-tool-group]')
  expect(group, '没有渲染出 [data-tool-group] 根节点').not.toBeNull()
  return group as HTMLElement
}

describe('ToolGroup — 摘要行（收起态唯一的信息）', () => {
  it('★ 3 个工具里有 2 个失败：文案、徽标、data 属性三者必须一致', () => {
    const { container } = render(<ToolGroup items={threeTools()} />)
    const group = groupOf(container)

    // data 属性：机器可读的那一份（卡片外边距 / 端到端断言都靠它）
    expect(group.getAttribute('data-tool-count')).toBe('3')
    expect(group.getAttribute('data-tool-failed')).toBe('2')

    const header = screen.getByRole('button', { name: /运行了 3 个工具/ })
    expect(header.textContent).toContain('运行了 3 个工具')
    // ★ 失败数必须出现在**收起态**的那一行上 —— 藏进展开层等于没提示
    expect(header.textContent).toContain('2 个失败')
    // 工具名是去重后的清单，不是"第几条消息"
    expect(header.textContent).toContain('list_scans')
    expect(header.textContent).toContain('run_shell')
    // 没有工具名的消息回落到 `tool`，而不是渲染成空字符串
    expect(header.textContent).toContain('tool')
  })

  it('★ 参数里没有可摘要的字段：头行只有工具名，不拼出 undefined / 无关参数', () => {
    const { container } = render(
      <ToolGroup items={[tool('t9', { toolName: 'wait_agent', toolArgs: { timeout: 30 } })]} />,
    )
    fireEvent.click(screen.getByRole('button', { name: /运行了 1 个工具/ }))

    const toggle = container.querySelector('[data-tool-toggle="true"]') as HTMLElement
    expect(toggle.textContent).toContain('wait_agent')
    expect(toggle.textContent).toContain('1 行')
    expect(toggle.textContent).not.toContain('undefined')
    // `timeout` 不在摘要白名单里，不该被随手拼进头行
    expect(toggle.textContent).not.toContain('30')
  })
})

describe('ToolGroup — 展开 / 收起', () => {
  it('★ 初始收起：aria-expanded=false，明细一条都不在 DOM 里', () => {
    const { container } = render(<ToolGroup items={threeTools()} />)
    const header = screen.getByRole('button', { name: /运行了 3 个工具/ })

    expect(header.getAttribute('aria-expanded')).toBe('false')
    expect(container.querySelectorAll('[data-message-row="tool"]').length).toBe(0)
  })

  it('★ 点开后 3 条明细逐条可再展开（第二条文案从「N 行」变「收起」）', () => {
    const { container } = render(<ToolGroup items={threeTools()} />)
    const header = screen.getByRole('button', { name: /运行了 3 个工具/ })

    fireEvent.click(header)

    expect(header.getAttribute('aria-expanded')).toBe('true')
    const rows = container.querySelectorAll('[data-message-row="tool"]')
    expect(rows.length).toBe(3)

    const toggles = container.querySelectorAll<HTMLElement>('[data-tool-toggle="true"]')
    expect(toggles.length).toBe(3)
    // 每条自己还有一层：收起时显示行数，点开后改成「收起」
    expect(toggles[0].textContent).toContain('1 行')
    fireEvent.click(toggles[0])
    expect(toggles[0].textContent).toContain('收起')
    // 只展开被点的那一条，别的仍然收着
    expect(toggles[1].textContent).toContain('1 行')
  })

  it('★ 展开后的列表限高 168px 且可滚动（一组十几条不能把结论顶出屏幕）', () => {
    const { container } = render(<ToolGroup items={threeTools()} />)
    fireEvent.click(screen.getByRole('button', { name: /运行了 3 个工具/ }))

    const group = groupOf(container)
    const list = Array.from(group.children).find(
      (child) => child.tagName === 'DIV',
    ) as HTMLElement | undefined
    expect(list, '展开后没有出现明细容器').toBeDefined()
    expect(list!.className).toContain('max-h-[168px]')
    expect(list!.className).toContain('overflow-y-auto')
  })
})

describe('ToolGroup — 跑着的时候', () => {
  it('★ live 默认展开，摘要行说「正在运行」而不是「运行了」', () => {
    const { container } = render(<ToolGroup items={threeTools()} live />)
    const header = screen.getByRole('button', { name: /正在运行 3 个工具/ })

    expect(header.getAttribute('aria-expanded')).toBe('true')
    expect(header.textContent).toContain('正在运行 3 个工具')
    expect(container.querySelectorAll('[data-message-row="tool"]').length).toBe(3)
  })

  it('★ 用户收起过之后，回合结束不再自动展开', () => {
    // 跑着的时候默认展开，用户点一下收起（不想被工具输出占屏）→ 回合结束。
    // 那次收起是用户的决定，不能被 live 的变化覆盖。
    const { container, rerender } = render(<ToolGroup items={threeTools()} live />)
    const header = screen.getByRole('button', { name: /正在运行 3 个工具/ })

    fireEvent.click(header)
    expect(header.getAttribute('aria-expanded')).toBe('false')

    rerender(<ToolGroup items={threeTools()} live={false} />)

    const after = screen.getByRole('button', { name: /运行了 3 个工具/ })
    expect(after.getAttribute('aria-expanded')).toBe('false')
    expect(container.querySelectorAll('[data-message-row="tool"]').length).toBe(0)
  })
})
