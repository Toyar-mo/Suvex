// ⚠️ 必须手动 import `cleanup`：本仓库没开 vitest 的 `globals: true`。
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ProviderSelect } from './ChatControls'

/*
  ══════════════════════════════════════════════════════════════
  模型接入点下拉：判据是「长得和旁边那两个下拉是一套」
  ══════════════════════════════════════════════════════════════

  v0.94 这里是原生 `<select>`。它的**展开层由操作系统画**，
  `appearance-none` 只管收起状态那个框，管不了弹出来的列表 ——
  于是在「技能」「MCP」两个自绘浮层旁边，弹出的是系统蓝色选中条。
  用户的反馈是"这个 ui 一点割裂"。

  所以这里的断言全部围绕"它是不是我们自己画的浮层"：
  ① 展开后必须出现 `role="listbox"`（原生 select 没有这个节点，
     它走的是浏览器进程里的弹层，DOM 里根本查不到）；
  ② 选项是 `role="option"`，可访问名就是接点名；
  ③ 点一条要**回调并关闭**（单选，不是勾选）；
  ④ 「平台默认模型」必须能选回去（否则选了具体模型就退不回默认）；
  ⑤ 收起状态显示的是当前选中项的名字，不是 id。
*/

function renderSelect(over: Partial<Parameters<typeof ProviderSelect>[0]> = {}) {
  const onChange = vi.fn()
  const props = {
    providers: [
      { id: 'p-ds', label: 'deepseek-v4.1-flash' },
      { id: 'p-qwen', label: 'qwen' },
    ],
    value: null as string | null,
    onChange,
    ...over,
  }
  const utils = render(<ProviderSelect {...props} />)
  return { ...utils, onChange }
}

afterEach(cleanup)

describe('ProviderSelect', () => {
  it('收起时显示「平台默认模型」，不显示 id', () => {
    renderSelect({ value: null })
    expect(screen.getByRole('button', { name: /平台默认模型/ })).toBeTruthy()
  })

  it('收起时显示当前选中的接点名', () => {
    renderSelect({ value: 'p-qwen' })
    expect(screen.getByRole('button', { name: /qwen/ })).toBeTruthy()
  })

  it('展开后是我们自己的 listbox，不是原生 select', () => {
    renderSelect()
    fireEvent.click(screen.getByRole('button', { name: /平台默认模型/ }))
    // 原生 <select> 在 DOM 里只有一个 SELECT 节点，不会有 listbox/option。
    expect(screen.getByRole('listbox')).toBeTruthy()
    expect(screen.getAllByRole('option').length).toBe(3) // 平台默认 + 两个接点
    expect(document.querySelector('select')).toBeNull()
  })

  it('点一个接点会回调它的 id，并关闭浮层', () => {
    const { onChange } = renderSelect()
    fireEvent.click(screen.getByRole('button', { name: /平台默认模型/ }))
    fireEvent.click(screen.getByRole('option', { name: 'qwen' }))
    expect(onChange).toHaveBeenCalledWith('p-qwen')
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('选回「平台默认模型」传的是 null，不是空串', () => {
    // 后端把 `null` 当"用平台默认"，`""` 会被当成一个不存在的接点 id。
    const { onChange } = renderSelect({ value: 'p-qwen' })
    fireEvent.click(screen.getByRole('button', { name: /qwen/ }))
    fireEvent.click(screen.getByRole('option', { name: '平台默认模型' }))
    expect(onChange).toHaveBeenCalledWith(null)
  })

  it('Esc 关掉浮层', () => {
    renderSelect()
    fireEvent.click(screen.getByRole('button', { name: /平台默认模型/ }))
    expect(screen.getByRole('listbox')).toBeTruthy()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('点浮层外面关掉', () => {
    renderSelect()
    fireEvent.click(screen.getByRole('button', { name: /平台默认模型/ }))
    expect(screen.getByRole('listbox')).toBeTruthy()
    fireEvent.pointerDown(document.body)
    expect(screen.queryByRole('listbox')).toBeNull()
  })
})
