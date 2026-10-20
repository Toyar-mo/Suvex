import { api, ApiError, toApiError } from '@/lib/api'
import { useSyncExternalStore } from 'react'
import { handleUnauthorized } from './auth-store'
import {
  PROVIDER_PRESETS,
  type ApiToken,
  type NoticeRule,
  type NotifyChannel,
  type ProviderAccount,
  type SandboxConfig,
  type TokenScope,
} from './settings'
import type { ProviderConfig } from './providers'

/**
 * 设置的**单一运行时事实来源**。
 *
 * 与 findings-store / scans-store / reports-store 同一套模式
 * （useSyncExternalStore + 整体替换快照），理由也一样：
 * 设置页改了 provider，新建扫描向导的第三步必须跟着变 ——
 * 那正是 data/providers.ts 顶部注释里写明的需求（"配置完成后回到这里，
 * 新配置的模型会自动出现"）。各自持一份快照就会漂移。
 *
 * ┌─ 接真实后端时改哪里 ──────────────────────────────────────┐
 * │ 初始化从 `seedAccounts()` 换成 GET /settings/providers；    │
 * │ save* 系列换成 PATCH 请求 + 乐观更新 + 失败回滚。            │
 * │ 订阅接口不变，组件不改。                                     │
 * └──────────────────────────────────────────────────────────┘
 *
 * ★ 关于"保存"的语义 —— 这是本页最容易做错的地方。
 *
 *   上一版考虑过"改完即时生效"（受控输入直接写 store）。放弃了，原因：
 *   API key 这类字段，用户**必须能明确知道"存住了没有"**。
 *   即时保存有三个说不清的地方：
 *     1. 打字打到一半就是一次"保存"，历史里全是半截值；
 *     2. 用户看不到"已保存"这个确认，只能靠猜；
 *     3. 校验失败的中间态会被写进 store —— 半截的 base URL 也是"已保存"。
 *
 *   所以这里采用**显式保存**：表单持有草稿，点保存才写入 store。
 *   保存结果由 store 返回（成功/失败/改了什么），组件据此显示持久状态，
 *   而不是飘一下就没的提示。
 */

type Listener = () => void

const listeners = new Set<Listener>()
function emit() {
  for (const listener of listeners) listener()
}
export function subscribeSettings(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** 把任意异常收敛成给用户看的一句话 */
function messageOf(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error && error.message) return error.message
  return '请求失败，请稍后重试。'
}

// ─────────────────────────────────────────────
// 初始数据
// ─────────────────────────────────────────────

/**
 * 初始 provider 账号。
 *
 * ★ 从 PROVIDER_PRESETS 推，不手写 —— 与报告页的种子数据同一个理由：
 *   手写意味着 label/model/baseUrl 要在预设和种子里各维护一份，
 *   迟早分叉。预设改了默认模型，种子这里没跟上，"新增 provider" 时
 *   用户看到的默认值和已配置的那条不一致。
 */
/**
 * 从预设生成一个账号骨架 —— 新增 provider 时用它填默认值。
 *
 * ⚠️ 这里刻意**不填凭据相关字段**：`apiKey` / `hasKey` / `apiKeyMask` /
 *    `verified` / `verifyError` 全部由服务端拥有。少 omit 一个，
 *    调用方就会以为"这里能填个默认值"，而那正是客户端自称
 *    "已验证可用"的入口 —— 后端 `extra="forbid"` 会拒绝它，
 *    但报错发生在运行期，而不是这里。
 */
export function fromPreset(
  preset: (typeof PROVIDER_PRESETS)[number],
  id: string,
): Omit<
  ProviderAccount,
  'isDefault' | 'apiKey' | 'verified' | 'verifyError' | 'hasKey' | 'apiKeyMask'
> {
  return {
    id,
    label: preset.label,
    model: preset.defaultModel,
    kind: preset.kind,
    baseUrl: preset.defaultBaseUrl,
    note: preset.note,
  }
}

/**
 * provider 账号列表。
 *
 * ★ **初始为空** —— 原来是 `seedAccounts()` 造的两条假数据
 *   （`sk-demo-…` + "已验证可用"）。任务 7 把它删了，因为在这里假数据
 *   不只"占位"那么无害：界面会显示两个**看起来可用**的模型，而新建扫描
 *   向导的 `configured` 读的正是 `verified` —— 用户选中一个根本没有凭据的
 *   provider，直到扫描跑到一半才认证失败。
 *
 * 真实数据由 `loadSettings()` 从 `GET /providers` 拉取；
 * 加载期间 SettingsPage 显示骨架（见那边的三态）。
 */
let accounts: ProviderAccount[] = []

/**
 * ★ 系统状态（沙箱 / 通知 / 令牌）的初始数据。
 *
 * 时间用相对量生成，让"3 小时前"这类显示始终成立 ——
 * 与其余 demo 时间戳用 ago() 是同一个约定。
 */
function ago(seconds: number): string {
  return new Date(Date.now() - seconds * 1000).toISOString()
}

/**
 * 沙箱配置的**占位初值**。
 *
 * ★ `status` 是 `'stopped'` 而不是演示数据里的 `'running'`：
 *   真实实例在加载完成前不该宣称沙箱在跑（那是状态语义撒谎，
 *   与契约拒绝把 `queued` 写成 `running` 是同一条原则）。
 *   加载完成后这份初值会被 `GET /settings/sandbox` 的真实值覆盖。
 *
 * ★ 执行类三件套的初值同样是占位 —— 真实值由服务端算好（DB 覆盖 env），
 *   `executionSource` 会说明它来自哪一侧。
 */
