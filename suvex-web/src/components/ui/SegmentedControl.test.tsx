import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SegmentedControl } from './SegmentedControl'

afterEach(cleanup)

const OPTIONS = [
  { id: 'compose' as const, label: '生成报告' },
  { id: 'history' as const, label: '历史报告' },
]

describe('SegmentedControl', () => {
  it('选中项通过 aria-selected 表达，滑块跟着走', () => {
    const { container, rerender } = render(
      <SegmentedControl
        ariaLabel="报告视图"
        options={OPTIONS}
        value="compose"
        onChange={() => {}}
      />,
    )

    expect(screen.getByRole('tab', { name: '生成报告' }).getAttribute('aria-selected')).toBe('true')
    expect(screen.getByRole('tab', { name: '历史报告' }).getAttribute('aria-selected')).toBe('false')

    const slider = container.querySelector('[aria-hidden="true"]') as HTMLElement
    expect(slider.style.transform).toBe('translateX(0%)')

    rerender(
      <SegmentedControl
        ariaLabel="报告视图"
        options={OPTIONS}
        value="history"
        onChange={() => {}}
      />,
    )

    expect(slider.style.transform).toBe('translateX(100%)')
    expect(screen.getByRole('tab', { name: '历史报告' }).getAttribute('aria-selected')).toBe('true')
  })

  it('点击未选项只回调一次，值由调用方决定', () => {
    const onChange = vi.fn()
    render(
      <SegmentedControl
        ariaLabel="报告视图"
        options={OPTIONS}
        value="compose"
        onChange={onChange}
      />,
    )

    fireEvent.click(screen.getByRole('tab', { name: '历史报告' }))

    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith('history')
  })
})
