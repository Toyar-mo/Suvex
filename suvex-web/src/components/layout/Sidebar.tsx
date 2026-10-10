import { StatusDot } from '@/components/ui/StatusDot'
import { cn } from '@/lib/cn'
import { logout } from '@/data/auth-store'
import { useFleetStatus } from '@/data/fleet'
import { LogOut, PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import SidebarNav from './SidebarNav'

/**
 * 桌面/平板侧栏。两部分：顶部 brand + 底部折叠开关。
 *
 * 宽度：展开 13rem(208px) / 折叠 rail(56px)。
 * 折叠后只剩图标，但**不隐藏** —— 折叠的目的是腾出横向空间给内容
 * （日志和表格都需要宽度），不是把导航藏起来。
 *
 * 折叠状态下依然保留 aria-label，键盘和读屏用户不受影响。
 */

export interface SidebarProps {
  collapsed: boolean
  onToggleCollapsed: () => void
  className?: string
}

export function Sidebar({ collapsed, onToggleCollapsed, className }: SidebarProps) {
  const { agents } = useFleetStatus()

  return (
    <aside
      className={cn(
        'flex shrink-0 flex-col border-r border-line bg-surface/60',
        /*
          ★ 任务 8 改：ease-enter → ease-state。

          侧栏折叠是**状态切换**（展开 ⇄ 折叠），不是入场。
          用 ease-enter（指数减速）会让"展开"很顺、但"收起"显得发钝 ——
          同一个动作反着做却有两种手感。ease-state 是对称曲线，两向一致。
          见 design-system §5.1 的曲线分工表。

          transition-[width] 是全站仅有的两处布局动画之一（另一处是
          SeverityChart 的数据条）。这里宽度只影响侧栏自身与主内容之间的
          flex 分配，且是用户主动触发的一次性动作，值得这个成本。
        */
        'transition-[width] duration-base ease-state',
        collapsed ? 'w-rail' : 'w-52',
        className,
      )}
      data-collapsed={collapsed}
    >
      {/* brand */}
      <div
        className={cn(
          'flex h-14 shrink-0 items-center gap-tight',
          collapsed ? 'justify-center px-0' : 'px-4',
        )}
      >
        <span className="text-base font-medium text-ink">
          Suvex
          <span className="text-accent">.</span>
        </span>
        {!collapsed && <span className="text-2xs text-ink-faint">控制台</span>}
      </div>

      <SidebarNav collapsed={collapsed} />

      <div className="flex-1" />

      {/* 折叠开关 + 登出 + agent 数 */}
      <div className={cn('shrink-0 border-t border-line p-2', collapsed && 'px-0')}>
        {/*
          登出口（契约 §8：加在侧栏底部，与"收起导航"同区）。

          ★ 它必须**无条件可用** —— 后端把 logout 做成了幂等的
          （凭据已过期时也返回 204）。所以即使会话已经失效，
          这里按下去仍能把本地状态和那个失效的 Cookie 一起清掉。

          反过来若按"未登录就禁用"来做，用户会被卡在一个
          既进不去、也退不出的状态 —— 而 Cookie 留在浏览器里
          没有任何好处。
        */}
        <button
          type="button"
          onClick={() => void logout()}
          aria-label="登出"
          title="登出"
          className={cn(
            'flex w-full items-center gap-tight rounded-sm py-1.5 text-xs text-ink-muted',
            'transition-ui duration-fast hover:bg-surface-raised hover:text-ink',
            collapsed ? 'justify-center px-0' : 'px-3',
          )}
        >
          <LogOut size={16} strokeWidth={1.75} aria-hidden="true" />
          {!collapsed && <span>登出</span>}
        </button>

        <button
          type="button"
          onClick={onToggleCollapsed}
          aria-expanded={!collapsed}
          aria-label={collapsed ? '展开导航' : '收起导航'}
          title={collapsed ? '展开导航' : '收起导航'}
          className={cn(
            'flex w-full items-center gap-tight rounded-sm py-1.5 text-xs text-ink-muted',
            'transition-ui duration-fast hover:bg-surface-raised hover:text-ink',
            collapsed ? 'justify-center px-0' : 'px-3',
          )}
        >
          {collapsed ? (
            <PanelLeftOpen size={16} strokeWidth={1.75} aria-hidden="true" />
          ) : (
            <>
              <PanelLeftClose size={16} strokeWidth={1.75} aria-hidden="true" />
              <span>收起</span>
            </>
          )}
        </button>

        {/* 侧栏底部也放一个 agent 数 —— 折叠后状态条在窄屏会被滚走，
            这里是一个稳定的兜底锚点 */}
        <div
          className={cn(
            'mt-1 flex items-center gap-tight text-2xs text-ink-faint',
            collapsed ? 'justify-center px-0' : 'px-3',
          )}
          /*
            ★ title 原来写的是「N 个智能体运行中」——**那是个错误表述**
            （任务 11 修正）。`useFleetStatus().agents.running` 数的是
            **扫描任务数**，不是智能体实例数（见 `data/fleet.ts` 文件头）。
            悬停提示是用户唯一能看到完整句子的地方，说错比不显示更糟 ——
            他会据此以为系统里有 N 个智能体在跑，而去活动页一数发现对不上。
          */
          title={`${agents.running} 个扫描运行中`}
        >
          <StatusDot tone={agents.running > 0 ? 'accent' : 'idle'} pulse={agents.running > 0} />
          {!collapsed && (
            <span className="tabular" data-numeric>
              {agents.running} 运行中
            </span>
          )}
        </div>
      </div>
    </aside>
  )
}

export default Sidebar
