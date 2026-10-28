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
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { EmptyState } from '@/components/ui/EmptyState'
import { ErrorState } from '@/components/ui/ErrorState'
import { Modal } from '@/components/ui/Modal'
import { SessionAttackChainModal } from '@/components/chat/SessionAttackChainModal'
import { SessionBatchModal } from '@/components/chat/SessionBatchModal'
import { GroupNameDialog } from '@/components/chat/GroupNameDialog'
import { ProcessGroup } from '@/components/chat/ProcessGroup'
import { SessionContextMenu } from '@/components/chat/SessionContextMenu'
import { SessionFindingsModal } from '@/components/chat/SessionFindingsModal'
import { Skeleton } from '@/components/ui/Skeleton'
import { useAuth } from '@/data/auth-store'
import {
  archiveSession,
  createSession,
  decideApproval,
  dropStagedAttachment,
  clearStaleSending,
  interject,
  interruptTurn,
  loadPersonas,
  loadSessions,
  loadTools,
  openSession,
  pinSession,
  purgeSession,
  renameSession,
  sendMessage,
  unarchiveSession,
  updateSettings,
  uploadAttachment,
  useChat,
  type ChatMessage,
  type ChatSession,
  type ChatSessionDetail,
  listTurns,
  resumeTurn,
  type ChatTurnRun,
} from '@/data/chat'
import type { PhasePayload } from '@/data/chat-stream'
import { groupMessageRows } from '@/lib/messageRows'
import {
  buildConversationMarkdown,
  buildConversationMarkdownFileName,
  downloadMarkdownFile,
  type MarkdownDetail,
} from '@/lib/conversationMarkdown'
import { fetchSessionFindings, fetchSessionMessages } from '@/data/session-insight'
import type { FindingListItem } from '@/data/finding'
import { useProjectedProviders } from '@/data/settings-store'
import { ensureToolsLoaded, useTools } from '@/data/tools'
import { useRevealOnExpand } from '@/hooks/useRevealOnExpand'
import { api, errorMessage } from '@/lib/api'
import { cn } from '@/lib/cn'
import {
  Archive,
  BookOpen,
  ChevronDown,
  ChevronRight,
  Folder,
  MessageSquarePlus,
  MessagesSquare,
  MoreHorizontal,
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

/**
 * 「正在想…」那一行该显示什么（v0.100）。
 *
 * ## 为什么它值得一个独立函数 + 独立注释
 *
 * 改造前这一行永远是「正在想…」。而一轮对话可能有 1 次模型调用 +
 * 最多 6 轮工具往返，实测能到 30 秒以上 —— 用户在那段时间里**无法判断**
 * 「它在推进」还是「它卡住了」。这两件事的应对完全不同（等 / 刷新重发）。
 *
 * 所以这里把后端推来的 `phase` 翻成一句人话：
 *   · `thinking` → 第几轮推理（数字让"在推进"看得见）
 *   · `tool`     → 正在跑哪个工具（名字让"它到底在干嘛"看得见）
 *
 * `phase` 为 `null`（刚点发送、还没收到第一帧）时回落到原文案 ——
 * 那一刻确实没有更多信息可说，硬编一个"准备中"反而像卡住了。
 */
export function phaseLabel(phase: PhasePayload | null | { phase: string; round: number; tool?: string; reason?: string; attempt?: number; maxAttempts?: number }): string {
  if (!phase) return '正在想…'
  const round = typeof phase.round === 'number' ? phase.round + 1 : 0
  if (phase.phase === 'resume') {
    // ★ v1.14：模型这一轮没写动作（或交了白卷），服务端正在自动续跑。
    //
    // 为什么要区分 `reason`：被输出上限切断时，模型是"话说了一半"，
    // 用户已经看到半句话；格式错时它是"想好了没说怎么动手"，界面上什么都没有。
    // 两种等待的体验不同，但**都不要在界面暴露协议内部词**（`ACTION`、
    // `finish_reason`）—— 用户只关心"还要不要等"。
    if (phase.reason === 'truncation') {
      return `第 ${round} 轮 · 回复太长被截断了，正在接着写…`
    }
    return `第 ${round} 轮 · 正在让它把话说完…`
  }
  if (phase.phase === 'tool' && phase.tool) {
    return `第 ${round} 轮 · 正在执行 ${phase.tool}…`
  }
  return round > 0 ? `第 ${round} 轮 · 模型在推理…` : '正在想…'
}

export function ChatPage() {
  const {
    sessions,
    archivedSessions,
    active,
    status,
    error,
    sending,
    sendingSessionId,
    personas,
    stagedAttachments,
    deciding,
    // ★ `tools` 是**对话工具**（`GET /chat/tools`，10 个：list_scans /
    //   create_scan / save_skill 一类），不是下面 `useTools()` 那份**内核
    //   技能库**。两者名字都在讲"工具"，混用会让 @补全列出根本点不动的条目。
    tools,
    pendingUserContent,
    // ★ v0.100：流式过程中的两个状态 —— 现在在做什么、用户插了哪些话。
    phase,
    interjections,
    // ★ v0.102：正在流入的正文（还没落库）。
    streamingText,
  } = useChat()
  const { user } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const [draft, setDraft] = useState('')
  const [showList, setShowList] = useState(false)
  // v1.19：服务端说"这一轮可以继续"时，把那个 run 存下来渲染提示条。
  const [stuckTurn, setStuckTurn] = useState<ChatTurnRun | null>(null)
  const [resuming, setResuming] = useState(false)
  //: 最近一次 `listTurns` 的结果。错误条要从中取「最近一条 failed 的 run」做重试。
  const [lastRuns, setLastRuns] = useState<ChatTurnRun[]>([])

  /*
    ★ 从 URL 派生"要打开哪个会话"，而不是在 effect 里 setState。

    与 `SchedulesPage` 的 `creatingDraft ?? searchParams.get('new')` 同一写法：
    先看本地覆盖，没有再看 URL。这样刷新页面/分享链接能回到同一个会话，
    而"刚新建、还没进 URL"那半秒也不会闪一下空屏。
  */
  const wantedId = searchParams.get('session')

  useEffect(() => {
    void loadSessions()
    // ⚠️ 这里有两个同名的 `loadTools`，它们拉的是**不同的东西**：
    //   · `@/data/chat` 的 `loadTools()` → `GET /chat/tools`，是给消息区做“这个模型能干什么”的说明文本；
    //   · `@/data/tools` 的 `ensureToolsLoaded()` → `GET /kernel/{skills,mcp-servers}`，才是下面「技能」「MCP」两个下拉的选项来源。
    //
    // ★ v1.18 修的是后者从来没被调用过：`skillOptions` / `mcpOptions` 读的是 `useTools()` 的快照，
    //   而那个 store 只有工具页会去加载。于是对话页的技能下拉永远是空的（显示“技能库是空的”），
    //   而技能库其实有 77 篇。这类“配了但是没接线”的缺陷在界面上只表现为一句空提示，
    //   很难联想到是加载入口漏了。
    void loadTools()
    void ensureToolsLoaded()
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
  /**
   * ★ v0.100：一次插话正在提交。
   *
   * 与 `sending` 分开：`sending` 是"整轮在跑"（由 store 管），这个是
   * "我刚按下插话、HTTP 还没回来"（几百毫秒）。合成一个会让发送按钮
   * 在整轮期间一直转圈 —— 暗示"点不动"，而它此刻正是**能**点的。
   */
  const [interjecting, setInterjecting] = useState(false)
  /**
   * ★ v1.25：「中断当前步骤」弹窗。
   *
   * `interrupting` 是"中断请求已发出、还在等服务端确认"的那几百毫秒；
   * `interruptNote` 是用户填的补充说明（只在「中断并继续」时有意义）。
   */
  const [interruptOpen, setInterruptOpen] = useState(false)
  const [interruptNote, setInterruptNote] = useState('')
  const [interrupting, setInterrupting] = useState(false)

  /*
    ── v1.15 会话管理（右键菜单 / 批量 / 已归档）────────────────────

    ★ `menuAnchor` 存的是**右键那一刻的视口坐标 + 会话 id**，不是"某条会话
      被选中"。菜单自己算边界要用坐标；而 `sessionId` 让菜单在列表刷新后
      还能确认自己指向谁（刷新会重排，靠索引会指错行）。
  */
  const [menuAnchor, setMenuAnchor] = useState<{
    x: number
    y: number
    sessionId: string
    /* 标题**在右键那一刻**就抄一份：列表刷新后那条会话可能暂时不在数组里，
       而菜单不该因此自己消失（用户会以为"鼠标放上去就没了"）。 */
    title: string
    archived: boolean
    pinned: boolean
    project: string | null
  } | null>(null)
  /** 正在重命名的会话（`null` = 对话框关着）。 */
  const [renameTarget, setRenameTarget] = useState<{ id: string; title: string } | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  const [renaming, setRenaming] = useState(false)
  /** 等待二次确认的危险操作。 */
  const [confirmAction, setConfirmAction] = useState<
    | { kind: 'archive'; id: string; title: string }
    | { kind: 'purge'; id: string; title: string }
    | null
  >(null)
  const [batchOpen, setBatchOpen] = useState(false)
  /* v1.20：新建分组弹窗。`ids` 让同一套 UI 同时服务「单条右键」与「批量勾选」。 */
  const [newGroupFor, setNewGroupFor] = useState<string[] | null>(null)
  const [batchBusy, setBatchBusy] = useState(false)
  /** 「已归档」区是否展开。默认收起 —— 它是低频的"回收站"。 */
  const [archivedOpen, setArchivedOpen] = useState(false)

  /*
    ── v1.16 两个只读洞察视图 ────────────────────────────────────

    ★ 取数放在**事件处理器**里，不放进 effect：本项目的 lint 明确禁止
      `set-state-in-effect`（在 effect 体里同步 setState 会触发级联渲染）。
      菜单点了才取，天然是事件；也顺手解决了"右键别人的会话"这个场景 ——
      `fetchSessionMessages` 不会改 `active`（`openSession` 会）。
  */
  const [chainView, setChainView] = useState<{
    open: boolean
    title: string
    loading: boolean
    error: string | null
    messages: ChatMessage[]
  }>({ open: false, title: '', loading: false, error: null, messages: [] })
  const [findingsView, setFindingsView] = useState<{
    open: boolean
    title: string
    loading: boolean
    error: string | null
    sessionId: string
    scanIds: string[]
    items: FindingListItem[]
  }>({ open: false, title: '', loading: false, error: null, sessionId: '', scanIds: [], items: [] })

  /** 右键时被点的那条会话。找不到时退回右键那一刻的快照，**不返回 null**。 */
  const menuSession = useMemo(() => {
    if (!menuAnchor) return null
    return (
      sessions.find((s) => s.id === menuAnchor.sessionId) ??
      archivedSessions.find((s) => s.id === menuAnchor.sessionId) ?? {
        id: menuAnchor.sessionId,
        title: menuAnchor.title,
        archived: menuAnchor.archived,
        pinned: menuAnchor.pinned,
        project: menuAnchor.project,
      }
    )
  }, [menuAnchor, sessions, archivedSessions])

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

  /**
   * ★ v0.100：能不能按发送。
   *
   * 两种合法动作，**判据不同**：
   *   · 空闲 → 发一条消息（会起一轮，所以要 `sending === false`）
   *   · 跑着 → 插一句话（不起新一轮，所以 `sending === true` **也允许**）
   *
   * 改造前这里写的是 `!sending` —— 于是跑的时候输入框被禁用，
   * 用户的原话「正在想的时候不能插话」说的就是这个。
   */
  /*
    ★ v1.19：进会话时问一次"上一轮是不是断在半路"。

    依赖里带 `sending` 是刻意的：一轮**刚结束**时要复查一次。中断的那一轮
    服务端会落成 `interrupted`，但前端手上没有那个信号 —— 只有等这一轮
    结束后再问一次，提示条才会在用户眼前出现，而不是要等刷新页面。

    拿不到（网络错/接口 404）就当没有：`listTurns` 内部已经吞掉了异常。
  */
  useEffect(() => {
    const sessionId = active?.id
    let alive = true
    if (!sessionId) {
      setStuckTurn(null)
      setLastRuns([])
      return () => {
        alive = false
      }
    }
    void listTurns(sessionId).then((runs) => {
      if (!alive) return
      setLastRuns(runs)
      setStuckTurn(runs.find((run) => run.resumable) ?? null)
    })
    return () => {
      alive = false
    }
  }, [active?.id, sending])

  /**
   * ★ v1.22：手动复查一次"这一轮还能不能继续"。
   *
   * 抽成函数的理由：`handleRetryError` 跑完之后要知道黄条该不该收起来
   * （它可能已经 done 了）。与上面那个 effect 是同一段逻辑，不抽出来就
   * 会漂成两份 —— 而两份口径不一致时的症状是"点了重试，黄条和红条同时
   * 挂在屏幕上"，用户不知道该看哪个。
   */
  async function refreshStuckTurn(sessionId: string) {
    const runs = await listTurns(sessionId)
    // 只在用户还停在同一个会话时才写状态（请求是异步的，期间可能切走）。
    if (active?.id !== sessionId) return
    setLastRuns(runs)
    setStuckTurn(runs.find((run) => run.resumable) ?? null)
  }

  /**
   * ★ v1.22：错误条上「重试」指向哪一轮。
   *
   * 判据：最近一次 `listTurns` 里**状态为 `failed` 的最近那条**。为什么
   * 不是 `interrupted`：`interrupted` 由后台 resumer 自动接手（黄条那条路），
   * 用户不需要点；而 `failed` 是我们已经放弃自动接手的（重试次数用尽或
   * 确定性失败，例如密钥无效），只有人工介入才可能好 —— 那正是这个按钮。
   *
   * 取最近一条而不是全部：重试是按轮次的，一次只推一轮。
   */
  const retryableRunId =
    lastRuns.find((run) => run.status === 'failed')?.id ?? null

  /**
   * ★ v1.24：正在跑的那一轮是不是**当前这个会话**的。
   *
   * 只有它才该把输入框变成「插话」。跑在别的会话里时，当前会话根本没有
   * 在读历史的轮次 —— 插进去的话永远不会被读到（实测事故见 `chat.ts` 里
   * `sendingSessionId` 的注释：A 会话长任务在跑，用户在新建的 B 会话发
   * 「你好」被当成插话，B 里就永远停在「模型在推理…」）。
   */
  const sendingHere = sending && sendingSessionId === active?.id
  /** 有别的会话在跑 —— 全局只允许一轮，这时当前会话不能再开一条。 */
  const sendingElsewhere = sending && !sendingHere

  const canSend =
    draft.trim().length > 0 && active !== null && !sendingElsewhere

  async function handleNew() {
    const session = await createSession()
    if (session) {
      setSearchParams({ session: session.id }, { replace: true })
      setDraft('')
      composerRef.current?.focus()
    }
  }

  /**
   * ★ v1.25：中断当前这一轮。
   *
   * 两个按钮落到同一个端点、两种 mode：
   * - 「中断并继续」（`continue`）：停下当前输出，把补充并入上下文后自动接着跑。
   * - 「彻底停止」（`stop`）：这一轮就地结束，后台不再自动接手。
   */
  async function submitInterrupt(mode: 'continue' | 'stop') {
    if (!active || interrupting) return
    setInterrupting(true)
    try {
      const ok = await interruptTurn(active.id, mode, interruptNote.trim())
      if (ok) {
        setInterruptOpen(false)
        setInterruptNote('')
      }
    } finally {
      setInterrupting(false)
    }
  }

  async function handleSend() {
    if (!canSend || !active) return
    const content = draft.trim()

    // ★★ v0.100：跑着的时候，这一下是**插话**，不是发消息。
    //
    // 两条路径的差别不是措辞：`sendMessage` 会起一整轮（新的模型调用、
    // 新的工具额度），而 `interject` 只往库里写一句话、由正在跑的那一轮
    // 在下一轮开头读到。混用会让用户在模型跑的时候莫名开启第二轮，
    // 而那一轮的上下文与第一轮并行 —— seq 顺序、工具预算都会乱。
    // ★ v1.24：先挡「另一个会话在跑」——这一支绝不能落到下面的 `interject`，
    //   那会把这句话塞进**另一个会话的**轮次里（甚至塞进一个没人读的历史）。
    if (sendingElsewhere) return

    if (sendingHere) {
      setDraft('')
      setInterjecting(true)
      try {
        const outcome = await interject(active.id, content)
        if (outcome === 'no-turn') {
          // ★ v1.24：服务端说这一轮其实早就结束了（我们这边的 `sending`
          //   是过期的 —— done 帧丢了的典型症状）。把过期状态清掉，退回
          //   "正常发送"，否则这句话会被静默吞掉（用户以为发了，没人回答）。
          clearStaleSending()
          const ok = await sendMessage(active.id, content)
          if (!ok) setDraft(content)
        } else if (outcome === 'failed') {
          setDraft(content)
        }
      } finally {
        setInterjecting(false)
      }
      return
    }

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
   * 手动接着跑一轮中断的对话（v1.19）。
   *
   * ★ 为什么要有这个按钮，而不是只靠后台自动接手：后台要等心跳过期
   *   （90 秒）才敢判定"那一轮没人跑了"—— 那个延迟是必须的，否则会把
   *   正在跑的一轮当成死的，造出两轮并发写同一个会话。但用户**眼看着**
   *   对话停住的时候，他知道那一轮已经死了，不需要等 90 秒。
   *
   * 失败时不把提示条收起来：服务端状态没变，用户还能再点一次；
   * 收起来反而会让人以为"点成功了但没反应"。
   */
  async function handleResume() {
    if (!active || !stuckTurn || resuming) return
    setResuming(true)
    try {
      await resumeTurn(active.id, stuckTurn.id)
      setStuckTurn(null)
    } catch {
      /* 保持提示条，让用户能重试 */
    } finally {
      setResuming(false)
    }
  }

  /**
   * ★ v1.22：错误条上的「重试」。
   *
   * 与 `handleResume`（黄条）的区别只在**触发时机**：黄条是服务端主动
   * 告诉我们"这一轮还能继续"（心跳判定），这里是用户在红色错误条上
   * 自己决定再试一次。两者落到同一个端点、同一个"从断点接着跑"的语义。
   */
  async function handleRetryError() {
    const runId = retryableRunId
    if (!active || !runId || resuming) return
    setResuming(true)
    try {
      await resumeTurn(active.id, runId)
      setStuckTurn(null)
    } catch {
      /* 新的失败会经 store 的 `error` 重新挂上错误条，保持可再点 */
    } finally {
      setResuming(false)
      // 无论成败都重新对一次账：这一轮可能已经 done，黄条要收起来。
      void refreshStuckTurn(active.id)
    }
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

  /**
   * 下载会话 Markdown。
   *
   * ★ 一定要**先拉详情**：`sessions` 里只有元信息，没有 `messages`。
   *   拿 `active.messages` 只在"导出的正是当前打开的那个会话"时才对，
   *   而右键菜单对**任意**一条都可用。
   */
  async function handleDownload(id: string, detail: MarkdownDetail) {
    try {
      const full = await api.get<ChatSessionDetail>(`/chat/sessions/${id}`)
      downloadMarkdownFile(
        buildConversationMarkdownFileName(full, detail),
        buildConversationMarkdown(full, detail),
      )
    } catch {
      // 导出失败**不**改全局 error 状态：那会把整页推进错误态，
      // 而用户只是没下到一个文件。静默失败 + 控制台留痕已足够。
      console.warn('导出对话 Markdown 失败')
    }
  }

  function openRename(target: { id: string; title: string }) {
    setRenameTarget(target)
    setRenameDraft(target.title)
  }

  /**
   * 打开「攻击链」：先确保拿到那条会话的**消息**。
   *
   * ★ 当前正在看的会话直接用 `active.messages`，省一次请求；别的会话才去拉。
   *   判断依据是 `active.id`，不是"会话在列表里"——列表里只有元信息。
   */
  async function openAttackChain(session: { id: string; title: string }) {
    const cached = active?.id === session.id ? active.messages : null
    setChainView({ open: true, title: session.title, loading: cached === null, error: null, messages: cached ?? [] })
    if (cached !== null) return
    try {
      const messages = await fetchSessionMessages(session.id)
      setChainView((view) => (view.open ? { ...view, loading: false, messages } : view))
    } catch (error) {
      setChainView((view) =>
        view.open ? { ...view, loading: false, error: errorMessage(error) } : view,
      )
    }
  }

  /** 取关联漏洞。`sessionId` 单独存一份，重试时不用再问调用方要。 */
  async function loadFindings(sessionId: string, title: string) {
    setFindingsView((view) => ({ ...view, open: true, title, loading: true, error: null, sessionId }))
    try {
      const { scanIds, items } = await fetchSessionFindings(sessionId)
      setFindingsView((view) =>
        view.sessionId === sessionId ? { ...view, loading: false, scanIds, items } : view,
      )
    } catch (error) {
      setFindingsView((view) =>
        view.sessionId === sessionId ? { ...view, loading: false, error: errorMessage(error) } : view,
      )
    }
  }

  function openFindings(session: { id: string; title: string }) {
    void loadFindings(session.id, session.title)
  }

  async function submitRename() {
    if (!renameTarget) return
    const next = renameDraft.trim()
    if (!next || next === renameTarget.title) {
      setRenameTarget(null)
      return
    }
    setRenaming(true)
    try {
      await renameSession(renameTarget.id, next)
      setRenameTarget(null)
    } finally {
      setRenaming(false)
    }
  }

  async function handleMoveTo(id: string, project: string | null) {
    await updateSettings(id, { project })
  }

  async function handleBatchMoveTo(ids: string[], project: string | null) {
    setBatchBusy(true)
    try {
      for (const id of ids) await updateSettings(id, { project })
      await loadSessions()
    } finally {
      setBatchBusy(false)
    }
  }

  /**
   * 批量删除（= 归档）。返回失败条数。
   *
   * ★ 逐个循环而不是 batch 端点：与 CyberStrikeAI 的做法一致
   *   （`chat.js:11121`），详见 `SessionBatchModal` 顶部注释。
   */
  /**
   * 新建分组并把 `ids` 放进去（v1.20）。
   *
   * 与 `handleBatchMoveTo` 共用同一条路径 —— 新建分组的最后一步**就是**
   * 一次普通移动，多的只有"名字校验"这一层，而校验在 `GroupNameDialog` 里。
   * 分成两份实现的话，一处改了另一处必然漂开。
   */
  async function handleCreateGroup(ids: string[], name: string) {
    await handleBatchMoveTo(ids, name)
    setNewGroupFor(null)
  }

  async function handleBatchArchive(ids: string[]): Promise<number> {
    setBatchBusy(true)
    let failed = 0
    try {
      for (const id of ids) {
        const ok = await archiveSession(id)
        if (!ok) failed += 1
        if (wantedId === id) setSearchParams({}, { replace: true })
      }
      await loadSessions()
    } finally {
      setBatchBusy(false)
    }
    return failed
  }

  /**
   * 点空态里的「可以这样问」——**真的把它发出去**（v0.102）。
   *
   * ## 修的是什么
   *
   * 原来这里只有 `setDraft(text)` + 聚焦输入框。而空态下 `active === null`，
   * 输入框在整段渲染里是**禁用**的（占位文案「请先新建或打开一个对话」），
   * 所以用户点了引导项之后：草稿写进去了、但看不见也发不出去 ——
   * 表现就是「点了没反应」。
   *
   * ## 为什么直接建会话 + 发送，而不是"先建会话再把草稿填好"
   *
   * 引导项是**整句话的提问**，不是半个模板。用户点它的意图就是"我要问这个"。
   * 还要再点一次发送等于让用户为自己的点击确认一次，那与"引导"的语义相反。
   *
   * ## 失败时不留残骸
   *
   * 建会话失败 → 什么都不做（`createSession` 已经 patch 了 error）。
   * 发送失败 → 会话留着（它已经建好了，用户可以在里面重新问），
   * 但把草稿填回输入框，让那句话不丢。
   *
   * ⚠️ `sending` 期间按钮要禁用（见渲染处），否则连点两下会建出两个会话。
   */
  async function handlePickStarter(text: string) {
    if (sending) return
    const session = await createSession()
    if (!session) return
    setSearchParams({ session: session.id }, { replace: true })
    // 先不填草稿：那句话马上会以乐观气泡的形式出现在消息流里，
    // 再把它留在输入框就是同屏两份。
    setDraft('')
    const ok = await sendMessage(session.id, text)
    if (!ok) setDraft(text)
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
  // ★ v1.25：把过程面（旁白 + 工具）折成折叠块，主对话流只留用户的话与结论。
  const messageRows = useMemo(() => groupMessageRows(messages), [messages])

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
                        onArchive={() => setConfirmAction({ kind: 'archive', id: session.id, title: session.title })}
                        onContextMenu={(anchor) =>
                          setMenuAnchor({
                            ...anchor,
                            sessionId: session.id,
                            title: session.title,
                            archived: Boolean(session.archived),
                            pinned: Boolean(session.pinned),
                            project: session.project ?? null,
                          })
                        }
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
                        onArchive={() => setConfirmAction({ kind: 'archive', id: session.id, title: session.title })}
                        onContextMenu={(anchor) =>
                          setMenuAnchor({
                            ...anchor,
                            sessionId: session.id,
                            title: session.title,
                            archived: Boolean(session.archived),
                            pinned: Boolean(session.pinned),
                            project: session.project ?? null,
                          })
                        }
                      />
                    ))}
                  </ul>
                </section>
              )}

              {/*
                ── 「已归档」区 ────────────────────────────────────

                ★ 默认收起、有条数才出现。它是"回收站"：频率极低，但**必须
                  有入口** —— 否则「删除此对话」对用户就是单向黑洞，而
                  「彻底删除」也没有地方可以点。
              */}
              {archivedSessions.length > 0 && (
                <section className="mt-1 border-t border-line pt-1">
                  <button
                    type="button"
                    onClick={() => setArchivedOpen((open) => !open)}
                    aria-expanded={archivedOpen}
                    className={cn(
                      'flex w-full items-center gap-1 rounded-sm px-2 py-1 text-2xs text-ink-faint',
                      'transition-ui duration-fast hover:text-ink',
                      'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
                    )}
                  >
                    {archivedOpen ? (
                      <ChevronDown size={10} strokeWidth={2} aria-hidden="true" />
                    ) : (
                      <ChevronRight size={10} strokeWidth={2} aria-hidden="true" />
                    )}
                    已归档（{archivedSessions.length}）
                  </button>
                  {archivedOpen && (
                    <ul className="mt-0.5 flex flex-col gap-0.5">
                      {archivedSessions.map((session) => (
                        <SessionRow
                          key={session.id}
                          session={session}
                          archived
                          active={active?.id === session.id}
                          onOpen={() => {
                            setSearchParams({ session: session.id }, { replace: true })
                            setShowList(false)
                          }}
                          onArchive={() => undefined}
                          onContextMenu={(anchor) =>
                          setMenuAnchor({
                            ...anchor,
                            sessionId: session.id,
                            title: session.title,
                            archived: Boolean(session.archived),
                            pinned: Boolean(session.pinned),
                            project: session.project ?? null,
                          })
                        }
                        />
                      ))}
                    </ul>
                  )}
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
              {/*
                ★ 把乐观气泡计入条数。不记的话，点下发送的那几秒里屏幕上
                  右侧明明已经有一句话，右上角却还是「0 条消息」——
                  与空态文案是同一个形状的谎报（数字说没有，画面说已有）。
              */}
              {messages.length + (pendingUserContent !== null ? 1 : 0)} 条消息
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
                    翻技能库、试 MCP 连接。它做完会在下面贴出做了什么。
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
                    onClick={() => void handlePickStarter(text)}
                    /*
                      ★ v0.102：`disabled` 是必须的。
                        点一下会**建一个会话 + 发一轮消息**，连着点两下
                        就是两个会话、两轮并发的模型调用 —— 而用户在界面上
                        只会看到一个（另一个在会话列表里躺着）。
                    */
                    disabled={sending}
                    className={cn(
                      'w-full rounded-sm border border-line bg-surface-sunken px-2.5 py-2 text-left',
                      'text-xs text-ink-muted transition-ui duration-fast',
                      'hover:border-line-strong hover:text-ink',
                      'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
                      'disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:border-line disabled:hover:text-ink-muted',
                    )}
                  >
                    {text}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="mx-auto flex max-w-[70ch] flex-col gap-2.5">
              {/*
                ★ 空态必须把乐观气泡算进来。只看 messages.length 的话，
                  用户点下发送的那 3 秒里，屏幕上会同时出现"这个对话还是空的"
                  和右侧自己刚发出去的那句话 —— 一句话既在又不在。
              */}
              {messages.length === 0 && pendingUserContent === null && (
                <p className="py-6 text-center text-xs text-ink-faint">
                  这个对话还是空的。说点什么，或者回到会话列表挑另一个。
                </p>
              )}
              {messageRows.map((row) =>
                row.kind === 'process' ? (
                  <ProcessGroup key={`p-${row.items[0].id}`} items={row.items} />
                ) : (
                  <MessageRow
                    key={row.message.id}
                    message={row.message}
                    deciding={deciding}
                    onDecide={
                      row.message.approvalStatus === 'pending'
                        ? (approve) => void handleDecide(row.message.id, approve)
                        : undefined
                    }
                  />
                ),
              )}

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

              {/*
                ★ v0.100：跑的过程中用户插的话。

                它们在时间上位于"已有消息之后、模型下一轮之前" ——
                而那正是下一轮模型会读到它们的位置。显示在消息流末尾
                让用户能确认「我这句话真的进去了」（这是插话功能
                唯一需要界面证明的事）。

                与 `pendingUserContent` 分开画：那个是"还没落库"的乐观气泡，
                这个是"已经落库、等模型下一轮看到"的真消息。
                合成一个状态会让"到底发出去没有"变得不可知。
              */}
              {interjections.map((message) => (
                <div key={message.id} className="flex justify-end" data-interjection="true">
                  <div className="max-w-[85%] rounded-md rounded-br-xs bg-accent/8 px-3 py-2">
                    <p className="mb-0.5 text-[11px] text-ink-faint">插话 · 模型下一轮会看到</p>
                    <p className="whitespace-pre-wrap break-words text-sm text-ink">
                      {message.content}
                    </p>
                  </div>
                </div>
              ))}

              {/*
                ★★ v0.102：正在流入的正文。

                ## 为什么它必须在这里（而不是等 message 帧）

                加这一块之前，正文是**整块**出现的：模型在服务端想完、落库、
                再推一帧全文，界面上那块文字一瞬间铺开。用户的原话是
                「消息是一瞬间蹦出一大块」。

                调研三个同类项目（LibreChat / Open WebUI / LobeChat）之后的
                结论是：它们看起来逐字长出来**不是因为有入场动画**，而是
                因为正文本来就逐 token 到达。所以这里做的是让服务端真的
                流起来（`app/chat/holdback.py` 负责不泄漏内部协议），
                而不是给消息行加一个动画盖住症状。

                ## 位置：消息之后、「正在想」之前

                与 `pendingUserContent` 同一条时间线理由 —— 这段文字是
                模型**正在说**的话，它在时间上接着已有消息、并先于转圈消失。

                ## `data-streaming-bubble` 是给端到端验证用的钩子

                没有它的话，验证脚本只能靠"屏幕上有没有非空文本"猜，
                而那分不出"流式气泡还在"与"真消息已经接管了"。
              */}
              {streamingText !== null && (
                <div
                  className="max-w-[85%] whitespace-pre-wrap break-words text-sm text-ink"
                  data-streaming-bubble="true"
                  data-message-row="assistant"
                >
                  {streamingText}
                  {/*
                    生成中的光标。对齐 Open WebUI 的形态
                    （`ResponseMessage.svelte` 里那个 0.125rem 宽的竖条）：
                    它证明"还在写"，而且在文字末尾随长度移动 —— 光靠
                    逐字增加是分不出"流得慢"和"卡住了"的。
                    ⚠️ 只动 opacity（`animate-cursor-pulse`），不动布局，
                    否则每闪一次整段文字会重排。
                  */}
                  <span
                    className="ml-0.5 inline-block h-3.5 w-[0.125rem] animate-cursor-pulse bg-ink-muted align-text-bottom"
                    aria-hidden="true"
                  />
                </div>
              )}

              {sending && (
                <div className="flex items-center gap-1.5 pl-1" role="status" aria-live="polite">
                  <span className="relative flex h-1.5 w-1.5" aria-hidden="true">
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-pill bg-accent opacity-60" />
                    <span className="relative inline-flex h-1.5 w-1.5 rounded-pill bg-accent" />
                  </span>
                  {/*
                    ★ v0.100：这一行不再只是「正在想…」。

                    它现在会说**在做什么、第几轮** —— 因为用户盯着屏幕的
                    那几十秒真正想知道的是「它在推进还是卡住了」，
                    而一个没有信息的转圈回答不了这个问题。
                  */}
                  <span className="text-2xs text-ink-faint">{phaseLabel(phase)}</span>
                </div>
              )}
              <div ref={bottomRef} />
            </div>
          )}
        </div>

        {/* 错误条：只占一行，不弹窗。它不阻止用户继续输入 */}
        {error && (
          <div
            className="shrink-0 border-t border-signal-risk/30 bg-signal-risk/5 px-3 py-1.5"
            data-error-code={error.code}
          >
            <div className="flex items-start gap-2">
              <p role="alert" className="flex-1 text-2xs text-signal-risk">
                {errorMessage(error)}
              </p>
              {/*
                ★ v1.22：重试入口直接放在错误条上。
                  用户看到"密钥无效"之后唯一能做的是去设置页改密钥，
                  然后回来点这里 —— 如果重试入口在别处（或根本没有），
                  他只能把问题重打一遍。服务端会把这一轮从断点接着跑
                  （`/turns/{id}/resume`），不会重复发用户消息。
              */}
              {retryableRunId && (
                <button
                  type="button"
                  data-error-retry={retryableRunId}
                  className="shrink-0 rounded border border-signal-risk/40 px-2 py-0.5 text-2xs text-signal-risk hover:bg-signal-risk/10 disabled:opacity-50"
                  disabled={resuming || sending}
                  onClick={() => void handleRetryError()}
                >
                  {resuming ? '正在重试…' : '重试'}
                </button>
              )}
            </div>
          </div>
        )}

        {/* 中断提示条（v1.19）：服务端说这一轮还能继续，就把"继续"交到用户手上 */}
        {stuckTurn && (
          <div
            className="shrink-0 border-t border-signal-warn/30 bg-signal-warn/5 px-3 py-1.5"
            data-stuck-turn={stuckTurn.id}
          >
            <p className="text-2xs text-signal-warn">
              上一轮没有跑完
              {stuckTurn.lastError ? `（${stuckTurn.lastError}）` : ''}
              ，可以接着做。
            </p>
            <button
              type="button"
              className="mt-1 rounded border border-signal-warn/40 px-2 py-0.5 text-2xs text-signal-warn hover:bg-signal-warn/10 disabled:opacity-50"
              disabled={resuming || sending}
              onClick={() => void handleResume()}
            >
              {resuming ? '正在接着跑…' : '继续这一轮'}
            </button>
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
                !active
                  ? '先新建或打开一个对话'
                  : sendingHere
                    ? '模型正在跑 —— 你现在打的字会在它下一轮开头被看到（Enter 插话）'
                    : sendingElsewhere
                      ? '另一个对话正在跑 —— 等它跑完再发'
                      : '说点什么…（Enter 发送，Shift+Enter 换行，@ 选工具）'
              }
              /*
                ★★ v0.100：跑的时候**不再禁用**输入框。

                改造前这里是 `disabled={!active || sending}` —— 而用户的原话
                正是「正在想的时候不能插话」。禁用的理由当时是
                "同一时刻只发一条"（`sending` 是全局布尔，两条并发会让
                seq 乱序）。但**插话不是发第二条**：它不启新一轮，
                只是往库里写一条 user 消息，由正在跑的那一轮在下一轮
                开头读到（见后端 `interject` 端点的 docstring）。

                所以这里放开输入，但**发送键的语义变了**（见 `canSend` 与
                `handleSend`）：跑的时候 Enter 是"插话"，不是"发消息"。
                文案必须跟着变，否则用户会以为自己在开第二轮。
              */
              disabled={!active}
              aria-label="输入消息"
              className={cn(
                'max-h-40 min-h-[2.5rem] flex-1 resize-y rounded-sm border border-line bg-surface-sunken',
                'px-2.5 py-2 text-sm text-ink placeholder:text-ink-faint',
                'transition-ui duration-fast focus:outline-none focus:ring-2 focus:ring-accent/40',
                'disabled:cursor-not-allowed disabled:opacity-60',
              )}
            />
            {/*
              ★ v1.25：「中断」入口。只在**当前会话**正在跑时出现 ——
                跑在别的会话里时这里根本没有可中断的轮次（见 `sendingElsewhere`）。
            */}
            {sendingHere && (
              <Button
                onClick={() => setInterruptOpen(true)}
                variant="ghost"
                size="sm"
                disabled={interrupting}
                aria-label="中断"
                data-interrupt-open
              >
                中断
              </Button>
            )}
            <Button
              onClick={() => void handleSend()}
              disabled={!canSend}
              /*
                ★ 跑着的时候这个按钮是「插话」，不该显示转圈：
                转圈意味着"这个按钮正在忙、点不动"，而它此刻恰恰**能**点。
                转圈只该出现在真正提交出去的那一下（`interjecting`）。
              */
              loading={interjecting}
              size="sm"
              aria-label={sendingHere ? '插话' : '发送'}
            >
              <Send size={14} strokeWidth={2} aria-hidden="true" />
              {sendingHere ? '插话' : '发送'}
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

      {/* ── v1.15：会话右键菜单 ─────────────────────────────── */}
      <SessionContextMenu
        anchor={menuAnchor}
        archived={Boolean(menuSession?.archived)}
        pinned={Boolean(menuSession?.pinned)}
        projects={projectSuggestions}
        currentProject={menuSession?.project ?? null}
        onRename={() => menuSession && openRename({ id: menuSession.id, title: menuSession.title })}
        onViewAttackChain={() => menuSession && void openAttackChain(menuSession)}
        onViewFindings={() => menuSession && openFindings(menuSession)}
        onTogglePin={() => {
          if (menuSession) void pinSession(menuSession.id, !menuSession.pinned)
        }}
        onDownload={(detail) => {
          if (menuSession) void handleDownload(menuSession.id, detail)
        }}
        onMoveTo={(project) => {
          if (menuSession) void handleMoveTo(menuSession.id, project)
        }}
        onMoveToNew={() => menuSession && setNewGroupFor([menuSession.id])}
        onBatchManage={() => setBatchOpen(true)}
        onArchive={() =>
          menuSession && setConfirmAction({ kind: 'archive', id: menuSession.id, title: menuSession.title })
        }
        onUnarchive={() => {
          if (menuSession) void unarchiveSession(menuSession.id)
        }}
        onPurge={() =>
          menuSession && setConfirmAction({ kind: 'purge', id: menuSession.id, title: menuSession.title })
        }
        onClose={() => setMenuAnchor(null)}
      />

      {/* ── 攻击链 / 关联漏洞（只读洞察） ───────────────────── */}
      <SessionAttackChainModal
        open={chainView.open}
        onClose={() => setChainView((view) => ({ ...view, open: false }))}
        sessionTitle={chainView.title}
        messages={chainView.messages}
        loading={chainView.loading}
        error={chainView.error}
      />
      <SessionFindingsModal
        open={findingsView.open}
        onClose={() => setFindingsView((view) => ({ ...view, open: false }))}
        sessionTitle={findingsView.title}
        scanIds={findingsView.scanIds}
        items={findingsView.items}
        loading={findingsView.loading}
        error={findingsView.error}
        onRetry={() => void loadFindings(findingsView.sessionId, findingsView.title)}
      />

      {/* ── 重命名对话框 ───────────────────────────────────── */}
      {/* ── 中断当前步骤（v1.25）────────────────────────────── */}
      <Modal
        open={interruptOpen}
        onClose={() => setInterruptOpen(false)}
        title="中断当前步骤"
        widthClass="max-w-xl"
        footer={
          <div className="flex flex-wrap justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => setInterruptOpen(false)}>
              关闭
            </Button>
            <Button
              variant="danger"
              size="sm"
              disabled={interrupting}
              data-interrupt-stop
              onClick={() => void submitInterrupt('stop')}
            >
              彻底停止
            </Button>
            <Button
              variant="primary"
              size="sm"
              disabled={interrupting}
              data-interrupt-continue
              onClick={() => void submitInterrupt('continue')}
            >
              中断并继续
            </Button>
          </div>
        }
      >
        <div className="flex flex-col gap-3 text-xs leading-relaxed text-ink-muted">
          <p>
            <span className="font-medium text-ink">中断并继续</span>：他上一条输出会立刻停下，
            你写的说明会并入上下文，然后他会接着做。中途改方向用这个。
          </p>
          <p>
            <span className="font-medium text-ink">彻底停止</span>：这一轮就地结束，后台不会再自动接着跑。
          </p>
          <p className="text-2xs text-ink-faint">
            注意：如果当前正在执行一个工具命令，它要跑完才会停 —— 我们没法中断一个已经在跑的子进程。
          </p>
          <label className="flex flex-col gap-1">
            <span className="text-2xs text-ink-faint">中断说明（可留空）</span>
            <textarea
              value={interruptNote}
              onChange={(event) => setInterruptNote(event.target.value)}
              rows={4}
              aria-label="中断说明"
              placeholder="例如：别扫端口了，先去看 /api/login 的鉴权逻辑"
              className="w-full resize-y rounded-sm border border-line bg-surface px-2.5 py-2 text-sm text-ink placeholder:text-ink-faint focus:outline-none focus:ring-2 focus:ring-accent/40"
            />
          </label>
        </div>
      </Modal>

      <Modal
        open={renameTarget !== null}
        onClose={() => setRenameTarget(null)}
        title="重命名对话"
        widthClass="max-w-md"
        footer={
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => setRenameTarget(null)}>
              取消
            </Button>
            <Button variant="primary" size="sm" disabled={renaming} onClick={() => void submitRename()}>
              保存
            </Button>
          </div>
        }
      >
        <input
          value={renameDraft}
          onChange={(event) => setRenameDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') void submitRename()
          }}
          aria-label="对话标题"
          className="h-9 w-full rounded-sm border border-line bg-surface px-2 text-sm text-ink"
          // 打开就把光标放进输入框、并全选 —— 重命名十次有九次要整条换掉
          autoFocus
          onFocus={(event) => event.currentTarget.select()}
        />
      </Modal>

      {/* ── 批量管理 ───────────────────────────────────────── */}
      <SessionBatchModal
        open={batchOpen}
        sessions={sessions}
        projects={projectSuggestions}
        busy={batchBusy}
        onClose={() => setBatchOpen(false)}
        onMoveTo={handleBatchMoveTo}
        onCreateGroup={handleCreateGroup}
        onArchive={handleBatchArchive}
      />

      {/* ── v1.20：新建分组（右键「移动到分组 → 新建分组…」与批量管理共用） ── */}
      <GroupNameDialog
        open={newGroupFor !== null}
        existing={projectSuggestions}
        busy={batchBusy}
        onConfirm={(name) => void handleCreateGroup(newGroupFor ?? [], name)}
        onCancel={() => setNewGroupFor(null)}
      />

      {/* ── 危险操作二次确认 ───────────────────────────────── */}
      {/*
        ★ 归档也要确认，这一点**偏离**了 `ConfirmDialog` 注释里那张表
          （"归档可撤销 → 不要确认"）。原因是 v0.106/v0.108 之后归档会
          **当场销毁沙箱与附件**：`/work` 里的产物、上传的原始文件都回不来，
          所以它实际上不可撤销。既然不可撤销，就该先问一句 ——
          让用户以为能后悔，比多一次点击糟得多。
      */}
      <ConfirmDialog
        open={confirmAction !== null}
        title={confirmAction?.kind === 'purge' ? '彻底删除这个对话？' : '删除这个对话？'}
        description={
          confirmAction?.kind === 'purge'
            ? `「${confirmAction.title}」及其全部消息将被**永久删除**，无法恢复。`
            : `「${confirmAction?.title ?? ''}」会移入「已归档」，对话里的沙箱文件与上传的附件会被立即清理。`
        }
        confirmLabel={confirmAction?.kind === 'purge' ? '永久删除' : '删除'}
        onCancel={() => setConfirmAction(null)}
        onConfirm={() => {
          const action = confirmAction
          setConfirmAction(null)
          if (!action) return
          if (action.kind === 'purge') {
            void purgeSession(action.id)
          } else {
            void handleArchive(action.id)
          }
        }}
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
  onContextMenu,
  archived = false,
}: {
  session: ChatSession
  active: boolean
  onOpen: () => void
  onArchive: () => void
  /** 右键（以及"⋯"按钮）打开上下文菜单。 */
  onContextMenu: (anchor: { x: number; y: number }) => void
  /** 已归档的行：不显示归档按钮，改由菜单里的「恢复/彻底删除」处理。 */
  archived?: boolean
}) {
  /*
    ★ 菜单按钮给出锚点坐标时，用**按钮自己**的位置而不是 (0,0)：
      键盘用户（Tab 到"⋯"再回车）没有鼠标坐标，用 (0,0) 会让菜单
      出现在窗口左上角，离他按的地方几百像素远。
  */
  function openMenuFromButton(element: HTMLElement) {
    const rect = element.getBoundingClientRect()
    onContextMenu({ x: rect.left, y: rect.bottom })
  }

  return (
    <li>
      <div
        className={cn(
          'group flex items-center gap-1 rounded-sm px-2 py-1.5',
          'transition-ui duration-fast',
          active ? 'bg-surface-raised' : 'hover:bg-surface-raised/60',
        )}
        // 整行都可以右键 —— 目标越大会越好点，而"必须精确点到标题"很烦
        onContextMenu={(event) => {
          event.preventDefault()
          onContextMenu({ x: event.clientX, y: event.clientY })
        }}
      >
        <button
          type="button"
          onClick={onOpen}
          className="min-w-0 flex-1 text-left"
          aria-current={active ? 'page' : undefined}
        >
          <span className="flex items-center gap-1">
            {session.pinned && (
              // 📌 用 Unicode 而不是 SVG：它是**状态标记**，不需要跟主题变色，
              // 而一个 12px 的图标会跟标题抢注意力。与 CyberStrikeAI 一致。
              <span className="shrink-0 text-2xs" title="已置顶" aria-label="已置顶">
                📌
              </span>
            )}
            <span className="truncate text-xs text-ink">{session.title}</span>
          </span>
          <span className="block text-2xs text-ink-faint">{session.messageCount} 条</span>
        </button>
        {/*
          ★ 归档按钮只在悬停/聚焦时出现，但要**始终可聚焦**：
            用 `opacity-0 group-hover:opacity-100` 而不是
            `hidden group-hover:block` —— 后者让键盘用户永远
            拿不到它（display:none 的元素不可聚焦）。
        */}
        {!archived && (
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
        )}
        {/*
          "⋯" 按钮是右键菜单的**可发现入口**：右键本身不可发现 ——
          用户不试就不会知道有这些功能（这正是"你忘记做了"的来源）。
        */}
        <button
          type="button"
          onClick={(event) => openMenuFromButton(event.currentTarget)}
          className={cn(
            'rounded-xs p-1 text-ink-faint opacity-0 transition-ui duration-fast',
            'hover:text-ink focus:opacity-100 group-hover:opacity-100',
            'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
          )}
          aria-label={`打开对话「${session.title}」的菜单`}
          aria-haspopup="menu"
          title="更多操作"
        >
          <MoreHorizontal size={12} strokeWidth={2} aria-hidden="true" />
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
      <div
        className="flex justify-end"
        // ★ v0.100：稳定标记。端到端验证要靠它数「屏幕上有几条消息」，
        //   而在此之前消息行**没有任何可定位的特征** —— 只能靠文案猜，
        //   那种断言在文案改一个字之后就失效。
        //   同时它也让"这条消息在第几位"在 DOM 里可查（`data-seq`）。
        data-message-row="user"
        data-seq={message.seq}
      >
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
    <div className="flex flex-col gap-1" data-message-row="assistant" data-seq={message.seq}>
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
 *
 * ## v0.103：默认收成**一行**
 *
 * 第一版（同一天早些时候）做的是"长输出才折叠、折叠时露前两行"。用户看了
 * 截图说「就是要类似这样的」，对照的是 Claude Code / Codex 的形态：
 * **每条工具调用默认就是一行灰色文字**（`⏺ 运行命令`），想看细节才点开。
 *
 * 两者差的不只是阈值，是**默认值代表谁的意见**：
 *   · 原来那版：默认铺开，长到一定程度才收 —— 默认值是"给你看全"。
 *   · 用户要的：默认收成一行，点了才开 —— 默认值是"别挡着我看结论"。
 *
 * 对话页的主线是**模型说了什么**，工具调用是过程证据。过程证据默认占一行，
 * 是这两种价值的正确配比。所以现在**无条件折叠**，不再按长度分支 ——
 * 也就不存在"多长才算长"这个只能拍脑袋定的阈值。
 *
 * ## 为什么不复用 ReasoningBlock
 *
 * 形态接近（都是一行可展开），但语义不同：思考过程是纯文本，工具卡要表达
 * **成功/失败**、要显示工具名和参数摘要。失败状态必须留在**折叠态**那一行上
 * —— 收起来之后看不见"失败"，用户就会漏掉"这个工具偷偷挂了"。
 */
function ToolCard({ message }: { message: ChatMessage }) {
  const failed = message.toolFailed === true
  const content = message.content ?? ''
  const lineCount = content.split('\n').length
  const [open, setOpen] = useState(false)
  const bodyRef = useRevealOnExpand<HTMLDivElement>(open)

  /*
    摘要行写什么 —— 这是折叠态**唯一**的信息，所以它必须回答
    "这次调用干了什么"，而不是"这是第几条消息"。

    优先用参数里最能说明问题的那一个（命令、路径、名字），没有参数就退回
    工具名本身。不做花哨的动词翻译：翻错了（把 list_challenges 说成"列出
    挑战"）比不翻更糟 —— 用户核对的是工具名和自己给的目标，不是我的概括。
  */
  const argDigest = useMemo(() => {
    const args = message.toolArgs
    if (!args) return ''
    for (const key of ['command', 'cmd', 'path', 'name', 'query', 'url', 'target']) {
      const value = args[key]
      if (typeof value === 'string' && value.trim()) {
        return value.trim().split('\n')[0].slice(0, 80)
      }
    }
    return ''
  }, [message.toolArgs])

  return (
    <div
      // ★ v0.100：工具卡也要能被数出来 —— 端到端验证的核心判据之一就是
      //   「工具卡是一张张出现的，不是最后一次性蹦出来的」。
      data-message-row="tool"
      data-seq={message.seq}
      data-tool-name={message.toolName ?? ''}
      // v0.103：折叠态能被验证脚本单独数出来
      data-tool-collapsed={open ? 'false' : 'true'}
    >
      {/* ── 折叠态 = 一行。整行是按钮，点击范围大、好点中。 ── */}
      <button
        type="button"
        // ★ 稳定钩子：页面里还有"收起会话列表"这类 aria-label 带"收起"的按钮，
        //   用文案选会撞车。测试与验证脚本都认这个属性。
        data-tool-toggle="true"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={cn(
          'flex w-full items-center gap-1.5 rounded-sm px-1 py-0.5 text-left',
          'transition-ui duration-fast',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
          failed ? 'text-signal-risk hover:bg-signal-risk/5' : 'text-ink-faint hover:text-ink-muted',
        )}
      >
        {open ? (
          <ChevronDown size={11} strokeWidth={2} className="shrink-0" aria-hidden="true" />
        ) : (
          <ChevronRight size={11} strokeWidth={2} className="shrink-0" aria-hidden="true" />
        )}
        {failed ? (
          <Wrench size={11} strokeWidth={2} className="shrink-0" aria-hidden="true" />
        ) : (
          <Terminal size={11} strokeWidth={2} className="shrink-0" aria-hidden="true" />
        )}
        <span className="shrink-0 font-mono text-2xs">{message.toolName ?? 'tool'}</span>
        {argDigest && (
          // 参数摘要可能很长：可缩、可截，但不能把工具名挤掉
          <span className="min-w-0 truncate text-2xs text-ink-faint/80">{argDigest}</span>
        )}
        {failed && (
          <span className="shrink-0 rounded-pill bg-signal-risk/15 px-1.5 text-2xs text-signal-risk">
            失败
          </span>
        )}
        {/* 右侧规模提示：收着时告诉用户"点开有多少"，开着时给个收起入口 */}
        <span className="ml-auto shrink-0 text-2xs text-ink-faint/60">
          {open ? '收起' : `${lineCount} 行`}
        </span>
      </button>

      {/* ── 展开态 = 带边框的面板，输出全文 ── */}
      {open && (
        <div
          ref={bodyRef}
          className={cn(
            'mt-1 rounded-sm border px-2.5 py-1.5',
            failed ? 'border-signal-risk/40 bg-signal-risk/5' : 'border-line bg-surface-sunken',
          )}
        >
          <p className="whitespace-pre-wrap break-words text-xs text-ink-muted">{content}</p>
        </div>
      )}
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
