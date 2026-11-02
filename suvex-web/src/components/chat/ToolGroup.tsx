import { useState } from 'react'
import { ChevronDown, ChevronRight, Wrench } from 'lucide-react'
import { ToolCard } from '@/components/chat/ToolCard'
import { cn } from '@/lib/cn'
import type { ChatMessage } from '@/data/chat'

/**
 * 一轮里连续的工具调用，收成一行摘要。
 *
 * ## 形态来自 metis 的 ToolGroup
 *
 * 一次任务里工具是连着来的（`list_challenges` → `run_shell` → `read_file`），
 * 每条都单独占一行仍然会把结论顶到屏幕外。收成一行：
 *
 *     3 个工具 · list_challenges、run_shell、read_file          ⌄
 *
 * 点开才逐条展开（每条再各自可展开看全文）。失败数会冒到摘要行上 ——
 * 收起时看不见失败，就等于把"工具挂了"藏起来。
 *
 * ## 为什么单条不套组
 *
 * 一条工具调用外面再包一层"1 个工具"是纯噪音，也会让 v0.103 的
 * 「折叠态一行能看到工具名 + 参数摘要」被藏进第二层。
 */
export function ToolGroup({
  items,
  live = false,
}: {
  items: ChatMessage[]
  live?: boolean
}) {
  const [override, setOverride] = useState<boolean | null>(null)
  /*
    默认跟随 `live`（跑着展开、结束后收起）；用户点过之后以那次选择为准。

    ★ 这里刻意不用 `useEffect(() => setOpen(live), [live])`：同步 setState
      多一轮渲染，而且会把用户刚展开的组在回合结束时偷偷收掉。
  */
  const open = override ?? live
  const failed = items.filter((m) => m.toolFailed === true).length
  const names = Array.from(new Set(items.map((m) => m.toolName ?? 'tool')))

  return (
    <div
      className="flex flex-col"
      data-tool-group
      data-tool-count={items.length}
      data-tool-failed={failed}
    >
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
        <Wrench size={11} strokeWidth={2} className="shrink-0" aria-hidden="true" />
        <span className="shrink-0">
          {live ? '正在运行' : '运行了'} {items.length} 个工具
        </span>
        <span className="min-w-0 truncate text-ink-faint/70">{names.join('、')}</span>
        {failed > 0 && (
          <span className="shrink-0 rounded-pill bg-signal-risk/15 px-1.5 text-2xs text-signal-risk">
            {failed} 个失败
          </span>
        )}
      </button>

      {open && (
        /*
          ★ 展开后的列表限高 168px（≈ 7 行）。一组十几条工具时无限高会把
            结论顶到屏幕外 —— 用户点开本意是抽查一下，不是读完全部。
        */
        <div className="ml-2 flex max-h-[168px] flex-col gap-0.5 overflow-y-auto border-l border-line/70 pl-2.5">
          {items.map((message) => (
            <ToolCard key={message.id} message={message} compact />
          ))}
        </div>
      )}
    </div>
  )
}
