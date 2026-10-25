import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import Modal from '@/components/ui/Modal'

/*
  ══════════════════════════════════════════════════════════════
  Modal：把 Drawer 那条「打一个字就失焦」的修法固定住
  ══════════════════════════════════════════════════════════════

  ## 为什么新组件要重抄一遍旧 bug 的测试

  Modal 是**新写的浮层**，而浮层里那条失焦 bug 已经在本项目真实发生过一次
  （Drawer，用户报「不能连续输入」）。它的成因不是"谁写错了"，而是
  React 的一个结构性陷阱：调用方写内联 `onClose` → 每次重渲染都是新引用
  → 若 effect 依赖它，则每敲一个字符就重跑一次"还焦点 / 移焦点"。

  ★ 所以这里刻意**再写一遍**同一个 harness，而不是"Drawer 测过了就不测了"。
    Drawer 的测试保护不了 Modal —— 它是另一个文件、另一份 effect。
    新写的浮层必须自己证明自己没踩这个坑。

  ## 断言必须是「真受控输入」

  harness 里 state 与 `onClose` 在**同一个组件**，且 `onClose` 是内联箭头
  函数 —— 这才是真实调用方的形状（SkillsPage 就是这样）。若传一个提到
  外部的 `vi.fn()`，即使依赖写错也照样通过（假绿）。
*/

function Harness() {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')

  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        打开
      </button>
      <Modal
        open={open}
        /* ★ 内联箭头函数，每次渲染都是新引用 —— 必须保持这样 */
        onClose={() => setOpen(false)}
        title="编辑"
      >
        <input
          aria-label="技能名"
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
        <textarea
          aria-label="正文"
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
        <span data-testid="mirror">{text}</span>
      </Modal>
    </>
  )
}

afterEach(cleanup)

describe('Modal · 受控输入不失焦', () => {
  it('连续输入三个字符后焦点仍在输入框，且值是完整的', () => {
    render(<Harness />)
    fireEvent.click(screen.getByText('打开'))

    const input = screen.getByLabelText('技能名') as HTMLInputElement
    input.focus()
    expect(document.activeElement).toBe(input)

    fireEvent.change(input, { target: { value: 'a' } })
    fireEvent.change(input, { target: { value: 'ab' } })
    fireEvent.change(input, { target: { value: 'abc' } })

    expect(screen.getByTestId('mirror').textContent).toBe('abc')
    expect(
      document.activeElement,
      '输入后焦点跑掉了 —— onClose 又被列进 effect 依赖了？',
    ).toBe(input)
  })

  it('在正文 textarea 里连续输入同样不失焦（多字段表单的真实形状）', () => {
    render(<Harness />)
    fireEvent.click(screen.getByText('打开'))

    const area = screen.getByLabelText('正文') as HTMLTextAreaElement
    area.focus()

    fireEvent.change(area, { target: { value: '## 标题' } })
    fireEvent.change(area, { target: { value: '## 标题\n第二行' } })

    expect(document.activeElement).toBe(area)
  })

  it('Escape 关闭', () => {
    const onClose = vi.fn()
    render(
      <Modal open onClose={onClose} title="x">
        <span>body</span>
      </Modal>,
    )
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('打开时锁 body 滚动，关闭后还原', () => {
    document.body.style.overflow = 'auto'
    render(<Harness />)
    fireEvent.click(screen.getByText('打开'))
    expect(document.body.style.overflow).toBe('hidden')

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(document.body.style.overflow).toBe('auto')
  })

  it('关闭时把焦点还给触发元素', () => {
    render(<Harness />)
    const trigger = screen.getByText('打开')
    trigger.focus()
    fireEvent.click(trigger)
    expect(document.activeElement).not.toBe(trigger)

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(document.activeElement).toBe(trigger)
  })

  it('标题出现在 dialog 上（读屏器能念出这是哪个弹窗）', () => {
    render(
      <Modal open onClose={vi.fn()} title="编辑 kerberos-preauth">
        <span>body</span>
      </Modal>,
    )
    const dialog = screen.getByRole('dialog')
    expect(dialog.getAttribute('aria-label')).toBe('编辑 kerberos-preauth')
  })

  it('默认不撑满高度 —— 内容短的弹窗不该在底部留半屏空白', () => {
    // ★ 这条守着 Modal.tsx 里那个被删掉的硬编码 `h-full`。
    //   它当时排在 heightClass 之后，于是**每一个**弹窗都被撑到 85vh：
    //   新建定时计划的最后一个字段到页脚之间有约 160px 空白，看起来像
    //   少渲染了一个字段。需要确定高度的调用方（技能编辑器）自己声明。
    render(
      <Modal open onClose={vi.fn()} title="短表单">
        <span>body</span>
      </Modal>,
    )
    const dialog = screen.getByRole('dialog')
    expect(dialog.className).not.toContain('h-full')
    // 高度上限仍在，只是不再是固定高度
    expect(dialog.className).toContain('max-h-[85vh]')
  })

  it('调用方给了高度类就用它，不被默认值覆盖', () => {
    render(
      <Modal open onClose={vi.fn()} title="两栏" heightClass="max-h-[88vh] h-[88vh]">
        <span>body</span>
      </Modal>,
    )
    const dialog = screen.getByRole('dialog')
    expect(dialog.className).toContain('h-[88vh]')
    expect(dialog.className).not.toContain('max-h-[85vh]')
  })
})
