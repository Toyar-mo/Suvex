import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { DatePicker } from './DatePicker'

describe('DatePicker', () => {
  it('opens a painted calendar and selects a day', () => {
    const onChange = vi.fn()
    render(<DatePicker id="date" value="2026-11-10" onChange={onChange} ariaLabel="开始日期" />)
    expect(screen.getByLabelText('开始日期')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '打开日历' }))
    expect(screen.getByRole('dialog', { name: '选择日期' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '15' }))
    expect(onChange).toHaveBeenCalledWith('2026-11-15')
    expect(screen.queryByRole('dialog', { name: '选择日期' })).toBeNull()
  })

  it('closes with Escape', () => {
    render(<DatePicker id="date" value="2026-11-10" onChange={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: '打开日历' }))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: '选择日期' })).toBeNull()
  })

  it('disables days outside min/max', () => {
    render(
      <DatePicker id="date" value="2026-11-10" min="2026-11-10" max="2026-11-20" onChange={() => {}} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '打开日历' }))
    expect((screen.getByRole('button', { name: '5' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: '15' }) as HTMLButtonElement).disabled).toBe(false)
  })
})