let sandbox: SandboxConfig = {
  engine: 'docker',
  status: 'stopped',
  image: 'suvex/sandbox:1.4',
  cpuLimit: 4,
  memoryLimitMb: 4096,
  timeoutMinutes: 30,
  networkIsolated: false,
  allowVolumeWrite: false,
  maxConcurrentScans: 1,
  scanMaxTurns: 500,
  scanMode: 'standard',
  executionSource: {
    maxConcurrentScans: 'default',
    scanMaxTurns: 'default',
    scanMode: 'default',
  },
  // ⚠️ 占位值取 `false`：它是**唯一安全的初值** —— 加载完成前
  //   界面不该显示"更强的手法已打开"。加载后被真实值覆盖。
  enableOffensivePrompt: false,
}

let notices: { rule: NoticeRule; channels: NotifyChannel[] } = {
  rule: {
    minSeverity: 'high',
    notifyOnScanComplete: true,
    notifyOnCriticalImmediate: true,
  },
  channels: [
    {
      id: 'ch_email',
      kind: 'email',
      label: '安全组邮件列表',
      target: 'security@acme.internal',
      enabled: true,
      verified: true,
      verifyError: null,
    },
    {
      id: 'ch_slack',
      kind: 'slack',
      label: '研发值班群',
      target: 'https://hooks.slack.com/services/T000/B000/xxxx',
      enabled: false,
      // 未验证的渠道要能说明原因，而不是只显示一个灰点
      verified: false,
      verifyError: '尚未发送过测试消息',
    },
  ],
}

let tokens: ApiToken[] = [
  {
    id: 'tok_01',
    label: 'CI 流水线',
    prefix: 'svx_7f2a',
    createdAt: ago(90 * 24 * 3600),
    lastUsedAt: ago(2 * 3600),
    expiresAt: null,
    scopes: ['read', 'scan'],
  },
  {
    id: 'tok_02',
    label: '报表导出脚本',
    prefix: 'svx_19c4',
    createdAt: ago(30 * 24 * 3600),
    lastUsedAt: null,
    expiresAt: new Date(Date.now() + 60 * 86_400_000).toISOString(),
    scopes: ['read'],
  },
]

// ─────────────────────────────────────────────
// 快照
// ─────────────────────────────────────────────

/** 设置数据的加载状态。接入真实后端后才有意义（之前是内存态，永远 ready） */
export type SettingsStatus = 'loading' | 'ready' | 'error'

/**
 * ★ **后端还没交付的设置区块**。
 *
 * ## v0.22：通知也摘掉了，这个常量现在是空的
 *
 * 它此前标着 `notices: true`，因为 `GET/PUT /settings/notices/*` 那几个
 * 端点未实现。后端 v0.22 交付了全部五个（含 `GET /settings` 的四块快照），
 * 所以通知也走真实接口了。
 *
 * ⚠️ **这个常量留着而不是删掉**，因为它表达的是一条**长期规矩**：
 *    某个区块的后端还没交付时，前端只能保持内存态，而界面**必须说出来** ——
 *    让用户以为"通知已经配好了"是最糟的，他会据此以为漏洞告警真的会发出去。
 *
 *    规矩的两个方向都要守（`sandbox` 那次是反向的例子）：
 *      · 未交付却不说 → 用户以为配置生效了（虚假的安心）；
 *      · 已交付却还标着未交付 → **否认一个已经可靠的功能**
 *        （与吹嘘没有的能力是同一种失真，只是方向相反）。
 *
 *    所以摘掉 `notices` 与当初摘掉 `sandbox` 是同一条判断：
 *    **提示必须与事实一致**，而不是"留着更保险"。
 */
export const BACKEND_PENDING = {
  sandbox: false,
  notices: false,
} as const

/** 是否还有未交付的区块 —— 有的话面板要显示提示条 */
export const HAS_PENDING_BLOCK = Object.values(BACKEND_PENDING).some(Boolean)

/**
 * 未交付区块的**保存提示**，直接挂在 `SaveBar` 的 `note` 上。
 *
 * ★ 为什么必须挂在保存按钮旁边，而不是只在页面底部说一次：
 *   用户改完配置、点"保存"、看到绿色的"已保存 15:30:42" ——
 *   他会合理地认为**配置已经生效且会持久**。而实际上刷新就回到初始值。
 *   这个误解的后果不轻：他可能据此以为沙箱策略已经收紧。
 *
 *   放在页面底部说明是**不够的** —— 那要用户先滚到底、再把它和
 *   上面那个"已保存"联系起来。提示必须出现在产生误解的那一刻。
 *
 * ⚠️ v0.22 起**没有**未交付区块了，所以这句话暂时不会被显示。
 *    留着它是给下一个新增区块用的 —— 届时改 `BACKEND_PENDING` 即可，
 *    文案不用重写（那句话本身仍然准确）。
 */
export const BACKEND_PENDING_NOTE =
  '这一块的后端接口尚未交付：保存在内存里，刷新页面会回到初始值。'

export interface SettingsSnapshot {
  accounts: ProviderAccount[]
  sandbox: SandboxConfig
  notices: { rule: NoticeRule; channels: NotifyChannel[] }
  tokens: ApiToken[]
  /** 数据加载状态。`tokens` 现在来自后端，所以它有一个真实的加载过程 */
  status: SettingsStatus
  error: ApiError | null
}

let dataStatus: SettingsStatus = 'loading'
let loadError: ApiError | null = null

/**
 * 「首次加载是否已经发起过」。
 *
 * ★ 存在的理由：`ensureSettingsLoaded()` 会被**外壳**（Layout）调用 ——
 *   顶部状态条要知道默认 provider 才能显示"模型 XXX"。
 *   而 store 的 `dataStatus` 初值就写作 `'loading'`，拿它判断
 *   "是不是正在加载"会把**第一次**调用也挡掉，于是永远没人加载。
 *
 *   所以用一个独立的布尔量，语义明确：**这个进程里有没有发起过**。
 */
