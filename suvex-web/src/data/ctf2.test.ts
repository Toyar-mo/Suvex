import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  canHaveWorkOrder,
  canHaveWriteup,
  loadCtf2ChallengeUsage,
  loadCtf2Usage,
  loadCtf2Writeup,
  saveCtf2Settings,
  type Ctf2Challenge,
} from './ctf2'

/*
  ══════════════════════════════════════════════════════════════
  CTF2 题解 / token 消耗 的**数据层**（v0.36）
  ══════════════════════════════════════════════════════════════

  ## 为什么测 URL 与筛选参数，而不是"函数能不能跑"

  这一层薄得几乎没有逻辑（把参数拼成 URL、把响应当类型返回），所以
  "跑起来不报错"这个断言毫无价值 —— 真正会错的是**拼出来的 URL**：

    · 章 id 里可能有 `/` 或空格 → 不 `encodeURIComponent` 就会打到
      一个不存在的路径，而症状是 404，看起来像"这道题不存在"；
    · `/ctf2/usage` 的两个筛选参数如果传了**空串**，`buildUrl` 会把它
      拼成 `?challengeId=`，后端把它当筛选条件 → **静默返回空集**，
      界面上显示"总共就花了 0 token"。

  这两个都不会抛异常，只会让界面说错话。所以这里逐字断言 URL。

  ## 怎么隔离

  替换全局 `fetch`，把请求的 URL 记下来，回一个契约形状的 JSON。
  与 `scans-store.test.ts` 同一套做法（它们都通过 `lib/api` 走 fetch）。
*/

/** 记录发出的完整 URL */
let requested: string[] = []

