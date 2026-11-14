import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { TerminalOutput } from './TerminalOutput'

describe('TerminalOutput', () => {
  it('JSON 的键、字符串和数字分别着色', () => {
    const { container } = render(
      <TerminalOutput text={'{"kind":"replay","status":200,"passed":true}'} />,
    )
    expect(container.querySelector('.text-syntax-property')?.textContent).toContain('kind')
    expect(container.querySelector('.text-syntax-string')?.textContent).toContain('replay')
    expect(container.querySelector('.text-syntax-number')?.textContent).toContain('200')
    expect(container.querySelector('.text-syntax-keyword')?.textContent).toContain('true')
  })

  it('终端 key=value 与方括号标签也着色', () => {
    const { container } = render(
      <TerminalOutput text={'[流水线 干净+XFF] bytes=6915 resp=2 msg="No!!!"'} />,
    )
    expect(container.querySelector('.text-accent')?.textContent).toContain('XFF')
    expect(container.querySelector('.text-syntax-property')?.textContent).toContain('bytes=')
    expect(container.querySelector('.text-syntax-string')?.textContent).toContain('No!!!')
  })
})
