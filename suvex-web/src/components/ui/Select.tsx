import { Check, ChevronDown } from 'lucide-react'
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react'
import { createPortal } from 'react-dom'
import { cn } from '@/lib/cn'

/**
 * 全站统一的单选下拉。
 *
 * ## 为什么不用原生 `<select>`
 *
 * 收起状态可以靠 `appearance-none` 伪装成主题控件，但**展开层由系统绘制**：
 * 白底、系统蓝色选中条、系统字体、方角。同一个页面里自绘的按钮/浮层与它
 * 并排，割裂感就是这么来的（用户截图：「类型」下拉盖住输入框、纯白列表、
 * 蓝色高亮）。原生控件的行为改不了，只能自己画。
 *
 * ## 实现要点
 *
 * - 菜单走 `createPortal` 到 `body` + `position: fixed`：表格、卡片、滚动容器
 *   的 `overflow` 不会再裁掉它。
 * - 空间不足时向上展开；左右夹在视口内。
 * - 触发器保持焦点，方向键移动高亮、Enter/空格选中、Esc 关闭、Tab 关闭。
 * - 滚动页面或改变窗口大小时关闭，避免菜单与触发器错位。
 * - 选项行与 `ProviderSelect` / `MultiSelect` 用同一套材质：圆角、hover
 *   底色、violet 勾选。
 */
export interface SelectOption<T extends string | number> {
  value: T
  label: string
  disabled?: boolean
}

export interface SelectProps<T extends string | number> {
  value: T
  options: readonly SelectOption<T>[]
  onChange: (value: T) => void
  id?: string
  ariaLabel?: string
  disabled?: boolean
  placeholder?: string
  size?: 'sm' | 'md'
  className?: string
  menuClassName?: string
  /** 原生 title 提示。 */
  title?: string
  /** 透传给触发按钮，测试与自动化用。 */
  testId?: string
}

const MENU_MAX_HEIGHT = 256
const VIEWPORT_PAD = 8

