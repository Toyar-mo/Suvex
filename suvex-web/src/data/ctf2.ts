import { ApiError, api, toApiError } from '@/lib/api'
import { useSyncExternalStore } from 'react'

/**
 * CTF2 平台对接的领域模型 + API 封装 + store。
 *
 * ★ 三者放在一个文件里，而不是拆成 `ctf2.ts` + `ctf2-store.ts`。
 *
 * 项目里的惯例本来是拆（`scan.ts` 是纯类型/常量，`scans-store.ts` 是 store）。
 * 拆的判据是**那个类型文件会不会被别的模块引**：`scan.ts` 被
 * 十几个组件引，所以它必须独立成模块（否则引一个 `ScanStatus`
 * 会把整个 store 与它的副作用一起拉进来）。
 *
 * 而 CTF2 的这几个类型**只有本模块与 CTF2 页用**，没有第三方消费者。
 * 分成两个文件只会让"加一个字段要改两处"。
 *
 * ⚠️ 它会随功能增长。**长到 800 行左右就该拆** ——
 * 那时按上面那个判据拆（类型与常量留下、store 与 API 出去），
 * 而不是按"文件太长了"随便切一刀。
 *
 * ## ★ 所有请求都走 `/api` 同源相对路径
 *
 * `lib/api.ts` 的 `API_BASE_URL` 默认是**当前页面的 origin**，
 * 所以这里只写 `/ctf2/...`，**不出现任何后端主机名或端口** ——
 * 换台机器、换个端口打开前端都不用改代码。见 `lib/api.ts` 文件头那段。
 */

// ─────────────────────────────────────────────
// 枚举（★ 与后端 vocab 是一组对账点）
// ─────────────────────────────────────────────
//
// 下面五个联合类型的取值必须与后端 `app/core/vocab.py` 里的
// `Ctf2ChallengeStatus` / `Ctf2SubmissionStatus` / `Ctf2EnvironmentStatus` /
// `Ctf2SubmitMode` / `Ctf2SandboxProfile` **逐字一致**。
//
// ⚠️ 靠人眼对账必然漂移（本项目在 `FINDING_STATUS_LABEL` 上真栽过：
// 后端把状态从 4 个扩到 6 个，前端标签表没跟上，于是新状态在界面上
// 显示成空白）。所以有一支脚本 `scripts/verify_ctf2_parity.py` 做
// 机器比对 —— **改这里必须跑它**。
//
// ★ 标签的权威在**前端**（后端只下发 status 字符串）——
// 与 `SCAN_STATUS_LABEL` 同一条分工。所以对账脚本比的是**取值集合**，
// 不是文案（文案改了不该让对账红）。

/** 一道题在我们这边的推进状态 */
export type Ctf2ChallengeStatus =
  | 'unsolved'
  | 'environment_ready'
  | 'solving'
  | 'candidate_ready'
  | 'submitting'
  | 'solved'
  | 'failed'

export const CHALLENGE_STATUS_LABEL: Record<Ctf2ChallengeStatus, string> = {
  unsolved: '未开始',
  environment_ready: '环境就绪',
  solving: '解题中',
  // ★ 这一档是 safe 模式的核心：它在等**人**。措辞必须让人一眼看出
  //   该自己动手了，而不是"还在跑"。
  candidate_ready: '待确认',
  submitting: '提交中',
  solved: '已解出',
  failed: '未解出',
}

/**
 * 状态的视觉色调。只有 `solving` / `submitting` 用 accent 呼吸，
 * `solved` 用 ok，`failed` 用 risk，其余中性。
 * 与扫描列表的状态语言一致，不另立一套。
 */
export const CHALLENGE_STATUS_TONE = {
  unsolved: 'idle',
  environment_ready: 'info',
  solving: 'accent',
  candidate_ready: 'warn',
  submitting: 'accent',
  solved: 'ok',
  failed: 'risk',
} as const satisfies Record<Ctf2ChallengeStatus, string>

/** 一次 flag 提交的结果 */
export type Ctf2SubmissionStatus = 'pending' | 'correct' | 'wrong' | 'rejected' | 'failed'

export const SUBMISSION_STATUS_LABEL: Record<Ctf2SubmissionStatus, string> = {
  pending: '待确认结果',
  correct: '判定正确',
  wrong: '判定错误',
  // ★ `rejected` 与 `wrong` 的措辞必须不同：前者是**我们本地拦下的**
  //   （不消耗平台额度），后者是平台判错（**消耗额度**）。
  //   混成一句话会让用户以为"还剩几次"被本地拦截白白扣掉了。
  rejected: '本地未提交',
  failed: '提交失败',
}

/** 题目环境的生命周期 */
export type Ctf2EnvironmentStatus = 'stopped' | 'starting' | 'ready' | 'error' | 'expired'

export const ENVIRONMENT_STATUS_LABEL: Record<Ctf2EnvironmentStatus, string> = {
  stopped: '已停止',
  starting: '启动中',
  ready: '就绪',
  error: '启动失败',
  // ★ `expired` 与 `error` 分开：到期是**正常老化**，补救动作是"重新启动"；
  //   而 error 的补救动作是"看日志、可能是镜像或平台侧的问题"。
  expired: '已到期',
}

/** 提交策略 */
export type Ctf2SubmitMode = 'safe' | 'auto'

export const SUBMIT_MODE_LABEL: Record<Ctf2SubmitMode, string> = {
  safe: '先确认再提交',
  auto: '自动提交',
}

/**
 * 两种策略的说明。
 *
 * ★ `auto` 的说明必须写出**代价**，不能只写"更省事"。
 *   用户开它的时候是在拿"这道题的答题机会"换"不用盯着" ——
 *   而那笔交易只有他自己能判断值不值。
 */
export const SUBMIT_MODE_HINT: Record<Ctf2SubmitMode, string> = {
  safe: '产出候选答案后停下，等你在界面上核对并确认。默认策略。',
  auto:
    '产出候选答案后直接提交，不等人。仍受每题错误提交次数上限约束 —— ' +
    '猜错会消耗不可再生的答题机会。',
}

/** 沙箱镜像 */
export type Ctf2SandboxProfile = 'ctf2-sandbox' | 'ctf2-sandbox-re' | 'ctf2-sandbox-ir'

export const SANDBOX_PROFILE_LABEL: Record<Ctf2SandboxProfile, string> = {
  'ctf2-sandbox': '通用（web / crypto / misc / 取证）',
  'ctf2-sandbox-re': '逆向专用',
  'ctf2-sandbox-ir': '应急响应专用',
}

export const SANDBOX_PROFILE_HINT: Record<Ctf2SandboxProfile, string> = {
  'ctf2-sandbox': '预装 web、密码学、取证与 Misc 常用工具。',
  // ★ 这里要写清"为什么逆向要单独一个镜像"，否则用户会问
  //   "多一个镜像是不是多余"。
  'ctf2-sandbox-re':
    '在通用镜像之上加逆向工具（radare2 / gdb / angr）。' +
    '逆向是唯一一类会主动执行题目给的代码的题型，所以单独收窄。',
  // ★ 同理：IR 单独一个镜像的理由不是"工具更多"，而是**方向相反** ——
  //   它要能读时间线（`mactime` / `log2timeline` 那一路），
  //   但**不该装攻击工具**：复盘题里给 agent 一把刀，它就会去砍。
  'ctf2-sandbox-ir':
    '在通用镜像之上加时间线重建工具（mactime / log2timeline），' +
    '但不含攻击工具 —— 复盘要读过去发生了什么，不是再打一遍。',
}

