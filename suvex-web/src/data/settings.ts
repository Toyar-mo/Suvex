import type { ProviderKind } from './providers'

/**
 * 设置项的**领域模型**。
 *
 * 每个子域一个类型，不合并成一个大 Settings 对象：
 *   - 它们的保存时机不同（改密码是独立动作，不该和通知开关一起提交）；
 *   - 它们的校验规则不同（API key 有格式与长度要求，通知 Webhook 要看是 URL）；
 *   - 合并之后，任何一处改动都会让整块表单变"脏"，用户分不清什么没保存。
 *
 * 这与项目里「验证程度 / 处置状态是两个轴」是同一个判断：
 * 把不同性质的东西塞进一个字段，省下的复杂度会在别处以 bug 的形式还回来。
 */

// ─────────────────────────────────────────────
// LLM Provider
// ─────────────────────────────────────────────

/**
 * 一个 provider 的**凭据与连接配置**（用户可编辑的部分）。
 *
 * 与 data/providers.ts 的 ProviderConfig 的分工要说清楚，否则很容易合并成一份：
 *   - ProviderConfig（providers.ts）是**给消费者看的投影**：
 *     要不要展示、能不能选、缺什么 —— 新建扫描向导只需要这些；
 *   - ProviderAccount（这里）是**凭据本身**：apiKey、baseUrl、是否默认。
 *
 * 为什么必须分开：`apiKey` 这种东西不该进入向导的数据结构。
 * 向导会把它渲染进 DOM、可能被打印、被截图；而它只需要知道"这个能用"。
 * 分开之后，"密钥流转到什么组件"这件事在类型上就是确定的。
 */
export interface ProviderAccount {
  /** 稳定 id。标识符，不翻译 */
  id: string
  /** 品牌名 —— 保留原文（译成中文反而不可检索） */
  label: string
  /** 模型标识符，与配置文件对齐，不翻译 */
  model: string
  kind: ProviderKind
  /**
   * API key。
   *
   * ⚠️ 空串有确切含义：**未配置**。
   *    它与"配置了但值被遮蔽"是两回事 —— 见 maskSecret 与 SecretField。
   *
   * ★ 接入真实后端后（任务 7）这个字段**恒为空串**：后端
   *   `ProviderOut.from_model` 硬编码它为空，那条路径上没有任何变量
   *   能通向明文。所以"有没有配凭据"要看下面的 `hasKey`，
   *   界面上显示的掩码要看 `apiKeyMask`。三者各管一件事，不能互相替代。
   *
   *   ⚠️ 也正因为它恒为空，**保存时绝不能用它去覆盖服务端的凭据** ——
   *   那会把真 key 静默换成空串。见 saveProviderAccount 的 apiKey 处理。
   */
  apiKey: string
  /**
   * 服务端是否已存有该凭据。
   *
   * ★ 与 `apiKey !== ''` **不是一回事**：接入后端后后者永远是 false
   *   （`apiKey` 恒为空串）。拿它判断"要不要提示用户去补 key"，
   *   会得到一个永远在催用户填一个其实已经填过的字段的界面。
   */
  hasKey: boolean
  /**
   * 服务端给出的**成品掩码**（如 `sk-****abcd`）。
   *
   * ⚠️ 不要再对它跑一遍 `maskSecret`：掩码本身已经是成品，
   *   再过一遍会变成 `••••••••`，用户反而看不出是哪一把 key。
   */
  apiKeyMask: string
  /** 自定义接入点。空串 = 用官方默认地址 */
  baseUrl: string
  /** 是否设为默认 provider。全部账号里至多一个为 true */
  isDefault: boolean
  /**
   * 该 key 是否已在服务端验证通过。
   *
   * 与"填了 key"是两件事：填了但打不通的 key 不能用于扫描。
   * 新建扫描向导的 `configured` 读的就是它。
   */
  verified: boolean
  /** 验证失败的原因。verified 为 false 且尝试过验证时才有值 */
  verifyError: string | null
  /** 这个 provider 的特点取舍说明（不是营销文案） */
  note: string
}

