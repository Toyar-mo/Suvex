import { ApiError, api, toApiError } from '@/lib/api'
import { useSyncExternalStore } from 'react'

import {
  TURN_AWAITING,
  TURN_DELTA,
  TURN_DONE,
  TURN_ERROR,
  TURN_MESSAGE,
  TURN_PHASE,
  asChatMessage,
  streamTurn,
  type PhasePayload,
} from './chat-stream'

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
  /**
   * 是否置顶。
   *
   * ★ 列表**已经**按「置顶在前、其余按最近更新」排好序（后端
   *   `service.list_sessions` 的 order_by 就是最终顺序）。这个字段只用来
   *   画条目上那个小旗子、以及切换菜单文案 —— 前端**不要**再自己排一次，
   *   否则判据就有两份，迟早漂。
   */
  pinned?: boolean

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
  /**
   * 已归档的会话（`GET /chat/sessions?includeArchived=true` 减去未归档的）。
   *
   * ★ 与 `sessions` **分两份存**，不是一份再过滤：侧栏的「已归档」是一个
   *   折叠区，只在用户展开时才需要；混进主列表就得在十几个渲染点各过滤
   *   一次，漏一处就会让归档的对话冒到主列表里（那正是归档想避免的）。
   */
  archivedSessions: ChatSession[]
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
  /**
   * 刚发出去、服务端还没回的那句话（v0.97）。
   *
   * ★ 为什么要有它：`sendMessage` 是**整轮一次 commit** —— 服务端返回的
   *   `messages` 里同时包含用户那句话和模型的全部回复，所以用户点发送后，
   *   在他自己那句话上屏幕上要等整整一轮（模型想 + 可能十几次工具调用）。
   *   实测一次能到 30 秒以上，期间界面只有一个小转圈，而输入框里的字
   *   还在（清空也排在 await 之后）—— 用户看到的是"我点了发送，什么都没
   *   发生"，然后就再点一次。
   *
   * ★ 为什么不直接往 `messages` 里插一条假消息：那需要伪造 `id` 与 `seq`，
   *   而真消息回来后要按 `id` 去替换 —— 一旦替换失败（后端改了 id 规则、
   *   或两次发送的乐观 id 撞了），界面上会留下一条永远消不掉的假气泡。
   *   存成独立字段就没有这个失败模式：服务端回包一到，把它置空即可，
   *   真消息由服务端数据自己负责。
   *
   * ★ 渲染位置固定在"已有消息之后、正在想之前"—— 那正是它在时间上的位置。
   */
  pendingUserContent: string | null
  /** 一轮正在跑。界面据此禁用输入框并显示"正在想"。 */
  sending: boolean
  /**
   * ★ v0.100：这一轮**现在在做什么**（`thinking` / `tool`）。
   *
   * ## 为什么它必须与 `sending` 分开
   *
   * `sending` 只回答"有没有在跑"。而用户盯着屏幕的那几十秒想问的是
   * 「它到底在推进还是卡住了」—— 那需要知道是第几轮、是不是在跑某个工具。
   * 合成一个布尔量就只能显示一个没有信息的转圈。
   *
   * `null` = 没有在跑。
   */
  phase: PhasePayload | null
  /**
   * ★ v0.100：跑的过程中**用户插的话**（还没被模型看到的那几句）。
   *
   * ## 为什么它们不能直接进 `messages`
   *
   * 插话是真实落库的消息，但它插在**正在跑的那一轮**中间 ——
   * 而那一轮的 `messages` 由服务端在结束时整体给出。两边同时往里写
   * 会撞 id / 顺序。所以：插话先显示在这里（用户能看见自己的话进去了），
   * 那一轮结束后由 `loadActiveSession` 从服务端拿真消息，这里清空。
   *
   * 这也是为什么插话端点**返回**那条消息：界面用它立刻显示，
   * 而不用等下一轮刷新。
   */
  interjections: ChatMessage[]
  /**
   * ★ v0.102：**正在流入的那段正文**（还没落库，模型还在写）。
   *
   * ## 为什么它是一个独立字段，不进 `messages`
   *
   * 与 `pendingUserContent` 同一条理由（见上）：往 `messages` 里插一条
   * 假消息要伪造 `id`/`seq`，而真消息回来后要按 id 替换 —— 替换失败就会
   * 留下一条永远消不掉的假气泡。
   *
   * 更重要的：这段文字**每来一个增量就变一次**。真让它进 `messages`，
   * 每一帧都会触发整个消息列表的重渲染与滚动计算，而列表里其它消息
   * 一个字节都没变。
   *
   * ## 谁负责清它
   *
   * 收到 `message` 帧（该轮的 assistant 正文落库了）→ 置 `null`，
   * 由那条真消息接管显示。`finally` 里也置 `null`（失败路径）。
   *
   * ⚠️ 不要"收到 done 才清"：一轮里可能有多次模型调用（工具往返），
   * 每次落库的正文都该立刻接替流式缓冲，否则工具往返之间那段文字
   * 会在缓冲和真消息里各显示一次。
   */
  streamingText: string | null
  /** 有一条审批正在提交。界面据此禁用那两个按钮，防重复点。 */
  deciding: boolean
}

