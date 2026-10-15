import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  canHaveWriteup,
  loadCtf2ChallengeUsage,
  loadCtf2Usage,
  loadCtf2Writeup,
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
