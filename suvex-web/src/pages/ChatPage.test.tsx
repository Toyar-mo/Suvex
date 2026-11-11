// ⚠️ 必须手动 import `cleanup`：本仓库没开 vitest 的 `globals: true`，
//    RTL 自动清理只在 globals 模式下注册。漏了它的症状是「Found multiple
//    elements」——上一个用例的 DOM 留在 document 里。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ensureToolsLoaded } from '@/data/tools'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/*
  ══════════════════════════════════════════════════════════════
  ChatPage：判据是「用户会不会被这条界面骗到」
  ══════════════════════════════════════════════════════════════

  对话页的风险与表格页不同：它**看起来**永远在工作。空列表可以画成
  "还没有对话"，失败也可以画成"还没有对话"；工具失败可以画得和成功
  一模一样；只读账号可以看起来和大管理员一样能建扫描。

  所以这里不测"渲染出来了"，只测四种会骗人的情况：
  ① 工具**失败**必须看得出来（否则用户去扫描页找一个不存在的任务）；
  ② 只读角色必须看到"你不能建扫描"，而不是点了才被后端拒绝；
  ③ 拿不到会话列表时给出重试路径，而不是伪装成空列表；
  ④ 中文输入法敲拼音按 Enter 不能把半截拼音发出去。
*/

// ── 桩：工具 store（技能 / MCP 挂载下拉的选项来源） ──
//
// ★ v1.18：对话页里有两个同名的 `loadTools`。`@/data/chat` 那个拉的是
//   `/chat/tools`（说明文本），而 `@/data/tools` 的 `ensureToolsLoaded()`
//   才是技能下拉的选项来源。这个 store 以前从来没被调用过，
//   于是技能下拉永远显示"技能库是空的"。这里把它换成替身，就能直接
//   断言"进页面有没有去加载"——这是那条缺陷唯一可靠的判据。
vi.mock('@/data/tools', () => ({
  ensureToolsLoaded: vi.fn(),
  useTools: vi.fn(() => ({
    mcp: { servers: [{ name: 'fake-server' }, { name: 'team-mcp' }] },
    catalog: { tools: [] },
    skills: { total: 0, categories: [] },
    recipes: { total: 0, recipes: [] },
    status: 'ready',
    error: null,
  })),
}))

// ── 桩：auth ───────────────────────────────────────────────
vi.mock('@/data/auth-store', () => ({
  useAuth: vi.fn(),
}))

// ── 桩：chat store（页面只是它的投影，真实逻辑在 chat.test.ts 里测） ──
vi.mock('@/data/chat', () => ({
  useChat: vi.fn(),
  loadSessions: vi.fn(async () => {}),
  loadTools: vi.fn(async () => {}),
  openSession: vi.fn(async () => {}),
  createSession: vi.fn(async () => null),
  sendMessage: vi.fn(async () => false),
  renameSession: vi.fn(async () => true),
  archiveSession: vi.fn(async () => true),
  // ★ v0.94 新增的 store 函数必须在这里逐个列出。
  //
  //   ESM 命名导入在 vitest 的 mock 里是**逐名解析**的：工厂少导出一个，
  //   页面那一行 `import` 不会在加载时报错，而是在**真正调用时**炸
  //   `No "xxx" export is defined on the "@/data/chat" mock`。
  //   症状是整组用例一起红，看起来像页面崩了，其实只是桩漏了一项 ——
  //   我为此白查了十几分钟。以后给 store 加导出，回来补一次。
  loadPersonas: vi.fn(async () => {}),
  updateSettings: vi.fn(async () => true),
  uploadAttachment: vi.fn(async () => null),
  dropStagedAttachment: vi.fn(async () => true),
  decideApproval: vi.fn(async () => true),
  // ★ v0.100：插话。跑着的时候 Enter 走这条路径（见 `handleSend`）。
  clearStaleSending: vi.fn(),
  interject: vi.fn(async () => 'ok'),
  interruptTurn: vi.fn(async () => true),
  // v1.15 会话生命周期：同样必须逐个列出，漏一个就在调用时炸。
  pinSession: vi.fn(async () => true),
  unarchiveSession: vi.fn(async () => true),
  purgeSession: vi.fn(async () => true),
  resetChatStore: vi.fn(),
  subscribeChat: vi.fn(() => () => {}),
  getChatSnapshot: vi.fn(
    () => ({ sending: false, sendingSessionIds: [], serverTurn: null }) as never,
  ),
  // ★ v1.19：中断轮次的账目与「继续」。同样必须逐个列出 —— 漏一项就在
  //   页面渲染时炸 `No "listTurns" export is defined`，整组用例一起红。
  listTurns: vi.fn(async () => []),
  resumeTurn: vi.fn(async () => []),
  // ★ v1.61：轮询入口与消息刷新。工厂少导出 → 调用时炸 No "xxx" export，
  //   所以新增 store 函数一律在这里补。
  pollTurnProgress: vi.fn(async () => false),
  refreshActiveMessages: vi.fn(async () => {}),
  // ★ v1.68：打开中断弹窗前清一次旧回执。工厂少导出 → 调用时炸，故必须列。
  clearLastInterrupt: vi.fn(),
  // ★ v1.80：MCP 挂载区的真实试连结果。不导出就会在设置面板打开时炸
  //   `No "fetchMcpStatus" export`。
  fetchMcpStatus: vi.fn(async () => ({ mountedCount: 0, connectedCount: 0, servers: [] })),
  // v1.89：计划条读 chat_todos。工厂少导出 → 组件调用时炸 No "listTodos" export。
  listTodos: vi.fn(async () => []),
}))

// `vi.mock` 会被提升，所以这里拿到的已是替身。
import { useAuth } from '@/data/auth-store'
import {
  archiveSession,
  createSession,
  clearStaleSending,
  fetchMcpStatus,
  interject,
  interruptTurn,
  listTurns,
  loadSessions,
  resumeTurn,
  sendMessage,
  updateSettings,
  useChat,
} from '@/data/chat'
import type { ChatMessage, ChatSession, ChatState } from '@/data/chat'
import { ApiError } from '@/lib/api'
import ChatPage, { phaseLabel, turnToolCount } from './ChatPage'

const mockedUseAuth = vi.mocked(useAuth)
const mockedUseChat = vi.mocked(useChat)
const mockedSendMessage = vi.mocked(sendMessage)
const mockedCreateSession = vi.mocked(createSession)
const mockedArchiveSession = vi.mocked(archiveSession)
const mockedInterject = vi.mocked(interject)
const mockedListTurns = vi.mocked(listTurns)
const mockedResumeTurn = vi.mocked(resumeTurn)
const mockedClearStaleSending = vi.mocked(clearStaleSending)
const mockedInterruptTurn = vi.mocked(interruptTurn)

function session(overrides: Partial<ChatSession> = {}): ChatSession {
  return {
    id: 'chat_1',
    title: '查一下最近的扫描',
    createdBy: 'u1',
    messageCount: 2,
    archived: false,
    pinned: false,
    running: false,
    createdAt: '2026-10-25T00:00:00Z',
    updatedAt: '2026-10-25T00:00:00Z',
    ...overrides,
  }
}

function message(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: 'm1',
    seq: 1,
    role: 'user',
    content: '你好',
    reasoning: null,
    toolName: null,
    toolArgs: null,
    toolFailed: null,
    createdAt: '2026-10-25T00:00:00Z',
    ...overrides,
  }
}

function chatState(overrides: Partial<ChatState> = {}): ChatState {
  const merged: ChatState = {
    sessions: [],
    // v1.15：已归档是独立一段。不给它，页面里 archivedSessions.length
    // 会读到 undefined 直接抛错，看起来像页面崩了。
    archivedSessions: [],
    active: null,
    tools: [],
    personas: [],
    stagedAttachments: [],
    status: 'ready',
    error: null,
    pendingUserContent: null,
    sending: false,
    sendingSessionId: null,
    // ★ v0.100：流式过程中的两个状态。
    //   夹具不补它们，tsc 会报"缺属性" —— 那正是它该报的：
    //   少一个字段意味着页面读到 undefined，渲染逻辑会静默走错分支。
    phase: null,
    interjections: [],
    // ★ v0.102：正在流入的正文。同上，必须显式给 —— 缺了 tsc 会报，
    //   而页面读到 undefined 时会走错分支（`!== null` 对 undefined 成立，
    //   于是渲染一个空白的流式气泡）。
    streamingText: null,
    deciding: false,
    // ★ v1.61：服务端轮询到的轮次进度。同上，必须显式给 —— 缺了 tsc 会报。
    serverTurn: null,
    // ★ v1.62：已请求停止（但这一轮还没真正停）。同上。
    stopRequested: false,
    // ★ v1.65：本浏览器在跑的会话集合。同上，必须显式给。
    sendingSessionIds: [],
    // ★ v1.68：最近一次中断/继续回执。默认无。
    lastInterrupt: null,
    ...overrides,
  }
  // ★ v1.65 迁移：老夹具用 `sending:true + sendingSessionId:'x'` 表示"这个
  //   会话在跑"。新模型下 `sendingHere` 读的是 `sendingSessionIds`，所以
  //   这里把老写法自动补成集合，避免逐个改几十处夹具。若显式给了集合则以它为准。
  if (merged.sending && merged.sendingSessionId && overrides.sendingSessionIds === undefined) {
    merged.sendingSessionIds = [merged.sendingSessionId]
  }
  return merged
}

function renderPage(initialPath = '/chat') {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <ChatPage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedUseAuth.mockReturnValue({
    phase: 'ready',
    user: {
      id: 'u1',
      username: 'admin',
      role: 'admin',
      createdAt: '2026-10-25T00:00:00Z',
    },
    error: null,
    pending: false,
  })
  mockedUseChat.mockReturnValue(chatState())
})

afterEach(() => {
  cleanup()
})