/** 新建 provider 时可选的服务商 */
export interface ProviderPreset {
  id: string
  label: string
  /** 默认模型名 */
  defaultModel: string
  kind: ProviderKind
  /** 官方 base URL。留空表示由 SDK 决定 */
  defaultBaseUrl: string
  note: string
  /** 这个服务商的 key 长什么样 —— 用于格式提示，不做强校验 */
  keyHint: string
}

/**
 * 预设服务商。
 *
 * 为什么给预设而不是让用户手填所有字段：
 *   手填意味着用户要自己去查"DeepSeek 的 base URL 是什么、模型名叫什么"。
 *   预设把这些已知答案直接给出来，用户只需要填 key。
 *   baseUrl 仍可改（自建网关、代理是真实需求），但默认值是对的。
 */
export const PROVIDER_PRESETS: ProviderPreset[] = [
  {
    id: 'deepseek',
    label: 'DeepSeek',
    defaultModel: 'deepseek-chat',
    kind: 'cloud',
    defaultBaseUrl: 'https://api.deepseek.com',
    note: '按量计费，覆盖大多数场景。',
    keyHint: '形如 sk-…',
  },
  {
    id: 'openai',
    label: 'OpenAI',
    defaultModel: 'gpt-4o',
    kind: 'cloud',
    defaultBaseUrl: 'https://api.openai.com/v1',
    note: '推理更稳，成本更高。',
    keyHint: '形如 sk-proj-…',
  },
  {
    id: 'anthropic',
    label: 'Anthropic',
    defaultModel: 'claude-sonnet-4',
    kind: 'cloud',
    defaultBaseUrl: 'https://api.anthropic.com',
    note: '长上下文与代码理解表现好。',
    keyHint: '形如 sk-ant-…',
  },
  {
    id: 'ollama',
    label: '本地模型（Ollama）',
    defaultModel: 'qwen2.5:32b',
    kind: 'local',
    // 本机服务，默认地址是确定的
    defaultBaseUrl: 'http://127.0.0.1:11434',
    note: '数据不出本机，速度取决于硬件。本地模型通常不需要 API key。',
    keyHint: '本地服务通常留空',
  },
]

// ─────────────────────────────────────────────
// 沙箱 / 执行环境
// ─────────────────────────────────────────────

export type SandboxStatus = 'running' | 'stopped' | 'error'

/**
 * 扫描深度。与后端 `core.vocab.ScanMode`、strix 的 `--scan-mode` 逐字一致。
 *
 * ⚠️ 取值是 strix 定义的，**不翻译**（它们是命令行参数）。
 *    中文只出现在标签与说明里。
 */
export type ScanMode = 'quick' | 'standard' | 'deep'

export const SCAN_MODE_LABEL: Record<ScanMode, string> = {
  quick: '快速',
  standard: '标准',
  deep: '深度',
}

export const SCAN_MODE_HINT: Record<ScanMode, string> = {
  quick: '跳过穷举，只打高价值攻击面。最快，也最容易漏。',
  standard: '结构化方法，覆盖与深度平衡。默认档。',
  deep: '尽可能彻底 —— 代价是更长的运行时间和更高的费用。',
}

/**
 * 执行类配置当前值的**来源**。
 *
 * ★ 不标出来会有一个具体的误解：部署时若设了
 *   `SUPEX_MAX_CONCURRENT_SCANS=3`，用户在这里看到 3，会以为是自己以前设的 ——
 *   于是既不知道改回什么，也不知道这个值其实是环境默认。
 *   `default` = 环境变量给的，`user` = 在设置页改过。
 */
export interface ExecutionSource {
  maxConcurrentScans: 'user' | 'default'
  scanMaxTurns: 'user' | 'default'
  scanMode: 'user' | 'default'
}

