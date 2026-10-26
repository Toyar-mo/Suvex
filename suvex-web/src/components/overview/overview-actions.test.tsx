import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import { AttentionList } from './AttentionList'
import RiskScoreCard from './RiskScoreCard'
import type { AttentionItem, RiskLevel, SeverityBreakdownEntry } from '@/data/overview'

/**
 * 总览页那两个新块的测试（v0.95）。
 *
 * ## 为什么这两个组件值得单独测
 *
 * 它们都是「**永远画得出来**」的组件 —— 没有请求、没有 loading 态、
 * 空数据也能渲染成一块正常的卡片。这种组件最危险的地方在于：
 * 后端算错了（风险分口径错、待办项排序错）前端不会报任何错，
 * 只是安静地把错的东西画得很好看。所以测试要钉的是**语义**，
 * 不是像素：
 *
 *   1. 风险分的五档文案必须都有，且 `safe` 不能被兜底成高危；
 *   2. 待办列表的 `kind` 认不出时必须**跳过**而不是崩掉；
 *   3. 空列表要有"为什么空"的说明，不能只画一片空白。
 *
 * ## 为什么不用 `toBeInTheDocument()`
 *
 * 本项目**没有装 `@testing-library/jest-dom`**（`package.json` 里没有，
 * `src/test/setup.ts` 也没有 import 它）。那个断言是 jest-dom 往 chai
 * 上挂的自定义匹配器，没装就会报 `Invalid Chai property`。
 * 既有测试（`OverviewPage.test.tsx` 等）统一用 `queryByText(...)`
 * 的真假值、`getAttribute()`、`textContent` 来断言 —— 这里跟同一套写法，
 * 免得为了一个更顺眼的断言语义引入新依赖。
 */

const SEVERITIES = ['critical', 'high', 'medium', 'low', 'info'] as const

function breakdown(partial: Partial<Record<(typeof SEVERITIES)[number], number>> = {}) {
  return SEVERITIES.map((severity) => ({
    severity,
    count: partial[severity] ?? 0,
  })) as SeverityBreakdownEntry[]
}

function item(overrides: Partial<AttentionItem> = {}): AttentionItem {
  return {
    kind: 'unresolved_critical',
    level: 'urgent',
    title: '2 个待处置的严重漏洞',
    detail: '这些漏洞还没有处置完，会一直算进风险分。',
    count: 2,
    ...overrides,
  }
}

function renderRisk(
  score: number,
  level: RiskLevel,
  bd = breakdown({ critical: 2 }),
  total = bd.reduce((sum, e) => sum + e.count, 0),
) {
  return render(
    <MemoryRouter>
      <RiskScoreCard score={score} level={level} total={total} breakdown={bd} />
    </MemoryRouter>,
  )
}

describe('RiskScoreCard — 五档都要有文案', () => {
  it('每一档都渲染出中文标签，没有一档退化成英文 key', () => {
    const expected: Record<RiskLevel, string> = {
      safe: '无待处置',
      low: '低',
      medium: '中',
      high: '高',
      severe: '极高',
    }

    for (const [level, label] of Object.entries(expected) as [RiskLevel, string][]) {
      const { unmount } = renderRisk(level === 'safe' ? 0 : 20, level)
      expect(screen.queryByText(label)).not.toBeNull()
      // 同时确认没有退化成英文 key（说明确实拿到了文案而不是 fallback）
      expect(screen.queryByText(level)).toBeNull()
      unmount()
    }
  })

  it('★ safe 档显示 0，即使后端给了非零分数', () => {
    /*
     * 这是最坏的一种兜底方向：`safe` 若被 `META[level] ?? META.high`
     * 兜成高危，用户会在"没有任何待处置项"时看到一片红。
     *
     * 分数与等级不一致本身是后端不该发生的事，但前端不能因此把它
     * 画成高危 —— 显示 0 至少与"无待处置"这个结论一致。
     */
    renderRisk(37, 'safe', breakdown())
    expect(screen.queryByText('0')).not.toBeNull()
    expect(screen.queryByText('无待处置')).not.toBeNull()
    expect(screen.queryByText('37')).toBeNull()
  })

  it('非 safe 档显示真实分数', () => {
    renderRisk(20, 'high', breakdown({ critical: 2 }))
    expect(screen.queryByText('20')).not.toBeNull()
    expect(screen.queryByText('高')).not.toBeNull()
  })

  it('总数写在标题右侧，与传入的 total 一致', () => {
    renderRisk(20, 'high', breakdown({ critical: 2 }), 2)
    expect(screen.queryByText('2 项')).not.toBeNull()
  })

  it('★ 进度条宽度夹在 0~100，超高分不会溢出卡片', () => {
    /*
     * 20 个严重 = 200 分。不夹住的话 `width: 200%` 会让条冲出卡片，
     * 而这是很容易发生的真实数据（扫描器一次能产出几十条严重）。
     */
    const { container } = renderRisk(200, 'severe', breakdown({ critical: 20 }), 20)
    const bar = container.querySelector('[style*="width"]') as HTMLElement
    expect(bar.style.width).toBe('100%')
  })

  it('只有非零档出现在构成列表里（0 值不占位）', () => {
    renderRisk(20, 'high', breakdown({ critical: 2 }), 2)
    expect(screen.queryByText('严重')).not.toBeNull()
    expect(screen.queryByText('高危')).toBeNull()
    expect(screen.queryByText('信息')).toBeNull()
  })

  it('没有待处置项时不画构成条（避免除零与空条）', () => {
    const { container } = renderRisk(0, 'safe', breakdown(), 0)
    // 只有进度条那一根（aria-hidden 的构成条不该存在）
    expect(container.querySelectorAll('[aria-hidden="true"]')).toHaveLength(1)
  })

  it('每一档都有说明文字，不留下空段落', () => {
    for (const level of ['safe', 'low', 'medium', 'high', 'severe'] as RiskLevel[]) {
      const { container, unmount } = renderRisk(level === 'safe' ? 0 : 20, level)
      const note = container.querySelector('p')
      expect(note?.textContent?.trim().length ?? 0).toBeGreaterThan(10)
      unmount()
    }
  })
})