describe('ChatPage — 工具失败必须看得出来', () => {
  it('★ 失败在收起的工作过程摘要上就能看到', () => {
    /*
      这条钉的是最贵的一类骗局：工具其实没干成，界面画得和干成了一样。
      事故形态是用户读到"扫描已建"，去扫描页找不到东西，然后开始不信平台。

      新形态下工具调用收在「工作过程」里，失败数必须冒到摘要行上 ——
      收起时看不见失败，等于把"工具挂了"藏起来。
    */
    const failed = message({
      id: 'm_tool',
      seq: 3,
      role: 'tool',
      content: '创建扫描失败：值为空',
      toolName: 'create_scan',
      toolFailed: true,
    })
    mockedUseChat.mockReturnValue(chatState({ active: { ...session(), messages: [failed] } }))

    const { container } = renderPage('/chat?session=chat_1')

    const work = container.querySelector('[data-turn-work]') as HTMLElement
    expect(work).not.toBeNull()
    expect(work.textContent).toContain('1 个失败')
  })

  it('★ 展开后工具卡带「失败」字样，且露出工具名', () => {
    const failed = message({
      id: 'm_tool',
      seq: 3,
      role: 'tool',
      content: '创建扫描失败：值为空',
      toolName: 'create_scan',
      toolFailed: true,
    })
    mockedUseChat.mockReturnValue(chatState({ active: { ...session(), messages: [failed] } }))
    const { container } = renderPage('/chat?session=chat_1')
    const toggle = container.querySelector('[data-turn-work] button') as HTMLElement | null
    if (!toggle) throw new Error('没有工作过程摘要')
    fireEvent.click(toggle)

    expect(screen.getByText('失败')).toBeTruthy()
    // 工具名也要露出来 —— 只说"失败"不告诉用户是哪个动作失败，等于没说。
    expect(screen.getByText('create_scan')).toBeTruthy()
  })

  it('成功的工具卡**不该**出现「失败」', () => {
    // 反向断言：如果某个改动把所有工具卡都标成失败，"永远显示失败"这一条会红。
    const ok = message({
      id: 'm_tool',
      seq: 3,
      role: 'tool',
      content: '已创建扫描 scan_9',
      toolName: 'create_scan',
      toolFailed: false,
    })
    mockedUseChat.mockReturnValue(chatState({ active: { ...session(), messages: [ok] } }))

    const { container } = renderPage('/chat?session=chat_1')
    expect(screen.queryByText(/个失败/)).toBeNull()
    const toggle = container.querySelector('[data-turn-work] button') as HTMLElement | null
    if (!toggle) throw new Error('没有工作过程摘要')
    fireEvent.click(toggle)

    expect(screen.getByText('create_scan')).toBeTruthy()
    expect(screen.queryByText('失败')).toBeNull()
  })
})

describe('ChatPage — 只读角色必须提前知道自己的边界', () => {
  it('★ 只读账号看到"不能建扫描或改技能库"的说明', () => {
    /*
      这条比"点了被后端 403"重要得多。只读账号点了「让它建个扫描」，
      模型会真的去调工具、真的失败，用户看到一堆工具报错 —— 那是最差的
      体验：既没干成事，又不知道是权限问题。
    */
    mockedUseAuth.mockReturnValue({
      phase: 'ready',
      user: {
        id: 'u2',
        username: 'viewer',
        role: 'viewer',
        createdAt: '2026-10-25T00:00:00Z',
      },
      error: null,
      pending: false,
    })
    mockedUseChat.mockReturnValue(chatState({ active: { ...session(), messages: [] } }))

    renderPage('/chat?session=chat_1')

    expect(screen.getByText(/只读角色/)).toBeTruthy()
  })

  it('管理员不该看到只读提示', () => {
    mockedUseChat.mockReturnValue(chatState({ active: { ...session(), messages: [] } }))
    renderPage('/chat?session=chat_1')

    expect(screen.queryByText(/只读角色/)).toBeNull()
  })
})

describe('ChatPage — 拿不到会话列表时给重试，而不是伪装成空', () => {
  it('★ 一条都没拿到 + 出错 → 显示"拿不到会话列表"并可重试', async () => {
    mockedUseChat.mockReturnValue(
      chatState({ status: 'error', error: new ApiError('NETWORK', '网络不通', 0), sessions: [] }),
    )

    renderPage()

    expect(screen.getByText('拿不到会话列表')).toBeTruthy()
    // 「还没有对话」是"确实没有"，和"没拿到"是两件事，不能同时出现。
    expect(screen.queryByText(/还没有对话/)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /重试/ }))
    await waitFor(() => expect(vi.mocked(loadSessions)).toHaveBeenCalled())
  })

  it('★ v1.18：进对话页必须去加载技能那份 store', () => {
    /*
      症状是技能下拉里写着"技能库是空的"，而技能库其实有 77 篇。

      根因不在渲染，在**加载入口**：页面调的是 `@/data/chat` 的
      `loadTools()`（`/chat/tools`，说明文本），而两个下拉读的是
      `useTools()` 的快照 —— 那个 store 只有工具页会去加载。
      所以这条只能断言"有没有去加载"，渲染结果测不出来。
    */
    renderPage()

    expect(vi.mocked(ensureToolsLoaded)).toHaveBeenCalled()
  })

  it('★ v1.80：MCP 多选可勾选保存，并展示真实试连结果', async () => {
    /*
      上一版这个下拉被移除，原因是"勾了没效果"：引擎不认识
      `mcpServerNames`，模型既看不到工具也调不了。现在恢复它之后，
      界面必须同时做到两件事：
        ① 勾选真的发 PATCH（不是只改本地）；
        ② 旁边显示 `/mcp-status` 的真实结果 —— 只回显勾选等于让用户
           继续猜"到底连上没有"。
    */
    mockedUseChat.mockReturnValue(chatState({ active: { ...session(), messages: [] } }))
    vi.mocked(fetchMcpStatus).mockResolvedValue({
      mountedCount: 2,
      connectedCount: 1,
      servers: [
        { name: 'fake-server', connected: true, toolCount: 3 },
        { name: 'team-mcp', connected: false, toolCount: 0, reason: '试连超时（>10 秒）' },
      ],
    })

    renderPage('/chat?session=chat_1')
    fireEvent.click(screen.getByRole('button', { name: '对话设置' }))
    fireEvent.click(screen.getByRole('button', { name: 'MCP' }))

    // 选项来自 `useTools()` 的 mcp 快照；这正是 v1.18 没接线的那份数据。
    expect(screen.getByText('fake-server')).toBeTruthy()
    expect(screen.getByText('team-mcp')).toBeTruthy()

    fireEvent.click(screen.getAllByRole('option')[0])

    await waitFor(() =>
      expect(vi.mocked(updateSettings)).toHaveBeenCalledWith('chat_1', {
        mcpServerNames: ['fake-server'],
      }),
    )
    // 面板一打开就去真试连，并如实显示"挂了几台 / 通了几台 / 几台失败"。
    await waitFor(() => expect(vi.mocked(fetchMcpStatus)).toHaveBeenCalledWith('chat_1'))
    expect(screen.getByText(/已挂载 2 台 · 连通 1 台/)).toBeTruthy()
    expect(screen.getByText(/1 台试连失败/)).toBeTruthy()
  })

  it('★ 已有列表时的失败**不抹掉列表**', () => {
    /*
      网抖一下就把已看到的对话全换成错误页，比"没刷新"严重得多 ——
      用户会以为对话被删了。这一条与 chat store 里
      「失败时保留已有列表」是同一套判断在界面上的体现。
    */
    mockedUseChat.mockReturnValue(
      chatState({
        status: 'error',
        error: new ApiError('NETWORK', '网络不通', 0),
        sessions: [session()],
      }),
    )

    renderPage()

    expect(screen.getByText('查一下最近的扫描')).toBeTruthy()
    expect(screen.queryByText('拿不到会话列表')).toBeNull()
  })
})

describe('ChatPage — 侧栏标出「哪个会话在跑」、不再画底部蓝条（v1.66）', () => {
  /*
    ★ 用户两条原话：
      m01705「可以不要显示"服务端仍在后台推进这一轮：第 N 轮 · 已用 M 次工具…"
             这个吗，CyberStrikeAI 刷新网页也不会有这个」
      m01720「左侧这边不知道哪个会话正在运行」

    做法对齐 CyberStrikeAI：删掉聊天区底部那条带轮次/工具数的常驻蓝条，
    改在**左侧会话列表**的条目上画一个转圈点 + 「运行中」。运行态来自服务端
    `running` 字段（跨刷新/跨浏览器权威），不是本地 SSE 布尔。
  */
  it('★ 会话 running=true → 该条目出现转圈点(data-session-running) 与「运行中」前缀', () => {
    mockedUseChat.mockReturnValue(
      chatState({
        sessions: [session({ id: 'chat_run', title: '正在跑的会话', running: true })],
      }),
    )
    const { container } = renderPage()

    const dot = container.querySelector('[data-session-running="true"]')
    expect(dot).not.toBeNull()
    // 「运行中」出现在副标题（messageCount 前）
    expect(screen.getByText(/运行中/)).toBeTruthy()
  })

  it('★ 会话 running=false/缺省 → 没有转圈点，也不显示「运行中」', () => {
    mockedUseChat.mockReturnValue(
      chatState({
        sessions: [session({ id: 'chat_idle', title: '空闲会话', running: false })],
      }),
    )
    const { container } = renderPage()

    expect(container.querySelector('[data-session-running="true"]')).toBeNull()
    expect(screen.queryByText(/运行中/)).toBeNull()
  })

  it('★ 子代理会话挂在父会话下面（缩进一级），不占顶层条目', () => {
    mockedUseChat.mockReturnValue(
      chatState({
        sessions: [
          session({ id: 'chat_parent', title: '父会话' }),
          session({
            id: 'chat_child',
            title: '[子代理] web-vuln',
            parentSessionId: 'chat_parent',
            running: true,
          }),
        ],
      }),
    )
    const { container } = renderPage()

    const parentLi = screen.getByText('父会话').closest('li')
    const childLi = screen.getByText('[子代理] web-vuln').closest('li')
    expect(parentLi).not.toBeNull()
    expect(childLi).not.toBeNull()
    // 子行有分支缩进/左边线；顶层没有。
    expect(childLi?.querySelector('.ml-3')).not.toBeNull()
    expect(parentLi?.querySelector('.ml-3')).toBeNull()
    // 子代理自己 running=true 时也照常画转圈点。
    expect(container.querySelector('[data-session-running="true"]')).not.toBeNull()
  })

  it('★ v1.68.2：侧栏还有会话 running=true 时，持续每 4s 重拉列表（自愈转圈点，无需刷新）', async () => {
    /*
      用户原话「结束了前端对话列还会打转，要刷新网页才行」。根因：轮询条件是
      `sendingSessionIds.length>0`，本会话一结束就停，侧栏 `session.running`
      最后一次拉到的 true 冻结不动。修法：侧栏还有 running 的会话时也持续轮询。
      这里用假定时器验证：本页无本地 sending，但列表里有一条 running=true →
      loadSessions 会被 4s 定时器反复调用。
    */
    vi.useFakeTimers()
    try {
      vi.mocked(loadSessions).mockClear()
      vi.mocked(useChat).mockReturnValue(
        chatState({
          // 本页没有本地在跑（sendingSessionIds 空）
          sendingSessionIds: [],
          // 但列表里有一条服务端 running=true（别的会话 / 后台残留）
          sessions: [session({ id: 'chat_run', title: '后台在跑', running: true })],
        }),
      )
      renderPage()

      // 初始 tick 后，推进 4 秒应触发一次 loadSessions（定时器）
      const before = vi.mocked(loadSessions).mock.calls.length
      await vi.advanceTimersByTimeAsync(4200)
      expect(vi.mocked(loadSessions).mock.calls.length).toBeGreaterThan(before)
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ 服务端在跑（本页无 SSE）时也**不再**出现底部蓝条 data-server-progress', () => {
    /*
      v1.61 加的底部蓝条已按用户要求删除。这里守住"别又画回来"：
      即便 serverRunning 为真（服务端在跑、本页没流），聊天区也不该再出现
      那条 `data-server-progress` 横幅。运行态的可见性交给侧栏转圈点 + 输入区
      的「中断」按钮。
    */
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: false,
        sendingSessionIds: [],
        serverTurn: { runId: 'r1', roundIndex: 3, toolsUsed: 7, heartbeatAt: null },
      }),
    )
    const { container } = renderPage('/chat?session=chat_1')

    expect(container.querySelector('[data-server-progress="true"]')).toBeNull()
  })
})