/**
 * 答案**看起来像** flag 吗。
 *
 * ⚠️ v0.21：这**不再是**"能不能提交"的判据 —— 见下面 `validateFlag`
 * 的说明。它现在只用来做展示与分类。
 *
 * **必须与后端 `app/ctf2/runner.py` 的 `FLAG_PATTERN` 逐字一致**
 * （对账脚本会比这一个）。
 *
 * ★ 正文里**允许空格**（`[^{}]` 而不是 `[^{}\s]`）。
 *   实测（2026-10-01，BUUCTF「Url编码」）：模型的答案 `flag{and 1=1}`
 *   被那个 `\s` 判成"形状不合法"而丢弃，题目以 failed 收场、
 *   一次提交都没发 —— 而它是**正确**的 flag。
 */
export const FLAG_PATTERN = /^[A-Za-z][A-Za-z0-9_]{0,31}\{[^{}]{1,200}\}$/

/** 占位符正文（`flag{...}` 这种）—— 它不是答案。 */
const PLACEHOLDER_BODY = /^[\s.。*＊_?？\-—]+$/

/**
 * 答案的长度上限。**必须与后端 `runner.MAX_ANSWER_LENGTH` 一致**
 * （平台 submit 请求体的 `maxLength` 是 4096，我们收在 1000）。
 */
export const MAX_ANSWER_LENGTH = 1000

/**
 * 优先级的取值分层（v0.24）—— 与 `app/core/vocab.py` 的常量逐字一致。
 *
 *     0            默认（没人管过，纯 FIFO）
 *     1 .. 999     人工在小范围内微调
 *     1000 .. 1999 新题自动加成（值 = 1000 + 分值，分值高的先做）
 *     2000 起      人工插队（「提前」一次就跳到这里）
 *
 * ★ `PRIORITY_MANUAL_BASE` 必须**大于** `AUTO_PRIORITY_MAX`，否则自动题里
 *   分值最高那道会压过人工插队的题 —— 而"人永远在环里"是赛事方明确
 *   允许的操作，不能被后台线程抢走。
 *
 * ★ 为什么需要 `PRIORITY_MANUAL_BASE` 这一档：题目卡片上的「提前」按钮
 *   是每次 +10。自动优先把新题推到 1000+ 之后，点十几次也追不上一道
 *   1500 的题 —— 那个按钮会从"有用"变成"点了没反应"。这是我们自己
 *   引入的回归，所以「提前」在低于这一档时**一步跳到 2000**。
 */
export const AUTO_PRIORITY_BASE = 1000
export const AUTO_PRIORITY_MAX = 1999
/**
 * ⚠️ 必须是 `AUTO_PRIORITY_MAX + 1`（= 2000）。
 *
 * 这里写**字面量**而不是 `AUTO_PRIORITY_MAX + 1`，是为了让
 * `scripts/verify_ctf2_parity.py` 能逐字对账（它解析的是常量字面量，
 * 不做算术）。改这一个数时，后端 `vocab.CTF2_PRIORITY_MANUAL_BASE`
 * 必须跟着改 —— 对账脚本会红给你看。
 */
export const PRIORITY_MANUAL_BASE = 2000

export function normalizeFlag(value: string): string {
  let text = (value ?? '').trim()
  for (const quote of ['`', "'", '"']) {
    if (text.length >= 2 && text.startsWith(quote) && text.endsWith(quote)) {
      text = text.slice(1, -1).trim()
    }
  }
  return text
}

/**
 * 校验一个要提交的**答案**。返回 null 表示通过，否则返回**可直接展示**
 * 的中文原因。
 *
 * ## ★★ v0.21：判据从"像不像 flag"改成"是不是明显不可能"
 *
 * 比赛规则原文：「提交**指定格式的字符串**（如 flag、**命令行、文件名、
 * 时间戳、IP信息**等）」。旧实现要求答案匹配 `FLAG_PATTERN`
 * （`前缀{正文}`），于是一条正确的命令行、一个 IP、一个时间戳会在
 * **本地**被判"格式不合法"。
 *
 * 而且这个组件里它是**硬拦**的：`FlagConfirmDialog` 拿它禁用"下一步"
 * 按钮 —— 用户连提交都点不了，系统连试都没试。
 *
 * 所以现在只拦明显不可能的三种（与后端 `validate_answer` 一一对应）：
 * 空、超长、以及**像 flag 但正文是占位符**。
 *
 * ⚠️ 第三条只在答案看起来像 flag 时适用 —— 否则题目要一个 `...` 当答案
 *    就会被误杀。这与"拿形状当对错"是同一个病。
 *
 * 返回字符串而不是布尔：调用方要把它显示在输入框下面，
 * 而"格式不正确"这四个字对用户毫无帮助。
 */
export function validateFlag(value: string): string | null {
  const text = normalizeFlag(value)
  if (!text) return '请填写要提交的答案。'
  if (text.length > MAX_ANSWER_LENGTH) {
    return `答案太长（${text.length} 字符，上限 ${MAX_ANSWER_LENGTH}）。`
  }
  // 占位符检查**只对"像 flag 的"生效**
  if (FLAG_PATTERN.test(text)) {
    const body = text.slice(text.indexOf('{') + 1, -1)
    if (PLACEHOLDER_BODY.test(body)) {
      return '这只是占位符（正文里只有标点），不可能是真答案。'
    }
  }
  return null
}

// ─────────────────────────────────────────────
// 类型
// ─────────────────────────────────────────────

export interface Ctf2Account {
  id: string
  label: string
  status: 'active' | 'expired' | 'revoked'
  remoteUserId: string | null
  remoteUsername: string | null
  /** 平台授予的 scope 列表。用它**提前**提示"少一个权限" */
  scopes: string[]
  /** 形如 `pat_****9f2a`。**不是凭据本身** */
  tokenMask: string
  hasToken: boolean
  lastSyncedAt: string | null
  lastError: string | null
  createdAt: string
  updatedAt: string
}

export interface Ctf2Competition {
  id: string
  accountId: string
  remoteId: string
  kind: 'competition' | 'practice'
  name: string
  /** 平台侧的状态原文。**不枚举** —— 那是别人家的取值空间 */
  remoteStatus: string
  startAt: string | null
  endAt: string | null
  challengeCount: number
  syncedAt: string
}

export interface Ctf2Environment {
  id: string
  challengeId: string
  status: Ctf2EnvironmentStatus
  sandboxProfile: Ctf2SandboxProfile
  /** ⚠️ 只有 `status === 'ready'` 时才非空（数据库层有约束守着） */
  accessUrl: string | null
  accessUrls: string[]
  containerId: string | null
  pollCount: number
  startedAt: string | null
  readyAt: string | null
  expiresAt: string | null
  lastError: string | null
}

