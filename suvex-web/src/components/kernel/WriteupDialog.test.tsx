import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { WriteupDialog } from './WriteupDialog'
import type { KernelChallenge } from '@/data/kernel'

/*
  ══════════════════════════════════════════════════════════════
  CTF2 题解弹窗 —— 用**真实响应**验一遍（2026-10-15）
  ══════════════════════════════════════════════════════════════

  ## 为什么这个文件存在

  之前 `WriteupDialog` 只有"入口显不显示"的测试，**没有**验过
  "真拿到 200 之后正文能不能渲染出来"。那一段最容易出问题的地方是：

    · `markdown` 是一整篇文档（含表格、围栏代码块、行内代码），
      交给 `WriteupMarkdown` 渲染时任何一处映射写错都会白屏；
    · 200 的 payload 是 `solved=false`（未解出也产出题解）——
      弹窗不该因此显示成失败态。

  所以下面把 Kali 上那次真实解题（`[ACTF2020 新生赛]Upload`，
  78 轮 / 未解出 / 题解 5572 字符）的响应形状**原样**喂进来。
  `markdown` 用真实正文的前若干行（结构与真的一致）。

  ## ⚠️ 与其它组件测试同样的三个坑

  手动 `cleanup`、不用 `toHaveTextContent`、没有 Router（这个组件
  不渲染 `Link`，所以不需要 MemoryRouter）。
*/
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

/** 真实题解正文的节选（结构照抄 Kali 上那份 5572 字符的响应） */
const REAL_MARKDOWN = [
  '# CTF Writeup · [ACTF2020 新生赛]Upload',
  '',
  '> **Flag**：未确认（这道题没有解出）',
  '',
  '## 题目信息',
  '',
  '| 项 | 值 |',
  '| --- | --- |',
  '| 标题 | [ACTF2020 新生赛]Upload |',
  '| 分类 | WEB |',
  '| 平台题目 id | `f7742433-8c0b-4f26-84b9-375b880904b6` |',
  '| 结果 | 未解出 |',
  '| 轮数 | 78 |',
  '| 耗时 | 51 分 |',
  '',
  '### 题面',
  '',
  '感谢 Y1ng  师傅供题。',
  '',
  '## 结论',
  '',
  '**未能确认答案。** 本次没有产出候选 flag。',
  '',
  '## 关键步骤',
  '',
  "1. `curl -s -i 'http://4028cb31f53c8e3dd92d01a2.http-ctf2.dasctf.com:80/' | head -60`",
  '',
  '用到的工具：curl, http_request, python3',
  '',
  '## 过程纪要',
  '',
  '```',
  '[t16] http_request http://4028cb31f53c8e3dd92d01a2.http-ctf2.dasctf.com:80/upload/shell.php?c=id → HTTP 404',
  '```',
  '',
  '## 资源消耗',
  '',
  '- LLM 调用：78 次调用，共 **5,536,545** token',
  '',
  '---',
  '',
  '*由 Suvex 自动生成 · 题目 `ctf2chal_01M4QRAT254DE91N89338Z7P4M`*',
].join('\n')

/** 真实的 200 响应体（字段与 Kali 上那次实际响应一致） */
const REAL_WRITEUP = {
  challengeId: 'ctf2chal_01M4QRAT254DE91N89338Z7P4M',
  title: '[ACTF2020 新生赛]Upload',
  category: 'WEB',
  solved: false,
  markdown: REAL_MARKDOWN,
  filename: 'CTF-Writeup-ACTF2020_新生赛_Upload.md',
  attemptId: 'ctf2att_01M4QRATKWQ9X4V0GB4H2TQ0C1',
  turns: 78,
  elapsedSec: 3060,
  generatedAt: '2026-08-15T08:49:54.107Z',
  updatedAt: '2026-08-15T08:49:54.107Z',
}

/** 挑战对象：字段照真实 snapshot 抄（status=unsolved，未解出） */
function realChallenge(): KernelChallenge {
  return {
    id: 'ctf2chal_01M4QRAT254DE91N89338Z7P4M',
    competitionId: 'ctf2comp_01M3PG846CV457JAFVBRPCG29M',
    remoteId: 'f7742433-8c0b-4f26-84b9-375b880904b6',
    stageId: null,
    title: '[ACTF2020 新生赛]Upload',
    category: 'WEB',
    description: '感谢 Y1ng  师傅供题。',
    attachments: [],
    score: 1,
    status: 'unsolved',
    candidateFlag: null,
    confirmedFlag: null,
    remainingWrongSubmissions: 3,
    wrongSubmissionCount: 0,
    maxWrongSubmissions: 3,
    candidateCount: 0,
    sandboxProfile: 'ctf2-sandbox',
    scanId: 'scan_01M4RD51TJSMXS4ZQF97QK636N',
    solvedAt: null,
    lastError: '本次未产出候选 flag。',
    environment: null,
    priority: 1001,
    createdAt: '2026-08-12T09:50:34.566Z',
    updatedAt: '2026-08-15T08:49:54.107Z',
  }
}

