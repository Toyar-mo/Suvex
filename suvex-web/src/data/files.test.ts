import { describe, expect, it, vi, beforeEach } from 'vitest'

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), del: vi.fn(), upload: vi.fn() },
}))

import { api } from '@/lib/api'
import {
  deleteFile,
  formatBytes,
  formatDate,
  formatTimestamp,
  listFiles,
  listUploadSessions,
  subPathOf,
  uploadFile,
} from './files'

const getMock = vi.mocked(api.get)
const delMock = vi.mocked(api.del)
const uploadMock = vi.mocked(api.upload)

beforeEach(() => {
  getMock.mockReset()
  delMock.mockReset()
  uploadMock.mockReset()
})

describe('formatBytes', () => {
  it('小文件按字节', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(512)).toBe('512 B')
  })
  it('KB / MB 取一位小数或整数', () => {
    expect(formatBytes(1024)).toBe('1.0 KB')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB')
  })
  it('非法值给占位符', () => {
    expect(formatBytes(Number.NaN)).toBe('—')
    expect(formatBytes(-1)).toBe('—')
  })
})

describe('subPathOf', () => {
  it('根目录下的文件', () => {
    expect(subPathOf('report.md')).toBe('(根目录)')
  })
  it('嵌套目录返回父目录', () => {
    expect(subPathOf('out/flag.txt')).toBe('out')
    expect(subPathOf('a/b/c.txt')).toBe('a/b')
  })
})

describe('时间格式化', () => {
  it('epoch 秒转本地时间戳', () => {
    // 用 UTC 构造再读回本地，只断言长度与日期段，避免时区抖动
    const stamp = formatTimestamp(1_700_000_000)
    expect(stamp).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/)
    expect(formatDate(1_700_000_000)).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  })
  it('0 或非法给占位符', () => {
    expect(formatTimestamp(0)).toBe('—')
    expect(formatDate(Number.NaN)).toBe('—')
  })
})

describe('listFiles', () => {
  it('把过滤条件下推成查询串', async () => {
    getMock.mockResolvedValue([])
    await listFiles({ sessionId: 'chat_1', name: '报告' })
    expect(getMock).toHaveBeenCalledTimes(1)
    const path = getMock.mock.calls[0][0] as string
    expect(path.startsWith('/chat/files?')).toBe(true)
    expect(path).toContain('sessionId=chat_1')
    expect(path).toContain(`name=${encodeURIComponent('报告')}`)
    // source=all 是默认值，不该出现在查询串里
    expect(path).not.toContain('source=')
  })

  it('没有条件时不带问号', async () => {
    getMock.mockResolvedValue([])
    await listFiles({})
    expect(getMock.mock.calls[0][0]).toBe('/chat/files')
  })
})

describe('deleteFile', () => {
  it('路径与文件名都做 URL 编码', async () => {
    delMock.mockResolvedValue(undefined)
    await deleteFile('chat_1', 'out/报告 1.md')
    const path = delMock.mock.calls[0][0] as string
    expect(path).toBe(
      `/chat/sessions/chat_1/files?path=${encodeURIComponent('out/报告 1.md')}`,
    )
  })
})

describe('uploadFile', () => {
  it('走 multipart，字段名是 file，落地到会话 files 端点', async () => {
    uploadMock.mockResolvedValue({ path: 'uploads/a.txt', name: 'a.txt', sizeBytes: 3 })
    const file = new File([new Uint8Array([1, 2, 3])], 'a.txt', { type: 'text/plain' })

    const result = await uploadFile('chat_1', file)

    expect(uploadMock).toHaveBeenCalledTimes(1)
    const [path, form] = uploadMock.mock.calls[0] as [string, FormData]
    expect(path).toBe('/chat/sessions/chat_1/files')
    expect(form).toBeInstanceOf(FormData)
    // 字段名必须是 file —— 后端按这个名字取，错一个字母就是 422。
    const sent = form.get('file')
    expect(sent).toBeInstanceOf(File)
    expect((sent as File).name).toBe('a.txt')
    expect(result.path).toBe('uploads/a.txt')
  })

  it('会话 id 做 URL 编码', async () => {
    uploadMock.mockResolvedValue({ path: 'uploads/x', name: 'x', sizeBytes: 1 })
    await uploadFile('a b', new File([new Uint8Array([1])], 'x'))
    expect(uploadMock.mock.calls[0][0]).toBe('/chat/sessions/a%20b/files')
  })
})

describe('listUploadSessions（v1.39 上传目标下拉）', () => {
  it('只列未归档会话，映射成 id/title', async () => {
    getMock.mockResolvedValue([
      { id: 'chat_1', title: '解题' },
      { id: 'chat_2', title: '旧的', archived: true },
    ])
    const out = await listUploadSessions()
    expect(getMock.mock.calls[0][0]).toBe('/chat/sessions')
    expect(out).toEqual([{ id: 'chat_1', title: '解题' }])
  })
})
