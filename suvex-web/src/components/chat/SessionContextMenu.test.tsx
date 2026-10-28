// ⚠️ 必须手动 import `cleanup`：本仓库没开 vitest 的 `globals: true`。
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SessionContextMenu, type SessionContextMenuProps } from './SessionContextMenu'

/*
  ══════════════════════════════════════════════════════════════
  会话右键菜单：判据是「用户点得到、也甩得掉」
  ══════════════════════════════════════════════════════════════

  菜单类控件的失败模式全在交互上，渲染出来是"对的"但用不了：

  ① **已归档的会话必须换一套项**（恢复 / 彻底删除），否则"恢复"根本没有
     入口 —— 归档就成了单向黑洞；
  ② 点了某一项必须**执行并把菜单关掉**，否则菜单留在屏幕上挡着；
  ③ 点空白处 / 按 Esc 必须关 —— 否则用户觉得"菜单甩不掉"；
  ④ 置顶项的文案必须随状态变（置顶 ↔ 取消置顶），否则用户不知道现在是什么状态。
*/

function makeProps(over: Partial<SessionContextMenuProps> = {}): SessionContextMenuProps {
  return {
    anchor: { x: 40, y: 60, sessionId: 'chat_1' },
    archived: false,
    pinned: false,
    projects: ['项目甲'],
    currentProject: null,
    onRename: vi.fn(),
    onTogglePin: vi.fn(),
    onDownload: vi.fn(),
    onMoveTo: vi.fn(),
    onBatchManage: vi.fn(),
    onArchive: vi.fn(),
    onUnarchive: vi.fn(),
    onPurge: vi.fn(),
    onClose: vi.fn(),
    ...over,
  }
}

afterEach(() => cleanup())

describe('SessionContextMenu', () => {
  it('anchor 为 null 时不渲染任何东西', () => {
    const props = makeProps({ anchor: null })
    const { container } = render(<SessionContextMenu {...props} />)
    expect(container.querySelector('[data-session-context-menu]')).toBeNull()
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('普通会话给出完整的操作项', () => {
    render(<SessionContextMenu {...makeProps()} />)
    for (const label of ['下载 Markdown', '重命名', '置顶此对话', '批量管理', '移动到分组', '删除此对话']) {
      expect(screen.getByText(label)).toBeTruthy()
    }
    // 普通会话不该出现"恢复"或"彻底删除"
    expect(screen.queryByText('恢复此对话')).toBeNull()
    expect(screen.queryByText('彻底删除')).toBeNull()
  })

  it('已归档的会话换成恢复 / 彻底删除（否则归档就是单向黑洞）', () => {
    render(<SessionContextMenu {...makeProps({ archived: true })} />)
    expect(screen.getByText('恢复此对话')).toBeTruthy()
    expect(screen.getByText('彻底删除')).toBeTruthy()
    // 这些对已归档的会话没有意义
    expect(screen.queryByText('置顶此对话')).toBeNull()
    expect(screen.queryByText('删除此对话')).toBeNull()
  })

  it('置顶状态改变文案（用户要能看出现在是什么状态）', () => {
    render(<SessionContextMenu {...makeProps({ pinned: true })} />)
    expect(screen.getByText('取消置顶')).toBeTruthy()
    expect(screen.queryByText('置顶此对话')).toBeNull()
  })

  it('点一项 → 执行回调并关闭菜单（不能留在屏幕上挡着）', () => {
    const props = makeProps()
    render(<SessionContextMenu {...props} />)
    fireEvent.click(screen.getByText('重命名'))
    expect(props.onRename).toHaveBeenCalledTimes(1)
    expect(props.onClose).toHaveBeenCalledTimes(1)
  })

  it('点危险项 → 也只"请求删除"，菜单照样关；最终删除由确认框再问一次', () => {
    const props = makeProps()
    render(<SessionContextMenu {...props} />)
    fireEvent.click(screen.getByText('删除此对话'))
    expect(props.onArchive).toHaveBeenCalledTimes(1)
    expect(props.onClose).toHaveBeenCalledTimes(1)
  })

  it('有子菜单的项：点它只展开，不执行也不关闭', () => {
    const props = makeProps()
    render(<SessionContextMenu {...props} />)

    fireEvent.click(screen.getByText('下载 Markdown'))
    expect(props.onDownload).not.toHaveBeenCalled()
    expect(props.onClose).not.toHaveBeenCalled()

    // 展开后可选「完整」
    fireEvent.click(screen.getByText('完整（含工具详情）'))
    expect(props.onDownload).toHaveBeenCalledWith('full')
    expect(props.onClose).toHaveBeenCalledTimes(1)
  })

  it('「移动到分组」子菜单列出已有分组 + 无分组，并标出当前所在组', () => {
    const props = makeProps({ projects: ['项目甲'], currentProject: '项目甲' })
    render(<SessionContextMenu {...props} />)

    fireEvent.click(screen.getByText('移动到分组'))
    // 当前所在的分组要打勾，否则"移动"这个动作没有反馈
    expect(screen.getByText('✓ 项目甲')).toBeTruthy()
    expect(screen.getByText('无分组')).toBeTruthy()

    fireEvent.click(screen.getByText('无分组'))
    expect(props.onMoveTo).toHaveBeenCalledWith(null)
  })

  it('按 Escape 关闭', () => {
    const props = makeProps()
    render(<SessionContextMenu {...props} />)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(props.onClose).toHaveBeenCalledTimes(1)
  })

  it('在菜单外按下指针 → 关闭；在菜单内按下 → 不关', () => {
    const props = makeProps()
    render(<SessionContextMenu {...props} />)

    const menu = document.querySelector('[data-session-context-menu]') as HTMLElement
    fireEvent.pointerDown(menu)
    expect(props.onClose).not.toHaveBeenCalled()

    fireEvent.pointerDown(document.body)
    expect(props.onClose).toHaveBeenCalledTimes(1)
  })
})
