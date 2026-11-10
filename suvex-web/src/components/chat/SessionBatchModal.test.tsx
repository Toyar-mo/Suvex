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
    onArchive: vi.fn(async () => [] as string[]),
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

/** 打开下拉并点选一项（自绘 Select，不再支持 fireEvent.change）。 */
function choose(label: string, option: string) {
  fireEvent.click(screen.getByLabelText(label))
  fireEvent.click(screen.getByRole('option', { name: option }))
}

describe('SessionBatchModal', () => {
  it('open 为 false 时不渲染', () => {
    renderModal({ open: false })
    expect(screen.queryByText('批量管理')).toBeNull()
  })

  it('按分组筛选：只剩该组的条目', () => {
    renderModal()
    expect(screen.getByText('客户 B 的排查')).toBeTruthy()
    choose('按分组筛选', '客户 A')
    expect(screen.getByText('客户 A 的排查')).toBeTruthy()
    expect(screen.queryByText('客户 B 的排查')).toBeNull()
    expect(screen.queryByText('随手问的')).toBeNull()
  })

  it('没勾任何条目时「应用」禁用（避免对空集发请求）', () => {
    renderModal()
    // 没勾条目时「移动到」本身也是禁用的，所以这里只看「应用」。
    expect((screen.getByText('应用') as HTMLButtonElement).disabled).toBe(true)
  })

  it('选已有分组 + 应用：把勾选的 id 交出去', async () => {
    const props = renderModal()
    pick('客户 B 的排查')
    choose('移动到', '客户 A')
    fireEvent.click(screen.getByText('应用'))
    await vi.waitFor(() => expect(props.onMoveTo).toHaveBeenCalledWith(['chat_2'], '客户 A'))
    expect(props.onCreateGroup).not.toHaveBeenCalled()
  })

  it('应用成功后清空勾选（否则用户以为没生效、再点一次重跑）', async () => {
    const props = renderModal()
    pick('客户 B 的排查')
    choose('移动到', '客户 A')
    fireEvent.click(screen.getByText('应用'))
    await vi.waitFor(() =>
      expect((screen.getByLabelText('选择「客户 B 的排查」') as HTMLInputElement).checked).toBe(
        false,
      ),
    )
    expect(props.onMoveTo).toHaveBeenCalledTimes(1)
  })

  it('「＋ 新建分组…」：输入名字后走 onCreateGroup 而不是 onMoveTo', async () => {
    const props = renderModal()
    pick('客户 B 的排查')
    pick('随手问的')
    choose('移动到', '＋ 新建分组…')
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
    choose('移动到', '＋ 新建分组…')
    fireEvent.click(screen.getByText('应用'))
    expect(props.onCreateGroup).not.toHaveBeenCalled()
    expect(screen.getByText('名字不能为空')).toBeTruthy()
  })

  it('新建分组重名：不发请求，并指向已有分组', () => {
    const props = renderModal()
    pick('客户 B 的排查')
    choose('移动到', '＋ 新建分组…')
    fireEvent.change(screen.getByLabelText('新分组名字'), { target: { value: '客户 A' } })
    fireEvent.click(screen.getByText('应用'))
    expect(props.onCreateGroup).not.toHaveBeenCalled()
    expect(screen.getByText(/已存在，直接选它即可/)).toBeTruthy()
  })

  it('切换下拉会清掉上一次的「新分组」报错（否则换个组还挂着红字）', () => {
    renderModal()
    pick('客户 B 的排查')
    choose('移动到', '＋ 新建分组…')
    fireEvent.click(screen.getByText('应用'))
    expect(screen.getByText('名字不能为空')).toBeTruthy()
    choose('移动到', '客户 A')
    expect(screen.queryByText('名字不能为空')).toBeNull()
  })

  /*
    ★ v1.79：选「无分组」必须真的筛出未分组的会话。

    原来直接把哨兵值 `__none__` 拿去和 `s.project` 比 —— 没有任何会话
    的项目叫这个名字，于是永远 0 条。用户会以为"我没有未分组的对话"。
  */
  it('★「无分组」筛选出的是**项目为空**的会话，而不是 0 条', () => {
    renderModal()
    choose('按分组筛选', '无分组')

    expect(screen.getByText('随手问的')).toBeTruthy()
    expect(screen.queryByText('客户 A 的排查')).toBeNull()
    expect(screen.queryByText('客户 B 的排查')).toBeNull()
  })

  /*
    ★ v1.79：批量删除失败要逐条留下。

    只报"3 条失败"时，用户不知道是哪 3 条 —— 下一步重试只能全选重来。
    现在失败的 id 留在选中里，并且明说"仍在选中里，可以直接重试"。
  */
  it('★ 批量删除部分失败：失败的留在选中里并说清楚', async () => {
    const props = renderModal({
      onArchive: vi.fn(async () => ['chat_2']),
    })
    pick('客户 A 的排查')
    pick('客户 B 的排查')
    fireEvent.click(screen.getByText('删除选中的 2 条'))

    await vi.waitFor(() => expect(screen.getByText(/已删除 1 条；1 条失败/)).toBeTruthy())
    expect(props.onArchive).toHaveBeenCalledWith(['chat_1', 'chat_2'])
    expect((screen.getByLabelText('选择「客户 B 的排查」') as HTMLInputElement).checked).toBe(true)
    expect((screen.getByLabelText('选择「客户 A 的排查」') as HTMLInputElement).checked).toBe(false)
  })

  it('批量删除全部成功时给出删除条数', async () => {
    renderModal()
    pick('随手问的')
    fireEvent.click(screen.getByText('删除选中的 1 条'))

    await vi.waitFor(() => expect(screen.getByText('已删除 1 条对话。')).toBeTruthy())
  })

  it('全选只影响当前筛选结果（不能"看着 2 条却选了 3 条"）', () => {
    renderModal()
    choose('按分组筛选', '客户 A')
    const selectAll = screen.getByLabelText('全选') as HTMLInputElement
    fireEvent.click(selectAll)
    choose('按分组筛选', '全部分组')
    // 回到全部分组：只有原本在「客户 A」里的那条是选中的
    expect((screen.getByLabelText('选择「客户 A 的排查」') as HTMLInputElement).checked).toBe(true)
    expect((screen.getByLabelText('选择「客户 B 的排查」') as HTMLInputElement).checked).toBe(false)
  })
})