const EMPTY: ChatState = {
  sessions: [],
  archivedSessions: [],
  active: null,
  tools: [],
  personas: [],
  stagedAttachments: [],
  status: 'idle',
  error: null,
  pendingUserContent: null,
  sending: false,
  phase: null,
  interjections: [],
  streamingText: null,
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
    /*
      ★ 一次拉**全部**（含已归档），在前端拆成两份，而不是发两次请求。

        两个列表的排序规则完全一致（置顶在前、其余按最近更新），拆请求会让
        两次响应之间的顺序可能不一致；而多一个请求就多一次可能失败的机会。
        拆的动作是纯过滤，没有额外信息量。
    */
    const all = await api.get<ChatSession[]>('/chat/sessions?includeArchived=true')
    patch({
      sessions: all.filter((s) => !s.archived),
      archivedSessions: all.filter((s) => s.archived),
      status: 'ready',
    })
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

/**
 * 只把「当前打开的那个会话」的消息列表重新拉一遍。
 *
 * ## 和 `openSession` 的区别
 *
 * `openSession` 会整块替换 `active`（标题、计数、消息全换），还会顺手清
 * 附件 —— 它的语义是"切会话"。这里只补消息，因为调用它的场景是**同一
 * 个会话正在看的时候**（插话那一轮刚结束），换掉整个对象会把别的状态
 * （比如 `stagedAttachments`）一起冲掉。
 *
 * ## 什么时候需要它
 *
 * 只要服务端产生了一条**引擎不会通过流发出来的** user 消息，前端就看不见
 * 它 —— 典型就是 `interject()`。它是直接调端点写库的，不经引擎，
 * 所以既没有 message 帧，也不在 `active.messages` 里。
 *
 * 失败静默：这是一次补数据的机会，不是用户主动操作。报错会盖掉真正的
 * 错误（比如那一轮本身的失败原因），反而更糊涂。
 */
export async function refreshActiveMessages(sessionId: string): Promise<void> {
  try {
    const detail = await api.get<ChatSessionDetail>(`/chat/sessions/${sessionId}`)
    const current = state.active
    // 等回来的这段时间用户可能已经切走了 —— 那就不要往新会话里写旧数据。
    if (!current || current.id !== sessionId) return
    patch({
      active: { ...current, messages: detail.messages, messageCount: detail.messages.length },
    })
  } catch {
    // 静默（见 docstring）
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
// 执行账目（v1.19）：一轮到底跑完了没有
// ─────────────────────────────────────────────

/**
 * 一轮对话的执行账目。
 *
 * ★ `resumable` 由**服务端**算（`turn_runs.is_resumable`），前端不要自己
 *   按 `status` 再推一遍 —— 那会变成两份判据，且后台接手用的就是服务端
 *   那一份，两边迟早漂开。
 */
export interface ChatTurnRun {
  id: string
  sessionId: string
  status: 'running' | 'interrupted' | 'done' | 'failed'
  resumeAttempts: number
  lastError?: string | null
  startedAt: string
  updatedAt: string
  resumable: boolean
}

/**
 * 列出一个会话最近的执行账目。
 *
 * 失败时返回空数组而不是抛：这是**辅助信息**（一条提示），拿不到就等于
 * "没有中断的轮次"，不该让整个会话打不开。
 */
export async function listTurns(sessionId: string): Promise<ChatTurnRun[]> {
  try {
    return await api.get<ChatTurnRun[]>(`/chat/sessions/${sessionId}/turns`)
  } catch {
    return []
  }
}

/**
 * 手动接着跑一轮中断的执行，并把新落库的消息接进当前会话。
 *
 * ★ 为什么复用 `sendMessage` 那套"整轮只 commit 一次"的做法：接手本质
 *   就是"把这一轮跑完再返回"，服务端返回的就是**新落库的消息**。前端
 *   追加即可，不需要第二条渲染路径。
 */
export async function resumeTurn(
  sessionId: string,
  runId: string,
): Promise<ChatMessage[]> {
  // ★ v1.22：重试前先清掉上一次的错误条。用户点「重试」就是"我处理过了"，
  //   旧错误继续挂着会让人以为这次也是同一个错 —— 而这次可能成功了。
  patch({ error: null })
  try {
    const out = await api.post<{ messages: ChatMessage[] }>(
      `/chat/sessions/${sessionId}/turns/${runId}/resume`,
    )
    const active = state.active
    if (active && active.id === sessionId) {
      patch({ active: { ...active, messages: [...active.messages, ...out.messages] } })
    }
    return out.messages
  } catch (error) {
    // 失败时把新原因摆出来（而不是让调用方自己猜）。
    patch({ error: messageOf(error) })
    throw error
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
  // ★ 让用户那句话**立刻**出现在屏幕上（v0.97）。放在 `sending: true` 同一个
  //   patch 里，所以从用户点下发送到气泡出现的这段时间是一个渲染帧，
  //   而不是一整轮模型推理。
  patch({
    sending: true,
    error: null,
    pendingUserContent: content,
    phase: null,
    interjections: [],
    // ★ v0.102：新的一轮从空缓冲开始。不清的话，上一轮异常退出时
    //   残留的半句话会拼在新一轮的开头（"这个对话还是空的"那种矛盾感）。
    streamingText: null,
  })

  // ★ 附件 id 只在这一条上生效。后端读完正文就删（见 attachments 模块），
  //   所以这里发完必须清空 `stagedAttachments` —— 不清的话界面会继续显示
  //   "已上传 3 个"，而它们在服务端已经不存在了；用户再发一条会看到
  //   "附件读不到"这种莫名其妙的错误。
  const attachmentIds = state.stagedAttachments.map((a) => a.id)

  // ★ v1.21：附件条**在按发送的那一刻**收起来，不再等整轮跑完。
  //
  // 之前是成功之后才清（原「附件已被后端读走并删除，本地也清掉」那句），
  // 于是整轮跑多久、附件条就在输入框上挂多久 —— 用户的原话是
  // 「对话发送了文件还贴在这」。更糟的是这期间输入框已经切成「插话」模式，
  // 那条残留的附件看起来像"下次发送还会带上"，其实这一轮已经读走它了。
  //
  // 失败/异常时在 `finally` 里放回去：上传物在服务端仍然是 staged 的，
  // 放回去用户能直接重发，不用重新选文件。放回用**合并**而不是覆盖 ——
  // 失败前用户可能已经又选了个新附件，覆盖会把它弄丢。
  const stagedBeforeSend = state.stagedAttachments
  if (attachmentIds.length > 0) patch({ stagedAttachments: [] })

  let sentOk = false
  try {
    /**
     * ★★ v0.100：走**流式**端点。
     *
     * ## 改的是什么
     *
     * 之前是 `await api.post(...)` —— 整轮一次回包（模型想 + 最多 6 轮
     * 工具往返，实测 30 秒以上），期间界面上只有一个转圈。
     * 用户原话：「没有思考过程、工具调用、调用结果的显示」。
     *
     * 现在每落一条消息就收到一帧，界面**边跑边长**。
     *
     * ## ★ 乐观气泡怎么退场
     *
     * `pendingUserContent`（v0.97 那个立刻显示的用户气泡）在收到**第一条
     * user 消息帧**时清掉，而不是等整轮结束。它对应的那条真消息
     * 紧接着就进了 `messages` —— 两者不会同屏，也不会在整轮期间留一条
     * 假气泡在真气泡旁边。
     */
    const ok = await streamTurn({
      sessionId,
      content,
      providerId,
      attachmentIds,
      explicitTools,
      onEvent: (frame) => {
        const current = state.active
        if (!current || current.id !== sessionId) return

        if (frame.event === TURN_PHASE) {
          // 「正在想…」那一行现在能显示"第几轮 / 在跑哪个工具"
          patch({ phase: frame.data as unknown as PhasePayload })
          return
        }

        if (frame.event === TURN_MESSAGE) {
          const message = asChatMessage(frame.data)
          if (!message) return
          // 同一条消息可能因为重连/重放来两次 —— 按 id 去重。
          // 不去重的话界面上会出现两张一模一样的卡（而"重复"看起来像模型重复了动作）。
          if (current.messages.some((m) => m.id === message.id)) return
          const messages = [...current.messages, message]
          patch({
            active: {
              ...current,
              messages,
              messageCount: messages.length,
              title:
                current.messageCount === 0 && message.role === 'user'
                  ? deriveTitle(current.title, content)
                  : current.title,
            },
            // 真用户消息到了 → 乐观气泡功成身退（不能等整轮结束：
            // 那会让两句话同屏几十秒）。
            pendingUserContent: message.role === 'user' ? null : state.pendingUserContent,
            /*
              ★ v0.102：assistant 正文落库 → 流式缓冲退场，由真消息接管。
                不清的话这段文字会显示两遍（一次在缓冲里、一次在真消息里）。
                ⚠️ 只清 assistant：tool 帧不该影响正文缓冲（它落的是工具卡，
                而正文可能还在往里流）。
            */
            streamingText: message.role === 'assistant' ? null : state.streamingText,
          })
          return
        }

        if (frame.event === TURN_DELTA) {
          /*
            ★ v0.102：正文增量。**拼接**而不是替换 —— 服务端推的是增量。

            ⚠️ 用 `?? ''` 而不是判断 `typeof`：字段缺失（旧版服务端）时
              应静默忽略这一帧，而不是把 undefined 拼进字符串变成 "undefined"。
          */
          const piece = typeof frame.data.text === 'string' ? frame.data.text : ''
          if (!piece) return
          patch({ streamingText: (state.streamingText ?? '') + piece })
          return
        }

        if (frame.event === TURN_AWAITING) {
          // 停下来等人工确认：清掉 phase，界面据此把输入框让出来并提示
          patch({ phase: null })
          return
        }

        if (frame.event === TURN_ERROR) {
          const detail =
            typeof frame.data.message === 'string' && frame.data.message.trim()
              ? frame.data.message
              : '这一轮失败了，但没有给出原因。'
          // ★ v1.22：必须造**真的** `ApiError`，不能 `{ message } as ApiError`。
          //
          // 原来那句类型断言把服务端发来的真实原因（例如
          // "模型接入点拒绝了这次请求（API key 无效或已过期）…（原始信息：HTTP 401 Invalid token）"）
          // 直接丢掉：`ChatPage` 渲染错误条时走的是
          // `errorMessage(error)`，而它的第一步是 `error instanceof ApiError`
          // —— 一个裸对象不是实例 → 落到"请求失败，请稍后再试"的兜底分支。
          // 用户因此只看到一句没有任何信息量的话（本次缺陷的直接成因）。
          //
          // 404/409 这类流开流前的失败由 `streamTurn` 抛 `ApiError` 处理；
          // 这里补的是**开流之后**才出现的 `error` 帧。
          patch({ error: new ApiError('TURN_FAILED', detail, 200) })
          return
        }

        if (frame.event === TURN_DONE) {
          patch({ phase: null })
        }
      },
    })

    if (!ok) {
      // 流里出现了 error 帧（onEvent 已经 patch 过 error）。这里只补一件事：
      // 把 phase 清掉，否则界面会一直显示"正在想"。
      patch({ phase: null })
      return false
    }

    sentOk = true

    /*
     * ★★ v0.101：把「插话」兑现成真消息。
     *
     * ## 为什么必须在这里刷新（这条修复的由来）
     *
     * `interject()` 写库后只把消息塞进本地 `interjections` 暂存区，
     * 界面上显示成「插话 · 模型下一轮会看到」。而这句提示的**有效期
     * 只有那一轮** —— 轮次一结束它就变成假话，因为：
     *
     *   - 引擎在每轮开头 `list_messages` 重读历史，所以它**确实**被模型
     *     看到了（`tests/test_chat_stream.py` 的
     *     `test_interjection_is_seen_by_the_next_round` 钉的正是这条）；
     *   - 但它**不是引擎落库的**，所以引擎不会为它发 message 帧 ——
     *     前端 `active.messages` 里永远没有它。
     *
     * 结果：那一轮结束后，屏幕上留着一条带「模型下一轮会看到」的僵尸
     * 气泡，而服务端的真实消息列表里它已经是一条**普通 user 消息**了。
     * 两边的说法互相矛盾，且要等用户发下一条消息（`sendMessage` 开头
     * 那句 `interjections: []`）才会消失。
     *
     * 修法就是 docstring 里一直写着、但代码里从没做过的那句：
     * 「那一轮结束时从服务端取回真消息，这里清空」。
     */
    const hadInterjection = state.interjections.length > 0
    if (hadInterjection) {
      // 不 await：气泡已经正确退场了（`finally` 里清），刷新只是把真消息
      // 补进列表。await 它会让 `sending` 多挂一个往返，输入框白等。
      void refreshActiveMessages(sessionId)
    }

    // 列表里的 updatedAt / messageCount 变了，静默刷新一次列表。
    // 不 await：它不影响这一轮的显示，慢了也不该让输入框卡住。
    void loadSessions()
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  } finally {
    // ★ v1.21：这一轮没成，把刚才收起来的附件条放回输入区（合并，不覆盖）。
    //
    // ★★ 前置条件是 `pendingUserContent !== null` —— 也就是**一条 user 消息帧
    //   都没收到**。这条判据不是保守，是必需：后端 `run_turn` 一进来就
    //   `attachments.hand_over(...)`，把暂存区的文件**搬**到会话交接目录
    //   （`/work/attachments`）。也就是说只要这一轮真的开始跑了，那些附件
    //   在服务端已经不在暂存区了；此时把 chip 放回去，用户下一条消息会带着
    //   一个查不到的 id 发出去 —— 附件静默丢失，比"要重新选一次文件"糟得多。
    //
    //   而 `pendingUserContent` 恰好是"服务端是否已经开始这一轮"的现成信号：
    //   页面在发送那一刻写它，收到第一条 user 消息帧时清它。所以：
    //   - 请求根本没到后端（网络错、连接被拒）→ 仍在 → 放回去，用户可直接重发；
    //   - 后端已开始（哪怕模型 502 挂了）→ 已清 → 不放回，因为文件已被搬走。
    if (!sentOk && stagedBeforeSend.length > 0 && state.pendingUserContent !== null) {
      const stillThere = new Set(state.stagedAttachments.map((a) => a.id))
      patch({
        stagedAttachments: [
          ...state.stagedAttachments,
          ...stagedBeforeSend.filter((a) => !stillThere.has(a.id)),
        ],
      })
    }

    // ★ 失败路径也要清乐观气泡：那句话没发出去（`pendingUserContent` 留着
    //   会让人以为发出去了，而实际服务端没有这条记录）。页面会把原文放回
    //   输入框，用户在那里能看到并重发。
    //
    // ★ 成功路径此时 `pendingUserContent` 已经是 null（第一条 user 消息帧
    //   清掉了），这里再写一次 null 是幂等的。
    //
    // ★★ v0.101：`interjections` 也在这里清 —— 那条提示「模型下一轮会看到」
    //   只管到这一轮结束。放 `finally` 而不是只在成功路径清，是因为失败
    //   路径上它同样已经过期：插话早就写进库了（`interject` 自己 commit），
    //   这一轮失败不影响那条消息存在，气泡留着只会继续说假话。
    patch({
      sending: false,
      pendingUserContent: null,
      phase: null,
      interjections: [],
      // ★ v0.102：流式缓冲在这里也必须清。失败路径上它留着一段
      //   "写了一半就断了"的正文，界面上会有一条永远停在半句的气泡，
      //   而错误条同时说这一轮失败了 —— 两者矛盾。
      streamingText: null,
    })
  }
}

/**
 * ★ v0.100：在一轮跑着的时候插一句话。
 *
 * ## 它为什么独立于 `sendMessage`
 *
 * `sendMessage` 会起一整轮（新的模型调用、预算、工具额度）。
 * 插话**不起新一轮** —— 它只是往库里写一条 user 消息，正在跑的那一轮
 * 在下一轮开头会重新读历史，于是自然看到它。
 *
 * ## 为什么返回 `boolean` 而不是抛
 *
 * 与 `sendMessage` 同一条纪律：失败时页面要把用户敲的字留在输入框里。
 *
 * ## ⚠️ 插话进 `interjections` 而不是 `active.messages`
 *
 * 那一轮的 `messages` 由服务端在结束时整体给出，两边同时往里写会撞
 * id / 顺序。`interjections` 是"已送出去、模型还没看到"的暂存区，
 * 界面把它显示在消息流末尾（用户能看见自己的话进去了）；
 * 那一轮结束时由刷新从服务端取回真消息，这里清空。
 */
export async function interject(sessionId: string, content: string): Promise<boolean> {
  const trimmed = content.trim()
  if (!trimmed) return false
  try {
    const message = await api.post<ChatMessage>(
      `/chat/sessions/${sessionId}/interject`,
      { content: trimmed },
      { timeoutMs: 15_000 },
    )
    const active = state.active
    if (active && active.id === sessionId) {
      patch({ interjections: [...state.interjections, message] })
    }
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
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
    const archived = await api.del<ChatSession>(`/chat/sessions/${sessionId}`)
    const moving = state.sessions.find((s) => s.id === sessionId)
    patch({
      sessions: state.sessions.filter((s) => s.id !== sessionId),
      /*
        ★ 归档后**当场**挪进「已归档」，而不是等下一次 `loadSessions`。

          有两个理由：① 用户刚点了「删除此对话」，如果侧栏的「已归档」区
          里立刻能看到它，他就知道"东西还在，只是挪走了"；② `purge` 的
          第一道闸就是"必须先归档"，用户要能马上接着点「彻底删除」。
          用服务端返回的对象（带 `archived: true`），不要自己拼一个 ——
          拼的那份缺字段，渲染时会静默少一块。
      */
      archivedSessions: moving ? [{ ...archived, archived: true }, ...state.archivedSessions] : state.archivedSessions,
      active: state.active?.id === sessionId ? null : state.active,
    })
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  }
}

/**
 * 置顶 / 取消置顶。
 *
 * ★ 成功后重新拉列表（`loadSessions`）而不是就地改这一条：
 *   置顶改变的是**整体顺序**（它要挪到最前），就地改只能改这一条的字段，
 *   位置不对用户就会觉得"点了没反应"。重新拉一次是唯一能保证顺序与后端
 *   一致的做法 —— 而这条路径一次只发生在一个用户点菜单的瞬间。
 */
export async function pinSession(sessionId: string, pinned: boolean): Promise<boolean> {
  try {
    await api.post<ChatSession>(`/chat/sessions/${sessionId}/pin`, { pinned })
    await loadSessions()
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  }
}

/**
 * 从「已归档」里恢复。
 *
 * ★ 只恢复**消息**。归档时后端已经把沙箱与附件当场销毁（`archive_session`
 *   里的 `drop_sandbox` / `drop_*`），`/work` 里跑出来的东西和上传的原始
 *   文件都回不来。所以归档前必须二次确认 —— 界面不能给出"可以后悔"的错觉。
 */
export async function unarchiveSession(sessionId: string): Promise<boolean> {
  try {
    await api.post<ChatSession>(`/chat/sessions/${sessionId}/unarchive`, {})
    await loadSessions()
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  }
}

/**
 * 彻底删除（**物理删除**，不可恢复）。
 *
 * 后端有一道闸：会话必须**已归档**，否则 409。界面上「彻底删除」只出现在
 * 「已归档」区的条目上，所以正常路径不会撞到那道闸 —— 留着它是为了防
 * 有人直接调 API。
 */
export async function purgeSession(sessionId: string): Promise<boolean> {
  try {
    await api.post<ChatSession>(`/chat/sessions/${sessionId}/purge`, {})
    patch({
      archivedSessions: state.archivedSessions.filter((s) => s.id !== sessionId),
      active: state.active?.id === sessionId ? null : state.active,
    })
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  }
}
