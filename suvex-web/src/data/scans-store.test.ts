import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { loadScans, replaceScans, subscribeScans, getScansSnapshot, getViewSnapshot } from './scans-store'

/*
  ══════════════════════════════════════════════════════════════
  scans-store 的 in-flight 去重
  ══════════════════════════════════════════════════════════════

  ## 为什么测这个

  它是一段**并发正确性**逻辑，而且规则不是"能合并就合并"那么朴素：
  请求分强弱（silent / 非 silent），还多一个新鲜度维度（`fresh`）。
  三者的组合表有 8 格，其中两格是**刻意不合**的：

    · 在飞 silent + 新来非 silent → 不合（否则用户点「重试」毫无反应）
    · `fresh: true` → 一律不合（否则写操作后的列表看不到刚改的那条）

  这两条都属于"看起来可以优化、其实会把错误吞掉"的地方。
  2026-10-10 我在真机浏览器里手工验过一次（同视图并发两发只发 1 个
  请求、silent 在飞时非 silent 照发 2 发），但那是一次性的 ——
  这个文件把它变成每次提交都会跑的东西。

  ## 怎么隔离

  store 通过 `lib/api` 走 `fetch`。这里替换全局 `fetch`，并让每个
  用例自己控制"什么时候 resolve" —— 因为要测的正是**在飞期间**的行为，
  请求立刻完成就什么都测不到。
*/

/** 记录所有发出的请求 URL */
let requested: string[] = []
/** 手动控制 resolve 的挂起器 */
let pending: Array<{ url: string; resolve: (rows: unknown[]) => void; reject: (e: unknown) => void }> = []

function installFetchStub() {
  requested = []
  pending = []

  vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString()
    requested.push(url)
    return new Promise<Response>((resolvePromise, rejectPromise) => {
      pending.push({
        url,
        resolve: (rows) =>
          resolvePromise(
            new Response(JSON.stringify(rows), {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }),
          ),
        reject: (error) => rejectPromise(error),
      })
    })
  })
}

/** 让**所有**挂起的请求成功返回（默认空列表） */
async function flushAll(rows: unknown[] = []) {
  const waiting = pending
  pending = []
  for (const item of waiting) item.resolve(rows)
  /*
    ⚠️ 这里必须把宏任务也让出去，不能只 await 几个 microtask。

    `loadScans` 返回的 Promise 链是
        fetchScans()  →  .finally(() => { delete inFlight[view] })
    而 `fetchScans` 内部还要 `await response.json()` —— 那也是一个
    异步步。只 await 3 次 microtask 时，`finally` 还没跑，
    于是 `inFlight` 里留着已完成的记录，下一次调用被误判成"还在飞"
    而复用了它。

    症状是测试里"请求数少了一个"，很容易被误读成产品 bug。
    用 setTimeout(0) 让出宏任务，链就真的跑完了。
  */
  await new Promise((resolve) => setTimeout(resolve, 0))
}

beforeEach(() => {
  installFetchStub()
  // 每个用例从干净状态开始：把默认视图替换成空列表
  replaceScans([])
})

afterEach(() => {
  vi.unstubAllGlobals()
})

// ══════════════════════════════════════════════════════════════
// 一、复用规则表
// ══════════════════════════════════════════════════════════════

describe('in-flight 复用规则', () => {
  it('★ 同一视图并发两发，只发一个请求且拿到同一个 Promise', async () => {
    /*
      这是那次真实重叠的复现：进扫描列表页时，`Layout` 的
      `ensureScansLoaded()` 与页面自己的加载 effect 在**同一帧**里
      都走到 `loadScans`，于是 `GET /scans` 发了两遍。

      断言两件事：
        · 只发一个请求（去重生效）；
        · 两发拿到**同一个 Promise 引用**（而不是各自包一层）。
      只断言请求数会漏掉"第二个调用者拿到一个不同的 Promise、
      于是 await 的时机不对"这种实现。
    */
    const first = loadScans({ view: 'archived' })
    const second = loadScans({ view: 'archived' })

    expect(requested).toHaveLength(1)
    expect(first).toBe(second)

    await flushAll()
    await Promise.all([first, second])
  })

  it('在飞的是 silent、新来的也是 silent → 复用', async () => {
    // 两个都是"顺便刷一下"，没必要发两遍
    loadScans({ view: 'archived', silent: true })
    loadScans({ view: 'archived', silent: true })

    expect(requested).toHaveLength(1)
    await flushAll()
  })

  it('在飞的是非 silent、新来的是 silent → 复用（强的吞弱的）', async () => {
    // 非 silent 会给出更全的结果（含 loading 态与失败处理）
    loadScans({ view: 'archived' })
    loadScans({ view: 'archived', silent: true })

    expect(requested).toHaveLength(1)
    await flushAll()
  })

  it('★ 在飞的是 silent、新来的是非 silent → **不复用**，自己发', async () => {
    /*
      这是设计里唯一不显然的一格，也是最不能省的一条。

      非 silent 的调用者是**用户刚点了「重试」**，它必须真的把
      `loading` 摆出来。若复用了那个 silent 请求，页面上什么都不会变 ——
      而 silent 分支**连失败都不改状态**，于是用户点了重试、接口 500，
      界面停在原地一个提示都没有。

      那不是省一次请求，是把错误吞掉。
    */
    loadScans({ view: 'archived', silent: true })
    loadScans({ view: 'archived' })

    expect(requested).toHaveLength(2)
    await flushAll()
  })

  it('★ fresh: true 一律不复用', async () => {
    /*
      复用意味着"结果来自更早发出的那个请求"。对轮询无所谓，
      但对**刚做完写操作**的调用就是错的 —— 那个在飞的请求是在
      写操作**之前**发出的，它返回的列表里没有刚改完的那条。

      症状：撤销删除后 toast 说"已恢复 1 项"，而列表里没有那条，
      切走再切回来才出现。
    */
    loadScans({ view: 'archived' })
    loadScans({ view: 'archived', fresh: true })

    expect(requested).toHaveLength(2)
    await flushAll()
  })

  it('★ 不同视图之间**不**共用 Promise', async () => {
    /*
      三个视图是三个不同的端点。共用一个 Promise 会让「切到回收站」
      拿到默认视图的列表 —— 那比多一个请求糟糕得多。
    */
    loadScans({ view: 'default' })
    loadScans({ view: 'archived' })
    loadScans({ view: 'deleted' })

    expect(requested).toHaveLength(3)
    await flushAll()
  })
})

