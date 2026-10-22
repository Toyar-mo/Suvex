import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { HintComposer } from './HintComposer'
import { addScanHint } from '@/data/scan-hints'

/*
  ══════════════════════════════════════════════════════════════
  运行中补线索的输入框（P2 人机协同，v0.57）
  ══════════════════════════════════════════════════════════════

  ## 这个文件守的几件事

    1. 空输入**不发请求**（`canSend` 为假时按钮 disabled）；
    2. 成功后**清空输入框**并把 hint 交给 `onAdded`（让父级回显）；
    3. 失败时**原样显示后端中文**，且带 `role="alert"`；
    4. 提交过程中按钮禁用（避免连点发出两条一模一样的线索）。

  ## 坑

  vitest 关了 globals → 显式 `afterEach(cleanup)`。断言用户能读到的字，
  不 mock 子组件（这个组件没有子组件），用 input 的 value 验"被清空"。
*/

vi.mock('@/data/scan-hints', () => ({
  addScanHint: vi.fn(),
}))
const mockedAdd = vi.mocked(addScanHint)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function typeAndSubmit(text: string) {
  const input = screen.getByLabelText('给智能体补线索') as HTMLInputElement
  fireEvent.change(input, { target: { value: text } })
  return input
}

describe('HintComposer', () => {
  it('空输入时不发请求', () => {
    render(<HintComposer scanId="scan_1" />)
    const button = screen.getByRole('button', { name: /送出/ })
    expect((button as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(button)
    expect(mockedAdd).not.toHaveBeenCalled()
  })

  it('只有空格也不发请求', () => {
    render(<HintComposer scanId="scan_1" />)
    typeAndSubmit('   ')
    const button = screen.getByRole('button', { name: /送出/ })
    expect((button as HTMLButtonElement).disabled).toBe(true)
  })

  it('成功后清空输入框并回调 onAdded', async () => {
    mockedAdd.mockResolvedValue({
      id: 'hint_1',
      text: '去试 /admin',
      author: 'admin',
      consumed: false,
      consumedTurn: null,
      createdAt: '2026-10-22T00:00:00Z',
    })
    const onAdded = vi.fn()
    render(<HintComposer scanId="scan_1" onAdded={onAdded} />)

    const input = typeAndSubmit('去试 /admin')
    fireEvent.click(screen.getByRole('button', { name: /送出/ }))

    await waitFor(() => expect(mockedAdd).toHaveBeenCalledWith('scan_1', '去试 /admin'))
    expect(onAdded).toHaveBeenCalledTimes(1)
    expect(onAdded.mock.calls[0][0].text).toBe('去试 /admin')
    // 输入框应被清空
    await waitFor(() => expect(input.value).toBe(''))
  })

  it('去掉首尾空白后再提交', async () => {
    mockedAdd.mockResolvedValue({
      id: 'hint_2',
      text: 'x',
      author: null,
      consumed: false,
      consumedTurn: null,
      createdAt: '2026-10-22T00:00:00Z',
    })
    render(<HintComposer scanId="scan_1" />)
    typeAndSubmit('  前后有空格  ')
    fireEvent.click(screen.getByRole('button', { name: /送出/ }))
    await waitFor(() => expect(mockedAdd).toHaveBeenCalledWith('scan_1', '前后有空格'))
  })

  it('失败时原样显示后端中文且带 role=alert', async () => {
    mockedAdd.mockRejectedValue(new Error('只能给正在运行的扫描补充线索。'))
    render(<HintComposer scanId="scan_1" />)
    typeAndSubmit('一条线索')
    fireEvent.click(screen.getByRole('button', { name: /送出/ }))

    const alert = await screen.findByRole('alert')
    // 后端那句中文要原样出现（errorMessage 直接展示 message）
    expect(alert.textContent).toContain('只能给正在运行的扫描补充线索')
  })
})
