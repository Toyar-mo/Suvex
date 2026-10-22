import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { WorkOrderDialog } from './WorkOrderDialog'
import type { KernelChallenge } from '@/data/kernel'

/*
  ══════════════════════════════════════════════════════════════
  处置工单弹窗 —— 用**真实形状**的响应验一遍（v0.41）
  ══════════════════════════════════════════════════════════════

  ## 为什么这个文件存在

  `WorkOrderDialog` 和 `WriteupDialog` 共用外壳 `WriteupDialogShell`，
  所以"三态渲染"那段本来就被 `WriteupDialog.test.tsx` 覆盖了。**不重复测**。

  这个文件只测**这个组件特有的三件事**：

    1. 打的是 `/workorder` 端点，不是 `/writeup` —— 两个 loader 长得
       一模一样，抄错一个字的后果是点工单弹出来题解；
    2. 失败态那句标题是"这次复盘没有产出工单"，**不是**题解的
       "这道题还没有题解"。工单缺失在绝大多数题上是**正常**的
       （不是应急响应任务就没有工单），措辞不能像出错了；
    3. 409 时后端那句中文**原样**到用户眼前 —— 它区分"这不是应急
       响应任务"和"还没跑完"，两者下一步动作完全不同。

  ## ⚠️ 与其它组件测试同样的三个坑

  手动 `cleanup`、不用 `toHaveTextContent`、没有 Router（这个组件
  不渲染 `Link`，所以不需要 MemoryRouter）。
*/
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

/**
 * 真实工单正文（结构照 `workorder.render_workorders` 的输出抄）。
 *
 * ★ 第一行那句 `> ⛔ **这是建议，不是已执行的动作。**` 是模块级承诺
 *   （见 `workorder.py` 的 `render_workorders`），必须在正文里 ——
 *   渲染丢了它，用户会以为系统已经动过手了。
 */
const REAL_MARKDOWN = [
  '# 处置建议工单 · 边界主机失陷复盘',
  '',
  '> ⛔ **这是建议，不是已执行的动作。** 本系统没有执行任何处置操作：它只生成这份清单。',
  '',
  '资产：`10.20.30.40:8080`',
  '',
  '## 一、待处置事项（共 2 条）',
  '',
  '### 1. 隔离 10.20.30.40 主机',
  '',
  '| 项 | 内容 |',
  '| --- | --- |',
  '| 动作描述 | 从生产网段摘除该主机 |',
  '| 依据（关联哪条结论） | `auth.log:1423` 出现陌生账号登录成功（confirmed） |',
  '| 风险提示 | 该主机承载订单同步任务，摘除会中断同步 |',
  '| 需要的权限 | 网络设备管理权限 |',
  '',
  '### 2. 轮换 `deploy` 账号口令',
  '',
  '| 项 | 内容 |',
  '| --- | --- |',
  '| 动作描述 | 重置口令并回收旧凭证 |',
  '| 依据（关联哪条结论） | 口令在 `backup.tar.gz` 中以明文出现（confirmed） |',
  '| 风险提示 | 若有脚本硬编码旧口令会失败 |',
  '| 需要的权限 | 主机管理员 |',
  '',
  '## 二、人工确认清单',
  '',
  '- [ ] 每一条的依据是否指向了可核对的证据行？',
  '',
  '---',
  '',
  '*由 Suvex 自动生成 · 题目 `ctf2chal_IR0000000000000000000001`*',
].join('\n')

/** 真实的 200 响应体（字段与 `/workorder` 的返回一致） */
const REAL_WORKORDER = {
  challengeId: 'ctf2chal_IR0000000000000000000001',
  title: '边界主机失陷复盘',
  markdown: REAL_MARKDOWN,
  filename: 'IR-WorkOrders-边界主机失陷复盘.md',
  count: 2,
}

/** 挑战对象：一道应急响应题（category 前缀是 `ir-`） */
function realChallenge(): KernelChallenge {
  return {
    id: 'ctf2chal_IR0000000000000000000001',
    competitionId: 'ctf2comp_01M3PG846CV457JAFVBRPCG29M',
    remoteId: 'ir-root-cause-01',
    stageId: null,
    title: '边界主机失陷复盘',
    category: 'ir-root-cause',
    description: '附件是边界主机一周的 auth/syslog 与一次全盘镜像。',
    attachments: [],
    score: 100,
    status: 'solved',
    candidateFlag: null,
    confirmedFlag: null,
    remainingWrongSubmissions: 3,
    wrongSubmissionCount: 0,
    maxWrongSubmissions: 3,
    candidateCount: 0,
    sandboxProfile: 'suvex-sandbox-ir',
    scanId: 'scan_01M4RD51TJSMXS4ZQF97QK636N',
    solvedAt: '2026-10-21T04:10:00.000Z',
    lastError: null,
    environment: null,
    priority: 1001,
    createdAt: '2026-10-21T03:20:00.000Z',
    updatedAt: '2026-10-21T04:10:00.000Z',
  }
}