describe('ChatPage — 输入框的中文输入法陷阱', () => {
  it('★ 组合态（拼音未上屏）按 Enter 不发送', async () => {
    /*
      `isComposing` 为 true 时按 Enter 是"上屏候选词"。不排除的话，
      中文用户每打一个词就会把半截拼音发出去 —— 这是中文界面上
      最常见也最恼人的一类 bug，而且英文环境测不出来。
    */
    mockedUseChat.mockReturnValue(chatState({ active: { ...session(), messages: [] } }))
    renderPage('/chat?session=chat_1')

    const box = screen.getByLabelText('输入消息') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'niha' } })

    // `fireEvent.keyDown` 的第三参可以带 `nativeEvent` 上的字段。
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: false, isComposing: true })

    expect(mockedSendMessage).not.toHaveBeenCalled()
  })

  it('组合态结束后 Enter 才发送', async () => {
    mockedUseChat.mockReturnValue(chatState({ active: { ...session(), messages: [] } }))
    renderPage('/chat?session=chat_1')

    const box = screen.getByLabelText('输入消息') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: '你好' } })
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: false, isComposing: false })

    /*
      ★ 断言的是**前两个实参**，不是完整调用。

      v0.94 给 `sendMessage` 加了第 3、4 个参数（`providerId`、`explicitTools`），
      而页面永远显式传四个。用 `toHaveBeenCalledWith('chat_1', '你好')` 会
      因为"实参个数不同"而失败 —— 那种失败看起来像"消息没发出去"，
      其实消息发得好好的。

      这里只关心"发给哪个会话、发了什么内容"，这两个才是这一条用例的主题
      （输入法组合态）。工具提示与模型选择各自有专门的用例。
    */
    await waitFor(() => {
      expect(mockedSendMessage).toHaveBeenCalled()
      const call = mockedSendMessage.mock.calls[0]
      expect(call[0]).toBe('chat_1')
      expect(call[1]).toBe('你好')
    })
  })

  it('★ @工具 会作为提示一起发出去，且正文原样保留 @', async () => {
    /*
      两个都要断言，因为它们各自会单独坏：

      · 提示没带上 → 模型不知道用户点名了哪个工具，`@` 这个功能等于装饰；
      · 正文被改写（把 `@list_scans` 删掉）→ 用户看到自己打的字消失，
        而且模型少了一条与提示互为印证的线索。
    */
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        tools: [
          {
            name: 'list_scans',
            label: '列出扫描',
            description: '看看跑过什么',
            params: '无',
            scope: null,
            available: true,
          },
        ],
      }),
    )
    renderPage('/chat?session=chat_1')

    const box = screen.getByLabelText('输入消息') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: '@list_scans 看看最近跑过什么' } })
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: false, isComposing: false })

    await waitFor(() => {
      expect(mockedSendMessage).toHaveBeenCalled()
      const call = mockedSendMessage.mock.calls[0]
      expect(call[1]).toBe('@list_scans 看看最近跑过什么')
      expect(call[3]).toEqual(['list_scans'])
    })
  })

  it('Shift+Enter 换行，不发送', () => {
    mockedUseChat.mockReturnValue(chatState({ active: { ...session(), messages: [] } }))
    renderPage('/chat?session=chat_1')

    const box = screen.getByLabelText('输入消息') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: '第一行' } })
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true, isComposing: false })

    expect(mockedSendMessage).not.toHaveBeenCalled()
  })
})

describe('ChatPage — 归档', () => {
  it('★ 归档按钮有可读的 aria-label（不能只是个图标）', () => {
    /*
      悬停才出现的图标按钮，如果只有图标没有标签，键盘和读屏用户
      永远不知道它是干什么的。aria-label 里带上会话标题，连归档哪个
      都说清楚了。
    */
    mockedUseChat.mockReturnValue(chatState({ sessions: [session()] }))
    renderPage()

    expect(screen.getByRole('button', { name: '归档对话「查一下最近的扫描」' })).toBeTruthy()
  })

  it('★ 点归档先问一句（因为归档会当场销毁沙箱与附件）', async () => {
    /*
      v1.15 起归档不再是“能后悔”的动作：后端会当场销毁对话沙箱与上传附件，
      /work 里跑出来的东西回不来。既然不能悔，就得先问。
    */
    mockedUseChat.mockReturnValue(chatState({ sessions: [session()] }))
    renderPage()

    fireEvent.click(screen.getByRole('button', { name: '归档对话「查一下最近的扫描」' }))
    // 还没确认，不能就归档了
    expect(mockedArchiveSession).not.toHaveBeenCalled()
    expect(screen.getByText('删除这个对话？')).toBeTruthy()
  })

  it('确认后才调用 archiveSession', async () => {
    mockedUseChat.mockReturnValue(chatState({ sessions: [session()] }))
    renderPage()

    fireEvent.click(screen.getByRole('button', { name: '归档对话「查一下最近的扫描」' }))
    fireEvent.click(screen.getByRole('button', { name: '删除' }))
    await waitFor(() => expect(mockedArchiveSession).toHaveBeenCalledWith('chat_1'))
  })
})

describe('ChatPage — 新对话', () => {
  it('新建成功后清空草稿（不把上一条的残留带进新对话）', async () => {
    mockedCreateSession.mockResolvedValue(session({ id: 'chat_new', title: '新对话' }))
    mockedUseChat.mockReturnValue(chatState({ sessions: [session()] }))
    renderPage()

    // ⚠️ 精确匹配 `新对话`：空状态里还有个「开始新对话」，`/新对话/` 会同时命中两个。
    fireEvent.click(screen.getByRole('button', { name: '新对话' }))

    await waitFor(() => expect(mockedCreateSession).toHaveBeenCalled())
  })

  it('新建失败时不跳转、不留错误（store 已挂 error，页面底下那条会显示）', async () => {
    mockedCreateSession.mockResolvedValue(null)
    mockedUseChat.mockReturnValue(
      chatState({
        sessions: [session()],
        error: new ApiError('VALIDATION_FAILED', '建不出来', 400),
      }),
    )
    renderPage()

    // ⚠️ 精确匹配 `新对话`：空状态里还有个「开始新对话」，`/新对话/` 会同时命中两个。
    fireEvent.click(screen.getByRole('button', { name: '新对话' }))

    await waitFor(() => expect(mockedCreateSession).toHaveBeenCalled())
    // 页面上仍能看到已有会话 —— 失败不该把用户已有的东西带走。
    expect(screen.getByText('查一下最近的扫描')).toBeTruthy()
  })
})

