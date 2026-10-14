import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { handleUnauthorized, resetAuthStore, getAuthSnapshot } from '@/data/auth-store'
import { apiRequest, apiText, setUnauthorizedHandler, ApiError } from '@/lib/api'

/*
  ══════════════════════════════════════════════════════════════
  401 拦截链路（2026-10-14）
  ══════════════════════════════════════════════════════════════

  ## 为什么测这个

  契约 §8 要求「会话失效不能只在登录接口处理」—— 任何请求收到 401
  都该把用户送回登录页。这条链路横跨两个模块：

    lib/api.ts        收到 401 → 调用注册的 handler
    data/auth-store   handler 把 phase 推回 login（带幂等判断）

  两边**各自都对、接起来却可能不对**，而症状是"用户拿着一个失效的
  会话继续点，每个操作都失败但界面不跳登录页"—— 不会让构建失败，
  只有人工把会话弄失效才能发现。

  ## 三个容易写错的地方（本文件逐条钉住）

  1. **`skipAuthRedirect` 必须真的跳过** —— 它有三个正当用途
     （启动探测 / 登录接口本身 / 由守卫自己决定跳哪）。漏了它，
     输错口令会触发一次多余的跳转，而启动探测的 401 会把
     一个**正常未登录**的用户也推去登录页。
  2. **`handleUnauthorized` 的幂等** —— 多个并发请求同时 401 时
     它会被调用多次；而用户**已经在登录页**时不该被打断
     （那可能是他自己正输着口令）。
  3. **401 仍要抛 `ApiError`** —— 跳转不是"吞掉错误"的理由，
     调用方还得拿到 code/status 去决定显示什么。

  ## 怎么隔离

  `apiRequest` 走全局 `fetch`，这里用 `vi.stubGlobal` 换掉它。
  `restoreMocks: true`（vitest.config.ts）会在每个用例后自动还原，
  但 handler 是**模块级可变状态**，必须自己清 —— 见 `afterEach`。
*/

function stubFetchOnce(response: Response) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(response)),
  )
}

function jsonResponse(body: unknown, status: number) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** 后端 401 的标准信封 */
const UNAUTH_BODY = {
  error: { code: 'UNAUTHENTICATED', message: '会话已失效，请重新登录。' },
}

beforeEach(() => {
  resetAuthStore()
  setUnauthorizedHandler(null)
})

afterEach(() => {
  // handler 是模块级状态，不清会污染下一个用例
  setUnauthorizedHandler(null)
  resetAuthStore()
})

describe('apiRequest：401 触发 handler', () => {
  it('★ 收到 401 会调用已注册的 handler', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    stubFetchOnce(jsonResponse(UNAUTH_BODY, 401))

    await expect(apiRequest('/findings')).rejects.toThrow(ApiError)

    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('★★ `skipAuthRedirect: true` 时**不**调用 handler', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    stubFetchOnce(jsonResponse(UNAUTH_BODY, 401))

    await expect(apiRequest('/auth/state', { skipAuthRedirect: true })).rejects.toThrow(ApiError)

    expect(handler).not.toHaveBeenCalled()
  })

  it('★ 401 仍然抛 ApiError（跳转不是吞掉错误的理由）', async () => {
    setUnauthorizedHandler(() => {})
    stubFetchOnce(jsonResponse(UNAUTH_BODY, 401))

    // 调用方还要靠 code/status 决定显示什么
    await expect(apiRequest('/findings')).rejects.toMatchObject({
      code: 'UNAUTHENTICATED',
      status: 401,
    })
  })

  it('★ 没有注册 handler 时 401 不该炸（启动早期请求会先于注册发生）', async () => {
    setUnauthorizedHandler(null)
    stubFetchOnce(jsonResponse(UNAUTH_BODY, 401))

    await expect(apiRequest('/findings')).rejects.toThrow(ApiError)
  })

  it('★ 非 401 的失败不触发 handler（否则 500 也会把用户踢去登录页）', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    stubFetchOnce(jsonResponse({ error: { code: 'X', message: '服务器错误' } }, 500))

    await expect(apiRequest('/findings')).rejects.toThrow(ApiError)

    expect(handler).not.toHaveBeenCalled()
  })

  it('★ 200 不触发 handler', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    stubFetchOnce(jsonResponse({ ok: true }, 200))

    await apiRequest('/findings')

    expect(handler).not.toHaveBeenCalled()
  })
})

