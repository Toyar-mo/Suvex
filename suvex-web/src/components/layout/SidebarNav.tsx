import { cn } from '@/lib/cn'
import { useI18n } from '@/i18n'
import { groupNavItems, isNavItemActive, type NavItem } from '@/navigation'
import { NavLink, useLocation } from 'react-router-dom'

/**
 * 侧栏导航项。
 *
 * 激活态只做两件事：文字提亮 + 左侧 2px 主色竖条。
 * 刻意不做背景填充 —— 侧栏一屏六个入口，六块底色会变成一堵墙；
 * 竖条是"当前在哪"，比整块高亮更安静，也更容易和后面的内容区共存。
 *
 * 为什么自己写 NavLink 而不是用 NavLink 的 className 回调：
 * isActive 需要前缀匹配（/findings/:id 要让 Findings 保持高亮），
 * 而 NavLink 默认只做全等。
 */

export interface SidebarNavItemProps {
  item: NavItem
  /** 折叠态：只显示图标，文字用 title 兜底 */
  collapsed: boolean
  /** 点击后回调 —— 移动端抽屉用它关闭自己 */
  onNavigate?: () => void
}

export function SidebarNavItem({ item, collapsed, onNavigate }: SidebarNavItemProps) {
  const { t } = useI18n()
  const { pathname } = useLocation()
  const active = isNavItemActive(item, pathname)
  const Icon = item.icon

  return (
    <NavLink
      to={item.path}
      onClick={onNavigate}
      aria-current={active ? 'page' : undefined}
      // 折叠时 aria-label 必须补上：图标本身不带可读文本，
      // 否则屏幕阅读器只会念出一个链接
      aria-label={collapsed ? t(`nav.${item.id}`) : undefined}
      title={collapsed ? t(`nav.${item.id}`) : undefined}
      className={cn(
        'group relative flex items-center gap-tight rounded-sm py-2 text-sm transition-ui duration-fast',
        collapsed ? 'justify-center px-0' : 'pl-3 pr-2',
        active ? 'text-ink' : 'text-ink-muted hover:bg-surface-raised hover:text-ink',
      )}
    >
      {/* 激活竖条：2px，落在左边缘 */}
      <span
        aria-hidden="true"
        className={cn(
          'absolute inset-y-1 left-0 w-0.5 rounded-pill transition-opacity duration-fast',
          active ? 'bg-accent opacity-100' : 'opacity-0',
        )}
      />
      <Icon size={18} strokeWidth={1.75} className="shrink-0" aria-hidden="true" />
      {!collapsed && <span className="truncate">{t(`nav.${item.id}`)}</span>}

      {/*
        折叠态的标签提示。自己画而不是靠原生 title：
          - 原生 title 有约 1 秒延迟，且样式无法控制，和这套视觉不搭；
          - 原生 title 在触摸设备上**永不显示** —— 而图标轨道最需要它的恰恰是
            新用户第一次看到六个图标的时候。
        用 group-focus-visible 让键盘 Tab 过去时也显示，不只是鼠标。
        pointer-events-none 保证它不挡住下面的元素。
      */}
      {collapsed && (
        <span
          aria-hidden="true"
          className={cn(
            'pointer-events-none absolute left-[calc(100%+6px)] z-50 whitespace-nowrap rounded-sm',
            'border border-line bg-surface-overlay px-2 py-1 text-xs text-ink shadow-overlay',
            'opacity-0 transition-opacity duration-fast',
            'group-hover:opacity-100 group-focus-visible:opacity-100',
          )}
        >
          {t(`nav.${item.id}`)}
        </span>
      )}
    </NavLink>
  )
}

export interface SidebarNavProps {
  collapsed: boolean
  onNavigate?: () => void
}

export function SidebarNav({ collapsed, onNavigate }: SidebarNavProps) {
  const { t } = useI18n()
  const groups = groupNavItems()

  return (
    <nav
      aria-label="主导航"
      data-sidebar-nav=""
      className="flex min-h-0 flex-col gap-0.5 overflow-y-auto px-2"
    >
      {/*
        分组解决的是「14 个并列入口不知道先点哪个」。组名给的是一条判断顺序：
        先看它在跑什么，再看它留下了什么，然后是自动跑的那部分，最后才是配置。
        折叠态没有横向空间放组名，改用一条细分隔线 —— 分组信息不丢，只是退化。
      */}
      {groups.map((group, index) => (
        <div key={group.id} className="flex flex-col gap-0.5" data-nav-group={group.id}>
          {collapsed ? (
            index > 0 && <span aria-hidden="true" className="mx-2 my-1.5 h-px shrink-0 bg-line" />
          ) : (
            <h2 className="px-3 pb-0.5 pt-2 text-[11px] font-medium tracking-[0.06em] text-ink-muted">
              {t(`navGroup.${group.id}`)}
            </h2>
          )}
          {group.items.map((item) => (
            <SidebarNavItem key={item.id} item={item} collapsed={collapsed} onNavigate={onNavigate} />
          ))}
        </div>
      ))}
    </nav>
  )
}

export default SidebarNav