export interface SandboxConfig {
  /** 沙箱引擎 —— 目前只有 Docker，保留字段以便将来扩展 */
  engine: 'docker'
  status: SandboxStatus
  /** 镜像与版本，标识符不翻译 */
  image: string
  /** 容器内可用 CPU 核数上限 */
  cpuLimit: number
  /** 内存上限（MB） */
  memoryLimitMb: number
  /**
   * 单次扫描的执行时长上限（分钟）。
   *
   * 单位用分钟而不是秒：用户心里想的是"给它半小时"，不是"1800 秒"。
   */
  timeoutMinutes: number
  /**
   * 是否禁止容器访问外网。
   *
   * ⚠️ 默认**允许**，因为扫描本身经常需要出网（打外部目标、拉依赖）。
   *    这个开关的真实用途是"限制爆炸半径"，不是"更安全就一定更好" ——
   *    打开它会让针对外部目标的扫描直接失败。界面上必须说清这个取舍，
   *    否则用户会以为"关掉网络 = 更安全"，然后困惑于扫描为什么跑不动。
   */
  networkIsolated: boolean
  /** 是否允许写入挂载卷。关掉后所有写入都在容器内，随容器销毁 */
  allowVolumeWrite: boolean

  // ── 执行类三件套（任务 13）──────────────────────────
  //
  // 这三项与上面的容器配额回答的是同一类问题："扫描跑起来是什么样"，
  // 所以放在同一个面板、同一次保存里。区别只在于**生效时机**：
  // 并发是秒级生效，另外两项在**下一次发起扫描**时生效。
  /** 同时运行的扫描数上限。超出的排队（不是拒绝） */
  maxConcurrentScans: number
  /** 单个 agent 的回合上限（实际值取它与「扫描深度」天花板中较小者） */
  scanMaxTurns: number
  /** 扫描深度（决定回合预算的天花板：快速 40 / 标准 80 / 深度 150） */
  scanMode: ScanMode
  /** 上面三项当前来自哪里（用户设过 / 环境默认） */
  executionSource: ExecutionSource

  /**
   * 是否给 agent 追加「越权与注入强化手法」那一块（v0.42）。
   *
   * ⚠️ 它**不是**"多几种漏洞类型"，而是把 agent 的**试探强度**调高一档：
   *    打开后解题与渗透扫描两侧的提示词末尾都会追加 `prompts/offensive_playbook.md`
   *    那一份手法矩阵（注入、越权、文件读取、SSRF、竞态的具体换形写法）。
   *
   * ⚠️ 它**不放松任何判定纪律** —— `prompts/ctf2_system.md` 里
   *    「不越界访问」「禁止口令爆破与压力测试」照旧成立。
   *
   * ⚠️ 与上面三项不同，它**没有 env 兜底**，也不在 `executionSource` 里：
   *    那三项是容量类配置（部署方定默认），这是**行为开关**
   *    （不能由部署环境静默决定用户 agent 的强度）。
   */
  enableOffensivePrompt: boolean
}

// ─────────────────────────────────────────────
// 通知
// ─────────────────────────────────────────────

export type NotifyChannelKind = 'email' | 'webhook' | 'slack'

/**
 * 通知渠道。
 *
 * ⚠️ 关键区分：**渠道本身的配置**（发给谁、发到哪个地址）与
 *    **触发条件**（什么级别的问题才发）是两个维度，不是一件事。
 *    把"Slack 地址"和"只通知严重漏洞"放在同一个对象里，会导致
 *    "我想让邮件收到所有级别、Slack 只收严重"这种真实需求无处表达。
 *    所以触发阈值放在 NoticeRule 里，渠道只管"怎么发"。
 */
export interface NotifyChannel {
  id: string
  kind: NotifyChannelKind
  /** 用户可读的渠道名，如"安全组邮件列表" */
  label: string
  /** 目标地址：邮箱 / Webhook URL / Slack Webhook URL */
  target: string
  enabled: boolean
  /** 该渠道是否已验证可用（Webhook 发过一次测试） */
  verified: boolean
  verifyError: string | null
}

/**
 * 通知规则 —— 什么情况下发。
 *
 * 与渠道分离的理由见 NotifyChannel 的说明。
 */