describe('ChatPage — 点发送立刻看见自己那句话（v0.97）', () => {
  /*
    ★ 这一组针对的是一次真实的体验事故：用户点发送后，输入框里的字
      还在、屏幕上没有新气泡、只有右下角一个「正在想…」。原因有两个，
      都在这组测试里被钉住：

      ① 页面是 `await sendMessage(...)` 之后才 `setDraft('')` —— 而那一等
         是整整一轮（模型想 + 可能十几次工具调用）。
      ② `sendMessage` 整轮只 commit 一次，用户那句话要和服务端的全部回复
         一起落地。

      用户的判读是"我点了，什么都没发生"，然后他会再点一次 —— 于是同一句
      话发两遍。所以这两条都不是"体验优化"，是会造成错误数据的缺陷。
  */

  it('★ v1.49：服务端说这一轮还在跑（本地 done 帧丢了）→ Enter 是「插话」不是发新轮', async () => {
    /*
      线上事故：done 帧丢了以后本地 `sending` 变成 false，可服务端那一轮还在
      跑。老代码这时会冒然 sendMessage → 起第二轮，老轮还在干、新轮 round=0
      空转 = 「问一句停一次 / 一直在循环」。

      现在：`lastRuns` 里最近一条是 `running` 时，输入框该显示「插话」按钮，
      并且 Enter 走 interject（不是 sendMessage）。
    */
    mockedListTurns.mockResolvedValue([
      {
        id: 'run_1',
        sessionId: 'chat_1',
        status: 'running',
        resumeAttempts: 0,
        startedAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
        resumable: false,
      },
    ])
    mockedInterject.mockResolvedValue('ok')
    mockedUseChat.mockReturnValue(chatState({ active: { ...session(), messages: [] } }))
    renderPage('/chat?session=chat_1')

    // 按钮文案变成「插话」（服务端在跑 → turnRunningHere 为真）
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: '插话' }),
        '服务端还在跑时按钮必须显示「插话」，而不是「发送」',
      ).toBeTruthy()
    })

    const box = screen.getByLabelText('输入消息') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: '换一个思路' } })
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: false, isComposing: false })

    await waitFor(() => expect(mockedInterject).toHaveBeenCalled())
    expect(
      mockedSendMessage,
      '服务端说还在跑时绝不能起新一轮（sendMessage），否则就是并发双轮',
    ).not.toHaveBeenCalled()
  })

  it('★ 点下发送的瞬间，输入框就空了', async () => {
    /*
      用 `Promise` 手动控制 `sendMessage` 何时 resolve —— 只要它没 resolve，
      就代表"服务端还在想"。此时输入框必须已经空了。
    */
    let release: ((ok: boolean) => void) | undefined
    mockedSendMessage.mockImplementation(
      () =>
        new Promise<boolean>((resolve) => {
          release = resolve
        }),
    )

    mockedUseChat.mockReturnValue(chatState({ active: { ...session(), messages: [] } }))
    renderPage('/chat?session=chat_1')

    const box = screen.getByLabelText('输入消息') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: '列出可用技能' } })
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: false, isComposing: false })

    // 还没等到任何响应
    await waitFor(() => expect(mockedSendMessage).toHaveBeenCalled())
    expect(box.value, '发送后输入框必须立刻清空（否则用户以为没发出去，会再点一次）').toBe('')

    release?.(true)
    await waitFor(() => expect(box.value).toBe(''))
  })

  it('★ 发送失败时把原文放回输入框，不丢字', async () => {
    /*
      清空是乐观的，所以失败路径必须有回填。没有的话用户刚敲的那段字
      就彻底没了 —— 而失败原因往往只是模型接入点抖了一下。
    */
    mockedSendMessage.mockResolvedValue(false)

    mockedUseChat.mockReturnValue(chatState({ active: { ...session(), messages: [] } }))
    renderPage('/chat?session=chat_1')

    const box = screen.getByLabelText('输入消息') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: '这段字不能丢' } })
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: false, isComposing: false })

    await waitFor(() => {
      expect((screen.getByLabelText('输入消息') as HTMLTextAreaElement).value).toBe('这段字不能丢')
    })
  })

  it('★ 服务端没回之前，那句话已经画在屏幕上', () => {
    /*
      这条直接测渲染：`pendingUserContent` 就是"刚发出去、还没回"的那句。
      它必须出现在屏幕上，而且带原文。
    */
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: true,
        sendingSessionId: 'chat_1',
        pendingUserContent: '列出可用技能',
      }),
    )
    const { container } = renderPage('/chat?session=chat_1')

    const bubble = container.querySelector('[data-pending-bubble="true"]')
    expect(bubble, '找不到乐观气泡 —— 发送中的那句话必须立刻可见').not.toBeNull()
    expect(bubble?.textContent).toBe('列出可用技能')
  })

  it('★ 乐观气泡在「正在想」**上面**（时间顺序不能反）', () => {
    /*
      画反了读起来就是"模型先想了，我才问"。用 `compareDocumentPosition`
      比较两个节点在文档里的先后，而不是比较 `textContent` 里的下标 ——
      后者会被无关文案干扰。
    */
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: true,
        sendingSessionId: 'chat_1',
        pendingUserContent: '列出可用技能',
      }),
    )
    const { container } = renderPage('/chat?session=chat_1')

    const bubble = container.querySelector('[data-pending-bubble="true"]')!
    const thinking = screen.getByText('正在想…')
    expect(
      bubble.compareDocumentPosition(thinking) & Node.DOCUMENT_POSITION_FOLLOWING,
      '「正在想」应该排在乐观气泡之后（下方）',
    ).toBeTruthy()
  })

  it('服务端回来后乐观气泡消失（不会和真消息重复）', () => {
    /*
      `pendingUserContent` 为 null 时不画。store 在回包里会把它置空，
      这里测的是"置空之后界面确实没有它"。
    */
    mockedUseChat.mockReturnValue(
      chatState({
        active: {
          ...session(),
          messages: [message({ id: 'm1', seq: 1, role: 'user', content: '列出可用技能' })],
        },
        sending: false,
        pendingUserContent: null,
      }),
    )
    const { container } = renderPage('/chat?session=chat_1')

    expect(container.querySelector('[data-pending-bubble="true"]')).toBeNull()
  })

  it('★ 有乐观气泡时不能说「这个对话还是空的」', () => {
    /*
      实测截图上抓到的矛盾：点下发送后右侧已经画出"列出可用技能"，
      中间却还挂着"这个对话还是空的。说点什么…"。一句话既在又不在。

      根因是空态只看 `messages.length`，而乐观气泡不在 messages 里。
      这条同时断言两件事，缺一不可：
        ① 气泡在
        ② 空态文案不在
      只断 ① 的话，把空态条件写错也测不出来。
    */
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: true,
        sendingSessionId: 'chat_1',
        pendingUserContent: '列出可用技能',
      }),
    )
    const { container } = renderPage('/chat?session=chat_1')

    expect(container.querySelector('[data-pending-bubble="true"]')).not.toBeNull()
    expect(
      screen.queryByText(/这个对话还是空的/),
      '用户已经发出话了，空态就不能再显示 —— 否则同一句话既在又不在',
    ).toBeNull()
  })

  it('确实一条消息都没有、也没在发时，才显示空态', () => {
    /* 上一条的对照组：防止「有气泡就永不显示空态」把正常空态也吞掉。 */
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: false,
        pendingUserContent: null,
      }),
    )
    renderPage('/chat?session=chat_1')

    expect(screen.getByText(/这个对话还是空的/)).not.toBeNull()
  })

  it('★ 条数徽标把乐观气泡算进去（不能一边显示气泡一边说「0 条消息」）', () => {
    /*
      实机截图上抓到的第三处同形谎报：右侧乐观气泡已画出，右上角还是
      「0 条消息」。数字说没有、画面说已有，用户没法判断到底发出去没有。

      只断"不是 0 条消息"还不够 —— 要断确切数字，否则把公式写成
      `messages.length + 2` 也能过。
    */
    mockedUseChat.mockReturnValue(
      chatState({
        active: {
          ...session(),
          messages: [message({ id: 'm1', seq: 1, role: 'user', content: '你好' })],
        },
        sending: true,
        sendingSessionId: 'chat_1',
        pendingUserContent: '列出可用技能',
      }),
    )
    renderPage('/chat?session=chat_1')

    // 1 条真消息 + 1 条乐观气泡 = 2
    expect(screen.getByText('2 条消息')).not.toBeNull()
    expect(screen.queryByText('1 条消息')).toBeNull()
  })
})

/*
  ══════════════════════════════════════════════════════════════
  v0.100：流式 —— 跑的时候能看见、能插话
  ══════════════════════════════════════════════════════════════

  用户的原话是「正在想的时候不能插话，没有思考过程、工具调用、
  调用结果的显示」。所以这里钉两件**用户能直接感知**的事：

  ① 那个转圈旁边必须真的说清「在做什么、第几轮」——
     一个没有信息的转圈回答不了"它在推进还是卡住了"。
  ② 跑的时候输入框**能用**，按下去是「插话」而不是再开一轮。
*/

describe('ChatPage — 流式：跑的时候看得见、能插话', () => {
  it('★ 转圈旁边显示"第几轮 + 在做什么"，而不是一句没有信息的"正在想"', () => {
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: true,
        sendingSessionId: 'chat_1',
        phase: { phase: 'thinking', round: 0 },
      }),
    )
    renderPage('/chat?session=chat_1')

    // round 是 0-based，界面上要 1-based（"第 1 轮"比"第 0 轮"自然）
    expect(screen.getByText(/第 1 轮/)).not.toBeNull()
    expect(screen.getByText(/推理/)).not.toBeNull()
  })

  it('★ 执行工具时显示工具名（用户能看出它在干嘛，而不是卡住）', () => {
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: true,
        sendingSessionId: 'chat_1',
        phase: { phase: 'tool', round: 1, tool: 'list_skills' },
      }),
    )
    renderPage('/chat?session=chat_1')

    expect(screen.getByText(/list_skills/)).not.toBeNull()
    expect(screen.getByText(/第 2 轮/)).not.toBeNull()
  })

  it('★ 没有 phase 时回落到"正在想…"（刚点发送、还没收到第一帧）', () => {
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: true,
        sendingSessionId: 'chat_1',
        phase: null,
      }),
    )
    renderPage('/chat?session=chat_1')

    expect(screen.getByText('正在想…')).not.toBeNull()
  })

  it('★★ 跑着的时候输入框**不能用 disabled 挡住**（这是"不能插话"的直接原因）', () => {
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: true,
        sendingSessionId: 'chat_1',
        phase: { phase: 'thinking', round: 0 },
      }),
    )
    renderPage('/chat?session=chat_1')

    const textarea = screen.getByLabelText('输入消息') as HTMLTextAreaElement
    expect(textarea.disabled, '跑的时候输入框被禁用了 —— 用户就没法插话，这正是要修的问题').toBe(
      false,
    )
  })

  it('★★ 跑着的时候按发送 → 走 interject，而不是再开一轮 sendMessage', async () => {
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: true,
        sendingSessionId: 'chat_1',
        phase: { phase: 'thinking', round: 0 },
      }),
    )
    renderPage('/chat?session=chat_1')

    const textarea = screen.getByLabelText('输入消息') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value: '顺便看一下 8080 端口' } })

    // 跑着的时候按钮文案是「插话」（不是「发送」）——
    // 文案不对用户会以为自己在开第二轮
    const button = screen.getByRole('button', { name: '插话' })
    fireEvent.click(button)

    await waitFor(() => {
      expect(mockedInterject).toHaveBeenCalledWith('chat_1', '顺便看一下 8080 端口')
    })
    expect(mockedSendMessage).not.toHaveBeenCalled()
  })

  it('★ 空闲时按发送 → 走 sendMessage（插话路径不能抢走正常发送）', async () => {
    mockedUseChat.mockReturnValue(
      chatState({ active: { ...session(), messages: [] }, sending: false }),
    )
    renderPage('/chat?session=chat_1')

    const textarea = screen.getByLabelText('输入消息') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value: '你好' } })

    const button = screen.getByRole('button', { name: '发送' })
    fireEvent.click(button)

    await waitFor(() => {
      expect(mockedSendMessage).toHaveBeenCalled()
    })
    expect(mockedInterject).not.toHaveBeenCalled()
  })

  it('★ 插话失败时把原文放回输入框（别让用户重打一遍）', async () => {
    mockedInterject.mockResolvedValueOnce('failed')
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: true,
        sendingSessionId: 'chat_1',
        phase: { phase: 'thinking', round: 0 },
      }),
    )
    renderPage('/chat?session=chat_1')

    const textarea = screen.getByLabelText('输入消息') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value: '插一句会失败的' } })
    fireEvent.click(screen.getByRole('button', { name: '插话' }))

    await waitFor(() => {
      expect((screen.getByLabelText('输入消息') as HTMLTextAreaElement).value).toBe(
        '插一句会失败的',
      )
    })
  })

  it('★ 插话显示在消息流里并说明"模型下一轮会看到"（用户要能确认话进去了）', () => {
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: true,
        sendingSessionId: 'chat_1',
        interjections: [message({ id: 'ij1', seq: 9, role: 'user', content: '顺便看下 8080' })],
      }),
    )
    const { container } = renderPage('/chat?session=chat_1')

    const box = container.querySelector('[data-interjection="true"]')
    expect(box, '插话没有显示在消息流里').not.toBeNull()
    expect(box?.textContent).toContain('顺便看下 8080')
    expect(box?.textContent).toContain('下一轮')
  })
})

