import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  archiveSession,
  createSession,
  getChatSnapshot,
  interject,
  loadSessions,
  loadTools,
  openSession,
  renameSession,
  clearStaleSending,
  resetChatStore,
  resumeTurn,
  sendMessage,
  uploadAttachment,
} from './chat'
import { ApiError } from '@/lib/api'
import { TURN_DELTA, TURN_DONE, TURN_ERROR, TURN_MESSAGE, TURN_PHASE } from './chat-stream'

/*
  ══════════════════════════════════════════════════════════════
  对话 store 的行为（v0.93）
  ══════════════════════════════════════════════════════════════

  ## 这个文件守什么

  这一层最容易错的地方不是"请求发得对不对"，而是**状态收敛**：

    1. 一轮里服务端追加了多条消息，前端要**整轮一次**接进去；
    2. 失败时不能把已有内容清掉（用户会以为对话被删了）；
    3. 两个请求同时回来时，后到的不能把先到的覆盖成旧数据。

  ## 桩法

  与其他 store 测试一致：`vi.stubGlobal('fetch', ...)` + `pending[]`
  数组手工控制 resolve 时机。`vitest.config.ts` 里 `globals: false`，
  所以全部显式 import。
*/

interface Deferred {
  resolve: (body: unknown, status?: number) => void
  /** 直接给一个现成的 Response —— 流式端点要自己造 `text/event-stream`。 */
  resolveRaw: (response: Response) => void
  reject: (error: Error) => void
}

let pending: Deferred[] = []
let calls: { url: string; method: string }[] = []

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/**
 * 造一条 SSE 帧。
 *
 * ★ 这一整个辅助（连同 `sseResponse`）是 v0.101 补的。在此之前这个文件
 *   的 15 条测试**全部**假设 `sendMessage` 走旧的 `api.post` 普通回包 ——
 *   流式改造之后它们喂的是 `{messages: [...]}` 这种 JSON 体，而
 *   `streamTurn` 要从响应体里读 `done` 帧才算成功。于是：
 *
 *     - 一条断言 `ok === true` 的用例真的红了；
 *     - 另外 14 条**全是假绿** —— 它们只看 `pendingUserContent` / `sending`
 *       / `error`，而 `sendMessage` 把 `ok=false` 当普通失败处理，
 *       恰好让那些字段仍然对得上。
 *
 *   也就是说：store 层「`streamTurn` 的装配 + onEvent 分发」这条路径
 *   此前**零覆盖**。`chat-stream.test.ts` 只测了切帧（`parseSseChunk`），
 *   而 `ChatPage.test.tsx` 把整个 `@/data/chat` mock 掉了。
 */
function sseFrame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
}

/** 把所有帧拼成流式响应。 */
function sseResponse(frames: string[], status = 200): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame))
      controller.close()
    },
  })
  return new Response(body, {
    status,
    headers: { 'Content-Type': 'text/event-stream' },
  })
}

function stubFetch() {
  pending = []
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      calls.push({ url, method: init?.method ?? 'GET' })
      return new Promise<Response>((resolve, reject) => {
        pending.push({
          // ★ 必须在这里包成 Response —— `apiRequest` 读的是 `res.ok` /
          //   `res.headers` / `res.json()`。直接把业务对象 resolve 出去，
          //   它会在读取 `.headers` 时抛错，而 store 把错误吞进 `error` 字段，
          //   表现成"请求跑了但 state 没变" —— 看起来像 store 的 bug。
          resolve: (body, status = 200) => resolve(jsonResponse(body, status)),
          resolveRaw: resolve,
          reject,
        })
      })
    }),
  )
}

/**
 * 结束一轮流式请求。
 *
 * 帧序列刻意按**后端真实顺序**写：先 user 帧（引擎在 user 消息落库后立刻
 * 发它），再 phase/message 交替，最后 done。手写顺序错了的话，测试会
 * 通过一个生产里不存在的时序。
 */
function finishTurn(
  index: number,
  frames: string[] = [sseFrame(TURN_DONE, { messageCount: 0, awaitingApproval: null, hitToolLimit: false })],
) {
  pending[index].resolveRaw(sseResponse(frames))
}

/** 等一轮微任务，让 store 里的 await 推进到下一个 pending。 */
function tick() {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

/**
 * 手动可控的流式响应（v0.102）。
 *
 * ## 为什么需要它，`finishTurn` 不够
 *
 * `finishTurn` 把整串帧一次性塞进流里，那是**终态**。而 `streamingText`
 * 这一组要断言的是**中间态**：「来了一个 delta 之后，界面上现在是什么」。
 * 一次性喂完的话，所有中间态在同一帧里闪过，断言只能看到最后结果 ——
 * 而"拼接 vs 替换"这类 bug 恰好只在中间态里现形。
 *
 * 所以这里把 controller 暴露出来，测试自己决定什么时候推哪一帧。
 */
let chatStreamController: ReadableStreamDefaultController<Uint8Array>

function openManualStream() {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      chatStreamController = controller
    },
  })
  pending[1].resolveRaw(
    new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }),
  )
}

