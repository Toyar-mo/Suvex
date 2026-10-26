import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { ResourceStrip } from './ResourceStrip'
import type { ResourceCount } from '@/data/overview'

/**
 * 「可用能力」三格（v0.96）。
 *
 * ## 这块最容易出的三种错，各有一组用例
 *
 * 1. **把「读不到」画成 0**。`muted` 与 `attention` 都带一个数字，
 *    如果语气没生效，用户会把"故障"读成"事实"—— 这是这块唯一的
 *    欺骗性失败，也是它存在的意义所在（家底要可信）。
 * 2. **认不出的 kind 白屏**。后端加一格时旧前端必须少画一格而不是崩。
 * 3. **全都不认识时画个空壳**。一个只有标题没有内容的卡片，
 *    看起来与"这里坏了"完全一样。
 */

function resource(overrides: Partial<ResourceCount> = {}): ResourceCount {
  return {
    kind: 'mcp_tool',
    label: '可用工具',
    count: 21,
    detail: '本机内置',
    tone: 'ok',
    ...overrides,
  }
}

function renderStrip(resources: ResourceCount[]) {
  return render(
    <MemoryRouter>
      <ResourceStrip resources={resources} />
    </MemoryRouter>,
  )
}

describe('ResourceStrip 渲染', () => {
  it('三格都在时标题与数字都出来', () => {
    renderStrip([
      resource({ kind: 'mcp_tool', label: '可用工具', count: 21 }),
      resource({ kind: 'mcp_server', label: '外部 MCP', count: 3 }),
      resource({ kind: 'skill', label: '技能卡', count: 31 }),
    ])

    expect(screen.getByText('可用能力')).toBeTruthy()
    expect(screen.getByText('可用工具')).toBeTruthy()
    expect(screen.getByText('21')).toBeTruthy()
    expect(screen.getByText('外部 MCP')).toBeTruthy()
    expect(screen.getByText('3')).toBeTruthy()
    expect(screen.getByText('技能卡')).toBeTruthy()
    expect(screen.getByText('31')).toBeTruthy()
  })

  it('补充口径为空串时不渲染空段', () => {
    const { container } = renderStrip([resource({ detail: '' })])
    // 只有标签与数字两行，没有第三个 span
    expect(container.textContent).not.toContain('undefined')
    expect(screen.getByText('可用工具')).toBeTruthy()
  })

  it('每格都链到对应的管理页', () => {
    renderStrip([
      resource({ kind: 'mcp_tool' }),
      resource({ kind: 'mcp_server' }),
      resource({ kind: 'skill' }),
    ])
    const links = screen.getAllByRole('link')
    // 工具与 MCP 都去 `/tools`（同一个页面管两件事），技能去 `/skills`
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['/tools', '/tools', '/skills'])
  })
})

describe('ResourceStrip 的三种语气必须可区分', () => {
  it('muted（读不到）要标「读不到」，不能长得像 0', () => {
    renderStrip([resource({ kind: 'skill', label: '技能卡', count: 0, tone: 'muted' })])
    // 数字照常显示，但必须带一个词说明它是"不知道"而不是"没有"
    expect(screen.getByText('读不到')).toBeTruthy()
  })

  it('attention（待配置）要标「待配置」', () => {
    renderStrip([resource({ kind: 'mcp_server', count: 0, tone: 'attention' })])
    expect(screen.getByText('待配置')).toBeTruthy()
  })

  it('ok 不挂任何语气词 —— 正常值加标签是噪音', () => {
    renderStrip([resource({ kind: 'mcp_tool', count: 21, tone: 'ok' })])
    expect(screen.queryByText('读不到')).toBeNull()
    expect(screen.queryByText('待配置')).toBeNull()
  })

  it('认不出的 tone 退化成 muted 而不是当正常值', () => {
    // 后端将来加一种语气时，旧前端的兜底必须是**保守**的：
    // 当成"读不到"会让人去查，当成 ok 会让人相信一个不知道的数字。
    renderStrip([
      resource({ kind: 'skill', tone: 'bogus' as unknown as ResourceCount['tone'] }),
    ])
    expect(screen.getByText('读不到')).toBeTruthy()
  })
})

describe('ResourceStrip 对未知 kind 的降级', () => {
  it('部分认不出时只画认得的那些', () => {
    renderStrip([
      resource({ kind: 'mcp_tool', label: '可用工具', count: 21 }),
      resource({ kind: 'knowledge', label: '知识库', count: 99 }),
    ])
    expect(screen.getByText('可用工具')).toBeTruthy()
    expect(screen.queryByText('知识库')).toBeNull()
    expect(screen.queryByText('99')).toBeNull()
  })

  it('全部认不出时整块不渲染，而不是画一个空壳标题', () => {
    // ★ 空壳标题比不画更糟：用户会以为这块加载失败或坏了。
    //   真值：后端把我们所有 kind 都改了名 —— 少画三格是可接受的降级。
    const { container } = renderStrip([
      resource({ kind: 'a' }),
      resource({ kind: 'b' }),
    ])
    expect(container.textContent).toBe('')
  })

  it('空数组也不渲染', () => {
    const { container } = renderStrip([])
    expect(container.textContent).toBe('')
  })
})