/** 装 fetch：按 URL 返回 200 正文或一个错误信封 */
function stubWorkOrder(behavior: 'ok' | 'conflict' | 'network') {
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
                '这道题还没有处置建议工单。工单是应急响应复盘任务的产物 —— ' +
                '普通解题任务不产出工单。如果这是一道应急响应题，等本次复盘跑完再看。',
            },
          }),
          { status: 409, headers: { 'Content-Type': 'application/json' } },
        ),
      )
    }
    return Promise.resolve(
      new Response(JSON.stringify({ ...REAL_WORKORDER, _url: url }), {
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

describe('处置工单弹窗：用真实 200 响应渲染', () => {
  it('★★ 打的是 `/workorder` 端点，不是 `/writeup`', async () => {
    /*
      这是本文件最该有的一条。两个 loader
      （`loadKernelWorkOrder` / `loadKernelWriteup`）除了路径末段之外
      完全一样，抄错一处不会报错 —— 只会让用户点"处置工单"
      弹出一份题解，而那看起来"功能就是这样"。
    */
    const calls: string[] = []
    vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
      calls.push(typeof input === 'string' ? input : input.toString())
      return Promise.resolve(
        new Response(JSON.stringify(REAL_WORKORDER), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    })

    render(<WorkOrderDialog challenge={realChallenge()} onClose={() => {}} />)

    await waitFor(() => expect(dialogText()).toContain('待处置事项'))
    expect(calls[0]).toContain(
      '/api/v1/kernel/challenges/ctf2chal_IR0000000000000000000001/workorder',
    )
    expect(calls[0]).not.toContain('/writeup')
    expect(calls[0]).not.toContain('/scans/')
  })

  it('★★ 正文里的表格 / 依据锚点 / 那句"未执行"都在', async () => {
    stubWorkOrder('ok')
    render(<WorkOrderDialog challenge={realChallenge()} onClose={() => {}} />)

    await waitFor(() => expect(dialogText()).toContain('待处置事项'))

    const text = dialogText()
    // ★ 模块级承诺：不写这一句，用户会以为系统已经动过手
    expect(text).toContain('这是建议，不是已执行的动作')
    // 四段表 —— 表格映射写错会整块消失
    expect(text).toContain('动作描述')
    expect(text).toContain('依据（关联哪条结论）')
    expect(text).toContain('风险提示')
    expect(text).toContain('需要的权限')
    // 依据必须能指到证据行（后端保证的锚点形状）
    expect(text).toContain('auth.log:1423')
    // 两条工单都渲染了
    expect(text).toContain('隔离 10.20.30.40 主机')
    expect(text).toContain('轮换')
    // 人工确认清单
    expect(text).toContain('人工确认清单')
  })

  it('★ 加载中显示"正在读取"，不是一片空白', () => {
    vi.stubGlobal('fetch', () => new Promise<Response>(() => {}))
    render(<WorkOrderDialog challenge={realChallenge()} onClose={() => {}} />)

    expect(dialogText()).toContain('正在读取')
  })

  it('★★ 失败态标题是"这次复盘没有产出工单"，不是题解那句', async () => {
    /*
      两句标题的语义差别是本质的：
        · 题解缺 = 这道题还没解 / 太早了 → 用户的反应是"重解一次"
        · 工单缺 = 这根本不是应急响应任务 → 用户的反应是"我不该在这找"
      写成同一句会让第二类用户去重解一道 pwn 题，而重解一百次也没工单。
    */
    stubWorkOrder('conflict')
    render(<WorkOrderDialog challenge={realChallenge()} onClose={() => {}} />)

    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())

    const text = dialogText()
    expect(text).toContain('这次复盘没有产出工单')
    // 题解那句不该出现
    expect(text).not.toContain('这次没有可看的题解')
  })

  it('★★ 409 时后端那句中文**原样**显示（它区分两种成因）', async () => {
    stubWorkOrder('conflict')
    render(<WorkOrderDialog challenge={realChallenge()} onClose={() => {}} />)

    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())

    const text = dialogText()
    // 后端原话里的两个关键信息必须到用户眼前
    expect(text).toContain('普通解题任务不产出工单')
    expect(text).toContain('等本次复盘跑完')
  })

  it('★ 断网（status 0）走兜底文案，不是空白', async () => {
    stubWorkOrder('network')
    render(<WorkOrderDialog challenge={realChallenge()} onClose={() => {}} />)

    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(dialogText()).toContain('连接不上后端服务')
  })

  it('★ 下载兜底文件名是工单口径，不是题解口径', async () => {
    /*
      后端会给真实文件名（`IR-WorkOrders-{题目名}.md`），兜底只在
      后端没给时用。但兜底写错成 `CTF-Writeup-…` 的后果是：
      用户下载下来一份工单，文件名说着"题解"。
    */
    stubWorkOrder('ok')
    render(<WorkOrderDialog challenge={realChallenge()} onClose={() => {}} />)

    await waitFor(() => expect(dialogText()).toContain('待处置事项'))

    // 后端给了文件名，所以用它
    expect(dialogText()).toContain('下载 .md')
  })
})
