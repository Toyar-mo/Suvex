import { Button } from '@/components/ui/Button'
import { EmptyState } from '@/components/ui/EmptyState'
import { ErrorState } from '@/components/ui/ErrorState'
import { Skeleton } from '@/components/ui/Skeleton'
import { useAuth } from '@/data/auth-store'
import {
  archiveSession,
  createSession,
  loadSessions,
  loadTools,
  openSession,
  sendMessage,
  useChat,
  type ChatMessage,
} from '@/data/chat'
import { errorMessage } from '@/lib/api'
import { cn } from '@/lib/cn'
import {
  Archive,
  ChevronDown,
  ChevronRight,
  MessageSquarePlus,
  MessagesSquare,
  Send,
  Sparkles,
  Terminal,
  Wrench,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

/**
 * 对话页（v0.93）。
 *
 * ## 这一页要解决什么
 *
 * 前面每一页都是**表单**：填参数、点按钮、等结果。而安全工作的实际节奏
 * 是"我先问一句、它查一下、我再追问"。表单撑不起这个过程 —— 用户要来回
 * 在四五个页面之间跳，然后自己把结果拼起来。
 *
 * 对话页让模型**直接动手**：它手上有四个工具（建扫描、看扫描、写技能、
 * 试 MCP 连接），用户说一句话，它调工具，结果回到同一个对话框里。
 *
 * ## 版面：左会话清单 + 右消息流（fillsViewport）
 *
 * 与「智能体活动」同一个形态，理由也相同：它是一块**工作台**，不是一篇
 * 文档。页面头（大标题 + 说明）会占掉 80px 且滚动时完全无用；
 * 消息流要的是一个自包含的滚动区，滚到顶/底不该带动整页。
 *
 * ## 三种消息长什么样
 *
 * | 角色 | 显示 |
 * |---|---|
 * | `user` | 右对齐气泡，深底 |
 * | `assistant` | 左对齐，正文 + 可折叠的"思考过程" |
 * | `tool` | 一张卡：工具名 + 参数 + 结果摘要，失败时变红 |
 *
 * 工具卡是这一页与普通聊天最大的差别：用户必须能看见**它到底做了什么**，
 * 否则一次"帮你建了扫描"和一次"我以为我建了其实没建"在界面上长得一样。
 *
 * ## ⚠️ 不显示原始动作协议
 *
 * `content` 是后端剥掉 `ACTION: {...}` 之后的正文字。前端**不要**再去
 * 找 `ACTION:` —— 那会把"内部协议处理"复制一份到浏览器里，而且两份
 * 逻辑必然会漂。见 `app/chat/engine.py::_narration_before_action`。
 */

/** 首条未选中时显示的引导。写清楚它能干什么，而不是"开始对话吧"。 */
const STARTERS = [
  '我上次扫了什么？有没有失败的？',
  '帮我对 example.com 发起一次 web 扫描',
  '现在手上有哪些技能？哪几篇没被用过？',
  '帮我加一个 MCP 工具连接',
]

export function ChatPage() {
  const { sessions, active, status, error, sending } = useChat()
  const { user } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const [draft, setDraft] = useState('')
  const [showList, setShowList] = useState(false)

  /*
    ★ 从 URL 派生"要打开哪个会话"，而不是在 effect 里 setState。

    与 `SchedulesPage` 的 `creatingDraft ?? searchParams.get('new')` 同一写法：
    先看本地覆盖，没有再看 URL。这样刷新页面/分享链接能回到同一个会话，
    而"刚新建、还没进 URL"那半秒也不会闪一下空屏。
  */
  const wantedId = searchParams.get('session')

  useEffect(() => {
    void loadSessions()
    void loadTools()
  }, [])

  /*
    ★ 只在**会话 id 变了**时才拉详情。

    依赖 `[wantedId]` 而不是 `[wantedId, active]`：后者会在 active 更新后
    再触发一次，于是无限拉取。而 `active?.id === wantedId` 守卫让同一会话
    重复渲染时不重复请求。
  */
  useEffect(() => {
    if (!wantedId) return
    if (active?.id === wantedId) return
    void openSession(wantedId)
  }, [wantedId, active?.id])

  const scrollRef = useRef<HTMLDivElement | null>(null)
  const bottomRef = useRef<HTMLDivElement | null>(null)
  const composerRef = useRef<HTMLTextAreaElement | null>(null)

  /*
    新消息到达时滚到底。

    ⚠️ 依赖是 `active?.messages.length` 而不是 `active`：后者每次
      patch 都换新对象，会让这段在"正在想"的转圈也滚一次 ——
      用户往上翻看历史时会被反复拽回去。
  */
  const messageCount = active?.messages.length ?? 0
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'auto', block: 'end' })
  }, [messageCount, sending])

  const canSend = draft.trim().length > 0 && !sending && active !== null

  async function handleNew() {
    const session = await createSession()
    if (session) {
      setSearchParams({ session: session.id }, { replace: true })
      setDraft('')
      composerRef.current?.focus()
    }
  }

  async function handleSend() {
    if (!canSend || !active) return
    const content = draft.trim()
    // 先清输入框再发：失败时 store 里有 error，页面会把内容留在下面一条
    // 错误提示里让用户重发。留字在框里反而会让人以为没发出去。
    const ok = await sendMessage(active.id, content)
    setDraft(ok ? '' : content)
  }

  async function handleArchive(id: string) {
    await archiveSession(id)
    if (wantedId === id) {
      setSearchParams({}, { replace: true })
    }
  }

  function handlePickStarter(text: string) {
    setDraft(text)
    composerRef.current?.focus()
  }

  const messages = active?.messages ?? []
  const grouped = useMemo(() => sessions, [sessions])

  return (
    <div className="flex min-h-0 flex-1">
      {/* ── 左：会话清单（窄屏收进抽屉） ── */}
      <aside
        className={cn(
          'flex w-60 shrink-0 flex-col border-r border-line bg-surface-sunken',
          // 窄屏：覆盖式浮层。`hidden` 而不是 `w-0` —— 后者仍会被读屏器读到
          showList ? 'absolute inset-y-0 left-0 z-30 shadow-lg' : 'hidden md:flex',
        )}
      >
        <div className="flex items-center gap-tight border-b border-line px-3 py-2">
          <Button variant="secondary" size="sm" onClick={handleNew} className="flex-1">
            <MessageSquarePlus size={14} strokeWidth={2} aria-hidden="true" />
            新对话
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setShowList(false)}
            className="md:hidden"
            aria-label="收起会话列表"
          >
            <X size={14} strokeWidth={2} aria-hidden="true" />
          </Button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
          {status === 'loading' && sessions.length === 0 ? (
            <div className="flex flex-col gap-1 p-1">
              <Skeleton className="h-8" />
              <Skeleton className="h-8" />
              <Skeleton className="h-8" />
            </div>
          ) : status === 'error' && sessions.length === 0 ? (
            /*
              ★ 只有**一条都没拿到**时才显示错误块。
                已有列表时的失败只在底部挂一行 error（见下面），
                不把列表换成错误页 —— 用户很可能只是网抖了一下，
                把已看到的对话抹掉比"没刷新"严重得多。
            */
            <div className="p-1.5">
              <ErrorState
                error={error}
                onRetry={() => void loadSessions()}
                title="拿不到会话列表"
                compact
              />
            </div>
          ) : grouped.length === 0 ? (
            <p className="px-2 py-6 text-center text-xs text-ink-faint">
              还没有对话。点上面的「新对话」开始。
            </p>
          ) : (
            <ul className="flex flex-col gap-0.5">
              {grouped.map((session) => {
                const isActive = active?.id === session.id
                return (
                  <li key={session.id}>
                    <div
                      className={cn(
                        'group flex items-center gap-1 rounded-sm px-2 py-1.5',
                        'transition-ui duration-fast',
                        isActive ? 'bg-surface-raised' : 'hover:bg-surface-raised/60',
                      )}
                    >
                      <button
                        type="button"
                        onClick={() => {
                          setSearchParams({ session: session.id }, { replace: true })
                          setShowList(false)
                        }}
                        className="min-w-0 flex-1 text-left"
                        aria-current={isActive ? 'page' : undefined}
                      >
                        <span className="block truncate text-xs text-ink">{session.title}</span>
                        <span className="block text-2xs text-ink-faint">
                          {session.messageCount} 条
                        </span>
                      </button>
                      {/*
                        ★ 归档按钮只在悬停/聚焦时出现，但要**始终可聚焦**：
                          用 `opacity-0 group-hover:opacity-100` 而不是
                          `hidden group-hover:block` —— 后者让键盘用户永远
                          拿不到它（display:none 的元素不可聚焦）。
                      */}
                      <button
                        type="button"
                        onClick={() => void handleArchive(session.id)}
                        className={cn(
                          'rounded-xs p-1 text-ink-faint opacity-0 transition-ui duration-fast',
                          'hover:text-signal-risk focus:opacity-100 group-hover:opacity-100',
                          'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
                        )}
                        aria-label={`归档对话「${session.title}」`}
                        title="归档这个对话"
                      >
                        <Archive size={12} strokeWidth={2} aria-hidden="true" />
                      </button>
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </aside>

      {/* ── 右：消息流 + 输入框 ── */}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        {/* 顶部一行紧凑工具条（替代 80px 页面头） */}
        <div className="flex shrink-0 items-center gap-tight border-b border-line px-3 py-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setShowList(true)}
            className="md:hidden"
            aria-label="打开会话列表"
          >
            <MessagesSquare size={14} strokeWidth={2} aria-hidden="true" />
          </Button>
          <h1 className="min-w-0 flex-1 truncate text-sm text-ink">
            {active?.title ?? '对话'}
          </h1>
          {active && (
            <span className="shrink-0 text-2xs text-ink-faint">
              {messages.length} 条消息
            </span>
          )}
        </div>

        {/* 消息流 */}
        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
          {!active ? (
            <div className="mx-auto flex max-w-[62ch] flex-col items-start gap-bay py-6">
              <EmptyState
                icon={Sparkles}
                title="有什么要查的、要做的？"
                description={
                  <>
                    这个对话框里的模型能直接调用平台的能力：发起扫描、看扫描结果、
                    翻技能库、试 MCP 连接。它做完会在下面贴出**做了什么**。
                  </>
                }
                action={
                  <Button onClick={() => void handleNew()} variant="primary" size="sm">
                    <MessageSquarePlus size={14} strokeWidth={2} aria-hidden="true" />
                    开始新对话
                  </Button>
                }
              />
              <div className="flex w-full flex-col gap-1.5">
                <p className="text-2xs uppercase tracking-wide text-ink-faint">可以这样问</p>
                {STARTERS.map((text) => (
                  <button
                    key={text}
                    type="button"
                    onClick={() => handlePickStarter(text)}
                    className={cn(
                      'w-full rounded-sm border border-line bg-surface-sunken px-2.5 py-2 text-left',
                      'text-xs text-ink-muted transition-ui duration-fast',
                      'hover:border-line-strong hover:text-ink',
                      'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
                    )}
                  >
                    {text}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="mx-auto flex max-w-[70ch] flex-col gap-2.5">
              {messages.length === 0 && (
                <p className="py-6 text-center text-xs text-ink-faint">
                  这个对话还是空的。说点什么，或者回到会话列表挑另一个。
                </p>
              )}
              {messages.map((message) => (
                <MessageRow key={message.id} message={message} />
              ))}

              {sending && (
                <div className="flex items-center gap-1.5 pl-1" role="status" aria-live="polite">
                  <span className="relative flex h-1.5 w-1.5" aria-hidden="true">
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-pill bg-accent opacity-60" />
                    <span className="relative inline-flex h-1.5 w-1.5 rounded-pill bg-accent" />
                  </span>
                  <span className="text-2xs text-ink-faint">正在想…</span>
                </div>
              )}
              <div ref={bottomRef} />
            </div>
          )}
        </div>

        {/* 错误条：只占一行，不弹窗。它不阻止用户继续输入 */}
        {error && (
          <div className="shrink-0 border-t border-signal-risk/30 bg-signal-risk/5 px-3 py-1.5">
            <p role="alert" className="text-2xs text-signal-risk">
              {errorMessage(error)}
            </p>
          </div>
        )}

        {/* 输入框 */}
        <div className="shrink-0 border-t border-line px-3 py-2">
          <div className="mx-auto flex max-w-[70ch] items-end gap-1.5">
            <textarea
              ref={composerRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                /*
                  Enter 发送、Shift+Enter 换行。
                  ⚠️ 必须同时排除输入法组合态（`e.nativeEvent.isComposing`）：
                    中文用户敲拼音时按 Enter 是"上屏候选词"，不是"发送"。
                    不排除的话，每打一个词就会把半截拼音发出去 —— 这是中文
                    界面上最常见也最恼人的一类 bug。
                */
                if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault()
                  void handleSend()
                }
              }}
              rows={2}
              placeholder={
                active ? '说点什么…（Enter 发送，Shift+Enter 换行）' : '先新建或打开一个对话'
              }
              disabled={!active || sending}
              aria-label="输入消息"
              className={cn(
                'max-h-40 min-h-[2.5rem] flex-1 resize-y rounded-sm border border-line bg-surface-sunken',
                'px-2.5 py-2 text-sm text-ink placeholder:text-ink-faint',
                'transition-ui duration-fast focus:outline-none focus:ring-2 focus:ring-accent/40',
                'disabled:cursor-not-allowed disabled:opacity-60',
              )}
            />
            <Button
              onClick={() => void handleSend()}
              disabled={!canSend}
              loading={sending}
              size="sm"
              aria-label="发送"
            >
              <Send size={14} strokeWidth={2} aria-hidden="true" />
              发送
            </Button>
          </div>
          {user?.role !== 'admin' && (
            <p className="mx-auto mt-1 max-w-[70ch] text-2xs text-ink-faint">
              你是只读角色：可以查询和看结果，但不能让它建扫描或改技能库。
            </p>
          )}
        </div>
      </div>
    </div>
  )
}

/** 一条消息。三种角色三种形状。 */
function MessageRow({ message }: { message: ChatMessage }) {
  if (message.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-md rounded-br-xs bg-accent/12 px-3 py-2">
          <p className="whitespace-pre-wrap break-words text-sm text-ink">{message.content}</p>
        </div>
      </div>
    )
  }

  if (message.role === 'tool') {
    return <ToolCard message={message} />
  }

  return (
    <div className="flex flex-col gap-1">
      {message.reasoning && <ReasoningBlock text={message.reasoning} />}
      <div className="rounded-md rounded-bl-xs bg-surface-raised px-3 py-2">
        <p className="whitespace-pre-wrap break-words text-sm text-ink">{message.content}</p>
      </div>
    </div>
  )
}

/**
 * 工具卡：它到底做了什么。
 *
 * ★ 失败必须**看得出来**。一次静默失败（工具返回了错误但界面显示成正常）
 *   会让用户以为"扫描已建"然后去扫描页找一个不存在的任务 —— 这比报错更糟。
 */
function ToolCard({ message }: { message: ChatMessage }) {
  const failed = message.toolFailed === true
  return (
    <div
      className={cn(
        'rounded-sm border px-2.5 py-1.5',
        failed ? 'border-signal-risk/40 bg-signal-risk/5' : 'border-line bg-surface-sunken',
      )}
    >
      <div className="flex items-center gap-1.5">
        {failed ? (
          <Wrench size={12} strokeWidth={2} className="text-signal-risk" aria-hidden="true" />
        ) : (
          <Terminal size={12} strokeWidth={2} className="text-ink-faint" aria-hidden="true" />
        )}
        <span className={cn('font-mono text-2xs', failed ? 'text-signal-risk' : 'text-ink-muted')}>
          {message.toolName ?? 'tool'}
        </span>
        {failed && (
          <span className="rounded-pill bg-signal-risk/15 px-1.5 text-2xs text-signal-risk">
            失败
          </span>
        )}
      </div>
      <p className="mt-1 whitespace-pre-wrap break-words text-xs text-ink-muted">
        {message.content}
      </p>
    </div>
  )
}

/**
 * 可折叠的「思考过程」。
 *
 * ★ 默认折叠。它是"我要看才展开"的东西，不是正文；默认铺开会让每条回复
 *   都变成两屏，而用户大多数时候只关心结论。
 */
function ReasoningBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-sm border border-line/60 bg-surface-sunken/50">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(
          'flex w-full items-center gap-1 px-2 py-1 text-left text-2xs text-ink-faint',
          'transition-ui duration-fast hover:text-ink-muted',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
        )}
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown size={11} strokeWidth={2} aria-hidden="true" />
        ) : (
          <ChevronRight size={11} strokeWidth={2} aria-hidden="true" />
        )}
        思考过程
      </button>
      {open && (
        <p className="whitespace-pre-wrap break-words border-t border-line/60 px-2 py-1.5 text-2xs text-ink-muted">
          {text}
        </p>
      )}
    </div>
  )
}

export default ChatPage