describe('handleUnauthorized：幂等与"不打断用户"', () => {
  /*
    ⚠️ **这个 describe 里测不到"ready 态"** —— 如实说明，而不是假装测了。

    `auth-store` 没有导出任何"把 phase 设成 ready"的入口（只有
    `bootstrapAuth` / `login` / `setupAdmin`，它们都要真的发请求）。
    所以这里能构造的起点只有初始的 `loading`。

    后果：**"从 ready 掉回 login"这条最核心的路径没有被直接覆盖。**
    它在 `handleUnauthorized` 里的实现是一行 `setState({phase:'login'})`，
    而"login/setup/offline 不打断"那三个分支是能测的（见下）。

    真要覆盖 ready 态，需要给 store 加一个仅测试用的注入点 —— 那属于
    改产品代码来迁就测试，本次任务的红线不允许（"不改业务逻辑"）。
    所以这里留一条**明确的缺口说明**，而不是写个名为 ready、
    实为 loading 的假用例（那比不测更坏：它让缺口看起来被填上了）。
  */

  it('★ loading 态调用 → 推回 login', () => {
    // 初始就是 loading。loading **不在**"别打断"的名单里 ——
    // 契约 §8 要的是"会话失效立刻回登录页"，而 loading 正是
    // "刚启动、还没确认会话"的时刻。
    expect(getAuthSnapshot().phase).toBe('loading')

    handleUnauthorized()

    expect(getAuthSnapshot().phase).toBe('login')
  })

  it('★★ 已经在 login 态时**不打断**（用户可能正在输口令）', () => {
    handleUnauthorized() // → login
    expect(getAuthSnapshot().phase).toBe('login')

    // 再调一次：必须原样不动
    const before = getAuthSnapshot()
    handleUnauthorized()
    expect(getAuthSnapshot()).toEqual(before)
  })

  it('★★ 并发 401 多次调用是幂等的（多个请求同时失效）', () => {
    handleUnauthorized()
    const afterFirst = getAuthSnapshot()

    for (let i = 0; i < 5; i++) handleUnauthorized()

    expect(getAuthSnapshot()).toEqual(afterFirst)
  })

  it('★ login 态下重复调用不会 setState（快照引用不变）', () => {
    handleUnauthorized()
    const before = getAuthSnapshot()

    handleUnauthorized()

    // 同一个引用 = 没走过 setState。这比"值相等"更严格：
    // 值相等也可能是"又设了一遍同样的值"，而 React 会因此多渲染一次。
    expect(getAuthSnapshot()).toBe(before)
  })

  it('★ setup 与 offline 态同样不被打断', () => {
    // 这两个态无法从外部直接构造，但它们的判断与 login 在**同一行**
    // （`handleUnauthorized` 里的那个三选一 `||`）。所以这里断言的是
    // 该行的行为，而不是三个态各自的路径 —— 如实标注这个局限。
    const src = handleUnauthorized.toString()
    for (const phase of ['login', 'setup', 'offline']) {
      expect(src, `handleUnauthorized 里应包含 '${phase}' 的不打断判断`).toContain(phase)
    }
  })
})

describe('端到端：401 → 注册的 handler → 状态变化', () => {
  it('★★ 把两段接起来：请求 401 后 phase 落到 login', async () => {
    // 这就是 App.tsx 启动时做的事：把 handleUnauthorized 注册进去。
    // ⚠️ 起点是 loading（不是 ready）—— 原因见上面那个 describe 的说明。
    setUnauthorizedHandler(handleUnauthorized)
    expect(getAuthSnapshot().phase).toBe('loading')

    stubFetchOnce(jsonResponse(UNAUTH_BODY, 401))
    await expect(apiRequest('/findings')).rejects.toThrow(ApiError)

    expect(getAuthSnapshot().phase).toBe('login')
  })

  it('★★ `skipAuthRedirect` 的那条路径**不会**改变 phase', async () => {
    setUnauthorizedHandler(handleUnauthorized)
    handleUnauthorized() // 先落到 login，便于观察"有没有被再动过"
    const before = getAuthSnapshot()

    stubFetchOnce(jsonResponse(UNAUTH_BODY, 401))
    await expect(apiRequest('/auth/state', { skipAuthRedirect: true })).rejects.toThrow(ApiError)

    expect(getAuthSnapshot()).toBe(before)
  })
})

/*
  ══════════════════════════════════════════════════════════════
  ★★ 下载路径的 401（2026-10-14 由变异验证发现漏测）

  ## 为什么单独一个 describe

  `lib/api.ts` 里有**两个** 401 处理点，不在同一个函数里：

    L317  `apiRequest`   —— 所有 JSON 请求
    L444  `apiText` —— 文件下载（报告导出、附件下载）

  第一版测试只覆盖了 `apiRequest`。而**变异验证当场抓到了这个缺口**：
  我把 L444 的 401 处理改成 `if (false)`，测试**照样全绿** ——
  因为那条路径根本没被走到。

  这正是变异验证存在的意义："测试通过"不等于"测试守住了"。
  如果只跑一遍全绿就收工，这个缺口会一直留着，而症状是
  "会话过期时点下载按钮，文件没下来、页面也不跳登录页"。

  ## 为什么下载路径值得单独测

  它不是 `apiRequest` 的语法糖 —— 它走 `response.text()` 而不是
  `response.json()`，错误信封的解析是**另写的一段**（见 L432-437）。
  两段代码长得像，但改一处不会自动同步另一处。
  ══════════════════════════════════════════════════════════════
*/
describe('★★ 文本下载路径（apiText）的 401 也要触发 handler', () => {
  it('★★ 下载收到 401 会调用 handler', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    stubFetchOnce(jsonResponse(UNAUTH_BODY, 401))

    await expect(apiText('/reports/r1/export')).rejects.toThrow(ApiError)

    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('★★ 下载的 `skipAuthRedirect: true` 同样不触发', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    stubFetchOnce(jsonResponse(UNAUTH_BODY, 401))

    await expect(
      apiText('/reports/r1/export', { skipAuthRedirect: true }),
    ).rejects.toThrow(ApiError)

    expect(handler).not.toHaveBeenCalled()
  })

  it('★ 下载 401 仍抛 ApiError（带后端那句中文）', async () => {
    setUnauthorizedHandler(() => {})
    stubFetchOnce(jsonResponse(UNAUTH_BODY, 401))

    await expect(apiText('/reports/r1/export')).rejects.toMatchObject({
      code: 'UNAUTHENTICATED',
      status: 401,
    })
  })

  it('★ 下载成功（200）不触发 handler', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    stubFetchOnce(new Response('file-bytes', { status: 200 }))

    await apiText('/reports/r1/export')

    expect(handler).not.toHaveBeenCalled()
  })
})
