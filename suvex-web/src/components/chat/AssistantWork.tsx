import { useState } from 'react'
import { ChevronDown, ChevronRight, Sparkles } from 'lucide-react'
import { ApprovalCard } from '@/components/chat/ChatControls'
import { ToolCard } from '@/components/chat/ToolCard'
import { ToolGroup } from '@/components/chat/ToolGroup'
import { cn } from '@/lib/cn'
import type { ChatMessage } from '@/data/chat'

/**
 * 助手回合里的「工作过程」。
 *
 * ## 排版顺序
 *
 * 保持消息真实顺序，但把连续同类的东西聚合：
 * - 连续旁白（`surface === 'process'`）→ 一个「过程详情 · N 步」折叠条；
 * - 连续工具调用 → 一个 `ToolGroup`（单条时直接是一行工具卡）；
 * - 待审批的工具 → 原样渲染 `ApprovalCard`，**绝不**收进折叠里。
 *
 * ## 默认开合
 *
 * 跑着的时候默认展开，让用户看得见它在干嘛；一轮结束后默认收起，
 * 把版面还给结论。用户手动点过之后不再自动抢回去。
 */
export function AssistantWork({
  items,
  live = false,
  deciding,
  onDecide,
}: {
  items: ChatMessage[]
  live?: boolean
  deciding?: boolean
  onDecide?: (messageId: string, approve: boolean) => void
}) {
  const segments = buildWorkSegments(items)

  return (
    <div className="flex flex-col gap-1" data-assistant-work>
      {segments.map((segment) => {
        if (segment.kind === 'approval') {
          return (
            <ApprovalCard
              key={`a-${segment.message.id}`}
              message={segment.message}
              onDecide={(approve) => onDecide?.(segment.message.id, approve)}
              disabled={deciding}
            />
          )
        }
        if (segment.kind === 'tools') {
          if (segment.items.length === 1) {
            return <ToolCard key={`t-${segment.items[0].id}`} message={segment.items[0]} />
          }
          return (
            <ToolGroup key={`t-${segment.items[0].id}`} items={segment.items} live={live} />
          )
        }
        return (
          <NarrationGroup key={`n-${segment.items[0].id}`} items={segment.items} live={live} />
        )
      })}
    </div>
  )
}

type WorkSegment =
  | { kind: 'narration'; items: ChatMessage[] }
  | { kind: 'tools'; items: ChatMessage[] }
  | { kind: 'approval'; message: ChatMessage }

function buildWorkSegments(items: ChatMessage[]): WorkSegment[] {
  // 段数很少（一轮通常 < 20 条），顺序扫一遍就够，不需要 useMemo。
  // ★ 名字里不要带 `use` —— 它不是 hook，叫 `useMemoSegments` 会让
  //   react-hooks 规则和读代码的人都以为它只能在渲染期按 hook 规则调用。
  const segments: WorkSegment[] = []
  let tools: ChatMessage[] = []
  let narration: ChatMessage[] = []

  const flushTools = () => {
    if (tools.length > 0) {
      segments.push({ kind: 'tools', items: tools })
      tools = []
    }
  }
  const flushNarration = () => {
    if (narration.length > 0) {
      segments.push({ kind: 'narration', items: narration })
      narration = []
    }
  }

  for (const message of items) {
    if (message.approvalStatus === 'pending') {
      flushTools()
      flushNarration()
      segments.push({ kind: 'approval', message })
      continue
    }
    if (message.role === 'tool') {
      flushNarration()
      tools.push(message)
      continue
    }
    flushTools()
    narration.push(message)
  }
  flushTools()
  flushNarration()
  return segments
}

/**
 * 连续旁白收成一行；跑着的时候默认展开，结束后默认收起。
 *
 * ★ 用户手动点过之后，开合由用户说了算 —— 用 `override` 记住那一次选择，
 *   没有点过（`null`）才跟随 `live`。
 *
 * 不用 `useEffect(() => setOpen(live), [live])`：那是同步 setState，
 * 会多一轮渲染（`react-hooks/set-state-in-effect`），而且每次 `live`
 * 变化都会把手动展开的组抢回去 —— 与本函数上面那句承诺相反。
 */
function NarrationGroup({ items, live }: { items: ChatMessage[]; live: boolean }) {
  const [override, setOverride] = useState<boolean | null>(null)
  const open = override ?? live

  return (
    <div className="flex flex-col" data-narration-group data-narration-steps={items.length}>
      <button
        type="button"
        onClick={() => setOverride(!open)}
        aria-expanded={open}
        className={cn(
          'flex w-full items-center gap-1.5 rounded-sm px-1 py-1 text-left',
          'text-2xs text-ink-faint transition-colors duration-150 hover:text-ink-muted',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
        )}
      >
        {open ? (
          <ChevronDown size={11} strokeWidth={2} className="shrink-0" aria-hidden="true" />
        ) : (
          <ChevronRight size={11} strokeWidth={2} className="shrink-0" aria-hidden="true" />
        )}
        <Sparkles size={11} strokeWidth={2} className="shrink-0" aria-hidden="true" />
        <span>
          {live ? '正在处理' : '过程详情'} · {items.length} 步
        </span>
      </button>
      {open && (
        <div className="ml-2 flex flex-col gap-1 border-l border-line/70 pl-2.5">
          {items.map((message) => (
            <p
              key={message.id}
              className="whitespace-pre-wrap break-words text-xs leading-relaxed text-ink-muted"
              data-narration-line
            >
              {message.content}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}