export interface NoticeRule {
  /** 达到或高于该严重度时触发通知 */
  minSeverity: 'critical' | 'high' | 'medium' | 'low' | 'info'
  /** 扫描完成时是否通知（无论有没有发现问题） */
  notifyOnScanComplete: boolean
  /**
   * 是否在发现**新**的严重问题时立即通知，而不是等扫描结束汇总。
   *
   * 这个开关有真实代价：扫描过程中发通知会打断人，
   * 但严重问题早一分钟知道就少一分钟暴露窗口。取舍要写在界面上。
   */
  notifyOnCriticalImmediate: boolean
}

// ─────────────────────────────────────────────
// 账号与安全
// ─────────────────────────────────────────────

export interface ApiToken {
  id: string
  /** 用户给这个 token 起的用途名，如"CI 流水线" */
  label: string
  /**
   * 令牌的**前缀**，用于识别是哪一个。
   *
   * ⚠️ 只存前缀，不存完整令牌 —— 完整令牌只在创建时显示**一次**。
   *    这是所有正经产品的做法（GitHub、Stripe 都如此）：
   *    服务端存哈希，界面上只留前几位供人辨认。
   *    所以这里没有、也永远不该有 `value` 字段。
   */
  prefix: string
  /** 创建时间。ISO 8601 */
  createdAt: string
  /** 最后使用时间。ISO 8601；从未使用为 null */
  lastUsedAt: string | null
  /**
   * 过期时间。null = 永不过期。
   *
   * 允许永不过期是刻意的：强制过期会打断自动化流水线，
   * 而用户往往会用"永久 token + 长期不轮换"来绕过它。
   * 不如让永不过期成为一个**显式选项**，并在界面上标注风险。
   */
  expiresAt: string | null
  /** 该令牌拥有的权限范围 */
  scopes: TokenScope[]
}

export type TokenScope = 'read' | 'scan' | 'admin'

export const TOKEN_SCOPE_LABEL: Record<TokenScope, string> = {
  read: '只读',
  scan: '发起扫描',
  admin: '管理权限',
}

/** 权限范围的风险说明 —— 界面上要能看到自己授出去了什么 */
export const TOKEN_SCOPE_NOTE: Record<TokenScope, string> = {
  read: '查看扫描结果与漏洞详情，不能改动任何数据。',
  scan: '在只读基础上，可以发起与终止扫描。',
  admin: '可以修改设置、管理其他令牌。仅授予完全可信的集成。',
}

// ─────────────────────────────────────────────
// 校验
// ─────────────────────────────────────────────

/**
 * 校验结果的统一形状。
 *
 * 用 `{ ok, error }` 而不是抛异常或返回字符串：
 *   - 组件要能同时拿到"行不行"和"为什么不行"；
 *   - 布尔 + 消息两个字段分开，比"空串代表通过"更不容易用错
 *     （空串在 JS 里是假值，`if (error)` 恰好能用，但 `error === ''`
 *     和 `error === undefined` 的区别会在重构时咬人）。
 */
export type Validation = { ok: true; error: null } | { ok: false; error: string }

const OK: Validation = { ok: true, error: null }
function fail(error: string): Validation {
  return { ok: false, error }
}

/**
 * API key 校验。
 *
 * ★ 刻意**不做格式强校验**。
 *
 *   这是被"正确性"逼出来的决定：各家的 key 前缀并不稳定 ——
 *   OpenAI 有过 sk- / sk-proj-，自建网关和代理可以发任意形式的 key，
 *   本地模型通常根本不需要 key。
 *   一旦按前缀拦，用户拿着一个**完全能用**的 key 被挡在门外，
 *   而他没有任何办法绕过 —— 那是比"少校验一次"严重得多的失败。
 *
 *   所以这里只拦两类确定错误：
 *     1. 空（但不是必填报错，必填性由调用方按 provider 决定）；
 *     2. 明显是误粘贴的（含空白字符、换行、或长得不像 key）。
 *   剩下的交给真实接口去验证 —— 那才是唯一权威的判断。
 */
