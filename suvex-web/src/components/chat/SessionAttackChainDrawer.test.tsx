import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ChatMessage } from '@/data/chat'
import { SessionAttackChainDrawer } from './SessionAttackChainDrawer'

/*
  ══════════════════════════════════════════════════════════════
  「查看攻击链」抽屉：判据是「三种状态别混为一谈」
  ══════════════════════════════════════════════════════════════

  这个抽屉的失败模式不是"渲染错了"，而是**把不同状态说成同一句话**：

  ① `loading` 不能落成"这个对话还没有内容" —— 用户会以为记录丢了；
  ② `error` 必须明说失败并让用户能重试/关掉，不能显示成空态；
  ③ 真正没内容才是空态；
  ④ 失败的工具步骤必须标出来（这是攻击链里最有价值的信息）。

  `Drawer` 关闭时不该留下任何节点（否则 Esc 之后还能被 tab 到）。
*/

let seq = 0
function msg(over: Partial<ChatMessage>): ChatMessage {
  seq += 1
  return {
    id: `m_${seq}`,
    seq,
    role: 'user',
    content: '',
    createdAt: '2026-10-27T00:00:00Z',
    ...over,
  }
}

function renderDrawer(over: Partial<React.ComponentProps<typeof SessionAttackChainDrawer>> = {}) {
  const props = {
    open: true,
    onClose: vi.fn(),
    sessionTitle: '测试会话',
    messages: [] as ChatMessage[],
    ...over,
  }
  return { props, ...render(<SessionAttackChainDrawer {...props} />) }
}

// ⚠️ Drawer 用 `createPortal` 渲染到 document.body，所以断言一律查 `document`，
// 而不是 render 返回的 container（那里永远是空的）。
afterEach(() => cleanup())

describe('SessionAttackChainDrawer', () => {
  it('关闭时不渲染内容（Esc 之后不能被键盘 focus 到）', () => {
    renderDrawer({ open: false })
    expect(document.querySelector('[data-attack-chain]')).toBeNull()
    expect(screen.queryByText('攻击链')).toBeNull()
  })

  it('loading 时只说"正在取消息"，不冒充空态', () => {
    renderDrawer({ loading: true })
    expect(document.querySelector('[data-chain-loading]')).toBeTruthy()
    expect(screen.queryByText(/还没有内容/)).toBeNull()
    expect(document.querySelector('[data-attack-chain]')).toBeNull()
  })

  it('error 时把失败原因原样显示出来', () => {
    renderDrawer({ error: '网络超时' })
    expect(document.querySelector('[data-chain-error]')).toBeTruthy()
    expect(screen.getByText(/网络超时/)).toBeTruthy()
  })

  it('真的没消息才显示空态', () => {
    renderDrawer({ messages: [] })
    expect(screen.getByText(/这个对话还没有内容/)).toBeTruthy()
  })

  it('按时间顺序渲染每一步，并标出失败的工具步骤', () => {
    seq = 0
    renderDrawer({
      messages: [
        msg({ role: 'user', content: '扫一下' }),
        msg({ role: 'tool', content: '失败：没有这个命令', toolName: 'run_shell', toolFailed: true }),
        msg({ role: 'assistant', content: '抱歉，工具用错了。' }),
      ],
    })
    const steps = document.querySelectorAll('[data-step-kind]')
    expect(Array.from(steps).map((el) => el.getAttribute('data-step-kind'))).toEqual([
      'ask',
      'tool',
      'answer',
    ])
    const failed = document.querySelector('[data-step-failed="true"]')
    expect(failed).toBeTruthy()
    expect(failed?.textContent).toContain('失败')
  })

  it('标题栏给出提问数 / 工具数 / 失败数摘要', () => {
    seq = 0
    renderDrawer({
      messages: [
        msg({ role: 'user', content: '一' }),
        msg({ role: 'tool', content: 'a', toolName: 'run_shell' }),
        msg({ role: 'tool', content: 'b', toolName: 'browser', toolFailed: true }),
      ],
    })
    expect(screen.getByText(/1 问 · 2 次工具 · 1 次失败/)).toBeTruthy()
  })

  it('点关闭按钮会调用 onClose', () => {
    const { props } = renderDrawer({ messages: [msg({ role: 'user', content: 'x' })] })
    // ⚠️ 精确匹配 "关闭"：遮罩那个按钮的可访问名是 "关闭详情"，用正则会把两个都选中。
    const close = screen.getByRole('button', { name: '关闭' })
    fireEvent.click(close)
    expect(props.onClose).toHaveBeenCalled()
  })
})
