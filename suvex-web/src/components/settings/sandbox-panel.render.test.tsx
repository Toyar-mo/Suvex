/**
 * 设置页文案**渲染**验证。
 *
 * ## 与 `settings-claims.test.ts` 的分工
 *
 * 那个文件断言**源码文本**（"这句话里不许出现什么"），守住的是"有人改回旧说法"。
 * 但它证明不了那句话**真的渲染出去**——文案可能被条件分支挡住、被 props 覆盖、
 * 或者组件根本没挂上。
 *
 * 这个文件渲染真组件、查 DOM 文本，回答的是另一半：
 * **用户实际看到的那句话，是改对的那句。**
 *
 * 两半都必要：只测源码会漏"文案没显示"，只测 DOM 会漏"另一个分支还是错的"。
 *
 * ⚠️ `vitest.config.ts` 关掉了 `globals`，自动 cleanup 不注册，手动 `cleanup()`。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { SandboxPanel } from './SandboxPanel'
import type { SandboxConfig } from '@/data/settings'

afterEach(cleanup)

const BASE: SandboxConfig = {
  image: 'suvex-sandbox:latest',
  status: 'stopped',
  cpuLimit: 2,
  memoryLimitMb: 2048,
  timeoutMinutes: 60,
  networkIsolated: false,
  allowVolumeWrite: false,
  maxConcurrentScans: 2,
  scanMaxTurns: 80,
  scanMode: 'standard',
  enableOffensivePrompt: false,
  executionSource: {
    maxConcurrentScans: 'user',
    scanMaxTurns: 'user',
    scanMode: 'user',
  },
} as SandboxConfig

describe('SandboxPanel 渲染出的隔离文案', () => {
  it('默认状态下：禁止出网标注了只对本地目标生效', () => {
    render(<SandboxPanel config={BASE} />)
    // 区间选择：文案里有"只对本地"，且不是"将无法执行"
    expect(screen.getAllByText(/只对本地/).length).toBeGreaterThan(0)
    expect(screen.queryByText(/将无法执行/)).toBeNull()
  })

  it('打开禁止出网后：警告块说管不住网络目标，而不是说扫描会失败', () => {
    render(<SandboxPanel config={{ ...BASE, networkIsolated: true }} />)
    // 警告块只在开关打开时渲染 —— 这正是"另一个分支"要被覆盖的原因。
    // 它不重复 hint 里的"强制开网"，而是补一句"边界在宿主机 iptables"：
    // 重复会把同一件事说两遍，而用户需要的是"那我靠什么保证安全"。
    expect(screen.getByText(/管不住网络目标/)).toBeTruthy()
    expect(screen.getByText(/DOCKER-USER/)).toBeTruthy()
    expect(screen.queryByText(/扫描会直接失败/)).toBeNull()
    expect(screen.queryByText(/将无法执行/)).toBeNull()
  })

  it('★ 超时与挂载卷**不再**标注「仅自研引擎生效」（后端限定语已过时）', () => {
    // 反向断言，理由见 settings-claims.test.ts:76 那段：2026-10-26 用户明确
    // 「现在没有 strix 相关的东西了，只有一个 Suvex 内核」。此时再说
    // 「仅自研引擎生效（strix 后端不读）」**不排除任何东西** —— 它只会让
    // 用户以为还存在另一个后端、并在心里给它配一份预期。
    //
    // 这条断言原来写的是 `getAllByText(/仅自研引擎生效/).length >= 2`
    // （要求那两句必须在），是**与产品事实相反**的守卫：它保护的是
    // 一句过时的话。改成反向之后，谁再把限定语加回来，是它先红。
    render(<SandboxPanel config={BASE} />)
    expect(screen.queryAllByText(/仅自研引擎生效/).length).toBe(0)
    expect(screen.queryByText(/strix/i)).toBeNull()
  })
})

describe('SandboxPanel 仍然如实渲染真生效的项', () => {
  it('执行组仍写明真正生效（不能因为修文案把真话删掉）', () => {
    render(<SandboxPanel config={BASE} />)
    expect(screen.getByText(/真的生效/)).toBeTruthy()
  })
})