export function validateApiKey(value: string, kind: ProviderKind): Validation {
  const trimmed = value.trim()

  // 本地模型通常不需要 key，空值合法
  if (trimmed.length === 0) {
    return kind === 'local' ? OK : fail('请填写 API key')
  }

  // 粘贴时常带上的隐形内容 —— 这是最常见的一类"key 明明对却认证失败"
  if (/\s/.test(trimmed)) {
    return fail('key 中含有空格或换行，可能是粘贴时带入了多余内容')
  }

  if (trimmed.length < 8) {
    return fail('长度过短，请确认没有截断')
  }

  // 上界防误粘贴整段文本（比如把 README 复制进来了）
  if (trimmed.length > 512) {
    return fail('长度异常，请确认粘贴的是 key 而不是整段文档')
  }

  return OK
}

/**
 * 用 http 时是否算"已确认风险"的判据。
 *
 * 抽成函数是为了让**提示与校验共用同一个判断** —— 两处各写一遍
 * 必然漂移，而漂移的表现是"提示说不安全、但校验已经放行了"。
 */
export function isInsecureHttp(value: string): boolean {
  try {
    const url = new URL(value.trim())
    if (url.protocol !== 'http:') return false
    const host = url.hostname
    return !(host === 'localhost' || host === '127.0.0.1' || host === '::1')
  } catch {
    return false
  }
}

/**
 * base URL 校验。
 *
 * 只在**填了**的时候校验 —— 留空表示用官方默认地址，是合法状态。
 * 这条"可空"语义必须与 placeholder 上的文案一致，否则用户会以为必填。
 *
 * ## `acknowledgedHttp` 是什么（v0.12）
 *
 * 非回环的 http 地址在云端 provider 上是真实风险：API key 会明文过网。
 * 但原来**直接判错**，把一类真实且正当的场景挡在门外 ——
 * 内网自建网关（`http://10.39.16.9/v1`）：它在私网里、可能由运维统一提供、
 * 未必配证书。被拦的人只能把 `kind` 改成 `local` 绕过，
 * 而那是**用错误的标签换通行** —— 比让他在知情后放行更糟。
 *
 * 所以改成**显式确认**：`acknowledgedHttp = true` 时放行。
 * 这与扫描授权确认是同一个套路 —— 不替用户做决定，
 * 但要他明确知道自己在做什么。
 *
 * ⚠️ 默认 `false`：确认必须是**主动动作**。默认放行等于没有这道提示。
 */
export function validateBaseUrl(value: string, acknowledgedHttp = false): Validation {
  const trimmed = value.trim()
  if (trimmed.length === 0) return OK

  let url: URL
  try {
    url = new URL(trimmed)
  } catch {
    return fail('不是合法的 URL，需要以 http:// 或 https:// 开头')
  }

  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    return fail('只支持 http 或 https')
  }

  /*
    回环地址（localhost / 127.0.0.1 / ::1）上的 http 不算风险：
    流量根本没离开本机，不存在"过网被看"的问题。
    这是唯一**不需要确认**就放行的 http 情形。
  */
  if (url.protocol === 'http:' && !isInsecureHttp(trimmed) ) {
    return OK
  }

  if (isInsecureHttp(trimmed) && !acknowledgedHttp) {
    return fail('该地址使用 http，API key 会以明文经过网络 —— 确认风险后可继续')
  }

  return OK
}

/** 模型名校验。非空即可 —— 模型名是自由文本，不预设枚举 */
export function validateModel(value: string): Validation {
  const trimmed = value.trim()
  if (trimmed.length === 0) return fail('请填写模型名称')
  if (/\s/.test(trimmed)) return fail('模型名中不应包含空格')
  return OK
}

/** provider 显示名 */
export function validateProviderLabel(value: string): Validation {
  const trimmed = value.trim()
  if (trimmed.length === 0) return fail('请填写名称')
  if (trimmed.length > 40) return fail('名称过长（最多 40 个字符）')
  return OK
}

/**
 * 邮箱。用宽松匹配而不是完整 RFC 5322 正则 —— 后者长得没人能维护，
 * 而且会拒绝一些合法地址。真正的验证手段是发一封信，不是正则。
 */
