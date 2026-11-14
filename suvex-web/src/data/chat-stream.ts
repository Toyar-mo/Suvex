import { API_BASE_URL, API_PREFIX } from '@/lib/api'
import { activeLocale } from '@/i18n'

import type { ChatMessage } from './chat'

/**
 * 读对话的 SSE 流（v0.100）。
 *
 * ## 为什么不用 `EventSource`
 *
 * `EventSource` 只能发 **GET**，而发一条消息要带正文、附件 id、@ 的工具名。
 * 塞进 query string 会撞 URL 长度上限，而且正文会出现在 nginx 访问日志里
 * （可能是用户写的敏感内容）。
 *
 * 所以这里是 **fetch + 手读流**：POST 发请求体，响应是
 * `text/event-stream`，我们用 `ReadableStream` 一帧帧解析。
 *
 * 代价是浏览器**不会自动重连**。对对话来说这是**对的**：
 * 一轮对话没有"补发"的语义 —— 断了就该重发一句，而不是把模型再跑一遍
 * （那会再烧一次 token，而且工具可能有副作用）。
 *
 * ## ★ 为什么要自己解析帧，而不是攒到最后再 JSON.parse
 *
 * 因为要的就是"边跑边出"。攒完再解析等于退化成一次回包 ——
 * 那正是这次改造要解决的问题。
 *
 * 解析规则按 SSE 规范：帧以空行分隔，帧内 `event:` 给类型、`data:` 给载荷
 * （多行 `data:` 要拼成一行，中间用 `\n`）。我们的服务端不产多行
 * `data:`（`stream.encode_frame` 用 JSON 编码，裸换行被转义了），
 * 但按规范解析比假设服务端不会变更稳妥。
 */

/** 一帧流事件。 */
export interface TurnEvent {
  event: string
  data: Record<string, unknown>
}

/**
 * 流里会用到的帧类型。
 *
 * ★ 与后端 `app/chat/stream.py` 里的 `EVENT_*` 常量**必须逐字一致**。
 *   对不上时不会报错 —— 前端只是不认识那个帧，于是**静默少一块界面**。
 */
export const TURN_PHASE = 'phase'
export const TURN_MESSAGE = 'message'
export const TURN_AWAITING = 'awaiting'
export const TURN_DONE = 'done'
export const TURN_ERROR = 'error'

/**
 * ★ v0.102：正文的**增量**。`{ text }` 是要追加到「正在生成那条」尾巴上的字。
 *
 * 加它的原因：`message` 帧推的是**已落库**的整条消息，它是权威的、刷新
 * 还在，但它是**成块**的 —— 模型想完一整段才有得推，界面上那块文字
 * 一瞬间铺开。用户的原话是「消息是一瞬间蹦出一大块」。
 *
 * 调研三个同类项目之后的结论推翻了「加个入场动画」的方案：它们看起来
 * 逐字长出来**不是因为动画**，是因为正文本来就逐 token 到达。
 *
 * 两者的分工：`delta` 负责过程，`message` 仍是结果。收到 `message` 帧时
 * 用它的 content **整体替换**流式缓冲 —— 不用增量拼出最终文本。理由：
 * 服务端落库时对正文做过裁剪（`_narration_before_action`），增量是它的
 * 前缀但不保证逐字节相等（比如首尾空白），拿增量当真值会让界面与刷新后
 * 的界面差一个字。
 */
export const TURN_DELTA = 'delta'

/** 模型思考增量；与正文分开，避免思考混进回答。 */
export const TURN_REASONING = 'reasoning'

/**
 * ★ v1.92：工具执行期间的输出增量。`{ tool, chunk }`。
 *
 * 与 `delta` 的分工：`delta` 是模型正文，这个是沙箱里命令的 stdout。
 * 它只用于「运行中的工具卡」；落库后的工具结果仍由 `message` 帧整体替换 ——
 * 增量可能有解码替换字符，不能当成最终事实。
 */
export const TURN_TOOL_OUTPUT = 'tool_output'

/**
 * 长工具的心跳：`{ tool, seconds }`。不是输出，只证明它还活着。
 * 用于 MCP / verify_http 这类没有可读 stdout 的工具。
 */
export const TURN_TOOL_PROGRESS = 'tool_progress'

