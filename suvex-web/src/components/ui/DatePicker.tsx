import { CalendarDays, ChevronLeft, ChevronRight } from 'lucide-react'
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { cn } from '@/lib/cn'

/**
 * 日期输入 + 自绘日历。
 *
 * 原生 `<input type="date">` 的弹层同样由系统绘制：浅色主题下是一块
 * 与页面无关的白板，深色主题下靠 `color-scheme` 才勉强跟上。这里保留
 * 文本输入（仍然可以手打 `YYYY-MM-DD`），但日历浮层自己画。
 *
 * 日期一律按**本地时区**解析/格式化：`new Date('2026-11-10')` 是 UTC
 * 午夜，东八区会变成 11-10 08:00，某些时区则退回前一天。
 */
const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六']
const PANEL_WIDTH = 248
const PANEL_HEIGHT = 300

function pad(value: number) {
  return String(value).padStart(2, '0')
}

function formatDate(date: Date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

function parseDate(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return null
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
  return Number.isNaN(date.getTime()) ? null : date
}

function sameDay(a: Date, b: Date) {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  )
}

export function DatePicker({
  id,
  value,
  onChange,
  min,
  max,
  ariaLabel,
  placeholder = 'YYYY-MM-DD',
}: {
  id: string
  value: string
  onChange: (value: string) => void
  min?: string
  max?: string
  ariaLabel?: string
  placeholder?: string
}) {
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState({ left: 0, top: 0 })
  const [month, setMonth] = useState(() => {
    const parsed = parseDate(value)
    const today = new Date()
    return { year: (parsed ?? today).getFullYear(), month: (parsed ?? today).getMonth() }
  })
  const triggerRef = useRef<HTMLInputElement | null>(null)
  const panelRef = useRef<HTMLDivElement | null>(null)
  const selected = parseDate(value)
  const today = new Date()

  const measure = useCallback(() => {
    const trigger = triggerRef.current
    if (!trigger) return
    const rect = trigger.getBoundingClientRect()
    const openUp =
      window.innerHeight - rect.bottom < PANEL_HEIGHT + 8 && rect.top > PANEL_HEIGHT + 8
    const left = Math.min(
      Math.max(rect.left, 8),
      window.innerWidth - PANEL_WIDTH - 8,
    )
    setPosition({
      left,
      top: openUp ? rect.top - PANEL_HEIGHT - 4 : rect.bottom + 4,
    })
  }, [])

  useLayoutEffect(() => {
    if (open) measure()
  }, [open, measure])

  useEffect(() => {
    if (!open) return
    function onPointerDown(event: PointerEvent) {
      const target = event.target as Node
      if (panelRef.current?.contains(target) || triggerRef.current?.contains(target)) return
      setOpen(false)
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }
    function onScroll() {
      setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onScroll)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onScroll)
    }
  }, [open])

  function openCalendar() {
    const parsed = parseDate(value)
    const base = parsed ?? new Date()
    setMonth({ year: base.getFullYear(), month: base.getMonth() })
    setOpen(true)
  }

  function moveMonth(offset: number) {
    setMonth((current) => {
      const next = new Date(current.year, current.month + offset, 1)
      return { year: next.getFullYear(), month: next.getMonth() }
    })
  }

  const firstWeekday = new Date(month.year, month.month, 1).getDay()
  const daysInMonth = new Date(month.year, month.month + 1, 0).getDate()
  const cells: Array<number | null> = [
    ...Array<number | null>(firstWeekday).fill(null),
    ...Array.from({ length: daysInMonth }, (_, index) => index + 1),
  ]
  while (cells.length % 7 !== 0) cells.push(null)

  return (
    <>
      <div className="relative">
        <input
          ref={triggerRef}
          id={id}
          type="text"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholder}
          aria-label={ariaLabel}
          autoComplete="off"
          className={cn(
            'h-8 w-full rounded-sm border border-line bg-surface-sunken px-2 pr-8 text-xs text-ink',
            'transition-ui duration-fast placeholder:text-ink-faint',
            'hover:border-line-strong focus:outline-none focus:ring-2 focus:ring-accent/40',
          )}
        />
        <button
          type="button"
          onClick={openCalendar}
          aria-label="打开日历"
          aria-expanded={open}
          className={cn(
            'absolute right-1 top-1/2 -translate-y-1/2 rounded-xs p-1',
            'text-ink-faint transition-ui duration-fast hover:bg-surface-raised hover:text-ink',
            'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
          )}
        >
          <CalendarDays size={13} strokeWidth={1.75} aria-hidden="true" />
        </button>
      </div>

      {open &&
        createPortal(
          <div
            ref={panelRef}
            role="dialog"
            aria-label="选择日期"
            style={{ left: position.left, top: position.top, width: PANEL_WIDTH }}
            className={cn(
              'fixed z-50 rounded-sm border border-line bg-surface-raised p-2 shadow-lg',
            )}
          >
            <div className="mb-1 flex items-center justify-between">
              <button
                type="button"
                aria-label="上个月"
                onClick={() => moveMonth(-1)}
                className="rounded-xs p-1 text-ink-muted transition-ui duration-fast hover:bg-surface-sunken hover:text-ink"
              >
                <ChevronLeft size={13} strokeWidth={2} aria-hidden="true" />
              </button>
              <span className="text-xs font-medium text-ink">
                {month.year} 年 {month.month + 1} 月
              </span>
              <button
                type="button"
                aria-label="下个月"
                onClick={() => moveMonth(1)}
                className="rounded-xs p-1 text-ink-muted transition-ui duration-fast hover:bg-surface-sunken hover:text-ink"
              >
                <ChevronRight size={13} strokeWidth={2} aria-hidden="true" />
              </button>
            </div>
            <div className="grid grid-cols-7 gap-0.5 text-center">
              {WEEKDAYS.map((weekday) => (
                <span key={weekday} className="py-1 text-2xs text-ink-faint">
                  {weekday}
                </span>
              ))}
              {cells.map((day, index) => {
                if (day === null) return <span key={`blank-${index}`} />
                const date = new Date(month.year, month.month, day)
                const iso = formatDate(date)
                const disabled = (min !== undefined && iso < min) || (max !== undefined && iso > max)
                const isSelected = selected !== null && sameDay(date, selected)
                const isToday = sameDay(date, today)
                return (
                  <button
                    key={iso}
                    type="button"
                    disabled={disabled}
                    onClick={() => {
                      onChange(iso)
                      setOpen(false)
                    }}
                    className={cn(
                      'aspect-square rounded-xs text-2xs transition-ui duration-fast',
                      isSelected
                        ? 'bg-accent text-canvas'
                        : 'text-ink-muted hover:bg-surface-sunken hover:text-ink',
                      isToday && !isSelected && 'ring-1 ring-accent/40',
                      disabled && 'cursor-not-allowed opacity-40 hover:bg-transparent hover:text-ink-muted',
                    )}
                  >
                    {day}
                  </button>
                )
              })}
            </div>
          </div>,
          document.body,
        )}
    </>
  )
}
