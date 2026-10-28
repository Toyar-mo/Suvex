// ⚠️ 必须手动 import `cleanup`：本仓库没开 vitest 的 `globals: true`。
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { GroupNameDialog } from './GroupNameDialog'

/*
  ══════════════════════════════════════════════════════════════
  新建分组弹窗：判据是「错的名字进不去、对的名字交得上去」
  ══════════════════════════════════════════════════════════════

  为什么这个弹窗值得单独测（它只有 3 条校验）：

  ① **空名字必须拦住**。后端 `PATCH /chat/sessions/{id}` 的 `project`
     是自由文本，空串不会报错、而是"移进了一个叫空字符串的分组" ——
     界面上表现为"点了新建，什么都没发生，也没报错"。这是最坏的一种。
  ② **重名必须拦住并说明**。分组清单是从会话里推导的，允许重名不会有
     数据冲突，但"新建"一个已经存在的组，用户的心智是"我应该直接移过去"，
     给他建一个同名的是误导。
  ③ **`onConfirm` 交出去的必须是 trim 过的**。前端不 trim 的话，
     " 项目甲" 和 "项目甲" 会变成两个分组，肉眼在侧栏里看不出来。

  另外两条交互细节：回车要能提交（中文输入法组合态下的回车不算），
  每次打开要重置（上一次输一半的名字不能带进来）。
*/

afterEach(() => cleanup())

function renderDialog(over: Partial<Parameters<typeof GroupNameDialog>[0]> = {}) {
  const props = {
    open: true,
    existing: ['项目甲'],
    busy: false,
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
    ...over,
  }
  const view = render(<GroupNameDialog {...props} />)
  return { ...props, view }
}

function typeName(value: string) {
  fireEvent.change(screen.getByLabelText('分组名字'), { target: { value } })
}

describe('GroupNameDialog', () => {
  it('open 为 false 时不渲染', () => {
    renderDialog({ open: false })
    expect(screen.queryByLabelText('分组名字')).toBeNull()
  })

  it('名字为空时点「新建并移动」：不提交，并说明原因', () => {
    const { onConfirm } = renderDialog()
    fireEvent.click(screen.getByText('新建并移动'))
    expect(onConfirm).not.toHaveBeenCalled()
    expect(screen.getByRole('alert').textContent).toContain('不能为空')
  })

  it('只输入空格也算空（trim 之后再判）', () => {
    const { onConfirm } = renderDialog()
    typeName('   ')
    fireEvent.click(screen.getByText('新建并移动'))
    expect(onConfirm).not.toHaveBeenCalled()
    expect(screen.getByRole('alert').textContent).toContain('不能为空')
  })

  it('与已有分组重名：不提交，并告诉用户"直接选那个组"', () => {
    const { onConfirm } = renderDialog()
    typeName('项目甲')
    fireEvent.click(screen.getByText('新建并移动'))
    expect(onConfirm).not.toHaveBeenCalled()
    expect(screen.getByRole('alert').textContent).toContain('已经是一个分组了')
  })

  it('超过 24 个字符：不提交，并报出当前字数', () => {
    const { onConfirm } = renderDialog()
    typeName('一'.repeat(25))
    fireEvent.click(screen.getByText('新建并移动'))
    expect(onConfirm).not.toHaveBeenCalled()
    expect(screen.getByRole('alert').textContent).toContain('25')
  })

  it('合法名字：提交的是 trim 后的值（前后空格不能带进分组名）', () => {
    const { onConfirm } = renderDialog()
    typeName('  客户 A  ')
    fireEvent.click(screen.getByText('新建并移动'))
    expect(onConfirm).toHaveBeenCalledWith('客户 A')
  })

  it('回车提交（不必去点按钮）', () => {
    const { onConfirm } = renderDialog()
    typeName('HW2026')
    fireEvent.keyDown(screen.getByLabelText('分组名字'), { key: 'Enter' })
    expect(onConfirm).toHaveBeenCalledWith('HW2026')
  })

  it('输入法组合态下的回车不提交（否则上屏候选词就把弹窗关了）', () => {
    const { onConfirm } = renderDialog()
    typeName('ceshi')
    fireEvent.keyDown(screen.getByLabelText('分组名字'), { key: 'Enter', isComposing: true })
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('busy 时按钮禁用并改文案（避免重复点出两次移动）', () => {
    renderDialog({ busy: true })
    const button = screen.getByText('正在移动…')
    expect(button).toBeTruthy()
    expect((button as HTMLButtonElement).disabled).toBe(true)
  })

  it('重新打开会清空上一次输的名字', () => {
    const { view, ...props } = renderDialog()
    typeName('输一半')
    act(() => {
      view.rerender(<GroupNameDialog {...props} open={false} />)
    })
    act(() => {
      view.rerender(<GroupNameDialog {...props} open />)
    })
    expect((screen.getByLabelText('分组名字') as HTMLInputElement).value).toBe('')
  })

  it('点「取消」走 onCancel，不提交', () => {
    const { onCancel, onConfirm } = renderDialog()
    typeName('客户 A')
    fireEvent.click(screen.getByText('取消'))
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onConfirm).not.toHaveBeenCalled()
  })
})