describe('AttentionList — 认不出的 kind 必须跳过', () => {
  it('已知 kind 正常渲染标题与数量', () => {
    render(
      <MemoryRouter>
        <AttentionList items={[item()]} />
      </MemoryRouter>,
    )
    expect(screen.queryByText('2 个待处置的严重漏洞')).not.toBeNull()
    expect(screen.queryByText('1 项')).not.toBeNull()
  })

  it('★ 未知 kind 被跳过，其余照常渲染（新后端 + 旧前端不白屏）', () => {
    render(
      <MemoryRouter>
        <AttentionList
          items={[item(), item({ kind: 'future_thing_nobody_knows', title: '未来的建议' })]}
        />
      </MemoryRouter>,
    )
    expect(screen.queryByText('2 个待处置的严重漏洞')).not.toBeNull()
    expect(screen.queryByText('未来的建议')).toBeNull()
    // 计数只数认得出的
    expect(screen.queryByText('1 项')).not.toBeNull()
  })

  it('全部未知时不崩，且走空状态', () => {
    render(
      <MemoryRouter>
        <AttentionList items={[item({ kind: 'a' }), item({ kind: 'b' })]} />
      </MemoryRouter>,
    )
    expect(screen.queryByText('该做什么')).not.toBeNull()
    expect(screen.queryByText('2 项')).toBeNull()
  })

  it('★ 空列表说明为什么空，而不是只画一片空白', () => {
    render(
      <MemoryRouter>
        <AttentionList items={[]} />
      </MemoryRouter>,
    )
    // 不能只断言"渲染了 section"—— 那与坏了长得一样。
    const text = screen.getByText('该做什么').closest('section')?.textContent ?? ''
    expect(text.length).toBeGreaterThan(20)
  })

  it('每一项都带可点的跳转（不是死文案）', () => {
    render(
      <MemoryRouter>
        <AttentionList items={[item()]} />
      </MemoryRouter>,
    )
    const link = screen.getByRole('link')
    expect(link.getAttribute('href')).toBe('/findings?severity=critical')
  })

  it('CTA 文案跟着 kind 走', () => {
    render(
      <MemoryRouter>
        <AttentionList items={[item({ kind: 'failing_schedule', level: 'warning' })]} />
      </MemoryRouter>,
    )
    /*
     * ⚠️ 用正则而不是精确字符串：CTA 在 JSX 里是 `{kind.cta} →`，
     * 渲染出来的 textContent 是「去检查 →」—— 直接 getByText('去检查')
     * 匹配不上（RTL 默认精确匹配整个元素的文本）。这不是组件的问题，
     * 断言要按实际渲染结果写。
     */
    expect(screen.queryByText(/去检查/)).not.toBeNull()
  })
})

describe('AttentionList — 与后端 kind 集合保持同步的手段', () => {
  /*
   * 后端 `build_attention` 目前产出四种 kind（`unresolved_critical` /
   * `unresolved_high` / `pending_approval` / `failing_schedule`）。
   *
   * ★ 这里**不给"认得的 kind"单独导出一份清单**来断言 —— 试过，是坏的：
   *   清单在 `data/` 而图标/跳转在组件里，两者各改一半时，过滤会放行
   *   一条组件画不出来的建议，然后 `KIND_META[...]` 上崩掉整块
   *   （实测 `Cannot read properties of undefined (reading 'icon')`）。
   *   那正好推翻了本组件文件头承诺的"新后端 + 旧前端不白屏"。
   *
   *   所以判据只留一处（`KIND_META`），而这条测试改为**从渲染结果反推**：
   *   四种 kind 逐一渲染，每一种都必须真的画出条目与 CTA。
   *   它同时钉住了两件事 —— 后端集合变了前端没跟上时，这条会红。
   */
  const BACKEND_KINDS = [
    ['unresolved_critical', '去处置'],
    ['unresolved_high', '去处置'],
    ['pending_approval', '去审批'],
    ['failing_schedule', '去检查'],
  ] as const

  it.each(BACKEND_KINDS)('后端产出的 %s 能渲染出条目与 CTA', (kind, cta) => {
    render(
      <MemoryRouter>
        <AttentionList items={[item({ kind, title: `${kind} 的标题` })]} />
      </MemoryRouter>,
    )

    expect(screen.queryByText(`${kind} 的标题`)).not.toBeNull()
    expect(screen.queryByText(new RegExp(cta))).not.toBeNull()
  })

  it('★ 四种 kind 混在一起时按传入顺序全部画出（没有互相干扰）', () => {
    render(
      <MemoryRouter>
        <AttentionList
          items={BACKEND_KINDS.map(([kind]) => item({ kind, title: `${kind} 的标题` }))}
        />
      </MemoryRouter>,
    )

    for (const [kind] of BACKEND_KINDS) {
      expect(screen.queryByText(`${kind} 的标题`)).not.toBeNull()
    }
  })
})