/** 补上正常收尾的 done 帧并关流。 */
function finishStream() {
  chatStreamController.enqueue(
    new TextEncoder().encode(
      sseFrame(TURN_DONE, {
        messageCount: 0,
        awaitingApproval: null,
        hitToolLimit: false,
      }),
    ),
  )
  chatStreamController.close()
}

beforeEach(() => {
  resetChatStore()
  stubFetch()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function session(overrides: Record<string, unknown> = {}) {
  return {
    id: 'chat_1',
    title: '新对话',
    createdBy: 'user_test',
    createdAt: '2026-10-25T10:00:00Z',
    updatedAt: '2026-10-25T10:00:00Z',
    messageCount: 0,
    archived: false,
    ...overrides,
  }
}

function message(overrides: Record<string, unknown> = {}) {
  return {
    id: 'msg_1',
    seq: 1,
    role: 'user',
    content: '你好',
    reasoning: null,
    toolName: null,
    toolArgs: null,
    toolFailed: null,
    createdAt: '2026-10-25T10:00:00Z',
    ...overrides,
  }
}

describe('loadSessions', () => {
  it('成功时把列表放进 state', async () => {
    const promise = loadSessions()
    pending[0].resolve([session()])
    await promise
    expect(getChatSnapshot().sessions).toHaveLength(1)
    expect(getChatSnapshot().status).toBe('ready')
  })

  it('★ 失败时**保留**已有列表 —— 不能让用户以为对话被删了', async () => {
    // 先成功一次
    const first = loadSessions()
    pending[0].resolve([session()])
    await first
    expect(getChatSnapshot().sessions).toHaveLength(1)

    // 再失败一次
    const second = loadSessions()
    pending[1].reject(new Error('network down'))
    await second

    // 列表还在（这是关键），但 error 挂上了
    expect(getChatSnapshot().sessions).toHaveLength(1)
    expect(getChatSnapshot().error).not.toBeNull()
  })
})

describe('loadTools', () => {
  it('★ 失败不设 error —— 工具清单拿不到不该挡住对话', async () => {
    const promise = loadTools()
    pending[0].reject(new Error('nope'))
    await promise
    // 这才是要点：清单是说明性的，失败不污染 error 字段。
    expect(getChatSnapshot().error).toBeNull()
    expect(getChatSnapshot().tools).toEqual([])
  })
})

describe('createSession', () => {
  it('新会话头插列表并立刻打开', async () => {
    const promise = createSession()
    pending[0].resolve(session({ id: 'chat_new' }), 201)
    const created = await promise

    expect(created?.id).toBe('chat_new')
    expect(getChatSnapshot().sessions[0].id).toBe('chat_new')
    expect(getChatSnapshot().active?.id).toBe('chat_new')
    // 刚建的会话一定没有消息 —— 不能沿用上一个会话的 messages。
    expect(getChatSnapshot().active?.messages).toEqual([])
  })

  it('失败时返回 null 且挂 error', async () => {
    const promise = createSession()
    pending[0].reject(new Error('boom'))
    const created = await promise
    expect(created).toBeNull()
    expect(getChatSnapshot().error).not.toBeNull()
  })
})

describe('openSession', () => {
  it('把详情（含消息）放进 active', async () => {
    const promise = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [message()] })
    await promise
    expect(getChatSnapshot().active?.messages).toHaveLength(1)
  })
})

/**
 * 一条**可手动推进**的流式响应。
 *
 * ★ 与 `sseResponse`（一次性把帧全塞进去）的区别是这个能测出
 *   "边跑边出"本身：帧可以在断言之间逐条 push，于是能验证
 *   "第一帧到了之后 `active.messages` 已经有东西，而这一轮还没结束"。
 *   用一次性响应的话，所有帧会在同一个微任务里落地 ——
 *   那样即使实现退化成"攒完再更新"，测试也照样绿。
 */
interface LiveStream {
  response: Response
  push: (event: string, data: unknown) => void
  close: () => void
}

function liveSseResponse(): LiveStream {
  const encoder = new TextEncoder()
  let controller!: ReadableStreamDefaultController<Uint8Array>
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c
    },
  })
  return {
    response: new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    }),
    push: (event, data) => controller.enqueue(encoder.encode(sseFrame(event, data))),
    close: () => controller.close(),
  }
}

const DONE_FRAME = { messageCount: 0, awaitingApproval: null, hitToolLimit: false }

