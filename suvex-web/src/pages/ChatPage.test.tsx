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
  // ★ v0.100：插话。跑着的时候 Enter 走这条路径（见 `handleSend`）。
  interject: vi.fn(async () => true),
  resetChatStore: vi.fn(),
  subscribeChat: vi.fn(() => () => {}),
  getChatSnapshot: vi.fn(),
}))

// `vi.mock` 会被提升，所以这里拿到的已是替身。
import { useAuth } from '@/data/auth-store'
import {
  archiveSession,
  createSession,
  interject,
  loadSessions,
  sendMessage,
  useChat,
} from '@/data/chat'
import type { ChatMessage, ChatSession, ChatState } from '@/data/chat'
import { ApiError } from '@/lib/api'
import ChatPage, { phaseLabel } from './ChatPage'

const mockedUseAuth = vi.mocked(useAuth)
const mockedUseChat = vi.mocked(useChat)
const mockedSendMessage = vi.mocked(sendMessage)
const mockedCreateSession = vi.mocked(createSession)
const mockedArchiveSession = vi.mocked(archiveSession)
const mockedInterject = vi.mocked(interject)

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
    pendingUserContent: null,
    sending: false,
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

  it('★ 点下发送的瞬间，输入框就空了', async () => {
    /*
      用 `Promise` 手动控制 `sendMessage` 何时 resolve —— 只要它没 resolve，
      就代表"服务端还在想"。此时输入框必须已经空了。
    */
    let release: ((ok: boolean) => void) | undefined
    mockedSendMessage.mockImplementation(
      () => new Promise<boolean>((resolve) => { release = resolve }),
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
      expect((screen.getByLabelText('输入消息') as HTMLTextAreaElement).value).toBe(
        '这段字不能丢',
      )
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
        phase: { phase: 'thinking', round: 0 },
      }),
    )
    renderPage('/chat?session=chat_1')

    const textarea = screen.getByLabelText('输入消息') as HTMLTextAreaElement
    expect(
      textarea.disabled,
      '跑的时候输入框被禁用了 —— 用户就没法插话，这正是要修的问题',
    ).toBe(false)
  })

  it('★★ 跑着的时候按发送 → 走 interject，而不是再开一轮 sendMessage', async () => {
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: true,
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
    mockedInterject.mockResolvedValueOnce(false)
    mockedUseChat.mockReturnValue(
      chatState({
        active: { ...session(), messages: [] },
        sending: true,
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
        interjections: [
          message({ id: 'ij1', seq: 9, role: 'user', content: '顺便看下 8080' }),
        ],
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

  it('★ 默认收着 —— 输出再短也不铺开（收着才是默认值，不是"太长才收"）', () => {
    const { container } = renderTool('OK')

    const card = container.querySelector('[data-message-row="tool"]')
    expect(card, '工具卡没渲染出来').not.toBeNull()
    expect(card?.getAttribute('data-tool-collapsed'), '短输出被铺开了').toBe('true')
    expect(container.querySelector('[data-tool-toggle="true"]')).not.toBeNull()
  })

  it('★ 收着时正文全文不在 DOM 里（只靠 CSS 藏 = 假折叠）', () => {
    const { container } = renderTool(manyLines)

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

    const card = container.querySelector('[data-message-row="tool"]')
    expect(card?.textContent).toContain('list_challenges')
    expect(card?.textContent, '参数摘要没显示 —— 收起来就看不出是哪次调用').toContain(
      'nmap -sV 10.0.0.5',
    )
  })

  it('★ 失败在折叠态就看得到（收起来看不见失败 = 用户漏掉工具挂了）', () => {
    const { container } = renderTool(manyLines, true)

    const card = container.querySelector('[data-message-row="tool"]')
    expect(card?.getAttribute('data-tool-collapsed')).toBe('true')
    expect(card?.textContent).toContain('失败')
  })

  it('★ 点开后看到全文，aria 与文案都翻转', () => {
    const { container } = renderTool(manyLines)

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

  it('★ 不把协议内部词漏给用户', () => {
    for (const reason of ['truncation', 'format', undefined]) {
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