export interface Ctf2Challenge {
  id: string
  competitionId: string
  remoteId: string
  stageId: string | null
  title: string
  category: string
  description: string | null
  attachments: { name?: string; url?: string }[]
  score: number | null
  status: Ctf2ChallengeStatus
  /**
   * LLM 提出、**等人工确认**的候选答案。
   *
   * ⚠️ 与 `confirmedFlag` 是两件事，**不要合成一个字段用**：
   *   · 这一列有值 ⟺ 有一个等着人点确认的候选（`candidate_ready`）；
   *   · `confirmedFlag` 有值 ⟺ 平台判过对（`solved`）。
   * 两者不会同时有值 —— 判定一出，候选就被清掉了（服务端保证）。
   * 合成一个字段时，界面无法回答"现在有没有东西等着我确认"，
   * 而在一道**判错退回未解出**的题上会显示一个看起来像答案的字符串。
   */
  candidateFlag: string | null
  /** **平台判过对**的 flag。只有已解出时才有值。 */
  confirmedFlag: string | null
  /**
   * 还剩几次错误提交机会。
   *
   * ★ 由**服务端**算好，前端**不做减法**：`max - count` 这条规则
   *   有两份实现迟早会分叉（比如将来加入平台侧额度时算法会变），
   *   而分叉的症状是"界面上还剩 2 次、实际提交时被拒"。
   */
  remainingWrongSubmissions: number
  wrongSubmissionCount: number
  maxWrongSubmissions: number
  candidateCount: number
  sandboxProfile: Ctf2SandboxProfile
  scanId: string | null
  solvedAt: string | null
  lastError: string | null
  environment: Ctf2Environment | null
  /**
   * 调度优先级。**越大越先做**，默认 0。
   *
   * ⚠️ 队列里同级按 FIFO（服务端的排序是 `priority DESC, updated_at ASC`），
   *   所以**全 0 时顺序与升级前完全一致** —— 这个字段是纯增量的。
   */
  priority: number
  createdAt: string
  updatedAt: string
}

export interface Ctf2Settings {
  submitMode: Ctf2SubmitMode
  maxWrongSubmissions: number
  defaultSandboxProfile: Ctf2SandboxProfile
  /**
   * 解题并发配额（v0.22）。
   *
   * ★ 它**不再等于**扫描那个上限。原来两者共用
   * `max_concurrent_scans`，而两个调度器各自数自己的名额 ——
   * 于是 `=3` 时真的跑出 3 个扫描 + 3 道题，是一处没人看得见的超卖。
   * 现在各领域守自己的子配额，**总并发 = 两者之和**。
   */
  maxConcurrentSolves: number
  /**
   * 认领轮询间隔（秒，v0.24）。
   *
   * ★ 它**不再借用**扫描那个旋钮。原来 CTF2 的调度循环读的是
   * `scan_poll_seconds`，于是"把扫描轮询调大"会顺带把 CTF2 的认领延迟
   * 一起调大，而界面上没有任何地方提示这两件事连着 —— 与 v0.22 的配额
   * 超卖是同一类病（两个独立的东西共用一个旋钮）。
   */
  solvePollSeconds: number
  /**
   * 自动增量同步间隔（秒，v0.24）。**0 = 关闭**。
   *
   * 它是"抢首杀"的入口：比赛规则里每道题前三名额外加 15%/10%/5%，
   * 而在此之前一道新题要等用户手动点同步才会出现在系统里，
   * 那段人工延迟是纯粹被让掉的分。
   */
  autoSyncSeconds: number
  /** 新题入库时自动给高优先级（默认开）。 */
  autoPriorityNew: boolean
  /** 新题入库时自动开始解题（**默认关**，会消耗模型额度与并发名额）。 */
  autoStartNew: boolean
  /**
   * 每题最多跑多少回合（v0.26）。区间 5-500。
   *
   * ★ **这个数没有全局最优。** 实测同一批题里，`[ACTF2020 新生赛]Exec`
   * 6 轮就出结果，而 `[FlareOn3]GUI`（.NET 逆向）跑满 40 轮、发出
   * 37 条各不相同的命令（一路分析到 `decryptBuffer` 那层）仍没做完。
   *
   * 所以：简单题调小省额度，硬题调大 —— 那是现场判断，不该写死。
   */
  maxTurns: number
  /**
   * 换模 fallback（v0.35）。默认 provider 调不动时改用哪个 provider；
   * `null` = 不启用（**默认**）。
   *
   * ★ 为什么默认关：自动挑一个备用意味着"默认 provider 挂了就悄悄换成
   * 另一个（可能是云端、要花钱的）"，而那是一次用户没同意的成本决定。
   * 换模要花谁的钱、换成谁，必须由人指定。
   */
  fallbackProviderId: string | null
  /**
   * 智能体安全评测场景包（v0.40）。**默认关闭。**
   *
   * ★ 它换的不是"题型数量"，是 agent 的**行为纪律**：分类属于
   * `agentsec-*` 那四类的题，多拿到一段「判定某个安全考察点是否成立」
   * 的提示词，结论按 `confirmed` / `needs_follow_up` / `ruled_out` 三级给。
   * 其余五类题拿到的提示词**逐字节不变**（后端 `build_system_prompt`
   * 在开关关闭时连那个 `.md` 文件都不读）。
   *
   * ⚠️ 那四类题**没有 flag**。开着它去跑普通题，模型的取舍会与题目要求
   * 无关 —— 它在判定"有没有问题"，而题目要的是答案。
   */
  enableAgentsecScenarios: boolean
  /**
   * 应急响应场景包（v0.41）。**默认关闭。**
   *
   * 打开后分类属于 `ir-root-cause` / `ir-timeline` / `ir-scope` 这三类的题，
   * 会额外拿到一段「复盘过去发生了什么」的场景块：它的产出是时间线、
   * 根因链、影响范围，按 `confirmed` / `needs_follow_up` / `ruled_out`
   * 三级给结论 —— 与上面那个开关**共用同一套分级**。
   *
   * ⚠️ 与 `enableAgentsecScenarios` **是两个独立开关，不是一个**。
   * 共用一个的话，`agentsec-*` 那族题会在 IR 开关打开时通过校验，
   * 而对应的提示词块却不注入 —— 分类合法、纪律没注入，静默失效。
   */
  enableIrScenarios: boolean
  updatedAt: string
}

export interface Ctf2Queue {
  running: number
  limit: number
  queued: number
  challenges: string[]
}

export interface Ctf2Snapshot {
  account: Ctf2Account | null
  competition: Ctf2Competition | null
  challenges: Ctf2Challenge[]
}

/** `POST /ctf2/challenges/pull` 的结果。 */
export interface Ctf2PullResult {
  challenge: Ctf2Challenge
  /** 这次是新建还是更新已有（重复拉同一道题时是更新）。 */
  created: boolean
  /** 认出来的题目 id（回显，便于核对是不是想要的那道）。 */
  challengeId: string
  /** 命中的练习集名字（让人知道这道题挂哪儿了）。 */
  competitionName: string
}

export interface Ctf2SyncResult {
  competitions: number
  challengesCreated: number
  challengesUpdated: number
  errors: string[]
  /**
   * 附件是**独立的一笔账**（服务端字段：`attachmentsDownloaded`
   * / `attachmentsSkipped` / `attachmentsFailed`）。
   *
   * ★ 它在界面上要显示出来，而不是并进"新增题目 N"：题面同步成功
   *   但附件全军覆没（出题人放了站外链接、磁盘满）是一种真实状态，
   *   而它的后果是**解题会失败得莫名其妙** —— agent 手里根本没有附件。
   *   把这三个数字露出来，用户才能把两件事连起来。
   */
  attachmentsDownloaded: number
  attachmentsSkipped: number
  attachmentsFailed: number
  message: string
}

/**
 * **平台上**的一场赛事（`GET /ctf2/competitions/remote` 的返回项）。
 *
 * ⚠️ 与本地那份 `Ctf2Competition` 是两件事，**不要混用**：
 *   · 这一份的 `remoteId` 是**平台 id**，它被原样回传给
 *     `syncCtf2([remoteId])`；本地那份的 `id` 是我们自己的主键。
 *   · 这一份可能包含"还没同步过"的赛事 —— 那正是它存在的意义
 *     （否则"要选赛事才能同步赛事"是个死结）。
 */
