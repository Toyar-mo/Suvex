import { describe, expect, it } from 'vitest'
import { groupMessageRows, isProcessMessage } from './messageRows'
import type { ChatMessage } from '@/data/chat'

function msg(over: Partial<ChatMessage> & { id: string }): ChatMessage {
  return {
    seq: 1,
    role: 'assistant',
    content: '',
    createdAt: '2026-10-28T00:00:00Z',
    ...over,
  } as ChatMessage
}

describe('isProcessMessage — 只有旁白收进折叠块（v1.25）', () => {
  it('标记 surface=process 的旁白收进折叠块', () => {
    expect(isProcessMessage(msg({ id: 'a', surface: 'process', content: '我先看看' }))).toBe(true)
  })

  it('★ 工具行**不**收 —— 它本来就是一行灰字，再折一层用户就看不到它在干嘛', () => {
    expect(isProcessMessage(msg({ id: 'a', role: 'tool', toolName: 'run_shell' }))).toBe(false)
  })

  it('最终答复不是过程面（必须画成气泡）', () => {
    expect(isProcessMessage(msg({ id: 'a', content: '结论是 flag{x}' }))).toBe(false)
  })

  it('用户消息不是过程面', () => {
    expect(isProcessMessage(msg({ id: 'a', role: 'user', content: '你好' }))).toBe(false)
  })

  it('审批中的消息永远不收（那是等用户点确认的卡片）', () => {
    expect(isProcessMessage(msg({ id: 'a', role: 'tool', approvalStatus: 'pending' }))).toBe(false)
  })
})

describe('groupMessageRows — 旁白收走、工具与结论留在主线', () => {
  it('★ 用户 → 折叠的旁白 → 工具行 → 结论', () => {
    const rows = groupMessageRows([
      msg({ id: 'u1', role: 'user', content: '解题', seq: 1 }),
      msg({ id: 'n1', surface: 'process', content: '我先看附件', seq: 2 }),
      msg({ id: 't1', role: 'tool', toolName: 'run_shell', seq: 3 }),
      msg({ id: 'n2', surface: 'process', content: '这是 pwn', seq: 4 }),
      msg({ id: 'a1', content: 'flag{abc}', seq: 6 }),
    ])
    expect(rows.map((r) => r.kind)).toEqual([
      'single', // user
      'process', // n1
      'single', // tool
      'process', // n2
      'single', // answer
    ])
  })

  it('连续的旁白合并成一个块', () => {
    const rows = groupMessageRows([
      msg({ id: 'n1', surface: 'process', seq: 1 }),
      msg({ id: 'n2', surface: 'process', seq: 2 }),
    ])
    expect(rows.map((r) => r.kind)).toEqual(['process'])
    const only = rows[0]
    expect(only.kind === 'process' && only.items.map((i) => i.id)).toEqual(['n1', 'n2'])
  })

  it('纯对话原样返回', () => {
    const rows = groupMessageRows([
      msg({ id: 'u1', role: 'user', content: '你好', seq: 1 }),
      msg({ id: 'a1', content: '你好呀', seq: 2 }),
    ])
    expect(rows.map((r) => r.kind)).toEqual(['single', 'single'])
  })

  it('空列表返回空', () => {
    expect(groupMessageRows([])).toEqual([])
  })
})