/*
  ══════════════════════════════════════════════════════════════
  ChatPage — 工具调用收成一行（v0.103）
  ══════════════════════════════════════════════════════════════

  用户先贴了一张 `list_challenges` 的卡把 20 道题全铺在对话流中间（整屏
  都是它），说「像这种可以折叠」；第一版做的是"长输出才折叠"，用户随后又贴
  了 Claude Code 的截图说「就是要类似这样的」—— 那里的工具调用**默认就是
  一行**，跟输出长短无关。

  所以判据从"多长算长"变成"默认收成一行"。这里钉几条会真的骗到人的形态：

  ① 默认必须收着（否则对话页又会被过程输出淹掉，回到用户抱怨的原点）
  ② 收着时**不能把全文留在 DOM 里**（否则折叠只是视觉把戏：Ctrl+F 还能搜到
     藏起来的内容，屏幕阅读器也照样读完全部）
  ③ 折叠态那一行必须说清"干了什么"（工具名 + 参数摘要），不能只剩一个图标
     —— 收起来就看不出这是哪次调用，用户得挨个点开找
  ④ 失败必须在**折叠态**就看得出来（收起来看不见"失败"，用户会漏掉工具偷偷挂了）
  ⑤ 展开后能看到全文，且 aria 状态跟着翻转（屏幕阅读器用户靠它判断）
*/
describe('ChatPage — 工具调用收成一行（v0.103）', () => {
  // 用户截图里那张卡的形状：20 行短记录
  const manyLines = Array.from({ length: 20 }, (_, i) => `- 第 ${i + 1} 条记录`).join('\n')

  function renderTool(
    content: string,
    failed: boolean | null = false,
    toolArgs: Record<string, unknown> | null = null,
  ) {
    mockedUseChat.mockReturnValue(
      chatState({
        active: {
          ...session(),
          messages: [
            message({ id: 'u1', seq: 1, role: 'user', content: '题库里还有什么没解' }),
            message({
              id: 't1',
              seq: 2,
              role: 'tool',
              content,
              toolName: 'list_challenges',
              toolArgs,
              toolFailed: failed,
            }),
          ],
        },
      }),
    )
    return renderPage('/chat?session=chat_1')
  }

  function openWork(container: HTMLElement): void {
    const toggle = container.querySelector('[data-turn-work] button') as HTMLElement | null
    if (!toggle) throw new Error('没有工作过程摘要')
    fireEvent.click(toggle)
  }

  it('★ 默认收着 —— 工具调用收进「工作过程」，一条摘要就够', () => {
    const { container } = renderTool('OK')

    // 工具卡不在 DOM 里：默认只露一条「工作过程 · N 步 · 1 个工具」
    expect(container.querySelector('[data-message-row="tool"]')).toBeNull()
    const work = container.querySelector('[data-turn-work]') as HTMLElement
    expect(work).not.toBeNull()
    expect(work.getAttribute('data-turn-work-tools')).toBe('1')
    expect(screen.getByRole('button', { name: /工作过程/ }).getAttribute('aria-expanded')).toBe(
      'false',
    )
  })

  it('★ 收着时正文全文不在 DOM 里（只靠 CSS 藏 = 假折叠）', () => {
    const { container } = renderTool(manyLines)
    openWork(container)

    const card = container.querySelector('[data-message-row="tool"]')
    expect(card?.getAttribute('data-tool-collapsed')).toBe('true')

    /*
      ★ 这条是折叠的关键判据。
        如果收着的时候全文仍在 DOM（只靠 CSS 隐藏），那"折叠"就是视觉把戏：
        Ctrl+F 能搜到藏起来的内容，屏幕阅读器也照样读完全部。用户以为自己
        收起来了，其实没有。
    */
    expect(card?.textContent, '折叠态把全文留在 DOM 里了 —— 那不是折叠').not.toContain(
      '第 20 条记录',
    )
  })

  it('★ 那一行要说清"干了什么"：工具名 + 参数摘要（只剩图标等于没信息）', () => {
    const { container } = renderTool('done', false, { command: 'nmap -sV 10.0.0.5' })
    openWork(container)

    const card = container.querySelector('[data-message-row="tool"]')
    expect(card?.textContent).toContain('list_challenges')
    expect(card?.textContent, '参数摘要没显示 —— 收起来就看不出是哪次调用').toContain(
      'nmap -sV 10.0.0.5',
    )
  })

  it('★ 失败在折叠态就看得到（收起来看不见失败 = 用户漏掉工具挂了）', () => {
    const { container } = renderTool(manyLines, true)

    // 折叠态：失败数冒到工作过程摘要行
    const work = container.querySelector('[data-turn-work]') as HTMLElement
    expect(work.textContent).toContain('1 个失败')

    openWork(container)
    const card = container.querySelector('[data-message-row="tool"]')
    expect(card?.getAttribute('data-tool-collapsed')).toBe('true')
    expect(card?.textContent).toContain('失败')
  })

  it('★ 点开后看到全文，aria 与文案都翻转', () => {
    const { container } = renderTool(manyLines)
    openWork(container)

    const toggle = container.querySelector('[data-tool-toggle="true"]') as HTMLElement | null
    expect(toggle, '工具卡没有折叠开关').not.toBeNull()
    expect(toggle?.getAttribute('aria-expanded')).toBe('false')

    // 上面那条 expect 只在运行时生效，tsc 不认它是类型收窄 —— 显式断言非空。
    if (!toggle) throw new Error('unreachable')
    fireEvent.click(toggle)

    const card = container.querySelector('[data-message-row="tool"]')
    expect(card?.getAttribute('data-tool-collapsed')).toBe('false')
    expect(card?.textContent).toContain('第 20 条记录')

    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(toggle.textContent).toContain('收起')
  })
})

/*
  ══════════════════════════════════════════════════════════════
  phaseLabel：自动纠偏续跑必须"看得见"（v1.14）
  ══════════════════════════════════════════════════════════════

  纠偏逻辑本身是静默的：模型这一轮没写动作，服务端悄悄再问它一次。
  改造前这个位置**一帧都不推**，界面上的「正在想…」就那么停着，然后
  凭空蹦出下一段 —— 用户分不清「在自动重试」（该等）与「卡死了」（该刷新）。
  这两件事的动作相反，所以"看不出来"本身就是缺陷。

  这里测的是**文案**，因为文案是用户唯一能看到的东西。三条判据：
  ① `resume` 必须有自己的文案，不能回落成「正在想…」（那就等于没做）；
  ② 被输出长度切断与"没写动作"要给不同的话 —— 用户已经看到半句话时，
     说"正在把话说完"才对；说"重试"会让他以为刚才那段作废了；
  ③ 不能把协议内部词（ACTION、finish_reason、truncation）漏到界面上。
*/

describe('phaseLabel：纠偏续跑的文案', () => {
  it('★ resume 必须有自己的文案，不能回落到推理/正在想', () => {
    const label = phaseLabel({ phase: 'resume', round: 0 })
    // ⚠️ 只断言 `not.toBe('正在想…')` 是不够的：`round: 0` 时回落分支给出
    //    的是「第 1 轮 · 模型在推理…」，那条断言照样绿。变异验证暴露过这一点。
    expect(label).not.toBe('正在想…')
    expect(label, 'resume 回落成了推理文案 —— 等于这个功能没做').not.toContain('模型在推理')
    expect(label).toContain('第 1 轮')
  })

  it('★ 被切断时说"接着写"，不说"重试"', () => {
    const label = phaseLabel({ phase: 'resume', round: 1, reason: 'truncation' })
    expect(label).toContain('截断')
    expect(label).not.toContain('重试')
  })

  it('★ 没写动作时说"把话说完"（与切断区分开）', () => {
    const truncated = phaseLabel({ phase: 'resume', round: 0, reason: 'truncation' })
    const format = phaseLabel({ phase: 'resume', round: 0, reason: 'format' })
    expect(format).not.toBe(truncated)
    expect(format).toContain('把话说完')
  })

  it('★ v1.29：FINAL 打回时说"正在确认做完了没"（与"重试"区分）', () => {
    const label = phaseLabel({ phase: 'resume', round: 2, reason: 'final-check' })
    expect(label).toContain('正在确认它是不是真的做完了')
    expect(label).not.toContain('重试')
    expect(label).not.toBe(phaseLabel({ phase: 'resume', round: 2, reason: 'format' }))
  })

  it('★ 不把协议内部词漏给用户', () => {
    for (const reason of ['truncation', 'format', 'final-check', undefined]) {
      const label = phaseLabel({ phase: 'resume', round: 0, reason })
      expect(label).not.toContain('ACTION')
      expect(label).not.toContain('finish_reason')
      expect(label).not.toContain('truncation')
      expect(label).not.toContain('format')
    }
  })

  it('既有两种 phase 的文案不变（别为了加 resume 改坏它们）', () => {
    expect(phaseLabel({ phase: 'thinking', round: 0 })).toBe('第 1 轮 · 模型在推理…')
    expect(phaseLabel({ phase: 'tool', round: 2, tool: 'run_shell' })).toBe(
      '第 3 轮 · 正在执行 run_shell…',
    )
    expect(phaseLabel(null)).toBe('正在想…')
  })
})

describe('turnToolCount：live 阶段行上的「已 N 次工具」计数（v1.30）', () => {
  /*
    这个计数接在 `<AssistantTurn progress={...}>` 上（ChatPage 传给
    AssistantTurn，AssistantTurn 用 progressLabel 渲染）。数错会让用户
    在跑的时候看到假的推进数 —— 所以只钉两条最会骗人的边界：

    ① 只数 `role === 'tool'`：旁白（`process`）也"动过"，但每轮开头模型
       必先写一句旁白；把旁白算进去，计数会在真正执行第一个工具前就从 1
       开始，反而看不出工具推进。
    ② 空列表 = 0，不 NaN、不崩。
  */
  it('★ 只数 tool 消息：旁白（process）不算', () => {
    const items: ChatMessage[] = [
      message({ role: 'assistant', content: '我先看看工作目录' }), // 旁白
      message({ role: 'tool' }),
      message({ role: 'assistant', content: '再跑一次' }), // 旁白
      message({ role: 'tool' }),
      message({ role: 'tool' }),
      message({ role: 'user', content: '继续' }),
    ]
    expect(turnToolCount(items)).toBe(3)
  })

  it('空回合 = 0', () => {
    expect(turnToolCount([])).toBe(0)
  })
})

