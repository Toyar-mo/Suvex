import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it } from 'vitest'

import { ScanList } from './ScanList'
import type { Scan } from '@/data/scan'

/*
  ══════════════════════════════════════════════════════════════
  扫描列表里一行怎么称呼自己（v0.44）
  ══════════════════════════════════════════════════════════════

  ## 为什么要单独一个渲染测试

  `scanDisplay()` 的单测（`data/scan.test.ts`）只能证明**算对了**。
  它证明不了那几个字有没有真的落到界面上 —— 而这次改动的四个点
  里有三个是"换一个变量"（`scan.name` → `display.name`），
  少换一处**不会让任何测试变红**，也不会抛异常，只会让某一格
  继续显示旧词。所以这里逐格断言用户能读到的字。

  ## 钉住的两侧

  · IR 行：不出现「TCP 服务」「[CTF2]」「夺旗」「题解」；
  · 普通行：**逐字节不变** —— 这条和上面同等重要。改展示层的
    风险从来不是"新东西没生效"，而是"顺手改了别处的字"。

  ## ⚠️ 三个必须自己处理的坑（与 ChallengeCard.test.tsx 记录的一致）

  1. **必须手动 `cleanup`** —— 本项目没开 vitest `globals`。
  2. **不能用 `toHaveTextContent`** —— 那是 `jest-dom`，本项目没装。
  3. `Link` 需要 Router 上下文 → 包 `MemoryRouter`。

  另外：**不断言 class 名**。那是易碎断言，而它不承载语义。
*/

afterEach(cleanup)

function makeScan(over: Partial<Scan> = {}): Scan {
  return {
    id: 'scan_1',
    name: '[CTF2] 一次入侵复盘',
    target: 'ctf2:local-abc',
    targetKind: 'service',
    status: 'completed',
    skills: [],
    providerId: 'prov_1',
    startedAt: '2026-10-21T00:00:00Z',
    finishedAt: '2026-10-21T00:05:00Z',
    durationSec: 300,
    agentCount: 1,
    findings: {},
    failureReason: null,
    progress: null,
    runId: null,
    brief: null,
    goal: 'captureFlag',
    flag: null,
    ...over,
  } as Scan
}

/**
 * 渲染一条记录，返回整行文本（`textContent` 一次读干，避免多元素匹配）。
 *
 * ★ `openMenu` 默认为真 —— 「题解」那一项在折叠菜单里（`menuOpen`
 *   才渲染），不点开它的断言是**空过**：既看不见「有」也看不见「没有」，
 *   两种结论都"通过"。这正是本次改动最需要防的那类假绿。
 */
function renderOne(
  scan: Scan,
  props: Partial<Parameters<typeof ScanList>[0]> = {},
  opts: { openMenu?: boolean } = {},
): string {
  const { openMenu = true } = opts
  const { container } = render(
    <MemoryRouter>
      <ScanList
        scans={[scan]}
        selectedIds={new Set<string>()}
        onToggleRow={() => {}}
        onToggleAll={() => {}}
        onAbort={() => {}}
        onRerun={() => {}}
        /* `emptyState` 是**必填** prop（空态由调用方决定，组件不自己写
           一句通用的空话）。本文件每条用例都渲染了至少一行，空态不会
           出现 —— 传个占位是为了满足契约，而不是在测它。 */
        emptyState={<p>没有扫描</p>}
        {...props}
      />
    </MemoryRouter>,
  )
  if (openMenu) {
    const trigger = screen.queryByLabelText(`${scan.target} 的更多操作`)
    if (trigger) fireEvent.click(trigger)
  }
  return container.textContent ?? ''
}

describe('ScanList · IR 行不冒充 CTF', () => {
  const ir = makeScan({ ctf2Category: 'ir-root-cause' })

  it('不出现「TCP 服务」', () => {
    expect(renderOne(ir)).not.toContain('TCP 服务')
  })

  it('不出现「[CTF2]」前缀', () => {
    expect(renderOne(ir)).not.toContain('[CTF2]')
  })

  it('显示「应急响应复盘」和「[IR] …」', () => {
    const text = renderOne(ir)
    expect(text).toContain('应急响应复盘')
    expect(text).toContain('[IR] 一次入侵复盘')
  })

  it('完成但零条目时不说「无漏洞条目」/「夺旗」', () => {
    // 那一条的措辞原本按 `goal === 'captureFlag'` 分叉，
    // 而 IR 的 goal 也是 captureFlag —— 不改就会说"夺旗任务"。
    const text = renderOne(ir)
    expect(text).not.toContain('无漏洞条目')
    expect(text).toContain('结论见复盘报告')
  })

  it('★ 不出现「题解」入口', () => {
    // 扫描侧的 writeup 端点对**一切** ctf2 壳都回 409
    // （`scans/router.py` 里那段 `SCAN_ORIGIN_CTF2` 守卫），
    // 所以这个入口点了必然失败。
    expect(renderOne(ir, { onWriteup: () => {} })).not.toContain('题解')
  })

  it('ir-timeline 也走 IR 那套（按前缀）', () => {
    const text = renderOne(makeScan({ ctf2Category: 'ir-timeline' }))
    expect(text).toContain('应急响应复盘')
    expect(text).not.toContain('TCP 服务')
  })
})

describe('ScanList · 原有显示逐字节不变', () => {
  it('普通 CTF2 题仍显示 [CTF2] + TCP 服务 + 题解入口', () => {
    // 这一侧与 IR 共享 `origin` / `goal` / 名字前缀，只有分类不同。
    // 特判若挂错了字段，最先坏的就是这里。
    const text = renderOne(makeScan({ ctf2Category: 'web' }), { onWriteup: () => {} })

    expect(text).toContain('[CTF2] 一次入侵复盘')
    expect(text).toContain('TCP 服务')
    expect(text).toContain('无漏洞条目')
    expect(text).toContain('题解')
    expect(text).not.toContain('[IR]')
  })

  it('普通夺旗任务的名字原样显示', () => {
    const text = renderOne(makeScan({ name: '拿 flag 那次', target: 'example.com' }))
    expect(text).toContain('拿 flag 那次')
    expect(text).not.toContain('[IR]')
  })

  it.each([
    ['url', '已部署的应用'],
    ['repo', 'Git 仓库'],
    ['path', '本地代码路径'],
    ['artifact', '题目附件'],
  ] as Array<[Scan['targetKind'], string]>)('%s 仍是「%s」', (kind, label) => {
    const text = renderOne(
      makeScan({ targetKind: kind, name: 'x', target: 'x', goal: 'findVulns' }),
    )
    expect(text).toContain(label)
  })

  it('例行评估零条目仍是「未发现漏洞」', () => {
    const text = renderOne(
      makeScan({ goal: 'findVulns', name: 'a.com', target: 'a.com', ctf2Category: null }),
    )
    expect(text).toContain('未发现漏洞')
  })

  it('复测零条目仍是「无漏洞条目」', () => {
    const text = renderOne(
      makeScan({ goal: 'verifyFix', name: 'a.com', target: 'a.com', ctf2Category: null }),
    )
    expect(text).toContain('无漏洞条目')
  })
})

describe('ScanList · 缺分类时降级', () => {
  it('★ ctf2Category 为 null 时按普通记录显示（不猜 IR）', () => {
    const text = renderOne(makeScan({ ctf2Category: null }), { onWriteup: () => {} })

    expect(text).toContain('[CTF2] 一次入侵复盘')
    expect(text).toContain('TCP 服务')
    expect(text).not.toContain('应急响应复盘')
  })
})
