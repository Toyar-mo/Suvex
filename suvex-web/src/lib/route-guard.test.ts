import { describe, expect, it } from 'vitest'

import type { AuthPhase } from '@/data/auth-store'
import {
  decideLoginRoute,
  decideRequireAuth,
  decideSetupRoute,
  type RouteDecision,
} from '@/lib/route-guard'

/*
  ─────────────────────────────────────────────────────────────
  路由守卫的决策表（2026-10-14）

  ## 为什么测这个

  这三个判断此前是 `App.tsx` 里的三段 `if`，**没有任何测试**。
  而它们决定"一个未登录的人能看见什么"：

    · 判宽了 → 受保护页面放给未登录用户（**安全问题**）；
    · 判严了 → 已登录的人被永远卡在登录页（**功能不可用**）。

  两种都不会让构建失败，只有人工点开页面才发现 —— 这正是
  README 缺口清单「前端测试仍不覆盖页面渲染」说的那类风险。

  ## 为什么是表驱动

  "5 个 phase × 3 个入口"本来就是一维的映射关系。写成 15 个
  独立 `it` 会让"哪一格是什么"淹没在样板里；一张表则让
  **缺格、重复、写错**一眼可见。

  ## ★ 期望值是怎么来的

  每一条都是**照着原 `App.tsx` 的 `if` 顺序抄下来的**，
  不是"我觉得应该这样"。这次是重构（抽函数），不是改行为 ——
  所以表里任何一格与原文不符，都说明抽错了。
  ─────────────────────────────────────────────────────────────
*/

const ALL_PHASES: AuthPhase[] = ['loading', 'setup', 'login', 'offline', 'ready']

const BOOT: RouteDecision = { kind: 'boot' }
const RENDER: RouteDecision = { kind: 'render' }
const TO_SETUP: RouteDecision = { kind: 'redirect', to: 'setup' }
const TO_LOGIN: RouteDecision = { kind: 'redirect', to: 'login' }
const TO_DEFAULT: RouteDecision = { kind: 'redirect', to: 'default' }

describe('decideRequireAuth（受保护区：六个 app 页面）', () => {
  const table: Array<[AuthPhase, RouteDecision]> = [
    // 还在问 /auth/state —— 显示兜底，**不能**先闪一下登录页
    ['loading', BOOT],
    // 未初始化 → 去初始化页（没有账号可登）
    ['setup', TO_SETUP],
    // 已初始化但没登录 → 去登录页
    ['login', TO_LOGIN],
    // 后端连不上 → 也去登录页（LoginPage 自己按 phase 显示网络错误）
    ['offline', TO_LOGIN],
    // 只有这一格放行
    ['ready', RENDER],
  ]

  for (const [phase, expected] of table) {
    it(`${phase} → ${expected.kind}${'to' in expected ? ` ${expected.to}` : ''}`, () => {
      expect(decideRequireAuth(phase)).toEqual(expected)
    })
  }

  it('★ 覆盖了全部 5 个 phase（漏一个就等于漏一个分支）', () => {
    expect(table.map(([p]) => p).sort()).toEqual([...ALL_PHASES].sort())
  })

  it('★ 只有 ready 放行 —— 其余一律挡住', () => {
    const allowed = ALL_PHASES.filter((p) => decideRequireAuth(p).kind === 'render')
    expect(allowed).toEqual(['ready'])
  })
})

