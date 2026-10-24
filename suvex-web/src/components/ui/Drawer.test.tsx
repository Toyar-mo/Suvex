import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import Drawer from '@/components/ui/Drawer'

/*
  ══════════════════════════════════════════════════════════════
  Drawer：受控输入时**不许**在每次按键后丢焦点（2026-10-25）
  ══════════════════════════════════════════════════════════════

  ## 这是什么 bug（用户报的「打一个字就失焦，不能连续输入」）

  抽屉里的受控输入每改一个字符，父组件就重渲染一次，于是 `onClose`
  （调用方几乎总是写内联箭头函数）拿到一个新引用。若 `useEffect`
  把它列进依赖，那么每次按键都会：

      清理函数 → 把焦点还给「打开抽屉的那个按钮」
      新 effect → 再把焦点移进面板

  用户看到的就是「敲一个字，光标跳走，得重新点输入框」。
  一个字都连不起来，但因为**外观完全正常**，截图看不出来。

  ## 为什么这条测试必须用「真受控输入」

  如果测试里传一个稳定的 `onClose`（`vi.fn()` 提到外面），
  effect 依赖写成 `[open, onClose]` 也照样通过 —— 那就测不到这个 bug。
  所以这里复刻真实用法：**父组件用内联箭头函数**，输入框受控。

  ## 断言什么

  输入三个字符之后，`document.activeElement` 必须仍是那个 input，
  且它的 `value` 是完整的三个字符（不是"只剩最后一个"）。
  后者能抓到另一类退化：值被重建但没有失焦。
*/

/*
  ★ 关键：state 和 onClose **在同一个组件里**，这才是真实形状。

  我第一版把 state 放在 Drawer 的子组件里，父组件不重渲染 →
  `onClose` 引用没变 → effect 不重跑 → 测试**假绿**（变异验证抓到了：
  把依赖改回 `[open, onClose]` 它照样 passed）。

  真实调用方（SkillsPage.tsx）就是：editor state 和
  `onClose={() => { setEditor(null) }}` 都在同一个函数组件里，
  所以每敲一个字符 → 该组件重渲染 → 新的 onClose → effect 重跑。
*/
function Harness() {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')

  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        打开
      </button>
      <Drawer
        open={open}
        /* ★ 内联箭头函数，每次渲染都是新引用 —— 必须保持这样 */
        onClose={() => setOpen(false)}
        title="新建"
      >
        <input
          aria-label="技能名"
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
        <span data-testid="mirror">{text}</span>
      </Drawer>
    </>
  )
}

afterEach(cleanup)

describe('Drawer · 受控输入不失焦', () => {
  it('连续输入三个字符后焦点仍在该输入框，且值是完整的', () => {
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

  it('Escape 仍然关闭（把 onClose 挪进 ref 不能把这条弄坏）', () => {
    const onClose = vi.fn()
    render(
      <Drawer open onClose={onClose} title="x">
        <span>body</span>
      </Drawer>,
    )

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('关闭时把焦点还给触发元素（无障碍那条不能因为修 bug 被丢掉）', () => {
    render(<Harness />)
    const trigger = screen.getByText('打开')
    trigger.focus()
    fireEvent.click(trigger)

    expect(document.activeElement).not.toBe(trigger)

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(document.activeElement).toBe(trigger)
  })
})
