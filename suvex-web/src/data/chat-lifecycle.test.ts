import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  getChatSnapshot,
  loadSessions,
  pinSession,
  purgeSession,
  resetChatStore,
  unarchiveSession,
} from './chat'

/*
  ══════════════════════════════════════════════════════════════
  会话生命周期在 store 层的行为（v1.15）
  ══════════════════════════════════════════════════════════════

  ## 这一层独有的失败模式

  后端已经有测试守着「置顶要真改变顺序」「彻底删除要两道闸」。这里守的是
  **前端状态收敛**，它们在后端全绿时依然可能是错的：

  ① `loadSessions` 必须把归档的挑出去放进 `archivedSessions`。混在
     `sessions` 里的话，已归档的会话会跟正常的混排，而「已归档」区又是空的
     ——用户以为彻底没了；
  ② 置顶/恢复都靠**重新拉列表**收敛，不能只改本地那一条：排序是服务端算的，
     本地改个 `pinned` 字段并不会让它挪到最前面（那正是"点了没反应"）；
  ③ 彻底删除必须把这条同时从 `archivedSessions` 和 `active` 里摘掉，否则
     用户会看到一个已经不存在于服务端的会话，点进去 404。
*/

interface Deferred {
  resolve: (body: unknown, status?: number) => void
  reject: (error: Error) => void
}

let pending: Deferred[] = []
let calls: { url: string; method: string }[] = []

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
          resolve: (body, status = 200) =>
            resolve(
              new Response(JSON.stringify(body), {
                status,
                headers: { 'Content-Type': 'application/json' },
              }),
            ),
          reject,
        })
      })
    }),
  )
}

function session(over: Record<string, unknown>) {
  return {
    id: 'chat_1',
    title: '对话',
    messageCount: 0,
    archived: false,
    pinned: false,
    createdAt: '2026-10-27T10:00:00Z',
    updatedAt: '2026-10-27T10:00:00Z',
    ...over,
  }
}

afterEach(() => {
  resetChatStore()
  vi.unstubAllGlobals()
})

describe('loadSessions', () => {
  it('把已归档的挑进 archivedSessions，不混在正常列表里', async () => {
    stubFetch()
    resetChatStore()
    const loading = loadSessions()
    pending[0].resolve([
      session({ id: 'chat_live', title: '在用' }),
      session({ id: 'chat_gone', title: '归档了', archived: true }),
    ])
    await loading

    const snapshot = getChatSnapshot()
    expect(snapshot.sessions.map((s) => s.id)).toEqual(['chat_live'])
    expect(snapshot.archivedSessions.map((s) => s.id)).toEqual(['chat_gone'])
  })

  it('请求带 includeArchived=true —— 否则「已归档」永远是空的', async () => {
    stubFetch()
    resetChatStore()
    const loading = loadSessions()
    pending[0].resolve([])
    await loading
    expect(calls[0].url).toContain('includeArchived=true')
  })
})

describe('pinSession', () => {
  it('POST 到 /pin 并带上目标状态，然后重新拉列表（排序由服务端算）', async () => {
    stubFetch()
    resetChatStore()

    const pinning = pinSession('chat_1', true)
    // 第一个请求是 POST /pin
    expect(calls[0].url).toContain('/chat/sessions/chat_1/pin')
    expect(calls[0].method).toBe('POST')
    pending[0].resolve(session({ id: 'chat_1', pinned: true }))

    // 成功后必须再拉一次列表 —— 本地改字段不会让它在列表里挪位置
    await vi.waitFor(() => expect(calls.length).toBe(2))
    expect(calls[1].url).toContain('/chat/sessions')
    pending[1].resolve([
      session({ id: 'chat_1', title: '置顶的', pinned: true }),
      session({ id: 'chat_2', title: '普通的' }),
    ])

    expect(await pinning).toBe(true)
    expect(getChatSnapshot().sessions[0].id).toBe('chat_1')
  })

  it('失败时返回 false 并把错误放进 state（不伪装成功）', async () => {
    stubFetch()
    resetChatStore()
    const pinning = pinSession('chat_1', true)
    pending[0].resolve({ error: { code: 'NOT_FOUND', message: '没这个对话' } }, 404)
    expect(await pinning).toBe(false)
    expect(getChatSnapshot().error).toBeTruthy()
  })
})

describe('unarchiveSession', () => {
  it('POST 到 /unarchive 并重新拉列表，归档区随之清空', async () => {
    stubFetch()
    resetChatStore()

    const restoring = unarchiveSession('chat_gone')
    expect(calls[0].url).toContain('/chat/sessions/chat_gone/unarchive')
    expect(calls[0].method).toBe('POST')
    pending[0].resolve(session({ id: 'chat_gone', archived: false }))

    await vi.waitFor(() => expect(calls.length).toBe(2))
    pending[1].resolve([session({ id: 'chat_gone', title: '回来了' })])

    expect(await restoring).toBe(true)
    const snapshot = getChatSnapshot()
    expect(snapshot.sessions.map((s) => s.id)).toEqual(['chat_gone'])
    expect(snapshot.archivedSessions).toEqual([])
  })
})

describe('purgeSession', () => {
  it('成功后从 archivedSessions 摘掉（不重新拉列表：这条已经不存在了）', async () => {
    stubFetch()
    resetChatStore()

    // ★ 先让 archivedSessions 里真的有一条。若直接 purge 一个空的归档区，
    //   "删除本地那条"与"什么都不做"结果一样 —— 这个断言会永远绿，
    //   漏掉的就是真实缺口（删完之后侧栏还挂着一条点进去 404 的会话）。
    const loading = loadSessions()
    pending[0].resolve([session({ id: 'chat_gone', title: '归档了', archived: true })])
    await loading
    expect(getChatSnapshot().archivedSessions.map((s) => s.id)).toEqual(['chat_gone'])

    const purging = purgeSession('chat_gone')
    expect(calls[1].url).toContain('/chat/sessions/chat_gone/purge')
    pending[1].resolve(session({ id: 'chat_gone', archived: true }))
    expect(await purging).toBe(true)

    // 只多发了一个请求（purge 本身）：之后没必要再拉列表
    expect(calls.length).toBe(2)
    expect(getChatSnapshot().archivedSessions.some((s) => s.id === 'chat_gone')).toBe(false)
  })

  it('409（没归档）时返回 false 并报错，绝不静默', async () => {
    stubFetch()
    resetChatStore()
    const purging = purgeSession('chat_1')
    pending[0].resolve(
      { error: { code: 'CHAT_SESSION_NOT_ARCHIVED', message: '先删除它' } },
      409,
    )
    expect(await purging).toBe(false)
    expect(getChatSnapshot().error).toBeTruthy()
  })
})
