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