describe('sendMessage — 流式（v0.100）', () => {
  /*
    ★ 这一组是 v0.101 补的。v0.100 把 `sendMessage` 从 `api.post` 改成
      `streamTurn` 时，**没有**同步改这个文件 —— 代价是 15 条里 1 条真红
      （`ok === true` 那条，因为 JSON 体里没有 done 帧，`sawDone` 永远 false）、
      14 条假绿（它们不看返回值）。store 层这条装配路径此前零覆盖。
  */

  it('★ 边跑边长：第一帧一到 active 就有内容，此时这一轮还没结束', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const stream = liveSseResponse()
    const promise = sendMessage('chat_1', '帮我扫一下')
    pending[1].resolveRaw(stream.response)

    // 引擎在 user 消息落库后立刻发它 —— 所以第一帧就是 user
    stream.push(TURN_MESSAGE, message({ id: 'm1', seq: 1, role: 'user', content: '帮我扫一下' }))
    await tick()

    // 关键：`sending` 还是 true（这一轮没结束），但消息已经在屏幕上了
    expect(getChatSnapshot().sending).toBe(true)
    expect(getChatSnapshot().active?.messages.map((m) => m.id)).toEqual(['m1'])

    stream.push(TURN_MESSAGE, message({ id: 'm2', seq: 2, role: 'assistant', content: '好的。' }))
    await tick()
    expect(getChatSnapshot().active?.messages.map((m) => m.id)).toEqual(['m1', 'm2'])
    // messageCount 要跟着长 —— 否则左侧列表一直显示"0 条"
    expect(getChatSnapshot().active?.messageCount).toBe(2)

    stream.push(TURN_DONE, { ...DONE_FRAME, messageCount: 2 })
    stream.close()
    const ok = await promise
    if (pending[2]) pending[2].resolve([])

    expect(ok).toBe(true)
    expect(getChatSnapshot().sending).toBe(false)
    expect(getChatSnapshot().phase).toBeNull()
  })

  it('★ 同一条消息来两次只接一次（重连/重放会重复，重复看起来像模型重复动作）', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const stream = liveSseResponse()
    const promise = sendMessage('chat_1', '你好')
    pending[1].resolveRaw(stream.response)

    const same = message({ id: 'm1', seq: 1, role: 'user', content: '你好' })
    stream.push(TURN_MESSAGE, same)
    stream.push(TURN_MESSAGE, same)
    await tick()

    expect(getChatSnapshot().active?.messages).toHaveLength(1)

    stream.push(TURN_DONE, { ...DONE_FRAME, messageCount: 1 })
    stream.close()
    await promise
    if (pending[2]) pending[2].resolve([])
  })

  it('★ phase 帧驱动「正在想…」那一行（能显示第几轮 / 在跑哪个工具）', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const stream = liveSseResponse()
    const promise = sendMessage('chat_1', '你好')
    pending[1].resolveRaw(stream.response)

    stream.push(TURN_PHASE, { phase: 'thinking', round: 1 })
    await tick()
    expect(getChatSnapshot().phase).toEqual({ phase: 'thinking', round: 1 })

    stream.push(TURN_PHASE, { phase: 'tool', round: 2, tool: 'create_scan' })
    await tick()
    expect(getChatSnapshot().phase).toEqual({ phase: 'tool', round: 2, tool: 'create_scan' })

    stream.push(TURN_DONE, DONE_FRAME)
    stream.close()
    await promise
    if (pending[2]) pending[2].resolve([])

    // done 之后必须清掉，否则界面永远转着"正在执行 create_scan…"
    expect(getChatSnapshot().phase).toBeNull()
  })

  it('★ error 帧 → 返回 false 并挂 error，sending 复位，已有消息不清空', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const stream = liveSseResponse()
    const promise = sendMessage('chat_1', '你好')
    pending[1].resolveRaw(stream.response)

    stream.push(TURN_MESSAGE, message({ id: 'm1', seq: 1, role: 'user', content: '你好' }))
    stream.push(TURN_ERROR, { message: '模型挂了' })
    stream.close()
    const ok = await promise
    if (pending[2]) pending[2].resolve([])

    expect(ok).toBe(false)
    expect(getChatSnapshot().sending).toBe(false)
    expect(getChatSnapshot().error?.message).toBe('模型挂了')
    // 已经显示出来的用户消息是**真的**（服务端确实落了库），不能清
    expect(getChatSnapshot().active?.messages.map((m) => m.id)).toEqual(['m1'])
    // 失败后不能继续转圈
    expect(getChatSnapshot().phase).toBeNull()
  })

  it('★ HTTP 层失败（开流之前就非 2xx）也返回 false 并复位', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const promise = sendMessage('chat_1', '你好')
    // 校验失败 / 会话不存在：此时没有流，是普通 JSON 错误
    pending[1].resolve({ message: '会话不存在' }, 404)
    const ok = await promise

    expect(ok).toBe(false)
    expect(getChatSnapshot().sending).toBe(false)
    expect(getChatSnapshot().error).not.toBeNull()
    expect(getChatSnapshot().active?.messages).toEqual([])
  })

  it('★ 一轮在跑时不允许再发（否则 seq 顺序取决于谁先返回 → 消息乱序）', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const stream = liveSseResponse()
    const first = sendMessage('chat_1', '第一条')
    pending[1].resolveRaw(stream.response)

    // 第一个还没回来就发第二条
    const second = await sendMessage('chat_1', '第二条')
    expect(second).toBe(false)
    // 第二条**不该**发出请求 —— 检查 fetch 只调了 2 次（open + first）
    expect(calls).toHaveLength(2)

    stream.push(TURN_DONE, DONE_FRAME)
    stream.close()
    await first
    if (pending[2]) pending[2].resolve([])
  })

  it('★ 服务端返回的消息只接进**它自己**那个会话', async () => {
    const open1 = openSession('chat_1')
    pending[0].resolve({ ...session({ id: 'chat_1' }), messages: [] })
    await open1

    // 发到 chat_1（请求发出后不立刻给流）
    const stream = liveSseResponse()
    const promise = sendMessage('chat_1', '你好')
    pending[1].resolveRaw(stream.response)

    // 期间用户切到 chat_2
    const open2 = openSession('chat_2')
    const openIdx = calls.findIndex(
      (c) => c.url.includes('/chat/sessions/chat_2') && c.method === 'GET',
    )
    pending[openIdx].resolve({ ...session({ id: 'chat_2' }), messages: [] })
    await open2

    // 现在让 chat_1 的那一轮推一条消息 + 结束
    stream.push(TURN_MESSAGE, message({ id: 'x', seq: 1 }))
    stream.push(TURN_DONE, DONE_FRAME)
    stream.close()
    await promise
    // 收尾：把 sendMessage 内部那个 loadSessions 也解决掉，避免悬挂的 Promise
    for (const [i, call] of calls.entries()) {
      if (call.url.includes('/chat/sessions') && call.method === 'GET' && !call.url.includes('chat_1') && !call.url.includes('chat_2')) {
        pending[i].resolve([])
      }
    }
    await tick()

    // chat_2 是当前会话，它**不该**被塞进 chat_1 的回复。
    expect(getChatSnapshot().active?.id).toBe('chat_2')
    expect(getChatSnapshot().active?.messages).toEqual([])
  })
})