let loadStarted = false

/**
 * 快照必须引用稳定 —— useSyncExternalStore 每次渲染都比较它，
 * 每次返回新对象会无限重渲染。所以所有变更都走"整体替换"。
 *
 * 注意这里每次 emit 前会重建 snapshot 对象，但**只在真的变更时**重建。
 * 未变更时不重建，引用保持稳定。
 */
let snapshot: SettingsSnapshot = {
  accounts,
  sandbox,
  notices,
  tokens,
  status: dataStatus,
  error: loadError,
}

function commit() {
  snapshot = { accounts, sandbox, notices, tokens, status: dataStatus, error: loadError }
  emit()
}

/**
 * 后端返回的 provider 形状。
 *
 * 字段名与前端 `ProviderAccount` **逐字一致**（后端 `ProviderOut` 就是照它
 * 定的），唯一差别是 `kind` 在后端是宽 `str`、前端是两档联合类型 ——
 * 所以只窄化它一个。
 */
interface ProviderOutShape extends Omit<ProviderAccount, 'kind'> {
  kind: string
}

/**
 * `ProviderOut` → `ProviderAccount`。
 *
 * ⚠️ `kind` 的未知值落到 `'cloud'` 而不是 `'local'`：这不是随手兜底，
 * 而是选**更严格**的那一侧 —— 云端要求填凭据、且必须验证通过才算可用；
 * 落到 `'local'` 会让一个没配凭据的条目在新建扫描向导里看起来能用，
 * 而错误要到"扫描跑到一半认证失败"才暴露。
 */
function toAccount(raw: ProviderOutShape): ProviderAccount {
  return { ...raw, kind: raw.kind === 'local' ? 'local' : 'cloud' }
}

/** `GET /providers`（admin 级）。掩码在 `apiKeyMask`，明文永远拿不到 */
async function loadAccounts(): Promise<ProviderAccount[]> {
  const list = await api.get<ProviderOutShape[]>('/providers')
  return list.map(toAccount)
}

/**
 * 拉取设置页的真实数据。
 *
 * ## 数据是**分源组装**的，而不是一次 `GET /settings`
 *
 *   · `tokens`   → `GET /settings/tokens`
 *   · `accounts` → `GET /providers`
 *   · `sandbox`  → `GET /settings/sandbox`
 *   · `notices`  → `GET /settings/notices/rule` + `GET /settings/notices/channels`（v0.22）
 *
 * 这个"分源"不是临时凑合：它比一次性快照**更抗演进** ——
 * 新增一块只需在这里多一行，不用推翻结构。
 *
 * ⚠️ `GET /settings`（四块快照）v0.22 起也实现了，但这里**仍然不用它**。
 *    理由：那个端点要求四块全部成功才返回，任何一块出问题整页进错误态；
 *    而分源拉可以只让**出问题的那一块**失败。当前两者行为其实一样
 *    （下面的 `Promise.all` 就是"任一路失败整页错误"），但分源保留了
 *    "将来某一块改成软失败"的余地，而聚合端点没有。
 *
 * ## ★ v0.22 修的一处真实缺口
 *
 * 这个函数原来只拉**三路**，`notices` 那一跳**根本没接** ——
 * 而 `BACKEND_PENDING.notices` 在同一轮改动里已经被置成 `false`、
 * 注释也宣称"通知走真实接口了"。
 *
 * 后果不是"少一个功能"，而是**显示假数据**：`notices` 永远停在
 * 文件上方那份内存种子（`ch_email` 安全组邮件列表、`ch_slack` 研发值班群），
 * 于是刷新设置页看到的是后端并不存在的两条渠道，而点它们的"删除"
 * 会打 `DELETE /settings/notices/channels/ch_email` → 后端 404。
 *
 * 教训：**改"这一块已交付"的声明时，必须同时确认读取路径真的接上了。**
 * 声明与实现不一致时，界面会以"看起来正常但内容是假的"方式坏掉 ——
 * 比直接报错难查得多。
 */
export async function loadSettings(): Promise<void> {
  loadStarted = true
  dataStatus = 'loading'
  loadError = null
  commit()

  try {
    // 五路**并行**拉：它们互不依赖，串行会白等四个往返。
    // ⚠️ 用 Promise.all 而不是分别 try —— 任何一路失败都该让整页进错误态，
    //    因为半加载的设置页比明确的错误页更让人困惑。
    const [remoteTokens, remoteAccounts, remoteSandbox, remoteRule, remoteChannels] =
      await Promise.all([
        api.get<ApiToken[]>('/settings/tokens'),
        loadAccounts(),
        api.get<SandboxConfig>('/settings/sandbox'),
        api.get<NoticeRule>('/settings/notices/rule'),
        api.get<NotifyChannel[]>('/settings/notices/channels'),
      ])
    tokens = remoteTokens
    accounts = remoteAccounts
    sandbox = remoteSandbox
    notices = { rule: remoteRule, channels: remoteChannels }
    dataStatus = 'ready'
    loadError = null
  } catch (error) {
    dataStatus = 'error'
    // 存 ApiError 本身，别只存 message —— 页面要靠 status 区分失败种类
    loadError = toApiError(error)
  }
  commit()
}

export function getSettingsSnapshot(): SettingsSnapshot {
  return snapshot
}
export function getSettingsServerSnapshot(): SettingsSnapshot {
  return snapshot
}