describe('ChatPage — 中断的一轮要能看见、能继续（v1.19）', () => {
  it('★ 服务端说可接手时，页面出现「继续这一轮」', async () => {
    /*
      用户原话：「对话又没反应的，没有长任务持久化啊」。

      这条钉的是**可见性**：后端把中断落了库、也给了接口，但页面不显示，
      用户看到的仍然是一片空白 —— 那就等于没修。判据用稳定的
      `data-stuck-turn`，不靠文案（文案会改，夹具不该跟着文案漂）。
    */
    mockedUseChat.mockReturnValue(chatState({ active: { ...session(), messages: [] } }))
    mockedListTurns.mockResolvedValue([
      {
        id: 'turn_1',
        sessionId: 'chat_1',
        status: 'interrupted',
        resumeAttempts: 1,
        lastError: 'ClientDisconnected: 客户端断开',
        startedAt: '2026-10-28T05:00:00Z',
        updatedAt: '2026-10-28T05:01:00Z',
        resumable: true,
      },
    ])

    renderPage('/chat?session=chat_1')

    const banner = await screen.findByText(/上一轮没有跑完/)
    expect(banner).toBeTruthy()
    expect(screen.getByText('继续这一轮')).toBeTruthy()
    // 原因要写出来，否则用户还是不知道"为什么停了"
    expect(screen.getByText(/客户端断开/)).toBeTruthy()
  })

  it('★ 没有可接手的轮次时不显示提示条', async () => {
    mockedUseChat.mockReturnValue(chatState({ active: { ...session(), messages: [] } }))
    mockedListTurns.mockResolvedValue([])

    const { container } = renderPage('/chat?session=chat_1')
    // 等一拍，让 effect 里的 promise 落定 —— 否则断言会在"还没查"时通过
    await screen.findByPlaceholderText(/说点什么/)
    expect(container.querySelector('[data-stuck-turn]')).toBeNull()
  })

  it('★ 点「继续这一轮」会用当前 run 调 resumeTurn', async () => {
    /*
      只验"点了有没有发出去"。真正跑一轮的语义在服务端测
      （`tests/chat/test_turns_api.py`）—— 前端不该假装能验模型说什么。
    */
    mockedUseChat.mockReturnValue(chatState({ active: { ...session(), messages: [] } }))
    mockedListTurns.mockResolvedValue([
      {
        id: 'turn_9',
        sessionId: 'chat_1',
        status: 'interrupted',
        resumeAttempts: 0,
        lastError: null,
        startedAt: '2026-10-28T05:00:00Z',
        updatedAt: '2026-10-28T05:01:00Z',
        resumable: true,
      },
    ])
    mockedResumeTurn.mockResolvedValue([])

    renderPage('/chat?session=chat_1')

    fireEvent.click(await screen.findByText('继续这一轮'))

    await waitFor(() => {
      expect(mockedResumeTurn).toHaveBeenCalledWith('chat_1', 'turn_9')
    })
  })
})

describe('ChatPage — 错误条要说出真原因、并给重试入口（v1.22）', () => {
  it('★ 错误条渲染 ApiError 的真实文案（不是泛化的「请求失败」）', async () => {
    /*
      用户看到的是「请求失败，请稍后再试。」—— 一句什么信息都没有的话。
      真因（密钥无效）其实已经到了前端，只是被塞进一个裸对象、丢了类型，
      于是 `errorMessage()` 走到兜底文案。这条钉住"真因必须显示出来"。
    */
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        error: new ApiError(
          'TURN_FAILED',
          '模型接入点拒绝了请求：密钥无效或已过期（HTTP 401）。',
          200,
        ),
      }),
    )
    mockedListTurns.mockResolvedValue([])

    renderPage('/chat?session=chat_1')

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('密钥无效或已过期')
    expect(alert.textContent).not.toContain('请稍后再试')
  })

  it('★ 有 failed 的轮次时，错误条出现可点的「重试」并调 resumeTurn', async () => {
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        error: new ApiError('TURN_FAILED', '密钥无效', 200),
      }),
    )
    mockedListTurns.mockResolvedValue([
      {
        id: 'turn_failed_1',
        sessionId: 'chat_1',
        status: 'failed',
        resumeAttempts: 3,
        lastError: '密钥无效',
        startedAt: '2026-10-28T05:00:00Z',
        updatedAt: '2026-10-28T05:01:00Z',
        resumable: false,
      },
    ])
    mockedResumeTurn.mockResolvedValue([])

    renderPage('/chat?session=chat_1')

    const retry = await screen.findByRole('button', { name: '重试' })
    expect(retry.getAttribute('data-error-retry')).toBe('turn_failed_1')
    fireEvent.click(retry)
    await waitFor(() => {
      expect(mockedResumeTurn).toHaveBeenCalledWith('chat_1', 'turn_failed_1')
    })
  })

  it('★ 没有 failed 的轮次时，错误条不出现「重试」（避免点了没反应）', async () => {
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        error: new ApiError('TURN_FAILED', '密钥无效', 200),
      }),
    )
    mockedListTurns.mockResolvedValue([])

    renderPage('/chat?session=chat_1')

    await screen.findByRole('alert')
    expect(screen.queryByRole('button', { name: '重试' })).toBeNull()
  })
})

describe('ChatPage — 别的会话在跑时当前会话仍能发（v1.65 对齐 CyberStrikeAI）', () => {
  /*
    ★ v1.24 的旧语义（"别的会话在跑就禁止当前会话发"）已在 v1.65 推翻。

    原因：后端从设计上就是**按请求独立并发**（每个轮次自己跑，无全局轮次锁，
    实测两个会话的 turn 同时 running）。CyberStrikeAI 也是请求级独立并发。
    前端老设计用全局 `sending` 把"同时只能发一条"写死，导致用户在一个长会话
    跑着时，切到新建会话发消息被拦下 —— 用户实测「另一个对话正在进行，新开
    一个对话发送不出去，同时只能一个会话运行吗？」。

    v1.65 改成按会话独立：判断"这个会话在不在跑"看 `sendingSessionIds` 是否
    包含它；别会话在跑只给一句提示，**不再拦发消息**。

    （旧 v1.24 真正防的那个事故 —— 把话插进**别的**会话的轮次 —— 仍被防住：
    `turnRunningHere` 只在本会话有轮次时为真，走的一定是正确分支。）
  */
  it('★ 别的会话正在跑 → 当前会话仍能发（按钮是「发送」、可点、发得出去）', async () => {
    mockedSendMessage.mockResolvedValueOnce(true)
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: true,
        sendingSessionId: 'chat_other',
        sendingSessionIds: ['chat_other'],
        phase: { phase: 'thinking', round: 0 },
      }),
    )
    renderPage('/chat?session=chat_1')

    const textarea = screen.getByLabelText('输入消息') as HTMLTextAreaElement
    // 提示只说明"别处也在跑"，但不再说"等它跑完再发"
    expect(textarea.placeholder).toContain('另一个对话')
    fireEvent.change(textarea, { target: { value: '你好' } })

    // 当前会话没在跑 → 按钮是「发送」，不是「插话」
    expect(screen.queryByRole('button', { name: '插话' })).toBeNull()
    const send = screen.getByRole('button', { name: '发送' }) as HTMLButtonElement
    // ★ 关键：不再被禁用（别的会话在跑不阻止当前会话发）
    expect(send.disabled).toBe(false)
    fireEvent.click(send)

    await waitFor(() => {
      // 走的是"发新消息"，不是"插进别的会话"
      expect(mockedSendMessage).toHaveBeenCalledTimes(1)
      expect(mockedInterject).not.toHaveBeenCalled()
    })
  })

  it('★ 服务端说"没在跑"（no-turn）→ 清掉过期状态并按普通消息发出去', async () => {
    mockedInterject.mockResolvedValueOnce('no-turn')
    mockedSendMessage.mockResolvedValueOnce(true)
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: true,
        sendingSessionId: 'chat_1',
        phase: { phase: 'thinking', round: 0 },
      }),
    )
    renderPage('/chat?session=chat_1')

    const textarea = screen.getByLabelText('输入消息') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value: '其实这一轮早结束了' } })
    fireEvent.click(screen.getByRole('button', { name: '插话' }))

    await waitFor(() => {
      // 关键：不能把这句话丢掉 —— 要退回正常发送
      expect(mockedSendMessage).toHaveBeenCalledWith('chat_1', '其实这一轮早结束了')
    })
    expect(mockedClearStaleSending).toHaveBeenCalled()
  })
})

describe('ChatPage — 中断当前步骤（v1.25）', () => {
  /*
    ★ 用户原话：「打断当前会话也没做，看人家 CyberStrikeAI 的设计」。
      插话只排队、不会停当前输出 —— 所以必须有一个真的"按停"入口。
  */
  it('★ 当前会话在跑 → 出现「中断」按钮；点开有三个选项', async () => {
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: true,
        sendingSessionId: 'chat_1',
        phase: { phase: 'thinking', round: 0 },
      }),
    )
    renderPage('/chat?session=chat_1')

    fireEvent.click(await screen.findByRole('button', { name: '中断' }))

    expect(await screen.findByRole('button', { name: '中断并继续' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '彻底停止' })).toBeTruthy()
    expect(screen.getByLabelText('中断说明')).toBeTruthy()
  })

  it('★ 「中断并继续」把说明带给 interruptTurn(continue, note)', async () => {
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: true,
        sendingSessionId: 'chat_1',
        phase: { phase: 'thinking', round: 0 },
      }),
    )
    renderPage('/chat?session=chat_1')

    fireEvent.click(await screen.findByRole('button', { name: '中断' }))
    fireEvent.change(screen.getByLabelText('中断说明'), {
      target: { value: '别扫端口了，先看登录' },
    })
    fireEvent.click(screen.getByRole('button', { name: '中断并继续' }))

    await waitFor(() => {
      expect(mockedInterruptTurn).toHaveBeenCalledWith('chat_1', 'continue', '别扫端口了，先看登录')
    })
  })

  it('★「彻底停止」用 stop 模式', async () => {
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: true,
        sendingSessionId: 'chat_1',
        phase: { phase: 'thinking', round: 0 },
      }),
    )
    renderPage('/chat?session=chat_1')

    fireEvent.click(await screen.findByRole('button', { name: '中断' }))
    fireEvent.click(screen.getByRole('button', { name: '彻底停止' }))

    await waitFor(() => {
      expect(mockedInterruptTurn).toHaveBeenCalledWith('chat_1', 'stop', '')
    })
  })

  it('★ 没在跑的时候**不出现**「中断」按钮（没有可中断的轮次）', async () => {
    mockedUseChat.mockReturnValue(
      chatState({ active: { ...session(), messages: [] }, sending: false }),
    )
    renderPage('/chat?session=chat_1')
    await screen.findByPlaceholderText(/说点什么/)
    expect(screen.queryByRole('button', { name: '中断' })).toBeNull()
  })
})