describe('archiveSession', () => {
  it('归档后从列表移除，且若它是当前会话则清空 active', async () => {
    const first = loadSessions()
    pending[0].resolve([session({ id: 'chat_1' }), session({ id: 'chat_2' })])
    await first

    const open = openSession('chat_1')
    pending[1].resolve({ ...session({ id: 'chat_1' }), messages: [] })
    await open

    const promise = archiveSession('chat_1')
    pending[2].resolve(session({ id: 'chat_1', archived: true }))
    await promise

    expect(getChatSnapshot().sessions.map((s) => s.id)).toEqual(['chat_2'])
    // 当前正在看的会话被归档了 → 回到欢迎屏，而不是继续显示已归档的内容。
    expect(getChatSnapshot().active).toBeNull()
  })
})

describe('renameSession', () => {
  it('同步更新列表与当前会话的标题', async () => {
    const first = loadSessions()
    pending[0].resolve([session({ id: 'chat_1' })])
    await first
    const open = openSession('chat_1')
    pending[1].resolve({ ...session({ id: 'chat_1' }), messages: [] })
    await open

    const promise = renameSession('chat_1', '改过的名字')
    pending[2].resolve(session({ id: 'chat_1', title: '改过的名字' }))
    await promise

    expect(getChatSnapshot().sessions[0].title).toBe('改过的名字')
    expect(getChatSnapshot().active?.title).toBe('改过的名字')
  })
})

describe('sendMessage — 乐观气泡（v0.97）', () => {
  /*
    ★ 这组钉的是"点下发送到服务端回包之间"那段时间窗口。

      没有它的话，`pendingUserContent` 这条链路可以整条删掉而 12 个旧测试
      全绿 —— 实测过：把 store 里的 `patch({..., pendingUserContent: content})`
      删掉，旧测试一条都不红。而生产线上的症状是"点了发送什么都没发生"。
  */

  it('★ 发出的那一刻就写进 pendingUserContent（不等服务端回包）', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    // 只发起、不 resolve —— 模拟"服务端还在想"
    const promise = sendMessage('chat_1', '列出可用技能')

    // 关键断言：此刻响应还没回来，但那句话已经被记住了
    expect(getChatSnapshot().pendingUserContent).toBe('列出可用技能')

    finishTurn(1, [
      sseFrame(TURN_MESSAGE, message({ id: 'm1', seq: 1, content: '列出可用技能' })),
      sseFrame(TURN_DONE, DONE_FRAME),
    ])
    await promise
    if (pending[2]) pending[2].resolve([])
  })

  it('★ 真 user 帧一到就清空（不等整轮结束：那会让两句话同屏几十秒）', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const stream = liveSseResponse()
    const promise = sendMessage('chat_1', '列出可用技能')
    pending[1].resolveRaw(stream.response)

    // 这一轮还远没结束，但真消息已经到了
    stream.push(TURN_MESSAGE, message({ id: 'm1', seq: 1, content: '列出可用技能' }))
    await tick()

    expect(getChatSnapshot().pendingUserContent).toBeNull()
    expect(getChatSnapshot().sending).toBe(true)

    stream.push(TURN_DONE, DONE_FRAME)
    stream.close()
    await promise
    if (pending[2]) pending[2].resolve([])
  })

  it('★ 失败时也清空 —— 那句话服务端没有，留着就是谎话', async () => {
    /*
      失败路径最容易被漏。留着 `pendingUserContent` 的话，用户看到自己那句话
      稳稳地画在屏幕上（像是发出去了），而服务端根本没这条记录 ——
      刷新一下就消失。页面那边负责把原文放回输入框。
    */
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const promise = sendMessage('chat_1', '这段会失败')
    pending[1].reject(new Error('接入点超时'))
    await promise

    expect(getChatSnapshot().pendingUserContent).toBeNull()
  })
})

