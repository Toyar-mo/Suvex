import {
  ApprovalCard,
  ApprovalToggle,
  AttachmentBar,
  MultiSelect,
  PersonaDialog,
  ProjectInput,
  ProviderSelect,
} from '@/components/chat/ChatControls'
import {
  ToolMention,
  parseMentionQuery,
  resolveMentions,
  type MentionQuery,
} from '@/components/chat/ToolMention'
import { Button } from '@/components/ui/Button'
import { EmptyState } from '@/components/ui/EmptyState'
import { ErrorState } from '@/components/ui/ErrorState'
import { Skeleton } from '@/components/ui/Skeleton'
import { useAuth } from '@/data/auth-store'
import {
  archiveSession,
  createSession,
  decideApproval,
  dropStagedAttachment,
  loadPersonas,
  loadSessions,
  loadTools,
  openSession,
  sendMessage,
  updateSettings,
  uploadAttachment,
  useChat,
  type ChatMessage,
  type ChatSession,
} from '@/data/chat'
import { useProjectedProviders } from '@/data/settings-store'
import { useTools } from '@/data/tools'
import { errorMessage } from '@/lib/api'
import { cn } from '@/lib/cn'
import {
  Archive,
  BookOpen,
  ChevronDown,
  ChevronRight,
  Folder,
  MessageSquarePlus,
  MessagesSquare,
  Plug,
  Send,
  Sparkles,
  Terminal,
  UserRound,
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
  const {
    sessions,
    active,
    status,
    error,
    sending,
    personas,
    stagedAttachments,
    deciding,
    // ★ `tools` 是**对话工具**（`GET /chat/tools`，10 个：list_scans /
    //   create_scan / save_skill 一类），不是下面 `useTools()` 那份**内核
    //   技能库**。两者名字都在讲"工具"，混用会让 @补全列出根本点不动的条目。
    tools,
    pendingUserContent,
  } = useChat()
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
    // ★ 人格列表与工具列表分开拉：`loadPersonas` 失败**不挂 error**
    //   （人格是可选功能，读不到只意味着下拉是空的），而 `loadTools`
    //   失败会让整页进错误态。两者的容错级别不同，所以不能并成一个。
    void loadPersonas()
  }, [])

  // 模型接入点：读设置里那份账号，只用**已配置好**的（没配的选了也跑不起来，
  // 而界面上摆一堆跑不起来的选项等于把失败推给用户）。
  const providers = useProjectedProviders()
  const usableProviders = useMemo(
    () => providers.filter((provider) => provider.configured),
    [providers],
  )

  // 技能库与 MCP server 清单：挂载下拉的选项来源。
  const toolsStore = useTools()
  const skillOptions = useMemo(
    () => toolsStore.skills.categories.flatMap((group) => group.skills.map((s) => s.name)),
    [toolsStore.skills],
  )
  const mcpOptions = useMemo(
    () => toolsStore.mcp.servers.map((server) => server.name),
    [toolsStore.mcp],
  )

  // 项目建议：从已有会话里取，去重。这样它自动反映用户实际用过的名字，
  // 不需要单独维护一份项目清单（平台里没有 project 表，见 ChatControls）。
  const projectSuggestions = useMemo(() => {
    const names = new Set<string>()
    for (const session of sessions) {
      if (session.project) names.add(session.project)
    }
    return [...names].sort()
  }, [sessions])

  const [personaOpen, setPersonaOpen] = useState(false)
  const [uploading, setUploading] = useState(false)

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
  /**
   * 输入框里正在输入的 `@提及`。`null` = 不显示补全层。
   *
   * ★ 每次 `onChange` / 光标移动都重算，不做防抖：计算量是
   *   `lastIndexOf` + 一次正则，比一次重渲染便宜得多；一旦防抖，
   *   快速打字时会看到补全层"慢半拍"地追着光标跑。
   */
  const [mention, setMention] = useState<MentionQuery | null>(null)

  /*
    新消息到达时滚到底。

    ⚠️ 依赖是 `active?.messages.length` 而不是 `active`：后者每次
      patch 都换新对象，会让这段在"正在想"的转圈也滚一次 ——
      用户往上翻看历史时会被反复拽回去。
  */
  const messageCount = active?.messages.length ?? 0
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'auto', block: 'end' })
    // ★ 依赖里带上 `pendingUserContent`（v0.97）：乐观气泡是在发送那一刻
    //   插进来的，此时 `messageCount` 还没变 —— 不带它的话气泡会出现在
    //   视口下方而页面不滚，用户以为点了没反应（正是这次要修的症状）。
  }, [messageCount, sending, pendingUserContent])

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
    // ★ 从**原始 draft** 里解析提及，而不是从 trim 后的 content：
    //   两者只差首尾空白，但用同一个来源能避免"编辑器看到的和发出去的
    //   不是同一段字"这类难以复现的偏差。
    const hinted = resolveMentions(draft, tools)
    // ★ 先清输入框再发（v0.97）。原来是 `await sendMessage(...)` 之后才清，
    //   而那一等是整整一轮（模型想 + 可能十几次工具调用，实测能到 30 秒）。
    //   期间输入框里字还在、屏幕上也没有新气泡，用户看到的是"点了发送，
    //   什么都没发生"—— 于是他会再点一次。现在点下即清、即出气泡。
    //   失败时下面 catch 会把原文放回框里，不会丢字。
    setDraft('')
    const ok = await sendMessage(active.id, content, undefined, hinted)
    if (!ok) setDraft(content)
    setMention(null)
  }

  /**
   * 选中补全项：把 `@正在输入` 换成 `@工具名 `。
   *
   * 末尾那个空格是刻意的 —— 换成 `@run_shell` 之后如果还要接别的字，
   * 没有分隔符的话下一次 `parseMentionQuery` 会把新字当成提及的一部分，
   * 补全层会一直挂着不走。
   */
  function handlePickMention(name: string) {
    if (!mention) return
    const caret = composerRef.current?.selectionStart ?? draft.length
    const next = `${draft.slice(0, mention.start)}@${name} ${draft.slice(caret)}`
    setDraft(next)
    setMention(null)
    // 光标放到插入内容之后。`requestAnimationFrame` 是因为受控 textarea
    // 要等 React 把 value 刷进 DOM 之后设 selectionStart 才不会被重置回末尾。
    requestAnimationFrame(() => {
      const el = composerRef.current
      if (!el) return
      const pos = mention.start + name.length + 2
      el.focus()
      el.setSelectionRange(pos, pos)
    })
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

  /** 打开人格弹窗并提交选择。 */
  async function handlePickPersona(personaId: string | null) {
    setPersonaOpen(false)
    if (!active) return
    await updateSettings(active.id, { persona: personaId })
  }

  async function handleUpload(file: File) {
    if (!active) return
    setUploading(true)
    try {
      await uploadAttachment(active.id, file)
    } finally {
      setUploading(false)
    }
  }

  async function handleDecide(messageId: string, approve: boolean) {
    if (!active) return
    await decideApproval(active.id, messageId, approve)
  }

  const messages = active?.messages ?? []

  /*
    按项目分组。

    ★ 「无项目」必须排**最后**且有一个显式标题，而不是当成
      `undefined` 悄悄并进第一组：用户新建的对话默认都没有项目，
      让它排在前面会把真正的项目组挤到下面，而"我分好的东西在哪"
      是分组功能唯一要解决的问题。

    ★ 空项目名不建组：后端把空串归一成 `null`（见 `update_settings`），
      但历史数据里可能有 `""`。这里再挡一次 —— 一个看不见名字的
      分组会让人以为是渲染 bug。
  */
  const grouped = useMemo(() => {
    const buckets = new Map<string, typeof sessions>()
    for (const session of sessions) {
      const key = session.project?.trim() || ''
      const bucket = buckets.get(key)
      if (bucket) bucket.push(session)
      else buckets.set(key, [session])
    }
    const named = [...buckets.entries()]
      .filter(([key]) => key !== '')
      .sort(([a], [b]) => a.localeCompare(b))
    const loose = buckets.get('') ?? []
    return { named, loose }
  }, [sessions])

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
          ) : grouped.named.length === 0 && grouped.loose.length === 0 ? (
            <p className="px-2 py-6 text-center text-xs text-ink-faint">
              还没有对话。点上面的「新对话」开始。
            </p>
          ) : (
            <div className="flex flex-col gap-2">
              {grouped.named.map(([project, items]) => (
                <section key={project}>
                  <h2 className="flex items-center gap-1 px-2 py-1 text-2xs uppercase tracking-wide text-ink-faint">
                    <Folder size={10} strokeWidth={2} aria-hidden="true" />
                    <span className="truncate">{project}</span>
                  </h2>
                  <ul className="flex flex-col gap-0.5">
                    {items.map((session) => (
                      <SessionRow
                        key={session.id}
                        session={session}
                        active={active?.id === session.id}
                        onOpen={() => {
                          setSearchParams({ session: session.id }, { replace: true })
                          setShowList(false)
                        }}
                        onArchive={() => void handleArchive(session.id)}
                      />
                    ))}
                  </ul>
                </section>
              ))}

              {grouped.loose.length > 0 && (
                <section>
                  {/*
                    ★ 「无项目」的标题在**有**命名项目时才出现。
                      只有一堆无项目对话时，顶上一行「无项目」是纯噪音。
                  */}
                  {grouped.named.length > 0 && (
                    <h2 className="px-2 py-1 text-2xs uppercase tracking-wide text-ink-faint">
                      无项目
                    </h2>
                  )}
                  <ul className="flex flex-col gap-0.5">
                    {grouped.loose.map((session) => (
                      <SessionRow
                        key={session.id}
                        session={session}
                        active={active?.id === session.id}
                        onOpen={() => {
                          setSearchParams({ session: session.id }, { replace: true })
                          setShowList(false)
                        }}
                        onArchive={() => void handleArchive(session.id)}
                      />
                    ))}
                  </ul>
                </section>
              )}
            </div>
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

        {/*
          设置条：只在有会话时出现。

          ★ 它读的是**这个会话**的设置（`active.project` 等），不是全局默认值。
            每个会话各自记着自己的项目/人格/模型 —— 一个全局条会让人以为
            改一下所有对话都变了。
        */}
        {active && (
          <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-line px-3 py-1.5">
            <ProjectInput
              value={active.project ?? null}
              suggestions={projectSuggestions}
              onCommit={(project) => void updateSettings(active.id, { project })}
            />

            <button
              type="button"
              onClick={() => setPersonaOpen(true)}
              className={cn(
                'inline-flex items-center gap-1 rounded-sm border border-line px-2 py-1',
                'text-2xs text-ink-muted transition-ui duration-fast',
                'hover:border-accent/40 hover:text-ink',
                'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
              )}
              title="这个人格决定模型扮演什么角色"
            >
              <UserRound size={11} strokeWidth={2} aria-hidden="true" />
              {personas.find((p) => p.id === (active.persona ?? null))?.label ?? '默认'}
            </button>

            <ProviderSelect
              providers={usableProviders}
              value={active.providerId ?? null}
              onChange={(providerId) => void updateSettings(active.id, { providerId })}
            />

            <ApprovalToggle
              enabled={active.requireApproval ?? false}
              onChange={(requireApproval) => void updateSettings(active.id, { requireApproval })}
            />

            <MultiSelect
              label="技能"
              icon={<BookOpen size={12} strokeWidth={2} aria-hidden="true" />}
              options={skillOptions}
              selected={active.skillNames ?? []}
              emptyHint="技能库是空的"
              onChange={(skillNames) => void updateSettings(active.id, { skillNames })}
            />

            <MultiSelect
              label="MCP"
              icon={<Plug size={12} strokeWidth={2} aria-hidden="true" />}
              options={mcpOptions}
              selected={active.mcpServerNames ?? []}
              emptyHint="还没配 MCP server（在工具页加）"
              onChange={(mcpServerNames) => void updateSettings(active.id, { mcpServerNames })}
            />
          </div>
        )}

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
                <MessageRow
                  key={message.id}
                  message={message}
                  deciding={deciding}
                  onDecide={
                    message.approvalStatus === 'pending'
                      ? (approve) => void handleDecide(message.id, approve)
                      : undefined
                  }
                />
              ))}

              {/*
                乐观气泡（v0.97）：用户刚发出去、服务端还没回的那句话。

                ★ 位置固定在"已有消息之后、正在想之前"——那正是它在时间上
                  的位置。画在别处（比如紧跟最后一条）都会让转圈出现在
                  用户消息上方，读起来像"模型先想了、我才问"。

                ★ 用 `role="status"` 的容器之外单独画，不用 MessageRow：
                  那条路要求一个完整的 `ChatMessage`（id/seq/createdAt），
                  伪造那些字段就会引出"真消息回来后按 id 替换失败、
                  界面上留一条消不掉的假气泡"这个失败模式。
              */}
              {pendingUserContent !== null && (
                <div className="flex justify-end" data-pending-bubble="true">
                  <div className="max-w-[85%] rounded-md rounded-br-xs bg-accent/12 px-3 py-2">
                    <p className="whitespace-pre-wrap break-words text-sm text-ink">
                      {pendingUserContent}
                    </p>
                  </div>
                </div>
              )}

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
          {active && (
            /*
              ★ 附件条**始终**渲染（含上传按钮），不只是有附件时。
                只在有附件时才显示上传按钮，会让"怎么传文件"变成一个
                要先传一个文件才能发现的谜。
            */
            <AttachmentBar
              attachments={stagedAttachments}
              uploading={uploading}
              onPick={(file) => void handleUpload(file)}
              onRemove={dropStagedAttachment}
              disabled={sending}
            />
          )}
          {mention && (
            <ToolMention
              tools={tools}
              query={mention}
              onPick={handlePickMention}
              onClose={() => setMention(null)}
            />
          )}
          <div className="mx-auto flex max-w-[70ch] items-end gap-1.5">
            <textarea
              ref={composerRef}
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value)
                setMention(
                  parseMentionQuery(e.target.value, e.target.selectionStart ?? 0),
                )
              }}
              // 光标移动也要重算：用户可能把光标移到前面一个 `@` 后面继续敲。
              // 只在 keyup/click 上做，不做 selectionchange 轮询 —— 后者
              // 对每个字符都触发一次，而这里只需要"用户主动挪了光标"。
              onKeyUp={(e) => {
                if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                  const el = e.currentTarget
                  setMention(parseMentionQuery(el.value, el.selectionStart ?? 0))
                }
              }}
              onClick={(e) => {
                const el = e.currentTarget
                setMention(parseMentionQuery(el.value, el.selectionStart ?? 0))
              }}
              onBlur={() => setMention(null)}
              onKeyDown={(e) => {
                /*
                  Enter 发送、Shift+Enter 换行。
                  ⚠️ 必须同时排除输入法组合态（`e.nativeEvent.isComposing`）：
                    中文用户敲拼音时按 Enter 是"上屏候选词"，不是"发送"。
                    不排除的话，每打一个词就会把半截拼音发出去 —— 这是中文
                    界面上最常见也最恼人的一类 bug。
                */
                /*
                  Esc 先关补全层。不给这条的话，用户想放弃补全只能
                  按方向键把光标挪走，或者把 `@` 删掉。
                */
                if (e.key === 'Escape' && mention) {
                  e.preventDefault()
                  setMention(null)
                  return
                }
                if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault()
                  void handleSend()
                }
              }}
              rows={2}
              placeholder={
                active
                  ? '说点什么…（Enter 发送，Shift+Enter 换行，@ 选工具）'
                  : '先新建或打开一个对话'
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

      <PersonaDialog
        open={personaOpen}
        personas={personas}
        current={active?.persona ?? null}
        onPick={(personaId) => void handlePickPersona(personaId)}
        onClose={() => setPersonaOpen(false)}
      />
    </div>
  )
}

