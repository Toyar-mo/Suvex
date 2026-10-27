// ⚠️ 必须手动 import `cleanup`：本仓库没开 vitest 的 `globals: true`，
//    RTL 自动清理只在 globals 模式下注册。漏了它的症状是「Found multiple
//    elements」——上一个用例的 DOM 留在 document 里。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import SkillsPage from './SkillsPage'
import type { SkillDetail, SkillListSnapshot, SkillSummary } from '@/data/skills'

/*
  ══════════════════════════════════════════════════════════════
  SkillsPage：这一页的判据是「能不能看见模型真正读到的那些字」
  ══════════════════════════════════════════════════════════════

  技能页看起来是个浏览列表，但它承担的是**审计**职责：技能正文直接进
  模型上下文，模型照着它做事。一篇技能教了内核里不存在的命令，
  后果是模型调一个永远失败的工具 —— 而这个页面是唯一能看见它的地方。

  2026-10-26 之前这里有一个致命形状：只给自定义技能回正文。
  76 篇里 72 篇是内置的，于是**真正决定模型行为的那 72 篇在界面上是空白**，
  只留一句"内置 · 只读"。当时一篇内置技能教着 strix 遗留的
  `create_vulnerability_report`，页面上完全看不见，只能去 grep 源码才发现。

  所以这一组测试盯的是四件事：

  ① 内置技能**必须**能看到正文（只读 ≠ 不可知）；
  ② 正文按需拉、切走要清空（否则会显示上一篇的内容，误读成这篇的）；
  ③ 慢请求回来不能覆盖新选中的那篇（这是最容易漏的竞态）；
  ④ 内置技能的写操作**仍然**被挡住（别为了能看把能改也放开了）。

  这些断言如果只写成"页面渲染出来了"，上面四种失效全部测不出来。
*/

const { fetchSkill, fetchSkillFiles, mockedState } = vi.hoisted(() => ({
  fetchSkill: vi.fn(),
  fetchSkillFiles: vi.fn(),
  // `useSkills()` 返回的快照，每个用例自己改（模块级单例，必须在
  // `vi.hoisted` 里建，否则 `vi.mock` 提升到 import 之前时它是 undefined）
  mockedState: {
    list: { total: 0, categories: [] } as SkillListSnapshot,
  },
}))

vi.mock('@/data/skills', async () => {
  const actual = await vi.importActual<typeof import('@/data/skills')>('@/data/skills')
  return {
    ...actual,
    useSkills: () => ({
      list: mockedState.list,
      usage: { sampledEvents: 0, since: null, items: [] },
      status: 'ready' as const,
      error: null,
      usageTruncated: false,
    }),
    // 这几个是页面用到的纯函数，桩成直通/空实现，避免把 store 的
    // 加载逻辑带进这组测试（它们有各自的测试）。
    // ⚠️ `filterSkillGroups(categories, query)` 的**第一个参数是
    //    `list.categories`（数组）**，不是整个 snapshot —— 一开始我按
    //    `(snapshot)` 桩，页面拿到非数组就 `flatMap is not a function`。
    usageSummary: () => ({}),
    loadsOf: () => null,
    filterSkillGroups: (groups: SkillListSnapshot['categories']) => groups,
    ensureSkillsLoaded: () => undefined,
    loadSkills: () => Promise.resolve(),
    fetchSkill: (name: string) => fetchSkill(name),
    fetchSkillFiles: (name: string) => fetchSkillFiles(name),
    createSkill: vi.fn(),
    updateSkill: vi.fn(),
    deleteSkill: vi.fn(),
    validateDraft: () => null,
  }
})

vi.mock('@/data/auth-store', () => ({
  useAuth: () => ({
    identity: { id: 'u1', name: 'admin', role: 'admin' },
    isAdmin: true,
    ready: true,
  }),
}))

function builtin(name: string, patch: Partial<SkillSummary> = {}): SkillSummary {
  return {
    name,
    category: 'analysis',
    description: `${name} 的描述`,
    source: 'builtin',
    fileCount: 0,
    ...patch,
  }
}

function detailOf(name: string, body: string): SkillDetail {
  return {
    name,
    category: 'analysis',
    description: `${name} 的描述`,
    source: 'builtin',
    body,
    lineCount: body.split('\n').length,
    files: [],
  }
}