/**
 * 首次加载设置；已经发起过就**什么都不做**。
 *
 * ★ 给"只是要读一下默认 provider"的消费方用 —— 具体说就是**顶部状态条**：
 *   它在每一个登录后的页面上都渲染，而它要显示"模型 XXX"。
 *
 *   ⚠️ 没有这个函数时踩到的症状：从总览页打开控制台，状态条写着
 *     **「未配置 · 去「设置」添加模型服务」** —— 而用户明明配好了两个。
 *     因为 provider 只在 Settings 页被加载过，别处谁也没拉。
 *     这是一句**说反了的话**，比不显示更糟（它会让人去重配一遍）。
 *
 * ⚠️ 它**不能**在公开页（落地页）调用 —— 未登录时 `GET /providers` 会 401，
 *    而 `lib/api.ts` 的 401 处理会把人踢到登录页，落地页就此不可访问。
 *    所以调用点是 `Layout`（登录后的外壳），不是 `TopStatusBar` 自己。
 */
export function ensureSettingsLoaded(): void {
  if (loadStarted) return
  void loadSettings()
}

/** 订阅全部设置 */
export function useSettings(): SettingsSnapshot {
  return useSyncExternalStore(subscribeSettings, getSettingsSnapshot, getSettingsServerSnapshot)
}

// ─────────────────────────────────────────────
// 保存结果
// ─────────────────────────────────────────────

/**
 * 保存结果 —— **不是布尔**。
 *
 * 为什么不用 `Promise<boolean>`：界面需要区分三种情形，
 * 它们的措辞完全不同：
 *   · ok        → "已保存"，并显示保存时刻
 *   · nochange  → "没有需要保存的改动"（用户点了但什么都没改，
 *                 不能报"已保存"，那是在制造虚假的动作感）
 *   · failed    → "保存失败：<原因>"，且草稿必须保留，不能丢用户的输入
 *
 * 这正是验收要求里"不要用一闪而过的 toast 糊弄"的落点：
 * 结果是一个**可渲染、可停留**的数据结构，而不是一句提示语。
 */
export type SaveResult =
  { status: 'ok'; at: string } | { status: 'nochange' } | { status: 'failed'; error: string }

/**
 * **动作型**操作的结果 —— 没有 nochange 这一态。
 *
 * ★ 为什么要把「验证凭据」「发送测试」单列一个类型，而不是复用 SaveResult：
 *   两者在语义上不同 ——
 *     · 保存是"把草稿写进去"，所以存在"没有需要写的东西"（nochange）；
 *     · 验证/发送是"执行一次动作"，它**必然**产生一个结果：
 *       要么通、要么不通。不存在"无改动"。
 *
 *   复用 SaveResult 的代价是调用方必须为一个**不可能发生**的分支写代码，
 *   而 TypeScript 会正确指出"result.error 在 nochange 上不存在"。
 *   与其在调用点加一个永远走不到的兜底分支，不如让类型如实描述：
 *   非法状态在类型上就不可表达 —— 这与项目里 ReportScope 用
 *   discriminated union 而不是"三个可选字段"是同一个判断。
 *
 * ## ★ 成功分支上的 `message` 是可选的（v0.22 补）
 *
 * 有些动作成功了但**需要补一句话**才说得清。最典型的是测试通知：
 *
 *   · "地址形状没问题，但真实发送还没接入" —— 这是一个**成功执行、
 *     但没有真的送达**的结果。只说 `ok` 会让界面显示一个绿勾，
 *     而用户会据此以为告警链路通了；
 *   · 而报 `failed` 也不对 —— 动作本身确实成功执行了。
 *
 * 所以 `message` 在 ok 分支上可选：**有话说时就说，没话说时不说**。
 * 调用方（`NotifyPanel`）据此显示一句中性说明，而不是一个会撒谎的绿勾。
 *
 * ⚠️ 这个字段是**可选**的，所以既有调用方（`verifyProvider`）不受影响。
 */
export type ActionResult =
  | { status: 'ok'; at: string; message?: string }
  | { status: 'failed'; error: string }

// ─────────────────────────────────────────────
// Provider 变更
// ─────────────────────────────────────────────

let accountSeq = 0
export function nextAccountId(presetId: string): string {
  accountSeq += 1
  return `acc_${presetId}_${String(accountSeq).padStart(2, '0')}`
}

/**
 * 保存一个 provider 账号（新增或更新）。
 *
 * 「设为默认」的**互斥性在这里保证**：设置某个为默认时必须同时取消其它的。
 * 放在 store 而不是组件里，是因为这是数据不变量（至多一个默认），
 * 组件层可能从多个入口改它（列表里的星标、卡片里的开关），
 * 每个入口都记得清别人一遍 —— 迟早漏一个。
 */
/** `POST/PATCH /providers` 的响应（契约 §3.2：`SaveResult` + 附带的 `provider`） */
type ProviderSaveResponse =
  | { status: 'ok'; at: string; provider: ProviderOutShape }
  | { status: 'nochange'; provider: ProviderOutShape }

/**
 * `POST /providers/{id}/verify` 的响应（契约 §3.2）。
 *
 * ⚠️ 两支**都是 200** —— 「验证失败」是业务结果，不是 HTTP 错误。
 *    所以判别式是响应体的 `status`，**不能**用 `res.ok` 去分。
 *    用状态码分的话，一次"key 打不通"会被报成"保存失败"，
 *    而用户面对那句提示不知道该重填还是该重试。
 */
type ProviderVerifyResponse =
  | {
      status: 'ok'
      at: string
      /** 真实往返耗时 —— 用户看它就是为了判断这条链路慢不慢 */
      durationMs: number
      endpoint: string
      reply: string
      provider: ProviderOutShape
    }
  | {
      status: 'failed'
      error: string
      durationMs: number
      endpoint: string | null
      provider: ProviderOutShape
    }

/**
 * 用**服务端返回的** provider 覆盖本地那一条（没有就插入）。
 *
 * ★ 一律以服务端返回为准，不在本地推算 `verified` / `isDefault` /
 *   `apiKeyMask` —— 那三个都是服务端算出来的。本地再推一遍就有了
 *   第二个事实来源，而两个来源一定会漂移，症状是
 *   "设置页显示已验证、新建扫描向导里却说没配好"。
 */
