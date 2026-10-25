import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  archiveSession,
  createSession,
  getChatSnapshot,
  loadSessions,
  loadTools,
  openSession,
  renameSession,
  resetChatStore,
  sendMessage,
} from './chat'

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
          reject,
        })
      })
    }),
  )
}

/** 等一轮微任务，让 store 里的 await 推进到下一个 pending。 */
function tick() {
  return new Promise((resolve) => setTimeout(resolve, 0))
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

describe('sendMessage', () => {
  it('★ 一轮追加的多条消息**整轮一次**接进 active', async () => {
    // 先打开会话
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const promise = sendMessage('chat_1', '帮我扫一下')
    // 服务端回：user + assistant(带动作) + tool 三条
    pending[1].resolve({
      messages: [
        message({ id: 'm1', seq: 1, role: 'user', content: '帮我扫一下' }),
        message({ id: 'm2', seq: 2, role: 'assistant', content: '好的，我先看看。' }),
        message({
          id: 'm3',
          seq: 3,
          role: 'tool',
          content: '建好了 scan_abc',
          toolName: 'create_scan',
        }),
      ],
    })
    // 不发第三个 pending 的话 loadSessions 会挂着，这里给它一个空列表
    const ok = await promise
    if (pending[2]) pending[2].resolve([])

    expect(ok).toBe(true)
    const active = getChatSnapshot().active
    expect(active?.messages.map((m) => m.role)).toEqual(['user', 'assistant', 'tool'])
    // messageCount 要跟着改 —— 否则左侧列表显示"0 条"而右边有三条。
    expect(active?.messageCount).toBe(3)
    // sending 必须复位，否则输入框永久禁用。
    expect(getChatSnapshot().sending).toBe(false)
  })

  it('★ 失败时返回 false，sending 也要复位', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const promise = sendMessage('chat_1', '你好')
    pending[1].reject(new Error('模型挂了'))
    const ok = await promise

    expect(ok).toBe(false)
    // 关键：sending 复位。不复位的话用户再也发不出消息，且没有任何提示。
    expect(getChatSnapshot().sending).toBe(false)
    expect(getChatSnapshot().error).not.toBeNull()
    // 已有消息不能被清掉。
    expect(getChatSnapshot().active?.messages).toEqual([])
  })

  it('★ 一轮在跑时不允许再发（否则 seq 顺序取决于谁先返回 → 消息乱序）', async () => {
    const open = openSession('chat_1')
    pending[0].resolve({ ...session(), messages: [] })
    await open

    const first = sendMessage('chat_1', '第一条')
    // 第一个还没回来就发第二条
    const second = await sendMessage('chat_1', '第二条')
    expect(second).toBe(false)
    // 第二条**不该**发出请求 —— 检查 fetch 只调了 2 次（open + first）
    expect(calls).toHaveLength(2)

    pending[1].resolve({ messages: [] })
    await first
    if (pending[2]) pending[2].resolve([])
  })

  it('★ 服务端返回的消息只接进**它自己**那个会话', async () => {
    const open1 = openSession('chat_1')
    pending[0].resolve({ ...session({ id: 'chat_1' }), messages: [] })
    await open1

    // 发到 chat_1（请求发出后不立刻 resolve）
    const promise = sendMessage('chat_1', '你好')

    // 期间用户切到 chat_2 —— 这里必须按**索引**取它自己的 pending：
    // 上面的 sendMessage 已经占了一个槽位，且它内部还会再发一个
    // loadSessions（`void` 出去的），所以槽位不能靠猜，按 URL 找。
    const open2 = openSession('chat_2')
    const openIdx = calls.findIndex(
      (c) => c.url.includes('/chat/sessions/chat_2') && c.method === 'GET',
    )
    pending[openIdx].resolve({ ...session({ id: 'chat_2' }), messages: [] })
    await open2

    // 现在让 chat_1 的那一轮回来
    const sendIdx = calls.findIndex(
      (c) => c.url.includes('/chat/sessions/chat_1/messages') && c.method === 'POST',
    )
    pending[sendIdx].resolve({ messages: [message({ id: 'x', seq: 1 })] })
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
