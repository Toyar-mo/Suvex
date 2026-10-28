import { describe, expect, it } from 'vitest'

import type { ChatSessionDetail } from '../data/chat'
import {
  buildConversationMarkdown,
  buildConversationMarkdownFileName,
} from './conversationMarkdown'

/*
  ══════════════════════════════════════════════════════════════
  导出 Markdown：判据是「导出的文件能不能当记录用」
  ══════════════════════════════════════════════════════════════

  这个函数纯拼字符串，很容易写得"看起来对"而实际漏东西，所以只测四件
  会让人拿着文件去核对时被误导的事：

  ① 消息必须按 `seq` 排（后端返回的顺序不保证，乱序的对话记录读起来像
     模型在自问自答）；
  ② `精简` 与 `完整` 必须有**可见差别** —— 差别在工具的参数与输出正文；
  ③ 文件名必须把会破坏路径的字符换掉，否则用户在 Windows 上会"下载失败"
     或得到一个被截断的名字；
  ④ 正文里本身含 ``` 围栏时不能被提前闭合（否则后半段全变成普通段落）。
*/

function makeSession(over: Partial<ChatSessionDetail> = {}): ChatSessionDetail {
  return {
    id: 'chat_abcdef123456',
    title: '测试对话',
    messageCount: 2,
    createdAt: '2026-10-27T10:00:00Z',
    updatedAt: '2026-10-27T10:05:00Z',
    ...over,
  } as unknown as ChatSessionDetail
}

describe('buildConversationMarkdownFileName', () => {
  it('把破坏路径的字符换成下划线，中文与其余字符保留', () => {
    const name = buildConversationMarkdownFileName(
      { id: 'chat_abcdef123456', title: 'A/B: C*D? E"F<G>H|I' },
      'summary',
    )
    // 中文标题不该被洗成空白 —— 那会让用户认不出导出的是哪个对话
    expect(name).toContain('_')
    expect(name).not.toMatch(/[\\/:*?"<>|]/)
    expect(name.endsWith('_精简.md')).toBe(true)
  })

  it('标题为空时回退成「对话」，不会产出 `_xxxx.md` 这种名字', () => {
    expect(buildConversationMarkdownFileName({ id: 'chat_x', title: '   ' }, 'full')).toBe(
      '对话_chat_x_完整.md',
    )
  })

  it('用 id 前 8 位区分同名对话，且粒度写进文件名', () => {
    const a = buildConversationMarkdownFileName({ id: 'chat_aaaaaaaa11', title: '同名' }, 'summary')
    const b = buildConversationMarkdownFileName({ id: 'chat_bbbbbbbb22', title: '同名' }, 'full')
    expect(a).toBe('同名_chat_aaa_精简.md')
    expect(b).toBe('同名_chat_bbb_完整.md')
  })
})

describe('buildConversationMarkdown', () => {
  const ordered = makeSession({
    messages: [
      {
        seq: 2,
        role: 'assistant',
        content: '第二条',
        createdAt: '2026-10-27T10:01:00Z',
      },
      {
        seq: 1,
        role: 'user',
        content: '第一条',
        createdAt: '2026-10-27T10:00:30Z',
      },
    ],
  } as unknown as Partial<ChatSessionDetail>)

  it('按 seq 升序输出，即使数组本身是乱序的', () => {
    const text = buildConversationMarkdown(ordered, 'summary')
    const first = text.indexOf('第一条')
    const second = text.indexOf('第二条')
    expect(first).toBeGreaterThan(-1)
    expect(second).toBeGreaterThan(-1)
    expect(first).toBeLessThan(second)
  })

  it('顶部有元信息，且「消息条数」数的是真实条数而不是 messageCount 字段', () => {
    const text = buildConversationMarkdown(ordered, 'summary')
    expect(text.startsWith('# 测试对话')).toBe(true)
    expect(text).toContain('- 对话 ID：`chat_abcdef123456`')
    expect(text).toContain('- 消息条数：2')
    expect(text).toContain('\n---\n')
  })

  it('角色翻成中文标签，未知角色原样回显（不吞掉）', () => {
    const text = buildConversationMarkdown(
      makeSession({
        messages: [{ seq: 1, role: 'system' as never, content: 'x', createdAt: null }],
      } as unknown as Partial<ChatSessionDetail>),
      'summary',
    )
    expect(text).toContain('## 1. system')
  })

  it('精简版：工具只写名字与成败，不附参数与输出', () => {
    const session = makeSession({
      messages: [
        {
          seq: 1,
          role: 'tool',
          content: '这是很长很长的输出正文',
          toolName: 'run_shell',
          toolArgs: { command: 'ls -la /work' },
          toolFailed: false,
          createdAt: null,
        },
      ],
    } as unknown as Partial<ChatSessionDetail>)

    const summary = buildConversationMarkdown(session, 'summary')
    expect(summary).toContain('**工具**：`run_shell`（成功）')
    expect(summary).not.toContain('ls -la /work')
    expect(summary).not.toContain('这是很长很长的输出正文')
  })

  it('完整版：附上参数 JSON 与输出正文（这是两个粒度的可见差别）', () => {
    const session = makeSession({
      messages: [
        {
          seq: 1,
          role: 'tool',
          content: '这是很长很长的输出正文',
          toolName: 'run_shell',
          toolArgs: { command: 'ls -la /work' },
          toolFailed: true,
          createdAt: null,
        },
      ],
    } as unknown as Partial<ChatSessionDetail>)

    const full = buildConversationMarkdown(session, 'full')
    expect(full).toContain('**工具**：`run_shell`（失败）')
    expect(full).toContain('"command": "ls -la /work"')
    expect(full).toContain('这是很长很长的输出正文')
  })

  it('输出正文自带 ``` 围栏时不会被提前闭合', () => {
    const session = makeSession({
      messages: [
        {
          seq: 1,
          role: 'tool',
          content: '```python\nprint(1)\n```',
          toolName: 'read_attachment',
          toolArgs: {},
          toolFailed: false,
          createdAt: null,
        },
      ],
    } as unknown as Partial<ChatSessionDetail>)

    const full = buildConversationMarkdown(session, 'full')
    // 四个反引号包住，正文里的三反引号就不会截断它
    expect(full).toContain('````text')
    expect(full).toContain('```python')
  })

  it('正文为空给占位符，而不是留一个空标题', () => {
    const text = buildConversationMarkdown(
      makeSession({
        messages: [{ seq: 1, role: 'assistant', content: '   ', createdAt: null }],
      } as unknown as Partial<ChatSessionDetail>),
      'summary',
    )
    expect(text).toContain('_（这条消息没有正文）_')
  })
})
