import { ApiError, api, toApiError } from '@/lib/api'
import { useSyncExternalStore } from 'react'

/**
 * 工具管理页的领域模型 + API 封装 + store。
 *
 * ★ 三者放在一个文件里，与 `data/kernel.ts` 同一个理由：这些类型只有
 *   本模块与工具页用，没有第三方消费者。拆成两个文件只会让"加一个字段
 *   要改两处"。（长到 800 行左右再按 `data/kernel.ts` 顶部那条判据拆。）
 *
 * ## 这一页管的是**两类完全不同**的东西
 *
 * | | MCP 连接 | 内置工具 |
 * | --- | --- | --- |
 * | 谁定义的 | 部署者在界面上添加 | 代码里固定 13 个 |
 * | 可增删吗 | 能 | 不能，只能开关 |
 * | 有凭据吗 | 有（`env`） | 没有 |
 * | 需要试连吗 | 需要（真 exec 一次） | 不需要 |
 *
 * 把它们合成一个"工具"概念会引出 `if kind === 'mcp'` 满天飞的界面，
 * 所以这一页分成两栏，store 也分成两半（`mcp*` / `tool*` 前缀）。
 *
 * ## ⚠️ `enabled` 与 `ready` 是**两个独立状态**，不要合成一个
 *
 * 后端特意把它们分开返回，因为用户该做的事不同：
 *   · `enabled=false`            → 你去开关里打开它
 *   · `enabled=true, ready=false` → 它没配好，去 MCP 连接里配那条连接
 *
 * 界面上合成一个灰色"不可用"，用户就不知道该去哪修 —— 而"知道去哪修"
 * 正是这个页面存在的理由。
 */

// ─────────────────────────────────────────────
// 类型（与后端 `app/kernel/schemas.py` 逐字对应）
// ─────────────────────────────────────────────
//
// ⚠️ 这里是**手写的 camelCase**，不是自动转换的。后端 `CamelModel` 只配了
//    `validation_alias`（入方向），不配 `serialization_alias` —— 所以后端
//    字段名写成 snake_case 时 JSON 里就是 snake_case。v0.81 那三个 MCP
//    出入参正是这样，本模块按**修正后**的 camelCase 写（v0.82 一并修了）。

/** 一条已保存的 MCP 连接。**没有 `env`** —— 只有 `hasEnv`。 */
export interface McpServer {
  name: string
  command: string
  args: string[]
  notes: string
  /** 白名单：只允许调这些工具名（`null` = 全放）。 */
  allowedTools: string[] | null
  /** 是否配了环境变量。只说"有没有"，不说是**什么**。 */
  hasEnv: boolean
  /**
   * ★ v0.83：单次工具调用超时（秒）。`null` = 用内核默认 60 秒。
   *
   * 之所以要在界面上管它：慢工具（网段扫描约 85 秒、批量模板处理 100 秒以上）
   * 在默认 60 秒下**必然超时**，而报错长得像"MCP 连接坏了" ——
   * 模型会去改配置，而不是意识到"这个操作本来就慢"。
   */
  callTimeout: number | null
}

/** `GET /kernel/mcp-servers` 的响应。 */
export interface McpServersSnapshot {
  servers: McpServer[]
}

/** 提交一条 MCP 连接时的载荷。**可以带 `env`**（后端只进子进程，不回显）。 */
export interface McpServerDraft {
  name: string
  command: string
  args: string[]
  env: Record<string, string>
  notes: string
  allowedTools: string[] | null
  /**
   * ★ 保留已保存的 `env`，忽略本次提交的 `env`。
   *
   * ## 为什么需要它（否则编辑一次就会丢凭据）
   *
   * `env` 是**只进不出**的 —— 后端 `McpServerOut` 里根本没有这个字段，
   * 只有 `hasEnv`。所以前端打开"编辑"时拿不到原值，输入框只能留空。
   * 而 `PUT /kernel/mcp-servers` 是**全量替换**：把空 `env` 提交上去，
   * 后端就真的把凭据清掉了。
   *
   * 那个失效形状最难查：用户只改了一下备注，保存成功、界面正常，
   * 而凭据已经没了 —— 要等到下一次真的调用那条连接时才暴露，
   * 届时没人会想到是几天前那次编辑。
   *
   * 所以编辑已有连接时**必须**带上它；新增一条时不带。
   */
  preserveEnv: boolean
  /** ★ v0.83：单次调用超时（秒）。`null` = 用内核默认 60 秒。 */
  callTimeout: number | null
}

/** `POST /kernel/mcp-servers/test` 的结果。 */
export interface McpTestResult {
  ok: boolean
  reason: string
  tools: string[]
  serverName: string
  serverVersion: string
  elapsedMs: number
}

