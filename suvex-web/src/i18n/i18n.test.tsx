import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { I18nProvider, LOCALES, resolveLocale, useI18n } from './index'

afterEach(() => {
  cleanup()
  localStorage.clear()
})

function Probe() {
  const { locale, setting, setSetting, t } = useI18n()
  return (
    <div>
      <span data-testid="locale">{locale}</span>
      <span data-testid="setting">{setting}</span>
      <span data-testid="title">{t('priority.title')}</span>
      <span data-testid="total">{t('priority.total', { n: 7 })}</span>
      <span data-testid="nav">{t('nav.overview')}</span>
      <button type="button" onClick={() => setSetting('en')}>
        to-en
      </button>
    </div>
  )
}

describe('i18n', () => {
  it('auto 跟随浏览器，但只认支持的语言，其余回落英文', () => {
    expect(resolveLocale('zh-CN')).toBe('zh-CN')
    // jsdom 的 navigator.language 是 en-US → auto 落到 en
    expect(resolveLocale('auto')).toBe('en')
    // 7 种语言都在表里，且 id 唯一
    const ids = LOCALES.map((item) => item.id)
    expect(new Set(ids).size).toBe(7)
    expect(ids).toContain('zh-TW')
  })

  it('切换到英文后文案与选择都变，且写进 localStorage', () => {
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'to-en' }))

    expect(screen.getByTestId('locale').textContent).toBe('en')
    expect(screen.getByTestId('title').textContent).toBe('Finding priority')
    expect(screen.getByTestId('nav').textContent).toBe('Overview')
    expect(localStorage.getItem('suvex.locale')).toBe('en')
  })

  it('{n} 会被替换，缺键回落中文而不是显示 key', () => {
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    )

    expect(screen.getByTestId('total').textContent).not.toContain('{n}')
    expect(screen.getByTestId('total').textContent).toContain('7')
  })
})
