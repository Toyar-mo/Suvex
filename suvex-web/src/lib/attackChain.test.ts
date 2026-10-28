import { describe, expect, it } from 'vitest'

import type { ChatMessage } from '@/data/chat'
import {
  buildAttackChain,
  collectScanIds,
  formatToolArgs,
  summarizeChain,
} from './attackChain'

let seq = 0
function msg(over: Partial<ChatMessage>): ChatMessage {
  seq += 1
  return {
    id: `m_${seq}`,
    seq,
    role: 'user',
    content: '',
    createdAt: '2026-10-27T00:00:00Z',
    ...over,
  }
}

function reset() {
  seq = 0
}

describe('buildAttackChain', () => {
  it('把一条完整轮次分成 提问 / 决策 / 工具 / 回答 四步', () => {
    reset()
    const steps = buildAttackChain([
      msg({ role: 'user', content: '帮我看看 example.com' }),
      msg({ role: 'assistant', content: '我先探测一下。' }),
      msg({ role: 'tool', content: 'HTTP/1.1 200 OK', toolName: 'run_shell' }),
      msg({ role: 'assistant', content: '对方返回 200，站点在线。' }),
    ])
    expect(steps.map((s) => s.kind)).toEqual(['ask', 'plan', 'tool', 'answer'])
    expect(steps.map((s) => s.label)).toEqual(['用户提问', '决策', '调用 run_shell', '回答'])
  })

  it('assistant 后面没有工具时算「回答」而不是「决策」', () => {
    reset()
    const steps = buildAttackChain([msg({ role: 'assistant', content: '结论：没问题。' })])
    expect(steps).toHaveLength(1)
    expect(steps[0].kind).toBe('answer')
  })

  it('工具失败会被标出来（这是这条链最有用的信息）', () => {
    reset()
    const steps = buildAttackChain([
      msg({ role: 'tool', content: '失败：命令不存在', toolName: 'run_shell', toolFailed: true }),
    ])
    expect(steps[0].failed).toBe(true)
    expect(steps[0].toolName).toBe('run_shell')
  })

  it('空内容的 assistant 消息不构成一步（历史占位消息不该出现在链上）', () => {
    reset()
    const steps = buildAttackChain([
      msg({ role: 'user', content: '解题' }),
      msg({ role: 'assistant', content: '   ' }),
      msg({ role: 'assistant', content: '这是答案。' }),
    ])
    expect(steps.map((s) => s.kind)).toEqual(['ask', 'answer'])
  })

  it('长正文会被截断，不把抽屉撑爆', () => {
    reset()
    const steps = buildAttackChain([msg({ role: 'user', content: 'x'.repeat(1000) })])
    expect(steps[0].detail.length).toBeLessThan(300)
    expect(steps[0].detail.endsWith('…')).toBe(true)
  })
})

describe('collectScanIds', () => {
  it('从工具结果文本里取扫描 id 并去重（保持首次出现顺序）', () => {
    const a = 'scan_01M5ZC079EK327M779ZJ4Z2WPE'
    const b = 'scan_01M5WJ6HSW3SYP22XJBHFMTBR8'
    const messages = [
      msg({ role: 'tool', content: `已创建扫描任务 \`${a}\``, toolName: 'create_scan' }),
      msg({ role: 'tool', content: `最近 2 个：\n- \`${b}\` foo\n- \`${a}\` bar`, toolName: 'list_scans' }),
    ]
    expect(collectScanIds(messages)).toEqual([a, b])
  })

  it('也认 toolArgs 里出现的 id（模型把上一条扫描当参数传下去）', () => {
    const a = 'scan_01M5ZC079EK327M779ZJ4Z2WPE'
    const messages = [
      msg({ role: 'tool', content: '正在查询…', toolName: 'x', toolArgs: { scanId: a } }),
    ]
    expect(collectScanIds(messages)).toEqual([a])
  })

  it('不把 scan_task / 截断的 id 当成扫描 id', () => {
    const messages = [
      msg({ role: 'assistant', content: '我在修 scan_task 这一堆，还有 scan_01M5 这种截断的。' }),
    ]
    expect(collectScanIds(messages)).toEqual([])
  })

  it('多条消息里的同一个 id 只算一次', () => {
    const a = 'scan_01M5ZC079EK327M779ZJ4Z2WPE'
    const messages = [
      msg({ role: 'tool', content: a }),
      msg({ role: 'assistant', content: `我看了 ${a}，没问题。` }),
    ]
    expect(collectScanIds(messages)).toEqual([a])
  })
})

describe('summarizeChain / formatToolArgs', () => {
  it('统计提问数、工具数、失败数与工具名', () => {
    reset()
    const steps = buildAttackChain([
      msg({ role: 'user', content: '一' }),
      msg({ role: 'tool', content: 'a', toolName: 'run_shell' }),
      msg({ role: 'tool', content: 'b', toolName: 'browser', toolFailed: true }),
      msg({ role: 'tool', content: 'c', toolName: 'run_shell' }),
    ])
    const summary = summarizeChain(steps)
    expect(summary.asks).toBe(1)
    expect(summary.tools).toBe(3)
    expect(summary.failed).toBe(1)
    expect(summary.toolNames).toEqual(['run_shell', 'browser'])
  })

  it('工具参数压成一行且跳过空值', () => {
    expect(formatToolArgs({ command: 'ls /work', limit: 5, note: null })).toBe('command=ls /work, limit=5')
    expect(formatToolArgs(null)).toBe('')
  })
})
