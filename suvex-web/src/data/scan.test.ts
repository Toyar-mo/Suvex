import { describe, expect, it } from 'vitest'

import { scanDisplay, type Scan } from './scan'

/*
  ══════════════════════════════════════════════════════════════
  scanDisplay：一行扫描记录该怎么称呼自己（v0.44）
  ══════════════════════════════════════════════════════════════

  ## 这个文件守什么

  一次应急响应复盘在库里是一条**ctf2 借的壳**（`ctf2/events.py`
  的 `ensure_solve_scan`），于是它带着两样从 CTF 场景继承来的东西：

    · `targetKind = 'service'` → 列表里显示「TCP 服务」
    · `goal = 'captureFlag'`   → 零条目时的措辞在说"夺旗"
    · 名字 `[CTF2] <标题>`

  这三样同时出现在一次**只读过日志、没对任何地址发包**的任务上，
  不是措辞不精致，是整行都在描述一件没发生过的事。

  ## 为什么测这个函数而不是测 ScanList

  同一行数据在**三处**渲染（列表、写操作回执、详情），判据收在
  `scanDisplay()` 里。测这个纯函数就能覆盖三处，而渲染测要写三份。
  渲染层另有 `ScanList.test.tsx` 守"词有没有真落到界面上"。

  ## 最要紧的一条：不许说错话

  拿不到 `ctf2Category` 时（老数据、题目被删、`scanId` 为 NULL）
  **必须降级成普通记录**，不能猜成 IR —— 把一次普通 CTF 显示成
  「应急响应复盘」比显示成「TCP 服务」严重得多：前者是编造。
*/

/** 造一条最小可用的扫描记录。只填被测函数会读的字段。 */
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

describe('scanDisplay · IR 行', () => {
  it('ir-root-cause 显示成「[IR] 标题」+「应急响应复盘」', () => {
    const d = scanDisplay(makeScan({ ctf2Category: 'ir-root-cause' }))

    expect(d.isIr).toBe(true)
    expect(d.name).toBe('[IR] 一次入侵复盘')
    expect(d.kindLabel).toBe('应急响应复盘')
  })

  it.each(['ir-timeline', 'ir-scope'])('%s 也是 IR（按前缀判，不按等值）', (cat) => {
    expect(scanDisplay(makeScan({ ctf2Category: cat })).isIr).toBe(true)
  })

  it('判前缀不判等 —— 将来加新分类不用改这里', () => {
    // 一个**还不存在**的分类。它必须已经被认成 IR，
    // 否则每加一个分类都要回来改一次，而漏改的表现是
    // 新分类静默退回「TCP 服务」（看起来完全正常）。
    expect(scanDisplay(makeScan({ ctf2Category: 'ir-something-new' })).isIr).toBe(true)
  })

  it('裸 ir（不带连字符）不算 IR', () => {
    // `ir` 本身不是分类值，`IR_CATEGORY_PREFIX` 是 `ir-`。
    // 判 `startsWith('ir')` 会把 `irx-` 也吃进来。
    expect(scanDisplay(makeScan({ ctf2Category: 'ir' })).isIr).toBe(false)
  })

  it('irx- 不算 IR', () => {
    expect(scanDisplay(makeScan({ ctf2Category: 'irx-foo' })).isIr).toBe(false)
  })

  it('空分类按普通记录处理', () => {
    expect(scanDisplay(makeScan({ ctf2Category: '' })).isIr).toBe(false)
  })

  it('大写不算 IR —— 与后端 is_ir_category 同样区分大小写', () => {
    // 后端 `ctf2/notes.py::is_ir_category` 就是 `startswith("ir-")`，
    // 区分大小写。这里跟着它走：两边对同一个输入给不同结论，
    // 会出现"列表说是复盘、后端按 CTF 处理"这种最难查的分歧。
    // 分类值来自后端枚举（全小写），大写只可能是脏数据。
    expect(scanDisplay(makeScan({ ctf2Category: 'IR-root-cause' })).isIr).toBe(false)
  })
})

describe('scanDisplay · 不说错话', () => {
  it('★ 拿不到分类时降级成普通记录，不猜 IR', () => {
    // `null` = 后端查不到（题目被删 / scanId 为 NULL / 老数据）。
    // 这是本文件最要紧的一条：宁可难看，不可编造。
    const d = scanDisplay(makeScan({ ctf2Category: null }))
    expect(d.isIr).toBe(false)
    expect(d.name).toBe('[CTF2] 一次入侵复盘')
    expect(d.kindLabel).toBe('TCP 服务')
  })

  it('字段整个缺失也降级（后端旧版本 / 缓存里的老对象）', () => {
    expect(scanDisplay(makeScan()).isIr).toBe(false)
  })

  it('★ IR 行的目标形态不能是「TCP 服务」', () => {
    // 那条任务根本没对任何地址发包，说它是 TCP 服务是编造。
    expect(scanDisplay(makeScan({ ctf2Category: 'ir-root-cause' })).kindLabel).not.toBe(
      'TCP 服务',
    )
  })

  it('IR 行去掉 [CTF2] 前缀 —— 复盘与 CTF2 平台无关', () => {
    expect(scanDisplay(makeScan({ ctf2Category: 'ir-root-cause' })).name).not.toContain(
      '[CTF2]',
    )
  })
})

describe('scanDisplay · 原有五类逐字节不变', () => {
  const cases: Array<[Scan['targetKind'], string]> = [
    ['url', '已部署的应用'],
    ['repo', 'Git 仓库'],
    ['path', '本地代码路径'],
    ['service', 'TCP 服务'],
    ['artifact', '题目附件'],
  ]

  it.each(cases)('%s 的形态名不变', (kind, label) => {
    const scan = makeScan({
      targetKind: kind,
      name: 'example.com',
      target: 'example.com',
      goal: 'findVulns',
    })
    const d = scanDisplay(scan)

    expect(d.kindLabel).toBe(label)
    expect(d.isIr).toBe(false)
    // ★ 名字必须**原样**返回，不加不加前缀
    expect(d.name).toBe('example.com')
  })

  it('★ 普通 CTF2 题（非 ir-*）也逐字节不变', () => {
    // 这是最容易误伤的一侧：它们同样 `origin === 'ctf2'`、
    // 同样 `goal === 'captureFlag'`、同样带 `[CTF2] ` 前缀。
    // 只有分类能区分它们，所以特判必须挂在分类上。
    const scan = makeScan({ ctf2Category: 'web' })
    const d = scanDisplay(scan)

    expect(d.isIr).toBe(false)
    expect(d.name).toBe('[CTF2] 一次入侵复盘')
    expect(d.kindLabel).toBe('TCP 服务')
  })

  it.each(['pwn', 'reverse', 'crypto', 'misc', 'forensics', 'agentsec-prompt-injection'])(
    '%s 分类都不被当成 IR',
    (cat) => {
      expect(scanDisplay(makeScan({ ctf2Category: cat })).isIr).toBe(false)
    },
  )
})
