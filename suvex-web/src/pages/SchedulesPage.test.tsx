// ⚠️ 必须手动 import `cleanup`：本仓库没开 vitest 的 `globals: true`，
//    RTL 自动清理只在 globals 模式下注册。漏了它的症状是「Found multiple
//    elements」——上一个用例的 DOM 留在 document 里。
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import SchedulesPage from './SchedulesPage'
import { resetSchedules } from '@/data/schedules-store'
import type { Schedule } from '@/data/schedule'

/*
  ══════════════════════════════════════════════════════════════
  SchedulesPage：这一页的判据是「能不能把已经失效的计划顶上来」
  ══════════════════════════════════════════════════════════════

  定时计划这类功能**失效起来是无声的**：
    · 连续失败的计划有下次执行时间、有历史记录，看起来一切正常，
      但它每次到点都没建出扫描来 —— 它并没有在帮你复查；
    · 停用三个月前随手关掉的计划，用户早忘了自己关过；
    · 调度器挂了时，所有计划的「下次执行」都停在过去，而列表完全正常。

  所以测试集中在三件事：
  ① 连续失败 / 数据异常必须排在「正常」前面（不是按名字、不是按创建时间）；
  ② 「已到点未派发」必须显式提示 —— 那是调度器停摆的唯一界面信号；
  ③ 停用不能排到前面占视线，但也不能被隐藏。

  这些断言如果只写成"表格渲染出来了"，上面三种失效全部测不出来。
*/

// provider 列表直接桩掉：这一页不关心 provider 怎么来的，
// 但 `defaultProviderId` 决定新建按钮能不能点，必须有可用值。
// （`vi.mock` 会被 vitest 提升到 import 之前，所以放在 import 之后写不影响。）
vi.mock('@/data/providers', () => ({
  useProviders: () => [
    { id: 'prov_default', label: 'DeepSeek', model: 'm', kind: 'remote', configured: true, missing: null, note: '' },
  ],
  defaultProviderId: () => 'prov_default',
}))

function makeSchedule(id: string, patch: Partial<Schedule> = {}): Schedule {
  const now = Date.now()
  return {
    id,
    name: id,
    target: `${id}.example`,
    targetKind: 'url',
    goal: 'findVulns',
    intervalMinutes: 1440,
    enabled: true,
    nextRunAt: new Date(now + 3_600_000).toISOString(),
    lastScanId: null,
    lastRunAt: new Date(now - 3_600_000).toISOString(),
    failureCount: 0,
    lastFailureReason: null,
    providerId: 'prov_default',
    providerLabel: 'DeepSeek',
    skills: [],
    brief: null,
    createdAt: new Date(now - 86_400_000).toISOString(),
    updatedAt: new Date(now - 86_400_000).toISOString(),
    ...patch,
  }
}

let response: { items: Schedule[]; total: number; dueCount: number } | null = null
let failWith: unknown = null