function renderPage() {
  return render(
    <MemoryRouter>
      <SkillsPage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  mockedState.list = {
    total: 2,
    categories: [
      { category: 'analysis', skills: [builtin('counterevidence'), builtin('oauth')] },
    ],
  }
  fetchSkill.mockReset()
  fetchSkillFiles.mockReset()
  // 内置技能的附件端点回 404（不可编辑），页面必须容得下这个失败
  fetchSkillFiles.mockRejectedValue(new Error('内置技能没有可编辑附件'))
})

afterEach(() => {
  cleanup()
})

describe('内置技能能看见正文（只读 ≠ 不可知）', () => {
  it('★ 默认选中第一篇时就把正文拉下来并显示', async () => {
    fetchSkill.mockResolvedValue(
      detailOf('counterevidence', '# 反证纪律\n\n证明一个 bug 是真的才算数。'),
    )

    renderPage()

    await waitFor(() => {
      expect(screen.getByText(/反证纪律/)).toBeTruthy()
    })
    // 断言的是**正文文字**进了 DOM，不是"某个容器存在" ——
    // 后者在正文为空串时也能通过，而那正是被修掉的那个 bug 的形状。
    expect(fetchSkill).toHaveBeenCalledWith('counterevidence')
  })

  it('★ 点另一篇会拉那一篇的正文（不是复用上一篇）', async () => {
    fetchSkill.mockImplementation((name: string) =>
      Promise.resolve(detailOf(name, name === 'counterevidence' ? '# 反证纪律' : '# OAuth 流程')),
    )

    renderPage()
    await waitFor(() => expect(screen.getByText(/反证纪律/)).toBeTruthy())

    fireEvent.click(screen.getByText('oauth'))

    await waitFor(() => expect(screen.getByText(/OAuth 流程/)).toBeTruthy())
    expect(fetchSkill).toHaveBeenCalledWith('oauth')
    expect(screen.queryByText(/反证纪律/)).toBeNull()
  })

  it('切换技能时立刻清空正文，不显示上一篇的内容', async () => {
    /* 第二篇的请求**永远不回来**。这时界面上若还留着第一篇的正文，
       用户会以为那就是 oauth 的内容 —— 这是最容易被信任的错误。 */
    fetchSkill.mockImplementation((name: string) => {
      if (name === 'counterevidence') {
        return Promise.resolve(detailOf('counterevidence', '# 反证纪律'))
      }
      return new Promise<SkillDetail>(() => {})
    })

    renderPage()
    await waitFor(() => expect(screen.getByText(/反证纪律/)).toBeTruthy())

    fireEvent.click(screen.getByText('oauth'))

    await waitFor(() => expect(screen.queryByText(/反证纪律/)).toBeNull())
  })

  it('慢请求回来不覆盖新选中的那篇（竞态）', async () => {
    /*
      counterevidence 先发、后回；oauth 后发、先回。
      若不校验 name，用户最后看到的是 counterevidence 的正文，
      而左侧高亮的是 oauth —— 界面自相矛盾，且没有任何报错。
    */
    /*
      用对象属性而不是裸变量存回调：`let resolveSlow = null` 然后只在闭包里赋值，
      会被 TS 的控制流分析窄化成 `null`，调用处直接报 "Type 'never' has no call
      signatures"（`tsc -b` 会红，而 `tsc --noEmit` 因为走 project references 看不出来）。
    */
    const pending: { resolve?: (d: SkillDetail) => void } = {}
    fetchSkill.mockImplementation((name: string) => {
      if (name === 'counterevidence') {
        return new Promise<SkillDetail>((resolve) => {
          pending.resolve = resolve
        })
      }
      return Promise.resolve(detailOf('oauth', '# OAuth 流程'))
    })

    renderPage()
    fireEvent.click(screen.getByText('oauth'))
    await waitFor(() => expect(screen.getByText(/OAuth 流程/)).toBeTruthy())

    // 现在把那个慢的放回来 —— 它属于已经切走的那一篇
    pending.resolve?.(detailOf('counterevidence', '# 反证纪律'))
    await new Promise((r) => setTimeout(r, 10))

    expect(screen.getByText(/OAuth 流程/)).toBeTruthy()
    expect(screen.queryByText(/反证纪律/)).toBeNull()
  })

  it('读不到正文时说实话，不显示成空文件', async () => {
    fetchSkill.mockRejectedValue(new Error('404 找不到'))

    renderPage()

    await waitFor(() => expect(screen.getByText('404 找不到')).toBeTruthy())
  })
})