function upsertAccount(raw: ProviderOutShape): void {
  const account = toAccount(raw)
  const exists = accounts.some((a) => a.id === account.id)
  accounts = exists
    ? accounts.map((a) => (a.id === account.id ? account : a))
    : [...accounts, account]
  commit()
}

/**
 * 保存一个 provider 账号 —— `POST /providers` 或 `PATCH /providers/{id}`。
 *
 * ## ★ 这里最关键的一条：`apiKey` 什么时候才发出去
 *
 * `ProviderAccount.apiKey` 接入后端后**恒为空串**（后端 `from_model` 硬编码，
 * 那条路径上没有任何变量能通向明文）。所以判据**不是**"字段有没有值"，
 * 而是"用户这次有没有输入"：
 *
 * | 场景 | 请求里的 apiKey |
 * | --- | --- |
 * | 用户新填了 key（表单草稿里有值） | 原样发出 |
 * | 用户没动 key（草稿里是空串） | **完全不发这个字段** |
 *
 * 第二条是必须的：契约 §2.9 把「省略 apiKey」定义为"保持原值不变"，
 * 而把空串发过去会被（正确地）理解成"要改成空" —— 于是
 * **用户只改了个显示名，凭据就被清空了**。契约 §7 专门警告过这个隐蔽 bug。
 *
 * 后端另有一层防御：连掩码本身传回去也会被识别为"不改"
 * （`crypto.looks_like_mask`）。那是兜底，不是我们偷懒的理由。
 *
 * ⚠️ `verified` / `verifyError` **不进请求体** —— 它们是服务端 owned 的，
 * 后端 `extra="forbid"` 会直接 400 拒绝。客户端不能自称"已验证可用"。
 */
export async function saveProviderAccount(next: ProviderAccount): Promise<SaveResult> {
  const existing = accounts.find((a) => a.id === next.id)

  const apiKey = next.apiKey.trim()
  const common = {
    label: next.label.trim(),
    model: next.model.trim(),
    kind: next.kind,
    // 去尾斜杠，避免和 SDK 拼出 //v1
    baseUrl: next.baseUrl.trim().replace(/\/+$/, ''),
  }

  try {
    let response: ProviderSaveResponse
    if (existing) {
      response = await api.patch<ProviderSaveResponse>(`/providers/${next.id}`, {
        ...common,
        note: next.note,
        // 只在用户真的输入了新 key 时才带上（见上面那张表）
        ...(apiKey.length > 0 ? { apiKey } : {}),
      })
    } else {
      // ⚠️ `POST /providers` **不接受 `note`**（`ProviderCreate` 里没有这个字段，
      //    而它是 `extra="forbid"`）—— 带上会被 400 拒绝。
      response = await api.post<ProviderSaveResponse>('/providers', { ...common, apiKey })
    }

    upsertAccount(response.provider)

    // `nochange` 要如实传递：用户点了保存但什么都没改，报"已保存"
    // 是制造虚假的动作感（契约 §3.7 就是这么要求的）
    if (response.status === 'nochange') return { status: 'nochange' }
    return { status: 'ok', at: response.at }
  } catch (error) {
    return { status: 'failed', error: messageOf(error) }
  }
}

/**
 * 删除一个 provider 账号 —— `DELETE /providers/{id}`。
 *
 * ## 两种 409 都由后端裁决，前端只负责把话说清楚
 *
 * | 错误码 | 含义 | 用户该做什么 |
 * | --- | --- | --- |
 * | `PROVIDER_IS_DEFAULT` | 它是当前默认 | 先把另一个设为默认 |
 * | `PROVIDER_IN_USE` | 有未结束的扫描在用（响应里会列出是哪几次） | 先终止那些扫描 |
 *
 * ★ 这里**不再自己挑一个新默认**。原来的 mock 实现会这么做，
 *   但后端把"默认不可删"定成了硬规则 —— 否则"当前模型"会在删除的
 *   那一瞬间变成未定义，而那正是总览页状态条要读的东西。
 *   前端猜一套、后端判一套，会出现"界面以为删掉了、其实没删"的错觉，
 *   而这种错觉比一个明确的报错糟糕得多。
 *
 * 后端的 `message` 已经是可直接展示的中文，直接透传即可。
 */
export async function removeProviderAccount(id: string): Promise<SaveResult> {
  try {
    await api.del(`/providers/${id}`)
    accounts = accounts.filter((a) => a.id !== id)
    commit()
    return { status: 'ok', at: new Date().toISOString() }
  } catch (error) {
    return { status: 'failed', error: messageOf(error) }
  }
}

/** 设为默认（列表里的快捷操作）—— `POST /providers/{id}/default` */
export async function setDefaultProvider(id: string): Promise<SaveResult> {
  try {
    const response = await api.post<ProviderSaveResponse>(`/providers/${id}/default`)

    // ★ 默认是**互斥**的：后端靠部分唯一索引 `uq_provider_accounts_default`
    //   保证这一点（两个并发请求同时设默认时，应用层各自都看不到冲突，
    //   唯一索引让第二个事务直接失败，而不是静默留下两个默认）。
    //   本地快照必须跟着同步，否则界面上会短暂出现两个"默认"。
    accounts = accounts.map((a) => ({ ...a, isDefault: a.id === id }))
    commit()

    if (response.status === 'nochange') return { status: 'nochange' }
    return { status: 'ok', at: response.at }
  } catch (error) {
    return { status: 'failed', error: messageOf(error) }
  }
}