function installFetchStub(payload: unknown = {}) {
  requested = []
  vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
    requested.push(typeof input === 'string' ? input : input.toString())
    return Promise.resolve(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('loadCtf2Writeup', () => {
  it('★ 打到题目卡片那条路径，而不是扫描的题解端点', async () => {
    installFetchStub({ markdown: '# x' })

    await loadCtf2Writeup('chal_1')

    expect(requested).toHaveLength(1)
    // 这是本次改动的**核心**：CTF2 的题解不在 /scans/{id}/writeup
    // （那条永远找不到 strix 报告，见契约 §3.19.5）。
    expect(requested[0]).toContain('/api/v1/ctf2/challenges/chal_1/writeup')
    expect(requested[0]).not.toContain('/scans/')
  })

  it('★ challengeId 被转义 —— 否则带斜杠的 id 会打到别的路径', async () => {
    installFetchStub({ markdown: '# x' })

    await loadCtf2Writeup('a/b c')

    expect(requested[0]).toContain('/ctf2/challenges/a%2Fb%20c/writeup')
  })
})

describe('loadCtf2Usage', () => {
  it('★ 两个筛选都不给时，URL 里**不出现**这两个参数', async () => {
    installFetchStub({ calls: 0 })

    await loadCtf2Usage()

    expect(requested[0]).toContain('/api/v1/ctf2/usage')
    /*
      这一条是"静默返回空集"的防线：`?challengeId=`（空串）会被后端
      当成一个真的筛选条件，于是总数变成 0 —— 而界面会把它显示成
      "没有消耗记录"，与"没跑过"混淆。
    */
    expect(requested[0]).not.toContain('challengeId')
    expect(requested[0]).not.toContain('competitionId')
  })

  it('给了筛选就带上（两个维度分别可单用）', async () => {
    installFetchStub({ calls: 1 })

    await loadCtf2Usage({ challengeId: 'chal_1' })
    await loadCtf2Usage({ competitionId: 'comp_9' })

    expect(requested[0]).toContain('challengeId=chal_1')
    expect(requested[0]).not.toContain('competitionId')
    expect(requested[1]).toContain('competitionId=comp_9')
  })

  it('★ 显式传 null 等同于"不筛"，不会被拼成字符串 "null"', async () => {
    installFetchStub({ calls: 0 })

    await loadCtf2Usage({ challengeId: null, competitionId: null })

    expect(requested[0]).not.toContain('null')
  })
})

describe('loadCtf2ChallengeUsage', () => {
  it('打到单题明细端点，且转义 id', async () => {
    installFetchStub([])

    await loadCtf2ChallengeUsage('chal 2')

    expect(requested[0]).toContain('/api/v1/ctf2/challenges/chal%202/usage')
  })
})

/*
  ─────────────────────────────────────────────────────────────
  `canHaveWriteup` —— 决定「题解」入口露不露出
  ─────────────────────────────────────────────────────────────

  ## 判据是 `scanId`，**不是** `status`

  第一版这里按状态白名单判断，被 2026-10-15 的真实数据打出一个真 bug：

    `[ACTF2020 新生赛]Upload` 跑满 78 轮、51 分钟、**未解出**，
    后端收尾把它回到 `unsolved` —— 而它**有题解**（`solved=false`
    照样产出）。按状态判断会把入口藏掉，用户永远看不到那份题解。

  所以判据换成 `scanId` 非空（= 至少开始过一次解题，与后端的
  `has_solve_attempt` 表达同一件事）。下面每一条都对应一个真实状态。
*/
describe('canHaveWriteup', () => {
  const challenge = (over: Partial<Ctf2Challenge>): Ctf2Challenge =>
    ({ id: 'chal_1', title: 't', status: 'unsolved', scanId: null, ...over }) as Ctf2Challenge

  it('★★ 真实回归：未解出但跑过一轮的题**必须**有入口', () => {
    /*
      这是上面那个真 bug 的钉子。真实样本的字段原样抄下来：
      status=unsolved、scanId 非空、有题解（solved=false）。
      如果判据再回到按状态判断，这一条会立刻红。
    */
    const real = challenge({
      status: 'unsolved',
      scanId: 'scan_01M4RD51TJSMXS4ZQF97QK636N',
      candidateFlag: null,
      confirmedFlag: null,
      lastError: '本次未产出候选 flag。',
    })

    expect(canHaveWriteup(real)).toBe(true)
  })

  it('★ 跑过一轮的各状态都给入口（solved / failed / candidate_ready）', () => {
    for (const status of ['solved', 'failed', 'candidate_ready'] as const) {
      expect(canHaveWriteup(challenge({ status, scanId: 'scan_1' })), status).toBe(true)
    }
  })

  it('★ 正在跑的题也给入口 —— 后端那句话现在是准确的', () => {
    /*
      `environment_ready` 是解题过程的主状态（不是瞬间中间态）。
      它 `scanId` 已非空，所以入口显示 —— 而后端对新判据下这类题
      回的是"这次没有生成题解，重新解一次会生成"，不是假话。
    */
    for (const status of ['solving', 'environment_ready', 'submitting'] as const) {
      expect(canHaveWriteup(challenge({ status, scanId: 'scan_1' })), status).toBe(true)
    }
  })

  it('★★ 从没开始过的题**不给**入口（点了必然 409）', () => {
    // 判据是 scanId 为空，而不是状态 —— 未解出的题两种都有
    expect(canHaveWriteup(challenge({ status: 'unsolved', scanId: null }))).toBe(false)
  })
})

/*
  ─────────────────────────────────────────────────────────────
  `canHaveWorkOrder` —— 决定「处置工单」入口露不露出
  ─────────────────────────────────────────────────────────────

  ## ★ 判据是**分类前缀**，与 `canHaveWriteup` 的 `scanId` 刚好相反

  题解对**每道跑过的题**都有（任何分类都可能产出）；工单只有
  **应急响应任务**才有 —— 普通 CTF 题的提示词里根本没让模型写
  `workorders`。

  拿 `scanId` 来判工单会怎样：每一道跑过的 pwn 题都露出一个点开就报 409
  的按钮。"入口本身就是一句承诺"被违反的样子就是这样。

  ## 为什么用前缀而不是三个硬编码分类

  前缀判定与后端 `notes.is_ir_category` 逐字对应。写成
  `category === 'ir-root-cause' || ...` 会让两处各维护一份清单，
  而新增第四个 `ir-*` 分类时必然漏改前端这一份 —— 症状是
  "后端能跑、界面上没入口"，没有任何报错。
*/
describe('canHaveWorkOrder', () => {
  const challenge = (over: Partial<Ctf2Challenge>): Ctf2Challenge =>
    ({ id: 'chal_1', title: 't', status: 'unsolved', category: 'pwn', scanId: null, ...over }) as Ctf2Challenge

  it('★★ 三个 `ir-*` 分类都给入口', () => {
    for (const category of ['ir-root-cause', 'ir-timeline', 'ir-scope']) {
      expect(canHaveWorkOrder(challenge({ category })), category).toBe(true)
    }
  })

  it('★★ 跑过的普通题**不给**入口（即使 scanId 非空）', () => {
    /*
      这是与 `canHaveWriteup` 的分歧点。同一道题：
        · canHaveWriteup = true（跑过就有题解）
        · canHaveWorkOrder = false（不是应急响应任务，没有工单）
    */
    const pwn = challenge({
      category: 'pwn',
      scanId: 'scan_01M4RD51TJSMXS4ZQF97QK636N',
      status: 'solved',
    })
    expect(canHaveWriteup(pwn)).toBe(true)
    expect(canHaveWorkOrder(pwn)).toBe(false)
  })

  it('★ 五个平台分类 + `agentsec-*` 都不给入口', () => {
    const categories = [
      'pwn', 'web', 'crypto', 'reverse', 'misc', 'forensics',
      'agentsec-prompt-injection', 'agentsec-tool-escalation',
      'agentsec-data-leak', 'agentsec-multi-agent',
    ]
    for (const category of categories) {
      /*
        ⚠️ `reverse` 值得单独盯一眼：`ir-` 与 `re` 都有字母 r，
        历史上 `profile_for_category` 就差点因为子串匹配把 `forensics`
        推进逆向镜像（`forensics` 含子串 `re`）。
      */
      expect(canHaveWorkOrder(challenge({ category })), category).toBe(false)
    }
  })

  it('★ 大小写与前后空白按后端规则归一（`strip` + `lower`）', () => {
    /*
      后端 `is_ir_category` 做了 `strip()`，前端不跟就是两套规则。
      平台的分类名理论上不会带空白 —— 但"理论上"不是一个能写进代码的依据：
      两侧归一化不一致时，错的那一侧永远不会有报错。
    */
    expect(canHaveWorkOrder(challenge({ category: '  IR-Root-Cause  ' }))).toBe(true)
    expect(canHaveWorkOrder(challenge({ category: 'IR-scope' }))).toBe(true)
  })

  it('★ 空分类 / 未分类不给入口（不抛异常）', () => {
    expect(canHaveWorkOrder(challenge({ category: '' }))).toBe(false)
    // 后端可能给 null（平台题型未知），不能让页面崩
    expect(canHaveWorkOrder(challenge({ category: null as unknown as string }))).toBe(false)
  })

  it('★★ `ir` 裸前缀不给入口（必须是 `ir-`）', () => {
    /*
      后端路由判据是 `startswith("ir-")`，裸 `ir` 不以它开头。
      前端若只用 `startsWith('ir')`，`ir` 与 `irx` 都会误判成应急响应题 ——
      而这两者后端根本不认，点进去必然 409。
    */
    expect(canHaveWorkOrder(challenge({ category: 'ir' }))).toBe(false)
    expect(canHaveWorkOrder(challenge({ category: 'irx-foo' }))).toBe(false)
  })
})

/*
  ══════════════════════════════════════════════════════════════
  `saveCtf2Settings` 的请求体 —— 两个场景包开关必须真的发出去
  ══════════════════════════════════════════════════════════════

  ## 为什么这条值得单独立一组

  `PUT /ctf2/settings` 是**全量提交**：后端 `update_ctf2_settings`
  逐字段赋值，没传的字段按后端形参默认值落库。而两个场景包开关的
  后端默认值是 `False`。

  所以"前端漏传"的后果不是 400，也不是不生效 —— 是**把用户已经打开的
  开关静默关掉**。而且它看不出来：设置页读回来的值是刚被写回去的
  `false`，界面显示"关着"，就像本来就是关的。

  这个缺口在 v0.40 真的存在过：后端加完开关、API 能用，而前端
  `Ctf2Settings` 与 `saveCtf2Settings` 都没有这两个字段。当时任何人
  在设置页点一次保存（改任何别的项都会触发），开关就会被关掉。

  ## 断言的是"发出去的 JSON 里有这个字段且值对"

  不是"函数返回了不报错"。前者是契约，后者什么都保证不了。
*/

/** 记录 PUT 的请求体（与上面的 `requested` 分开，避免互相干扰） */
let putBodies: unknown[] = []

function installBodyCapturingFetch(payload: unknown = {}) {
  requested = []
  putBodies = []
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
    requested.push(typeof input === 'string' ? input : input.toString())
    if (init?.body) {
      putBodies.push(JSON.parse(String(init.body)))
    }
    return Promise.resolve(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })
}

describe('saveCtf2Settings 的场景包开关', () => {
  it('★★ 两个开关都在 PUT 请求体里，且值原样带过去', async () => {
    installBodyCapturingFetch({ submitMode: 'safe' })

    await saveCtf2Settings({
      submitMode: 'safe',
      maxWrongSubmissions: 3,
      defaultSandboxProfile: 'ctf2-sandbox',
      maxConcurrentSolves: 2,
      solvePollSeconds: 1,
      autoSyncSeconds: 60,
      autoPriorityNew: true,
      autoStartNew: false,
      maxTurns: 40,
      fallbackProviderId: null,
      enableAgentsecScenarios: true,
      enableIrScenarios: true,
    })

    expect(putBodies).toHaveLength(1)
    const body = putBodies[0] as Record<string, unknown>
    // ⚠️ 用 toBe(true) 而不是 toBeTruthy：后者对 `undefined` 也过 —— 而
    //    "字段根本不存在"正是这条要防的那个 bug，它必须红。
    expect(body.enableAgentsecScenarios).toBe(true)
    expect(body.enableIrScenarios).toBe(true)
  })

  it('★ 关掉时发的是 `false`，不是"不传"', async () => {
    /*
      这是全量提交语境下最容易写错的一处：`false` 与"不传"在后端
      落库结果相同（都变 false），所以两种写法看起来都"对"。
      但只有显式发 `false` 才表达得出"我确认要关掉"这个意图 ——
      而"不传"是歧义的：它既可能表示"我要关"，也可能是"我忘了"。
      前端这一侧不允许有歧义。
    */
    installBodyCapturingFetch({ submitMode: 'safe' })

    await saveCtf2Settings({
      submitMode: 'safe',
      maxWrongSubmissions: 3,
      defaultSandboxProfile: 'ctf2-sandbox',
      maxConcurrentSolves: 2,
      solvePollSeconds: 1,
      autoSyncSeconds: 60,
      autoPriorityNew: true,
      autoStartNew: false,
      maxTurns: 40,
      fallbackProviderId: null,
      enableAgentsecScenarios: false,
      enableIrScenarios: false,
    })

    const body = putBodies[0] as Record<string, unknown>
    expect('enableAgentsecScenarios' in body).toBe(true)
    expect('enableIrScenarios' in body).toBe(true)
    expect(body.enableAgentsecScenarios).toBe(false)
    expect(body.enableIrScenarios).toBe(false)
  })

  it('★★ 两个开关**各自独立**，不会互相带翻', async () => {
    /*
      后端把两个开关分开的理由是"共用会让一族的分类在另一族开着时
      通过校验"。前端这一侧的同一条风险是：绑定到同一个 state、
      或者 onChange 里写错字段名，症状就是勾一个另一个跟着变 ——
      而两边都是"打开了"，界面上看起来完全正常。

      ⚠️ **两种不对称组合都要试**。只试 `agentsec=true / ir=false`
      抓不到"把 `enableIrScenarios` 写死成 `false`"这种错 ——
      它恰好与那个组合一致。反过来的那一组才是它的钉子。
    */
    for (const [agentsec, ir] of [
      [true, false],
      [false, true],
    ] as const) {
      installBodyCapturingFetch({ submitMode: 'safe' })

      await saveCtf2Settings({
        submitMode: 'safe',
        maxWrongSubmissions: 3,
        defaultSandboxProfile: 'ctf2-sandbox',
        maxConcurrentSolves: 2,
        solvePollSeconds: 1,
        autoSyncSeconds: 60,
        autoPriorityNew: true,
        autoStartNew: false,
        maxTurns: 40,
        fallbackProviderId: null,
        enableAgentsecScenarios: agentsec,
        enableIrScenarios: ir,
      })

      const body = putBodies[0] as Record<string, unknown>
      expect(body.enableAgentsecScenarios, `agentsec=${agentsec} ir=${ir}`).toBe(agentsec)
      expect(body.enableIrScenarios, `agentsec=${agentsec} ir=${ir}`).toBe(ir)
    }
  })
})