describe('decideLoginRoute（/login）', () => {
  const table: Array<[AuthPhase, RouteDecision]> = [
    ['loading', BOOT],
    // 还没初始化就没有账号可对 —— 让用户对着登不进去的表单试是浪费
    ['setup', TO_SETUP],
    // ★ 已登录的人不该停在登录页
    ['ready', TO_DEFAULT],
    // 这两种才真的渲染登录页
    ['login', RENDER],
    ['offline', RENDER],
  ]

  for (const [phase, expected] of table) {
    it(`${phase} → ${expected.kind}${'to' in expected ? ` ${expected.to}` : ''}`, () => {
      expect(decideLoginRoute(phase)).toEqual(expected)
    })
  }

  it('★ 覆盖了全部 5 个 phase', () => {
    expect(table.map(([p]) => p).sort()).toEqual([...ALL_PHASES].sort())
  })

  it('★ offline 也渲染登录页（由 LoginPage 自己显示网络错误，不在这里拦）', () => {
    expect(decideLoginRoute('offline')).toEqual(RENDER)
  })
})

describe('decideSetupRoute（/setup）', () => {
  const table: Array<[AuthPhase, RouteDecision]> = [
    ['loading', BOOT],
    // ★ 已初始化后这个入口就不该再可达
    ['ready', TO_DEFAULT],
    // 已有账号 → 该去登录，不是重新初始化
    ['login', TO_LOGIN],
    ['offline', TO_LOGIN],
    // 只有未初始化时才渲染初始化页
    ['setup', RENDER],
  ]

  for (const [phase, expected] of table) {
    it(`${phase} → ${expected.kind}${'to' in expected ? ` ${expected.to}` : ''}`, () => {
      expect(decideSetupRoute(phase)).toEqual(expected)
    })
  }

  it('★ 覆盖了全部 5 个 phase', () => {
    expect(table.map(([p]) => p).sort()).toEqual([...ALL_PHASES].sort())
  })

  it('★ 只有 setup 放行 —— 初始化页在其它任何 phase 下都不该可达', () => {
    const allowed = ALL_PHASES.filter((p) => decideSetupRoute(p).kind === 'render')
    expect(allowed).toEqual(['setup'])
  })
})

describe('三个守卫之间的关系（不是单个守卫能看出来的约束）', () => {
  it('★ loading 时三个入口都给兜底 —— 不能有任何一处直接渲染', () => {
    for (const decide of [decideRequireAuth, decideLoginRoute, decideSetupRoute]) {
      expect(decide('loading')).toEqual(BOOT)
    }
  })

  it('★ 每个 phase 都至少被一个入口"接住"（不会三个都跳走形成死循环）', () => {
    // ⚠️ 判据是"**不是所有入口都 redirect**"，而不是"至少一个 render"。
    //
    //    第一版我写的是 `some(d => d(phase).kind === 'render')`，它**误报**了
    //    `loading`：那一格三个入口都返回 `boot`（全屏兜底、停在原地等），
    //    没有任何一个 render —— 但那**不是**死循环，`boot` 不是跳转。
    //
    //    真正的死循环形状是"所有入口都 redirect"：用户被三方踢来踢去，
    //    永远落不到一个会渲染的页面。所以判据要针对 redirect 写。
    const deciders = [decideRequireAuth, decideLoginRoute, decideSetupRoute]
    for (const phase of ALL_PHASES) {
      const allRedirect = deciders.every((d) => d(phase).kind === 'redirect')
      expect(allRedirect, `phase=${phase} 在三个入口都只跳转，会形成重定向死循环`).toBe(false)
    }
  })

  it('★ loading 时三个入口都给兜底（不渲染、也不跳转）', () => {
    // 这条钉住上面那个 `loading` 的特殊性：它是唯一"三处都 boot"的 phase。
    // 若哪天有人把某个入口的 loading 分支改成 redirect，这里会红。
    for (const decide of [decideRequireAuth, decideLoginRoute, decideSetupRoute]) {
      expect(decide('loading')).toEqual(BOOT)
    }
  })

  it('★ ready 时三个入口的落点互不冲突（受保护区放行、另两个跳去默认页）', () => {
    expect(decideRequireAuth('ready')).toEqual(RENDER)
    expect(decideLoginRoute('ready')).toEqual(TO_DEFAULT)
    expect(decideSetupRoute('ready')).toEqual(TO_DEFAULT)
  })
})
