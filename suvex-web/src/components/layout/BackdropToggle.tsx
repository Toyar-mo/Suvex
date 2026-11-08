import { Check, Layers } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { setBackdrop, useBackdrop } from '@/data/backdrop-store'
import { BACKDROPS } from '@/lib/backdrop'
import { cn } from '@/lib/cn'

/**
 * 背景（画布氛围）切换 —— 在右上角、主题开关左边。
 *
 * ## 为什么和主题开关并列，而不是塞进设置页
 *
 * "哪种底好看"是一个**看一眼才知道**的决定：要对比就得来回切。放到设置页
 * 意味着每次试一档都要走两步、还会离开正在看的那个页面 —— 而背景好不好看，
 * 恰恰要在真实页面上看才作数。
 *
 * ## 为什么带预览小样，而不是四个文字项
 *
 * 这四项的差别是**视觉**的，用文字描述（"点阵场 / 光晕"）等于让用户先背
 * 四个词再去猜。小样直接把"这档长什么样"摊开 —— 而它只有 28×18，不会
 * 抢走注意力。
 *
 * ## 交互沿用 `ThemeToggle`
 *
 * Esc / 点外部关闭、当前项打勾、焦点还给按钮 —— 同一个界面里两套菜单行为
 * 不一致，比少一种关闭方式更烦人。
 */
export function BackdropToggle() {
  const current = useBackdrop()
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      if (wrapRef.current?.contains(event.target as Node)) return
      setOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setOpen(false)
      buttonRef.current?.focus()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  const currentLabel = BACKDROPS.find((item) => item.id === current)?.label ?? '点阵场'

  return (
    <div ref={wrapRef} className="relative shrink-0">
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`背景：${currentLabel}，点击切换`}
        title={`背景：${currentLabel}`}
        className={cn(
          'flex h-7 items-center gap-1 rounded-sm px-1.5 text-2xs text-ink-faint',
          'max-sm:min-h-9 max-sm:min-w-9 max-sm:justify-center',
          'transition-ui duration-fast hover:bg-ink-ghost/10 hover:text-ink-muted',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
          open && 'bg-ink-ghost/10 text-ink-muted',
        )}
      >
        <Layers size={13} strokeWidth={1.75} aria-hidden="true" />
        <span className="hidden lg:inline">背景</span>
      </button>

      {open && (
        <div
          role="menu"
          aria-label="选择背景"
          className={cn(
            'absolute right-0 top-full z-30 mt-1 w-64 rounded-md border border-line bg-surface p-1',
            'shadow-[0_10px_30px_-12px_rgb(0_0_0/0.45)]',
          )}
        >
          {BACKDROPS.map((item) => {
            const active = item.id === current
            return (
              <button
                key={item.id}
                type="button"
                role="menuitemradio"
                aria-checked={active}
                onClick={() => {
                  setBackdrop(item.id)
                  setOpen(false)
                  buttonRef.current?.focus()
                }}
                className={cn(
                  'flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left',
                  'transition-ui duration-fast hover:bg-ink-ghost/10',
                  'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
                )}
              >
                <span
                  aria-hidden="true"
                  className={cn(
                    'backdrop-swatch h-[26px] w-10 shrink-0 rounded-xs border border-line',
                    `backdrop-swatch-${item.id}`,
                  )}
                />
                <span className="flex min-w-0 flex-col">
                  <span className={cn('text-xs', active ? 'text-ink' : 'text-ink-muted')}>
                    {item.label}
                  </span>
                  <span className="truncate text-2xs text-ink-faint">{item.hint}</span>
                </span>
                {active && (
                  <Check
                    size={12}
                    strokeWidth={2.4}
                    className="ml-auto shrink-0 text-accent"
                    aria-hidden="true"
                  />
                )}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

export default BackdropToggle
