import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/*
  ══════════════════════════════════════════════════════════════
  LoginPage 的两个 phase 分支（2026-10-18）
  ══════════════════════════════════════════════════════════════

  ## 为什么测这个

  `src/pages/` 此前**零测试文件** —— 缺口清单里那条「前端测试仍不覆盖
  页面渲染」说的就是它：页面级的崩溃（少一个 null 检查、hook 顺序错了、
  某个字段在空数据下没定义）只有人工点开页面才能发现。

  LoginPage 是这批页面里**最该有测试的一个**，因为它在
  `auth-store` 的 phase 机器里处于最容易出错的位置：

    · `offline`（后端没起）→ **不能**显示登录表单
    · `login`（后端在、未登录）→ 显示表单

  只有这两个。`App.tsx:152` 是 `decideLoginRoute(phase)` 为真时才渲染
  LoginPage，而它只对 `login` / `offline` 返回真 —— 所以 `loading` /
  `ready` / `setup` 下这个组件**根本不会挂载**，给它们写用例是在测一个
  不可能出现的输入。

  第一条是真会误导人的：后端连不上时如果照样显示表单，用户会去输口令，
  然后拿到一个和口令毫无关系的失败 —— 而 `auth-store` 的注释写明
  `offline` 是个**独立状态**、正是为了避免这一点。这段推理如果哪天
  被重构掉，表现是"登录失败但口令是对的"，不会有任何测试变红。

  ## 为什么断言文案而不是内部状态

  用户看到的就是文案与输入框。断言 `phase === 'offline'` 会让
  "状态算对了但渲染时用错了分支"漏过去 —— 而那正是要防的。

  ## ★ 必须手动 cleanup

  同 `ErrorState.test.tsx`：`vitest.config.ts` 里刻意关了 `globals`，
  于是 `@testing-library/react` 的自动 cleanup **不会注册**。不手动清，
  DOM 会在用例之间累积，第二个用例开始 `getByRole` 就报
  `Found multiple elements`。
*/

vi.mock('@/data/auth-store', () => ({
  useAuth: vi.fn(),
  // LoginPage 在挂载时调 bootstrapAuth、卸载/提交时调 clearAuthError，
  // 这里给空实现即可 —— 测的是渲染分支，不是这些动作的副作用。
  bootstrapAuth: vi.fn(),
  clearAuthError: vi.fn(),
  login: vi.fn(),
}))

// 注意 import 顺序：vi.mock 会被提升到所有 import 之前，
// 所以这里拿到的 useAuth 已经是替身。
import { useAuth } from '@/data/auth-store'
import LoginPage from '@/pages/LoginPage'

const mockedUseAuth = vi.mocked(useAuth)

function setPhase(phase: 'login' | 'offline', error: string | null = null) {
  // `pending` 是提交中的标志（auth-store 里 `login()` 一开始就置 true），
  // 这几条用例测的是"渲染哪个分支"，都按空闲态给 false。
  mockedUseAuth.mockReturnValue({ phase, user: null, pending: false, error })
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  cleanup()
})

describe('LoginPage 按 auth phase 渲染', () => {
  it('offline：说"连不上后端"，不给登录表单', () => {
    setPhase('offline', '无法连接后端服务。')

    render(<LoginPage />)

    // ★ 核心断言：表单**不该**出现。
    //   出现了就意味着用户会拿口令去喂一个根本连不上的后端。
    expect(screen.queryByRole('button', { name: /登录/ })).toBeNull()
    expect(screen.queryByLabelText(/用户名/)).toBeNull()
  })

  it('login：渲染用户名 / 口令两个输入框与提交按钮', () => {
    setPhase('login')

    render(<LoginPage />)

    expect(screen.getByLabelText(/用户名/)).toBeTruthy()
    expect(screen.getByLabelText(/口令|密码/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /登录/ })).toBeTruthy()
  })

  it('offline 且 error 为空：用兜底文案，不是空白页', () => {
    // `LoginPage.tsx:74` 写的是 `error ?? '连接不上后端服务。'`。
    // 那个 `??` 是给"状态变成了 offline 但手上没有现成句子"的情况兜底的 ——
    // 少了它就是一片空白加一个重试按钮，用户不知道发生了什么。
    setPhase('offline')

    render(<LoginPage />)

    // 用 getAllByText 而不是 getByText：这句兜底文案会同时出现在
    // AuthShell 的标题和 ErrorState 里 —— 两处都该有，不是重复渲染。
    expect(screen.getAllByText(/连接不上后端服务/).length).toBeGreaterThan(0)
  })

  it('login 且带 error：把错误显示成 alert', () => {
    setPhase('login', '用户名或口令不正确。')

    render(<LoginPage />)

    // role="alert" 是屏幕阅读器会立刻朗读的角色 —— 登录失败必须用它，
    // 只加个红字的话读屏用户不会知道发生了什么。
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toContain('用户名或口令不正确')
  })
})