/**
 * 验证一份凭据是否可用 —— `POST /providers/{id}/verify`。
 *
 * ## ★ 为什么"验证"必须是一个独立动作，而不是保存时顺手做
 *
 * 两者失败的含义完全不同：
 *   · **保存失败** = 没存进去（网络 / 校验问题）
 *   · **验证失败** = 存进去了，但这个 key 打不通
 *
 * 混成一个动作，用户就不知道"我该重填还是该等一会再试"。
 * 真实产品（各家云厂商控制台）也都是分开的两个按钮。
 *
 * ## ⚠️ 只能验证**已保存**的账号
 *
 * 后端是 `POST /providers/{id}/verify`，它用**服务端已存的那份配置**
 * 发一次最小化请求。所以一个还没保存的草稿**没有 id 可验证** ——
 * 这是真实约束，不是实现偷懒。界面上必须把"验证"限制在已保存的条目上，
 * 否则用户点下去只会拿到 404，而那个报错和"key 不对"完全无从区分。
 *
 * ## 失败也是 200
 *
 * 后端刻意如此（契约 §3.2）：「验证失败」是一个**正常的业务结果**。
 * 所以这里按响应体的 `status` 分流，而不是按 HTTP 状态码。
 *
 * 直接采用响应里回传的 `provider`（验证会写回 `verified`/`verifyError`），
 * 不再单独发一次查询 —— 那会取到"别人的验证结果"这种竞态。
 */
export async function verifyProvider(account: ProviderAccount): Promise<ActionResult> {
  try {
    const result = await api.post<ProviderVerifyResponse>(`/providers/${account.id}/verify`)

    upsertAccount(result.provider)

    return result.status === 'ok'
      ? { status: 'ok', at: result.at }
      : { status: 'failed', error: result.error }
  } catch (error) {
    return { status: 'failed', error: messageOf(error) }
  }
}

// ─────────────────────────────────────────────
// 沙箱
// ─────────────────────────────────────────────

export async function saveSandbox(next: SandboxConfig): Promise<SaveResult> {
  // 客户端校验保留：它挡的是"还没发出去就能看出来"的错误，省一次往返。
  // ⚠️ 服务端仍会独立校验一遍 —— 客户端校验**不是**安全边界，只是体验。
  // 资源下限：给 0 核或 0 内存会让沙箱起不来，那是配置错误而不是"很小"
  if (next.cpuLimit < 1) return { status: 'failed', error: 'CPU 至少 1 核' }
  if (next.memoryLimitMb < 512) {
    return { status: 'failed', error: '内存至少 512 MB —— 更小会导致沙箱无法启动' }
  }
  if (next.timeoutMinutes < 1) return { status: 'failed', error: '超时至少 1 分钟' }

  try {
    /*
      ⚠️ 请求体**恰好**是这 9 个字段，一个不多一个不少。

      后端 `SandboxIn` 是 `extra="forbid"` 的严格模型，多带一个字段就是 422。
      而这里最容易多带的是 `status` / `executionSource` / `updatedAt` ——
      它们都是**服务端产出的只读值**：
        · `status` 是运行时事实（沙箱到底在不在跑），不是用户能设的东西；
        · `executionSource` 由服务端按"DB 有没有覆盖"算出来；
        · `updatedAt` 由服务端写。
      把它们回传上去，等于让调用方有机会声称"我认为沙箱正在运行"。
    */
    const saved = await api.put<SandboxConfig>('/settings/sandbox', {
      image: next.image,
      cpuLimit: next.cpuLimit,
      memoryLimitMb: next.memoryLimitMb,
      timeoutMinutes: next.timeoutMinutes,
      networkIsolated: next.networkIsolated,
      allowVolumeWrite: next.allowVolumeWrite,
      maxConcurrentScans: next.maxConcurrentScans,
      scanMaxTurns: next.scanMaxTurns,
      scanMode: next.scanMode,
      // ★ v0.42：手法块开关。它**必须**在这里 —— `SandboxIn` 是全必填的，
      //   而漏传的后果不是 422（后端会拦），是**用户在设置页改任何别的项
      //   都会把这个开关关掉**（前端拿不到它，payload 里就没有）。
      enableOffensivePrompt: next.enableOffensivePrompt,
    })
    // ★ 用**服务端回传的**对象整体替换，而不是把本地 draft 存下来：
    //   服务端可能做了归一（例如把并发夹到区间内），
    //   而回传值里还带着被重新算过的 executionSource。
    //   只存本地 draft 会让界面显示一个服务端并没有采纳的值。
    sandbox = saved
    commit()
    return { status: 'ok', at: new Date().toISOString() }
  } catch (error) {
    return { status: 'failed', error: messageOf(error) }
  }
}

// ─────────────────────────────────────────────
// 通知（v0.22 接真实接口）
// ─────────────────────────────────────────────
//
// ★ 这四块此前是**内存态**：改完刷新就回到初始值，而界面上挂着
//   "后端接口尚未交付"的提示。后端 v0.22 交付了这五个端点，
//   所以这里必须真的走 API。
//
//   ⚠️ 提示也要跟着改 —— 继续显示"后端未交付"是**反向失真**：
//      它在否认一个已经可靠的功能。与吹嘘没有的能力是同一种失真，
//      只是方向相反（`BACKEND_PENDING` 那段注释里已经写过这条原则，
//      当时是用它把 `sandbox` 摘出去的）。

export async function saveNoticeRule(rule: NoticeRule): Promise<SaveResult> {
  try {
    const saved = await api.put<NoticeRule>('/settings/notices/rule', {
      minSeverity: rule.minSeverity,
      notifyOnScanComplete: rule.notifyOnScanComplete,
      notifyOnCriticalImmediate: rule.notifyOnCriticalImmediate,
    })
    notices = { ...notices, rule: saved }
    commit()
    return { status: 'ok', at: new Date().toISOString() }
  } catch (error) {
    return { status: 'failed', error: messageOf(error) }
  }
}

