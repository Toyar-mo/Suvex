import { describe, expect, it } from 'vitest'

import { isThemePreference, resolveTheme } from './theme'

describe('主题偏好', () => {
  it('Tern 是合法的显式主题，不会被解析成 system', () => {
    expect(isThemePreference('tern')).toBe(true)
    expect(resolveTheme('tern')).toBe('tern')
  })
})