describe('内置技能仍然不可写', () => {
  it('★ 内置技能不画「编辑」/「删除」按钮', () => {
    fetchSkill.mockResolvedValue(detailOf('counterevidence', '# 反证纪律'))
    renderPage()

    expect(screen.queryByText('编辑')).toBeNull()
    expect(screen.queryByText('删除')).toBeNull()
    expect(screen.getByText('内置 · 只读')).toBeTruthy()
  })

  it('★ 但画「复制为自定义」—— 那是一个真能做成的动作', async () => {
    fetchSkill.mockResolvedValue(detailOf('counterevidence', '# 反证纪律\n\n正文内容'))
    renderPage()

    await waitFor(() => expect(screen.getByText('复制为自定义')).toBeTruthy())
  })
})

/*
  正文的两种看法（2026-10-26 加）。

  背景：用户反馈「文字太紧凑了吧，右边还空着一大块」。根因是技能 .md
  按 72 列硬换行，`whitespace-pre-wrap` 原样保留 → 12px 等宽下每行只占
  三分之一宽度。修法是加一个「排版」视图走 Markdown 渲染，段落自然重排。

  ⚠️ 这一组守的不是"按钮画出来了"，而是**两个视图看到的东西不一样** ——
     如果哪天有人把两个分支写成同一段 JSX，按钮还在，但切换毫无效果，
     这里的断言必须变红。
*/
describe('正文可切换「排版 / 原文」', () => {
  const BODY = '# 反证纪律\n\n证明一个 bug 是真的才算数。\n\n- 第一点\n- 第二点'

  it('默认是「排版」：段落被重排，不再是整块 pre', async () => {
    fetchSkill.mockResolvedValue(detailOf('counterevidence', BODY))
    renderPage()

    await waitFor(() => expect(screen.getByText(/反证纪律/)).toBeTruthy())

    // 排版视图：`# 反证纪律` 应该被吃掉井号变成标题，
    // 所以**页面上不该再出现带 `# ` 前缀的那串字**。
    expect(screen.queryByText(/# 反证纪律/)).toBeNull()
    expect(screen.getByRole('tab', { name: /排版/ }).getAttribute('aria-selected')).toBe('true')
    expect(screen.getByRole('tab', { name: /原文/ }).getAttribute('aria-selected')).toBe('false')
  })

  it('★ 切到「原文」能看见逐字符保真的 markdown（井号还在）', async () => {
    fetchSkill.mockResolvedValue(detailOf('counterevidence', BODY))
    renderPage()

    await waitFor(() => expect(screen.getByText(/反证纪律/)).toBeTruthy())

    fireEvent.click(screen.getByRole('tab', { name: /原文/ }))

    // 原文视图里 `# ` 前缀必须**原样在**——这正是它存在的理由：
    // 核对"作者到底怎么写的"，而不是看渲染结果。
    await waitFor(() => expect(screen.getByText(/# 反证纪律/)).toBeTruthy())
    expect(screen.getByRole('tab', { name: /原文/ }).getAttribute('aria-selected')).toBe('true')
  })

  it('★ 原文视图提示这里才看得到 HTML 注释与原始缩进', async () => {
    fetchSkill.mockResolvedValue(detailOf('counterevidence', BODY))
    renderPage()

    await waitFor(() => expect(screen.getByText(/反证纪律/)).toBeTruthy())
    // 排版视图下不显示这句（否则它会成为一句无意义的常驻噪声）
    expect(screen.queryByText(/这里才看得到 HTML 注释/)).toBeNull()

    fireEvent.click(screen.getByRole('tab', { name: /原文/ }))
    await waitFor(() =>
      expect(screen.getByText(/这里才看得到 HTML 注释/)).toBeTruthy(),
    )
  })

  it('切到「原文」后换一篇，仍停在「原文」（视图是偏好，不是一次性动作）', async () => {
    fetchSkill.mockImplementation((name: string) =>
      Promise.resolve(detailOf(name, name === 'counterevidence' ? '# 反证纪律' : '# OAuth 流程')),
    )
    renderPage()

    await waitFor(() => expect(screen.getByText(/反证纪律/)).toBeTruthy())
    fireEvent.click(screen.getByRole('tab', { name: /原文/ }))
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: /原文/ }).getAttribute('aria-selected')).toBe('true'),
    )

    fireEvent.click(screen.getByText('oauth'))
    await waitFor(() => expect(screen.getByText(/# OAuth 流程/)).toBeTruthy())

    expect(screen.getByRole('tab', { name: /原文/ }).getAttribute('aria-selected')).toBe('true')
  })
})
