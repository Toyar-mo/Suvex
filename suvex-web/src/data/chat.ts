import { ApiError, api, toApiError } from '@/lib/api'
import { activeLocale } from '@/i18n'
import { useSyncExternalStore } from 'react'

import {
  TURN_AWAITING,
  TURN_DELTA,
  TURN_DONE,
  TURN_ERROR,
  TURN_MESSAGE,
  TURN_PHASE,
  TURN_TOOL_OUTPUT,
  TURN_TOOL_PROGRESS,
  asChatMessage,
  streamTurn,
  type PhasePayload,
} from './chat-stream'

/**
 * 对话页的会话与消息（v0.93）。
 *
 * ## 为什么这是一个 store，而不是页面里的 useState
 *
 * 会话列表要跨页面存活：用户在对话里让模型建了一次扫描，然后点侧栏去
 * 「扫描任务」看它，再点回来 —— 回来时列表还在，不用重新拉。
 * 更重要的是**消息要能续**：同一轮对话里模型连调 3 个工具，中间任何
 * 一步都不该因为重渲染丢上下文。
 *
 * ## 与 `data/skills.ts` 的结构差异（故意不同）
 *
 * 技能库是"读一份清单 + 一份统计"，两个都只读、都在进页面时拉一次。
 * 对话是**写的**：发一条消息会往后追加消息，且追加的是**服务端决定**的
 * 那几条（模型可能回一条文本 + 三张工具卡）。所以这里不做乐观更新 ——
 * 乐观更新意味着前端要预测"服务端会追加几条、什么角色"，那等于把
 * engine 的逻辑复制一份到前端，而它必然会漂。
 *
 * 发消息时界面显示"正在想"，等服务端返回再整体落进 list。
 *
 * ## ⚠️ 一次只允许一轮在跑
 *
 * `sending` 是全局布尔而不是按会话：同一时刻只发一条。
 * 允许并发发两条会让 `seq` 的顺序取决于谁先返回，而列表按 seq 排序 ——
 * 于是消息会**乱序**显示。这不是限制用户，是限制一个本来就没定义的行为。
 */

/** 消息角色。与后端 `CHAT_ROLES` 同源。 */
export type ChatRole = 'user' | 'assistant' | 'tool'

/**
 * 一条消息。
 *
 * ★ `content` 是**已经剥掉动作协议**的正文（后端 `_narration_before_action`
 *   处理过）。前端不需要、也不应该自己去找 `ACTION:` —— 那会把"内部协议
 *   处理"这件事复制一份到浏览器里。
 */
export interface ChatMessage {
  id: string
  seq: number
  role: ChatRole
  content: string
  /** 模型的思考过程。有就显示在一块可折叠区里，不进正文。 */
  reasoning?: string | null
  /** 工具卡：角色是 `tool` 时才有。 */
  toolName?: string | null
  toolArgs?: Record<string, unknown> | null
  /** 工具是否失败。`null`/缺省 = 成功或不是工具消息。 */
  toolFailed?: boolean | null
  /** 工具执行耗时（毫秒）。老数据为 null —— 不显示，而不是显示 0ms。 */
  durationMs?: number | null
  createdAt: string
  /**
   * ★ v1.25：这条消息属于哪一面。
   *
   * - `answer`（默认）：对话内容 —— 用户自己的话、模型的最终答复，画成气泡。
   * - `process`：过程证据 —— 边干边说的旁白、工具结果，收进「过程详情」折叠块。
   *
   * 老数据/老后端没有这个字段 → 当作 `answer`，行为不变。
   */
  surface?: 'answer' | 'process'

  /**
   * 审批状态。`null`/缺省 = 这条消息与审批无关。
   *
   * ★ `'pending'` 时工具**还没执行过**，`content` 里写的是"等待确认"。
   *   界面必须据此渲染确认按钮，而不是渲染成一张普通工具卡 ——
   *   那样用户会以为它已经跑过了。
   */
  approvalStatus?: 'pending' | 'approved' | 'rejected' | null
  approvalDecidedBy?: string | null
  approvalDecidedAt?: string | null
}

/** 会话元信息（不含消息）。 */
export interface ChatSession {
  id: string
  title: string
  createdBy: string
  createdAt: string
  updatedAt: string
  messageCount: number
  archived: boolean
  /**
   * 是否置顶。
   *
   * ★ 列表**已经**按「置顶在前、其余按最近更新」排好序（后端
   *   `service.list_sessions` 的 order_by 就是最终顺序）。这个字段只用来
   *   画条目上那个小旗子、以及切换菜单文案 —— 前端**不要**再自己排一次，
   *   否则判据就有两份，迟早漂。
   */
  pinned?: boolean

  /**
   * ★ v1.66：这一会话此刻是否**正在跑**（服务端有 status='running' 的轮次）。
   *
   * 侧栏条目据此画一个转圈点 + 「运行中」——这样一眼能看出哪个会话在跑，
   * 而不是在聊天区底部画一条带轮次/工具数的常驻蓝条（用户实测不想要那个，
   * CyberStrikeAI 也没那条）。跨刷新/跨浏览器权威：来自服务端列表接口，
   * 不是本地 SSE 布尔。
   */
  running?: boolean

  /**
   * 关联的 CTF 题目 id（从看板「查看」进来时写的）。`null`/缺失 = 普通对话。
   *
   * ★ 会话与题目是**一对一**：同一道题再点一次「查看」会拿回同一个会话
   *   （服务端做 find-or-create），所以这个字段也是一个稳定的事实。
   */
  challengeId?: string | null

  /** 子代理会话的父会话；null/缺失 = 顶层会话。 */
  parentSessionId?: string | null

  // ── v0.94 的六项「对话设置」 ──────────────────────────────
  //
  // ★ 它们出现在**列表**响应里（不只是详情）：侧栏要按 `project` 分组、
  //   要在条目上标出人格/审批状态。为三个小字段让列表逐条再请求详情
  //   是浪费 —— 而侧栏是每次进页面都要渲染的。
  /** 归属项目。`null` = 无项目。 */
  project?: string | null
  /** 人格 id。`null` = 默认。 */
  persona?: string | null
  /** 模型接入点 id。`null` = 平台默认；逐条消息可以用 `providerId` 覆盖。 */
  providerId?: string | null
  /** 有副作用的工具是否要人工确认。 */
  requireApproval?: boolean
  /** 自动放行截止时间；null/过去 = 关闭。 */
  autoApproveUntil?: string | null
  /** 挂载的技能名。`null`/空 = 没挂。 */
  skillNames?: string[] | null
  /** 挂载的 MCP server 名。 */
  mcpServerNames?: string[] | null
}

/** 供界面渲染的一种人格。 */
export interface ChatPersona {
  id: string
  label: string
  summary: string
}

/** 已上传、还没随消息发出去的附件。 */
export interface ChatAttachment {
  id: string
  name: string
  sizeBytes: number
}

/** `GET /chat/sessions/{id}` 的响应。 */
/**
 * 会话关联题目的只读投影（对话页顶部那条「题目条」用）。
 *
 * ★ 只给五项：标题 / 状态 / 分值 / 候选 / 已确认。题面不在这里 ——
 *   题目条是一条常驻的窄条，塞题面只会把它撑高，而读题面该去看板或题解。
 */
export interface ChatChallengeRef {
  id: string
  title: string
  status: string
  score: number | null
  remoteId: string
  platformName: string
  candidateFlag: string | null
  confirmedFlag: string | null
  /** 这一轮解题的 scan id —— 对话页用它读过程事件；`null` = 还没开始解 */
  scanId: string | null
}

export interface ChatSessionDetail extends ChatSession {
  messages: ChatMessage[]
  /**
   * 这份消息是不是"最后一页"（前面还有更早的）。
   *
   * ★ 由**服务端**按 `messages[0].seq > 1` 给出，前端不要用
   *   `messages.length < messageCount` 自己推 —— seq 有空洞时会算错，
   *   而算错的表现是「加载更早」要么永远出现、要么永远不出现。
   */
  hasEarlier?: boolean
  /**
   * 这道题（会话由看板「查看」打开时才有）。
   *
   * ⚠️ `null` 有两种意思：普通对话，**或者题目已被删**。题目条据此
   *   前者不渲染、后者写一句"题目已不存在"—— 把后者的整条藏掉，
   *   会让用户以为这段对话从来没关联过题目。
   */
  challenge?: ChatChallengeRef | null
}

/** 一页消息的条数。够填满几屏，又不至于让首屏下载几百 KB。 */
const MESSAGE_PAGE_SIZE = 200

/** `GET /chat/tools` 的一条。 */
export interface ChatTool {
  name: string
  label: string
  description: string
  params: string
  scope: string | null
  /**
   * 按**当前身份**算出来的可用性。
   *
   * ★ 不要在前端按 scope 自己算：判据在后端（`engine._need_scope`），
   *   两处不一致会让界面说"能"而执行时说"不能"。
   */
  available: boolean
}

