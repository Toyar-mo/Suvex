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
  status: ChatStatus
  error: ApiError | null
  /** 一轮正在跑。界面据此禁用输入框并显示"正在想"。 */
  sending: boolean
}

const EMPTY: ChatState = {
  sessions: [],
  active: null,
  tools: [],
  status: 'idle',
  error: null,
  sending: false,
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
): Promise<boolean> {
  if (state.sending) return false
  patch({ sending: true, error: null })
  try {
    const body: Record<string, unknown> = { content }
    if (providerId) body.providerId = providerId
    const result = await api.post<{ messages: ChatMessage[] }>(
      `/chat/sessions/${sessionId}/messages`,
      body,
      // 一轮要等模型想 + 调工具，默认 20 秒不够。
      { timeoutMs: 180_000 },
    )

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
