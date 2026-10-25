import { ApiError, api, toApiError } from '@/lib/api'
import { useSyncExternalStore } from 'react'

/**
 * 对话页的会话与消息（v0.93）。
 *
 * ## 为什么这是一个 store，而不是页面里的 useState
 *
 * 会话列表要跨页面存活：用户在对话里让模型建了一次扫描，然后点侧栏去
 * 「扫描任务」看它，再点回来 —— 回来时列表还在，不用重新拉。
 * 更重要的是**消息要能续**：同一轮对话里模型连调 3 个工具，中间任何
 * 一步都不该因为重渲染丢上下文。
 *
 * ## 与 `data/skills.ts` 的结构差异（故意不同）
 *
 * 技能库是"读一份清单 + 一份统计"，两个都只读、都在进页面时拉一次。
 * 对话是**写的**：发一条消息会往后追加消息，且追加的是**服务端决定**的
 * 那几条（模型可能回一条文本 + 三张工具卡）。所以这里不做乐观更新 ——
 * 乐观更新意味着前端要预测"服务端会追加几条、什么角色"，那等于把
 * engine 的逻辑复制一份到前端，而它必然会漂。
 *
 * 发消息时界面显示"正在想"，等服务端返回再整体落进 list。
 *
 * ## ⚠️ 一次只允许一轮在跑
 *
 * `sending` 是全局布尔而不是按会话：同一时刻只发一条。
 * 允许并发发两条会让 `seq` 的顺序取决于谁先返回，而列表按 seq 排序 ——
 * 于是消息会**乱序**显示。这不是限制用户，是限制一个本来就没定义的行为。
 */

/** 消息角色。与后端 `CHAT_ROLES` 同源。 */
export type ChatRole = 'user' | 'assistant' | 'tool'

/**
 * 一条消息。
 *
 * ★ `content` 是**已经剥掉动作协议**的正文（后端 `_narration_before_action`
 *   处理过）。前端不需要、也不应该自己去找 `ACTION:` —— 那会把"内部协议
 *   处理"这件事复制一份到浏览器里。
 */
export interface ChatMessage {
  id: string
  seq: number
  role: ChatRole
  content: string
  /** 模型的思考过程。有就显示在一块可折叠区里，不进正文。 */
  reasoning?: string | null
  /** 工具卡：角色是 `tool` 时才有。 */
  toolName?: string | null
  toolArgs?: Record<string, unknown> | null
  /** 工具是否失败。`null`/缺省 = 成功或不是工具消息。 */
  toolFailed?: boolean | null
  createdAt: string

  /**
   * 审批状态。`null`/缺省 = 这条消息与审批无关。
   *
   * ★ `'pending'` 时工具**还没执行过**，`content` 里写的是"等待确认"。
   *   界面必须据此渲染确认按钮，而不是渲染成一张普通工具卡 ——
   *   那样用户会以为它已经跑过了。
   */
  approvalStatus?: 'pending' | 'approved' | 'rejected' | null
  approvalDecidedBy?: string | null
  approvalDecidedAt?: string | null
}

/** 会话元信息（不含消息）。 */
export interface ChatSession {
  id: string
  title: string
  createdBy: string
  createdAt: string
  updatedAt: string
  messageCount: number
  archived: boolean

  // ── v0.94 的六项「对话设置」 ──────────────────────────────
  //
  // ★ 它们出现在**列表**响应里（不只是详情）：侧栏要按 `project` 分组、
  //   要在条目上标出人格/审批状态。为三个小字段让列表逐条再请求详情
  //   是浪费 —— 而侧栏是每次进页面都要渲染的。
  /** 归属项目。`null` = 无项目。 */
  project?: string | null
  /** 人格 id。`null` = 默认。 */
  persona?: string | null
  /** 模型接入点 id。`null` = 平台默认；逐条消息可以用 `providerId` 覆盖。 */
  providerId?: string | null
  /** 有副作用的工具是否要人工确认。 */
  requireApproval?: boolean
  /** 挂载的技能名。`null`/空 = 没挂。 */
  skillNames?: string[] | null
  /** 挂载的 MCP server 名。 */
  mcpServerNames?: string[] | null
}

/** 供界面渲染的一种人格。 */
export interface ChatPersona {
  id: string
  label: string
  summary: string
}

/** 已上传、还没随消息发出去的附件。 */
export interface ChatAttachment {
  id: string
  name: string
  sizeBytes: number
}