export type ChatStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface ChatState {
  sessions: ChatSession[]
  /**
   * 已归档的会话（`GET /chat/sessions?includeArchived=true` 减去未归档的）。
   *
   * ★ 与 `sessions` **分两份存**，不是一份再过滤：侧栏的「已归档」是一个
   *   折叠区，只在用户展开时才需要；混进主列表就得在十几个渲染点各过滤
   *   一次，漏一处就会让归档的对话冒到主列表里（那正是归档想避免的）。
   */
  archivedSessions: ChatSession[]
  /** 当前打开的会话；`null` = 还没选（欢迎屏）。 */
  active: ChatSessionDetail | null
  tools: ChatTool[]
  /** 可选人格（进页面拉一次即可，内容只随代码变）。 */
  personas: ChatPersona[]
  /**
   * 当前会话里已上传、还没发出去的附件。
   *
   * ★ 它跟着**会话**而不是跟着输入框：切走再切回来时，用户传的那几个
   *   文件还在（后端确实还留着，见 `attachments.STAGED_TTL_SECONDS`）。
   *   放在页面状态里会让切换会话把附件"藏起来"，而用户会以为传丢了。
   */
  stagedAttachments: ChatAttachment[]
  status: ChatStatus
  error: ApiError | null
  /**
   * 刚发出去、服务端还没回的那句话（v0.97）。
   *
   * ★ 为什么要有它：`sendMessage` 是**整轮一次 commit** —— 服务端返回的
   *   `messages` 里同时包含用户那句话和模型的全部回复，所以用户点发送后，
   *   在他自己那句话上屏幕上要等整整一轮（模型想 + 可能十几次工具调用）。
   *   实测一次能到 30 秒以上，期间界面只有一个小转圈，而输入框里的字
   *   还在（清空也排在 await 之后）—— 用户看到的是"我点了发送，什么都没
   *   发生"，然后就再点一次。
   *
   * ★ 为什么不直接往 `messages` 里插一条假消息：那需要伪造 `id` 与 `seq`，
   *   而真消息回来后要按 `id` 去替换 —— 一旦替换失败（后端改了 id 规则、
   *   或两次发送的乐观 id 撞了），界面上会留下一条永远消不掉的假气泡。
   *   存成独立字段就没有这个失败模式：服务端回包一到，把它置空即可，
   *   真消息由服务端数据自己负责。
   *
   * ★ 渲染位置固定在"已有消息之后、正在想之前"—— 那正是它在时间上的位置。
   */
  pendingUserContent: string | null
  /** 一轮正在跑。界面据此禁用输入框并显示"正在想"。 */
  sending: boolean
  /**
   * ★ v1.24：**哪一个会话**在跑（`null` = 没有在跑）。
   *
   * ## 为什么 `sending` 一个布尔不够
   *
   * `sending` 是全局的（"同一时刻只允许一轮"），但界面必须区分
   * 「跑的是**当前这个**会话」还是「跑的是**另一个**会话」：
   *   · 前者 → 输入框变成「插话」，Enter 往那一轮里塞话；
   *   · 后者 → 这里根本没有在跑的轮次，**绝不能**变成插话 —— 插进去的
   *     那句话不会被任何一轮读到（没有轮次在读历史），用户会看到一条
   *     「插话 · 模型下一轮会看到」的僵尸气泡 + 永久「模型在推理…」。
   *
   * 实测事故：会话 A 的长任务在跑（78 秒），用户切到新建的会话 B 打「你好」
   * 按 Enter → 走了 `interject` → 服务端 200 并落了一条 user 消息 → B 里
   * 永远没有回复。
   */
  sendingSessionId: string | null
  /**
   * ★ v0.100：这一轮**现在在做什么**（`thinking` / `tool`）。
   *
   * ## 为什么它必须与 `sending` 分开
   *
   * `sending` 只回答"有没有在跑"。而用户盯着屏幕的那几十秒想问的是
   * 「它到底在推进还是卡住了」—— 那需要知道是第几轮、是不是在跑某个工具。
   * 合成一个布尔量就只能显示一个没有信息的转圈。
   *
   * `null` = 没有在跑。
   */
  phase: PhasePayload | null
  /** ★ v1.92：运行中工具的输出增量（仅界面展示，不落库）。 */
  liveToolOutput: { tool: string; text: string } | null
  /** ★ v1.92：长工具心跳（MCP / verify_http …），用于运行卡的状态行。 */
  liveToolProgress: {
    tool: string
    seconds: number
    message?: string
    progress?: number
    total?: number
  } | null
  /**
   * ★ v0.100：跑的过程中**用户插的话**（还没被模型看到的那几句）。
   *
   * ## 为什么它们不能直接进 `messages`
   *
   * 插话是真实落库的消息，但它插在**正在跑的那一轮**中间 ——
   * 而那一轮的 `messages` 由服务端在结束时整体给出。两边同时往里写
   * 会撞 id / 顺序。所以：插话先显示在这里（用户能看见自己的话进去了），
   * 那一轮结束后由 `loadActiveSession` 从服务端拿真消息，这里清空。
   *
   * 这也是为什么插话端点**返回**那条消息：界面用它立刻显示，
   * 而不用等下一轮刷新。
   */
  interjections: ChatMessage[]
  /**
   * ★ v0.102：**正在流入的那段正文**（还没落库，模型还在写）。
   *
   * ## 为什么它是一个独立字段，不进 `messages`
   *
   * 与 `pendingUserContent` 同一条理由（见上）：往 `messages` 里插一条
   * 假消息要伪造 `id`/`seq`，而真消息回来后要按 id 替换 —— 替换失败就会
   * 留下一条永远消不掉的假气泡。
   *
   * 更重要的：这段文字**每来一个增量就变一次**。真让它进 `messages`，
   * 每一帧都会触发整个消息列表的重渲染与滚动计算，而列表里其它消息
   * 一个字节都没变。
   *
   * ## 谁负责清它
   *
   * 收到 `message` 帧（该轮的 assistant 正文落库了）→ 置 `null`，
   * 由那条真消息接管显示。`finally` 里也置 `null`（失败路径）。
   *
   * ⚠️ 不要"收到 done 才清"：一轮里可能有多次模型调用（工具往返），
   * 每次落库的正文都该立刻接替流式缓冲，否则工具往返之间那段文字
   * 会在缓冲和真消息里各显示一次。
   */
  streamingText: string | null
  /** 有一条审批正在提交。界面据此禁用那两个按钮，防重复点。 */
  deciding: boolean
  /**
   * ★ v1.61：**服务端**说当前会话这一轮跑到哪了（轮询得来，见
   * `pollTurnProgress`）。
   *
   * ## 为什么要在 `sending` / `phase` 之外再加一个
   *
   * `sending` 是**本浏览器**握着 SSE 流时的本地布尔，`phase` 是那条流里
   * 推来的瞬时状态。两者都只在一件事成立时有值：这条 SSE 还活着。
   *
   * 可服务端从 v1.54 起**断开不取消**：刷新页面、切会话、done 帧丢失，
   * 那一轮都还在后台跑。此时 `sending=false`、`phase=null`，界面认为
   * "没在跑"—— 而它明明在跑。用户原话「服务端在后台跑、前端没有显示，
   * 我不知道进行得怎么样」说的就是这个。
   *
   * 所以本页在没有 SSE 时**轮询** `/chat/sessions/{id}/turns`，把最新一条
   * `running` 的 round/tools/heartbeat 存到这里，界面据此画"仍在推进"。
   * 照抄 CyberStrikeAI 的 resumable polling（跑着时 1.5s 一次）。
   *
   * `null` = 没有在轮询 / 服务端没有在跑的轮次。
   */
  serverTurn: ChatServerTurn | null
  /**
   * ★ v1.62：用户已请求「彻底停止」，但服务端这一轮还没真正停下
   * （典型情况：正卡在一个跑着的工具里 —— 子进程没法中途打断，
   * 要等它返回，见 `interrupts.py` 的「时间点」说明）。
   *
   * ## 为什么需要它
   *
   * `stop` 会立刻把本地 `sending` 清掉（"真的停了"的乐观假设）。但服务端
   * 可能还在跑工具：轮询随后又发现 `serverTurn` 仍 `running`，于是界面把
   * 刚才那条「服务端仍在后台推进…可以点『中断』停下」的条**又画了回来** ——
   * 用户刚点了「彻底停止」，却看到界面像是没收到、还劝他"再点一次中断"。
   * 实测（v1.61 线上，`sleep 90` 场景）就是这样，会让人以为停止按钮坏了。
   *
   * 所以单独记一个"已请求停止"的标记：轮次还在跑时，进度条改说
   * 「已请求停止 · 等当前工具返回后这一轮就结束」，不再劝他重复点中断。
   * 等服务端真的不再 `running`（轮询返回 false），这个标记一并清掉。
   */
  stopRequested: boolean
  /**
   * ★ v1.65：**本浏览器**正在跑的会话集合（按会话独立并发）。
   *
   * ## 为什么需要它（对齐 CyberStrikeAI）
   *
   * 后端从设计上就是**按请求独立**跑：每个轮次自己开会话、自己跑，不存在
   * 「全局同时只允许一轮」的锁 —— 实测同一时刻能有两个会话的轮次并行
   * （`chat_turn_runs` 里两条 status=running 同时存在）。CyberStrikeAI 也是
   * 请求级独立并发（Go/Gin，每个 HTTP 请求跑自己的 agent 循环）。
   *
   * 但前端老设计用一个全局 `sending` 布尔把"同时只能发一条"写死了，于是
   * 用户在一个长会话跑着时，切到新建的会话发消息会被前端拦下（`canSend`
   * 要求 `!sendingElsewhere` 且 `sendMessage` 里 `if (state.sending) return
   * false`）—— 用户原话「另一个对话正在进行，新开一个对话发送不出去」。
   *
   * ## 语义
   *
   * 这是**唯一事实来源**：某个会话在不在跑，看它是否在这个集合里。
   * `sending` / `sendingSessionId` / `phase` / `streamingText` 这些**展示字段**
   * 只描述"用户此刻正在看的那个会话的流"，是它的投影；切到别的会话时它们
   * 由该会话自己的帧/轮询重建（`onEvent` 已按 `active.id === sessionId` 过滤，
   * 别的会话的帧不会污染当前展示）。
   *
   * 这样：A 跑着 → 用户切到 B → B 不在集合里 → 可以正常发 → B 也进集合 →
   * A、B 并行；切回 A，A 的 `sendingHere` 由「A 在集合里」为真判定，
   * 展示靠轮询/刷新接回。
   */
  sendingSessionIds: string[]
  /**
   * ★ v1.68（对齐 CyberStrikeAI）：最近一次「中断 / 继续」操作的结果。
   *
   * ## 为什么需要它
   *
   * 用户原话「我按了中断继续，没有反馈」。根因不一而足：
   *  - 那一轮其实已经结束了 → 后端返回 409 `NO_RUNNING_TURN`（"这个对话当前
   *    没有在跑的回复，不需要中断"），而旧界面只把错误塞进全局 `error`，弹窗
   *    原地不动、看不到任何变化 —— 用户以为"点了没反应"；
   *  - `continue` 时服务端只是**投递一个信号**（下一个检查点才生效），点完这一
   *    瞬间界面也没有明确回执。
   *
   * 所以把每次中断的结果单独记在这里，弹窗直接读它显示：成功→「已请求中断并
   * 继续，正在等待模型响应…」；失败→如实显示服务端那句话（尤其是 409 "不需要
   * 中断"这种最容易被误当成"没反馈"的情况）。
   *
   * `null` = 本次弹窗还没点过。弹窗重新打开时清空。
   */
  lastInterrupt: { mode: 'continue' | 'stop'; ok: boolean; message: string } | null
}

/** v1.61：轮询拿到的"服务端这一轮跑到哪了"。 */
export interface ChatServerTurn {
  runId: string
  roundIndex: number
  toolsUsed: number
  /** ISO 时间；`null` = 服务端没给（老后端）。 */
  heartbeatAt: string | null
}

const EMPTY: ChatState = {
  sessions: [],
  archivedSessions: [],
  active: null,
  tools: [],
  personas: [],
  stagedAttachments: [],
  status: 'idle',
  error: null,
  pendingUserContent: null,
  sending: false,
  sendingSessionId: null,
  phase: null,
  liveToolOutput: null,
  liveToolProgress: null,
  interjections: [],
  streamingText: null,
  deciding: false,
  serverTurn: null,
  stopRequested: false,
  sendingSessionIds: [],
  lastInterrupt: null,
}

let state: ChatState = EMPTY

type Listener = () => void
const listeners = new Set<Listener>()
function emit() {
  for (const listener of listeners) listener()
}

/**
 * 只改指定的字段并广播。
 *
 * ★ 必须造**新对象**：`useSyncExternalStore` 用 `Object.is` 比较快照，
 *   就地改字段不会触发重渲染，界面就此停在旧状态 —— 而 store 里
 *   明明已经是新的。那种"数据对了但界面不动"是最难查的一类。
 */
function patch(next: Partial<ChatState>) {
  state = { ...state, ...next }
  emit()
}

function getSnapshot(): ChatState {
  return state
}

/*
  正文增量按帧合并（rAF）。

  ## 为什么不在每个 delta 上 patch 一次

  `patch` 会替换整个 store 快照并通知页面，而 ChatPage 订阅的是整份状态：
  长会话里每个 token 都会让整段消息列表重渲染一次。一个网络分片常常带好
  几个 token，合并成一次渲染能直接砍掉大部分主线程工作，观感也更稳。

  顺序不受影响：任何非 delta 帧（message/phase/error/done）进入前都会先
  `flushDelta()`，保证正文先落、状态后改。
*/
let deltaBuffer = ''
let deltaFrame: number | null = null