describe('ChatPage — 进程状态只属于正在跑的那个会话（v1.28）', () => {
  /*
    ★ 用户原话：「显示有问题啊，一个对话正在推理，全部对话都显示在推理」

    `phase` / `pendingUserContent` / `streamingText` / `interjections` 都是
    **store 级全局字段**（同一时刻全站只跑一轮，所以 store 只存一份是对的）。
    但渲染时必须按「这一轮是不是跑在你正看的这个会话里」过滤 —— 否则切到
    任何一个历史会话，底部都挂着「第 7 轮 · 正在执行 run_shell…」，
    读起来像那个对话也在推理（而它其实什么都没做）。
  */

  const busy = (sendingSessionId: string) =>
    chatState({
      active: { ...session(), messages: [] },
      sending: true,
      sendingSessionId,
      phase: { phase: 'tool', round: 6, tool: 'run_shell' },
      pendingUserContent: '别处正在发的那句话',
      streamingText: '别处正在流入的半句话',
      interjections: [message({ id: 'i1', content: '别处的插话' })],
    })

  it('★ 跑在别的会话时，本会话**一条进程状态都不该出现**', () => {
    mockedUseChat.mockReturnValue(busy('chat_2'))
    renderPage('/chat?session=chat_1')

    expect(screen.queryByText(/第 7 轮/), '别处的轮次被画到了本会话').toBeNull()
    expect(document.querySelector('[data-pending-bubble]'), '别处的乐观气泡漏过来了').toBeNull()
    expect(document.querySelector('[data-streaming-bubble]'), '别处的流式正文漏过来了').toBeNull()
    expect(screen.queryByText('插话 · 模型下一轮会看到'), '别处的插话气泡漏过来了').toBeNull()
    // 条数也不能把别处的乐观气泡算进来
    expect(screen.queryByText(/1 条消息/)).toBeNull()
    expect(screen.getByText(/0 条消息/)).not.toBeNull()
  })

  it('★ 但跑在**本会话**时这些必须都在（别为了过滤把它们全关掉）', () => {
    mockedUseChat.mockReturnValue(busy('chat_1'))
    renderPage('/chat?session=chat_1')

    expect(screen.getByText(/第 7 轮/)).not.toBeNull()
    expect(screen.getByText(/run_shell/)).not.toBeNull()
    expect(document.querySelector('[data-pending-bubble]')).not.toBeNull()
    expect(document.querySelector('[data-streaming-bubble]')).not.toBeNull()
    expect(screen.getByText('插话 · 模型下一轮会看到')).not.toBeNull()
    expect(screen.getByText(/1 条消息/)).not.toBeNull()
  })
})

describe('ChatPage — 助手回合的版面（v1.29 metis 形态）', () => {
  /*
    ★ 这一组补的是 v1.29 重写之后**一直没人测过**的那半边：上面全部用例
      渲染的都是用户消息与工具卡，没有一条碰过助手回合的结构。

      重写的判据有三条，每条都在下面钉住：
      ① 正在跑的那一轮**留在原地**（过程在上、正文在下、状态行最底、无页脚）；
      ② 最终答复是**纯文本**，不能套用户气泡的皮；
      ③ 插话只属于正在跑的那个会话，不能飘到历史会话里。
  */

  it('★ 跑着的回合原地变 live：流式正文与阶段行都在回合内，且没有回合页脚', () => {
    mockedUseChat.mockReturnValue(
      chatState({
        active: {
          ...session(),
          messages: [
            message({ id: 'm1', seq: 1, role: 'user', content: '扫一下' }),
            message({
              id: 'm2',
              seq: 2,
              role: 'tool',
              toolName: 'run_shell',
              content: '{"ok":true}',
            }),
          ],
        },
        sending: true,
        sendingSessionId: 'chat_1',
        pendingUserContent: null,
        // round 是 0 起的下标：第 7 轮 = round 6
        phase: { phase: 'thinking', round: 6 },
        streamingText: '半截',
      }),
    )
    renderPage('/chat?session=chat_1')

    const live = document.querySelector(
      '[data-conversation-turn][data-turn-live="true"]',
    ) as HTMLElement | null
    expect(live, '正在跑的这一轮没有在回合上标 live').not.toBeNull()

    const bubble = live!.querySelector('[data-streaming-bubble]') as HTMLElement | null
    expect(bubble, '流式正文没有画在回合里').not.toBeNull()
    // 不是空气泡：流入的半句必须真的在里面
    expect(bubble!.textContent).toContain('半截')

    const status = live!.querySelector('[role="status"]') as HTMLElement | null
    expect(status, '阶段行没有画在回合里').not.toBeNull()
    expect(status!.textContent).toContain('第 7 轮')

    // ★ 这一轮还没结束：页脚上的「N 步 · 时间」现在出现就是在说谎
    expect(live!.querySelector('[data-turn-footer]'), '跑着的回合不该显示页脚').toBeNull()
  })

  it('★ 最终答复是纯文本（不是气泡），并带回合页脚', () => {
    mockedUseChat.mockReturnValue(
      chatState({
        active: {
          ...session(),
          messages: [
            message({ id: 'm1', seq: 1, role: 'user', content: '扫一下' }),
            message({
              id: 'm2',
              seq: 2,
              role: 'assistant',
              surface: 'answer',
              content: '已清点完毕',
            }),
          ],
        },
      }),
    )
    renderPage('/chat?session=chat_1')

    const paragraph = screen.getByText('已清点完毕')
    const row = paragraph.closest('[data-message-row="assistant"]') as HTMLElement | null
    expect(row, '助手正文没有 [data-message-row="assistant"] 标记').not.toBeNull()
    // ★ 气泡皮只属于用户消息；助手正文套上它，长回答就变成一大块灰砖
    expect(row!.className).not.toContain('rounded-lg')
    expect(row!.className).not.toContain('bg-surface')

    const footer = document.querySelector('[data-turn-footer]') as HTMLElement | null
    expect(footer, '回合结束时应该有页脚').not.toBeNull()
    expect(footer!.getAttribute('data-turn-tools')).toBe('0')
    // 回合里只有这一条答复（用户那句属于回合头，不算「步」）
    expect(footer!.getAttribute('data-turn-steps')).toBe('1')
  })

  it('★ 插话只画在正在跑的那个会话里：别的会话在跑时不出现', () => {
    mockedUseChat.mockReturnValue(
      chatState({
        active: {
          ...session(),
          messages: [message({ id: 'm1', seq: 1, role: 'user', content: '扫一下' })],
        },
        sending: true,
        sendingSessionId: 'chat_2',
        pendingUserContent: null,
        interjections: [message({ id: 'i1', seq: 9, role: 'user', content: '别处的插话' })],
      }),
    )
    renderPage('/chat?session=chat_1')

    expect(document.querySelector('[data-interjection]')).toBeNull()
    expect(screen.queryByText(/别处的插话/)).toBeNull()
  })

  it('★ 本会话在跑时，插话画在回合**内部**（下一轮就会读到它）', () => {
    mockedUseChat.mockReturnValue(
      chatState({
        active: {
          ...session(),
          messages: [message({ id: 'm1', seq: 1, role: 'user', content: '扫一下' })],
        },
        sending: true,
        sendingSessionId: 'chat_1',
        pendingUserContent: null,
        interjections: [message({ id: 'i1', seq: 9, role: 'user', content: '别处的插话' })],
      }),
    )
    renderPage('/chat?session=chat_1')

    const turn = document.querySelector('[data-conversation-turn]') as HTMLElement | null
    expect(turn).not.toBeNull()
    const note = turn!.querySelector('[data-interjection]') as HTMLElement | null
    expect(note, '插话没有画在回合里').not.toBeNull()
    expect(note!.textContent).toContain('别处的插话')
    // 只有一条：不能既在回合里又飘在回合外
    expect(document.querySelectorAll('[data-interjection]').length).toBe(1)
  })
})

/*
  ══════════════════════════════════════════════════════════════
  底栏守卫（v1.30）：输入区周围不许再出现「第二层底色」
  ══════════════════════════════════════════════════════════════

  事故：底栏 <div> 用 `bg-gradient-to-t from-canvas via-canvas to-transparent`
  把页面底色又画了一遍。页面背景是被 AppShell 的 bg-canvas、AgentField 的
  压角层、main 的 bg-canvas/80 三次合成出来的 209，而 canvas 是 245 ——
  于是输入卡左右各多出一条 35 级的亮白带，全宽、被卡片跨在中间。

  为什么不在浏览器里量一次像素就完事：那是脚本，不落库，下一个人把背景类
  加回来它不会红。这里把「这一带由谁负责涂色」写成结构约束 ——
    · 底栏自己的 class 恰好四个，一个不多一个不少；
    · 卡片以外的元素一个涂色 token 都不许有（底色/渐变/阴影/描边/滤镜/文字色）；
    · 整棵子树不许有伪元素、内联 style、绝对定位、负值与任意值逃逸；
    · 字号的任意值（text-[180px]）也算 —— 那是用字形把白带画到卡外；
    · 父层兄弟只允许工具条与滚动区（外加带 data-* 的两条状态条）；
    · 祖先层到 body 为止不许自带涂色，页面里不许出现 <style>/<link> 注入。

  代价是有意为之：改动输入区内部结构要同步改这里。这正是这条用例的价值
  —— 涂色这件事必须被显式想一遍，而不是顺手加一个类。
*/
const DOCK_CLASSES = ['shrink-0', 'px-3', 'pb-3', 'pt-4']
const CARD_CLASSES = [
  'mx-auto',
  'w-full',
  'max-w-[880px]',
  'rounded-lg',
  'border',
  'border-line',
  'bg-surface',
  'shadow-[0_6px_22px_-16px_rgba(15,23,42,0.28)]',
  'transition-shadow',
  'duration-200',
  'focus-within:border-line-strong',
  'focus-within:shadow-[0_10px_30px_-18px_rgba(15,23,42,0.34)]',
]
const TOOLBAR_CLASSES = [
  'flex',
  'shrink-0',
  'items-center',
  'gap-tight',
  'border-b',
  'border-line',
  'px-3',
  'py-2',
]
// 会话列里滚动区的外层容器（真正滚动的那层在它内部）。
const SCROLLER_CLASSES = ['relative', 'flex', 'min-h-0', 'flex-1', 'flex-col']

