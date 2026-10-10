import { cn } from '@/lib/cn'
import { isNavItemActive, MOBILE_MORE_ITEMS, MOBILE_TAB_ITEMS } from '@/navigation'
import { MoreHorizontal } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { NavLink, useLocation } from 'react-router-dom'

/**
 * 移动端导航。
 *
 * 断点：只在 **<md(768px) 真手机** 出现。平板（768–1023）交给侧栏图标轨道 ——
 * 平板竖屏横向有富余，而垂直空间宝贵；更重要的是轨道能让 6 个入口全部一步直连，
 * 底栏只能放 4 个。这条直接对应验收里「操作路径不比桌面端多一步以上」。
 *
 * 设计取舍（对应任务要求「不要用汉堡菜单藏所有功能」）：
 *   - 底栏常驻 4 个高频入口：Overview / Scans / Agent Activity / Findings。
 *     Agent Activity 放进常驻栏而不是 More —— 它是这个产品的核心叙事页面，
 *     藏起来等于把主功能藏起来。
 *   - 只有 Reports / Settings 进 More。它们是低频的"配置和产出"，
 *     不属于日常扫视路径。
 *   - More 用**锚定在底栏上方的轻面板**，不用全屏抽屉/模态：
 *     只是切个页面，不需要打断用户当前上下文，也不需要焦点陷阱。
 *
 * 底栏固定定位，所以主内容区必须预留等高 padding（见 AppShell），
 * 否则最后一行内容会被压在底栏下面。
 */

export function MobileNav() {
  const { pathname } = useLocation()
  const moreRef = useRef<HTMLDivElement>(null)
  const moreButtonRef = useRef<HTMLButtonElement>(null)

  /**
   * 存的是「在哪个路径下把 More 打开了」，而不是一个布尔值。
   * 这样路由一变，派生的 moreOpen 自动变 false，不需要 effect 里 setState
   * （那会触发级联渲染，React 官方明确反对）。
   */
  const [openForPath, setOpenForPath] = useState<string | null>(null)
  const moreOpen = openForPath === pathname

  const closeMore = () => setOpenForPath(null)

  // 点击面板外部 / Esc 关闭
  useEffect(() => {
    if (!moreOpen) return

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      if (moreRef.current?.contains(target) || moreButtonRef.current?.contains(target)) return
      setOpenForPath(null)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setOpenForPath(null)
        // 焦点还给触发它的按钮，键盘用户不会掉到页面顶部
        moreButtonRef.current?.focus()
      }
    }

    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [moreOpen])

  const moreActive = MOBILE_MORE_ITEMS.some((item) => isNavItemActive(item, pathname))

  return (
    <>
      {/* More 面板：锚在底栏上方，右对齐 */}
      {moreOpen && (
        <div
          ref={moreRef}
          id="mobile-more-panel"
          className="fixed bottom-[calc(3.5rem+env(safe-area-inset-bottom)+0.5rem)] right-3 z-40 w-56 animate-stream-in rounded-md border border-line bg-surface-overlay p-1 shadow-overlay md:hidden"
        >
          {MOBILE_MORE_ITEMS.map((item) => {
            const Icon = item.icon
            const active = isNavItemActive(item, pathname)
            return (
              <NavLink
                key={item.id}
                to={item.path}
                aria-current={active ? 'page' : undefined}
                onClick={closeMore}
                className={cn(
                  'flex items-center gap-tight rounded-sm px-3 py-2 text-sm transition-ui duration-fast',
                  active ? 'text-ink' : 'text-ink-muted hover:bg-surface-raised hover:text-ink',
                )}
              >
                <Icon size={18} strokeWidth={1.75} aria-hidden="true" />
                <span className="truncate">{item.label}</span>
              </NavLink>
            )
          })}
        </div>
      )}

      <nav
        aria-label="主导航（移动端）"
        data-mobile-nav=""
        className="fixed inset-x-0 bottom-0 z-30 border-t border-line bg-surface/95 backdrop-blur-md md:hidden"
        style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}
      >
        <div className="flex h-14 items-stretch">
          {MOBILE_TAB_ITEMS.map((item) => {
            const Icon = item.icon
            const active = isNavItemActive(item, pathname)
            return (
              <NavLink
                key={item.id}
                to={item.path}
                aria-current={active ? 'page' : undefined}
                className={cn(
                  'relative flex flex-1 flex-col items-center justify-center gap-1 text-2xs transition-ui duration-fast',
                  active ? 'text-accent-strong' : 'text-ink-muted',
                )}
              >
                {/* 顶部指示条：和侧栏的左侧竖条同语言 */}
                <span
                  aria-hidden="true"
                  className={cn(
                    'absolute inset-x-4 top-0 h-0.5 rounded-pill transition-opacity duration-fast',
                    active ? 'bg-accent opacity-100' : 'opacity-0',
                  )}
                />
                <Icon size={20} strokeWidth={active ? 2 : 1.75} aria-hidden="true" />
                {/* 底栏 5 格约每格 78px，"智能体活动" 会折行，用 shortLabel */}
                <span className="max-w-full truncate px-0.5">{item.shortLabel ?? item.label}</span>
              </NavLink>
            )
          })}

          <button
            ref={moreButtonRef}
            type="button"
            onClick={() => setOpenForPath(moreOpen ? null : pathname)}
            aria-expanded={moreOpen}
            aria-controls="mobile-more-panel"
            aria-label="更多导航"
            className={cn(
              'relative flex flex-1 flex-col items-center justify-center gap-1 text-2xs transition-ui duration-fast',
              moreActive || moreOpen ? 'text-accent-strong' : 'text-ink-muted',
            )}
          >
            <span
              aria-hidden="true"
              className={cn(
                'absolute inset-x-4 top-0 h-0.5 rounded-pill transition-opacity duration-fast',
                moreActive ? 'bg-accent opacity-100' : 'opacity-0',
              )}
            />
            {/* 有激活项但自身不是下拉态时，用一个点提示"东西在里面" */}
            {moreActive && !moreOpen && (
              <span
                aria-hidden="true"
                className="absolute right-1/4 top-2 h-1 w-1 rounded-pill bg-accent"
              />
            )}
            <MoreHorizontal size={20} strokeWidth={moreOpen ? 2 : 1.75} aria-hidden="true" />
            <span>更多</span>
          </button>
        </div>
      </nav>
    </>
  )
}

export default MobileNav