function flushDelta(): void {
  deltaFrame = null
  if (!deltaBuffer) return
  const piece = deltaBuffer
  deltaBuffer = ''
  patch({ streamingText: (state.streamingText ?? '') + piece })
}

function queueDelta(piece: string): void {
  deltaBuffer += piece
  if (deltaFrame !== null) return
  const raf: (cb: FrameRequestCallback) => number =
    typeof requestAnimationFrame === 'function'
      ? requestAnimationFrame
      : (cb) => window.setTimeout(() => cb(performance.now()), 16)
  deltaFrame = raf(flushDelta)
}

function clearDelta(): void {
  if (deltaFrame !== null && typeof cancelAnimationFrame === 'function') {
    cancelAnimationFrame(deltaFrame)
  }
  deltaFrame = null
  deltaBuffer = ''
}

/** SSR 快照。本项目是纯 CSR，但 `useSyncExternalStore` 要求给一个。 */
const SERVER_SNAPSHOT: ChatState = EMPTY
function getServerSnapshot(): ChatState {
  return SERVER_SNAPSHOT
}

export function subscribeChat(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** 订阅对话状态。 */
export function useChat(): ChatState {
  return useSyncExternalStore(subscribeChat, getSnapshot, getServerSnapshot)
}

/**
 * 取当前快照。**给测试与非 React 调用点用**。
 *
 * 与 `schedules-store.ts::getSchedulesSnapshot` 同一用途：测试里不能用
 * hook，只能直接读。生产代码（页面）应该用 `useChat()` —— 只读快照不会
 * 触发重渲染，页面里用它等于写死。
 */
export function getChatSnapshot(): ChatState {
  return state
}

/**
 * ★ v1.68：清掉上一次「中断/继续」的回执 —— 弹窗每次重新打开时调用，
 * 免得用户看到上一次（不同会话 / 早已过时）的反馈。
 */
export function clearLastInterrupt(): void {
  if (state.lastInterrupt !== null) patch({ lastInterrupt: null })
}

/**
 * 把错误帧里那句"机器话"收拾成用户能读的一句话。
 *
 * ★ v1.23：服务端发来的 `detail` 形如
 *   `ChatProviderAuthError: 模型接入点拒绝了这次请求（API key 无效或已过期）。`
 * 前面那截是 Python 异常类名 —— 对排查有用，对用户是纯噪音（"Chat..."）。
 * 真正要读的是冒号后面那句人话。
 *
 * 只剥**类名式**前缀（`Foo` / `Foo.Bar` / `FooError` / `FooException`），
 * 不碰正常句子里的冒号（例如「原因：磁盘满了」不该被切）。
 */
function humanizeDetail(detail: string): string {
  return detail.replace(/^[A-Za-z_][\w.]*(?:Error|Exception):\s*/, '').trim() || detail
}

function messageOf(error: unknown): ApiError {
  return error instanceof ApiError ? error : toApiError(error)
}

/**
 * 重置成初始态。**给测试用** —— 生产代码里 store 是进程级的，
 * 一个测试跑完不清会让下一个测试看见上一个的会话。
 */
export function resetChatStore(): void {
  state = EMPTY
  /*
    ★ 连"在飞的请求"一起清掉。

    合流用的 promise 是模块级状态：一个测试若故意让请求悬着不收尾
    （很多流式用例就是这么写的），下一次 `loadSessions()` 会拿到那个
    永不 settle 的旧 promise，于是**根本不会发新请求** —— 表现不是
    "慢"，而是后面所有用例都读不到 mock 的调用记录。重置语义里
    "忘掉一切"本来就该包括挂起的请求。
  */
  sessionsInFlight = null
  detailInFlight.clear()
  earlierInFlight = null
  emit()
}

// ─────────────────────────────────────────────
// 读取
// ─────────────────────────────────────────────

/**
 * 拉会话列表。失败时**保留**已有列表并挂上 error。
 *
 * ★ 保留的理由与 `loadSkills` 里统计失败那条一致：列表读的是数据库，
 *   大概率成功；把一次瞬时失败变成"列表变空"，用户会以为自己的对话
 *   被删了 —— 那是比"列表没刷新"严重得多的误判。
 */
/**
 * 同一时刻只允许一次 `/chat/sessions` 在飞。
 *
 * ★ 实测（2026-11-07）：进对话页时这个接口被**并发发了三次**
 *   （挂载 effect + URL 会话 effect + 运行中轮询），每次 1.7–3.7 秒，
 *   互相抢连接池，越排越慢。合流之后只发一次，其余调用者等同一个 promise。
 *
 * ★ 只合并**并发**的调用：结束后再调就是新请求，归档/改名这类
 *   需要立刻看到最新数据的路径不受影响。
 */
let sessionsInFlight: Promise<void> | null = null

export function loadSessions(): Promise<void> {
  if (sessionsInFlight) return sessionsInFlight
  sessionsInFlight = fetchSessions().finally(() => {
    sessionsInFlight = null
  })
  return sessionsInFlight
}

async function fetchSessions(): Promise<void> {
  patch({ status: state.sessions.length === 0 ? 'loading' : state.status, error: null })
  try {
    /*
      ★ 一次拉**全部**（含已归档），在前端拆成两份，而不是发两次请求。

        两个列表的排序规则完全一致（置顶在前、其余按最近更新），拆请求会让
        两次响应之间的顺序可能不一致；而多一个请求就多一次可能失败的机会。
        拆的动作是纯过滤，没有额外信息量。
    */
    const all = await api.get<ChatSession[]>('/chat/sessions?includeArchived=true')
    patch({
      sessions: all.filter((s) => !s.archived),
      archivedSessions: all.filter((s) => s.archived),
      status: 'ready',
    })
  } catch (error) {
    patch({ status: 'error', error: messageOf(error) })
  }
}

/** 拉工具清单。失败不致命（界面只是少一块能力说明），所以不设 error。 */
export async function loadTools(): Promise<void> {
  try {
    const tools = await api.get<ChatTool[]>('/chat/tools')
    patch({ tools })
  } catch {
    // 故意吞掉：工具清单是说明性的，拿不到不该挡住对话本身。
    // 界面在 tools 为空时不渲染那一块，而不是显示一个错误。
  }
}

/**
 * 只把「当前打开的那个会话」的消息列表重新拉一遍。
 *
 * ## 和 `openSession` 的区别
 *
 * `openSession` 会整块替换 `active`（标题、计数、消息全换），还会顺手清
 * 附件 —— 它的语义是"切会话"。这里只补消息，因为调用它的场景是**同一
 * 个会话正在看的时候**（插话那一轮刚结束），换掉整个对象会把别的状态
 * （比如 `stagedAttachments`）一起冲掉。
 *
 * ## 什么时候需要它
 *
 * 只要服务端产生了一条**引擎不会通过流发出来的** user 消息，前端就看不见
 * 它 —— 典型就是 `interject()`。它是直接调端点写库的，不经引擎，
 * 所以既没有 message 帧，也不在 `active.messages` 里。
 *
 * 失败静默：这是一次补数据的机会，不是用户主动操作。报错会盖掉真正的
 * 错误（比如那一轮本身的失败原因），反而更糊涂。
 */
/**
 * 轮末**对账**：把当前打开的会话换成服务端的最后一页。
 *
 * ## ★ 为什么不能只用 `after` 游标（v1.79 修）
 *
 * `refreshActiveMessages` 只追加"比最后一条更新"的消息。可轮末有两类改动
 * **落在旧 seq 上**：
 *
 *   1. **插话**：它是引擎外的写入，seq 往往比流式已经推给前端的结论小；
 *   2. **结论降级**：引擎收尾时把"提前给出的结论"降级成过程面
 *      （`_collapse_turn_answers`），改的也是**已有**的那几条。
 *
 * 用 after 游标，这两类改动永远取不回来 —— 而 `finally` 会把本地插话
 * 气泡清掉，于是那句用户刚打的字在屏幕上直接消失（服务端有，界面没有）。
 *
 * 服务端列表是最终事实，所以轮末**整页替换**最后 200 条：代价是一次
 * 有界的下载，换来的是"界面与服务端一致"这条不变量。
 */
export async function reconcileActiveMessages(sessionId: string): Promise<void> {
  try {
    const current = state.active
    if (!current || current.id !== sessionId) return

    const detail = await fetchSessionDetail(sessionId, { limit: MESSAGE_PAGE_SIZE })

    const latest = state.active
    if (!latest || latest.id !== sessionId) return

    patch({
      active: {
        ...latest,
        messages: detail.messages,
        messageCount: detail.messageCount,
        hasEarlier: detail.hasEarlier ?? false,
      },
    })
  } catch {
    // 对账失败不该打断这一轮的收尾：界面保持现状，用户下次刷新会校正。
  }
}

export async function refreshActiveMessages(sessionId: string): Promise<void> {
  try {
    const current = state.active
    if (!current || current.id !== sessionId) return
    const lastSeq = current.messages.at(-1)?.seq
    /*
      ★ 只拉"最后一条之后"的新消息，不再把整个会话重下一遍。

        原来这里是无参全量拉：插话、后台补写这类"补一条"的场景，
        代价是几百 KB 到几 MB 的下载 + 全量重渲染。现在带 after 游标，
        返回通常是一条或零条。

        没有消息时退回全量：空会话没有游标可用，而那份数据本来也小。
    */
    const detail =
      lastSeq === undefined
        ? await fetchSessionDetail(sessionId, { limit: MESSAGE_PAGE_SIZE })
        : await fetchSessionDetail(sessionId, { after: lastSeq })

    // 等回来的这段时间用户可能已经切走了 —— 那就不要往新会话里写旧数据。
    const latest = state.active
    if (!latest || latest.id !== sessionId) return

    /*
      只接**比最后一条更新**的行。

      服务端带 after 游标时只会返回这些；这里再筛一遍是防御：
      老后端不认识 after 参数会退回全量，那时不筛就会把已有消息
      整段再拼一遍（界面上同一句话出现两次）。
    */
    const incoming =
      lastSeq === undefined ? detail.messages : detail.messages.filter((m) => m.seq > lastSeq)
    if (lastSeq !== undefined && incoming.length === 0) return
    const merged = lastSeq === undefined ? incoming : [...latest.messages, ...incoming]

    patch({
      active: {
        ...latest,
        messages: merged,
        messageCount: detail.messageCount,
        // 增量刷新不会改变"前面还有没有更早的"
        hasEarlier: detail.hasEarlier ?? latest.hasEarlier,
      },
    })
  } catch {
    // 静默（见 docstring）
  }
}

/**
 * 往上游标翻一页（「加载更早」）。
 *
 * 返回是否真的插进去了一页 —— 页面用它决定要不要保持滚动位置。
 */
export async function loadEarlierMessages(sessionId: string): Promise<boolean> {
  const current = state.active
  if (!current || current.id !== sessionId) return false
  const firstSeq = current.messages[0]?.seq
  if (firstSeq === undefined || !current.hasEarlier) return false

  // 同一时刻只允许一次：连点两下不该把同一页插两遍
  if (earlierInFlight) return earlierInFlight

  earlierInFlight = (async () => {
    try {
      const page = await fetchSessionDetail(sessionId, {
        before: firstSeq,
        limit: MESSAGE_PAGE_SIZE,
      })
      const latest = state.active
      if (!latest || latest.id !== sessionId) return false

      // 期间可能已经刷新过消息：按 id 去重，绝不插重复行
      const known = new Set(latest.messages.map((m) => m.id))
      const older = page.messages.filter((m) => !known.has(m.id))
      if (older.length === 0) {
        patch({ active: { ...latest, hasEarlier: false } })
        return false
      }

      patch({
        active: {
          ...latest,
          messages: [...older, ...latest.messages],
          hasEarlier: page.hasEarlier ?? false,
        },
      })
      return true
    } catch {
      return false
    } finally {
      earlierInFlight = null
    }
  })()

  return earlierInFlight
}

let earlierInFlight: Promise<boolean> | null = null

/**
 * 拉会话详情，**同一会话并发时合流**。
 *
 * 实测进对话页时 `/chat/sessions/{id}` 会被发两次（URL 还原一次、
 * 打开会话一次），而它带着整段消息历史 —— 长会话下每次都是上百 KB。
 * 两次内容完全一样，没必要发两遍。
 */
const detailInFlight = new Map<string, Promise<ChatSessionDetail>>()

interface DetailQuery {
  /** 只取最近 N 条 */
  limit?: number
  /** 取 seq 小于它的（往上翻页） */
  before?: number
  /** 只取 seq 大于它的（增量刷新） */
  after?: number
}

function fetchSessionDetail(
  sessionId: string,
  query: DetailQuery = {},
): Promise<ChatSessionDetail> {
  const params = new URLSearchParams()
  if (query.limit !== undefined) params.set('limit', String(query.limit))
  if (query.before !== undefined) params.set('before', String(query.before))
  if (query.after !== undefined) params.set('after', String(query.after))
  const suffix = params.toString()
  const key = suffix ? `${sessionId}?${suffix}` : sessionId

  const existing = detailInFlight.get(key)
  if (existing) return existing
  const request = api
    .get<ChatSessionDetail>(`/chat/sessions/${sessionId}${suffix ? `?${suffix}` : ''}`)
    .finally(() => detailInFlight.delete(key))
  detailInFlight.set(key, request)
  return request
}

/** 打开一个会话（拉详情）。 */
export async function openSession(sessionId: string): Promise<void> {
  patch({ status: 'loading', error: null })
  try {
    // 切会话先把上一个会话的附件清掉：后端按 session 目录找附件，
    // 留着上一个会话的 id 在这里，发出去会读不到（症状是"附件丢了"）。
    clearStagedAttachments()
    // 只取最后一页：长会话（实测有到几 MB 的）不该为了第一屏全下下来
    const detail = await fetchSessionDetail(sessionId, { limit: MESSAGE_PAGE_SIZE })
    patch({ active: detail, status: 'ready' })
  } catch (error) {
    patch({ status: 'error', error: messageOf(error) })
  }
}

// ─────────────────────────────────────────────
// 执行账目（v1.19）：一轮到底跑完了没有
// ─────────────────────────────────────────────

/**
 * 一轮对话的执行账目。
 *
 * ★ `resumable` 由**服务端**算（`turn_runs.is_resumable`），前端不要自己
 *   按 `status` 再推一遍 —— 那会变成两份判据，且后台接手用的就是服务端
 *   那一份，两边迟早漂开。
 */
export interface ChatTurnRun {
  id: string
  sessionId: string
  status: 'running' | 'interrupted' | 'done' | 'failed' | 'cancelled'
  resumeAttempts: number
  lastError?: string | null
  startedAt: string
  updatedAt: string
  resumable: boolean
  /** v1.61：进度（老后端缺省 → 0 / null）。 */
  roundIndex?: number
  toolsUsed?: number
  heartbeatAt?: string | null
  /** v1.92：这一轮累计 token；usageReported=false 时 0 不代表没消耗。 */
  promptTokens?: number
  completionTokens?: number
  totalTokens?: number
  usageReported?: boolean
}

export interface ChatTodoItem {
  seq: number
  text: string
  status: 'pending' | 'in_progress' | 'done'
}

/**
 * 读这次对话的结构化计划（`chat_todos`，v1.89）。
 *
 * ★ 失败返回 `[]` 而不是抛：计划是辅助信息，拿不到它不该让对话页打不开。
 *   与 `listTurns` 的 `null` 不同 —— 那里"拿不到"会让界面误判"后台没在跑"，
 *   这里只是少显示一条进度条。
 */
export async function listTodos(sessionId: string): Promise<ChatTodoItem[]> {
  try {
    return await api.get<ChatTodoItem[]>(`/chat/sessions/${sessionId}/todos`)
  } catch {
    return []
  }
}

/**
 * 列出一个会话最近的执行账目。
 *
 * ## ★ 失败返回 `null`，**不是**空数组（v1.79）
 *
 * "拿不到"和"没有中断的轮次"是两件事，而原来两者都表现为 `[]`：
 * 轮询把网络失败当成"服务端没在跑"，接着 `clearStaleSending` 把**仍在跑**
 * 的那一轮从本地状态里清掉 —— 界面显示已结束，实际后台还在干活。
 *
 * 辅助信息失败不该让整个会话打不开，所以不抛；但调用方必须能区分
 * "空"与"未知"。
 */
export async function listTurns(sessionId: string): Promise<ChatTurnRun[] | null> {
  try {
    return await api.get<ChatTurnRun[]>(`/chat/sessions/${sessionId}/turns`)
  } catch {
    return null
  }
}

/**
 * ★ v1.61：轮询服务端"这一轮跑到哪了"，并落进 `serverTurn`。
 *
 * ## 它解决什么（用户原话）
 *
 * 「服务端在后台跑、前端没有显示，我不知道进行得怎么样」+「中断/继续
 * 点了要刷新网页才行」。两者的共同根因：前端只信本页那条 SSE 流，
 * 一旦流断了/丢了（刷新、切页、网络抖动），界面就再也看不到服务端那一轮。
 *
 * ## 做法照抄 CyberStrikeAI 的 resumable polling
 *
 * 它的长工具用 `execution_id` 异步跑，客户端 `get_tool_execution` /
 * `wait_tool_execution` **可恢复地轮询**（plan 进度页 1500ms 一次）。
 * 我们这里没有 execution_id（一轮就是一条 turn_run 行），但形态一样：
 * 只要服务端还有 `running` 的轮次，就每隔一会儿拉一次 `/turns`，
 * 把最新进度写进 `serverTurn`；顺便**重取消息**（服务端可能已经落了新的
 * tool 结果/正文，而本页没收到帧）。
 *
 * ## 返回值：还有没有在跑
 *
 * `true` = 服务端说还在跑（调用方应继续轮询）；`false` = 已经结束
 * （调用方该停下来，并做最后一次消息刷新 —— 见 `refreshActiveMessages`）。
 *
 * ⚠️ 只读**当前打开的那个会话**：轮询是页面级行为，切走了就不该再往
 *    `serverTurn` 里写旧会话的进度。
 */
export type TurnPollResult =
  | { status: 'running'; runs: ChatTurnRun[] }
  | { status: 'idle'; runs: ChatTurnRun[] }
  /** 请求失败：**不能**据此认为这一轮结束了 */
  | { status: 'unknown'; runs: null }

export async function pollTurnProgress(sessionId: string): Promise<TurnPollResult> {
  const runs = await listTurns(sessionId)
  // 异步期间用户可能已经切走 —— 不往新会话里写旧数据。
  if (state.active?.id !== sessionId) return { status: 'idle', runs: [] }
  if (runs === null) {
    /*
      ★ v1.79：网络失败 ≠ 没在跑。

      原来这里直接按"没有 running"处理：清 `serverTurn`、把会话从
      `sendingSessionIds` 里摘掉、`sending` 置 false —— 一次抖动就能让
      仍在跑的一轮在界面上"被结束"，而用户看到的是输入框突然从「插话」
      变回「发送」。现在交给调用方：保持现状，下一拍再试。
    */
    return { status: 'unknown', runs: null }
  }
  const running = runs.find((run) => run.status === 'running') ?? null
  if (running) {
    const next = {
      runId: running.id,
      roundIndex: running.roundIndex ?? 0,
      toolsUsed: running.toolsUsed ?? 0,
      heartbeatAt: running.heartbeatAt ?? null,
    }
    const prev = state.serverTurn
    const changed =
      prev === null ||
      prev.runId !== next.runId ||
      prev.roundIndex !== next.roundIndex ||
      prev.toolsUsed !== next.toolsUsed ||
      prev.heartbeatAt !== next.heartbeatAt
    // 数值没变时不要再 patch：1.5s 一次的无意义更新会让整页重渲染。
    if (changed) patch({ serverTurn: next })
    return { status: 'running', runs }
  }
  // 没有在跑的了：清掉进度提示，也清掉「已请求停止」的标记
  // （v1.62：服务端真的停下来了，停止确认气泡该收了）。
  // v1.65：同时把这个会话从"在跑集合"里摘掉 —— 服务端确认它没在跑了，
  //   本地若还挂着它会导致 `sendingHere` 一直为真、输入框一直是"插话"。
  const remaining = state.sendingSessionIds.filter((id) => id !== sessionId)
  patch({
    serverTurn: null,
    stopRequested: false,
    sendingSessionIds: remaining,
    sending: state.sendingSessionId === sessionId && remaining.length > 0 ? true : false,
  })
  return { status: 'idle', runs }
}

/** 会话持久工作区（`/work/persist`）里的一个可下载产物。 */
export interface ChatWorkspaceFile {
  /** 相对工作区根目录的路径，下载时原样放进 `?path=`。 */
  path: string
  name: string
  sizeBytes: number
  modifiedAt: number
}

/**
 * 列出会话持久工作区里的产物。
 *
 * ★ 与 `listTurns` 同一条取舍：这是辅助信息，拿不到就当"没有产物"，
 *   不该让对话页整块报错。
 */
export async function listWorkspaceFiles(sessionId: string): Promise<ChatWorkspaceFile[]> {
  try {
    return await api.get<ChatWorkspaceFile[]>(`/chat/sessions/${sessionId}/files`)
  } catch {
    return []
  }
}

/**
 * 手动接着跑一轮中断的执行，并把新落库的消息接进当前会话。
 *
 * ★ 为什么复用 `sendMessage` 那套"整轮只 commit 一次"的做法：接手本质
 *   就是"把这一轮跑完再返回"，服务端返回的就是**新落库的消息**。前端
 *   追加即可，不需要第二条渲染路径。
 */
export async function resumeTurn(sessionId: string, runId: string): Promise<ChatMessage[]> {
  // ★ v1.22：重试前先清掉上一次的错误条。用户点「重试」就是"我处理过了"，
  //   旧错误继续挂着会让人以为这次也是同一个错 —— 而这次可能成功了。
  patch({ error: null })
  try {
    const out = await api.post<{ messages: ChatMessage[] }>(
      `/chat/sessions/${sessionId}/turns/${runId}/resume`,
      // 接手是**同步跑完整轮**才返回的（服务端 await resume_one）。用默认
      // 20 秒超时的话，任何超过 20 秒的轮次都会在前端变成"点继续没反应"，
      // 而服务端其实还在跑 —— 用户会反复点，越点越乱。
      undefined,
      { timeoutMs: 30 * 60_000 },
    )
    const active = state.active
    if (active && active.id === sessionId) {
      patch({ active: { ...active, messages: [...active.messages, ...out.messages] } })
    }
    /*
      ★ v1.61：接手这一轮是**同步跑完再返回**（`resume_turn` 里 await
      `resumer.resume_one`），所以这里返回时那一轮已经结束了。补两件事：
      （1）把 `sending` 清掉 —— 否则如果调用方（`handleResume`）没清，
          界面会一直显示"在跑"，用户以为没反应。
      （2）重取一次执行账目与消息 —— 用户原话「重新发送继续没有响应，
          还是要刷新网页」。刷新让黄条/错误条/消息列表都跟服务端对齐。
    */
    // v1.65：本会话接手跑完后，从"在跑集合"里摘掉它（别的会话仍可能跑着，
    // 所以 `sending` 按剩余集合算，不再无脑置 false）。
    const remaining = state.sendingSessionIds.filter((id) => id !== sessionId)
    patch({
      sending: remaining.length > 0,
      sendingSessionId: null,
      sendingSessionIds: remaining,
      phase: null,
  liveToolOutput: null,
  liveToolProgress: null,
      stopRequested: false,
    })
    void pollTurnProgress(sessionId)
    void refreshActiveMessages(sessionId)
    return out.messages
  } catch (error) {
    // 失败时把新原因摆出来（而不是让调用方自己猜）。
    patch({ error: messageOf(error) })
    throw error
  }
}

// ─────────────────────────────────────────────
// 写
// ─────────────────────────────────────────────

/** 新建一个空会话并打开它。 */
export async function createSession(title?: string): Promise<ChatSession | null> {
  patch({ error: null })
  try {
    const session = await api.post<ChatSession>('/chat/sessions', title ? { title } : {})
    // 列表头插：最新建的排最前（后端按 updatedAt 倒序，这里保持一致）。
    patch({
      sessions: [session, ...state.sessions],
      active: { ...session, messages: [] },
      status: 'ready',
    })
    return session
  } catch (error) {
    patch({ error: messageOf(error) })
    return null
  }
}

/**
 * 打开「这道题的那个对话」（没有就建一个）。
 *
 * ★ 看板行上的「查看」调它。服务端是 find-or-create，所以连点两次
 *   不会多出两个空会话 —— 见后端 `get_or_create_challenge_session`。
 *
 * 返回 `null` 表示失败（原因已写进 store 的 `error`）。
 */
export async function openChallengeSession(challengeId: string): Promise<ChatSession | null> {
  patch({ error: null })
  try {
    const session = await api.post<ChatSession>(
      `/chat/sessions/for-challenge/${encodeURIComponent(challengeId)}`,
    )
    // 侧栏里已经有就不要重复插入（点第二次查看时它就在列表里）
    const exists = state.sessions.some((item) => item.id === session.id)
    if (!exists) patch({ sessions: [session, ...state.sessions] })
    return session
  } catch (error) {
    patch({ error: messageOf(error) })
    return null
  }
}

/**
 * 发一条消息，把服务端追加的**全部**消息接进 `active`。
 *
 * ## 为什么整轮只 commit 一次
 *
 * 服务端返回的 `messages` 是一次原子追加（一条 user + 若干 assistant/tool）。
 * 逐条 commit 会让界面在中间态闪一下（比如先只看见用户那句话、
 * 工具卡半秒后才出现）。整轮一次落地，视觉上是"想完了，一起给"。
 *
 * ## 返回值而不是抛异常
 *
 * 调用方（页面）需要区分"成功"和"失败"来清输入框：成功才清。
 * 失败时把用户刚敲的字留在框里，否则他要重打一遍 —— 而失败原因
 * 往往只是模型接入点抖了一下。
 */
export async function sendMessage(
  sessionId: string,
  content: string,
  providerId?: string | null,
  explicitTools?: string[],
): Promise<boolean> {
  // ★ v1.65：**按会话**判重 —— 只有"这个会话自己已经有一轮在跑"才拦。
  //   别的会话在跑不该阻止当前会话发（后端本来就支持多会话并发）。
  if (state.sendingSessionIds.includes(sessionId)) return false
  // ★ 让用户那句话**立刻**出现在屏幕上（v0.97）。放在 `sending: true` 同一个
  //   patch 里，所以从用户点下发送到气泡出现的这段时间是一个渲染帧，
  //   而不是一整轮模型推理。
  // 上一轮异常退出时可能还有未落帧的增量，先清掉再开新一轮。
  clearDelta()
  patch({
    sending: true,
    sendingSessionId: sessionId,
    // ★ v1.65：登记进"本浏览器在跑的会话集合"（按会话独立并发的唯一事实来源）。
    sendingSessionIds: [...state.sendingSessionIds, sessionId],
    error: null,
    pendingUserContent: content,
    phase: null,
  liveToolOutput: null,
  liveToolProgress: null,
    interjections: [],
    // ★ v0.102：新的一轮从空缓冲开始。不清的话，上一轮异常退出时
    //   残留的半句话会拼在新一轮的开头（"这个对话还是空的"那种矛盾感）。
    streamingText: null,
    // ★ v1.62：新的一轮开始了，上一轮遗留的「已请求停止」标记作废。
    stopRequested: false,
  })

  // ★ 附件 id 只在这一条上生效。后端读完正文就删（见 attachments 模块），
  //   所以这里发完必须清空 `stagedAttachments` —— 不清的话界面会继续显示
  //   "已上传 3 个"，而它们在服务端已经不存在了；用户再发一条会看到
  //   "附件读不到"这种莫名其妙的错误。
  const attachmentIds = state.stagedAttachments.map((a) => a.id)

  // ★ v1.21：附件条**在按发送的那一刻**收起来，不再等整轮跑完。
  //
  // 之前是成功之后才清（原「附件已被后端读走并删除，本地也清掉」那句），
  // 于是整轮跑多久、附件条就在输入框上挂多久 —— 用户的原话是
  // 「对话发送了文件还贴在这」。更糟的是这期间输入框已经切成「插话」模式，
  // 那条残留的附件看起来像"下次发送还会带上"，其实这一轮已经读走它了。
  //
  // 失败/异常时在 `finally` 里放回去：上传物在服务端仍然是 staged 的，
  // 放回去用户能直接重发，不用重新选文件。放回用**合并**而不是覆盖 ——
  // 失败前用户可能已经又选了个新附件，覆盖会把它弄丢。
  const stagedBeforeSend = state.stagedAttachments
  if (attachmentIds.length > 0) patch({ stagedAttachments: [] })

  let sentOk = false
  let sawErrorFrame = false
  try {
    /**
     * ★★ v0.100：走**流式**端点。
     *
     * ## 改的是什么
     *
     * 之前是 `await api.post(...)` —— 整轮一次回包（模型想 + 最多 6 轮
     * 工具往返，实测 30 秒以上），期间界面上只有一个转圈。
     * 用户原话：「没有思考过程、工具调用、调用结果的显示」。
     *
     * 现在每落一条消息就收到一帧，界面**边跑边长**。
     *
     * ## ★ 乐观气泡怎么退场
     *
     * `pendingUserContent`（v0.97 那个立刻显示的用户气泡）在收到**第一条
     * user 消息帧**时清掉，而不是等整轮结束。它对应的那条真消息
     * 紧接着就进了 `messages` —— 两者不会同屏，也不会在整轮期间留一条
     * 假气泡在真气泡旁边。
     */
    const ok = await streamTurn({
      sessionId,
      content,
      providerId,
      attachmentIds,
      explicitTools,
      onEvent: (frame) => {
        const current = state.active
        if (!current || current.id !== sessionId) return
        // 任何非增量帧进入前，先把还没渲染的正文增量落下去，保持顺序。
        if (frame.event !== TURN_DELTA) flushDelta()

        if (frame.event === TURN_PHASE) {
          // 「正在想…」那一行现在能显示"第几轮 / 在跑哪个工具"
          const phase = frame.data as unknown as PhasePayload
          const previous = state.phase
          patch({
            phase,
            // 换了一个工具 → 上一段输出不再属于当前卡；同一工具的新 phase
            // （重试）也清空，避免把两次执行的输出拼成一段。
            liveToolOutput:
              phase.phase === 'tool' && previous?.tool === phase.tool
                ? state.liveToolOutput
                : null,
            liveToolProgress:
              phase.phase === 'tool' && previous?.tool === phase.tool
                ? state.liveToolProgress
                : null,
          })
          return
        }

        if (frame.event === TURN_TOOL_OUTPUT) {
          const tool = typeof frame.data.tool === 'string' ? frame.data.tool : ''
          const chunk = typeof frame.data.chunk === 'string' ? frame.data.chunk : ''
          if (!tool || !chunk) return
          const previous = state.liveToolOutput
          const text = (previous?.tool === tool ? previous.text : '') + chunk
          patch({
            liveToolOutput: {
              tool,
              // 上限 64 KiB：`yes` 之类的无限输出不该把浏览器内存吃光。
              text: text.length > 65_536 ? text.slice(-65_536) : text,
            },
          })
          return
        }

        if (frame.event === TURN_TOOL_PROGRESS) {
          const tool = typeof frame.data.tool === 'string' ? frame.data.tool : ''
          if (!tool) return
          const previous = state.liveToolProgress
          const sameTool = previous?.tool === tool
          const seconds =
            typeof frame.data.seconds === 'number'
              ? frame.data.seconds
              : sameTool
                ? previous.seconds
                : 0
          const progress =
            typeof frame.data.progress === 'number' ? frame.data.progress : undefined
          const total = typeof frame.data.total === 'number' ? frame.data.total : undefined
          const message = typeof frame.data.message === 'string' ? frame.data.message : undefined
          // 既没有秒数也没有服务端进度 = 无效帧（旧服务端/空 payload）。
          if (seconds <= 0 && progress === undefined && !message) return
          patch({
            liveToolProgress: {
              tool,
              seconds,
              ...(message ? { message } : {}),
              ...(progress !== undefined ? { progress } : {}),
              ...(total !== undefined ? { total } : {}),
            },
          })
          return
        }

        if (frame.event === TURN_MESSAGE) {
          const message = asChatMessage(frame.data)
          if (!message) return
          // 同一条消息可能因为重连/重放来两次 —— 按 id 去重。
          // 不去重的话界面上会出现两张一模一样的卡（而"重复"看起来像模型重复了动作）。
          if (current.messages.some((m) => m.id === message.id)) return
          const messages = [...current.messages, message]
          patch({
            active: {
              ...current,
              messages,
              messageCount: messages.length,
              title:
                current.messageCount === 0 && message.role === 'user'
                  ? deriveTitle(current.title, content)
                  : current.title,
            },
            // 真用户消息到了 → 乐观气泡功成身退（不能等整轮结束：
            // 那会让两句话同屏几十秒）。
            pendingUserContent: message.role === 'user' ? null : state.pendingUserContent,
            /*
              ★ v0.102：assistant 正文落库 → 流式缓冲退场，由真消息接管。
                不清的话这段文字会显示两遍（一次在缓冲里、一次在真消息里）。
                ⚠️ 只清 assistant：tool 帧不该影响正文缓冲（它落的是工具卡，
                而正文可能还在往里流）。
            */
            streamingText: message.role === 'assistant' ? null : state.streamingText,
            // 工具结果已落库 → 运行卡退场（最终文本以 message 为准）。
            liveToolOutput:
              message.role === 'tool' && message.toolName === state.liveToolOutput?.tool
                ? null
                : state.liveToolOutput,
            liveToolProgress:
              message.role === 'tool' && message.toolName === state.liveToolProgress?.tool
                ? null
                : state.liveToolProgress,
          })
          return
        }

        if (frame.event === TURN_DELTA) {
          /*
            ★ v0.102：正文增量。**拼接**而不是替换 —— 服务端推的是增量。

            ⚠️ 用 `?? ''` 而不是判断 `typeof`：字段缺失（旧版服务端）时
              应静默忽略这一帧，而不是把 undefined 拼进字符串变成 "undefined"。
          */
          const piece = typeof frame.data.text === 'string' ? frame.data.text : ''
          if (!piece) return
          // 按渲染帧合并，避免每个 token 都替换一次整份 store 快照。
          queueDelta(piece)
          return
        }

        if (frame.event === TURN_AWAITING) {
          // 停下来等人工确认：清掉 phase，界面据此把输入框让出来并提示
          patch({ phase: null, liveToolOutput: null, liveToolProgress: null })
          return
        }

        if (frame.event === TURN_ERROR) {
          sawErrorFrame = true
          const detail =
            typeof frame.data.message === 'string' && frame.data.message.trim()
              ? frame.data.message
              : '这一轮失败了，但没有给出原因。'
          // ★ v1.22：必须造**真的** `ApiError`，不能 `{ message } as ApiError`。
          //
          // 原来那句类型断言把服务端发来的真实原因（例如
          // "模型接入点拒绝了这次请求（API key 无效或已过期）…（原始信息：HTTP 401 Invalid token）"）
          // 直接丢掉：`ChatPage` 渲染错误条时走的是
          // `errorMessage(error)`，而它的第一步是 `error instanceof ApiError`
          // —— 一个裸对象不是实例 → 落到"请求失败，请稍后再试"的兜底分支。
          // 用户因此只看到一句没有任何信息量的话（本次缺陷的直接成因）。
          //
          // 404/409 这类流开流前的失败由 `streamTurn` 抛 `ApiError` 处理；
          // 这里补的是**开流之后**才出现的 `error` 帧。
          patch({ error: new ApiError('TURN_FAILED', humanizeDetail(detail), 200) })
          return
        }

        if (frame.event === TURN_DONE) {
          patch({ phase: null, liveToolOutput: null, liveToolProgress: null })
        }
      },
    })

    if (!ok) {
      /*
        ★ 流被**干净地**关掉、但既没 done 也没 error 帧时，旧代码只清 phase：
        用户看到"正在想"消失、正文停在半截，屏幕上没有任何失败原因 ——
        观感就是卡死。这里补一条明确的连接中断错误（有 error 帧时不动它）。
      */
      flushDelta()
      const next: Partial<ChatState> = { phase: null }
      if (!sawErrorFrame && state.error === null) {
        next.error = new ApiError(
          'TURN_STREAM_BROKEN',
          '连接中断，这一轮没有跑完。点「重试」可以接着跑。',
          200,
        )
      }
      patch(next)
      return false
    }

    sentOk = true

    /*
     * ★★ v0.101：把「插话」兑现成真消息。
     *
     * ## 为什么必须在这里刷新（这条修复的由来）
     *
     * `interject()` 写库后只把消息塞进本地 `interjections` 暂存区，
     * 界面上显示成「插话 · 模型下一轮会看到」。而这句提示的**有效期
     * 只有那一轮** —— 轮次一结束它就变成假话，因为：
     *
     *   - 引擎在每轮开头 `list_messages` 重读历史，所以它**确实**被模型
     *     看到了（`tests/test_chat_stream.py` 的
     *     `test_interjection_is_seen_by_the_next_round` 钉的正是这条）；
     *   - 但它**不是引擎落库的**，所以引擎不会为它发 message 帧 ——
     *     前端 `active.messages` 里永远没有它。
     *
     * 结果：那一轮结束后，屏幕上留着一条带「模型下一轮会看到」的僵尸
     * 气泡，而服务端的真实消息列表里它已经是一条**普通 user 消息**了。
     * 两边的说法互相矛盾，且要等用户发下一条消息（`sendMessage` 开头
     * 那句 `interjections: []`）才会消失。
     *
     * 修法就是 docstring 里一直写着、但代码里从没做过的那句：
     * 「那一轮结束时从服务端取回真消息，这里清空」。
     */
    /*
     * ★ v1.25：**无条件**从服务端重取一次本轮消息。
     *
     * 两个理由（前一个是 v0.101 的插话兑现，后一个是 v1.25 的"轮末收拾"）：
     *
     * 1. 插话（`interject`）是引擎外的写入，不会发 message 帧 —— 不刷新就
     *    留下一条带「模型下一轮会看到」的僵尸气泡。
     * 2. 引擎在**轮末**会把"提前给出的结论"降级成过程面（`_collapse_turn_answers`），
     *    但那些消息在流式过程中已经以 `answer` 推给前端了。不重取，界面上
     *    会留着两条几乎一样的结论气泡，直到用户手动刷新 —— 实测就是这么
     *    暴露的（seq4/seq5 两条「当前目录是 /work」）。
     *
     * 服务端列表才是**最终事实**，界面以它为准。
     * 不 await：气泡已经正确退场了（`finally` 里清），刷新只是校正列表，
     * await 它会让 `sending` 多挂一个往返，输入框白等。
     */
    void reconcileActiveMessages(sessionId)

    // 列表里的 updatedAt / messageCount 变了，静默刷新一次列表。
    // 不 await：它不影响这一轮的显示，慢了也不该让输入框卡住。
    void loadSessions()
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  } finally {
    // ★ v1.21：这一轮没成，把刚才收起来的附件条放回输入区（合并，不覆盖）。
    //
    // ★★ 前置条件是 `pendingUserContent !== null` —— 也就是**一条 user 消息帧
    //   都没收到**。这条判据不是保守，是必需：后端 `run_turn` 一进来就
    //   `attachments.hand_over(...)`，把暂存区的文件**搬**到会话交接目录
    //   （`/work/attachments`）。也就是说只要这一轮真的开始跑了，那些附件
    //   在服务端已经不在暂存区了；此时把 chip 放回去，用户下一条消息会带着
    //   一个查不到的 id 发出去 —— 附件静默丢失，比"要重新选一次文件"糟得多。
    //
    //   而 `pendingUserContent` 恰好是"服务端是否已经开始这一轮"的现成信号：
    //   页面在发送那一刻写它，收到第一条 user 消息帧时清它。所以：
    //   - 请求根本没到后端（网络错、连接被拒）→ 仍在 → 放回去，用户可直接重发；
    //   - 后端已开始（哪怕模型 502 挂了）→ 已清 → 不放回，因为文件已被搬走。
    if (!sentOk && stagedBeforeSend.length > 0 && state.pendingUserContent !== null) {
      const stillThere = new Set(state.stagedAttachments.map((a) => a.id))
      patch({
        stagedAttachments: [
          ...state.stagedAttachments,
          ...stagedBeforeSend.filter((a) => !stillThere.has(a.id)),
        ],
      })
    }

    // ★ 失败路径也要清乐观气泡：那句话没发出去（`pendingUserContent` 留着
    //   会让人以为发出去了，而实际服务端没有这条记录）。页面会把原文放回
    //   输入框，用户在那里能看到并重发。
    //
    // ★ 成功路径此时 `pendingUserContent` 已经是 null（第一条 user 消息帧
    //   清掉了），这里再写一次 null 是幂等的。
    //
    // ★★ v0.101：`interjections` 也在这里清 —— 那条提示「模型下一轮会看到」
    //   只管到这一轮结束。放 `finally` 而不是只在成功路径清，是因为失败
    //   路径上它同样已经过期：插话早就写进库了（`interject` 自己 commit），
    //   这一轮失败不影响那条消息存在，气泡留着只会继续说假话。
    // ★ v1.65：从这个会话的"在跑集合"里去掉它（按会话独立并发的收尾）。
    const remaining = state.sendingSessionIds.filter((id) => id !== sessionId)
    // ★ v1.65：展示字段（sending/phase/streamingText/pendingUserContent/
    //   interjections）只属于"用户此刻正在看的那个会话"。如果这一轮结束时
    //   **正在看的仍是本会话**（`sendingSessionId === sessionId`），就清掉这些
    //   展示字段；否则（用户早切到别的会话、那边正握着自己的流）**不能清**，
    //   否则会把另一会话刚画上去的进度/正文误清成空。
    const stillWatched = state.sendingSessionId === sessionId
    patch({
      sendingSessionIds: remaining,
      // `sending` 只要集合里还有别的会话在跑就仍为 true（有轮在跑的事实），
      // 但 sendingHere 由页面按 `sendingSessionIds.includes(active.id)` 判，
      // 不再依赖这个全局布尔做并发闸。
      sending: remaining.length > 0,
      ...(stillWatched
        ? {
            sendingSessionId:
              remaining.length > 0 ? (remaining[remaining.length - 1] ?? null) : null,
            pendingUserContent: null,
            phase: null,
            interjections: [],
            // ★ v0.102：流式缓冲在这里也必须清。失败路径上它留着一段
            //   "写了一半就断了"的正文，界面上会有一条永远停在半句的气泡，
            //   而错误条同时说这一轮失败了 —— 两者矛盾。
            streamingText: null,
          }
        : {}),
    })
  }
}

/**
 * ★ v0.100：在一轮跑着的时候插一句话。
 *
 * ## 它为什么独立于 `sendMessage`
 *
 * `sendMessage` 会起一整轮（新的模型调用、预算、工具额度）。
 * 插话**不起新一轮** —— 它只是往库里写一条 user 消息，正在跑的那一轮
 * 在下一轮开头会重新读历史，于是自然看到它。
 *
 * ## 为什么返回 `boolean` 而不是抛
 *
 * 与 `sendMessage` 同一条纪律：失败时页面要把用户敲的字留在输入框里。
 *
 * ## ⚠️ 插话进 `interjections` 而不是 `active.messages`
 *
 * 那一轮的 `messages` 由服务端在结束时整体给出，两边同时往里写会撞
 * id / 顺序。`interjections` 是"已送出去、模型还没看到"的暂存区，
 * 界面把它显示在消息流末尾（用户能看见自己的话进去了）；
 * 那一轮结束时由刷新从服务端取回真消息，这里清空。
 */
export type InterjectOutcome = 'ok' | 'no-turn' | 'failed'

/**
 * ★ v1.25：中断正在跑的那一轮。
 *
 * - `mode: 'continue'` —— 停下当前输出，把 `note` 并入上下文后自动接着跑。
 * - `mode: 'stop'` —— 这一轮就地结束（后台不会再自动接手）。
 *
 * 与 `interject`（插话）的区别：插话只排队，**当前输出不会停**。
 */
export async function interruptTurn(
  sessionId: string,
  mode: 'continue' | 'stop',
  note = '',
): Promise<boolean> {
  try {
    // ★ v1.68：接住服务端回执里的 `message`（"已请求中断…" / 具体原因），
    //   放进 `lastInterrupt` 让弹窗给用户一个明确的反馈，而不是"点了没反应"。
    const body = await api.post<{ message?: string }>(`/chat/sessions/${sessionId}/interrupt`, {
      mode,
      note,
    })
    /*
      ★ v1.61：点完中断，立刻做两件事，别等用户手动刷新网页。
      
      用户原话「中断并继续按钮是坏的，点击之后要刷新网页才行」。根因：
      `/interrupt` 只是**投递一个信号**给正在跑的那一轮（服务端在这一轮
      的下一个检查点才生效），它**不返回新版消息**，也不改前端状态 ——
      于是界面看起来"点了没反应"，必须刷新页面重新拉 `/turns` 与 `/messages`
      才看得到变化。

      这里点完就：（1）刷新一次轮次账目（`interrupting` 的 spinner 之外，
      用户能马上看到"已请求中断"这一轮的状态）；（2）如果是 `continue`，
      把本地 `sending` 拉起来 —— 服务端会接着跑，界面据此把转圈续上，
      而不是停在"没在跑"。`stop` 则反过来把本地 `sending` 清掉（真的停了）。
      两处都不 await：这是附带的界面校正，失败也不该让按钮报错。
    */
    if (mode === 'continue') {
      // 服务端会接着跑同一轮；本地把"在跑"状态续上，配合轮询显示进度。
      // v1.62：改方向后就不该再显示"已请求停止"。
      // v1.65：把本会话登记回"在跑集合"（它确实又跑起来了）。
      const ids = state.sendingSessionIds.includes(sessionId)
        ? state.sendingSessionIds
        : [...state.sendingSessionIds, sessionId]
      patch({
        sending: true,
        sendingSessionId: sessionId,
        sendingSessionIds: ids,
        stopRequested: false,
      })
    } else {
      // ★ v1.62：stop 的服务端语义是「下一个检查点停」——若正卡在一个
      //   跑着的工具里，它要等工具返回才停（子进程打断不了）。所以这里
      //   不清 `serverTurn`、并置 `stopRequested`：轮次仍 running 时，
      //   进度条改说「已请求停止，等当前工具返回后结束」，而不是又弹出
      //   「服务端仍在后台推进…点中断停下」（那会让用户以为停止没生效）。
      // v1.65：本会话从"在跑集合"里摘掉；别的会话仍在跑时 `sending` 保持 true。
      const remaining = state.sendingSessionIds.filter((id) => id !== sessionId)
      patch({
        sending: remaining.length > 0,
        sendingSessionId: null,
        sendingSessionIds: remaining,
        phase: null,
        stopRequested: true,
      })
    }
    void refreshActiveMessages(sessionId)
    void pollTurnProgress(sessionId)
    // ★ v1.68：给弹窗一个明确回执（用户在等"到底点没点上"）。
    patch({
      lastInterrupt: {
        mode,
        ok: true,
        message:
          body?.message ||
          (mode === 'continue' ? '已请求中断并继续，正在等待模型响应…' : '已请求彻底停止。'),
      },
    })
    return true
  } catch (error) {
    const api_error = messageOf(error)
    // ★ v1.68：失败也要有明确反馈 —— 尤其 409「这个对话当前没有在跑的回复」
    //   是最常被误当成"点了没反应"的情形，把服务端原话显示到弹窗里。
    patch({
      error: api_error,
      lastInterrupt: {
        mode,
        ok: false,
        message: api_error.message || '中断请求失败，请稍后重试。',
      },
    })
    return false
  }
}

export async function interject(sessionId: string, content: string): Promise<InterjectOutcome> {
  const trimmed = content.trim()
  if (!trimmed) return 'failed'
  try {
    const message = await api.post<ChatMessage>(
      `/chat/sessions/${sessionId}/interject`,
      { content: trimmed },
      { timeoutMs: 15_000 },
    )
    const active = state.active
    if (active && active.id === sessionId) {
      patch({ interjections: [...state.interjections, message] })
    }
    return 'ok'
  } catch (error) {
    const api_error = messageOf(error)
    // ★ v1.24：服务端说"这一轮其实没在跑"（我们这边的 sending 是过期的）。
    //   这不是失败 —— 调用方应退回"正常发送"，把那句话当一条新消息发出去。
    if (api_error.code === 'NO_RUNNING_TURN') return 'no-turn'
    patch({ error: api_error })
    return 'failed'
  }
}

/**
 * ★ v1.24：当我们以为有一轮在跑、服务端说没有时，把这个过期状态清掉。
 *
 * 为什么需要它：`sendMessage` 一进来就 `if (state.sending) return false`。
 * 如果 `sending` 是过期的（那一轮的 done 帧丢了），不清它就没法退回去
 * 发一条正常消息 —— 用户会看到自己的话被吞掉。
 *
 * ⚠️ 只在**确认服务端没有在跑的轮次**之后调用（`interject` 返回 `'no-turn'`）。
 *   别处调用它会把正在跑的那一轮的界面状态抹掉（进度、插话暂存区）。
 */
export function clearStaleSending(sessionId?: string): void {
  // ★ v1.68.2：原来这里 `if (!state.sending) return`。但 v1.65 起 `sending` 只是
  //   "本浏览器有任意会话在跑"的投影，可能已经是 false，而 `sendingSessionIds`
  //   里还留着当前会话、`stopRequested` 还挂着 —— 此时直接 return 就漏清了，
  //   表现为"这一轮明明结束了，输入框还写着「已请求停止」、侧栏还在转圈"。
  //   改成只要还有**任何**待清的痕迹就继续清。
  const target = sessionId ?? state.sendingSessionId
  const hasEntry = target ? state.sendingSessionIds.includes(target) : false
  if (!state.sending && state.sendingSessionIds.length === 0 && !state.stopRequested && !hasEntry) {
    return
  }
  // ★ v1.62：清掉卡死的本地 sending 时，也一并收掉可能残留的「已请求停止」
  //   标记（轮询随后会把 serverTurn 也清掉）。否则一个早已停下的轮次会
  //   留着"已请求停止"的红条。
  // ★ v1.65/v1.68.2：`clearStaleSending` 是"看到服务端说**这个会话**没在跑"
  //   才调的，要清的就是这一条；把它从集合里摘掉，别把别的会话（可能真在跑）
  //   也一起当成死的。`target` 明确指向要清的那条（调用方传入或退回被观看的那条）。
  const remaining = target
    ? state.sendingSessionIds.filter((id) => id !== target)
    : state.sendingSessionIds
  // 只有被观看的会话被清掉时，才清它的展示字段（别的会话在跑不该被误清）。
  const wasWatched = target === state.sendingSessionId || target === undefined
  patch({
    sendingSessionIds: remaining,
    sending: remaining.length > 0,
    ...(wasWatched
      ? {
          sendingSessionId: remaining.length ? remaining[remaining.length - 1] : null,
          phase: null,
          interjections: [],
          stopRequested: false,
        }
      : {}),
  })
}

/**
 * 拉人格列表。
 *
 * ★ 失败**不挂 error**：人格是"没有也能用"的东西（默认就是通用助手）。
 *   为它把整页变成错误态，会让一个可选功能拖垮必选功能。
 *   这与 `loadTools` 的取舍不同 —— 工具列表决定界面上显示哪些能力，
 *   读不到就该报错；人格读不到只意味着下拉框是空的。
 */
export async function loadPersonas(): Promise<void> {
  try {
    const personas = await api.get<ChatPersona[]>('/chat/personas')
    patch({ personas })
  } catch {
    // 故意吞掉：见上面 docstring。
  }
}

/**
 * 拉当前会话的暂存附件。
 *
 * ★ 目前后端没有"列出暂存附件"的端点，所以这个函数**只清空**。
 *   保留它是为了在切会话时把上一个会话的附件从界面上抹掉 ——
 *   不抹的话，切到 B 会话还会看到 A 的附件，而发出去时 B 会读不到
 *   （后端按 session 目录找），症状是"附件莫名其妙丢了"。
 */
function clearStagedAttachments(): void {
  if (state.stagedAttachments.length > 0) patch({ stagedAttachments: [] })
}

/**
 * 上传一个附件到当前会话的暂存区。
 *
 * 返回 `null` 表示失败（错误已挂进 `state.error`）。
 *
 * ## 为什么用 `FormData` 而不是 base64
 *
 * 一个 5 MB 的文件 base64 之后是 6.7 MB，而 JSON 里还要再套一层字符串。
 * 而且后端 `attachments.store` 收的是 `bytes`，走 multipart 能让 Starlette
 * 直接流式交给它，不必先整段解码。
 *
 * ## 为什么前端也判大小
 *
 * 后端会拒（`MAX_ATTACHMENT_BYTES`），但那是**传完之后**才拒 ——
 * 5 MB 的文件已经上传完了。在这里先判一次，用户立刻知道不行。
 * ⚠️ 两个数必须一致：这里用的是同一个常量值，改后端要同步改这里。
 */
export async function uploadAttachment(
  sessionId: string,
  file: File,
): Promise<ChatAttachment | null> {
  const MAX_BYTES = 5 * 1024 * 1024
  if (file.size > MAX_BYTES) {
    /*
      ★ 必须是 **真的** `ApiError` 实例，不能 `{...} as ApiError`。

        `as` 只是骗过 TypeScript，运行时那个裸对象 `instanceof ApiError` 为假，
        页面渲染走 `errorMessage()` 就落到兜底「请求失败，请稍后重试。」—— 用户
        传一个 96.9 MB 的 zip 时看到的就是这句（v1.38.1 修复）。这和 `TURN_ERROR`
        （v1.22）踩过的是同一个坑，只是这里漏改了。

      文案要给出**下一步**：对话附件是"给模型看"的一次性附件，上限 5 MB；
      真要放大文件（几十上百 MB）应当去「文件管理」页，那里上限 512 MB。
    */
    patch({
      error: new ApiError(
        'VALIDATION_FAILED',
        `文件太大（${(file.size / 1024 / 1024).toFixed(1)} MB）。对话附件是给模型看的一次性文件，` +
          `单个上限 5 MB。若想传大文件，请到左侧「文件管理」页上传（上限 512 MB）。`,
        400,
      ),
    })
    return null
  }
  if (file.size === 0) {
    patch({
      error: new ApiError('VALIDATION_FAILED', '文件是空的，没有内容可以给模型看。', 400),
    })
    return null
  }

  const form = new FormData()
  form.append('file', file)
  try {
    const stored = await api.upload<ChatAttachment>(`/chat/sessions/${sessionId}/attachments`, form)
    // 只在**还是同一个会话**时收进状态：上传是异步的，用户在等待期间
    // 可能已经切走了。收进去会让它在另一个会话的界面上出现。
    if (state.active?.id === sessionId) {
      patch({ stagedAttachments: [...state.stagedAttachments, stored], error: null })
    }
    return stored
  } catch (error) {
    patch({ error: messageOf(error) })
    return null
  }
}

/**
 * 从界面上移除一个暂存附件（**只移除本地引用**，不删服务端文件）。
 *
 * ★ 为什么不删服务端：服务端那个文件会在一小时后被 `sweep` 清掉，
 *   而现在删它需要额外一个 `DELETE` 端点。用户"不想要这个附件了"
 *   这个动作的后果只是"这条消息不带它" —— 本地移除已经足够，
 *   而少一个端点就少一处要维护的东西。
 */
export function dropStagedAttachment(attachmentId: string): void {
  patch({
    stagedAttachments: state.stagedAttachments.filter((a) => a.id !== attachmentId),
  })
}

/**
 * 改会话的六项设置（部分提交）。
 *
 * `partial` 里**只放要改的字段**。注意区分：
 * - 不传某个键 → 不改
 * - 传 `null` → 清空该项
 *
 * ★ 这个区分由后端 `update_settings` 的 `model_fields_set` 实现，
 *   前端只需要"别把没改的字段也塞进去"。所以这里的入参是
 *   `Partial<...>`，调用方按需拼对象。
 */
export interface ChatSettingsPatch {
  project?: string | null
  persona?: string | null
  providerId?: string | null
  requireApproval?: boolean | null
  autoApproveHours?: number | null
  skillNames?: string[] | null
  mcpServerNames?: string[] | null
}

export async function updateSettings(
  sessionId: string,
  partial: ChatSettingsPatch,
): Promise<boolean> {
  try {
    const updated = await api.patch<ChatSession>(`/chat/sessions/${sessionId}/settings`, partial)
    patch({
      sessions: state.sessions.map((s) => (s.id === sessionId ? updated : s)),
      active:
        state.active?.id === sessionId
          ? { ...state.active, ...updated, messages: state.active.messages }
          : state.active,
      error: null,
    })
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  }
}

/**
 * 本会话挂载的 MCP server 的**真实**试连结果（`GET …/mcp-status`）。
 *
 * ★ 为什么要一个接口而不只看 `mcpServerNames`：勾选只是"偏好"，真正
 *   决定模型能不能用那些工具的是"server 现在连不连得上"。只把偏好
 *   回显给用户，等于让他去猜有没有生效 —— 这个功能上一版就是因为
 *   "勾了没效果"被移除的。
 */
export interface ChatMcpServerStatus {
  name: string
  connected: boolean
  /** server 实际提供的工具数（发现成功时）。 */
  toolCount: number
  /** 连不上时的一句话原因；连上时为 null。 */
  reason?: string | null
}

export interface ChatMcpStatus {
  /** 会话设置里挂了几台（含已从平台删除的）。 */
  mountedCount: number
  /** 其中真正连上、并发现到工具的有几台。 */
  connectedCount: number
  servers: ChatMcpServerStatus[]
}

/**
 * 试连本会话挂载的 MCP server。
 *
 * 每次调用都会真的拉起子进程 / 打远端 URL，可能花几秒 —— 调用方
 * （对话设置的挂载区）只在面板打开或改完挂载后调它，不要放进渲染循环。
 */
export async function fetchMcpStatus(sessionId: string): Promise<ChatMcpStatus> {
  return api.get<ChatMcpStatus>(`/chat/sessions/${sessionId}/mcp-status`)
}

/**
 * 对一条挂起的工具调用做决定。
 *
 * 批准 → 后端执行那个工具并**接着跑完这一轮**，返回新消息。
 * 拒绝 → 只把那条消息改成 `rejected`，不调模型。
 *
 * ★ 返回的 `messages` 要**追加**进 active，不能整体替换：
 *   替换会把这一轮的上下文全丢掉，而用户会看到对话"跳回"上一步。
 */
export async function decideApproval(
  sessionId: string,
  messageId: string,
  approve: boolean,
): Promise<boolean> {
  if (state.deciding) return false
  patch({ deciding: true, error: null })
  try {
    const result = await api.post<{
      messages: ChatMessage[]
      awaitingApproval?: string | null
    }>(
      `/chat/sessions/${sessionId}/messages/${messageId}/approval`,
      { approve, locale: activeLocale() },
      // 批准之后要执行工具 + 再调一轮模型，与 sendMessage 同量级。
      { timeoutMs: 180_000 },
    )

    const active = state.active
    if (active && active.id === sessionId) {
      // ★ 按 id 合并而不是盲追加：被审批的那条消息**已经在** active 里了
      //   （它是挂起时落的那条），后端返回的是它改过状态之后的版本。
      //   盲追加会让它在界面上出现两次。
      const byId = new Map(active.messages.map((m) => [m.id, m]))
      for (const message of result.messages) byId.set(message.id, message)
      const messages = [...byId.values()].sort((a, b) => a.seq - b.seq)
      patch({
        active: { ...active, messages, messageCount: messages.length },
      })
    }
    void loadSessions()
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  } finally {
    patch({ deciding: false })
  }
}

/**
 * 前端这一份标题猜测。**只是为了那半秒的显示**，真值由后端给。
 *
 * ⚠️ 别把它当成规则实现：后端的截断规则（去换行、限长）是唯一口径，
 *   这里只是"在列表刷新回来之前别显示空标题"。
 */
function deriveTitle(currentTitle: string, content: string): string {
  if (currentTitle.trim()) return currentTitle
  const oneLine = content.replace(/\s+/g, ' ').trim()
  return oneLine.length > 24 ? `${oneLine.slice(0, 24)}…` : oneLine
}

/** 改标题。 */
export async function renameSession(sessionId: string, title: string): Promise<boolean> {
  try {
    const updated = await api.patch<ChatSession>(`/chat/sessions/${sessionId}`, { title })
    patch({
      sessions: state.sessions.map((s) => (s.id === sessionId ? updated : s)),
      active:
        state.active?.id === sessionId ? { ...state.active, title: updated.title } : state.active,
    })
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  }
}

/**
 * 归档会话（软删）。
 *
 * ★ 归档之后**从列表里移除**，而不是留在列表里标记成"已归档"：
 *   这个列表的用途是"接着上次继续说"，已归档的不该在这里。
 *   数据没删（后端 `archived=true`），只是不再打扰。
 */
export async function archiveSession(sessionId: string): Promise<boolean> {
  try {
    const archived = await api.del<ChatSession>(`/chat/sessions/${sessionId}`)
    const moving = state.sessions.find((s) => s.id === sessionId)
    patch({
      sessions: state.sessions.filter((s) => s.id !== sessionId),
      /*
        ★ 归档后**当场**挪进「已归档」，而不是等下一次 `loadSessions`。

          有两个理由：① 用户刚点了「删除此对话」，如果侧栏的「已归档」区
          里立刻能看到它，他就知道"东西还在，只是挪走了"；② `purge` 的
          第一道闸就是"必须先归档"，用户要能马上接着点「彻底删除」。
          用服务端返回的对象（带 `archived: true`），不要自己拼一个 ——
          拼的那份缺字段，渲染时会静默少一块。
      */
      archivedSessions: moving
        ? [{ ...archived, archived: true }, ...state.archivedSessions]
        : state.archivedSessions,
      active: state.active?.id === sessionId ? null : state.active,
    })
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  }
}

/**
 * 置顶 / 取消置顶。
 *
 * ★ 成功后重新拉列表（`loadSessions`）而不是就地改这一条：
 *   置顶改变的是**整体顺序**（它要挪到最前），就地改只能改这一条的字段，
 *   位置不对用户就会觉得"点了没反应"。重新拉一次是唯一能保证顺序与后端
 *   一致的做法 —— 而这条路径一次只发生在一个用户点菜单的瞬间。
 */
export async function pinSession(sessionId: string, pinned: boolean): Promise<boolean> {
  try {
    await api.post<ChatSession>(`/chat/sessions/${sessionId}/pin`, { pinned })
    await loadSessions()
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  }
}

/**
 * 从「已归档」里恢复。
 *
 * ★ 只恢复**消息**。归档时后端已经把沙箱与附件当场销毁（`archive_session`
 *   里的 `drop_sandbox` / `drop_*`），`/work` 里跑出来的东西和上传的原始
 *   文件都回不来。所以归档前必须二次确认 —— 界面不能给出"可以后悔"的错觉。
 */
export async function unarchiveSession(sessionId: string): Promise<boolean> {
  try {
    await api.post<ChatSession>(`/chat/sessions/${sessionId}/unarchive`, {})
    await loadSessions()
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  }
}

/**
 * 彻底删除（**物理删除**，不可恢复）。
 *
 * 后端有一道闸：会话必须**已归档**，否则 409。界面上「彻底删除」只出现在
 * 「已归档」区的条目上，所以正常路径不会撞到那道闸 —— 留着它是为了防
 * 有人直接调 API。
 */
export async function purgeSession(sessionId: string): Promise<boolean> {
  try {
    await api.post<ChatSession>(`/chat/sessions/${sessionId}/purge`, {})
    patch({
      archivedSessions: state.archivedSessions.filter((s) => s.id !== sessionId),
      active: state.active?.id === sessionId ? null : state.active,
    })
    return true
  } catch (error) {
    patch({ error: messageOf(error) })
    return false
  }
}

/**
 * 一键彻底删除全部已归档会话（不可恢复）。
 *
 * 后端在同一事务里删完再清理文件；这里只把本地归档列表清空。当前会话
 * 若恰好在归档里，也要一起退出 —— 否则右侧会停在一个已经不存在的会话上。
 */
export async function purgeArchivedSessions(): Promise<number | null> {
  try {
    const result = await api.post<{ deleted: number; messages: number }>(
      '/chat/sessions/purge-archived',
      {},
    )
    const archivedIds = new Set(state.archivedSessions.map((s) => s.id))
    patch({
      archivedSessions: [],
      active: state.active && archivedIds.has(state.active.id) ? null : state.active,
    })
    return result.deleted
  } catch (error) {
    patch({ error: messageOf(error) })
    return null
  }
}

/**
 * 只读取一个会话的最近消息，**不切换当前会话**（v1.92，Agent Hub 预览用）。
 *
 * 与 `openSession` 的分工：那个会把 `state.active` 换掉（导航语义）；
 * 子代理面板需要在主对话旁边看一小段转录，不能因此丢掉主线。
 */
export async function loadSessionPreview(
  sessionId: string,
  limit = 12,
): Promise<ChatSessionDetail> {
  return api.get<ChatSessionDetail>(`/chat/sessions/${sessionId}?limit=${limit}`)
}

/**
 * 从某条消息处分叉：复制截至该消息的历史到新会话（v1.92）。
 *
 * 成功后刷新侧栏列表并返回新会话；调用方负责切过去。失败写进 `error`
 * 并返回 null —— 分叉是显式动作，失败不能静默。
 */
export async function forkSession(
  sessionId: string,
  messageSeq?: number,
  title?: string,
): Promise<ChatSession | null> {
  try {
    const created = await api.post<ChatSession>(
      `/chat/sessions/${sessionId}/fork`,
      { messageSeq, title },
      { timeoutMs: 30_000 },
    )
    await loadSessions()
    return created
  } catch (error) {
    patch({ error: messageOf(error) })
    return null
  }
}
