import { API_BASE_URL, API_PREFIX } from '@/lib/api'

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

  const body: Record<string, unknown> = { content }
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
    //   （校验失败、会话不存在）。此时没有流可读，直接报错。
    //   开流之后的失败只会以 `error` 帧出现（见后端 stream_turn 的 docstring）。
    let detail = `HTTP ${response.status}`
    try {
      const payload = (await response.json()) as { message?: string }
      if (payload?.message) detail = payload.message
    } catch {
      // 不是 JSON 就保留状态码
    }
    throw new Error(detail)
  }
  if (!response.body) {
    throw new Error('这个浏览器不支持读取流式响应。')
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
