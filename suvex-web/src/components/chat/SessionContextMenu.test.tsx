// ⚠️ 必须手动 import `cleanup`：本仓库没开 vitest 的 `globals: true`。
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
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
    onViewAttackChain: vi.fn(),
    onViewFindings: vi.fn(),
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
    for (const label of [
      '查看攻击链',
      '查看漏洞',
      '下载 Markdown',
      '重命名',
      '置顶此对话',
      '批量管理',
      '移动到分组',
      '删除此对话',
    ]) {
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

/*
  v1.16：两个只读洞察项（查看攻击链 / 查看漏洞）。

  为什么单独测：它们是**菜单里唯二不写数据的项**，而"只读"最容易出的错恰恰是
  点了没反应（handler 没接上）或点了不关菜单（用户以为没生效又点一次）。
  另外已归档的会话也必须能看 —— 归档只影响写操作，不该把复盘入口一起关掉。
*/
describe('SessionContextMenu · 只读洞察项', () => {
  it('点「查看攻击链」会调用对应 handler 并关掉菜单', () => {
    const props = makeProps()
    render(<SessionContextMenu {...props} />)
    fireEvent.click(screen.getByText('查看攻击链'))
    expect(props.onViewAttackChain).toHaveBeenCalledTimes(1)
    expect(props.onClose).toHaveBeenCalled()
  })

  it('点「查看漏洞」会调用对应 handler 并关掉菜单', () => {
    const props = makeProps()
    render(<SessionContextMenu {...props} />)
    fireEvent.click(screen.getByText('查看漏洞'))
    expect(props.onViewFindings).toHaveBeenCalledTimes(1)
    expect(props.onClose).toHaveBeenCalled()
  })

  it('已归档的会话同样能看到这两项（归档只关写操作）', () => {
    render(<SessionContextMenu {...makeProps({ archived: true })} />)
    expect(screen.getByText('查看攻击链')).toBeTruthy()
    expect(screen.getByText('查看漏洞')).toBeTruthy()
  })
})

/*
  ══════════════════════════════════════════════════════════════
  子菜单的「过桥」：鼠标移过去不能被收掉
  ══════════════════════════════════════════════════════════════

  真实鼠标从父项移到右侧子菜单，要跨过一小段距离，途中必然先触发父项的
  `mouseleave`。若立刻收起，用户看到的就是「鼠标根本放不上去就没了」——
  这是用户实际报过的现象。

  判据两条：
  ① 离开父项后立刻进入子菜单 → 宽限期过后子菜单**仍在**（过桥生效）；
  ② 离开父项且不进子菜单 → 宽限期过后子菜单**收起**（不能一直挂着）。
*/
describe('SessionContextMenu · 子菜单过桥', () => {
  /** 拿到「下载 Markdown」那一行（父项 div）与它的子菜单。 */
  function openDownloadSubmenu() {
    render(<SessionContextMenu {...makeProps()} />)
    const parent = screen.getByRole('menuitem', { name: /下载 Markdown/ })
    fireEvent.mouseEnter(parent.parentElement as HTMLElement)
    const submenu = screen.getByRole('menu', { name: '下载 Markdown' })
    return { parent, submenu }
  }

  it('子菜单与父项之间的空隙必须用 padding 撑出来（margin 是死区）', () => {
    /*
      ★ 这条是真正钉住「鼠标放不上去就没了」的那一条。

      用户报的现象：鼠标从父项移向右侧子菜单，还没到子菜单就整块消失了。
      根因是父项与子菜单之间用 `margin` 让开 4px —— margin 在盒子**外面**，
      那 4px 不属于任何元素，鼠标经过时 `mouseleave` 立刻触发收起。

      修法是把这段距离变成 `padding`（盒子**里面**），于是「空隙」始终落在
      子树的命中范围里，`mouseleave` 不会触发。

      jsdom 没有布局，量不出 4px；所以这里断言**结构契约**：留白必须是
      wrapper 的 padding 类，而不是子菜单面板的 margin —— 结构一变，这条红。
    */
    render(<SessionContextMenu {...makeProps()} />)
    const parent = screen.getByRole('menuitem', { name: /下载 Markdown/ })
    fireEvent.mouseEnter(parent.parentElement as HTMLElement)
    const submenu = screen.getByRole('menu', { name: '下载 Markdown' })

    const bridge = submenu.parentElement as HTMLElement
    expect(bridge.className.includes('pl-1') || bridge.className.includes('pr-1')).toBe(true)
    // 面板自己不能再靠 margin 让位（那又会变成死区）
    expect(submenu.style.marginLeft).toBe('')
    expect(submenu.style.marginRight).toBe('')
  })

  it('离开父项后立刻回到父项区域 → 不收起（宽限期会被撤销）', () => {
    vi.useFakeTimers()
    try {
      const { parent } = openDownloadSubmenu()

      fireEvent.mouseLeave(parent.parentElement as HTMLElement)
      // 鼠标抖动/回移：重新进入父项子树（子菜单也在这棵子树里）
      fireEvent.mouseEnter(parent.parentElement as HTMLElement)
      act(() => {
        vi.advanceTimersByTime(400)
      })

      expect(screen.getByRole('menu', { name: '下载 Markdown' })).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('离开父项且不进子菜单 → 宽限期后收起（不能一直挂着）', () => {
    vi.useFakeTimers()
    try {
      const { parent } = openDownloadSubmenu()
      fireEvent.mouseLeave(parent.parentElement as HTMLElement)

      // 宽限期内还在
      act(() => {
        vi.advanceTimersByTime(100)
      })
      expect(screen.queryByRole('menu', { name: '下载 Markdown' })).toBeTruthy()

      act(() => {
        vi.advanceTimersByTime(100)
      })
      expect(screen.queryByRole('menu', { name: '下载 Markdown' })).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })
})