/** 一个内置工具。 */
export interface KernelTool {
  /** 标识符 —— 模型在 `ACTION: {"tool": "<name>"}` 里写的就是它。不翻译。 */
  name: string
  label: string
  group: string
  summary: string
  params: string
  /** 当前是否启用（= 不在 `disabled_tools` 里）。 */
  enabled: boolean
  /** 关掉它会明显废掉主力能力。 */
  core: boolean
  /** 依赖的 MCP 连接名（空串 = 无依赖）。 */
  requiresConnection: string
  /** 那条依赖是否**真的就绪**（与 `enabled` 是两个独立状态）。 */
  ready: boolean
  /** 未就绪的原因（中文）。 */
  reason: string
}

/** `GET /kernel/tools` 的响应。 */
export interface KernelToolsSnapshot {
  tools: KernelTool[]
  /** 分组顺序 —— 直接用它排栏，不从 tools 二次推导。 */
  groups: string[]
  /** 被显式关掉的工具名。 */
  disabled: string[]
}

// ─────────────────────────────────────────────
// store
// ─────────────────────────────────────────────

type Listener = () => void

const listeners = new Set<Listener>()
function emit() {
  for (const listener of listeners) listener()
}

export type ToolsStatus = 'loading' | 'ready' | 'error'

export interface ToolsState {
  mcp: McpServersSnapshot
  catalog: KernelToolsSnapshot
  status: ToolsStatus
  error: ApiError | null
}

const EMPTY_MCP: McpServersSnapshot = { servers: [] }
const EMPTY_CATALOG: KernelToolsSnapshot = {
  tools: [],
  groups: [],
  disabled: [],
}

let mcp: McpServersSnapshot = EMPTY_MCP
let catalog: KernelToolsSnapshot = EMPTY_CATALOG
let status: ToolsStatus = 'loading'
let loadError: ApiError | null = null

/**
 * 「首次加载是否已经发起过」。
 *
 * ★ 必须与 `status` 分开：`status` 初值就是 `'loading'`，拿它判断
 *   "是不是正在加载"会把**第一次**调用也挡掉，于是永远没人加载。
 *   这个坑 `settings-store.ts` 里已经记过一次，这里照同一条办。
 */
let loadStarted = false

/**
 * 快照必须**引用稳定** —— `useSyncExternalStore` 每次渲染都比较它。
 * 所以所有变更走"整体替换"，且只在真的变更时替换。
 */
let snapshot: ToolsState = { mcp, catalog, status, error: loadError }

function commit() {
  snapshot = { mcp, catalog, status, error: loadError }
  emit()
}

function getSnapshot(): ToolsState {
  return snapshot
}

const SERVER_SNAPSHOT: ToolsState = {
  mcp: EMPTY_MCP,
  catalog: EMPTY_CATALOG,
  status: 'loading',
  error: null,
}

function getServerSnapshot(): ToolsState {
  return SERVER_SNAPSHOT
}