export interface Ctf2RemoteCompetition {
  remoteId: string
  name: string
  /** 平台给的时间字符串。**原样展示**（不在这里解析）—— 见后端 schema 的说明。 */
  startAt: string | null
  endAt: string | null
  /** 平台侧状态原文（不枚举：那是别人家的取值空间）。 */
  status: string
}

export interface Ctf2SolveResult {
  challenge: Ctf2Challenge
  environmentId: string | null
  scanId: string | null
  message: string
}

export interface Ctf2SubmitResult {
  status: Ctf2SubmissionStatus
  message: string
  challenge: Ctf2Challenge
}

// ══════════════════════════════════════════════════════════
// 题解与 token 消耗（v0.36）
// ══════════════════════════════════════════════════════════
//
// ## ★ 为什么 CTF2 不能复用扫描的题解端点
//
// 扫描的 `GET /scans/{id}/writeup` 拼的是 **strix 报告**的版式重排。
// 而 CTF2 解题**不走 strix**（由 `runner.solve_in_sandbox` 自己的循环
// 驱动），从不产生 `penetration_test_report.md` —— 所以那条路对它
// **永远**报"没有留下报告文件"，而那句话会让用户去翻一个不该存在的文件。
//
// 契约 §3.19.5 记了这次修正：扫描那边认出 CTF2 的壳之后返回
// "题解在题目卡片上"，而真正的题解由**下面这个端点**给出。
//
// ## ★★ flag 在这里**不脱敏** —— 与方法卡（`ctf2_solve_notes`）相反
//
// 方法卡会进**另一道题**的 prompt，带 flag 就是泄漏；而题解是
// **赛后提交给人看**的，flag 正是要展示的东西。判据是这份内容的**去向**，
// 不是"flag 是不是秘密"。所以下面的 `markdown` 里出现答案是对的，
// 不要"顺手统一"去脱敏。

/**
 * 一道题的题解（`GET /ctf2/challenges/{id}/writeup`）。
 *
 * ⚠️ 它在**解题收尾时生成并落库**（`service.save_writeup`），端点只读库。
 * 所以 v0.36 **之前**解过的题没有这份记录 —— 那时后端返
 * `409 WRITEUP_UNAVAILABLE`，界面要**原样显示后端那句话**，因为它区分了
 * "从没解过"与"解过但没有题解"两种处境（补救动作不同）。
 */
export interface Ctf2Writeup {
  challengeId: string
  title: string
  category: string
  /** 产出时这道题**解没解出**。未解出的题解同样保留（"试过什么"也是材料） */
  solved: boolean
  /** 题解正文。**含 flag 原文**（见上面那段说明） */
  markdown: string
  /** 下载用的文件名，后端拼好（`CTF-Writeup-{安全化的题目名}.md`） */
  filename: string
  /** 出自哪一次尝试。可空（尝试记录被清掉时后端置 NULL） */
  attemptId: string | null
  turns: number
  /** 耗时。**null = 没测到**（收尾没跑、壳扫描被删），不是 0 秒 */
  elapsedSec: number | null
  generatedAt: string
  updatedAt: string
}

/**
 * **一次 LLM 调用**的用量（`GET /ctf2/challenges/{id}/usage` 的项）。
 *
 * 这是"细分到每一次任务的消耗"最直接的视图 —— 能看出哪一轮最贵、
 * 是不是在兜圈子烧钱、换模前后各花了多少。
 */
export interface Ctf2UsageEntry {
  /** 第几轮 */
  turn: number
  model: string
  /** provider 的显示名 */
  providerLabel: string
  promptTokens: number
  completionTokens: number
  totalTokens: number
  /** 推理开销。⚠️ 实测默认网关恒给 0，**只备查、不参与统计** */
  reasoningTokens: number
  /**
   * ★★ 接入点**到底给没给**用量。
   *
   * `false` 时上面四个 token 值都是 0，而那个 0 **不代表没消耗** ——
   * 所以界面必须把"0"与"未记录"分开显示。见 `Ctf2TokenUsage` 的说明。
   */
  usageReported: boolean
  at: string
}

/** 按模型分组的用量。换模（v0.35 的 fallback）之后同一次解题会用多个模型。 */
export interface Ctf2UsageByModel {
  model: string
  providerLabel: string
  calls: number
  totalTokens: number
}

/** 按题分组的用量（"哪道题最烧钱"）。 */
export interface Ctf2UsageByChallenge {
  challengeId: string
  title: string
  calls: number
  totalTokens: number
}

/**
 * token 消耗统计（`GET /ctf2/usage`）。
 *
 * ## ★★ `calls` 与 `recordedCalls` 是**两个**字段，不是冗余
 *
 * · `calls` —— 调用次数。**永远准确**（后端从行数数得出来）；
 * · `recordedCalls` —— 其中**接入点真的返回了用量**的次数。
 *
 * 两者不等时 `totalTokens` 是**下限**（有几次没算进去）。合成一个字段的
 * 后果很具体：接入点不给 `usage` 时 token 记 0，而"消耗 0"与"没记录到"
 * 在界面上完全一样 —— 用户会以为某次解题没跑过，或者以为那次特别省。
 *
 * 这与 `hasKey`/`apiKey` 分两列、`candidateFlag`/`confirmedFlag` 分两列
 * 是同一条纪律：**两种语义不能共用一个"空"**。
 *
 * ## ⚠️ 不做 token → 金额的换算
 *
 * 单价随模型、供应商、时段变化，写死在代码里会误导。只给 token 数。
 */
export interface Ctf2TokenUsage {
  calls: number
  recordedCalls: number
  promptTokens: number
  completionTokens: number
  totalTokens: number
  byModel: Ctf2UsageByModel[]
  byChallenge: Ctf2UsageByChallenge[]
  /**
   * 统计从哪一刻开始。**null = 一条记录都没有**。
   *
   * ⚠️ v0.36 **之前**的解题没有 usage 记录。不给这个字段的话，用户会
   * 以为"总共就花了这么点"，而实际是**没记**。所以界面在它为 null 时
   * 要说"还没有数据"，**而不是**显示"0 token"。
   */
  since: string | null
}

/**
 * 读一道题的题解。
 *
 * ⚠️ **失败在这里不兜底**：`ApiError` 原样抛给调用方。后端那句中文区分了
 * 三种处境（题不存在 / 从没解过 / 解过但早于 v0.36），而它们的下一步动作
 * 完全不同 —— 前端换一个说法只会把可行动的信息换成泛泛的失败。
 * 与 `loadWriteup`（扫描侧）同一条纪律。
 */
export async function loadCtf2Writeup(challengeId: string): Promise<Ctf2Writeup> {
  return api.get<Ctf2Writeup>(`/ctf2/challenges/${encodeURIComponent(challengeId)}/writeup`)
}

