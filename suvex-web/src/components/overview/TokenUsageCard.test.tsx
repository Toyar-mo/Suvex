import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import TokenUsageCard from './TokenUsageCard'
import type { Ctf2TokenUsage } from '@/data/ctf2'

/*
  ══════════════════════════════════════════════════════════════
  总览页 token 消耗卡片（v0.36）
  ══════════════════════════════════════════════════════════════

  ## 这个文件钉住的是什么

  不是"卡片能渲染"，而是**三种"空"必须说成三句不同的话**。这是整个
  token 功能里最容易被做错、也最不会被测试发现的地方 —— 把三者混成
  "消耗 0"，界面不会报错、构建不会失败，但用户会得到错误结论：

    | 情况 | 用户会误以为 |
    | --- | --- |
    | 一条记录都没有（`since === null`） | "确实没跑过" ✅ |
    | 跑了但接入点没给用量（`recordedCalls === 0`） | "消耗为 0" ❌ 其实只是没记 |
    | 部分没给（`recordedCalls < calls`） | "这就是全部消耗" ❌ 其实只是下限 |

  所以下面按这三种情况分别断言**渲染出来的那句话**，而不是内部变量 ——
  用户看到的只有文案，"算对了但渲染时用错了"必须被抓住。

  ## ⚠️ 三个坑（与 ErrorState.test.tsx / ChallengeCard.test.tsx 记录一致）

  1. 必须手动 `cleanup`（本项目没开 vitest `globals`）；
  2. 不能用 `toHaveTextContent`（没装 jest-dom）；
  3. 组件里有 `Link`，需要 Router 上下文。

  ## 细分是**第二次请求**，所以 fetch 要按 URL 分流

  组件在 `calls > 0` 时会额外打 `GET /ctf2/usage` 取按题细分。这里用
  一个按 URL 分流的 stub：让明细那条**可控地失败**，用来验"细分失败
  不影响总消耗"这条降级。
*/
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const EMPTY_USAGE: Ctf2TokenUsage = {
  calls: 0,
  recordedCalls: 0,
  promptTokens: 0,
  completionTokens: 0,
  totalTokens: 0,
  byModel: [],
  byChallenge: [],
  since: null,
}

/**
 * 装一个 fetch stub。
 *
 * 按 URL **分流**（v0.37 起卡片会打两个端点）：
 *   · `/ctf2/usage`            → 细分（`byModel` + `byChallenge`）
 *   · `/ctf2/challenges/{id}/usage` → 逐轮明细（点开某道题时才发）
 *
 * `detail`：
 *   · `'ok'`    细分返回一组真实形状的数据；
 *   · `'fail'`  细分返回 500（验降级）；
 *   · `'never'` 细分永远挂着 —— **不该发生**，因为 `calls === 0` 时
 *               组件根本不发这个请求，那条断言就靠它。
 *
 * `turns`：
 *   · `'ok'`     逐轮返回 3 行（含一行 `usageReported: false`）；
 *   · `'fail'`   逐轮 500；
 *   · `'never'`  逐轮永远挂着（验"还在读"）。
 */