// ══════════════════════════════════════════════════════════════
// 二、清理：槽位不能被错误地抹掉
// ══════════════════════════════════════════════════════════════

describe('在飞记录的生命周期', () => {
  it('★ 请求结束后记录被清掉，下一次调用会重新发', async () => {
    loadScans({ view: 'archived' })
    expect(requested).toHaveLength(1)
    await flushAll()

    // 前一个已经完成，不应再被复用
    loadScans({ view: 'archived' })
    expect(requested).toHaveLength(2)
    await flushAll()
  })

  it('★ 被顶掉的 silent 请求结束时，不能抹掉后来者的记录', async () => {
    /*
      这是最容易写错的一处：一个非 silent 请求会**顶掉**正在飞的
      silent 记录（按规则表它不复用，而是自己发并覆盖槽位）。
      若那个旧的 silent 结束时无条件 `delete inFlight[view]`，
      它抹掉的是**后来者**的记录 —— 于是后来者还在飞，而 store
      以为自己空闲，下一次调用又发一发新的。

      去重就废了，而且是间歇性失效，最难查。

      复现步骤：
        1. 发一个 silent（占住槽位）
        2. 发一个非 silent（顶掉槽位，自己发）
        3. 让**旧的 silent** 先结束
        4. 此时再发一个 → 应当复用第 2 步那个（因为它还在飞）
    */
    loadScans({ view: 'archived', silent: true })
    loadScans({ view: 'archived' })
    expect(requested).toHaveLength(2)

    // 只让**第一个**（silent）结束
    const silentOne = pending[0]
    pending = pending.slice(1)
    silentOne.resolve([])
    // 等它整条链跑完（含 `.finally` 里那次"要不要清槽位"的判断）——
    // 这正是本测试要验的时刻，不能只让出 microtask
    await new Promise((resolve) => setTimeout(resolve, 0))

    // 第 2 步那个非 silent 仍在飞 —— 新的调用应当复用它
    const after = loadScans({ view: 'archived' })
    expect(requested).toHaveLength(2) // 没有第 3 个请求

    await flushAll()
    await after
  })
})

// ══════════════════════════════════════════════════════════════
// 三、失败路径
// ══════════════════════════════════════════════════════════════

describe('失败处理', () => {
  it('非 silent 失败时把状态写成 error', async () => {
    const promise = loadScans({ view: 'archived' })
    const item = pending[0]
    pending = []
    item.reject(new TypeError('Failed to fetch'))
    await promise.catch(() => undefined)
    await Promise.resolve()

    /* ★ 2026-10-24 修：这条用例原来只断言 `getScansSnapshot()` 是
       `toBeDefined()` —— 而那是**默认视图**的快照，跟本次失败的
       archived 槽毫无关系。把源码里 `status: 'error'` 改成 `'ready'`
       之后它依然全绿（变异实测过），等于名字在骗人。

       错误写在**自己那个视图的槽**里，所以必须读那个槽。 */
    const archived = getViewSnapshot('archived')
    expect(archived.status).toBe('error')
    /* `lib/api` 会把网络层错误规范化成给人看的中文，所以这里断言的是
       「确实带了错误信息」而不是原始 `Failed to fetch` —— 断字符串内容
       会让这条用例绑在文案上，改一次提示就红一次。 */
    expect(archived.error).not.toBeNull()
    expect(archived.error?.message.length ?? 0).toBeGreaterThan(0)
    // 失败不该把列表清成"什么都没有"的假象。
    expect(archived.scans).toEqual([])

    /* 默认视图没参与这次请求，它的槽不该被顺手写坏。
       不断言具体值（默认槽的初始 `loading` 会被本文件前面的用例推进到
       `ready`），只断言**没被这次失败带成 error** —— 那才是这条守的东西。 */
    expect(getScansSnapshot().status).not.toBe('error')
  })

  it('★ silent 失败时**不**把状态写成 error', async () => {
    /*
      静默轮询的失败必须压掉：一次超时不代表列表没了，
      而下一次轮询可能就成功。把用户正在看的列表换成错误页，
      是把"数据暂时没更新"表现成"页面坏了"。
    */
    const views: string[] = []
    const unsubscribe = subscribeScans(() => views.push('notified'))

    const promise = loadScans({ view: 'default', silent: true })
    const item = pending[0]
    pending = []
    item.reject(new TypeError('Failed to fetch'))
    await promise.catch(() => undefined)
    await Promise.resolve()

    // 状态不应变成 error
    expect(getScansSnapshot().status).not.toBe('error')
    unsubscribe()
  })
})