export function subscribeTools(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** 订阅工具页状态。 */
export function useTools(): ToolsState {
  return useSyncExternalStore(subscribeTools, getSnapshot, getServerSnapshot)
}

function messageOf(error: unknown): ApiError {
  return error instanceof ApiError ? error : toApiError(error)
}

// ─────────────────────────────────────────────
// 读取
// ─────────────────────────────────────────────

/**
 * 拉一次完整状态（两个端点并行）。
 *
 * ## 为什么两个端点并行而不是串行
 *
 * 它们互不依赖，串行会让这一页的可用时间翻倍。而失败处理必须**分开**：
 * 一个端点失败不该让另一个的数据也丢掉 —— 见下面 `Promise.allSettled`
 * 那段说明。
 */
export async function loadTools(): Promise<void> {
  loadStarted = true
  status = 'loading'
  loadError = null
  commit()

  const [mcpResult, catalogResult] = await Promise.allSettled([
    api.get<McpServersSnapshot>('/kernel/mcp-servers'),
    api.get<KernelToolsSnapshot>('/kernel/tools'),
  ])

  /*
    ★ 用 `allSettled` 而不是 `all`，且**部分成功也保留数据**。

    理由：这两块回答的是不同问题（"外部接进来什么" vs "内置的开了哪些"）。
    用 `all` 的话，MCP 端点 500 会让工具清单也变成空白 —— 而用户
    只是想看工具清单，却得到一个"整页加载失败"。部分成功时把拿到的
    显示出来，比整页白屏有用。

    只有**两个都失败**才算整页失败。
  */
  const errors: ApiError[] = []

  if (mcpResult.status === 'fulfilled') {
    mcp = mcpResult.value
  } else {
    errors.push(messageOf(mcpResult.reason))
  }

  if (catalogResult.status === 'fulfilled') {
    catalog = catalogResult.value
  } else {
    errors.push(messageOf(catalogResult.reason))
  }

  if (errors.length === 2) {
    status = 'error'
    loadError = errors[0]
  } else {
    status = 'ready'
    loadError = errors[0] ?? null
  }
  commit()
}

/** 页面入口调用它 —— 同一进程里只发起一次。 */
export function ensureToolsLoaded(): void {
  if (loadStarted) return
  void loadTools()
}

// ─────────────────────────────────────────────
// MCP 连接：写
// ─────────────────────────────────────────────

/**
 * 保存整份 MCP 连接清单（**全量替换**）。
 *
 * ⚠️ 全量语义：传上来的就是最终状态。删除一条 = 清单里没有它。
 * 后端也是整份写回（`PUT /kernel/mcp-servers`），不做增量合并 ——
 * 增量会引入"顺序"与"没传 = 保持还是删"两类歧义。
 *
 * 成功返回新快照；失败抛出 `ApiError` 让调用方渲染具体原因
 * （重名、缺 command 都是后端逐条校验过才拒的，原因可直接展示）。
 */
export async function saveMcpServers(servers: McpServerDraft[]): Promise<McpServersSnapshot> {
  const saved = await api.put<McpServersSnapshot>('/kernel/mcp-servers', { servers })
  mcp = saved
  // 连接变了 → 依赖那些连接的工具的就绪状态跟着变，工具清单必须一起刷新。
  //
  // ★ 这里**不能**只更新本地字段：`catalog` 里每个工具的 `ready`
  //   是后端算的（见 `service.tools_catalog`），本地猜会与运行期不一致。
  //   所以拉一次。失败不回滚已保存的连接（它确实存进去了），
  //   只是清单停在旧的就绪状态 —— 下一次进页面会纠正。
  try {
    catalog = await api.get<KernelToolsSnapshot>('/kernel/tools')
  } catch (error) {
    // 拉不到就留着旧的；不把它算成保存失败（保存本身成功了）。
    console.warn('保存 MCP 连接成功，但刷新工具清单失败', error)
  }
  commit()
  return saved
}

/** 试连一条**尚未保存**的配置。失败也是一条正常结果（`ok=false` + 原因）。 */
export async function testMcpServer(draft: McpServerDraft): Promise<McpTestResult> {
  return api.post<McpTestResult>('/kernel/mcp-servers/test', { server: draft })
}

// ─────────────────────────────────────────────
// 内置工具：写
// ─────────────────────────────────────────────

/**
 * 保存"关掉哪些工具"（**全量替换**，空数组 = 全开）。
 *
 * ★ 提交的是 `disabled` 而不是完整的 tools 清单：后端按名字比对，
 *   前端不需要知道全部 13 个名字就能表达"我只要关这一个"，
 *   也避免了"界面漏显示一个工具 → 保存时把它一并关掉"这种事故。
 */
export async function saveDisabledTools(disabled: string[]): Promise<KernelToolsSnapshot> {
  const saved = await api.put<KernelToolsSnapshot>('/kernel/tools', { disabled })
  catalog = saved
  commit()
  return saved
}

// ─────────────────────────────────────────────
// 纯函数（可单测，不碰网络）
// ─────────────────────────────────────────────

/**
 * 把工具按 `groups` 分组，**顺序严格跟 `groups` 走**。
 *
 * ★ 为什么不用 `tools.reduce` 边遍历边建 Map：那样分组顺序取决于
 *   工具出现的先后，而后端明确给了 `groups` 这个顺序（`TOOL_GROUPS`）。
 *   两处各定一次顺序，界面与提示词的排列就会漂移。
 *
 * ⚠️ 空分组**保留**（返回空数组），因为"这一栏没有工具"与"没有这一栏"
 *   在界面上是两回事 —— 前者说明清单配置有问题，后者是正常布局。
 *   保留它才有机会把问题显示出来。
 */
export function groupTools(
  tools: KernelTool[],
  groups: string[],
): { group: string; tools: KernelTool[] }[] {
  return groups.map((group) => ({
    group,
    tools: tools.filter((tool) => tool.group === group),
  }))
}

/**
 * 一个工具在界面上的**综合状态**。
 *
 * ★ 四个状态而不是两个（可用/不可用），因为每种状态对应的**动作**不同：
 *
 * | 状态 | 含义 | 用户该做什么 |
 * | --- | --- | --- |
 * | `ready` | 开着且依赖就绪 | 没事 |
 * | `disabled` | 被关掉了 | 打开开关 |
 * | `unconfigured` | 开着但依赖没配 | 去配那条 MCP 连接 |
 * | `misconfigured` | 开着、依赖配了但不可用（如白名单挡住） | 去看连接详情 |
 *
 * 把后两者合成一个"不可用"，用户就不知道该去哪修。
 */
export type ToolHealth = 'ready' | 'disabled' | 'unconfigured' | 'misconfigured'

export function toolHealth(tool: KernelTool): ToolHealth {
  if (!tool.enabled) return 'disabled'
  if (tool.ready) return 'ready'
  // 有依赖声明但没配 → 用户得去添加那条连接；
  // 没依赖声明却不 ready → 配置错位（理论上不该发生），归到 misconfigured。
  return tool.requiresConnection ? 'unconfigured' : 'misconfigured'
}

/** 给每个工具算一次状态（列表渲染用，避免每行各调一次）。 */
export function healthOf(tools: KernelTool[]): Record<string, ToolHealth> {
  const out: Record<string, ToolHealth> = {}
  for (const tool of tools) out[tool.name] = toolHealth(tool)
  return out
}

/**
 * "关掉它会不会明显废掉主力能力" —— 用于给确认对话框准备措辞。
 *
 * ★ 单独提出来是因为它决定**要不要二次确认**：关掉 `core` 工具是一个
 *   会明显影响后续所有任务的改动，随手一点就生效不合适；
 *   而关掉 `list_requests` 只是个偏好，弹窗反而碍事。
 */
export function disabledCoreTools(
  tools: KernelTool[],
  nextDisabled: string[],
): KernelTool[] {
  const set = new Set(nextDisabled)
  return tools.filter((tool) => tool.core && set.has(tool.name))
}

/** 校验一条草稿的必填项（前端先挡一遍，给出**就地**的错误而不是等后端 400）。 */
export function validateDraft(draft: McpServerDraft): string | null {
  if (!draft.name.trim()) return '连接名不能为空 —— 模型用它在 `call_mcp` 里指认这条连接。'
  if (!/^[A-Za-z0-9_.-]+$/.test(draft.name.trim())) {
    return '连接名只能含字母、数字、下划线、点、连字符。'
  }
  if (!draft.command.trim()) {
    return '要填 command —— 它是拉起这个 server 的可执行文件（如 npx / python3 / 绝对路径）。'
  }
  return null
}

/** 解析参数字符串（界面用空格分隔输入）→ 数组。空串得到空数组。 */
export function parseArgs(raw: string): string[] {
  return raw
    .split(/\s+/)
    .map((part) => part.trim())
    .filter(Boolean)
}

/** 解析白名单输入（逗号分隔）→ 数组；空 = `null`（= 全放）。 */
export function parseAllowedTools(raw: string): string[] | null {
  const items = raw
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)
  return items.length > 0 ? items : null
}