/** `GET /chat/sessions/{id}` 的响应。 */
export interface ChatSessionDetail extends ChatSession {
  messages: ChatMessage[]
}

/** `GET /chat/tools` 的一条。 */
export interface ChatTool {
  name: string
  label: string
  description: string
  params: string
  scope: string | null
  /**
   * 按**当前身份**算出来的可用性。
   *
   * ★ 不要在前端按 scope 自己算：判据在后端（`engine._need_scope`），
   *   两处不一致会让界面说"能"而执行时说"不能"。
   */
  available: boolean
}

export type ChatStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface ChatState {
  sessions: ChatSession[]
  /** 当前打开的会话；`null` = 还没选（欢迎屏）。 */
  active: ChatSessionDetail | null
  tools: ChatTool[]
  /** 可选人格（进页面拉一次即可，内容只随代码变）。 */
  personas: ChatPersona[]
  /**
   * 当前会话里已上传、还没发出去的附件。
   *
   * ★ 它跟着**会话**而不是跟着输入框：切走再切回来时，用户传的那几个
   *   文件还在（后端确实还留着，见 `attachments.STAGED_TTL_SECONDS`）。
   *   放在页面状态里会让切换会话把附件"藏起来"，而用户会以为传丢了。
   */
  stagedAttachments: ChatAttachment[]
  status: ChatStatus
  error: ApiError | null
  /** 一轮正在跑。界面据此禁用输入框并显示"正在想"。 */
  sending: boolean
  /** 有一条审批正在提交。界面据此禁用那两个按钮，防重复点。 */
  deciding: boolean
}

const EMPTY: ChatState = {
  sessions: [],
  active: null,
  tools: [],
  personas: [],
  stagedAttachments: [],
  status: 'idle',
  error: null,
  sending: false,
  deciding: false,
}

let state: ChatState = EMPTY

type Listener = () => void
const listeners = new Set<Listener>()
function emit() {
  for (const listener of listeners) listener()
}

/**
 * 只改指定的字段并广播。
 *
 * ★ 必须造**新对象**：`useSyncExternalStore` 用 `Object.is` 比较快照，
 *   就地改字段不会触发重渲染，界面就此停在旧状态 —— 而 store 里
 *   明明已经是新的。那种"数据对了但界面不动"是最难查的一类。
 */
function patch(next: Partial<ChatState>) {
  state = { ...state, ...next }
  emit()
}

function getSnapshot(): ChatState {
  return state
}

/** SSR 快照。本项目是纯 CSR，但 `useSyncExternalStore` 要求给一个。 */
const SERVER_SNAPSHOT: ChatState = EMPTY
function getServerSnapshot(): ChatState {
  return SERVER_SNAPSHOT
}