describe('interject — 插话兑现（v0.101）', () => {
  /*
    ★ 这一组钉的是「插话气泡什么时候退场」。

      背景：`interject()` 只把消息写进本地 `interjections` 暂存区，界面显示
      「插话 · 模型下一轮会看到」。引擎在每轮开头重读历史，所以模型**确实**
      看到了它；但它不是引擎落库的（是 interject 端点自己 commit 的），
      引擎不会为它发 message 帧 —— 于是 `active.messages` 里永远没有它。

      没有这一组的话，那个气泡会在轮次结束后继续挂着说假话，且要等用户发
      下一条消息才被 `sendMessage` 开头那句 `interjections: []` 顺手清掉。
  */

  it('★ 一轮结束后插话气泡必须退场，且真消息由刷新补进来', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    // 用户在一轮进行中插话
    const stream = liveSseResponse()
    const promise = sendMessage('chat_1', '先看一眼 nmap')
    pending[1].resolveRaw(stream.response)

    const interjectPromise = interject('chat_1', '顺便看一下 ffuf 的配方')
    const interjectIdx = calls.findIndex((c) => c.url.includes('/interject'))
    pending[interjectIdx].resolve(
      message({ id: 'm_inter', seq: 2, role: 'user', content: '顺便看一下 ffuf 的配方' }),
    )
    await interjectPromise
    expect(getChatSnapshot().interjections).toHaveLength(1)

    // 这一轮正常跑完
    stream.push(TURN_MESSAGE, message({ id: 'm1', seq: 1, role: 'user', content: '先看一眼 nmap' }))
    stream.push(TURN_DONE, { ...DONE_FRAME, messageCount: 3 })
    stream.close()
    const ok = await promise
    expect(ok).toBe(true)

    // 关键 1：暂存区清空 —— 气泡不该再显示「模型下一轮会看到」
    expect(getChatSnapshot().interjections).toEqual([])

    // 关键 2：真的去服务端取回了完整消息列表（插话现在是其中一条普通 user 消息）
    //
    // ★ 从**后往前**找：`openSession` 打的是同一个 URL，正着找会命中开头那次
    //   （它早就 resolve 过了，于是把刷新那次留在悬挂状态，测试看起来"没刷新"）。
    let refreshIdx = -1
    for (let i = calls.length - 1; i >= 0; i--) {
      if (calls[i].url.endsWith('/chat/sessions/chat_1') && calls[i].method === 'GET') {
        refreshIdx = i
        break
      }
    }
    expect(refreshIdx).toBeGreaterThan(0)
    pending[refreshIdx].resolve({
      ...session(),
      messages: [
        message({ id: 'm1', seq: 1, role: 'user', content: '先看一眼 nmap' }),
        message({ id: 'm_inter', seq: 2, role: 'user', content: '顺便看一下 ffuf 的配方' }),
        message({ id: 'm3', seq: 3, role: 'assistant', content: '好，nmap 和 ffuf 都用上。' }),
      ],
    })
    await tick()

    const active = getChatSnapshot().active
    expect(active?.messages.map((m) => m.id)).toEqual(['m1', 'm_inter', 'm3'])
    // 插话现在是真消息了，不再是"待兑现"的暂存条目
    expect(active?.messageCount).toBe(3)

    for (const [i] of calls.entries()) {
      if (pending[i] && i !== refreshIdx) pending[i].resolve([])
    }
  })

  it('★ 失败路径也要退场 —— 插话早已自己 commit 进库，气泡留着只继续说假话', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const stream = liveSseResponse()
    const promise = sendMessage('chat_1', '先看一眼 nmap')
    pending[1].resolveRaw(stream.response)

    const interjectPromise = interject('chat_1', '顺便看一下 ffuf 的配方')
    const interjectIdx = calls.findIndex((c) => c.url.includes('/interject'))
    pending[interjectIdx].resolve(
      message({ id: 'm_inter', seq: 2, role: 'user', content: '顺便看一下 ffuf 的配方' }),
    )
    await interjectPromise
    expect(getChatSnapshot().interjections).toHaveLength(1)

    // 这一轮失败了
    stream.push(TURN_ERROR, { message: '模型挂了' })
    stream.close()
    const ok = await promise
    expect(ok).toBe(false)

    expect(getChatSnapshot().interjections).toEqual([])
  })

  it('★ 空白插话不发请求（Enter 手滑不该产生一条空消息）', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const before = calls.length
    const ok = await interject('chat_1', '   ')
    expect(ok).toBe('failed')
    // 不发请求 —— Enter 手滑不该打后端一次、更不该产生一条空消息
    expect(calls.length).toBe(before)
    expect(calls.filter((c) => c.url.includes('/interject'))).toHaveLength(0)
    expect(getChatSnapshot().interjections).toEqual([])
  })
})