/** 解析 `env` 输入（每行 `KEY=VALUE`）→ 对象。坏行会被忽略。 */
export function parseEnv(raw: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of raw.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed || trimmed.startsWith('#')) continue
    const at = trimmed.indexOf('=')
    if (at <= 0) continue
    const key = trimmed.slice(0, at).trim()
    const value = trimmed.slice(at + 1).trim()
    if (key) out[key] = value
  }
  return out
}

/**
 * 解析超时输入（秒）→ `number | null`。
 *
 * ★ 三个语义必须分清，混一个就会配出错的连接：
 *
 * | 输入 | 结果 | 含义 |
 * | --- | --- | --- |
 * | 空串 / 全空格 | `null` | **不配置**，用内核默认 60 秒 |
 * | `"300"` | `300` | 这条连接的单次调用超时 |
 * | `"abc"` / `"0"` / `"-5"` | `null` | 非法 → 按不配置处理 |
 *
 * ⚠️ 非法值**不能**返回 `NaN`：`NaN` 会被 `JSON.stringify` 变成
 * `null`（碰巧对），但也会让 `Number.isFinite` 之类的判断在别处失效；
 * 更糟的是如果某处把它当数字比较，`NaN > 0` 恒为 false，行为会静默走偏。
 *
 * ⚠️ 也不能返回 `0`：后端 `Field(gt=0)` 会 422，而用户只是清空了输入框，
 * 看到"必须大于 0"会一头雾水。
 */
export function parseTimeout(raw: string): number | null {
  const trimmed = raw.trim()
  if (!trimmed) return null
  const value = Number(trimmed)
  if (!Number.isFinite(value) || value <= 0) return null
  // 与后端 `Field(le=1800)` 对齐：超上限的输入按"不配置"处理，
  // 而不是提交上去换一个 422。
  if (value > 1800) return null
  return value
}
