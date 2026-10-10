import { cn } from '@/lib/cn'
import { StatusDot, type StatusTone } from '@/components/ui/StatusDot'
import { AGENT_ROLE_LABEL, AGENT_STATUS_LABEL, type Agent, type AgentStatus } from '@/data/activity'
import { formatRelativeTime } from '@/lib/datetime'

/**
 * 左侧的智能体名册。
 *
 * 这一块要回答的是「**这一队智能体各自在干什么**」，所以：
 *   - 按角色分组显示（信息收集 / 漏洞扫描 / 漏洞利用 / 结果验证 / 报告整理）——
 *     这是 Suvex 多 agent 协作的实际组织方式，比按 id 排更说明问题；
 *   - 每个 agent 显示它负责的目标与当前状态；
 *   - 有未读事件时给一个计数角标，让用户知道哪个 agent 在他没看时又动了。
 *
 * 视觉克制：不使用彩色卡片、不使用进度条。整块就是「一行一个 agent」，
 * 靠状态点和字重分层。这一页的核心是右侧的流，左侧是导航而不是仪表盘。
 */

const STATUS_TONE: Record<AgentStatus, StatusTone> = {
  running: 'accent',
  idle: 'idle',
  blocked: 'warn',
  done: 'ok',
  failed: 'risk',
}

export interface AgentRosterProps {
  agents: Agent[]
  /** 当前选中的 agent；null = 看全部 */
  selectedAgentId: string | null
  onSelect: (agentId: string | null) => void
  /** agentId → 未读事件数（暂停期间新增的） */
  unreadByAgent?: Record<string, number>
  /** 事件总数，用于"全部"这一项 */
  totalEvents: number
  className?: string
}

export function AgentRoster({
  agents,
  selectedAgentId,
  onSelect,
  unreadByAgent = {},
  totalEvents,
  className,
}: AgentRosterProps) {
  const running = agents.filter((a) => a.status === 'running').length

  return (
    /*
      响应式形态（两种，由外层 aside 的 flex 方向决定）：

        桌面：竖直列表。有充足高度，一行一个 agent 能显示角色、状态、计数。

        窄屏：**横向可滚动的一行**。手机上高度是最稀缺的资源 ——
              竖直列表会占掉 350px 以上，把事件流挤到只剩几十像素
              （实际踩过：320px 屏上流只剩 38px，等于不可用）。
              横排之后每个 agent 是一个窄块，一行约 58px，
              横向滚动切换 agent，纵向空间全留给流。

      标题在窄屏隐藏：它不提供信息（就一个"智能体"），却要占 40px 高度。
    */
    <div className={cn('flex min-h-0 flex-col', className)}>
      <div className="hidden items-baseline justify-between gap-tight px-panel py-3 md:flex">
        <h2 className="text-sm font-medium text-ink">智能体</h2>
        <span className="mono-ui tabular text-ink-faint" data-numeric>
          {running}/{agents.length}
        </span>
      </div>

      <div className="relative md:contents">
        {/*
          横向滚动提示（仅窄屏）。
          实测问题：第 5、6 个 agent 块在 390px 下被视口切掉一半，
          而没有任何"右边还有内容"的暗示 —— 用户会以为只有 4 个 agent。

          做法是从右边缘渐隐到透明：这是"可继续滑动"的通用视觉语言，
          且不会挡住内容（pointer-events-none）。比放一个箭头好：
          箭头是额外元素，会与内容重叠。
        */}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 right-0 z-10 w-8 bg-gradient-to-l from-canvas to-transparent md:hidden"
        />

        <nav
          aria-label="智能体过滤"
          // 窄屏：隐藏横向滚动条（已有渐隐提示，滚动条还会占高度）
          className={cn(
            'flex gap-1.5 px-2 pb-2',
            'overflow-x-auto md:flex-col md:gap-0.5 md:overflow-x-visible',
            '[scrollbar-width:none] md:[scrollbar-width:auto]',
            '[&::-webkit-scrollbar]:hidden md:[&::-webkit-scrollbar]:block',
          )}
        >
          {/* 「全部」项 —— 默认视图，一次看完整队的交错流 */}
          <RosterRow
            label="全部活动"
            sublabel={`${totalEvents} 条`}
            active={selectedAgentId === null}
            onClick={() => onSelect(null)}
            tone="idle"
            isAll
          />

          {/* 竖直布局下的分隔线；横向布局里改用间距 */}
          <div className="my-1 hidden h-px shrink-0 bg-line md:block" />

          {agents.map((agent) => {
            const unread = unreadByAgent[agent.id] ?? 0
            return (
              <RosterRow
                key={agent.id}
                label={agent.id}
                sublabel={`${AGENT_ROLE_LABEL[agent.role]} · ${AGENT_STATUS_LABEL[agent.status]}`}
                meta={agent.eventCount > 0 ? String(agent.eventCount) : undefined}
                time={agent.eventCount > 0 ? formatRelativeTime(agent.lastActiveAt) : undefined}
                active={selectedAgentId === agent.id}
                onClick={() => onSelect(agent.id)}
                tone={STATUS_TONE[agent.status]}
                pulse={agent.status === 'running'}
                unread={unread}
              />
            )
          })}
        </nav>
      </div>
    </div>
  )
}