/**
 * 侧栏里的一行会话。
 *
 * 抽出来是因为分组之后它出现两次（命名项目下 / 无项目下），
 * 复制两份必然漂 —— 一处加了悬停样式另一处忘了。
 */
function SessionRow({
  session,
  active,
  onOpen,
  onArchive,
}: {
  session: ChatSession
  active: boolean
  onOpen: () => void
  onArchive: () => void
}) {
  return (
    <li>
      <div
        className={cn(
          'group flex items-center gap-1 rounded-sm px-2 py-1.5',
          'transition-ui duration-fast',
          active ? 'bg-surface-raised' : 'hover:bg-surface-raised/60',
        )}
      >
        <button
          type="button"
          onClick={onOpen}
          className="min-w-0 flex-1 text-left"
          aria-current={active ? 'page' : undefined}
        >
          <span className="block truncate text-xs text-ink">{session.title}</span>
          <span className="block text-2xs text-ink-faint">{session.messageCount} 条</span>
        </button>
        {/*
          ★ 归档按钮只在悬停/聚焦时出现，但要**始终可聚焦**：
            用 `opacity-0 group-hover:opacity-100` 而不是
            `hidden group-hover:block` —— 后者让键盘用户永远
            拿不到它（display:none 的元素不可聚焦）。
        */}
        <button
          type="button"
          onClick={onArchive}
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
}

/** 一条消息。三种角色三种形状。`onDecide` 只在该消息待审批时传。 */
function MessageRow({
  message,
  deciding,
  onDecide,
}: {
  message: ChatMessage
  /** 有一条审批正在提交 —— 禁用按钮防重复点。 */
  deciding?: boolean
  onDecide?: (approve: boolean) => void
}) {
  /*
    ★ 待审批的消息**优先**按审批卡渲染，不看 role。

      它 role 是 `tool`，但工具还没执行过。当普通工具卡画出来，
      用户在界面上看到的就是"它跑完了" —— 而实际上它在等一句话。
      这个分支必须排在 role 判断之前。
  */
  if (message.approvalStatus === 'pending' && onDecide) {
    return <ApprovalCard message={message} onDecide={onDecide} disabled={deciding} />
  }

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