/*
  ══════════════════════════════════════════════════════════════
  正文增量（v0.102）
  ══════════════════════════════════════════════════════════════

  ## 这一组守什么

  用户原话：「消息是一瞬间蹦出一大块」。调研三个同类项目之后的结论是：
  它们看起来"逐字长出来"不是因为入场动画，而是因为正文本来就是逐 token
  到达的。所以后端加了 `delta` 帧，前端把增量拼进 `streamingText`。

  这条路径的失败模式**全是静默的**：

    1. 拼错（用替换而不是拼接）→ 只有最后一个 token 显示；
    2. 落库后不清 → 同一段文字在缓冲和真消息里各显示一遍；
    3. 失败/结束时不清 → 半句话粘在下一轮开头；
    4. 换会话时不重新开始 → 上一轮的残字出现在新会话里。

  没有一条会抛异常，所以只能靠这里的断言。
*/
describe('streamingText — 正文增量（v0.102）', () => {
  it('★ 多个 delta 帧按到达顺序拼起来', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const promise = sendMessage('chat_1', '你好')
    await tick()

    // 手动控制流，逐帧推进 —— 这样才能断言"中间态"。
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        chatStreamController = controller
      },
    })
    pending[1].resolveRaw(
      new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }),
    )
    await tick()

    const encoder = new TextEncoder()
    chatStreamController.enqueue(
      encoder.encode(sseFrame(TURN_DELTA, { text: '好的' })),
    )
    await tick()
    expect(getChatSnapshot().streamingText).toBe('好的')

    chatStreamController.enqueue(
      encoder.encode(sseFrame(TURN_DELTA, { text: '，我查一下。' })),
    )
    await tick()
    // ★ 拼接而不是替换 —— 用替换的话这里会是「，我查一下。」
    expect(getChatSnapshot().streamingText).toBe('好的，我查一下。')

    finishStream()
    const ok = await promise
    expect(ok).toBe(true)
  })

  it('★ assistant 消息落库后缓冲退场（否则同一段文字显示两遍）', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const promise = sendMessage('chat_1', '你好')
    await tick()
    openManualStream()

    chatStreamController.enqueue(
      new TextEncoder().encode(sseFrame(TURN_DELTA, { text: '好的' })),
    )
    await tick()
    expect(getChatSnapshot().streamingText).toBe('好的')

    // 这一轮的 assistant 正文落库了 → 真消息接管
    chatStreamController.enqueue(
      new TextEncoder().encode(
        sseFrame(TURN_MESSAGE, {
          ...message({ id: 'msg_a1', role: 'assistant', content: '好的' }),
          seq: 2,
        }),
      ),
    )
    await tick()
    expect(getChatSnapshot().streamingText).toBeNull()
    // 真消息接上了
    const messages = getChatSnapshot().active?.messages ?? []
    expect(messages.some((m) => m.id === 'msg_a1')).toBe(true)

    finishStream()
    await promise
  })

  it('★ tool 帧不清正文缓冲（工具卡不该打断还在流的正文）', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const promise = sendMessage('chat_1', '你好')
    await tick()
    openManualStream()

    chatStreamController.enqueue(
      new TextEncoder().encode(sseFrame(TURN_DELTA, { text: '让我' })),
    )
    await tick()
    expect(getChatSnapshot().streamingText).toBe('让我')

    chatStreamController.enqueue(
      new TextEncoder().encode(
        sseFrame(TURN_MESSAGE, {
          ...message({ id: 'msg_t1', role: 'tool', content: '3 条结果' }),
          seq: 3,
        }),
      ),
    )
    await tick()
    // ★ 只清 assistant。工具卡落库不代表正文说完了。
    expect(getChatSnapshot().streamingText).toBe('让我')

    finishStream()
    await promise
  })

  it('★ 一轮结束（done）后缓冲必须清空', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const promise = sendMessage('chat_1', '你好')
    await tick()
    openManualStream()

    chatStreamController.enqueue(
      new TextEncoder().encode(sseFrame(TURN_DELTA, { text: '还没来得及落库的半句' })),
    )
    await tick()
    expect(getChatSnapshot().streamingText).toBe('还没来得及落库的半句')

    // 直接 done，没有 message 帧（模型写了又被截断的形态）
    finishStream()
    const ok = await promise
    expect(ok).toBe(true)
    // 不清的话这半句话会永远挂在界面上
    expect(getChatSnapshot().streamingText).toBeNull()
  })

  it('★ 失败路径也要清空（半句话不能粘在界面或下一轮开头）', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const promise = sendMessage('chat_1', '你好')
    await tick()
    openManualStream()

    chatStreamController.enqueue(
      new TextEncoder().encode(sseFrame(TURN_DELTA, { text: '写到一半' })),
    )
    await tick()
    expect(getChatSnapshot().streamingText).toBe('写到一半')

    chatStreamController.enqueue(
      new TextEncoder().encode(sseFrame(TURN_ERROR, { message: '模型挂了' })),
    )
    chatStreamController.close()
    const ok = await promise
    expect(ok).toBe(false)
    expect(getChatSnapshot().streamingText).toBeNull()
  })

  it('★ 新一轮开始时清掉上一轮残留', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    // 第一轮：留半句（异常路径模拟 —— 直接不发 done 就 close）
    const first = sendMessage('chat_1', '第一句')
    await tick()
    openManualStream()
    chatStreamController.enqueue(
      new TextEncoder().encode(sseFrame(TURN_DELTA, { text: '残留' })),
    )
    chatStreamController.close()
    await first

    // 第二轮开始时必须从空开始
    const second = sendMessage('chat_1', '第二句')
    await tick()
    // ★ 不清的话新一轮开头会显示「残留」
    expect(getChatSnapshot().streamingText).toBeNull()
    finishTurn(2)
    await second
  })

  it('★ 其它会话的 delta 不写进当前会话', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const promise = sendMessage('chat_1', '你好')
    await tick()
    openManualStream()

    // 用户切走了
    const other = openSession('chat_2')
    pending[2].resolve({ ...session({ id: 'chat_2' }), messages: [] })
    await other

    chatStreamController.enqueue(
      new TextEncoder().encode(sseFrame(TURN_DELTA, { text: '不该出现在 chat_2 里' })),
    )
    await tick()
    // ★ onEvent 里那句 `current.id !== sessionId` 的守卫
    expect(getChatSnapshot().streamingText).toBeNull()

    finishStream()
    await promise
  })
})

