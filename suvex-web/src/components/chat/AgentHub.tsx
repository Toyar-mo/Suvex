import { useEffect, useState } from 'react'
import { ArrowUpRight, CircleDot, Send, Square, Users } from 'lucide-react'

import { Drawer } from '@/components/ui/Drawer'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/cn'
import { loadSessionPreview, type ChatMessage, type ChatSession } from '@/data/chat'
import { WriteupMarkdown } from '@/components/writeup/WriteupMarkdown'

/**
 * 子代理面板（v1.92）—— Web 端的 Agent Hub。
 *
 * ## 为什么不是又一个会话列表
 *
 * 侧栏已经按父子缩进画出整棵树；这里回答的是**这一次任务**的问题：
 * 「我派出去的那几个调查现在到哪了，能不能给其中一句提示，要不要停掉」。
 * 所以它只列**当前会话的直接子代理**，并且每条都带动作（打开 / 引导 / 停止），
 * 而不是把整棵树再画一遍。
 *
 * ## 为什么打开子代理是切换会话，而不是内嵌一份转录
 *
 * 内嵌预览需要再拉一次消息、再实现一套只读渲染与滚动，且与主对话的
 * 运行态（SSE / 轮询）是两套状态；切过去则复用同一个会话页，历史、
 * 工具卡、中断都与正常对话一致。代价是离开主线 —— 所以子会话顶部
 * 有一条「由某主对话派出」的返回路径。
 */