export function Select<T extends string | number>({
  value,
  options,
  onChange,
  id,
  ariaLabel,
  disabled,
  placeholder = '无可选项',
  size = 'md',
  className,
  menuClassName,
  title,
  testId,
}: SelectProps<T>) {
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const [position, setPosition] = useState({ left: 0, top: 0, width: 0 })
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)
  const listId = useId()
  const current = options.find((option) => option.value === value)
  const selectedIndex = options.findIndex((option) => option.value === value)

  const close = useCallback((focusTrigger: boolean) => {
    setOpen(false)
    if (focusTrigger) triggerRef.current?.focus()
  }, [])

  const measure = useCallback(() => {
    const trigger = triggerRef.current
    if (!trigger) return
    const rect = trigger.getBoundingClientRect()
    const width = Math.min(Math.max(rect.width, 180), window.innerWidth - VIEWPORT_PAD * 2)
    const estimated = Math.min(options.length * 30 + 8, MENU_MAX_HEIGHT)
    const openUp =
      window.innerHeight - rect.bottom < estimated + VIEWPORT_PAD &&
      rect.top > estimated + VIEWPORT_PAD
    const left = Math.min(
      Math.max(rect.left, VIEWPORT_PAD),
      window.innerWidth - width - VIEWPORT_PAD,
    )
    const top = openUp ? rect.top - estimated - 4 : rect.bottom + 4
    setPosition({ left, top, width })
  }, [options.length])

  useLayoutEffect(() => {
    if (open) measure()
  }, [open, measure])

  useEffect(() => {
    if (!open) return
    function onPointerDown(event: PointerEvent) {
      const target = event.target as Node
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return
      setOpen(false)
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Escape') return
      event.preventDefault()
      close(true)
    }
    function onScroll(event: Event) {
      // 菜单自己滚不算；页面/祖先容器滚动会让 fixed 菜单错位，直接关。
      if (menuRef.current?.contains(event.target as Node)) return
      setOpen(false)
    }
    function onResize() {
      setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onResize)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onResize)
    }
  }, [open, close])

  function openMenu() {
    setActive(selectedIndex >= 0 ? selectedIndex : 0)
    setOpen(true)
  }

  useEffect(() => {
    if (!open) return
    menuRef.current
      ?.querySelector<HTMLElement>(`[data-index="${active}"]`)
      ?.scrollIntoView({ block: 'nearest' })
  }, [open, active])

  function choose(index: number) {
    const option = options[index]
    if (!option || option.disabled) return
    onChange(option.value)
    close(true)
  }

  function onTriggerKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>) {
    if (disabled) return
    if (!open) {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) {
        event.preventDefault()
        openMenu()
      }
      return
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActive((index) => Math.min(index + 1, options.length - 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActive((index) => Math.max(index - 1, 0))
    } else if (event.key === 'Home') {
      event.preventDefault()
      setActive(0)
    } else if (event.key === 'End') {
      event.preventDefault()
      setActive(Math.max(options.length - 1, 0))
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      choose(active)
    } else if (event.key === 'Tab') {
      setOpen(false)
    }
  }

  return (
    <>
      <button
        ref={triggerRef}
        id={id}
        type="button"
        role="combobox"
        disabled={disabled}
        title={title}
        data-testid={testId}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        onClick={() => (open ? setOpen(false) : openMenu())}
        onKeyDown={onTriggerKeyDown}
        className={cn(
          'flex w-full items-center justify-between gap-2 rounded-sm border border-line',
          'bg-surface-sunken text-left text-ink transition-ui duration-fast',
          'hover:border-line-strong focus:outline-none focus:ring-2 focus:ring-accent/40',
          'disabled:cursor-not-allowed disabled:opacity-50',
          size === 'sm' ? 'h-8 px-2 text-xs' : 'h-9 px-2.5 text-sm',
          className,
        )}
      >
        <span className={cn('truncate', current ? 'text-ink' : 'text-ink-faint')}>
          {current?.label ?? placeholder}
        </span>
        <ChevronDown
          size={12}
          strokeWidth={2}
          aria-hidden="true"
          className={cn(
            'shrink-0 text-ink-faint transition-transform duration-fast',
            open && 'rotate-180',
          )}
        />
      </button>

      {open &&
        createPortal(
          <div
            ref={menuRef}
            id={listId}
            role="listbox"
            aria-label={ariaLabel}
            style={{ left: position.left, top: position.top, width: position.width }}
            className={cn(
              'fixed z-50 overflow-y-auto overscroll-contain rounded-sm border border-line',
              'bg-surface-raised p-1 shadow-lg',
              menuClassName,
            )}
          >
            {options.length === 0 ? (
              <p className="px-2 py-3 text-2xs text-ink-faint">无可选项</p>
            ) : (
              options.map((option, index) => {
                const selected = option.value === value
                return (
                  <button
                    key={String(option.value)}
                    type="button"
                    role="option"
                    data-index={index}
                    aria-selected={selected}
                    disabled={option.disabled}
                    onPointerDown={(event) => event.preventDefault()}
                    onMouseEnter={() => setActive(index)}
                    onClick={() => choose(index)}
                    className={cn(
                      'flex w-full items-center gap-1.5 rounded-xs px-1.5 py-1 text-left',
                      'text-2xs transition-ui duration-fast',
                      index === active ? 'bg-surface-sunken' : '',
                      selected ? 'text-ink' : 'text-ink-muted',
                      option.disabled && 'cursor-not-allowed opacity-50',
                    )}
                  >
                    <Check
                      size={10}
                      strokeWidth={2.5}
                      aria-hidden="true"
                      className={cn('shrink-0', selected ? 'text-accent' : 'invisible')}
                    />
                    <span className="truncate">{option.label}</span>
                  </button>
                )
              })
            )}
          </div>,
          document.body,
        )}
    </>
  )
}