/**
 * 一份处置建议工单（`GET /ctf2/challenges/{id}/workorder`，v0.41）。
 *
 * ## 它与题解是**两份**产物，不是一节
 *
 * 题解是叙事文档（人会通读）；工单是**待办清单**（人要逐条批）。
 * 工单会被复制进工单系统、打印出来批、当附件发出去 —— 所以它
 * 能单独下载，而不是让人从一篇 markdown 里手工摘。
 *
 * ## ⚠️ `markdown` 里没有任何"已执行"的动作
 *
 * 这是后端的硬约束不是文案选择：系统**没有**执行通道，工单只到
 * 「建议」为止。文件头第一句就是否定句（`workorder.render_workorders`）。
 * 所以前端**不要**额外加"已应用 / 已同步"之类的措辞 —— 那会把
 * 一份建议清单变成执行记录，而系统根本没有那个能力。
 *
 * ## 只有应急响应任务有工单
 *
 * 普通 CTF 题的提示词没让模型写 `workorders`，所以后端对它们返回
 * `409`。**这不是失败**，是这类任务不产出工单 —— 界面要把
 * 这种情况和"真的出错了"分开（与题解那三种 409 同理，
 * 靠后端那句中文原样显示来区分）。
 */
export interface Ctf2WorkOrder {
  challengeId: string
  title: string
  /** 工单全文（markdown）。四段齐全的在前、缺段的标注缺什么 */
  markdown: string
  /** 下载用的文件名（`IR-WorkOrders-{安全化的题目名}.md`） */
  filename: string
  /**
   * 工单条数。
   *
   * ★ `0` 是**正常结果**（本次复盘没有需要处置的事项，后端会产出
   * 一份带解释的说明），与"取不到工单"（409）是两件事。
   * 判据是数 `### ` 标题而不是 `## ` —— 后者会把空结果说明也算进去。
   */
  count: number
}

/**
 * 读一道题的处置建议工单。
 *
 * ⚠️ 与 `loadCtf2Writeup` 同一条纪律：**失败不在这里兜底**，
 * `ApiError` 原样抛给调用方 —— 后端那句中文说明了"这不是应急响应任务"
 * 还是"还没跑完"，两者的下一步动作完全不同。
 */
export async function loadCtf2WorkOrder(challengeId: string): Promise<Ctf2WorkOrder> {
  return api.get<Ctf2WorkOrder>(
    `/ctf2/challenges/${encodeURIComponent(challengeId)}/workorder`,
  )
}

/**
 * 建任务的结果：一道可以立刻开跑的题 + 它认领到的证据包。
 *
 * ## 为什么返回整道题而不只返回 id
 *
 * 建完之后界面要立刻显示"建了什么"（标题、镜像、状态、带了几份证据）。
 * 只给 id 的话前端要为了一行字再发一次 `GET /ctf2/challenges/{id}`，
 * 而那次请求与这次是同一个事务刚写下的数据 —— 多一次往返、多一个
 * "刚建完却读到旧数据"的机会。
 */
export interface IrTask {
  challenge: Ctf2Challenge
  /**
   * 认领到的证据文件（名字 + 字节数）。
   *
   * ⚠️ 空数组有两种意思：**没传附件**、或**暂存批次过期了**。
   * 后者在 `notice` 里有说明 —— 所以不要只看这个数组就下结论。
   */
  attachments: Array<{ name: string; sizeBytes?: number }>
  /** 后端的一句提示（比如"暂存批次已过期，本次没有证据包"）。没话要说时为 null。 */
  notice: string | null
}

/**
 * 建一个**本地应急响应复盘任务**。
 *
 * ## 为什么这件事需要前端发一个请求
 *
 * v0.41 时应急响应只是 CTF2 里的一个题分类，案例靠
 * `scripts/run_ir_closed_loop.py` 直接写库造出来 —— 也就是说
 * **没有任何办法从界面上发起一次复盘**。侧栏那个「应急响应」入口
 * 如果只能看不能建，它就只是一块提示牌。
 *
 * ## 附件为什么不在这个请求里
 *
 * 任务一创建就会被调度器毫秒级认领，"建完再传附件"的窗口不存在
 * （扫描附件那边已经被实测抓过一次）。所以顺序是：
 *
 *     ① `stageAttachments(files)`  → stagingId
 *     ② `createIrTask({ ..., attachmentStagingId })` → 后端认领附件
 *     ③ `solveChallenge(id)`       → 真正开跑
 *
 * 第 ② 步之后**不等于已经开跑**：建任务只是把证据包摆好。这个拆分
 * 是刻意的 —— 用户应该有机会在建完之后、开跑之前看一眼"证据包里有
 * 哪几份文件"，因为传错包是最常见的一种错误，而它一旦开跑就会消耗
 * 一整轮 LLM 额度。
 *
 * ## `notice` 必须显示
 *
 * 附件批次过期（暂存只留 2 小时）时后端**照建不误**，只在 `notice`
 * 里说明。不显示它，用户会得到一次"跑了半天说我没给证据"的复盘，
 * 而磁盘上明明有一批文件 —— 那是我们静默丢掉了他的输入。
 */
export async function createIrTask(payload: {
  title: string
  brief?: string
  attachmentStagingId?: string | null
}): Promise<IrTask> {
  return api.post<IrTask>('/ctf2/ir/tasks', {
    title: payload.title,
    brief: payload.brief ?? '',
    attachmentStagingId: payload.attachmentStagingId ?? null,
  })
}

/**
 * 读 token 统计。
 *
 * `filter` 里没给的维度就**真的不传**（`buildUrl` 会丢掉 null）——
 * 这一点很重要：后端把 query 参数当成筛选条件，传一个空串会筛出空集，
 * 而那看起来像"总共就花了 0"。
 */
export async function loadCtf2Usage(
  filter: { challengeId?: string | null; competitionId?: string | null } = {},
): Promise<Ctf2TokenUsage> {
  return api.get<Ctf2TokenUsage>('/ctf2/usage', {
    query: {
      challengeId: filter.challengeId ?? null,
      competitionId: filter.competitionId ?? null,
    },
  })
}

/** 读单道题的**逐次**消耗明细（按轮次升序）。 */
export async function loadCtf2ChallengeUsage(challengeId: string): Promise<Ctf2UsageEntry[]> {
  return api.get<Ctf2UsageEntry[]>(
    `/ctf2/challenges/${encodeURIComponent(challengeId)}/usage`,
  )
}