/** `delta.data` 的形态。 */
export interface DeltaPayload {
  text: string
}

/** `phase.data` 的形态。 */
export interface PhasePayload {
  /**
   * `resume` 是 v1.14 加的：模型这一轮没写动作（或干脆交了白卷），
   * 服务端正在**自动纠偏续跑**。它不是答案，不落库，只用于告诉用户
   * 「这一轮还没结束，在替你再问一次模型」。
   *
   * 加它的原因：改造前这个位置**一帧都不推**，界面上「正在想…」停住
   * 然后凭空蹦出下一段，用户无法区分「它在自动重试」与「它卡死了」。
   */
  phase: 'thinking' | 'tool' | 'resume'
  round: number
  /** `phase === 'tool'` 时是工具名。 */
  tool?: string
  /** `phase === 'tool'` 时的浅层参数摘要（只含标量，已截断）。 */
  args?: Record<string, string>
  /** `phase === 'resume'` 时：为什么会续跑。 */
  reason?: 'truncation' | 'format' | 'final-check'
  /** `phase === 'resume'` 时：这是第几次纠偏（从 1 起）。 */
  attempt?: number
  /** `phase === 'resume'` 时：最多纠偏几次。 */
  maxAttempts?: number
}

/** `done.data` 的形态。 */
export interface DonePayload {
  messageCount: number
  awaitingApproval: string | null
  hitToolLimit: boolean
}

/**
 * 把一段原始文本切成帧。
 *
 * 导出它是为了能单独测：这是整条流里**唯一**有状态的地方
 * （半帧要留到下一次），也是最容易写错的地方 ——
 * 切早了一帧被丢弃，切晚了一条消息要等下一帧才显示。
 */
export function parseSseChunk(buffer: string): { frames: TurnEvent[]; rest: string } {
  const frames: TurnEvent[] = []
  // 帧之间用空行分隔。统一换行符，避免服务端/代理用 \r\n 时切不开。
  const normalized = buffer.replace(/\r\n/g, '\n')
  const parts = normalized.split('\n\n')
  // 最后一段可能是半帧（还没收到结尾空行）—— 留到下一次。
  const rest = parts.pop() ?? ''

  for (const part of parts) {
    if (!part.trim()) continue
    let event = 'message'
    const dataLines: string[] = []
    for (const line of part.split('\n')) {
      if (line.startsWith(':')) continue // 注释行（心跳）
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
    }
    if (dataLines.length === 0) continue
    try {
      frames.push({ event, data: JSON.parse(dataLines.join('\n')) })
    } catch {
      // 半帧或坏帧：**不要**抛 —— 抛出去会让整条流中断，而界面上
      // 已经显示出来的那些步骤也跟着作废。跳过这一帧继续读。
      continue
    }
  }
  return { frames, rest }
}

/**
 * WebKit（Safari / Tern 内嵌 WKWebView）对 `fetch` 的 ReadableStream
 * 存在缓冲：SSE 帧会攒到连接结束才交给 JS。`EventSource` 走浏览器原生
 * SSE 实现，能逐帧回调，所以只在这种浏览器上切过去。Chromium 继续用
 * POST 流，避免多一次连接。
 */
function prefersPolling(): boolean {
  const ua = typeof navigator === 'undefined' ? '' : navigator.userAgent
  // Tern 的 WebView 会缓冲 fetch SSE；普通 JSON 轮询不受影响。
  return /Edg\//i.test(ua)
}

function prefersEventSource(): boolean {
  if (typeof EventSource === 'undefined') return false
  const ua = typeof navigator === 'undefined' ? '' : navigator.userAgent
  return /Edg\//i.test(ua) || (/AppleWebKit/i.test(ua) && !/Chrome|Chromium/i.test(ua))
}

const EVENT_NAMES = [
  'phase',
  'message',
  'delta',
  'reasoning',
  'tool_output',
  'tool_progress',
  'awaiting',
  'done',
  'error',
] as const

