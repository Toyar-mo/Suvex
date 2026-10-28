import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatMessage } from '@/data/chat'
import type { FindingListItem } from '@/data/finding'

/*
  ══════════════════════════════════════════════════════════════
  「查看漏洞」的取数层：判据是「一条坏了不能拖垮另外两条」
  ══════════════════════════════════════════════════════════════

  这个模块最容易写错的地方不是取数，而是**健壮性**：

  ① 一个会话通常关联 1~3 个扫描 —— 用 `Promise.all` 的话，任意一条 500
     就让整个抽屉空白，用户以为"这个对话没有漏洞"，而其实另外两条好好的；
  ② 同一个漏洞可能被两个扫描都报出来 —— 不去重就会重复计数；
  ③ 排序必须按严重度，否则高危埋在"待确认"下面；
  ④ 没有扫描 id 时**不该发任何 /findings 请求**（省一次往返，也避免
     后端对空 scanId 报参数错）。
*/

const get = vi.fn()

vi.mock('@/lib/api', () => ({
  API_PREFIX: '/api/v1',
  api: {
    get: (...args: unknown[]) => get(...args),
  },
}))

import { fetchSessionFindings, fetchSessionMessages, findingsPath, scanFindingsHref } from './session-insight'

let seq = 0
function msg(over: Partial<ChatMessage>): ChatMessage {
  seq += 1
  return {
    id: `m_${seq}`,
    seq,
    role: 'tool',
    content: '',
    createdAt: '2026-10-27T00:00:00Z',
    ...over,
  }
}

function finding(over: Partial<FindingListItem> = {}): FindingListItem {
  return {
    id: 'f_1',
    scanId: 'scan_a',
    severity: 'low',
    status: 'new',
    title: 't',
    target: 'x',
    ...over,
  } as FindingListItem
}

beforeEach(() => {
  get.mockReset()
  seq = 0
})

describe('fetchSessionMessages', () => {
  it('把会话详情里的 messages 取出来（没有就是空数组）', async () => {
    get.mockResolvedValueOnce({ id: 'chat_1', messages: [{ id: 'm1' }] })
    await expect(fetchSessionMessages('chat_1')).resolves.toEqual([{ id: 'm1' }])

    get.mockResolvedValueOnce({ id: 'chat_2' })
    await expect(fetchSessionMessages('chat_2')).resolves.toEqual([])
  })

  it('sessionId 会做 URL 编码（id 里带特殊字符不能拼坏路径）', async () => {
    get.mockResolvedValueOnce({ messages: [] })
    await fetchSessionMessages('a/b c')
    expect(get).toHaveBeenCalledWith('/chat/sessions/a%2Fb%20c')
  })
})

describe('fetchSessionFindings', () => {
  it('没有扫描 id 时不发任何 /findings 请求', async () => {
    get.mockResolvedValueOnce({ messages: [msg({ content: '这里没有扫描 id' })] })
    await expect(fetchSessionFindings('chat_1')).resolves.toEqual({ scanIds: [], items: [] })
    expect(get).toHaveBeenCalledTimes(1) // 只有取消息那一次
  })

  it('按严重度排序，且跨扫描去重', async () => {
    const a = 'scan_01M5ZC079EK327M779ZJ4Z2WPE'
    const b = 'scan_01M5WJ6HSW3SYP22XJBHFMTBR8'
    get.mockResolvedValueOnce({ messages: [msg({ content: `${a} 和 ${b}` })] })
    get.mockResolvedValueOnce([finding({ id: 'f_low', scanId: a, severity: 'low' })])
    get.mockResolvedValueOnce([
      finding({ id: 'f_crit', scanId: b, severity: 'critical' }),
      // 同一个漏洞 id 被两个扫描都报出来 —— 这是真实情况（同一目标重复扫描）。
      finding({ id: 'f_low', scanId: b, severity: 'low' }),
    ])

    const result = await fetchSessionFindings('chat_1')
    expect(result.scanIds).toEqual([a, b])
    expect(result.items.map((i) => i.id)).toEqual(['f_crit', 'f_low'])
  })

  it('一条扫描查询失败，其余照常返回（不是整体失败）', async () => {
    const a = 'scan_01M5ZC079EK327M779ZJ4Z2WPE'
    const b = 'scan_01M5WJ6HSW3SYP22XJBHFMTBR8'
    get.mockResolvedValueOnce({ messages: [msg({ content: `${a} ${b}` })] })
    get.mockRejectedValueOnce(new Error('500'))
    get.mockResolvedValueOnce([finding({ id: 'f_ok', scanId: b, severity: 'high' })])

    const result = await fetchSessionFindings('chat_1')
    expect(result.scanIds).toEqual([a, b])
    expect(result.items.map((i) => i.id)).toEqual(['f_ok'])
  })

  it('未知严重度排在最后，不会插到 critical 前面', async () => {
    const a = 'scan_01M5ZC079EK327M779ZJ4Z2WPE'
    get.mockResolvedValueOnce({ messages: [msg({ content: a })] })
    get.mockResolvedValueOnce([
      finding({ id: 'f_unknown', severity: 'no-such-level' as unknown as FindingListItem['severity'] }),
      finding({ id: 'f_crit', severity: 'critical' }),
    ])

    const result = await fetchSessionFindings('chat_1')
    expect(result.items.map((i) => i.id)).toEqual(['f_crit', 'f_unknown'])
  })
})

describe('深链拼装', () => {
  it('scanFindingsHref / findingsPath 都对 id 做了编码，且前缀不同', () => {
    expect(scanFindingsHref('s 1')).toBe('/findings?scanId=s%201')
    expect(findingsPath('s 1')).toBe('/api/v1/findings?scanId=s%201')
  })
})
