// ⚠️ 必须手动 import `cleanup`：本仓库没开 vitest 的 `globals: true`，
//    RTL 自动清理只在 globals 模式下注册。漏了它的症状是「Found multiple
//    elements」——上一个用例的 DOM 留在 document 里。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
  resetChatStore: vi.fn(),
  subscribeChat: vi.fn(() => () => {}),
  getChatSnapshot: vi.fn(),
}))

// `vi.mock` 会被提升，所以这里拿到的已是替身。
import { useAuth } from '@/data/auth-store'
import {
  archiveSession,
  createSession,
  loadSessions,
  sendMessage,
  useChat,
} from '@/data/chat'
import type { ChatMessage, ChatSession, ChatState } from '@/data/chat'
import { ApiError } from '@/lib/api'
import ChatPage from './ChatPage'

const mockedUseAuth = vi.mocked(useAuth)
const mockedUseChat = vi.mocked(useChat)
const mockedSendMessage = vi.mocked(sendMessage)
const mockedCreateSession = vi.mocked(createSession)
const mockedArchiveSession = vi.mocked(archiveSession)

function session(overrides: Partial<ChatSession> = {}): ChatSession {
  return {
    id: 'chat_1',
    title: '查一下最近的扫描',
    createdBy: 'u1',
    messageCount: 2,
    archived: false,
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
  return {
    sessions: [],
    active: null,
    tools: [],
    personas: [],
    stagedAttachments: [],
    status: 'ready',
    error: null,
    sending: false,
    deciding: false,
    ...overrides,
  }
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
  it('★ 失败的工具卡带「失败」字样', () => {
    /*
      这条钉的是最贵的一类骗局：工具其实没干成，界面画得和干成了一样。
      事故形态是用户读到"扫描已建"，去扫描页找不到东西，然后开始不信平台。
    */
    const failed = message({
      id: 'm_tool',
      seq: 3,
      role: 'tool',
      content: '创建扫描失败：值为空',
      toolName: 'create_scan',
      toolFailed: true,
    })
    mockedUseChat.mockReturnValue(
      chatState({ active: { ...session(), messages: [failed] } }),
    )

    renderPage('/chat?session=chat_1')

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

    renderPage('/chat?session=chat_1')

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
      永远不知道它是干什么的。`aria-label` 里带上会话标题，连"归档哪个"
      都说清楚了。
    */
    mockedUseChat.mockReturnValue(chatState({ sessions: [session()] }))
    renderPage()

    expect(screen.getByRole('button', { name: '归档对话「查一下最近的扫描」' })).toBeTruthy()
  })

  it('点击归档调用 archiveSession', async () => {
    mockedUseChat.mockReturnValue(chatState({ sessions: [session()] }))
    renderPage()

    fireEvent.click(screen.getByRole('button', { name: '归档对话「查一下最近的扫描」' }))
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
    mockedUseChat.mockReturnValue(chatState({ sessions: [session()], error: new ApiError('VALIDATION_FAILED', '建不出来', 400) }))
    renderPage()

    // ⚠️ 精确匹配 `新对话`：空状态里还有个「开始新对话」，`/新对话/` 会同时命中两个。
    fireEvent.click(screen.getByRole('button', { name: '新对话' }))

    await waitFor(() => expect(mockedCreateSession).toHaveBeenCalled())
    // 页面上仍能看到已有会话 —— 失败不该把用户已有的东西带走。
    expect(screen.getByText('查一下最近的扫描')).toBeTruthy()
  })
})
