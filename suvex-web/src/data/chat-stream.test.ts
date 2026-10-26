import { describe, expect, it } from 'vitest'

import {
  TURN_DONE,
  TURN_MESSAGE,
  TURN_PHASE,
  asChatMessage,
  parseSseChunk,
} from '@/data/chat-stream'

/**
 * ## 这一组只测一件事：**切帧**
 *
 * `parseSseChunk` 是整条流里唯一有状态的地方（半帧要留到下一次），
 * 也是最容易写错的地方：
 *
 *   · 切早了 → 一帧被丢弃，界面上**少一个步骤**（而看起来像模型没做那步）
 *   · 切晚了 → 一条消息要等下一帧才显示，表现为"卡一下然后一次出现两条"
 *   · 半帧当整帧解析 → JSON.parse 抛错，把整条流打断（界面上已经显示的
 *     步骤也跟着作废）
 *
 * 这三种都不会报错到用户面前，只会让界面"看起来怪"。
 *
 * ## 为什么单独测它而不是测 `streamTurn`
 *
 * `streamTurn` 要 mock `fetch` + `ReadableStream`，测起来大部分代码是
 * 在搭脚手架；而它真正的逻辑复杂度**全在这个函数里**（其余是读流 + 分发）。
 * 所以：切帧逻辑在这里密集地测，`streamTurn` 的装配在
 * `chat-stream.integration.test.ts` 里测一次。
 */

describe('parseSseChunk：切帧', () => {
  it('切出一整帧（event + data）', () => {
    const { frames, rest } = parseSseChunk('event: phase\ndata: {"phase":"thinking"}\n\n')
    expect(frames).toHaveLength(1)
    expect(frames[0].event).toBe(TURN_PHASE)
    expect(frames[0].data).toEqual({ phase: 'thinking' })
    expect(rest).toBe('')
  })

  it('★ 半帧留到下一次（不能当整帧解析）', () => {
    const { frames, rest } = parseSseChunk('event: message\ndata: {"id":"m1"')
    expect(frames).toHaveLength(0)
    expect(rest).toContain('event: message')

    // 补上后半截后应该能切出来
    const next = parseSseChunk(rest + ',"seq":1}\n\n')
    expect(next.frames).toHaveLength(1)
    expect(next.frames[0].data).toEqual({ id: 'm1', seq: 1 })
  })

  it('★ 一次给多帧要全部切出来（不能只取第一帧）', () => {
    const buffer =
      'event: phase\ndata: {"phase":"thinking","round":0}\n\n' +
      'event: message\ndata: {"id":"m1","seq":1,"role":"user"}\n\n' +
      'event: done\ndata: {"messageCount":1}\n\n'
    const { frames } = parseSseChunk(buffer)
    expect(frames.map((f) => f.event)).toEqual([TURN_PHASE, TURN_MESSAGE, TURN_DONE])
  })

  it('★ 注释行（心跳）不产生帧', () => {
    const { frames } = parseSseChunk(': keepalive\n\n')
    expect(frames).toHaveLength(0)
  })

  it('★ 坏 JSON 跳过而不是抛（抛出去会打断整条流）', () => {
    const { frames } = parseSseChunk('event: message\ndata: {坏掉的}\n\n')
    expect(frames).toHaveLength(0)
    // 关键：没有抛异常。抛了的话调用方那一轮就废了。
  })

  it('兼容 \\r\\n 换行（代理可能改写）', () => {
    const { frames } = parseSseChunk('event: phase\r\ndata: {"a":1}\r\n\r\n')
    expect(frames).toHaveLength(1)
    expect(frames[0].data).toEqual({ a: 1 })
  })

  it('中文不转义（帧里直接是原文）', () => {
    const { frames } = parseSseChunk('event: message\ndata: {"content":"正在读取端口"}\n\n')
    expect(frames[0].data.content).toBe('正在读取端口')
  })

  it('没有 event 行时默认按 message 处理（SSE 规范）', () => {
    const { frames } = parseSseChunk('data: {"id":"m1"}\n\n')
    expect(frames[0].event).toBe('message')
  })
})

describe('asChatMessage：收窄', () => {
  it('有 id 与 role 才算一条消息', () => {
    const msg = asChatMessage({ id: 'm1', seq: 1, role: 'user', content: '你好' })
    expect(msg).not.toBeNull()
    expect(msg?.id).toBe('m1')
  })

  it('缺 role 时返回 null（不能硬塞进消息列表）', () => {
    expect(asChatMessage({ id: 'm1', seq: 1 })).toBeNull()
  })

  it('缺 id 时返回 null', () => {
    expect(asChatMessage({ role: 'user', content: 'x' })).toBeNull()
  })

  it('多余字段原样保留（前向兼容：后端加字段前端不用改）', () => {
    const msg = asChatMessage({
      id: 'm1',
      seq: 1,
      role: 'user',
      content: 'x',
      brandNewField: { nested: true },
    })
    expect((msg as unknown as Record<string, unknown>).brandNewField).toEqual({ nested: true })
  })
})