/**
 * 这道题**有没有可能已经有题解** —— 决定要不要露出「题解」入口。
 *
 * ## ★★ 判据是 `scanId`，不是 `status`（2026-10-15 用真实数据修）
 *
 * 第一版这里用的是状态白名单（`solved | failed | candidate_ready`）。
 * 那是个**真 bug**，被真实数据当场打出来：
 *
 *   `[ACTF2020 新生赛]Upload` 跑满 78 轮、51 分钟、**未解出**，
 *   后端收尾时把它回到 `unsolved` —— 而它**有题解**（`solved=False`
 *   照样产出，见 `writeup.py` 的"未解出也产出"）。按状态判断会把
 *   这道题的入口**藏起来**，用户永远看不到那份题解。
 *
 * 而且这类题不是个例：库里 `unsolved` 且有壳 scan 的题有 3 道，
 * 有题解的这 1 道正好是 `unsolved`。
 *
 * ## 正确的判据：有没有**借过壳 scan**
 *
 * `Ctf2Challenge.scanId` 在**第一次开始解题**时被写上（CTF2 借一条
 * scan 复用活动流，见 `ctf2/events.ensure_solve_scan`），之后**不再清空**。
 * 所以"`scanId` 非空" ⟺ "这道题至少被解过一次"。
 *
 * 这与后端 `service.has_solve_attempt`（查 `ctf2_solve_attempts` 受理快照）
 * **想表达同一件事**：都是"有没有真的跑过"，而不是"现在停在哪个状态"。
 * 但两者**在真实数据上并不等价**（全库 135 道逐题比过）：
 *
 *   · `scanId` 非空 ⟺ 至少开始过一次解题（这个列不会被清空）；
 *   · `has_solve_attempt` 只覆盖 **v0.28 之后**的解题 ——
 *     `ctf2_solve_attempts` 是迁移 `20261011_2330` 才建的表，
 *     所以 9/30–10/8 那批解出的题（20 道 `solved`，都有
 *     `confirmedFlag`）**没有 attempt 行**。
 *
 * ⚠️ 因此前端用 `scanId`、后端用 attempts 时，对那些老题会出现
 *    **不一致**：界面给入口（对，它有题解价值），而后端回
 *    「这道题还没有解过」—— 对一道已解出的题是**假话**。
 *    这是**后端判据**的问题，已单独报给 Dev（建议改成
 *    "attempt 存在 **或** scanId 非空"）。前端这边选 `scanId` 是对的：
 *    它是唯一能覆盖全量历史的证据。
 *
 * ## 什么情况仍然不显示
 *
 * `scanId` 为空 = 从没开始过解题 → 后端 409「点「解题」跑一次」，
 * 入口摆出来就是让人点一次才知道没有（"入口本身就是一句承诺"，
 * 与 `ScanList` 只给夺旗任务显示题解同一条）。
 *
 * 正在跑的题（`solving` / `environment_ready` / `submitting`）：
 * 此刻 `scanId` 已经非空，所以入口会显示 —— 这是**对的**：
 * 后端对"跑过但没生成题解"的那句话是准确的，且跑完一轮才有题解。
 * 用户点开看到"这次没有生成题解，重新解一次会生成"也是有用的信息。
 */
export function canHaveWriteup(challenge: Ctf2Challenge): boolean {
  return Boolean(challenge.scanId)
}

/**
 * 这道题的卡片上要不要露「处置工单」入口（v0.41）。
 *
 * ## ★ 判据是**分类**，不是 `scanId` —— 与 `canHaveWriteup` 刚好相反
 *
 * 题解对**每道解过的题**都有（任何分类都可能产出）；工单只有
 * **应急响应任务**才有 —— 普通 CTF 题的提示词里根本没让模型写
 * `workorders`，后端返回 409。
 *
 * 所以：
 * - `canHaveWriteup` 判 `scanId`（跑过就有）；
 * - 本函数判 `category` 前缀（是不是 `ir-*`）。
 *
 * 拿 `scanId` 来判工单会怎样：每一道跑过的 pwn 题都露出一个点开
 * 就报错的按钮。那正是「入口本身就是一句承诺」被违反的样子。
 *
 * ## 为什么不写成"分类属于某个白名单"
 *
 * 前缀判定与后端 `notes.is_ir_category` **逐字对应**。写成三条硬编码
 * （`ir-root-cause` / `ir-timeline` / `ir-scope`）会让两处各维护一份
 * 清单，而新增第四个 `ir-*` 分类时必然漏改前端这一份 —— 症状是
 * "后端能跑、界面上没入口"，且没有任何报错。
 *
 * ⚠️ 大小写与前缀空白：后端 `is_ir_category` 做了 `strip()`，
 * 这里也要 `trim()`。平台的分类名理论上不会带空白，但"理论上"不是
 * 一个能写进代码的依据 —— 两侧归一化规则不一致时，错的那一侧
 * 永远不会有报错。
 */
export function canHaveWorkOrder(challenge: Ctf2Challenge): boolean {
  return (challenge.category || '').trim().toLowerCase().startsWith('ir-')
}

// ─────────────────────────────────────────────
// store
// ─────────────────────────────────────────────
//
// 用 `useSyncExternalStore` 而不是 Context：与 `scan-retention` 同一个模式。
// 判据是**它会不会被非组件代码读** —— 解题受理之后要立刻刷新列表，
// 而那发生在事件处理器里，不是渲染期。

export type Ctf2Status = 'idle' | 'loading' | 'ready' | 'error'

export interface Ctf2SnapshotState {
  account: Ctf2Account | null
  challenges: Ctf2Challenge[]
  settings: Ctf2Settings | null
  queue: Ctf2Queue | null
  status: Ctf2Status
  error: ApiError | null
  /** 最近一次同步/受理的结果，供页内提示用 */
  notice: string | null
  /**
   * 设置读不到的原因（可空）。
   *
   * ★ 为什么要单独存它：`/ctf2/settings` 是 admin 级，而只有 read 的
   *   会话会拿到 403。那时界面回落到 `safe` 是**对的**，但
   *   "设置为什么不见了"必须有答案 —— 只把失败吞掉会让它变成一个
   *   无法解释的现象（某人为了策略改了设置，刷新后看到的是默认值）。
   */
  settingsError: ApiError | null
}

const listeners = new Set<() => void>()

let account: Ctf2Account | null = null
let challenges: Ctf2Challenge[] = []
let settings: Ctf2Settings | null = null
let queue: Ctf2Queue | null = null
let status: Ctf2Status = 'idle'
let error: ApiError | null = null
let notice: string | null = null
let settingsError: ApiError | null = null

let snapshot: Ctf2SnapshotState = {
  account,
  challenges,
  settings,
  queue,
  status,
  error,
  notice,
  settingsError,
}

function commit() {
  snapshot = { account, challenges, settings, queue, status, error, notice, settingsError }
  for (const listener of listeners) listener()
}

