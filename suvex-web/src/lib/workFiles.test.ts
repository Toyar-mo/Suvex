import { describe, expect, it } from 'vitest'

import { PERSIST_PREFIX, splitWorkLinks, workspaceFileUrl } from '@/lib/workFiles'

describe('splitWorkLinks', () => {
  it('把 /work/persist/ 路径切出来，其余保持文字', () => {
    const segments = splitWorkLinks('结果在 /work/persist/report.md 里')
    expect(segments.map((s) => s.type)).toEqual(['text', 'link', 'text'])
    const link = segments[1]
    expect(link.type).toBe('link')
    if (link.type === 'link') {
      expect(link.path).toBe('report.md')
      expect(link.value).toBe('/work/persist/report.md')
    }
  })

  it('末尾的中文句号/反引号不算进路径', () => {
    const segments = splitWorkLinks('已生成 `/work/persist/out/flag.txt`。')
    const link = segments.find((s) => s.type === 'link')
    expect(link && link.type === 'link' ? link.path : null).toBe('out/flag.txt')
  })

  it('没有持久区路径时原样返回一段文字', () => {
    const segments = splitWorkLinks('全是普通文字 /work/out/x.bin')
    expect(segments).toEqual([{ type: 'text', value: '全是普通文字 /work/out/x.bin' }])
  })

  it('空字符串返回空数组', () => {
    expect(splitWorkLinks('')).toEqual([])
  })
})

describe('workspaceFileUrl', () => {
  it('带 /api/v1 前缀，并对会话 id 与路径编码', () => {
    const url = workspaceFileUrl('chat_1', 'out/a b.txt')
    expect(url).toContain('/api/v1/chat/sessions/chat_1/files/download')
    expect(url).toContain('path=out%2Fa%20b.txt')
  })

  it('持久区前缀是 /work/persist/', () => {
    expect(PERSIST_PREFIX).toBe('/work/persist/')
  })
})
