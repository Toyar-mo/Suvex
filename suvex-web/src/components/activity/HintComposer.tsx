import { errorMessage } from '@/lib/api'
import { cn } from '@/lib/cn'
import { addScanHint, type ScanHint } from '@/data/scan-hints'
import { Lightbulb, Send } from 'lucide-react'
import { useState } from 'react'

/**
 * 运行中给智能体补线索的输入框（P2 人机协同，v0.57）。
 *
 * ## 它解决的真实问题
 *
 * 智能体正在跑的时候，人看到活动流里它走错了方向（比如在无用路径上反复
 * 撞 404），却没有任何办法把它拉回来 —— 只能等它跑完再重跑，浪费一轮
 * LLM 额度。这个输入框让人把一句话送进**下一轮**的上下文。
 *
 * ## 为什么只在「这次扫描是 running」时出现
 *
 * 线索的消费方是引擎的每轮循环。排队中补线索没有读的人（该写任务说明）；
 * 已结束补线索没有作用点。两种情况都显示输入框会制造「我补了但没反应」的
 * 困惑 —— 与后端 `service.add_hint` 的 409 是同一个判断，只是这里提前藏起来。
 *
 * ## 提交后不回显「我发过了」
 *
 * 回显交给 `onAdded` 让父组件插入列表。这里只负责「清空输入框 + 处理失败」。
 * 失败时**原样显示后端中文**（`errorMessage` 已能拿到可直接展示的文案）——
 * 用户需要知道是"字数超了"还是"扫描已结束"，而不是一句笼统的失败。
 */
export function HintComposer({
  scanId,
  onAdded,
  className,
}: {
  scanId: string
  onAdded?: (hint: ScanHint) => void
  className?: string
}) {
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const trimmed = text.trim()
  const canSend = trimmed.length > 0 && trimmed.length <= 500 && !sending

  async function submit() {
    if (!canSend) return
    setSending(true)
    setError(null)
    try {
      const hint = await addScanHint(scanId, trimmed)
      setText('')
      onAdded?.(hint)
    } catch (err) {
      setError(errorMessage(err) || '线索没能送出，请稍后重试。')
    } finally {
      setSending(false)
    }
  }

  return (
    <div className={cn('flex flex-col gap-1 border-b border-line px-panel py-1.5', className)}>
      <div className="flex items-center gap-1.5">
        {/*
          图标 + 一句话说明「这不是聊天，是给智能体递线索」——
          否则用户会以为自己在跟它对话并要求它回话。
        */}
        <Lightbulb size={13} className="shrink-0 text-signal-warn" aria-hidden="true" />
        <form
          className="flex min-w-0 flex-1 items-center gap-1.5"
          onSubmit={(e) => {
            e.preventDefault()
            void submit()
          }}
        >
          <input
            type="text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            maxLength={500}
            placeholder="给智能体补一条线索（它会在下一轮看到，例如「试试 /admin 后台」）"
            aria-label="给智能体补线索"
            className="min-w-0 flex-1 rounded-xs border border-line bg-surface px-2 py-1 text-xs text-ink placeholder:text-ink-ghost focus:border-accent focus:outline-none"
            disabled={sending}
          />
          <button
            type="submit"
            disabled={!canSend}
            title={sending ? '发送中…' : '把这条线索交给智能体'}
            className={cn(
              'inline-flex shrink-0 items-center gap-1 rounded-xs border px-2 py-1 text-xs transition-ui duration-fast',
              canSend
                ? 'border-accent text-accent-strong hover:bg-accent/10'
                : 'cursor-not-allowed border-line text-ink-ghost',
            )}
          >
            <Send size={12} aria-hidden="true" />
            送出
          </button>
        </form>
      </div>
      {/*
        ⚠️ 错误信息给 `role="alert"`：读屏用户也要知道线索没送出去，
        而不是以为它在排队。文案原样来自后端。
      */}
      {error && (
        <p role="alert" className="text-2xs leading-relaxed text-signal-risk">
          {error}
        </p>
      )}
      {trimmed.length > 450 && (
        <p className="text-2xs text-ink-faint">已 {trimmed.length} / 500 字</p>
      )}
    </div>
  )
}