export function subscribeCtf2(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function getCtf2Snapshot(): Ctf2SnapshotState {
  return snapshot
}

export function clearCtf2Notice(): void {
  notice = null
  commit()
}

/**
 * 拉一次全量。
 *
 * ## ★ 为什么用 `/snapshot` 而不是分别拉三个端点
 *
 * 账号、题目、环境在任何一次有意义的展示里都会**同时**被需要。
 * 分三次请求会让界面出现"题目出来了、环境还没出来"的中间闪烁，
 * 而那个闪烁看起来就像 bug。后端为此专门提供了 `/snapshot`。
 *
 * ⚠️ 三件事是**并行**发的（`Promise.all`），但**失败策略不同**：
 * 快照失败 → 整页错误；设置与队列失败 → 只是不显示那两处提示。
 * 理由是它们的**后果不同**：没有题目列表这一页就没意义，
 * 而没有队列数字只少一行小字。
 */
export async function loadCtf2(options: { silent?: boolean } = {}): Promise<void> {
  const { silent = false } = options
  if (!silent) {
    status = 'loading'
    error = null
    commit()
  }

  try {
    const [snapshotData, settingsOutcome, queueOutcome] = await Promise.all([
      api.get<Ctf2Snapshot>('/ctf2/snapshot'),
      // 设置是 admin 级 —— 一个只有 read 的会话会拿到 403。
      // 那种情况下**不该让整页失败**（题目列表本来就能看），
      // 所以它自己 catch。但失败原因要留下来（见 settingsError）。
      api
        .get<Ctf2Settings>('/ctf2/settings')
        .then((value) => ({ ok: true as const, value }))
        .catch((err: unknown) => ({ ok: false as const, error: toApiError(err) })),
      api
        .get<Ctf2Queue>('/ctf2/queue')
        .then((value) => ({ ok: true as const, value }))
        .catch(() => ({ ok: false as const, error: null })),
    ])
    account = snapshotData.account
    challenges = snapshotData.challenges
    if (settingsOutcome.ok) {
      settings = settingsOutcome.value
      settingsError = null
    } else {
      settingsError = settingsOutcome.error
    }
    queue = queueOutcome.ok ? queueOutcome.value : null
    status = 'ready'
    error = null
  } catch (err) {
    status = 'error'
    error = toApiError(err)
  }
  commit()
}

/**
 * 同步。**会真的调平台**，所以慢是正常的。
 *
 * ## ★ 必须覆盖默认超时，否则会报**假失败**
 *
 * `api.ts` 的 `DEFAULT_TIMEOUT_MS` 是 20 秒，而首次同步要跑
 * **98+ 个平台请求**（客户端限速 5 req/s，那是平台的礼貌线），
 * 实测约 **45 秒**。用默认值的话前端到点就 abort，界面上弹出
 * "同步失败：请求超时或被取消" —— 而后端**其实每次都跑完了**
 * （`last_synced_at` 正常推进、题目也在库里）。
 *
 * 实测（2026-10-01，用户截图）：那条假失败就是这么来的。
 * 用户看到"失败"就会再点一次，于是永远卡在同一个错觉里。
 *
 * 后端那边已经做了配套优化：**侧门只补新的题目 id**，所以
 * 「重复同步」现在只发一个请求、秒级返回 —— 120 秒这个值是留给
 * **首次同步**（或一次补进很多新题）的。
 *
 * ⚠️ 与 `api.postFile` 的 `timeoutMs: 60_000` 是同一条理由：
 *    "这一步本来就要几十秒"的调用不该套默认值。
 */
export async function syncCtf2(competitionIds: string[] = []): Promise<Ctf2SyncResult> {
  const result = await api.post<Ctf2SyncResult>(
    '/ctf2/sync',
    { competitionIds },
    { timeoutMs: 120_000 },
  )
  notice = result.message + (result.errors.length ? `（${result.errors[0]}）` : '')
  await loadCtf2({ silent: true })
  return result
}

/**
 * **平台上**有哪些赛事（即时查询，不写库）。
 *
 * ★ 为什么不复用本地那份 `competitions`：本地库里的赛事正是**上一次同步
 *   的结果** —— 第一次用的时候它是空的，而"要选赛事才能同步赛事"就成了
 *   一个死结。所以这一份必须直接问平台。
 *
 * 它走 `GET /ctf2/competitions/remote`，需要 **scan** 权限：虽然只读，
 * 但它每次都真的打一次平台接口。
 */
export async function listRemoteCompetitions(): Promise<Ctf2RemoteCompetition[]> {
  return api.get<Ctf2RemoteCompetition[]>('/ctf2/competitions/remote')
}

/**
 * 按题目链接/ID 拉取**一道**练习集题目。
 *
 * ★ 它是唯一能真正拿到练习集题目的路：平台的开放 API 没有"列出题目"
 *   的接口（实测 11 条候选路径全部 `resource=route`），而"按 id 取单题详情"
 *   是活的。所以从平台页面上复制题目链接过来，服务端抠出 id 精确拉取。
 *
 * 服务端会自己在已知练习集里试出这道题属于哪个，所以只需要一条链接。
 */
export async function pullCtf2Challenge(input: string): Promise<Ctf2PullResult> {
  const result = await api.post<Ctf2PullResult>('/ctf2/challenges/pull', { input })
  await loadCtf2({ silent: true })
  return result
}

export async function bindCtf2Account(token: string, label: string): Promise<Ctf2Account> {
  const created = await api.post<Ctf2Account>('/ctf2/accounts', { token, label })
  account = created
  await loadCtf2({ silent: true })
  return created
}

export async function unbindCtf2Account(accountId: string): Promise<void> {
  await api.del<void>(`/ctf2/accounts/${encodeURIComponent(accountId)}`)
  account = null
  commit()
  await loadCtf2({ silent: true })
}

export async function saveCtf2Settings(next: {
  submitMode: Ctf2SubmitMode
  maxWrongSubmissions: number
  defaultSandboxProfile: Ctf2SandboxProfile
  maxConcurrentSolves: number
  solvePollSeconds: number
  autoSyncSeconds: number
  autoPriorityNew: boolean
  autoStartNew: boolean
  maxTurns: number
  fallbackProviderId: string | null
  /**
   * 智能体安全评测开关（v0.40）。
   *
   * ★ **必填，不给默认值** —— 与设置页其他字段同一条纪律。这里不是
   * "可选的额外参数"，而是"这次保存要写进去的那个值"。给默认 `false`
   * 会让"调用方忘了传"变成"静默关掉一个用户已经打开的开关"。
   *
   * ⚠️ `PUT /ctf2/settings` 是**全量提交**：后端逐字段赋值，没传的
   * 字段按后端形参默认值落库。所以这个字段一旦漏传，开关会被静默关掉，
   * 而界面上看不出发生过这件事 —— 那正是它这么写的原因。
   */
  enableAgentsecScenarios: boolean
  /** 应急响应场景包开关（v0.41）。必填，同上 —— 漏传会被静默关掉。 */
  enableIrScenarios: boolean
}): Promise<Ctf2Settings> {
  const saved = await api.put<Ctf2Settings>('/ctf2/settings', next)
  settings = saved
  settingsError = null
  commit()
  return saved
}

/**
 * 开始解题。
 *
 * ⚠️ 它返回的是**受理凭据**，不是 flag —— 解题要跑几分钟到几十分钟，
 * 同步等它意味着一个必然超时的请求（见后端 router 的说明）。
 * 真正的进展走活动流。
 */
export async function solveChallenge(
  challengeId: string,
  body: {
    mode?: Ctf2SubmitMode
    sandboxProfile?: Ctf2SandboxProfile
    maxWrongSubmissions?: number
    brief?: string | null
    /**
     * 续解（v0.26）：接着上一次的进度跑，而不是从零开始。
     *
     * ★ 实测（`[FlareOn3]GUI`）：一道 .NET 逆向题跑满 40 轮、发出
     * 37 条各不相同的命令，最后因为预算耗尽而停。那 37 步是真金白银
     * 换来的，而"再解一次"会让模型把它们再做一遍。
     *
     * ⚠️ 后端找不到上次的过程文件时**不是错误**，会退回从头开始。
     */
    resume?: boolean
  },
): Promise<Ctf2SolveResult> {
  const result = await api.post<Ctf2SolveResult>(
    `/ctf2/challenges/${encodeURIComponent(challengeId)}/solve`,
    body,
  )
  notice = result.message
  await loadCtf2({ silent: true })
  return result
}

export async function submitFlag(
  challengeId: string,
  flag: string,
  mode?: Ctf2SubmitMode,
): Promise<Ctf2SubmitResult> {
  const result = await api.post<Ctf2SubmitResult>(
    `/ctf2/challenges/${encodeURIComponent(challengeId)}/submit`,
    { flag, mode },
  )
  notice = result.message
  await loadCtf2({ silent: true })
  return result
}

export async function cancelChallenge(challengeId: string): Promise<Ctf2Challenge> {
  const updated = await api.post<Ctf2Challenge>(
    `/ctf2/challenges/${encodeURIComponent(challengeId)}/cancel`,
  )
  notice = '已取消。若它已经在跑，那一轮会跑完但不会产出候选。'
  await loadCtf2({ silent: true })
  return updated
}

export async function releaseEnvironment(challengeId: string): Promise<void> {
  await api.del<void>(`/ctf2/challenges/${encodeURIComponent(challengeId)}/environment`)
  notice = '已释放题目环境。'
  await loadCtf2({ silent: true })
}

/**
 * 设置一道题的调度优先级。**越大越先做**，默认 0。
 *
 * ## 为什么这是比赛现场会用到的动作
 *
 * 规则里写着「每道题目前三名成功解题的团队将额外获得本题分值的
 * 15%、10%、5%」—— 速度直接换分；而同一份规则也允许队伍
 * 「调整解题优先级」。所以这是一个**被明确允许并期待**的操作，
 * 不是绕过机制。
 *
 * ⚠️ 它**不影响已经在跑的题**：只有还在排队的会被重新排序。
 *   中断一道跑到一半的题会浪费掉已经消耗的平台环境与 LLM 调用。
 */
export async function setChallengePriority(
  challengeId: string,
  priority: number,
): Promise<Ctf2Challenge> {
  const updated = await api.put<Ctf2Challenge>(
    `/ctf2/challenges/${encodeURIComponent(challengeId)}/priority`,
    { priority },
  )
  await loadCtf2({ silent: true })
  return updated
}

/**
 * 按分值给**排队中**的题批量设优先级。返回改了几道。
 *
 * ★ 它不会自动跑 —— 必须由人点。理由见后端
 *   `service.set_priorities_by_score` 的说明：高分题往往更难，
 *   "先做高分"还是"先做容易"是战术判断，取决于当前比分与剩余时间。
 */
export async function prioritizeByScore(
  competitionId?: string | null,
): Promise<number> {
  const result = await api.post<{ changed: number }>('/ctf2/priorities/by-score', {
    competitionId: competitionId ?? null,
    limit: 50,
  })
  notice =
    result.changed > 0
      ? `已按分值调整 ${result.changed} 道排队题的优先级。`
      : '当前没有排队中的题，无需调整。'
  await loadCtf2({ silent: true })
  return result.changed
}

// ══════════════════════════════════════════════════════════
// 平台接入（v0.25）
// ══════════════════════════════════════════════════════════
//
// ## 这一块解决什么
//
// 之前"平台"是写死的 CTF2。比赛要换成赛事方给的 API（地址、认证头、
// 端点路径、字段名全不同），于是有了"填个表 → agent 自己识别 → 启用"
// 这条路。
//
// ## ★ 为什么生成要轮询，而不是等一个请求返回
//
// 一次识别最坏两分钟（探测几十个候选端点 + 最多三次大模型调用 + 试跑）。
// 挂在一次请求上**必然超时** —— 而超时之后后台还在跑：用户看到"失败"，
// 重试一次却"成功"，看起来像个偶发 bug。
//
// 所以 `POST .../draft` 只负责受理（返回 202），真正的进度从
// `GET /ctf2/platforms/{id}` 轮询 —— 那里的 `isBuilding` 就是"还要不要
// 继续轮"的判据。

export type Ctf2PlatformDriver = 'ctf2' | 'generic'
export type Ctf2PlatformStatus = 'building' | 'draft' | 'verified' | 'failed'

export interface Ctf2Platform {
  /**
   * `null` = **内置的 CTF2**。
   *
   * ⚠️ 它不是数据库里的一行（后端刻意不插初始数据，为的是让"账号用哪个
   * 平台"只有一种表示法）。所以对 `null` 的条目不要调详情/启用/删除接口。
   */
  id: string | null
  slug: string
  displayName: string
  baseUrl: string
  driver: Ctf2PlatformDriver
  source: 'builtin' | 'generated'
  status: Ctf2PlatformStatus
  lastError: string | null
  attempts: number
  isBuilding: boolean
  /** 当前账号正在用这个平台。 */
  active: boolean
  /** 模型最后一次的原始输出。**失败时才有意义**（排查"为什么识别不对"）。 */
  lastRaw: string | null
  createdAt: string | null
  updatedAt: string | null
}

export const PLATFORM_STATUS_LABEL: Record<Ctf2PlatformStatus, string> = {
  building: '识别中',
  draft: '待验证',
  verified: '可用',
  failed: '识别失败',
}

export const PLATFORM_STATUS_TONE: Record<Ctf2PlatformStatus, 'ok' | 'warn' | 'risk' | 'idle'> = {
  building: 'idle',
  draft: 'warn',
  verified: 'ok',
  failed: 'risk',
}

export async function listCtf2Platforms(): Promise<Ctf2Platform[]> {
  return api.get<Ctf2Platform[]>('/ctf2/platforms')
}

/**
 * 受理一次"识别平台"。**立刻返回**（后端 202），进度靠 `readCtf2Platform` 轮询。
 *
 * ⚠️ `accessKey` 在这里只走一次内存：后端用它去探测和试跑，**不落库**。
 * 真正保存要等用户在结果上点「启用」。
 */
export async function draftCtf2Platform(body: {
  baseUrl: string
  accessKey: string
  document: string
}): Promise<Ctf2Platform> {
  return api.post<Ctf2Platform>('/ctf2/platforms/draft', {
    baseUrl: body.baseUrl.trim(),
    accessKey: body.accessKey.trim(),
    document: body.document,
  })
}

export async function readCtf2Platform(id: string): Promise<Ctf2Platform> {
  return api.get<Ctf2Platform>(`/ctf2/platforms/${encodeURIComponent(id)}`)
}

/** 启用：后端会**再验一次**凭据（中间隔着一段不确定的窗口），然后写进账号表。 */
export async function activateCtf2Platform(
  id: string,
  body: { accessKey: string; label?: string },
): Promise<Ctf2Platform> {
  return api.post<Ctf2Platform>(`/ctf2/platforms/${encodeURIComponent(id)}/activate`, {
    accessKey: body.accessKey.trim(),
    label: (body.label ?? '').trim(),
  })
}

/**
 * 切回内置 CTF2。**不删**任何配置 —— 那些平台还留在列表里。
 *
 * ⚠️ 名字**刻意不以 `use` 开头**：`useXxx` 会被 eslint 的
 * `rules-of-hooks` 当成 React Hook，于是在组件里调用它就会报
 * "Hook 不能在普通函数里调用"。这是一个纯 API 函数，与 React 无关。
 */
export async function switchToBuiltinPlatform(): Promise<void> {
  await api.post<void>('/ctf2/platforms/use-builtin', {})
}

export async function deleteCtf2Platform(id: string): Promise<void> {
  await api.del<void>(`/ctf2/platforms/${encodeURIComponent(id)}`)
}

/**
 * 有没有"正在动"的题 —— 决定要不要开轮询。
 *
 * ★ 只有这些状态才轮询。`solved` / `unsolved` / `failed` 都不会自己
 *   变化（它们要等一个外部动作），为它们轮询是白烧请求。
 *
 * ⚠️ `candidate_ready` **算在里面**：它虽然"在等人"，但那道题的
 *   `wrongSubmissionCount` 可能因为别处的操作而变（用户可能在另一个
 *   标签页里提交了），而界面必须跟上。
 */
export function hasActiveChallenges(items: Ctf2Challenge[]): boolean {
  return items.some((item) =>
    ['environment_ready', 'solving', 'submitting', 'candidate_ready'].includes(item.status),
  )
}

export function useCtf2(): Ctf2SnapshotState {
  return useSyncExternalStore(subscribeCtf2, getCtf2Snapshot, getCtf2Snapshot)
}

/** 便捷取当前状态（非组件代码用，比如事件处理器） */
export function currentCtf2State(): Ctf2SnapshotState {
  return getCtf2Snapshot()
}