beforeEach(() => {
  resetSchedules()
  response = null
  failWith = null
  vi.stubGlobal('fetch', () => {
    if (failWith) return Promise.reject(failWith)
    return Promise.resolve(
      new Response(JSON.stringify(response), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  resetSchedules()
})

function renderPage() {
  return render(
    <MemoryRouter>
      <SchedulesPage />
    </MemoryRouter>,
  )
}

/** 表格里所有数据行的名称，按渲染顺序（第 2 列是「名称 / 目标」）。 */
function rowNames(): string[] {
  const table = screen.getByRole('table')
  const rows = within(table).getAllByRole('row').slice(1) // 去掉表头
  return rows.map((row) => within(row).getAllByRole('cell')[1].textContent ?? '')
}

describe('SchedulesPage', () => {
  it('把「连续失败」排在「正常」前面 —— 而不是按名字或创建时间', async () => {
    response = {
      items: [
        // 名字故意让正常的那条排前面：如果实现是字母序，这条会赢
        makeSchedule('a-normal', { name: 'a-normal' }),
        makeSchedule('z-failing', {
          name: 'z-failing',
          failureCount: 4,
          lastFailureReason: '目标连不上',
        }),
      ],
      total: 2,
      dueCount: 0,
    }
    renderPage()

    await screen.findByRole('table')

    // ★ 这是这一页存在的理由：失败的计划看起来"还在跑"，
    //   不把它顶上来，用户永远不会发现它其实没在工作。
    expect(rowNames()[0]).toContain('z-failing')
  })

  it('「数据异常」（启用却没有下次时间）排最前，且明说它是异常', async () => {
    response = {
      items: [
        makeSchedule('a-ok', { name: 'a-ok' }),
        makeSchedule('z-broken', { name: 'z-broken', enabled: true, nextRunAt: null }),
      ],
      total: 2,
      dueCount: 0,
    }
    renderPage()

    await screen.findByRole('table')

    expect(rowNames()[0]).toContain('z-broken')
    // 不能显示成「已停用」—— 那会让用户以为是自己关的。
    // 注意要限定在表格内：工具条里有个同名的筛选按钮「已停用」，
    // 全页查会命中那个按钮，测出来的东西跟状态列没关系。
    const table = screen.getByRole('table')
    expect(within(table).getAllByText('数据异常').length).toBeGreaterThan(0)
    expect(within(table).queryByText('已停用')).toBeNull()
  })

  it('已到点时给出「调度器可能没在运行」的显式提示', async () => {
    response = {
      items: [
        makeSchedule('a-due', {
          name: 'a-due',
          nextRunAt: new Date(Date.now() - 3_600_000).toISOString(),
        }),
      ],
      total: 1,
      dueCount: 1,
    }
    renderPage()

    await screen.findByRole('table')

    // ★ 这是调度器停摆的唯一界面信号。只把状态列画成"已到点"不够 ——
    //   用户不知道那是正常的等待还是系统坏了。
    const alert = screen.getByRole('status')
    expect(alert.textContent).toContain('调度器')
    expect(alert.textContent).toContain('1')
  })

  it('停用排在正常之后 —— 但不能被隐藏', async () => {
    response = {
      items: [
        makeSchedule('a-paused', { name: 'a-paused', enabled: false, nextRunAt: null }),
        makeSchedule('z-ok', { name: 'z-ok' }),
      ],
      total: 2,
      dueCount: 0,
    }
    renderPage()

    await screen.findByRole('table')

    expect(rowNames()[0]).toContain('z-ok')
    // 必须仍然在列表里：三个月前随手关掉的那条，藏起来就永远想不起来
    expect(screen.getByText('a-paused')).toBeTruthy()
  })

  it('连续失败必须同时给出次数和最近一次的原因', async () => {
    response = {
      items: [
        makeSchedule('a-failing', {
          name: 'a-failing',
          failureCount: 7,
          lastFailureReason: '没有可用的模型服务',
        }),
      ],
      total: 1,
      dueCount: 0,
    }
    renderPage()

    await screen.findByRole('table')

    expect(screen.getByText(/连续 7 次失败/)).toBeTruthy()
    expect(screen.getByText('没有可用的模型服务')).toBeTruthy()
  })

  it('空库时给的是"还没有定时计划"，不装一个空表格', async () => {
    response = { items: [], total: 0, dueCount: 0 }
    renderPage()

    await screen.findByText('还没有定时计划')

    // 空表格会让用户以为是加载失败/权限问题
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('加载失败时给 ErrorState 与可用的重试，不是一片白', async () => {
    failWith = new Error('boom')
    renderPage()

    const retry = await screen.findByRole('button', { name: /重试/ })
    expect(retry).toBeTruthy()
  })

  it('搜索框按名称或目标过滤', async () => {
    response = {
      items: [
        makeSchedule('a-api', { name: 'a-api', target: 'api.acme.internal' }),
        makeSchedule('z-web', { name: 'z-web', target: 'www.other.example' }),
      ],
      total: 2,
      dueCount: 0,
    }
    renderPage()

    await screen.findByRole('table')

    fireEvent.change(screen.getByLabelText('按名称或目标筛选定时计划'), {
      target: { value: 'acme' },
    })

    await waitFor(() => {
      expect(rowNames()).toHaveLength(1)
    })
    expect(rowNames()[0]).toContain('a-api')
  })

  it('状态筛选只留下那一档', async () => {
    response = {
      items: [
        makeSchedule('a-ok', { name: 'a-ok' }),
        makeSchedule('z-failing', { name: 'z-failing', failureCount: 2 }),
      ],
      total: 2,
      dueCount: 0,
    }
    renderPage()

    await screen.findByRole('table')

    fireEvent.click(screen.getByRole('button', { name: '连续失败' }))

    await waitFor(() => {
      expect(rowNames()).toHaveLength(1)
    })
    expect(rowNames()[0]).toContain('z-failing')
  })

  it('点「停用」调的是 /enabled 端点，而不是 PATCH', async () => {
    response = { items: [makeSchedule('a-ok', { name: 'a-ok' })], total: 1, dueCount: 0 }
    renderPage()

    await screen.findByRole('table')

    /*
      按方法路由的桩：GET 回列表，POST 回"被改过的那一条"。
      如果这里让 POST 也回列表体，`upsert` 收到的对象没有 `id`，
      会往快照里塞一条 id=undefined 的记录 —— React 的 key 警告就会响，
      而那是**测试桩的错**，不是页面的错。桩要如实反映真实契约。
    */
    const calls: string[] = []
    vi.stubGlobal('fetch', (input: RequestInfo, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      calls.push(`${method} ${String(input)}`)
      const body = method === 'GET' ? response : makeSchedule('a-ok', { name: 'a-ok', enabled: false, nextRunAt: null })
      return Promise.resolve(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    })

    fireEvent.click(screen.getByRole('button', { name: '停用' }))

    await waitFor(() => {
      expect(calls.some((c) => c.includes('/enabled'))).toBe(true)
    })
    // ★ 启用时服务端要重算 nextRunAt（保住相位），PATCH 表达不了
    expect(calls.some((c) => c.startsWith('PATCH'))).toBe(false)
    // 停用后状态列必须真的变成「已停用」，不能只发请求不改界面
    await waitFor(() => {
      const table = screen.getByRole('table')
      expect(within(table).getByText('已停用')).toBeTruthy()
    })
  })

  it('删除要二次确认，确认前不发请求', async () => {
    response = { items: [makeSchedule('a-ok', { name: 'a-ok' })], total: 1, dueCount: 0 }
    renderPage()

    await screen.findByRole('table')

    const calls: string[] = []
    vi.stubGlobal('fetch', (input: RequestInfo, init?: RequestInit) => {
      calls.push(`${init?.method ?? 'GET'} ${String(input)}`)
      return Promise.resolve(
        new Response(JSON.stringify(response), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    })

    // 打开确认弹窗
    fireEvent.click(screen.getByTitle('删除这条计划'))
    await screen.findByText('删除这条定时计划？')

    // 还没确认 —— 一条请求都不该发出去
    expect(calls).toHaveLength(0)

    // 确认之后才真的删
    fireEvent.click(screen.getByRole('button', { name: '删除' }))
    await waitFor(() => {
      expect(calls.some((c) => c.startsWith('DELETE'))).toBe(true)
    })
  })
})

describe('新建定时计划 — 目标形态的取舍', () => {
  /*
    「题目附件」在定时计划里**立不起来**，所以选项里不该有它。

    这条链路的两个断点（都不是猜测，是实测过的）：
      · 附件的合法 target 是空串，而后端 `ScheduleCreateIn.target` 是
        `min_length=1` —— 空串 422；
      · 随便填个非空值能过 schema，但 `targets.normalize_and_validate`
        对任何非空输入都回「这类题目不需要目标地址」—— 400。

    也就是说：用户在界面上看到这个选项、选中它、填完整个表单、点创建，
    得到的必定是一条报错。**更好的做法是它一开始就不出现。**

    这个断言看着很薄，但它锁住的正是"界面允许了一个后端做不到的动作"
    这个形状 —— 之前 `TARGET_KIND_LABEL` 是全量遍历的，加一个形态就自动
    掉进这个坑里，没有任何测试会红。
  */
  it('目标形态里没有「题目附件」，其余四种都在', async () => {
    response = { items: [], total: 0, dueCount: 0 }
    renderPage()

    await screen.findByText('还没有定时计划')
    fireEvent.click(screen.getAllByRole('button', { name: '新建计划' })[0])

    const dialog = await screen.findByRole('dialog')

    expect(within(dialog).queryByRole('button', { name: '题目附件' })).toBeNull()
    for (const label of ['已部署的应用', 'Git 仓库', '本地代码路径', 'TCP 服务']) {
      expect(within(dialog).getByRole('button', { name: label })).toBeTruthy()
    }
  })

  it('是居中弹窗（role=dialog）而不是右侧抽屉', async () => {
    response = { items: [], total: 0, dueCount: 0 }
    renderPage()

    await screen.findByText('还没有定时计划')
    fireEvent.click(screen.getAllByRole('button', { name: '新建计划' })[0])

    // Modal 带 aria-modal；Drawer 没有这个属性（它是并存式浮层，不是遮断式）。
    // 这条断言把"用哪个浮层"固定下来 —— 用错不会报错，只会让表单又变回竖长条。
    const dialog = await screen.findByRole('dialog')
    expect(dialog.getAttribute('aria-modal')).toBe('true')
  })

  it('没填目标时「创建」不可点，并给出原因', async () => {
    response = { items: [], total: 0, dueCount: 0 }
    renderPage()

    await screen.findByText('还没有定时计划')
    fireEvent.click(screen.getAllByRole('button', { name: '新建计划' })[0])

    const dialog = await screen.findByRole('dialog')
    const create = within(dialog).getByRole('button', { name: '创建' })
    expect((create as HTMLButtonElement).disabled).toBe(true)
    expect(dialog.textContent).toContain('请先填写要定期复查的目标')
  })

  it('填了目标之后「创建」变可点 —— 且不发请求直到真的点它', async () => {
    response = { items: [], total: 0, dueCount: 0 }
    const calls: string[] = []
    vi.stubGlobal('fetch', (input: RequestInfo, init?: RequestInit) => {
      calls.push(`${init?.method ?? 'GET'} ${String(input)}`)
      return Promise.resolve(
        new Response(JSON.stringify(response), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    })

    renderPage()
    await screen.findByText('还没有定时计划')
    fireEvent.click(screen.getAllByRole('button', { name: '新建计划' })[0])

    const dialog = await screen.findByRole('dialog')
    const targetInput = within(dialog).getAllByRole('textbox')[1]
    fireEvent.change(targetInput, { target: { value: 'api.acme.internal' } })

    const create = within(dialog).getByRole('button', { name: '创建' })
    await waitFor(() => {
      expect((create as HTMLButtonElement).disabled).toBe(false)
    })
    // 只是"能点了"，还没点 —— 编辑表单不该有任何副作用
    expect(calls.every((c) => c.startsWith('GET'))).toBe(true)
  })
})