/** Tailwind 的变体前缀以「括号外的第一个冒号」结尾；任意值里的冒号在括号内。 */
function stripVariants(token: string): string {
  let depth = 0
  for (let i = 0; i < token.length; i += 1) {
    const ch = token[i]
    if (ch === '[') depth += 1
    else if (ch === ']') depth -= 1
    else if (ch === ':' && depth === 0) return stripVariants(token.slice(i + 1))
  }
  return token
}

const tokensOf = (el: Element): string[] =>
  (el.getAttribute('class') ?? '').split(/\s+/).filter(Boolean)

/** 所有能把颜色留在屏幕上的 token 词根。 */
const PAINT_ROOTS = [
  'bg-',
  'from-',
  'via-',
  'to-',
  'shadow',
  'ring-',
  'outline-',
  'border-',
  'divide-',
  'fill-',
  'stroke-',
  'backdrop-',
  'blur',
  'brightness',
  'contrast',
  'grayscale',
  'invert',
  'saturate',
  'sepia',
  'mask-',
  'opacity-',
  'mix-blend-',
  'text-[',
  'text-canvas',
  'text-surface',
  'font-[',
  'leading-[',
  'tracking-[',
]

/** 能把绘制挪出自身盒子的写法。 */
const ESCAPE_ROOTS = [
  'absolute',
  'fixed',
  'sticky',
  'inset-',
  'w-screen',
  'h-screen',
  '100vw',
  '100vh',
]

/**
 * 这些写法在输入区里**任何时候**都不合法，包括卡片内部：
 * 任意字号是用字形把颜色画到卡外（text-[180px] + 白色方块），
 * 任意阴影/描边/底色是不经过类名黑名单的整块涂色（shadow-[0_0_0_9999px_…]），
 * 任意属性 token（[background:#F4F5F7]）则是直接写 CSS。
 */
const ALWAYS_BANNED = [
  'text-[',
  'font-[',
  'leading-[',
  'tracking-[',
  'shadow-[',
  'ring-[',
  'outline-[',
  'bg-[',
  'border-[',
  'from-[',
  'via-[',
  'to-[',
  'fill-[',
  'stroke-[',
]

/**
 * @param allowPaint 卡片内部允许涂色（那里本来就有合法的浅底按钮、主色发送键）；
 *   但逃逸、伪元素、内联 style、任意值在任何位置都不允许。
 */
function guardViolations(el: Element, allowPaint: boolean, isCard = false): string[] {
  const found: string[] = []
  for (const token of tokensOf(el)) {
    const core = stripVariants(token)
    if (core.startsWith('[')) {
      found.push(token)
      continue
    }
    if (/^(before|after|content):/.test(token) || core.startsWith('content-')) found.push(token)
    if (core.startsWith('-')) found.push(token)
    if (ESCAPE_ROOTS.some((root) => core === root || core.startsWith(root))) found.push(token)
    // 卡片的 class 已被第一条断言钉成精确集合；这里不再重复报它的两层阴影。
    if (!isCard && ALWAYS_BANNED.some((root) => core === root || core.startsWith(root))) {
      found.push(token)
    }
    if (!allowPaint && PAINT_ROOTS.some((root) => core === root || core.startsWith(root))) {
      found.push(token)
    }
  }
  return found
}

function walkElements(root: Element): Element[] {
  const out: Element[] = []
  const visit = (el: Element) => {
    out.push(el)
    for (const child of Array.from(el.children)) visit(child)
  }
  visit(root)
  return out
}

function isInsideCard(el: Element, card: Element): boolean {
  return el === card || card.contains(el)
}

describe('ChatPage — 输入区单行形态（v1.75）', () => {
  function renderComposerOnly() {
    mockedUseChat.mockReturnValue(chatState({ active: { ...session(), messages: [] } }))
    return renderPage('/chat?session=chat_1')
  }

  it('★ 设置默认收起，点齿轮才展开，点「完成」收起', () => {
    const { container } = renderComposerOnly()
    expect(container.querySelector('[data-composer-settings]')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '对话设置' }))
    expect(container.querySelector('[data-composer-settings]')).not.toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '完成' }))
    expect(container.querySelector('[data-composer-settings]')).toBeNull()
  })

  it('★ 输入框是单行，且与附件按钮在同一行', () => {
    renderComposerOnly()
    const box = screen.getByLabelText('输入消息') as HTMLTextAreaElement
    expect(box.getAttribute('rows')).toBe('1')

    const attach = screen.getByRole('button', { name: '附件' })
    const row = attach.closest('.flex.items-end')
    expect(row).not.toBeNull()
    expect(row?.contains(box)).toBe(true)
  })
})

describe('ChatPage — 输入区周围不许出现第二层底色（v1.30）', () => {
  function renderComposer(): HTMLElement {
    mockedUseChat.mockReturnValue(
      chatState({
        active: {
          ...session(),
          messages: [message({ id: 'm1', seq: 1, role: 'user', content: 'hi' })],
        },
      }),
    )
    renderPage('/chat?session=chat_1')
    return screen.getByTestId('composer-dock')
  }

  it('★ 底栏与卡片的 class 都是精确集合（多一个涂色 token 就红）', () => {
    /*
      最直接的回归面：事故就是把三个背景类加在这里。这条用精确集合
      而不是「不含 bg-gradient」的黑名单 —— 黑名单永远漏下一个变体
      （[background:#F4F5F7]、before:bg-canvas、border-[999px]）。
      卡片同理：它的两层阴影是唯一允许的任意值，换一个就红 ——
      否则 shadow-[0_0_0_9999px_#F4F5F7] 能用合法类把整条白带铺回来。
    */
    renderComposer()
    const dock = screen.getByTestId('composer-dock')
    const card = screen.getByTestId('composer-card')
    expect(tokensOf(dock).sort()).toEqual([...DOCK_CLASSES].sort())
    expect(tokensOf(card).sort()).toEqual([...CARD_CLASSES].sort())
  })

  it('★ 卡片以外：一个涂色 token 都不许有', () => {
    const dock = renderComposer()
    const card = screen.getByTestId('composer-card')
    const offenders: string[] = []
    for (const el of walkElements(dock)) {
      if (isInsideCard(el, card)) continue
      for (const hit of guardViolations(el, false)) offenders.push(`${el.tagName}.${hit}`)
    }
    expect(offenders).toEqual([])
  })

  it('★ 整棵子树：没有伪元素、没有内联 style、没有逃逸写法', () => {
    /*
      这三样是「卡内元素把白带画到卡外」的通道：
        before:absolute before:inset-x-[-500px] before:bg-canvas（伪元素）
        style={{ background: '#F4F5F7' }}（内联）
        absolute -inset-x-[100vw] / -mt-[200px]（逃逸）
      卡片内部允许涂色，但「涂在自己盒子里」这一条对它同样成立。
    */
    const dock = renderComposer()
    const card = screen.getByTestId('composer-card')
    const offenders: string[] = []
    for (const el of walkElements(dock)) {
      for (const hit of guardViolations(el, true, el === card)) {
        offenders.push(`${el.tagName}.${hit}`)
      }
      if (el.hasAttribute('style')) offenders.push(`${el.tagName}[style]`)
    }
    expect(offenders).toEqual([])
  })

  it('★ 父层兄弟只允许工具条与滚动区，底栏必须是最后一个', () => {
    /*
      只在底栏身上设防是不够的：在会话列里另起一个兄弟 div，写
      `absolute right-0 bottom-0 h-32 w-[136px] bg-canvas`，就能把
      同一條白带画回来，而底栏自己一个类都没多。
    */
    const dock = renderComposer()
    const column = dock.parentElement
    expect(column).toBeTruthy()
    const children = Array.from(column!.children)
    expect(children[children.length - 1]).toBe(dock)
    const allowed = [
      { classes: TOOLBAR_CLASSES, status: null },
      { classes: SCROLLER_CLASSES, status: null },
    ]
    const rest = children.filter((child) => child !== dock)
    expect(rest.length).toBeGreaterThanOrEqual(2)
    expect(tokensOf(rest[0]).sort()).toEqual([...allowed[0].classes].sort())
    expect(tokensOf(rest[1]).sort()).toEqual([...allowed[1].classes].sort())
    // 第三类只允许「带 data-* 的状态条」：错误条与中断条，其余一律红。
    for (const extra of rest.slice(2)) {
      expect(extra.hasAttribute('data-error-code') || extra.hasAttribute('data-stuck-turn')).toBe(
        true,
      )
      for (const hit of guardViolations(extra, true)) {
        throw new Error(`状态条上出现了额外绘制：${extra.tagName}.${hit}`)
      }
    }
  })

  it('★ 祖先层到 body 为止不许自带涂色', () => {
    const dock = renderComposer()
    const offenders: string[] = []
    let cur: Element | null = dock.parentElement
    while (cur && cur !== document.body) {
      for (const hit of guardViolations(cur, false)) offenders.push(`${cur.tagName}.${hit}`)
      if (cur.hasAttribute('style')) offenders.push(`${cur.tagName}[style]`)
      cur = cur.parentElement
    }
    expect(offenders).toEqual([])
  })

  it('★ 页面里没有 <style>/<link rel=stylesheet> 注入', () => {
    /*
      类名守卫看不见 CSSOM：一个指向祖先 ::before 的 <style> 就能重画白带。
      测试环境本身没有样式表（index.css 由入口文件引入，不在这里），
      所以「0 条」就是基线。
    */
    renderComposer()
    expect(document.querySelectorAll('style').length).toBe(0)
    expect(document.querySelectorAll('link[rel="stylesheet"]').length).toBe(0)
    expect(document.styleSheets.length).toBe(0)
  })
})