/** 装 fetch：按 URL 返回 200 正文或一个错误信封 */
function stubWriteup(behavior: 'ok' | 'conflict' | 'network') {
  vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (behavior === 'network') return Promise.reject(new TypeError('Failed to fetch'))
    if (behavior === 'conflict') {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            error: {
              code: 'WRITEUP_UNAVAILABLE',
              message:
                '这道题的解题记录早于题解功能上线（v0.36）。重新解一次就会生成。',
            },
          }),
          { status: 409, headers: { 'Content-Type': 'application/json' } },
        ),
      )
    }
    return Promise.resolve(
      new Response(JSON.stringify({ ...REAL_WRITEUP, _url: url }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })
}

/** 弹窗正文（dialog 里的文字） */
function dialogText(): string {
  return screen.getByRole('dialog').textContent ?? ''
}

describe('CTF2 题解弹窗：用真实 200 响应渲染', () => {
  it('★ 打的是 CTF2 的题解端点，不是扫描那条', async () => {
    const calls: string[] = []
    vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
      calls.push(typeof input === 'string' ? input : input.toString())
      return Promise.resolve(
        new Response(JSON.stringify(REAL_WRITEUP), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    })

    render(<WriteupDialog challenge={realChallenge()} onClose={() => {}} />)

    await waitFor(() => expect(dialogText()).toContain('题目信息'))
    expect(calls[0]).toContain(
      '/api/v1/kernel/challenges/ctf2chal_01M4QRAT254DE91N89338Z7P4M/writeup',
    )
    expect(calls[0]).not.toContain('/scans/')
  })

  it('★★ 真实正文渲染出来（标题 / 表格 / 代码块 / 结论都在）', async () => {
    stubWriteup('ok')
    render(<WriteupDialog challenge={realChallenge()} onClose={() => {}} />)

    await waitFor(() => expect(dialogText()).toContain('题目信息'))

    const text = dialogText()
    // 头部
    expect(text).toContain('[ACTF2020 新生赛]Upload')
    // 题目信息表 —— 表格映射写错会整块消失
    expect(text).toContain('平台题目 id')
    expect(text).toContain('f7742433-8c0b-4f26-84b9-375b880904b6')
    expect(text).toContain('78')
    // 各段落标题
    expect(text).toContain('结论')
    expect(text).toContain('关键步骤')
    expect(text).toContain('过程纪要')
    expect(text).toContain('资源消耗')
    // 正文内容（不是只渲染了标题）
    expect(text).toContain('感谢 Y1ng')
    expect(text).toContain('未能确认答案')
    expect(text).toContain('HTTP 404')
    // token 统计那行
    expect(text).toContain('5,536,545')
  })

  it('★★ `solved=false` 的题解**不是**失败态（未解出也产出题解）', async () => {
    stubWriteup('ok')
    render(<WriteupDialog challenge={realChallenge()} onClose={() => {}} />)

    await waitFor(() => expect(dialogText()).toContain('题目信息'))

    // 失败态的标题不该出现
    expect(dialogText()).not.toContain('这道题还没有题解')
    // 也不该有 role="alert"
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('★ 加载中显示"正在读取题解…"（而不是一片空白）', () => {
    // 永不 resolve 的 fetch：停在 loading
    vi.stubGlobal('fetch', () => new Promise<Response>(() => {}))
    render(<WriteupDialog challenge={realChallenge()} onClose={() => {}} />)

    expect(dialogText()).toContain('正在读取题解')
  })

  it('★★ 409 时把后端那句中文**原样**显示（它区分两种成因）', async () => {
    stubWriteup('conflict')
    render(<WriteupDialog challenge={realChallenge()} onClose={() => {}} />)

    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())

    const text = dialogText()
    expect(text).toContain('这道题还没有题解')
    // 后端原话里的关键信息必须到用户眼前
    expect(text).toContain('v0.36')
    expect(text).toContain('重新解一次')
  })

  it('★ 断网（status 0）走兜底文案，不是空白', async () => {
    stubWriteup('network')
    render(<WriteupDialog challenge={realChallenge()} onClose={() => {}} />)

    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(dialogText()).toContain('连接不上后端服务')
  })
})