describe('附件条即发即收（v1.21）', () => {
  /*
    ★ 用户原话：「对话发送了文件还贴在这」。

      之前是**整轮跑完**之后才清 `stagedAttachments`，于是这期间输入框上方
      一直挂着那条附件 —— 而输入框此时已经切成「插话」模式，那条残留让人
      以为"下次发送还会带上"。上传物其实已经被这一轮读走了。
  */

  async function stageOne(): Promise<void> {
    const uploading = uploadAttachment('chat_1', new File(['x'], 'a.pcapng'))
    pending[1].resolve({ id: 'att1', name: 'a.pcapng', sizeBytes: 1 })
    await uploading
    expect(getChatSnapshot().stagedAttachments).toHaveLength(1)
  }

  it('★ 按发送那一刻就收起，不等整轮跑完', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open
    await stageOne()

    const stream = liveSseResponse()
    const promise = sendMessage('chat_1', '看这个包')
    pending[2].resolveRaw(stream.response)
    await tick()

    // 关键：这一轮还在跑（sending 仍 true），但附件条已经收起
    expect(getChatSnapshot().sending).toBe(true)
    expect(getChatSnapshot().stagedAttachments).toEqual([])

    stream.push(TURN_MESSAGE, message({ id: 'm1', seq: 1, role: 'user', content: '看这个包' }))
    stream.push(TURN_DONE, { ...DONE_FRAME, messageCount: 1 })
    stream.close()
    await promise
    if (pending[3]) pending[3].resolve([])
    expect(getChatSnapshot().stagedAttachments).toEqual([])
  })

  it('★ 发送失败要把附件条放回去 —— 否则用户得重新选文件', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open
    await stageOne()

    const promise = sendMessage('chat_1', '看这个包')
    pending[2].reject(new Error('接入点超时'))
    await promise

    expect(getChatSnapshot().stagedAttachments.map((a) => a.id)).toEqual(['att1'])
  })

  it('★ 后端已经开始这一轮之后失败，附件条**不要**放回来（文件已被搬走）', async () => {
    /*
      ★ 这条守的是「放回去」那条修复的边界。

        后端 `run_turn` 一进来就 `attachments.hand_over(...)`：暂存区的文件被
        **搬**到会话交接目录（挂进沙箱 `/work/attachments`）。所以只要这一轮
        真的开始跑了，服务端就不再有那些暂存文件 —— 此时把 chip 放回去，用户
        下一条消息会带一个查不到的 id 发出去，附件静默丢失。

        判据是 `pendingUserContent`：页面发送时写它，收到第一条 user 消息帧时
        清它。所以「收到了 user 帧然后失败」= 后端已开始 = 不放回。
    */
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open
    await stageOne()

    const stream = liveSseResponse()
    const promise = sendMessage('chat_1', '看这个包')
    pending[2].resolveRaw(stream.response)
    // 先来 user 消息帧（服务端已落库 → 附件已被 hand_over 搬走），再来 error 帧
    stream.push(TURN_MESSAGE, message({ id: 'm1', seq: 1, role: 'user', content: '看这个包' }))
    stream.push(TURN_ERROR, { message: '模型挂了' })
    stream.close()
    const ok = await promise
    if (pending[3]) pending[3].resolve([])

    expect(ok).toBe(false)
    expect(getChatSnapshot().stagedAttachments).toEqual([])
  })
})

describe('TURN_ERROR 挂的是带真因的 ApiError（v1.22）', () => {
  /*
    ★ 用户看到的是「请求失败，请稍后再试。」

      真因一直在 SSE 的 error 帧里（`detail`），但 store 把它塞进一个裸对象
      `{ message: detail } as ApiError` —— 类型断言不等于实例。页面渲染走
      `errorMessage()`，它的第一行是 `error instanceof ApiError`，裸对象为假，
      于是落到泛化兜底。这条钉住"error 必须是真 ApiError、message 是 detail"。
  */
  it('★ error 帧的真因进入 ApiError.message（不是泛化文案）', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const stream = liveSseResponse()
    const promise = sendMessage('chat_1', '你好')
    pending[1].resolveRaw(stream.response)

    stream.push(TURN_MESSAGE, message({ id: 'm1', seq: 1, role: 'user', content: '你好' }))
    stream.push(TURN_ERROR, {
      message: 'ChatEngineError: 调用模型失败：密钥无效或已过期（HTTP 401）。',
    })
    stream.close()
    await promise
    if (pending[2]) pending[2].resolve([])

    const err = getChatSnapshot().error
    expect(err?.message).toContain('密钥无效或已过期')
    // 关键：是 ApiError 实例，`errorMessage()` 才不会走兜底
    expect(err).toBeInstanceOf(ApiError)
  })

  it('★ error 帧没给原因时，至少说"没有给出原因"而不是空白', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const stream = liveSseResponse()
    const promise = sendMessage('chat_1', '你好')
    pending[1].resolveRaw(stream.response)

    stream.push(TURN_MESSAGE, message({ id: 'm1', seq: 1, role: 'user', content: '你好' }))
    stream.push(TURN_ERROR, {})
    stream.close()
    await promise
    if (pending[2]) pending[2].resolve([])

    const err = getChatSnapshot().error
    expect(err).toBeInstanceOf(ApiError)
    expect(err?.message).toContain('没有给出原因')
  })
})

