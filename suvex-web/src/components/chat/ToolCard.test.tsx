import { describe, expect, it } from 'vitest'

import { digestToolArgs } from './toolCardDigest'

/*
  digestToolArgs：折叠态那行工具参数摘要（v1.30）

  ## ★ 为什么值得测

  老实现把命令硬切 80 字符、**什么都不说**，用户看到一条断在中间的
  命令（`export KEY=… ; H=https://pro.dasctf.com; c`），第一反应是
  「命令写了一半 / 卡住了」，去点开看"到底哪里错了"——可后端数据完好。

  新实现分两件事：
  1. 视觉省略交给 CSS（渲染处的 `truncate`）；
  2. **"还有内容"这件事必须说出来**：超一行或超 80 字符时，末尾附
     `…（共 N 字符）`，N 是首行之外也算在内的**总长度**。

  这里钉的是判据：短单行原样返回（不加噪声）；长/多行才加提示，且 N 是总长。
*/

describe('digestToolArgs：折叠态参数摘要（v1.30）', () => {
  it('短单行命令原样返回，不加任何提示', () => {
    expect(digestToolArgs({ command: 'pwd' })).toBe('pwd')
    expect(digestToolArgs({ command: 'ls -la /work' })).toBe('ls -la /work')
  })

  it('恰好 80 字符的单行不加提示（边界内）', () => {
    const cmd = 'a'.repeat(80)
    expect(digestToolArgs({ command: cmd })).toBe(cmd)
  })

  it('★ 超过 80 字符的单行：首行 + …（共 N 字符），N 是总长', () => {
    const cmd = 'x'.repeat(200)
    const out = digestToolArgs({ command: cmd })
    expect(out).toBe(`${cmd}…（共 200 字符）`)
    expect(out).toContain('…（共 200 字符）')
  })

  it('★ 多行脚本即使首行很短也加提示，N 是整段命令的总长（不是首行）', () => {
    const cmd = ['curl -C - -o misc.zip URL', 'echo done', 'unzip -l misc.zip'].join('\n')
    const out = digestToolArgs({ command: cmd })
    // 摘要只露首行，但要说清整段有多长 —— 这正是"点开看"的理由。
    expect(out.startsWith('curl -C - -o misc.zip URL')).toBe(true)
    expect(out).toContain(`…（共 ${cmd.length} 字符）`)
    expect(cmd.length).toBeGreaterThan('curl -C - -o misc.zip URL'.length)
  })

  it('首行内部连续空白折成一个空格（制表符不撑开行）', () => {
    expect(digestToolArgs({ command: 'echo\t\t  hi' })).toBe('echo hi')
  })

  it('按白名单顺序找第一个非空字符串键（command 优先于 url）', () => {
    expect(digestToolArgs({ url: 'https://x', command: 'pwd' })).toBe('pwd')
  })

  it('没有可用键 = 空串（不崩、不伪造）', () => {
    expect(digestToolArgs(null)).toBe('')
    expect(digestToolArgs({})).toBe('')
    expect(digestToolArgs({ command: '   ' })).toBe('')
    expect(digestToolArgs({ unrelated: 'x' })).toBe('')
  })
})