export function subscribeChat(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** 订阅对话状态。 */
export function useChat(): ChatState {
  return useSyncExternalStore(subscribeChat, getSnapshot, getServerSnapshot)
}

/**
 * 取当前快照。**给测试与非 React 调用点用**。
 *
 * 与 `schedules-store.ts::getSchedulesSnapshot` 同一用途：测试里不能用
 * hook，只能直接读。生产代码（页面）应该用 `useChat()` —— 只读快照不会
 * 触发重渲染，页面里用它等于写死。
 */
export function getChatSnapshot(): ChatState {
  return state
}

function messageOf(error: unknown): ApiError {
  return error instanceof ApiError ? error : toApiError(error)
}

/**
 * 重置成初始态。**给测试用** —— 生产代码里 store 是进程级的，
 * 一个测试跑完不清会让下一个测试看见上一个的会话。
 */
export function resetChatStore(): void {
  state = EMPTY
  emit()
}

// ─────────────────────────────────────────────
// 读取
// ─────────────────────────────────────────────

/**
 * 拉会话列表。失败时**保留**已有列表并挂上 error。
 *
 * ★ 保留的理由与 `loadSkills` 里统计失败那条一致：列表读的是数据库，
 *   大概率成功；把一次瞬时失败变成"列表变空"，用户会以为自己的对话
 *   被删了 —— 那是比"列表没刷新"严重得多的误判。
 */
export async function loadSessions(): Promise<void> {
  patch({ status: state.sessions.length === 0 ? 'loading' : state.status, error: null })
  try {
    const sessions = await api.get<ChatSession[]>('/chat/sessions')
    patch({ sessions, status: 'ready' })
  } catch (error) {
    patch({ status: 'error', error: messageOf(error) })
  }
}

/** 拉工具清单。失败不致命（界面只是少一块能力说明），所以不设 error。 */
export async function loadTools(): Promise<void> {
  try {
    const tools = await api.get<ChatTool[]>('/chat/tools')
    patch({ tools })
  } catch {
    // 故意吞掉：工具清单是说明性的，拿不到不该挡住对话本身。
    // 界面在 tools 为空时不渲染那一块，而不是显示一个错误。
  }
}

/** 打开一个会话（拉详情）。 */
export async function openSession(sessionId: string): Promise<void> {
  patch({ status: 'loading', error: null })
  try {
    // 切会话先把上一个会话的附件清掉：后端按 session 目录找附件，
    // 留着上一个会话的 id 在这里，发出去会读不到（症状是"附件丢了"）。
    clearStagedAttachments()
    const detail = await api.get<ChatSessionDetail>(`/chat/sessions/${sessionId}`)
    patch({ active: detail, status: 'ready' })
  } catch (error) {
    patch({ status: 'error', error: messageOf(error) })
  }
}

// ─────────────────────────────────────────────
// 写
// ─────────────────────────────────────────────

/** 新建一个空会话并打开它。 */
export async function createSession(title?: string): Promise<ChatSession | null> {
  patch({ error: null })
  try {
    const session = await api.post<ChatSession>('/chat/sessions', title ? { title } : {})
    // 列表头插：最新建的排最前（后端按 updatedAt 倒序，这里保持一致）。
    patch({
      sessions: [session, ...state.sessions],
      active: { ...session, messages: [] },
      status: 'ready',
    })
    return session
  } catch (error) {
    patch({ error: messageOf(error) })
    return null
  }
}

/**
 * 发一条消息，把服务端追加的**全部**消息接进 `active`。
 *
 * ## 为什么整轮只 commit 一次
 *
 * 服务端返回的 `messages` 是一次原子追加（一条 user + 若干 assistant/tool）。
 * 逐条 commit 会让界面在中间态闪一下（比如先只看见用户那句话、
 * 工具卡半秒后才出现）。整轮一次落地，视觉上是"想完了，一起给"。
 *
 * ## 返回值而不是抛异常
 *
 * 调用方（页面）需要区分"成功"和"失败"来清输入框：成功才清。
 * 失败时把用户刚敲的字留在框里，否则他要重打一遍 —— 而失败原因
 * 往往只是模型接入点抖了一下。
 */
export async function sendMessage(
  sessionId: string,
  content: string,
  providerId?: string | null,
  explicitTools?: string[],
): Promise<boolean> {
  if (state.sending) return false
  patch({ sending: true, error: null })
  try {
    const body: Record<string, unknown> = { content }
    if (providerId) body.providerId = providerId
    // ★ 附件 id 只在这一条上生效。后端读完正文就删（见 attachments 模块），
    //   所以这里发完必须清空 `stagedAttachments` —— 不清的话界面会继续显示
    //   "已上传 3 个"，而它们在服务端已经不存在了；用户再发一条会看到
    //   "附件读不到"这种莫名其妙的错误。
    const attachmentIds = state.stagedAttachments.map((a) => a.id)
    if (attachmentIds.length > 0) body.attachmentIds = attachmentIds
    // 用户在正文里 @ 的工具。★ 只影响提示词里的一句"优先用这些"，
    //   不限制模型用别的 —— 强行限制会让它在工具不适用时无法退让。
    if (explicitTools && explicitTools.length > 0) body.explicitTools = explicitTools

    const result = await api.post<{
      messages: ChatMessage[]
      awaitingApproval?: string | null
    }>(`/chat/sessions/${sessionId}/messages`, body, {
      // 一轮要等模型想 + 调工具，默认 20 秒不够。
      timeoutMs: 180_000,
    })

    const active = state.active
    if (active && active.id === sessionId) {
      const messages = [...active.messages, ...result.messages]
      patch({
        active: {
          ...active,
          messages,
          messageCount: messages.length,
          // 首条消息会让后端自动生成标题，这里同步一份猜测值 ——
          // 下一次 loadSessions 会用真值覆盖。
          title: active.messageCount === 0 ? deriveTitle(active.title, content) : active.title,
        },
        // 附件已被后端读走并删除，本地也清掉。
        stagedAttachments: attachmentIds.length > 0 ? [] : state.stagedAttachments,
      })
    }

    // 列表里的 updatedAt / messageCount 变了，静默刷新一次列表。
    // 不 await：它不影响这一轮的显示，慢了也不该让输入框卡住。
    void loadSessions()
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  } finally {
    patch({ sending: false })
  }
}

/**
 * 拉人格列表。
 *
 * ★ 失败**不挂 error**：人格是"没有也能用"的东西（默认就是通用助手）。
 *   为它把整页变成错误态，会让一个可选功能拖垮必选功能。
 *   这与 `loadTools` 的取舍不同 —— 工具列表决定界面上显示哪些能力，
 *   读不到就该报错；人格读不到只意味着下拉框是空的。
 */
export async function loadPersonas(): Promise<void> {
  try {
    const personas = await api.get<ChatPersona[]>('/chat/personas')
    patch({ personas })
  } catch {
    // 故意吞掉：见上面 docstring。
  }
}

/**
 * 拉当前会话的暂存附件。
 *
 * ★ 目前后端没有"列出暂存附件"的端点，所以这个函数**只清空**。
 *   保留它是为了在切会话时把上一个会话的附件从界面上抹掉 ——
 *   不抹的话，切到 B 会话还会看到 A 的附件，而发出去时 B 会读不到
 *   （后端按 session 目录找），症状是"附件莫名其妙丢了"。
 */
function clearStagedAttachments(): void {
  if (state.stagedAttachments.length > 0) patch({ stagedAttachments: [] })
}

/**
 * 上传一个附件到当前会话的暂存区。
 *
 * 返回 `null` 表示失败（错误已挂进 `state.error`）。
 *
 * ## 为什么用 `FormData` 而不是 base64
 *
 * 一个 5 MB 的文件 base64 之后是 6.7 MB，而 JSON 里还要再套一层字符串。
 * 而且后端 `attachments.store` 收的是 `bytes`，走 multipart 能让 Starlette
 * 直接流式交给它，不必先整段解码。
 *
 * ## 为什么前端也判大小
 *
 * 后端会拒（`MAX_ATTACHMENT_BYTES`），但那是**传完之后**才拒 ——
 * 5 MB 的文件已经上传完了。在这里先判一次，用户立刻知道不行。
 * ⚠️ 两个数必须一致：这里用的是同一个常量值，改后端要同步改这里。
 */
export async function uploadAttachment(
  sessionId: string,
  file: File,
): Promise<ChatAttachment | null> {
  const MAX_BYTES = 5 * 1024 * 1024
  if (file.size > MAX_BYTES) {
    patch({
      error: {
        code: 'VALIDATION_FAILED',
        message: `文件太大（${Math.round(file.size / 1024)} KB）。单个附件上限 5 MB。`,
      } as ApiError,
    })
    return null
  }
  if (file.size === 0) {
    patch({
      error: { code: 'VALIDATION_FAILED', message: '文件是空的，没有内容可以给模型看。' } as ApiError,
    })
    return null
  }

  const form = new FormData()
  form.append('file', file)
  try {
    const stored = await api.upload<ChatAttachment>(
      `/chat/sessions/${sessionId}/attachments`,
      form,
    )
    // 只在**还是同一个会话**时收进状态：上传是异步的，用户在等待期间
    // 可能已经切走了。收进去会让它在另一个会话的界面上出现。
    if (state.active?.id === sessionId) {
      patch({ stagedAttachments: [...state.stagedAttachments, stored], error: null })
    }
    return stored
  } catch (error) {
    patch({ error: messageOf(error) })
    return null
  }
}

/**
 * 从界面上移除一个暂存附件（**只移除本地引用**，不删服务端文件）。
 *
 * ★ 为什么不删服务端：服务端那个文件会在一小时后被 `sweep` 清掉，
 *   而现在删它需要额外一个 `DELETE` 端点。用户"不想要这个附件了"
 *   这个动作的后果只是"这条消息不带它" —— 本地移除已经足够，
 *   而少一个端点就少一处要维护的东西。
 */
export function dropStagedAttachment(attachmentId: string): void {
  patch({
    stagedAttachments: state.stagedAttachments.filter((a) => a.id !== attachmentId),
  })
}

/**
 * 改会话的六项设置（部分提交）。
 *
 * `partial` 里**只放要改的字段**。注意区分：
 * - 不传某个键 → 不改
 * - 传 `null` → 清空该项
 *
 * ★ 这个区分由后端 `update_settings` 的 `model_fields_set` 实现，
 *   前端只需要"别把没改的字段也塞进去"。所以这里的入参是
 *   `Partial<...>`，调用方按需拼对象。
 */
export interface ChatSettingsPatch {
  project?: string | null
  persona?: string | null
  providerId?: string | null
  requireApproval?: boolean | null
  skillNames?: string[] | null
  mcpServerNames?: string[] | null
}

export async function updateSettings(
  sessionId: string,
  partial: ChatSettingsPatch,
): Promise<boolean> {
  try {
    const updated = await api.patch<ChatSession>(
      `/chat/sessions/${sessionId}/settings`,
      partial,
    )
    patch({
      sessions: state.sessions.map((s) => (s.id === sessionId ? updated : s)),
      active:
        state.active?.id === sessionId
          ? { ...state.active, ...updated, messages: state.active.messages }
          : state.active,
      error: null,
    })
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  }
}

/**
 * 对一条挂起的工具调用做决定。
 *
 * 批准 → 后端执行那个工具并**接着跑完这一轮**，返回新消息。
 * 拒绝 → 只把那条消息改成 `rejected`，不调模型。
 *
 * ★ 返回的 `messages` 要**追加**进 active，不能整体替换：
 *   替换会把这一轮的上下文全丢掉，而用户会看到对话"跳回"上一步。
 */
export async function decideApproval(
  sessionId: string,
  messageId: string,
  approve: boolean,
): Promise<boolean> {
  if (state.deciding) return false
  patch({ deciding: true, error: null })
  try {
    const result = await api.post<{
      messages: ChatMessage[]
      awaitingApproval?: string | null
    }>(
      `/chat/sessions/${sessionId}/messages/${messageId}/approval`,
      { approve },
      // 批准之后要执行工具 + 再调一轮模型，与 sendMessage 同量级。
      { timeoutMs: 180_000 },
    )

    const active = state.active
    if (active && active.id === sessionId) {
      // ★ 按 id 合并而不是盲追加：被审批的那条消息**已经在** active 里了
      //   （它是挂起时落的那条），后端返回的是它改过状态之后的版本。
      //   盲追加会让它在界面上出现两次。
      const byId = new Map(active.messages.map((m) => [m.id, m]))
      for (const message of result.messages) byId.set(message.id, message)
      const messages = [...byId.values()].sort((a, b) => a.seq - b.seq)
      patch({
        active: { ...active, messages, messageCount: messages.length },
      })
    }
    void loadSessions()
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  } finally {
    patch({ deciding: false })
  }
}

/**
 * 前端这一份标题猜测。**只是为了那半秒的显示**，真值由后端给。
 *
 * ⚠️ 别把它当成规则实现：后端的截断规则（去换行、限长）是唯一口径，
 *   这里只是"在列表刷新回来之前别显示空标题"。
 */
function deriveTitle(currentTitle: string, content: string): string {
  if (currentTitle.trim()) return currentTitle
  const oneLine = content.replace(/\s+/g, ' ').trim()
  return oneLine.length > 24 ? `${oneLine.slice(0, 24)}…` : oneLine
}

/** 改标题。 */
export async function renameSession(sessionId: string, title: string): Promise<boolean> {
  try {
    const updated = await api.patch<ChatSession>(`/chat/sessions/${sessionId}`, { title })
    patch({
      sessions: state.sessions.map((s) => (s.id === sessionId ? updated : s)),
      active: state.active?.id === sessionId ? { ...state.active, title: updated.title } : state.active,
    })
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  }
}

/**
 * 归档会话（软删）。
 *
 * ★ 归档之后**从列表里移除**，而不是留在列表里标记成"已归档"：
 *   这个列表的用途是"接着上次继续说"，已归档的不该在这里。
 *   数据没删（后端 `archived=true`），只是不再打扰。
 */
export async function archiveSession(sessionId: string): Promise<boolean> {
  try {
    await api.del<ChatSession>(`/chat/sessions/${sessionId}`)
    patch({
      sessions: state.sessions.filter((s) => s.id !== sessionId),
      active: state.active?.id === sessionId ? null : state.active,
    })
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  }
}