function installFetchStub(
  detail: 'ok' | 'fail' | 'never',
  turns: 'ok' | 'fail' | 'never' = 'ok',
) {
  vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString()

    // 逐轮明细：路径更长，必须先判它（否则会被 /ctf2/usage 的 includes 吃掉）
    if (url.includes('/challenges/')) {
      if (turns === 'never') return new Promise<Response>(() => {})
      if (turns === 'fail') {
        return Promise.resolve(
          new Response(JSON.stringify({ error: { code: 'INTERNAL_ERROR', message: '炸了' } }), {
            status: 500,
            headers: { 'Content-Type': 'application/json' },
          }),
        )
      }
      return Promise.resolve(
        new Response(
          JSON.stringify([
            {
              turn: 1,
              model: 'deepseek-v4.1-flash',
              providerLabel: 'gzd',
              promptTokens: 4999,
              completionTokens: 89,
              totalTokens: 5088,
              reasoningTokens: 0,
              usageReported: true,
              at: '2026-10-15T08:49:54.129Z',
            },
            {
              turn: 2,
              model: 'deepseek-v4.1-flash',
              providerLabel: 'gzd',
              promptTokens: 98048,
              completionTokens: 879,
              totalTokens: 98927,
              reasoningTokens: 0,
              usageReported: true,
              at: '2026-10-15T08:49:54.130Z',
            },
            {
              turn: 3,
              model: 'backup-model',
              providerLabel: 'other',
              promptTokens: 0,
              completionTokens: 0,
              totalTokens: 0,
              reasoningTokens: 0,
              // ★ 这一行是刻意的：接入点没给用量 -> 界面必须显示「未记录」
              usageReported: false,
              at: '2026-10-15T08:49:54.131Z',
            },
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
    }

    if (!url.includes('/ctf2/usage')) {
      return Promise.reject(new Error(`不该请求这个地址：${url}`))
    }
    if (detail === 'never') {
      return new Promise<Response>(() => {})
    }
    if (detail === 'fail') {
      return Promise.resolve(
        new Response(JSON.stringify({ error: { code: 'INTERNAL_ERROR', message: '炸了' } }), {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    }
    return Promise.resolve(
      new Response(
        JSON.stringify({
          ...EMPTY_USAGE,
          byModel: [
            { model: 'deepseek-v4.1-flash', providerLabel: 'gzd', calls: 76, totalTokens: 8900 },
            { model: 'backup-model', providerLabel: 'other', calls: 2, totalTokens: 100 },
          ],
          byChallenge: [
            { challengeId: 'c1', title: '最贵的那道题', calls: 20, totalTokens: 9000 },
          ],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
  })
}

function renderCard(props: {
  calls: number
  recordedCalls: number
  totalTokens: number
  since: string | null
}) {
  return render(
    <MemoryRouter>
      <TokenUsageCard {...props} />
    </MemoryRouter>,
  )
}

/** 卡片正文（section 的全部文字）。用它断言文案，避免受内部结构影响 */
function cardText(): string {
  return screen.getByRole('region').textContent ?? ''
}

describe('TokenUsageCard：一条记录都没有', () => {
  it('★ 说"还没有消耗记录"，而**不是**显示 0 token', () => {
    installFetchStub('never')
    renderCard({ ...EMPTY_USAGE })

    const text = cardText()
    expect(text).toContain('还没有消耗记录')
    /*
      ★ 这一条是核心：`0 token` 与"没记录"在界面上长得一样，而
      显示 0 会让用户以为"跑过、没消耗"。所以整张卡**不该出现
      任何 token 数字**。
    */
    expect(text).not.toContain('0 token')
    expect(text).not.toContain('共 0 次调用')
  })

  it('★ 这种情况下**不发**细分请求（calls === 0 是判据）', async () => {
    // stub 对任何请求都 reject —— 一旦组件发了请求，用例会因未处理的
    // rejection 而失败。这就是"不发"的断言。
    installFetchStub('never')
    renderCard({ ...EMPTY_USAGE })

    await Promise.resolve()
    expect(cardText()).toContain('还没有消耗记录')
  })

  it('since 为 null 时不显示"自…起统计"', () => {
    installFetchStub('never')
    renderCard({ ...EMPTY_USAGE })

    expect(cardText()).not.toContain('起统计')
  })
})

describe('TokenUsageCard：接入点没给用量', () => {
  it('★★ recordedCalls === 0 时必须说"没返回用量"，不能说成 0 消耗', () => {
    installFetchStub('never')
    renderCard({ calls: 12, recordedCalls: 0, totalTokens: 0, since: '2026-10-14T10:00:00Z' })

    const text = cardText()
    expect(text).toContain('接入点都没有返回 token 用量')
    // 关键：必须明确否认"消耗为 0"这个读法
    expect(text).toContain('没记录到')
    // 调用次数仍然如实显示（它永远准确）
    expect(text).toContain('共 12 次调用')
  })
})

describe('TokenUsageCard：部分没给用量', () => {
  it('★★ recordedCalls < calls 时总量必须被标成**下限**', () => {
    installFetchStub('never')
    renderCard({ calls: 318, recordedCalls: 316, totalTokens: 4821130, since: '2026-10-14T10:00:00Z' })

    const text = cardText()
    expect(text).toContain('下限')
    expect(text).toContain('2 次')
    // 数字照常显示（有 316 次的真实数据）
    expect(text).toContain('共 318 次调用')
  })

  it('★ 全部都有用量时**不**出现下限/未返回的措辞（否则会变成噪音）', () => {
    installFetchStub('never')
    renderCard({ calls: 10, recordedCalls: 10, totalTokens: 12345, since: '2026-10-14T10:00:00Z' })

    const text = cardText()
    expect(text).not.toContain('下限')
    expect(text).not.toContain('未返回用量')
    expect(text).toContain('共 10 次调用')
  })

  /*
    ══════════════════════════════════════════════════════════
    真实数字（2026-10-15，Kali 上一次 78 轮解题的实际读数）
    ══════════════════════════════════════════════════════════

    这一条验的是**格式化**而不是逻辑：真实的 `totalTokens` 是七位数
    （5,536,545），而卡片用 `formatCompact` 把它压成 `5.5M`。
    这类地方最容易出的是"缩写之后与精确值对不上"或"精确值被截断"，
    所以两个形态**都要**出现在卡上，并且精确值必须带千分位。
  */
  it('★★ 真实读数 5,536,545 的两个形态都在，且精确值带千分位', async () => {
    installFetchStub('ok')
    renderCard({
      calls: 78,
      recordedCalls: 78,
      totalTokens: 5536545,
      since: '2026-10-15T08:49:54.129Z',
    })

    const text = cardText()
    // ① 大数字的紧凑形态（formatCompact：5536545 -> 5.5M）
    expect(text).toContain('5.5M')
    // ② 精确值（带千分位，供用户据以换算成本）
    expect(text).toContain('5,536,545')
    // ③ 调用次数
    expect(text).toContain('共 78 次调用')
    // ④ 全都有用量 -> 不能出现"下限"这类措辞（否则是假警报）
    expect(text).not.toContain('下限')
    expect(text).not.toContain('未返回用量')
    // ⑤ 统计起点有值 -> 显示"自…起统计"
    expect(text).toContain('起统计')
  })

  it('★ 细分只有一条时也照常列出（真实库就是一道题）', async () => {
    installFetchStub('ok')
    renderCard({
      calls: 78,
      recordedCalls: 78,
      totalTokens: 5536545,
      since: '2026-10-15T08:49:54.129Z',
    })

    await waitFor(() => expect(cardText()).toContain('最贵的那道题'))
    // 合计那行：单条时合计 == 该条
    expect(cardText()).toContain('1 项合计')
  })
})

describe('TokenUsageCard：按题细分的降级', () => {
  it('★ 细分请求失败时，**总消耗照常显示**，只降级一句提示', async () => {
    installFetchStub('fail')
    renderCard({ calls: 5, recordedCalls: 5, totalTokens: 6789, since: '2026-10-14T10:00:00Z' })

    await waitFor(() => {
      expect(cardText()).toContain('细分数据暂时取不到')
    })
    // 总量没有被这次失败带走
    expect(cardText()).toContain('共 5 次调用')
  })

  it('拿到细分时列出题目名，并说明条长的口径', async () => {
    installFetchStub('ok')
    renderCard({ calls: 20, recordedCalls: 20, totalTokens: 9000, since: '2026-10-14T10:00:00Z' })

    await waitFor(() => {
      expect(cardText()).toContain('最贵的那道题')
    })
    /*
      ★ 条长按"最大值"归一，不是按总量。不说明的话用户会把最长的
      那根读成"占了全部" —— 而它只是相对最大。这句话必须存在。
    */
    expect(cardText()).toContain('不代表占总量比例')
  })
})

/*
  ══════════════════════════════════════════════════════════════
  v0.37 新增：按模型细分 + 逐轮下钻
  ══════════════════════════════════════════════════════════════

  这两块是用户"细分不够"那三次反馈的直接落点：

    · 缺口一 —— 后端一直在返回 `byModel`，前端从没用它。换模（v0.35
      的 fallback）会让同一次解题用多个模型，而"换模前后各花多少"
      是这张卡最有信息量的读数之一；
    · 缺口二 —— 用户说的是"每个**任务**"，而只有按题聚合、点不进去。
      逐轮端点早就有了（`loadCtf2ChallengeUsage`），只是没有入口用。
*/
describe('TokenUsageCard：按模型细分（v0.37）', () => {
  it('★★ 渲染出 byModel 的每一行（含 providerLabel 与调用次数）', async () => {
    installFetchStub('ok')
    renderCard({ calls: 78, recordedCalls: 78, totalTokens: 9000, since: '2026-10-15T08:49:54.129Z' })

    await waitFor(() => expect(cardText()).toContain('按模型'))

    const text = cardText()
    // 两个模型都要在 —— 这正是"换模前后各花多少"的读法
    expect(text).toContain('deepseek-v4.1-flash（gzd）')
    expect(text).toContain('backup-model（other）')
    // 组标题与计数
    expect(text).toContain('2 项')
  })

  it('★★ 按模型与按题**并列**，不合成一组', async () => {
    installFetchStub('ok')
    renderCard({ calls: 78, recordedCalls: 78, totalTokens: 9000, since: '2026-10-15T08:49:54.129Z' })

    await waitFor(() => expect(cardText()).toContain('按模型'))

    // 两个维度同时在场 —— 合成一组会永久丢掉另一个维度
    expect(cardText()).toContain('按模型')
    expect(cardText()).toContain('按题目')
  })

  it('byModel 为空时给"还没有记录"，而不是留一片空白', async () => {
    installFetchStub('ok')
    // 只让 byChallenge 有数据
    vi.stubGlobal('fetch', () =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            ...EMPTY_USAGE,
            byModel: [],
            byChallenge: [{ challengeId: 'c1', title: 'x', calls: 1, totalTokens: 10 }],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )
    renderCard({ calls: 1, recordedCalls: 1, totalTokens: 10, since: '2026-10-15T08:49:54.129Z' })

    await waitFor(() => expect(cardText()).toContain('还没有按模型的记录'))
  })
})

describe('TokenUsageCard：逐轮下钻（v0.37）', () => {
  it('★★ 点某道题就地展开逐轮明细（不是跳走）', async () => {
    installFetchStub('ok', 'ok')
    renderCard({ calls: 20, recordedCalls: 20, totalTokens: 9000, since: '2026-10-15T08:49:54.129Z' })

    await waitFor(() => expect(cardText()).toContain('最贵的那道题'))

    /*
      ⚠️ 这里**不能**断言 `not.toContain('逐轮消耗')` —— "按题目"那组的
      提示语里就有这四个字（"点某道题可以就地展开它的逐轮消耗"），
      那样写会永远失败。用只有展开面板才有的标记来判断。
    */
    expect(cardText()).not.toContain('最贵的 5 轮')

    screen.getByRole('button', { name: /最贵的那道题/ }).click()

    await waitFor(() => expect(cardText()).toContain('最贵的 5 轮'))
    const text = cardText()
    // 逐轮的行内容
    expect(text).toContain('第 1 轮')
    expect(text).toContain('第 2 轮')
    // 两段排序口径都要标出来
    expect(text).toContain('逐轮消耗')
    expect(text).toContain('全部轮次')
  })

  it('★★★ 某轮 usageReported=false 时显示「未记录」，**不是** 0', async () => {
    installFetchStub('ok', 'ok')
    renderCard({ calls: 20, recordedCalls: 20, totalTokens: 9000, since: '2026-10-15T08:49:54.129Z' })

    await waitFor(() => expect(cardText()).toContain('最贵的那道题'))
    screen.getByRole('button', { name: /最贵的那道题/ }).click()
    await waitFor(() => expect(cardText()).toContain('逐轮消耗'))

    const text = cardText()
    /*
      ★★ 这是整卡那条"两种语义不能共用一个空"的纪律落到每一轮上。
      stub 里第 3 轮 usageReported=false、token 全 0 —— 界面必须说
      「未记录」，**而且三个数字列都不能露出 0**。

      ⚠️ 只把合计列写成「未记录」是不够的：后端对未记录的轮次把
      prompt/completion 也存成 0，所以入/出两列照旧渲染 `0 入 / 0 出`
      会自相矛盾 —— 用户会信那两个数字（"这轮没花钱"）。下面两条
      断言就是在钉这一点。
    */
    expect(text).toContain('未记录')
    expect(text).toContain('1 轮接入点未返回用量')
    // 有记录的那两轮照常显示数值
    expect(text).toContain('第 1 轮')
    // 未记录那轮不能出现 "0 入 / 0 出" 这种看起来像读数的东西
    expect(text).not.toContain('0 入 / 0 出')
  })

  it('★ 逐轮明细请求失败只降级一句，不把整卡打成错误态', async () => {
    installFetchStub('ok', 'fail')
    renderCard({ calls: 20, recordedCalls: 20, totalTokens: 9000, since: '2026-10-15T08:49:54.129Z' })

    await waitFor(() => expect(cardText()).toContain('最贵的那道题'))
    screen.getByRole('button', { name: /最贵的那道题/ }).click()
    await waitFor(() => expect(cardText()).toContain('逐轮明细暂时取不到'))
    // 总消耗与按题合计都还在
    expect(cardText()).toContain('共 20 次调用')
    expect(cardText()).toContain('最贵的那道题')
  })

  it('★ 再点一次收起（同一道题切换）', async () => {
    installFetchStub('ok', 'ok')
    renderCard({ calls: 20, recordedCalls: 20, totalTokens: 9000, since: '2026-10-15T08:49:54.129Z' })

    await waitFor(() => expect(cardText()).toContain('最贵的那道题'))
    const row = screen.getByRole('button', { name: /最贵的那道题/ })

    row.click()
    await waitFor(() => expect(cardText()).toContain('最贵的 5 轮'))

    row.click()
    // 同样用"只有展开面板才有"的标记（见上一条的说明）
    await waitFor(() => expect(cardText()).not.toContain('最贵的 5 轮'))
  })

  it('★ 展开的行有 aria-expanded（读屏器能说出展开状态）', async () => {
    installFetchStub('ok', 'ok')
    renderCard({ calls: 20, recordedCalls: 20, totalTokens: 9000, since: '2026-10-15T08:49:54.129Z' })

    await waitFor(() => expect(cardText()).toContain('最贵的那道题'))
    const row = screen.getByRole('button', { name: /最贵的那道题/ })

    expect(row.getAttribute('aria-expanded')).toBe('false')
    row.click()
    await waitFor(() => expect(row.getAttribute('aria-expanded')).toBe('true'))
  })

  it('★ 按模型那组**不可**展开（逐轮是按题取的）', async () => {
    installFetchStub('ok', 'ok')
    renderCard({ calls: 78, recordedCalls: 78, totalTokens: 9000, since: '2026-10-15T08:49:54.129Z' })

    await waitFor(() => expect(cardText()).toContain('按模型'))
    // 模型名是纯文本，不是按钮 —— 点它不该冒出一个逐轮区块
    expect(screen.queryByRole('button', { name: /deepseek-v4\.1-flash/ })).toBeNull()
  })
})

/*
  ══════════════════════════════════════════════════════════════
  那行链接的文案必须**诚实**（2026-10-15 修，Chief 指出）
  ══════════════════════════════════════════════════════════════

  v0.36/v0.37 初版写的是「去 CTF2 看逐轮明细」—— 那是**假承诺**：

    · 逐轮明细的 UI **只有本组件的就地展开有**。CTF2 页面根本没有
      逐轮视图（全项目 `loadCtf2ChallengeUsage` 只有这里在用）；
    · 而 CTF2 页也不读任何 query 参数，跳过去连"哪道题"都定位不到。

  用户点过去**看不到任何轮次明细**，与契约 §3.18「宁可少说，不说错的」
  冲突。现在改成指向**题解** —— 那才是确实只存在于那张卡片上的东西
  （扫描页那条路对 CTF2 的壳会 409 并指回这里，见契约 §3.19.5）。

  这条测试钉住"不要再说逐轮明细"，防止文案回退。
*/
describe('TokenUsageCard：链接文案的诚实性', () => {
  it('★★ 不承诺"去 CTF2 看逐轮明细"（那里没有逐轮 UI）', async () => {
    installFetchStub('ok', 'ok')
    renderCard({ calls: 20, recordedCalls: 20, totalTokens: 9000, since: '2026-10-15T08:49:54.129Z' })

    await waitFor(() => expect(cardText()).toContain('最贵的那道题'))
    screen.getByRole('button', { name: /最贵的那道题/ }).click()
    await waitFor(() => expect(cardText()).toContain('最贵的 5 轮'))

    const text = cardText()
    // 逐轮明细只在就地展开里 —— 链接不能把它说成"去别处看"
    expect(text).not.toContain('去 CTF2 看逐轮明细')
    expect(text).not.toContain('看逐轮明细')
  })

  it('★ 链接指向题解，且指向 /ctf2', async () => {
    installFetchStub('ok', 'ok')
    renderCard({ calls: 20, recordedCalls: 20, totalTokens: 9000, since: '2026-10-15T08:49:54.129Z' })

    await waitFor(() => expect(cardText()).toContain('最贵的那道题'))
    screen.getByRole('button', { name: /最贵的那道题/ }).click()
    await waitFor(() => expect(cardText()).toContain('最贵的 5 轮'))

    const link = screen.getByRole('link', { name: /去 CTF2/ })
    expect(link.getAttribute('href')).toBe('/ctf2')
    expect(link.textContent).toContain('题解')
  })
})