describe('resumeTurn — 重试前清掉旧错误（v1.22）', () => {
  /*
    ★ 用户点「重试」= "我处理过了"。旧错误如果还挂在 error 上，用户会以为
      这次又是同一个错（哪怕这次成功了）。这条钉住"进函数先清"。
  */
  it('★ resumeTurn 一进来就把上一次的错误清掉', async () => {
    // 先制造一个错误状态
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open
    const stream = liveSseResponse()
    const p1 = sendMessage('chat_1', '你好')
    pending[1].resolveRaw(stream.response)
    stream.push(TURN_MESSAGE, message({ id: 'm1', seq: 1, role: 'user', content: '你好' }))
    stream.push(TURN_ERROR, { message: '密钥无效' })
    stream.close()
    await p1
    if (pending[2]) pending[2].resolve([])
    expect(getChatSnapshot().error).not.toBeNull()

    // 现在点重试：清错误 + 发请求
    const p2 = resumeTurn('chat_1', 'turn_x')
    // 请求还没返回，错误就应该已经清掉了
    expect(getChatSnapshot().error).toBeNull()
    pending[pending.length - 1].resolve({ messages: [] })
    await p2
  })
})

describe('humanizeDetail — 错误条不该以异常类名开头（v1.23）', () => {
  /*
    ★ 服务端 detail 形如 `ChatProviderAuthError: 模型接入点拒绝了…`。
      类名对用户是噪音，界面要从那句人话开始。
  */
  it('★ 剥掉 Python 异常类名前缀', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const stream = liveSseResponse()
    const promise = sendMessage('chat_1', '你好')
    pending[1].resolveRaw(stream.response)
    stream.push(TURN_MESSAGE, message({ id: 'm1', seq: 1, role: 'user', content: '你好' }))
    stream.push(TURN_ERROR, {
      message: 'ChatProviderAuthError: 模型接入点拒绝了这次请求（API key 无效或已过期）。',
    })
    stream.close()
    await promise
    if (pending[2]) pending[2].resolve([])

    const msg = getChatSnapshot().error?.message ?? ''
    expect(msg.startsWith('模型接入点拒绝')).toBe(true)
    expect(msg).not.toContain('ChatProviderAuthError')
  })

  it('★ 不误伤正常句子里的冒号', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const stream = liveSseResponse()
    const promise = sendMessage('chat_1', '你好')
    pending[1].resolveRaw(stream.response)
    stream.push(TURN_MESSAGE, message({ id: 'm1', seq: 1, role: 'user', content: '你好' }))
    stream.push(TURN_ERROR, { message: '原因：磁盘满了' })
    stream.close()
    await promise
    if (pending[2]) pending[2].resolve([])

    expect(getChatSnapshot().error?.message).toBe('原因：磁盘满了')
  })
})

describe('interject 的 no-turn 语义 + clearStaleSending（v1.24）', () => {
  /*
    ★ 服务端在"这个会话没有在跑的轮次"时回 409 `NO_RUNNING_TURN`。
      前端必须把它当**信号**而不是错误：`sending` 是过期的（done 帧丢了），
      要清掉它并退回正常发送，否则用户那句话被静默吞掉。
  */
  it('★ 服务端说没在跑 → 返回 no-turn，且不挂 error', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const p = interject('chat_1', '这一轮其实早结束了')
    const idx = calls.length - 1
    pending[idx].resolve(
      { error: { code: 'NO_RUNNING_TURN', message: '这个对话当前没有在跑的回复。' } },
      409,
    )
    const outcome = await p

    expect(outcome).toBe('no-turn')
    // ★ 关键：不是"失败"，不该把红条挂上去吓用户
    expect(getChatSnapshot().error).toBeNull()
  })

  it('★ clearStaleSending 会把过期的 sending/phase 清掉（否则发不出去）', () => {
    // 先造一个"以为在跑"的状态
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    return open.then(() => {
      // 直接改 store 里的 sending 需要走 sendMessage；这里用它的公开路径：
      const p = sendMessage('chat_1', '先跑一轮')
      // sendMessage 已把 sending 置 true（还没等响应）
      expect(getChatSnapshot().sending).toBe(true)
      expect(getChatSnapshot().sendingSessionId).toBe('chat_1')

      clearStaleSending()

      expect(getChatSnapshot().sending).toBe(false)
      expect(getChatSnapshot().sendingSessionId).toBeNull()
      expect(getChatSnapshot().phase).toBeNull()

      // 收尾：把悬挂的请求 resolve 掉
      pending[pending.length - 1].resolveRaw(
        sseResponse([sseFrame(TURN_DONE, DONE_FRAME)]),
      )
      return p
    })
  })
})