/**
 * 新建渠道 —— `POST /settings/notices/channels`。
 *
 * ⚠️ **id 由服务端分配**，前端不再自己造 `ch_webhook_01` 这种编号。
 *    让客户端指定主键等于让它决定"覆盖哪一条"。
 */
export async function createNotifyChannel(
  draft: Omit<NotifyChannel, 'id' | 'verified' | 'verifyError'>,
): Promise<{ channel: NotifyChannel } | { error: string }> {
  try {
    const channel = await api.post<NotifyChannel>('/settings/notices/channels', {
      kind: draft.kind,
      label: draft.label,
      target: draft.target.trim(),
      enabled: draft.enabled,
    })
    notices = { ...notices, channels: [...notices.channels, channel] }
    commit()
    return { channel }
  } catch (error) {
    return { error: messageOf(error) }
  }
}

/**
 * 改渠道 —— `PATCH /settings/notices/channels/{id}`。
 *
 * ★ 只提交**改动的那几项**。后端对 `target` 的改动会把 `verified`
 *   打回 `False`（因为"这个地址验证过"的事实不再成立），
 *   所以响应里的 `verified` 可能与我们提交的不同 —— **以响应为准**。
 */
export async function updateNotifyChannel(
  id: string,
  patch: Partial<Pick<NotifyChannel, 'label' | 'target' | 'enabled'>>,
): Promise<{ channel: NotifyChannel } | { error: string }> {
  try {
    const channel = await api.patch<NotifyChannel>(`/settings/notices/channels/${id}`, patch)
    notices = {
      ...notices,
      channels: notices.channels.map((c) => (c.id === id ? channel : c)),
    }
    commit()
    return { channel }
  } catch (error) {
    return { error: messageOf(error) }
  }
}

export async function removeNotifyChannel(id: string): Promise<SaveResult> {
  try {
    // ⚠️ 方法名是 `del` 而不是 `delete` —— `delete` 是 JS 保留字，
    //    写成对象字面量的属性名虽然合法，但读起来像语法错误。
    await api.del(`/settings/notices/channels/${id}`)
    notices = { ...notices, channels: notices.channels.filter((c) => c.id !== id) }
    commit()
    return { status: 'ok', at: new Date().toISOString() }
  } catch (error) {
    return { status: 'failed', error: messageOf(error) }
  }
}

/**
 * 发送测试通知 —— `POST /settings/notices/channels/{id}/test`。
 *
 * ## 这里原来有一段"按地址形态编造结果"的 mock
 *
 * 占位地址报失败、看起来合法的地址报成功，界面上显示
 * 「测试消息已送达 · 刚刚」，而**实际上一个字节都没发出去**。
 *
 * 那比"没有这个功能"严重得多。通知是**安全告警通道**，用户配置时
 * 唯一要问的问题就是"出事的时候我收得到吗" —— 这个按钮存在的全部意义
 * 就是把"通知到底通不通"从**事故时**提前到**配置时**。
 * 给他一个假的「已送达」，等于让他带着一条从未验证过的告警通道上线，
 * 而且他会因为见过那句绿字而**更加确信它没问题**。
 *
 * ## 现在的诚实做法
 *
 * 后端 v0.22 实现了这个端点，但它**只做本地校验、不真的往外发**
 * （真实发送需要 SMTP / Webhook / Slack 三套协议实现）。
 * 所以后端把结论如实写进 `verifyError`，这里**原样透传那句话** ——
 * 包括"地址形状正确，但真实发送尚未接入"这种"没成功但也没错"的结论。
 *
 * ⚠️ **不要把它包装成成功**。返回 `status: 'failed'` 并带上后端那句原话，
 *    用户看到的是"地址没问题，但真实发送还没接" —— 这是一个**准确的**
 *    认知，而不是一个会撒谎的绿勾。
 */
export async function sendTestNotification(channel: NotifyChannel): Promise<ActionResult> {
  try {
    const updated = await api.post<NotifyChannel>(
      `/settings/notices/channels/${channel.id}/test`,
    )
    notices = {
      ...notices,
      channels: notices.channels.map((c) => (c.id === channel.id ? updated : c)),
    }
    commit()

    if (updated.verified) {
      return { status: 'ok', at: new Date().toISOString(), message: '测试通知已送达。' }
    }
    /*
      ⚠️ 走到这里**是当前唯一可能的结果** —— 后端 v0.22 的测试端点
      只做本地校验、不真的往外发（真实发送需要 SMTP / Webhook / Slack
      三套协议实现，是独立的一块工作）。所以 `verified` 恒为 `false`，
      而 `verifyError` 里写着具体原因。

      这不是"失败"，也不该报成功 —— 它是**"动作执行了，但结论是
      还不能确认送达"**。所以如实透传后端那句话，让用户看到
      "地址形状没问题，但真实发送还没接"，而不是一个绿勾。

      ⚠️ 上面那个 `if (updated.verified)` 分支现在走不到，但**不能删** ——
         后端接上真实发送之后它就会走到。留着它，并在注释里说明现状，
         比删掉它、等那时再想起来补要可靠。
    */
    return {
      status: 'failed',
      error: updated.verifyError ?? '测试未通过，但服务端没有给出原因。',
    }
  } catch (error) {
    return { status: 'failed', error: messageOf(error) }
  }
}

// ─────────────────────────────────────────────
// 令牌
// ─────────────────────────────────────────────

export interface CreatedToken {
  token: ApiToken
  /**
   * 完整令牌 —— **只在这一刻返回一次**。
   *
   * ★ 这个字段刻意不进入 store、不进入列表、不进入任何持久结构。
   *   它是"创建"这个动作的**返回值**，不是令牌对象的一部分。
   *   类型上分开（ApiToken 里没有 value），就从结构上保证了
   *   它不可能被列表渲染出来、被截图带上、被写进 localStorage。
   *
   *   真实产品都是这个做法（GitHub / Stripe）：服务端只存哈希，
   *   界面关掉这个对话框就再也看不到完整值。忘记保存只能重新生成 ——
   *   这个"麻烦"是安全性的一部分，不是缺陷。
   */
  plaintext: string
}