export function validateEmail(value: string): Validation {
  const trimmed = value.trim()
  if (trimmed.length === 0) return fail('请填写收件邮箱')
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed)) return fail('邮箱格式不正确')
  return OK
}

/** Webhook / Slack 地址 */
export function validateWebhookUrl(value: string): Validation {
  const trimmed = value.trim()
  if (trimmed.length === 0) return fail('请填写 Webhook 地址')

  let url: URL
  try {
    url = new URL(trimmed)
  } catch {
    return fail('不是合法的 URL')
  }

  if (url.protocol !== 'https:') {
    /*
      Webhook 与 base URL 不同，这里**强制 https**。
      原因：Webhook 载荷里带着漏洞标题、目标地址、有时还有 PoC 摘要 ——
      用 http 发等于把客户资产清单挂在明文信道上。
      而不像 base URL 那样有"内网 http"的正当场景（内部 webhook 也用 https 是常态）。
    */
    return fail('Webhook 必须使用 https —— 通知内容含漏洞与目标信息')
  }

  return OK
}

/**
 * 密码强度。
 *
 * 给**分档**而不是只给通过/不通过：
 * 密码强度是连续量，只回一句"不达标"用户不知道该补什么。
 * 分档同时用于界面上的强度条，让用户看到自己在哪里。
 */
export type PasswordStrength = 'weak' | 'fair' | 'strong'

export interface PasswordCheck {
  strength: PasswordStrength
  /** 逐条要求的达成情况 —— 界面直接渲染它，不在组件里重写一遍规则 */
  requirements: { label: string; met: boolean }[]
  /** 能否提交。弱密码不允许提交（这是账号安全，不是可以商量的偏好） */
  acceptable: boolean
}

export function checkPassword(password: string): PasswordCheck {
  const requirements = [
    { label: '至少 12 个字符', met: password.length >= 12 },
    { label: '包含大写与小写字母', met: /[a-z]/.test(password) && /[A-Z]/.test(password) },
    { label: '包含数字', met: /\d/.test(password) },
    // 符号不是硬要求 —— 长密码短语同样是强密码，强制符号反而促使用户写 "Password1!"
    { label: '包含符号（可选）', met: /[^A-Za-z0-9]/.test(password) },
  ]

  const hardMet = requirements.slice(0, 3).every((r) => r.met)

  let strength: PasswordStrength = 'weak'
  if (hardMet && requirements[3].met) strength = 'strong'
  else if (hardMet) strength = 'fair'

  return {
    strength,
    requirements,
    // 只有三条硬要求全满足才能提交
    acceptable: hardMet && password.length > 0,
  }
}

/** 修改密码的整体校验 */
export function validatePasswordChange(current: string, next: string, confirm: string): Validation {
  if (current.length === 0) return fail('请输入当前密码')
  if (next.length === 0) return fail('请输入新密码')

  const check = checkPassword(next)
  if (!check.acceptable) return fail('新密码未满足要求')

  if (next === current) return fail('新密码不能与当前密码相同')
  if (confirm !== next) return fail('两次输入的新密码不一致')

  return OK
}

/**
 * 令牌有效期选项。
 *
 * 提供"永不过期"是刻意的，理由见 ApiToken.expiresAt：
 * 强制过期会被绕过（用户写死一个永久 token 然后忘记它），
 * 不如让它成为一个显式选项，并在界面上标注风险。
 */
export type TokenExpiry = '30d' | '90d' | '1y' | 'never'

export const TOKEN_EXPIRY_LABEL: Record<TokenExpiry, string> = {
  '30d': '30 天',
  '90d': '90 天',
  '1y': '1 年',
  never: '永不过期',
}

/** 把有效期选项换算成 ISO 时间。/ never 返回 null */
export function expiryToIso(expiry: TokenExpiry, now: number): string | null {
  if (expiry === 'never') return null
  const days = expiry === '30d' ? 30 : expiry === '90d' ? 90 : 365
  return new Date(now + days * 86_400_000).toISOString()
}
