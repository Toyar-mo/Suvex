import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { FindingListItem } from '@/data/finding'
import { scanFindingsHref } from '@/data/session-insight'
import { SessionFindingsModal } from './SessionFindingsModal'

/*
  ══════════════════════════════════════════════════════════════
  「查看漏洞」抽屉：核心判据是「三种空态必须分开」
  ══════════════════════════════════════════════════════════════

  这个抽屉最贵的错误是把"没数据源"和"取数失败"说成同一句"暂无漏洞"：
  用户会以为目标真的没有漏洞，而实际上请求根本没成功。

  ① 没扫描 → 明说「还没有创建过扫描」（这不是坏，是没数据源）
  ② 有扫描但零漏洞 → 明说「关联的扫描还没有产出漏洞」
  ③ 取数失败 → 报错 + 重试按钮
  ④ 成功 → 每条给出严重级、状态、标题、目标，且「查看」链到该扫描的漏洞页
*/

function finding(over: Partial<FindingListItem> = {}): FindingListItem {
  return {
    id: 'f_1',
    scanId: 'scan_01M5ZC079EK327M779ZJ4Z2WPE',
    severity: 'high',
    status: 'new',
    title: 'TLS 1.0 仍启用',
    target: 'https://example.com:443',
    ...over,
  } as FindingListItem
}

function renderDrawer(over: Partial<React.ComponentProps<typeof SessionFindingsModal>> = {}) {
  const props = {
    open: true,
    onClose: vi.fn(),
    sessionTitle: '测试会话',
    scanIds: [] as string[],
    items: [] as FindingListItem[],
    loading: false,
    error: null as string | null,
  }
  return {
    props,
    ...render(
      <MemoryRouter>
        <SessionFindingsModal {...props} {...over} />
      </MemoryRouter>,
    ),
  }
}

// ⚠️ Drawer 用 `createPortal` 渲染到 document.body，所以断言一律查 `document`，
// 而不是 render 返回的 container（那里永远是空的）。
afterEach(() => cleanup())

describe('SessionFindingsModal', () => {
  it('关闭时不渲染内容', () => {
    renderDrawer({ open: false })
    expect(document.querySelector('[data-findings-list]')).toBeNull()
    expect(screen.queryByText('关联漏洞')).toBeNull()
  })

  it('loading 时不显示任何空态（否则用户以为没漏洞）', () => {
    renderDrawer({ loading: true, scanIds: ['scan_a'] })
    expect(document.querySelector('[data-findings-loading]')).toBeTruthy()
    expect(document.querySelector('[data-findings-empty]')).toBeNull()
  })

  it('没扫描时明说"还没有创建过扫描"', () => {
    renderDrawer({ scanIds: [] })
    const empty = document.querySelector('[data-findings-empty="no-scan"]')
    expect(empty).toBeTruthy()
    expect(empty?.textContent).toContain('还没有创建过扫描')
  })

  it('有扫描但零漏洞时说的是另一句话（不能被读成"没数据源"）', () => {
    renderDrawer({ scanIds: ['scan_a', 'scan_b'] })
    const empty = document.querySelector('[data-findings-empty="no-finding"]')
    expect(empty).toBeTruthy()
    expect(empty?.textContent).toContain('2 个扫描')
    expect(empty?.textContent).toContain('还没有产出漏洞')
    expect(document.querySelector('[data-findings-empty="no-scan"]')).toBeNull()
  })

  it('取数失败时报错并给重试按钮，不是空态', () => {
    const onRetry = vi.fn()
    renderDrawer({ scanIds: ['scan_a'], error: '请求超时', onRetry })
    expect(document.querySelector('[data-findings-error]')).toBeTruthy()
    expect(screen.getByText(/请求超时/)).toBeTruthy()
    expect(document.querySelector('[data-findings-empty]')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('有漏洞时逐条渲染，并链到该扫描的漏洞页', () => {
    renderDrawer({
      scanIds: ['scan_01M5ZC079EK327M779ZJ4Z2WPE'],
      items: [
        finding(),
        finding({ id: 'f_2', severity: 'low', status: 'fixed', title: '缺少 HSTS', target: 'https://a.test' }),
      ],
    })
    const rows = document.querySelectorAll('[data-findings-list] > li')
    expect(rows).toHaveLength(2)
    expect(screen.getByText('TLS 1.0 仍启用')).toBeTruthy()
    expect(screen.getByText('缺少 HSTS')).toBeTruthy()
    const links = screen.getAllByRole('link', { name: '查看' })
    expect(links).toHaveLength(2)
    // 用同一个 helper 算期望值，避免测试里硬编码一份路径而和实现漂开。
    expect(links[0].getAttribute('href')).toBe(scanFindingsHref('scan_01M5ZC079EK327M779ZJ4Z2WPE'))
  })

  it('标题栏摘要同时给出条数与扫描数', () => {
    renderDrawer({ scanIds: ['scan_a'], items: [finding()] })
    /*
      标题是**一个字符串**（`Modal` 只有这样才会设 `aria-label`，弹窗才有
      可访问名）。所以断言读弹窗的可访问名，而不是去找一个独立的文本节点 ——
      后者会因为「摘要现在长在标题里」而假红。
    */
    const dialog = screen.getByRole('dialog')
    const label = dialog.getAttribute('aria-label') ?? ''
    expect(label).toContain('关联漏洞')
    expect(label).toContain('1 条 · 1 个扫描')
  })
})
