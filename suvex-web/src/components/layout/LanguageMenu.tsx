import { useEffect, useRef, useState } from 'react'
import { Check, Languages } from 'lucide-react'

import { cn } from '@/lib/cn'
import { LOCALES, useI18n, type LocaleSetting } from '@/i18n'

/**
 * 语言菜单（v1.90）。
 *
 * ## 为什么挂在顶栏右端
 *
 * 与主题开关同一类：**随时可点的控件**，不是系统状态。塞进左边那条状态带
 * 会被读成"第四段状态"，也会破坏那条按断点逐级减字的规则。
 *
 * ## 窄屏只显示图标
 *
 * 语言名长短差异巨大（`Français` vs `中文`），写出来会把状态带挤掉。
 * 图标 + 下拉里的全名，是这一行里唯一不会随文案变化的形态。
 */
export function LanguageMenu() {
  const { setting, setSetting, t } = useI18n()
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const current = LOCALES.find((item) => item.id === setting)

  return (
    <div ref={rootRef} className="relative shrink-0">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t('lang.label')}
        title={`${t('lang.label')}：${current ? current.name : t('lang.auto')}`}
        onClick={() => setOpen((value) => !value)}
        className="flex h-8 items-center gap-1.5 rounded-sm px-2 text-xs text-ink-muted transition-colors hover:bg-surface-sunken hover:text-ink"
        data-language-menu-trigger=""
      >
        <Languages size={15} strokeWidth={1.8} aria-hidden="true" />
        <span className="hidden md:inline">{current ? current.short : t('lang.auto')}</span>
      </button>

      {open && (
        <div
          role="menu"
          aria-label={t('lang.label')}
          className="absolute right-0 top-9 z-40 w-52 overflow-hidden rounded-md border border-line bg-surface py-1 shadow-lg"
          data-language-menu=""
        >
          {LOCALES.map((item) => {
            const active = setting === item.id
            return (
              <button
                key={item.id}
                type="button"
                role="menuitemradio"
                aria-checked={active}
                onClick={() => {
                  setSetting(item.id)
                  setOpen(false)
                }}
                className={cn(
                  'flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors',
                  active ? 'bg-accent/10 text-ink' : 'text-ink-muted hover:bg-surface-sunken',
                )}
              >
                <span aria-hidden="true">{item.flag}</span>
                <span className="flex-1">{item.name}</span>
                {active && <Check size={13} strokeWidth={2} aria-hidden="true" />}
              </button>
            )
          })}
          <div className="my-1 h-px bg-line" aria-hidden="true" />
          <button
            type="button"
            role="menuitemradio"
            aria-checked={setting === 'auto'}
            onClick={() => {
              setSetting('auto' satisfies LocaleSetting)
              setOpen(false)
            }}
            className={cn(
              'flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors',
              setting === 'auto' ? 'bg-accent/10 text-ink' : 'text-ink-muted hover:bg-surface-sunken',
            )}
          >
            <span aria-hidden="true">🌐</span>
            <span className="flex-1">{t('lang.auto')}</span>
            {setting === 'auto' && <Check size={13} strokeWidth={2} aria-hidden="true" />}
          </button>
        </div>
      )}
    </div>
  )
}
