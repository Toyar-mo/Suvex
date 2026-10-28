// ⚠️ 必须手动 import `cleanup`：本仓库没开 vitest 的 `globals: true`。
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ChatSession } from '@/data/chat'

import { SessionBatchModal } from './SessionBatchModal'

/*
  ══════════════════════════════════════════════════════════════
  批量管理：判据是「勾得动、移得到、移完不留残影」
  ══════════════════════════════════════════════════════════════

  v1.20 补的三件事，每件都对应一个真实会踩的坑：

  ① **分组筛选**。条目一多，"按标题搜"不够——用户常常只知道"在客户 A
     那个组里"，不知道标题叫什么。
  ② **新建分组**。原来只能选已有分组，而分组清单是从会话推导的：
     一个分组都没有的时候，下拉里只有「无分组」，选了等于没选。
  ③ **应用后清空勾选**。留着勾选会让用户以为没生效，再点一次就是
     对同一批的重复网络往返（幂等但白跑）。
*/

afterEach(() => cleanup())

function session(over: Partial<ChatSession> & { id: string }): ChatSession {
  return {
    title: over.id,
    createdBy: 'u1',
    createdAt: '2026-10-28T00:00:00Z',
    updatedAt: '2026-10-28T00:00:00Z',
    messageCount: 1,
    archived: false,
    pinned: false,
    project: null,
    ...over,
  } as ChatSession
}

const SESSIONS = [
  session({ id: 'chat_1', title: '客户 A 的排查', project: '客户 A' }),
  session({ id: 'chat_2', title: '客户 B 的排查', project: '客户 B' }),
  session({ id: 'chat_3', title: '随手问的' }),
]

function renderModal(over: Partial<Parameters<typeof SessionBatchModal>[0]> = {}) {
  const props = {
    open: true,
    sessions: SESSIONS,
    projects: ['客户 A', '客户 B'],
    busy: false,
    onClose: vi.fn(),
    onMoveTo: vi.fn(async () => {}),
    onCreateGroup: vi.fn(async () => {}),
    onArchive: vi.fn(async () => 0),
    ...over,
  }
  render(<SessionBatchModal {...props} />)
  return props
}

/** 勾一条（复选框的可见标签是标题）。 */
function pick(title: string) {
  // 复选框的可访问名是 `选择「标题」`（见 SessionBatchModal 里 Checkbox 的 label）
  const box = screen.getByLabelText(`选择「${title}」`) as HTMLInputElement
  fireEvent.click(box)
}

/** 底部「移动到」下拉（label 的 htmlFor 指向它）。 */
function moveSelect() {
  return screen.getByLabelText('移动到')
}

describe('SessionBatchModal', () => {
  it('open 为 false 时不渲染', () => {
    renderModal({ open: false })
    expect(screen.queryByText('批量管理')).toBeNull()
  })

  it('按分组筛选：只剩该组的条目', () => {
    renderModal()
    expect(screen.getByText('客户 B 的排查')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('按分组筛选'), { target: { value: '客户 A' } })
    expect(screen.getByText('客户 A 的排查')).toBeTruthy()
    expect(screen.queryByText('客户 B 的排查')).toBeNull()
    expect(screen.queryByText('随手问的')).toBeNull()
  })

  it('没勾任何条目时「应用」禁用（避免对空集发请求）', () => {
    renderModal()
    fireEvent.change(moveSelect(), { target: { value: '客户 A' } })
    expect((screen.getByText('应用') as HTMLButtonElement).disabled).toBe(true)
  })

  it('选已有分组 + 应用：把勾选的 id 交出去', async () => {
    const props = renderModal()
    pick('客户 B 的排查')
    fireEvent.change(moveSelect(), { target: { value: '客户 A' } })
    fireEvent.click(screen.getByText('应用'))
    await vi.waitFor(() => expect(props.onMoveTo).toHaveBeenCalledWith(['chat_2'], '客户 A'))
    expect(props.onCreateGroup).not.toHaveBeenCalled()
  })

  it('应用成功后清空勾选（否则用户以为没生效、再点一次重跑）', async () => {
    const props = renderModal()
    pick('客户 B 的排查')
    fireEvent.change(moveSelect(), { target: { value: '客户 A' } })
    fireEvent.click(screen.getByText('应用'))
    await vi.waitFor(() =>
      expect((screen.getByLabelText('选择「客户 B 的排查」') as HTMLInputElement).checked).toBe(false),
    )
    expect(props.onMoveTo).toHaveBeenCalledTimes(1)
  })

  it('「＋ 新建分组…」：输入名字后走 onCreateGroup 而不是 onMoveTo', async () => {
    const props = renderModal()
    pick('客户 B 的排查')
    pick('随手问的')
    fireEvent.change(moveSelect(), { target: { value: '__new__' } })
    fireEvent.change(screen.getByLabelText('新分组名字'), { target: { value: 'HW2026' } })
    fireEvent.click(screen.getByText('应用'))
    await vi.waitFor(() =>
      expect(props.onCreateGroup).toHaveBeenCalledWith(['chat_2', 'chat_3'], 'HW2026'),
    )
    expect(props.onMoveTo).not.toHaveBeenCalled()
  })

  it('新建分组时名字为空：不发请求，就地报错', () => {
    const props = renderModal()
    pick('客户 B 的排查')
    fireEvent.change(moveSelect(), { target: { value: '__new__' } })
    fireEvent.click(screen.getByText('应用'))
    expect(props.onCreateGroup).not.toHaveBeenCalled()
    expect(screen.getByText('名字不能为空')).toBeTruthy()
  })

  it('新建分组重名：不发请求，并指向已有分组', () => {
    const props = renderModal()
    pick('客户 B 的排查')
    fireEvent.change(moveSelect(), { target: { value: '__new__' } })
    fireEvent.change(screen.getByLabelText('新分组名字'), { target: { value: '客户 A' } })
    fireEvent.click(screen.getByText('应用'))
    expect(props.onCreateGroup).not.toHaveBeenCalled()
    expect(screen.getByText(/已存在，直接选它即可/)).toBeTruthy()
  })

  it('切换下拉会清掉上一次的「新分组」报错（否则换个组还挂着红字）', () => {
    renderModal()
    pick('客户 B 的排查')
    fireEvent.change(moveSelect(), { target: { value: '__new__' } })
    fireEvent.click(screen.getByText('应用'))
    expect(screen.getByText('名字不能为空')).toBeTruthy()
    fireEvent.change(moveSelect(), { target: { value: '客户 A' } })
    expect(screen.queryByText('名字不能为空')).toBeNull()
  })

  it('全选只影响当前筛选结果（不能"看着 2 条却选了 3 条"）', () => {
    renderModal()
    fireEvent.change(screen.getByLabelText('按分组筛选'), { target: { value: '客户 A' } })
    const selectAll = screen.getByLabelText('全选') as HTMLInputElement
    fireEvent.click(selectAll)
    fireEvent.change(screen.getByLabelText('按分组筛选'), { target: { value: '' } })
    // 回到全部分组：只有原本在「客户 A」里的那条是选中的
    expect((screen.getByLabelText('选择「客户 A 的排查」') as HTMLInputElement).checked).toBe(true)
    expect((screen.getByLabelText('选择「客户 B 的排查」') as HTMLInputElement).checked).toBe(false)
  })
})
