import { describe, expect, it } from 'vitest'

import { groupConversationTurns } from './conversationTurns'
import type { ChatMessage } from '@/data/chat'

function message(id: string, role: ChatMessage['role'] = 'assistant'): ChatMessage {
  return {
    id,
    seq: 1,
    role,
    content: id,
    createdAt: '2026-11-14T00:00:00Z',
  }
}

describe('groupConversationTurns', () => {
  it('同一个 id 的重复帧只渲染一次', () => {
    const answer = message('a1')
    const turns = groupConversationTurns([
      message('u1', 'user'),
      answer,
      answer,
    ])

    expect(turns).toHaveLength(1)
    expect(turns[0].items).toHaveLength(1)
  })
})