interface RosterRowProps {
  label: string
  sublabel: string
  meta?: string
  time?: string
  active: boolean
  onClick: () => void
  tone: StatusTone
  pulse?: boolean
  unread?: number
  isAll?: boolean
}

function RosterRow({
  label,
  sublabel,
  meta,
  time,
  active,
  onClick,
  tone,
  pulse,
  unread = 0,
  isAll = false,
}: RosterRowProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? 'true' : undefined}
      className={cn(
        'group relative flex flex-col gap-0.5 rounded-sm text-left',
        'transition-ui duration-fast',
        // 窄屏：固定宽度的一块，横排滚动；不 flex-1，否则会被压成等分宽度。
        // 宽度取 9rem —— 实测能容纳「agent-01 + 计数」与「信息收集」一行，
        // 128px 时副标题会截成「信息收集 · 运行中」并顶掉时间，看着像坏掉。
        'w-36 shrink-0 py-1.5 pl-2.5 pr-2',
        // 桌面：撑满宽度、竖直堆叠
        'md:w-full md:py-2 md:pl-3 md:pr-2',
        active ? 'bg-surface-raised' : 'hover:bg-surface-raised/60',
      )}
    >
      {/* 选中竖条：与侧栏导航同一套语言 */}
      <span
        aria-hidden="true"
        className={cn(
          'absolute inset-y-1.5 left-0 w-0.5 rounded-pill transition-opacity duration-fast',
          active ? 'bg-accent opacity-100' : 'opacity-0',
        )}
      />

      <span className="flex items-center gap-tight">
        <StatusDot tone={tone} pulse={pulse} />

        <span
          className={cn(
            'min-w-0 flex-1 truncate',
            isAll ? 'text-sm' : 'mono-ui',
            active ? 'text-ink' : 'text-ink-muted',
          )}
        >
          {label}
        </span>

        {/* 未读角标。只在暂停期间出现 —— 没暂停时用户本来就在看着流走 */}
        {unread > 0 && (
          <span
            className="tabular shrink-0 rounded-pill bg-accent-wash px-1.5 text-2xs text-accent-strong"
            data-numeric
          >
            +{unread}
          </span>
        )}

        {meta && unread === 0 && (
          <span className="tabular shrink-0 text-2xs text-ink-faint" data-numeric>
            {meta}
          </span>
        )}
      </span>

      <span className="flex items-center gap-tight pl-[calc(0.375rem+0.5rem)]">
        <span className="min-w-0 flex-1 truncate text-2xs text-ink-faint">{sublabel}</span>
        {/*
          时间只在桌面显示。窄屏的横向块里它是最不重要的信息，
          留着会把副标题（角色 · 状态）挤到截断 —— 那两样才决定"点它值不值"。
        */}
        {time && (
          <span className="tabular hidden shrink-0 text-2xs text-ink-faint md:inline">{time}</span>
        )}
      </span>
    </button>
  )
}

export default AgentRoster