function readEventSource(
  sessionId: string,
  onEvent: (event: TurnEvent) => void,
  signal?: AbortSignal,
): Promise<boolean> {
  return new Promise((resolve) => {
    const source = new EventSource(
      `${API_BASE_URL}${API_PREFIX}/chat/sessions/${sessionId}/stream`,
      { withCredentials: true },
    )
    let settled = false
    let sawDone = false
    const finish = (value: boolean) => {
      if (settled) return
      settled = true
      source.close()
      signal?.removeEventListener('abort', onAbort)
      resolve(value)
    }
    const onAbort = () => finish(false)
    const handle = (event: MessageEvent<string>, name: string) => {
      let data: Record<string, unknown>
      try {
        data = JSON.parse(event.data) as Record<string, unknown>
      } catch {
        return
      }
      if (name === 'done') sawDone = true
      onEvent({ event: name, data })
      if (name === 'done' || name === 'error') finish(name === 'done')
    }
    for (const name of EVENT_NAMES) {
      source.addEventListener(name, (event) => handle(event as MessageEvent<string>, name))
    }
    source.onerror = () => {
      // `error` 既可能是服务端的自定义 error 帧，也可能是连接错误；
      // 有 data 的已经由 addEventListener 处理。网络错误直接收尾。
      if (!sawDone) finish(false)
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

function pollLiveFrames(
  sessionId: string,
  onEvent: (event: TurnEvent) => void,
  signal?: AbortSignal,
): Promise<boolean> {
  let after = 0
  return new Promise((resolve) => {
    let stopped = false
    let timer: number | null = null
    const stop = (value: boolean) => {
      if (stopped) return
      stopped = true
      if (timer !== null) window.clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
      resolve(value)
    }
    const onAbort = () => stop(false)
    const tick = async () => {
      if (stopped) return
      try {
        const response = await fetch(
          `${API_BASE_URL}${API_PREFIX}/chat/sessions/${sessionId}/live?after=${after}`,
          { credentials: 'include', signal },
        )
        if (!response.ok) {
          stop(false)
          return
        }
        const payload = (await response.json()) as {
          frames?: Array<{ event?: string; data?: Record<string, unknown> }>
          next?: number
          ended?: boolean
        }
        for (const frame of payload.frames ?? []) {
          if (!frame.event || !frame.data) continue
          onEvent({ event: frame.event, data: frame.data })
        }
        after = typeof payload.next === 'number' ? payload.next : after
        if (payload.ended) {
          stop(true)
          return
        }
      } catch {
        if (!stopped) timer = window.setTimeout(tick, 150)
        return
      }
      if (!stopped) timer = window.setTimeout(tick, 120)
    }
    signal?.addEventListener('abort', onAbort, { once: true })
    void tick()
  })
}

export interface StreamTurnOptions {
  sessionId: string
  content: string
  providerId?: string | null
  attachmentIds?: string[]
  explicitTools?: string[]
  /** 每收到一帧调一次。 */
  onEvent: (event: TurnEvent) => void
  signal?: AbortSignal
}

/**
 * 发一条消息并**边跑边读**。
 *
 * 返回 `true` = 这一轮正常结束（收到 `done`）；`false` = 失败
 * （收到 `error` 帧、HTTP 非 2xx、或流意外中断）。
 *
 * ★ 调用方拿它决定要不要清输入框 —— 与 `sendMessage` 同一条纪律：
 *   失败时把用户敲的字留在框里，否则他要重打一遍。
 */
export async function streamTurn(options: StreamTurnOptions): Promise<boolean> {
  const { sessionId, content, providerId, attachmentIds, explicitTools, onEvent, signal } = options

  const body: Record<string, unknown> = { content, locale: activeLocale() }
  if (providerId) body.providerId = providerId
  if (attachmentIds && attachmentIds.length > 0) body.attachmentIds = attachmentIds
  if (explicitTools && explicitTools.length > 0) body.explicitTools = explicitTools

  const response = await fetch(
    `${API_BASE_URL}${API_PREFIX}/chat/sessions/${sessionId}/messages/stream`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      credentials: 'include',
      signal,
    },
  )

  if (!response.ok) {
    // ★ 流式端点在**开流之前**的失败仍然是普通 JSON 错误
    //   （校验失败、会话不存在、这一轮已在跑）。此时没有流可读，直接报错。
    //   开流之后的失败只会以 `error` 帧出现（见后端 stream_turn 的 docstring）。
    //
    // ★ v1.58：后端错误信封是 `{ error: { code, message, details? } }`
    //   （见 `src/lib/api.ts` 顶部的契约说明），message 在 **error 里面**。
    //   早先这里错读成顶层 `payload.message` —— 永远取不到，于是用户看到的
    //   只有 `HTTP 409`（线上实测：那一轮还在跑时按发送 → 裸 409，
    //   后端写好的一句「这个对话还有一轮正在跑…用插话/中断/继续」没被读出来）。
    //   与 lib/api.ts 的解析口径对齐：优先 `error.message`，兼容顶层 `message`，
    //   都没有才回退到状态码。
    let detail = `HTTP ${response.status}`
    try {
      const payload = (await response.json()) as {
        message?: string
        error?: { code?: string; message?: string }
      }
      const server_message = payload?.error?.message ?? payload?.message
      if (server_message) detail = server_message
    } catch {
      // 不是 JSON 就保留状态码
    }
    throw new Error(detail)
  }
  if (!response.body) {
    throw new Error('这个浏览器不支持读取流式响应。')
  }

  if (prefersPolling()) {
    try {
      await response.body.cancel()
    } catch {
      // 已经结束/不可取消都无妨
    }
    return pollLiveFrames(sessionId, onEvent, signal)
  }

  if (prefersEventSource()) {
    // POST 只负责启动轮次；响应体不再读取（服务器在客户端断开后仍继续跑）。
    // 真正的逐帧通道改走 GET EventSource，绕开 WebKit 的 fetch 缓冲。
    try {
      await response.body.cancel()
    } catch {
      // 已经结束/不可取消都无妨
    }
    return readEventSource(sessionId, onEvent, signal)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let sawDone = false
  let sawError = false

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const parsed = parseSseChunk(buffer)
      buffer = parsed.rest
      for (const frame of parsed.frames) {
        if (frame.event === TURN_DONE) sawDone = true
        if (frame.event === TURN_ERROR) sawError = true
        onEvent(frame)
      }
    }
    // 收尾：流结束时可能还有一帧没有结尾空行
    const tail = parseSseChunk(buffer + '\n\n')
    for (const frame of tail.frames) {
      if (frame.event === TURN_DONE) sawDone = true
      if (frame.event === TURN_ERROR) sawError = true
      onEvent(frame)
    }
  } finally {
    reader.releaseLock()
  }

  return sawDone && !sawError
}

/** 把 `message` 帧的载荷收窄成 `ChatMessage`。 */
export function asChatMessage(data: Record<string, unknown>): ChatMessage | null {
  // 只做最小校验：有 id 与 role 才算一条消息。
  // 多余的字段留给它（前向兼容：后端加字段时前端不用改）。
  if (typeof data.id !== 'string' || typeof data.role !== 'string') return null
  return data as unknown as ChatMessage
}

/**
 * 接上一个**已经在跑**的轮次（OMP resumable stream 的最小版）。
 *
 * 与 `streamTurn` 的区别：它不发送新消息，只读取服务端事件总线；
 * 先回放已经产生的帧，再接实时帧。刷新后的页面/另一个标签页用它，
 * 才不会只靠轮询整条消息（那样正文是一段一段蹦出来的）。
 */
export async function attachTurnStream(
  sessionId: string,
  onEvent: (event: TurnEvent) => void,
  signal?: AbortSignal,
): Promise<boolean> {
  if (prefersPolling()) {
    return pollLiveFrames(sessionId, onEvent, signal)
  }
  if (prefersEventSource()) {
    return readEventSource(sessionId, onEvent, signal)
  }

  const response = await fetch(
    `${API_BASE_URL}${API_PREFIX}/chat/sessions/${sessionId}/stream`,
    { method: 'GET', credentials: 'include', signal },
  )
  if (!response.ok || !response.body) return false
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let sawDone = false
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const parsed = parseSseChunk(buffer)
      buffer = parsed.rest
      for (const frame of parsed.frames) {
        if (frame.event === TURN_DONE) sawDone = true
        onEvent(frame)
      }
    }
    const tail = parseSseChunk(buffer + '\n\n')
    for (const frame of tail.frames) {
      if (frame.event === TURN_DONE) sawDone = true
      onEvent(frame)
    }
  } finally {
    reader.releaseLock()
  }
  return sawDone
}
