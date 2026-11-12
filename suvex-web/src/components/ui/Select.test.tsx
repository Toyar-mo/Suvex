import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Select } from './Select'

const options = [
  { value: 'a', label: 'Alpha' },
  { value: 'b', label: 'Beta' },
]

describe('Select', () => {
  it('renders the current label and opens the listbox', () => {
    render(<Select value="a" options={options} onChange={() => {}} />)
    expect(screen.getByRole('combobox')).toBeTruthy()
    fireEvent.click(screen.getByRole('combobox'))
    expect(screen.getByRole('listbox')).toBeTruthy()
    expect(screen.getByRole('option', { name: 'Beta' })).toBeTruthy()
  })

  it('selects an option by click and closes', () => {
    const onChange = vi.fn()
    render(<Select value="a" options={options} onChange={onChange} />)
    fireEvent.click(screen.getByRole('combobox'))
    fireEvent.click(screen.getByRole('option', { name: 'Beta' }))
    expect(onChange).toHaveBeenCalledWith('b')
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('opens with ArrowDown and selects with Enter', () => {
    const onChange = vi.fn()
    render(<Select value="a" options={options} onChange={onChange} />)
    const trigger = screen.getByRole('combobox')
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    fireEvent.keyDown(trigger, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('b')
  })

  it('closes with Escape', () => {
    render(<Select value="a" options={options} onChange={() => {}} />)
    fireEvent.click(screen.getByRole('combobox'))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('listbox')).toBeNull()
  })
})

it('选项很多时菜单自身限高可滚，滚动菜单不会把它关掉（v1.92）', async () => {
  const many = Array.from({ length: 40 }, (_, i) => ({ value: `v${i}`, label: `选项 ${i}` }))
  render(
    <Select
      value="v0"
      options={many}
      onChange={() => {}}
      ariaLabel="长列表"
    />,
  )
  fireEvent.click(screen.getByRole('combobox', { name: '长列表' }))
  const listbox = screen.getByRole('listbox')
  expect(listbox.style.maxHeight).toBe('256px')
  // 菜单内部滚动被 is-inside 判据忽略，不应关闭
  fireEvent.scroll(listbox)
  expect(screen.getByRole('listbox')).toBeTruthy()
})
