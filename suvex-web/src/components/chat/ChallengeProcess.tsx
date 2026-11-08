import { cn } from '@/lib/cn'
import { api, errorMessage } from '@/lib/api'
import { EVENT_TYPE_LABEL, type ActivityEvent } from '@/data/activity'
import { useEffect, useState } from 'react'
import { Terminal } from 'lucide-react'

/**
 * 这道题的**解题过程**（对话页里、题目条下面那一段）。
 *
 * ## 为什么它必须存在
 *
 * 用户点看板「查看」进来，看到的是一个空对话 —— 而那道题此刻正在沙箱里
 * 被解（题目条上写着「解题中」）。"解题中"和"这个对话还是空的"放在一起，
 * 读起来就是"坏了"。
 *
 * 过程本来就在活动流里（`scans/{id}/events`），只是要走到另一个页面才看得到。
 * 这里把**同一份事件**拉过来，按时间排好 —— 不新造数据源，也不假装它们是
 * 聊天消息（那是模型说的话，这些是工具干的事，两者不该混在一列里）。
 *
 * ## 为什么轮询而不是 SSE
 *
 * 对话页已经有一条 SSE 在跑（消息流）。再加一条会多一个连接与一套重连状态，
 * 而这里要的只是"每几秒看一眼"。解题在跑时 3 秒一次、跑完就停 —— 与看板
 * 轮询同一条口径（终态不轮询）。
 */
const POLL_MS = 3000
const MAX_EVENTS = 40

export function ChallengeProcess({
  scanId,
  running,
}: {
  scanId: string | null
  /** 题还在解 → 持续刷新；终态只拉一次 */
  running: boolean
}) {
  const [events, setEvents] = useState<ActivityEvent[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!scanId) return
    let cancelled = false

    const load = async () => {
      try {
        const data = await api.get<{ events: ActivityEvent[]; hasMore: boolean }>(
          `/scans/${encodeURIComponent(scanId)}/events/recent`,
          { query: { limit: MAX_EVENTS } },
        )
        if (cancelled) return
        setEvents(data.events)
        setError(null)
      } catch (err) {
        if (!cancelled) setError(errorMessage(err))
      }
    }

    void load()
    if (!running)
      return () => {
        cancelled = true
      }

    const timer = window.setInterval(() => void load(), POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [scanId, running])

  if (!scanId) {
    return (
      <p className="text-2xs text-ink-faint">
        这道题还没有开始解题 —— 点题目条右侧的「看板」回去，用「开始」或「再解」发起。
      </p>
    )
  }

  return (
    <section aria-label="解题过程" className="flex flex-col gap-1.5">
      <div className="flex items-center gap-1.5 text-2xs text-ink-faint">
        <Terminal size={11} strokeWidth={2} aria-hidden="true" />
        {/* ★ 不在跑时**必须**说清这是旧记录：事件是绑定在这道题上一次
            解题的 scan 上的，新一轮排队时它还没换 —— 不标注的话，用户会把
            几小时前的工具调用当成"现在正在做的事"。 */}
        {running ? '解题过程' : '上一次运行的过程'}
        {running && (
          <span className="flex items-center gap-1 text-accent-strong">
            <span className="h-1 w-1 animate-pulse rounded-pill bg-accent" aria-hidden="true" />
            进行中
          </span>
        )}
        <span className="ml-auto">{events.length} 条</span>
      </div>

      {error && <p className="text-2xs text-signal-risk">读不到过程事件：{error}</p>}

      {!running && events.length > 0 && (
        <p className="text-2xs text-ink-ghost">这道题当前没有在跑 —— 下面是上一次运行的记录。</p>
      )}

      {!error && events.length === 0 && (
        <p className="text-2xs text-ink-faint">
          {running ? '刚开跑，还没有产生动作。' : '这一轮没有留下过程记录。'}
        </p>
      )}

      <ol className="flex flex-col gap-0.5">
        {events.slice(-MAX_EVENTS).map((event) => {
          const command =
            event.detail?.kind === 'tool_call' ? event.detail.command || event.detail.tool : null
          return (
            <li
              key={event.id}
              className="flex min-w-0 items-baseline gap-2 rounded-sm px-1.5 py-0.5 text-2xs hover:bg-surface-sunken"
            >
              <span className="tabular shrink-0 text-ink-ghost">
                {new Date(event.at).toLocaleTimeString('zh-CN', { hour12: false })}
              </span>
              <span
                className={cn(
                  'shrink-0',
                  event.type === 'failed' || event.type === 'blocked'
                    ? 'text-signal-risk'
                    : event.type === 'discovery' || event.type === 'verified'
                      ? 'text-signal-ok'
                      : 'text-ink-faint',
                )}
              >
                {EVENT_TYPE_LABEL[event.type]}
              </span>
              <span
                className="min-w-0 flex-1 truncate text-ink-muted"
                title={command ?? event.summary}
              >
                {command ? <code className="mono-ui">{command}</code> : event.summary}
              </span>
            </li>
          )
        })}
      </ol>
    </section>
  )
}

export default ChallengeProcess