export function AgentHub({
  open,
  onClose,
  parent,
  children,
  runningIds,
  onOpen,
  onGuide,
  onStop,
}: {
  open: boolean
  onClose: () => void
  /** 主线会话；当前打开的是子代理时，用它作为回退目标。 */
  parent: ChatSession | null
  children: ChatSession[]
  /** 本浏览器正在跑的子会话（服务端 `running` 之外的实时补充）。 */
  runningIds: string[]
  onOpen: (session: ChatSession) => void
  onGuide: (sessionId: string, text: string) => Promise<'ok' | 'no-turn' | 'failed'>
  onStop: (sessionId: string) => void
}) {
  const { t } = useI18n()
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [previews, setPreviews] = useState<Record<string, ChatMessage[]>>({})
  const [previewLoading, setPreviewLoading] = useState<string | null>(null)

  const runningCount = children.filter(
    (child) => child.running || runningIds.includes(child.id),
  ).length

  /*
    展开某个子代理时拉最近 12 条；运行中每 3 秒刷新一次。
    只在面板打开且确实展开时拉 —— 不受"面板常驻但不看"的消耗。
  */
  useEffect(() => {
    if (!open || !expandedId) return
    let alive = true
    const child = children.find((item) => item.id === expandedId)
    const running = Boolean(child?.running || runningIds.includes(expandedId))
    const load = async () => {
      setPreviewLoading(expandedId)
      try {
        const detail = await loadSessionPreview(expandedId)
        if (alive) setPreviews((prev) => ({ ...prev, [expandedId]: detail.messages }))
      } catch {
        // 预览失败不弹错：打开完整对话仍可用。留空态即可。
      } finally {
        if (alive) setPreviewLoading(null)
      }
    }
    void load()
    if (!running) return () => {
      alive = false
    }
    const timer = window.setInterval(() => void load(), 3000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [open, expandedId, children, runningIds])


  const sendGuide = async (session: ChatSession) => {
    const text = (drafts[session.id] ?? '').trim()
    if (!text) return
    setBusy(session.id)
    setNotice(null)
    try {
      const outcome = await onGuide(session.id, text)
      if (outcome === 'ok') {
        setDrafts((prev) => ({ ...prev, [session.id]: '' }))
        setNotice(t('chat.agentHub.sent', { name: displayName(session) }, `已把提示发给 ${displayName(session)}。`))
      } else if (outcome === 'no-turn') {
        setNotice(
          t(
            'chat.agentHub.notRunning',
            undefined,
            '这个子代理已经结束了 —— 打开它可以直接接着问。',
          ),
        )
      }
    } finally {
      setBusy(null)
    }
  }

  return (
    <Drawer
      open={open}
      onClose={onClose}
      labelledBy="agent-hub-title"
      title={
        <span className="flex items-center gap-2">
          <Users size={14} strokeWidth={2} aria-hidden="true" />
          <span id="agent-hub-title">{t('chat.agentHub.title', undefined, '子代理')}</span>
          {children.length > 0 && (
            <span className="text-2xs font-normal text-ink-faint">
              {t(
                'chat.agentHub.count',
                { n: children.length, running: runningCount },
                `${children.length} 个 · ${runningCount} 个运行中`,
              )}
            </span>
          )}
        </span>
      }
      className="w-[26rem] max-w-[92vw]"
    >
      {children.length === 0 ? (
        <div className="flex flex-col gap-2 px-4 py-6 text-sm text-ink-muted">
          <p className="font-medium text-ink">
            {t('chat.agentHub.empty', undefined, '这一轮没有派生子代理。')}
          </p>
          <p className="text-xs leading-relaxed text-ink-faint">
            {t(
              'chat.agentHub.emptyHint',
              undefined,
              '当主对话需要并行调查几件互不依赖的事时，它会用 spawn_subagents 派出子代理；它们会出现在这里。',
            )}
          </p>
        </div>
      ) : (
        <ul className="flex flex-col">
          {children.map((child) => {
            const running = child.running || runningIds.includes(child.id)
            const draft = drafts[child.id] ?? ''
            return (
              <li key={child.id} className="border-b border-line/60 px-4 py-3 last:border-b-0">
                <div className="flex items-start gap-2">
                  <span
                    className={cn(
                      'mt-1.5 size-1.5 shrink-0 rounded-full',
                      running ? 'animate-pulse bg-signal-ok' : 'bg-ink-faint/60',
                    )}
                    aria-hidden="true"
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => setExpandedId(expandedId === child.id ? null : child.id)}
                        className="min-w-0 truncate text-left text-sm text-ink hover:text-accent"
                        aria-expanded={expandedId === child.id}
                        title={child.title}
                      >
                        {displayName(child)}
                      </button>
                      <span
                        className={cn(
                          'shrink-0 text-2xs',
                          running ? 'text-signal-ok' : 'text-ink-faint',
                        )}
                      >
                        {running
                          ? t('chat.agentHub.running', undefined, '运行中')
                          : t('chat.agentHub.idle', undefined, '已结束')}
                      </span>
                    </div>
                    <p className="mt-0.5 flex items-center gap-2 text-2xs text-ink-faint">
                      <CircleDot size={10} strokeWidth={2} aria-hidden="true" />
                      {t(
                        'chat.agentHub.meta',
                        { n: child.messageCount, at: clock(child.updatedAt) },
                        `${child.messageCount} 条消息 · ${clock(child.updatedAt)}`,
                      )}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => onOpen(child)}
                    className="flex shrink-0 items-center gap-1 rounded-sm border border-line px-1.5 py-1 text-2xs text-ink-muted transition-ui duration-fast hover:border-line-strong hover:text-ink"
                  >
                    <ArrowUpRight size={11} strokeWidth={2} aria-hidden="true" />
                    {t('chat.agentHub.open', undefined, '打开')}
                  </button>
                </div>

                {expandedId === child.id && (
                  <div className="mt-2 max-h-[260px] overflow-auto rounded-sm border border-line/60 bg-surface-sunken/30 px-2.5 py-2">
                    {previewLoading === child.id && !previews[child.id] ? (
                      <p className="text-2xs text-ink-faint">
                        {t('chat.agentHub.loading', undefined, '读取转录…')}
                      </p>
                    ) : (previews[child.id]?.length ?? 0) === 0 ? (
                      <p className="text-2xs text-ink-faint">
                        {t('chat.agentHub.noMessages', undefined, '还没有消息。')}
                      </p>
                    ) : (
                      <ul className="flex flex-col gap-1.5">
                        {previews[child.id]?.map((message) => (
                          <li key={message.id} className="text-xs leading-relaxed">
                            {message.role === 'user' ? (
                              <p className="whitespace-pre-wrap break-words text-ink">
                                {message.content}
                              </p>
                            ) : message.role === 'tool' ? (
                              <p
                                className={cn(
                                  'font-mono text-[11px]',
                                  message.toolFailed ? 'text-signal-risk' : 'text-ink-faint',
                                )}
                              >
                                {message.toolName ?? 'tool'}
                                {message.toolFailed ? ' · 失败' : ''}
                              </p>
                            ) : message.surface === 'process' ? (
                              <p className="whitespace-pre-wrap break-words text-ink-faint">
                                {message.content}
                              </p>
                            ) : (
                              <WriteupMarkdown markdown={message.content} />
                            )}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}

                {running && (
                  <form
                    className="mt-2 flex items-center gap-1.5 pl-3.5"
                    onSubmit={(event) => {
                      event.preventDefault()
                      void sendGuide(child)
                    }}
                  >
                    <input
                      value={draft}
                      onChange={(event) =>
                        setDrafts((prev) => ({ ...prev, [child.id]: event.target.value }))
                      }
                      placeholder={t(
                        'chat.agentHub.guidePlaceholder',
                        undefined,
                        '给这个子代理补一句（下一轮生效）',
                      )}
                      className="h-7 min-w-0 flex-1 rounded-sm border border-line bg-surface px-2 text-xs text-ink outline-none focus:border-line-accent"
                    />
                    <button
                      type="submit"
                      disabled={busy === child.id || !draft.trim()}
                      className="rounded-sm border border-line p-1 text-ink-muted transition-ui duration-fast hover:border-line-strong disabled:opacity-40"
                      aria-label={t('chat.agentHub.guide', undefined, '发送提示')}
                    >
                      <Send size={12} strokeWidth={2} aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      onClick={() => onStop(child.id)}
                      className="rounded-sm border border-line p-1 text-signal-risk/80 transition-ui duration-fast hover:border-signal-risk/50"
                      aria-label={t('chat.agentHub.stop', undefined, '停止子代理')}
                    >
                      <Square size={12} strokeWidth={2} aria-hidden="true" />
                    </button>
                  </form>
                )}
              </li>
            )
          })}
        </ul>
      )}

      {notice && (
        <p className="border-t border-line/60 px-4 py-2 text-2xs text-ink-muted">{notice}</p>
      )}

      {parent && (
        <div className="border-t border-line/60 px-4 py-3">
          <button
            type="button"
            onClick={() => onOpen(parent)}
            className="text-2xs text-ink-faint transition-ui duration-fast hover:text-ink"
          >
            {t('chat.agentHub.backToParent', { title: parent.title }, `返回主对话「${parent.title}」`)}
          </button>
        </div>
      )}
    </Drawer>
  )
}

/** 子代理标题统一是 `[子代理] name`；展示时去掉前缀。 */
function displayName(session: ChatSession): string {
  return session.title.replace(/^\[子代理\]\s*/, '') || session.title
}

function clock(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}