/**
 * 创建令牌 —— `POST /settings/tokens`。
 *
 * ★ 完整令牌（`plaintext`）**只在这一刻、这一次响应里存在**。
 *   服务端存的是哈希，之后任何接口都拿不回来。
 *   所以它直接透传给调用方去显示，**不写入 store** ——
 *   类型上 `ApiToken` 没有 `value` 字段，结构上就不可能被列表渲染出来。
 *
 * 失败会真的抛错（不再返回一个"看起来成功"的假令牌）：
 * 限流、校验失败、网络不可达都必须让用户看到，而不是给他一个用不了的令牌。
 */
export async function createToken(input: {
  label: string
  scopes: TokenScope[]
  expiresAt: string | null
}): Promise<CreatedToken> {
  const created = await api.post<CreatedToken>('/settings/tokens', {
    label: input.label,
    scopes: input.scopes,
    // `null` = 永不过期，契约 §3.7 把它定为**显式选项**
    expiresAt: input.expiresAt,
  })

  tokens = [created.token, ...tokens]
  commit()
  return created
}

/**
 * 吊销令牌 —— `DELETE /settings/tokens/{id}`。
 *
 * 后端是**软吊销**（写 `revoked_at`，不删行）——「这个令牌什么时候没的」
 * 在安全复盘时必须能回答。所以本地也要把它从列表移除，
 * 而不是留一条标注为"已吊销"的行：列表的语义是"现在能用的令牌"。
 */
export async function revokeToken(id: string): Promise<SaveResult> {
  try {
    await api.del(`/settings/tokens/${id}`)
    tokens = tokens.filter((t) => t.id !== id)
    commit()
    return { status: 'ok', at: new Date().toISOString() }
  } catch (error) {
    return { status: 'failed', error: messageOf(error) }
  }
}

// ─────────────────────────────────────────────
// 账号密码
// ─────────────────────────────────────────────

/**
 * 修改口令 —— `POST /settings/password`。
 *
 * ## ★ 成功后**所有会话都会被吊销**，包括当前这一个
 *
 * 这是后端刻意做的。契约 §1.2 拒绝 JWT 的理由正是「JWT 无法即时吊销
 * （改密码后旧令牌仍有效）」，所以这个行为必须真的发生 ——
 * 否则当初选不透明令牌就白选了。
 *
 * 因此这里在成功后**主动把本地认证状态推回未登录**。不这么做的话，
 * 界面会停在一个「看起来还登录着、但每个请求都 401」的状态：
 * 用户刚改完密码，紧接着满屏开始报错，而他完全不知道这两件事有关。
 * 主动登出把那个现象变成一句可理解的「请重新登录」。
 *
 * ⚠️ 明文口令只存在于这一跳的请求体里，不写入 store、不碰 localStorage。
 */
export async function changePassword(input: {
  current: string
  next: string
}): Promise<SaveResult> {
  try {
    const result = await api.post<{ status: 'ok'; at: string }>('/settings/password', {
      current: input.current,
      next: input.next,
    })
    // 见上：吊销是后端确定的副作用，不是 UI 的选择
    handleUnauthorized()
    return { status: 'ok', at: result.at }
  } catch (error) {
    return { status: 'failed', error: messageOf(error) }
  }
}

/**
 * 仅供测试与开发：复位。
 *
 * ⚠️ 复位到**空**而不是"造两条假数据" —— 假数据源已经删掉了
 * （见 `accounts` 的说明）。要重新拿到数据，调 `loadSettings()`。
 */
export function resetSettings(): void {
  accounts = []
  commit()
}

// ─────────────────────────────────────────────
// 给新建扫描向导的投影
// ─────────────────────────────────────────────

/**
 * 把设置里的账号投影成**向导需要的形状**（data/providers.ts 的 ProviderConfig）。
 *
 * ★ 这是两处数据"接上"的地方，也是本任务的一个硬要求：
 *   data/providers.ts 顶部写着"配置完成后回到这里，新配置的模型会自动出现"。
 *   在设置页可编辑之前，useProviders() 直接返回常量，那句话是空头承诺。
 *   现在向导读的是这里的投影，改完设置立刻生效。
 *
 * ★ 投影里**不含 apiKey** —— 这不是顺手删的，是刻意的：
 *   向导只需要知道"能不能用"，凭据不该流进它的数据结构。
 *   见 data/settings.ts 里 ProviderAccount 与 ProviderConfig 分工的说明。
 */
export function projectToProviderConfigs(list: ProviderAccount[]): ProviderConfig[] {
  return list.map((account) => {
    /*
      configured 的判定：必须是"验证通过的"。
      只是填了 key 不算 —— 打不通的 key 会让扫描在跑到一半时失败，
      而那比一开始就选不了糟糕得多。
    */
    const configured = account.kind === 'local' ? true : account.verified

    return {
      id: account.id,
      label: account.label,
      model: account.model,
      kind: account.kind,
      configured,
      missing: configured ? null : (account.verifyError ?? '尚未验证这个 API key'),
      note: account.note,
    }
  })
}

/** 向导与状态条共用的 hook —— 读的是设置里那一份账号 */
export function useProjectedProviders(): ProviderConfig[] {
  const { accounts: list } = useSettings()
  return projectToProviderConfigs(list)
}

/** 当前默认 provider —— 总览状态条读它 */
export function defaultAccount(list: ProviderAccount[]): ProviderAccount | null {
  return list.find((a) => a.isDefault) ?? list[0] ?? null
}
