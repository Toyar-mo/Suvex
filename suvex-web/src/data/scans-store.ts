import { ApiError, api, toApiError } from '@/lib/api'
import { useSyncExternalStore } from 'react'
import type { Scan, ScanGoal } from './scan'

/**
 * 扫描数据的**单一运行时事实来源**。
 *
 * 与 findings-store / settings-store 同一套模式
 * （useSyncExternalStore + 整体替换快照）。为什么需要它：
 * Scans 页新建一次扫描后，Overview 的"进行中的扫描"数、Activity 页的
 * 扫描上下文都该跟着变。各自 useState 一份快照就会漂移。
 *
 * ## 接入真实后端后（任务 7）这一层多了两件事
 *
 * 1. **加载态**。数据从网络来，所以"空"和"还没到"必须分开 ——
 *    混在一起的话，进页面会先显示"还没有扫描记录"，几十毫秒后再跳出
 *    一堆记录，用户看到的是闪一下的假空状态。
 * 2. **轮询**。契约 §7 明确要求：有扫描处于 `queued`/`running` 时按
 *    2–3 秒轮询 `GET /scans`，**全部到终态就停** —— 一直轮询是白烧请求，
 *    而只在挂载时拉一次会让"运行中"永远不变。
 */

type Listener = () => void

/**
 * 加载状态。
 *
 * `idle` 是任务 9 加的：它表示"**还没人问过这个视图**"，与 `loading`
 * （正在问）是两件事。归档/回收站两个 tab 在打开之前是 `idle` ——
 * 混成 `loading` 会让没打开过的 tab 永远显示"加载中"。
 */
export type ScansStatus = 'idle' | 'loading' | 'ready' | 'error'

/**
 * 三个视图。与后端的 `?archived=` / `?deleted=` 一一对应。
 *
 * `default` 不是"全部" —— 它是**默认视图**：既不含归档、也不含回收站。
 * 名字叫 `all` 会误导（后端默认就是排除这两类的）。
 */
export type ScanView = 'default' | 'archived' | 'deleted'

export interface ScansSnapshot {
  scans: Scan[]
  status: ScansStatus
  /** ★ 真的 `ApiError`，不是一句文案 —— `ErrorState` 要靠它区分失败种类 */
  error: ApiError | null
  /**
   * ★ 这个槽的数据**可能已经过期**，下次进入该视图时要重新拉。
   *
   * 存在的理由：批量操作是**跨越视图**的。在默认视图里归档一条，它离开
   * 默认槽（乐观移除，没问题）同时**进入**归档槽 —— 而归档槽并不知道
   * 这件事，它还是上次拉下来的那份。如果什么都不做，用户切到「已归档」
   * 会发现刚归档的那条不在里面，切回来又在（默认槽是乐观更新的）。
   *
   * 两种修法里选了"标记 + 下次进入时拉"而不是"当场把对侧槽也拉一遍"：
   * 用户可能根本不打开那个 tab，当场拉就是白烧请求；而"标记"把成本
   * 推迟到真的要看的时候。
   *
   * 这不是缓存失效策略的花哨用法 —— 它只是承认**一次操作会同时改变
   * 两个视图**，而 `leavesView` 只描述了其中一半。
   */
  stale: boolean
}

const listeners = new Set<Listener>()

/**
 * ## ★ 三个视图各占一个槽（任务 9）
 *
 * | 槽 | 谁在用 | 取什么 |
 * | --- | --- | --- |
 * | `default` | 侧栏"运行中 N"、总览、Findings/Reports 的"扫过没有" | `GET /scans` |
 * | `archived` | 只有扫描列表页的「已归档」tab | `GET /scans?archived=true` |
 * | `deleted` | 只有扫描列表页的「回收站」tab | `GET /scans?deleted=true` |
 *
 * ### 为什么必须分槽，不能合成一个数组
 *
 * 侧栏与总览读的是这个 store。如果页面切到「已归档」时把归档记录塞进
 * 同一个数组，那么：
 *
 * * 侧栏的"运行中 N"会把归档的也算进去；
 * * 总览的"有没有扫过"会被归档记录满足 —— 而那句话的意思是
 *   "**当前**有没有记录"。
 *
 * 两个数字都会当场撒谎，而它们恰恰是用户判断"现在忙不忙"的唯一依据。
 *
 * ### 为什么是这里的模块级状态而不是页面里的 useState
 *
 * 三个槽的**加载与失效规则是一样的**（同一个端点、同一套 silent/骨架/错误），
 * 拆到页面里就要把 `loadScans` 那段抄三遍 —— 而抄三遍的东西必然漂移。
 *
 * ★ **初始为空** —— 原来是 `MOCK_SCANS`。假数据在这里的危害不只是"占位"：
 *   它会让 Overview 的"进行中的扫描"显示一个假的数字。
 */
function initialSlot(status: ScansStatus): ScansSnapshot {
  return { scans: [], status, error: null, stale: false }
}

let slots: Record<ScanView, ScansSnapshot> = {
  // 默认视图初始 `loading`：页面一进来就会拉它，先显示骨架是对的。
  default: initialSlot('loading'),
  /*
    归档与回收站初始 **`idle`** —— 不是 `loading`。

    它们没人进那两个 tab 之前不该显示骨架，也不该在进页面时白拉两次请求。
    `idle` 与"加载中"是两件事：前者是"还没人问过"，后者是"正在问"。
    页面把两者**都渲染成骨架**（因为 `idle` 之后立刻就会变 loading），
    但 store 这一层必须分得清 —— 混在一起会让"从没打开过的 tab"
    永远显示加载中。
  */
  archived: initialSlot('idle'),
  deleted: initialSlot('idle'),
}

/**
 * 「首次加载是否已经发起过」—— 给 `ensureScansLoaded()` 用。
 *
 * 不能用 `dataStatus` 判断：它初值就是 `'loading'`，
 * 拿它当"正在加载"会把第一次调用也挡掉，于是永远没人加载。
 *
 * ⚠️ 它只挡得住 `ensureScansLoaded` **自己**（而且只在 default 视图上）。
 *    `Layout` 与页面各自的加载是两条独立的路径，两者在同一个 tick 里
 *    都会走到 `loadScans` —— 所以真正的去重放在 `loadScans` 里，
 *    见下面 `inFlight` 的说明。两道防线各管一件事：
 *      · `loadStarted` 管"这一次会话还要不要主动拉"（跨时间）；
 *      · `inFlight` 管"同一瞬间有没有人正在拉"（同一时刻）。
 */
let loadStarted = false

/**
 * ★ 正在飞行中的请求，**按视图分别记**（2026-10 修）。
 *
 * ## 它修的是什么
 *
 * `Layout` 挂载时 `ensureScansLoaded()` → `loadScans()`，
 * 而 `ScansPage` 自己的 effect 同时也在 `loadScans({ view: 'default' })`
 * （初始槽是 `loading`，不是 fresh，所以那一发是非 silent 的）。
 * 两个 effect 在**同一帧**里跑，于是 `GET /scans` 被发了两遍 ——
 * 实测就是控制台里两条一模一样的请求。
 *
 * `loadStarted` 挡不住它：那是 `ensureScansLoaded` 自己的开关，
 * 而页面根本不走 `ensureScansLoaded`。反过来说，页面也不能去调
 * `ensureScansLoaded` —— 那个函数**没有 view 参数**，用它切 tab 就废了。
 *
 * ## 为什么必须按视图分键
 *
 * 三个视图是三个不同的端点（`?archived=` / `?deleted=`），
 * 共用一个 Promise 会让「切到回收站」拿到默认视图的列表 ——
 * 那比多一个请求糟糕得多。
 *
 * ## 复用规则：**强的可以吞弱的，弱的不许吞强的**
 *
 * 请求分强弱：**非 silent 是强**（它承诺先把槽置成 `loading`、失败要
 * 写成 `error`），**silent 是弱**（不改 status，失败静默）。
 *
 * | 在飞的 | 新来的 | 怎么办 | 为什么 |
 * | --- | --- | --- | --- |
 * | 非 silent | 任意 | **复用** | 新来的想要的东西它都会给，而且给得更全 |
 * | silent | silent | 复用 | 两个都是"顺便刷一下"，没必要发两遍 |
 * | silent | **非 silent** | **不复用，自己发** | 见下 |
 *
 * ⚠️ 最后一行是这个设计里唯一不显然的地方，也是最不能省的一条：
 *    非 silent 的调用者是**用户刚点了「重试」**或刚切进一个脏视图，
 *    它必须真的把 `loading` 摆出来。若它复用了那个 silent 请求，
 *    页面上什么都不会变 —— 而 `loadScans` 的 silent 分支**连失败都不改状态**
 *    （`if (!silent)` 那两处），于是用户点了重试、接口 500，
 *    界面停在原地**一个提示都没有**。那不是省一次请求，是把错误吞掉。
 *
 * ## ⚠️ 还有一个"不许复用"的维度：`fresh`
 *
 * 上面那张表只看了**请求强度**，还漏了一件事：**数据的新鲜度**。
 *
 * 复用意味着"这一发的结果来自更早发出的那个请求"。对轮询与首次加载
 * 无所谓（它们要的就是"当下的列表"），但对**刚做完一次写操作**的调用
 * 就是错的 —— 那个在飞的请求是在写操作**之前**发出的，它返回的列表里
 * **没有**刚改完的那条记录。
 *
 * 这不是理论风险，它正好会撞上 `runBatch` 注释里警告过的那个症状：
 * 撤销删除（`batchRestore(ids, 'default')`）之后记录要回到默认视图，
 * 靠的就是那次 `loadScans({ silent: true, view })`；若它被一个 2.5 秒前
 * 发出的轮询吞掉，用户会看到「已恢复 1 项」的 toast，而列表里没有那条 ——
 * 切走再切回来才出现。所以这类调用点显式传 `fresh: true`。
 *
 * 换句话说：去重只能合并"至少一样强、而且一样旧或更新"的请求。
 * 两个维度都得看，只看强度会漏掉写操作后的那一次。
 *
 * ## 为什么没有 `force` 参数
 *
 * 签名里只有 `silent` / `view` / `fresh`，没有任何"强制刷新"的开关 ——
 * 所以不存在"强制刷新被误去重"这回事。用户主动触发的重试一律是非 silent，
 * 按上表它会自己发新请求，不会被任何在飞的请求挡住。
 */
interface InFlightLoad {
  /** 这一发是不是 silent —— 决定它能不能吞掉新来的请求 */
  silent: boolean
  promise: Promise<void>
}
const inFlight: Partial<Record<ScanView, InFlightLoad>> = {}

/**
 * 快照必须**引用稳定** —— useSyncExternalStore 每次渲染都比较它，
 * 返回新对象会无限重渲染。所以所有变更都走 `commit()`，
 * 且**只替换目标槽**（其余槽的对象引用保持不变，于是依赖它们的组件不重渲染）。
 */
function commit(view: ScanView, next: Partial<ScansSnapshot>) {
  slots = { ...slots, [view]: { ...slots[view], ...next } }
  for (const listener of listeners) listener()
}

function setScans(view: ScanView, next: Scan[]) {
  slots = { ...slots, [view]: { ...slots[view], scans: next } }
  for (const listener of listeners) listener()
}

export function subscribeScans(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** 默认视图的快照。侧栏/总览/其他页面用的都是它。 */
export function getScansSnapshot(): ScansSnapshot {
  return slots.default
}

export function getScansServerSnapshot(): ScansSnapshot {
  return slots.default
}

/** 任意视图的快照（扫描列表页的三个 tab 用） */
export function getViewSnapshot(view: ScanView): ScansSnapshot {
  return slots[view]
}

/** 整体替换默认视图（供测试与特殊场景使用） */
export function replaceScans(next: Scan[]): void {
  setScans('default', next)
}

// ─────────────────────────────────────────────
// 读取
// ─────────────────────────────────────────────

/** 视图 → 查询串。三处必须与后端一致，所以收在一张表里。 */
const VIEW_QUERY: Record<ScanView, string> = {
  default: '',
  archived: '?archived=true',
  deleted: '?deleted=true',
}

/**
 * 真正发请求并落进槽里。**只由 `loadScans` 调用** —— 去重那一层在它外面，
 * 这样"要不要发"与"发了怎么落"是两件事，各自只看自己那点逻辑。
 */
async function fetchScans(view: ScanView, silent: boolean): Promise<void> {
  if (!silent) commit(view, { status: 'loading', error: null })

  try {
    const rows = await api.get<Scan[]>(`/scans${VIEW_QUERY[view]}`)
    commit(view, { scans: rows, status: 'ready', error: null, stale: false })
  } catch (error) {
    if (!silent) commit(view, { status: 'error', error: toApiError(error) })
  }
  // silent 时不改 status：保持 `ready`，只是数据没更新
}

/**
 * 读扫描列表 —— `GET /scans`。
 *
 * ### `silent` 是给轮询用的，而且它是必需的
 *
 * 有扫描在跑时每 2.5 秒刷一次。那种刷新：
 *
 * * **不能**把页面打回骨架态（每 2.5 秒闪一次骨架，比不刷新还糟）；
 * * **失败也不能**把用户正在看的列表换成一个错误页 ——
 *   一次超时不代表列表没了，而下一次轮询可能就成功。
 *   把静默轮询的失败压掉，用户看到的是"数据暂时没更新"而不是"页面坏了"。
 *
 * ### ★ in-flight 去重（2026-10 修）
 *
 * 同一个视图的同一个请求在飞时，后续调用**复用同一个 Promise**，
 * 而不是再发一遍。触发它的是一个真实的重叠：进扫描列表页时，
 * `Layout` 的 `ensureScansLoaded()` 与这一页自己的加载 effect
 * 在**同一帧**里都走到了这里，于是 `GET /scans` 发了两遍。
 * 详见上面 `inFlight` 那段（含"哪种请求可以吞掉哪种"的表）。
 *
 * ⚠️ 复用**不改变**任何一方的语义：非 silent 的调用者拿到的是同一个
 *    Promise，而那一发本来就是非 silent 的（表里的规则保证强请求不会被
 *    弱请求吞掉），所以 `loading` 骨架、失败写成 `error` 都照常发生。
 *
 * ⚠️ 返回的是同一个 Promise 引用 —— 调用方可以 `await` 它，
 *    也可以在 `.then` 里读结果。但**别把它的 resolve 当成"我刚拿到最新数据"**：
 *    复用时它就是别人那一发的结束时刻，数据是那一刻的（要保证"含我刚做的改动"
 *    就传 `fresh: true`，见上面那段）。
 */
export function loadScans(
  options: { silent?: boolean; view?: ScanView; fresh?: boolean } = {},
): Promise<void> {
  const view = options.view ?? 'default'
  const silent = options.silent ?? false
  const fresh = options.fresh ?? false
  if (view === 'default') loadStarted = true

  const pending = inFlight[view]
  /*
    复用判据（对照上面那张表 + `fresh` 那段）：
      · `fresh` 为真 → **一律不复用**。调用方刚做完写操作，
        它要的是"包含那次改动"的数据，而在飞的请求比改动更早发出。
      · 在飞的是**非 silent** → 它比新来的更强或同级，复用；
      · 在飞的是 silent 而新来的也是 silent → 两者等价，复用；
      · 在飞的是 silent 而新来的是**非 silent** → 不复用，自己发。
        这是唯一不能合并的一格 —— 否则用户点的「重试」会变成一个
        连失败都不改状态的静默请求，界面上一点反应都没有。
  */
  if (!fresh && pending && (!pending.silent || silent)) return pending.promise

  const promise = fetchScans(view, silent).finally(() => {
    /*
      只清掉"自己还是当前那一发"的记录。

      为什么不能无条件 `delete inFlight[view]`：上面第三格里，
      一个非 silent 请求会**顶掉**正在飞的 silent 记录。若那个旧的
      silent 结束时把槽清空，它抹掉的是**后来者**的记录 ——
      于是后来者还在飞，而 store 以为自己空闲，下一次调用又发一发新的。
      去重就废了，而且是间歇性失效，最难查。
    */
    if (inFlight[view]?.promise === promise) delete inFlight[view]
  })

  inFlight[view] = { silent, promise }
  return promise
}

/** 什么时候该轮询：只要还有扫描没到终态 */
export function hasActiveScans(list: Scan[]): boolean {
  return list.some((scan) => scan.status === 'queued' || scan.status === 'running')
}

/**
 * 把某个视图标记为「不新鲜」—— 下次进入它时重新拉一次。
 *
 * ⚠️ **只标脏、不重拉**：调用点通常正身处另一个视图，此刻把用户看不见的
 * tab 拉一遍纯属浪费。真正的拉取交给"进入该视图"那一处统一处理，
 * 于是所有加载都只有一条路径，不会出现两个地方各写一套时机判断。
 *
 * `stale` 已经是 true 时不重复 commit —— 快照必须引用稳定，
 * 无意义地换引用会让订阅方白重渲染一轮。
 */
export function invalidateView(view: ScanView): void {
  if (slots[view].stale) return
  commit(view, { stale: true })
}

/** 这个槽现在能不能直接用？`ready` 且不脏才算数。 */
export function isViewFresh(view: ScanView): boolean {
  const slot = slots[view]
  return slot.status === 'ready' && !slot.stale
}

/**
 * 首次加载扫描；已经发起过就**什么都不做**。
 *
 * ★ 与 `ensureSettingsLoaded` 同一个理由：顶部状态条与侧栏都要显示
 *   "N 个运行中"，而它们**每个页面**都在渲染。
 *
 *   ⚠️ 没有它时踩到的症状：直接落在**总览页**时，`/overview` 只回一份
 *     指标快照，不经过 scans-store —— 于是侧栏的"运行中"永远是 0，
 *      而系统里可能真的有几个扫描在跑。那个数字是用户判断"现在忙不忙"
 *      的唯一依据，不能凭空是 0。
 *
 * ⚠️ 同样**不能**在公开页调用（未登录会 401 并被踢到登录页）。
 *    调用点是 `Layout`。
 */
export function ensureScansLoaded(): void {
  if (loadStarted) return
  void loadScans()
}

// ─────────────────────────────────────────────
// 变更操作
// ─────────────────────────────────────────────

export interface CreateScanInput {
  name: string
  target: string
  targetKind: Scan['targetKind']
  skills: Scan['skills']
  providerId: string
  /**
   * 授权确认（契约 §5）。
   *
   * ★ 它**必须**随请求发出，而且缺失时后端返的是 `403 TARGET_NOT_AUTHORIZED`
   *   而不是 400 —— 前端对这两者的处理不同：403 要引导用户去勾选授权，
   *   400 要提示重新确认声明。用错一个码，界面就会把用户指向错误的方向。
   *
   * 这里只做**透传**，不在本地判断"有没有勾" —— 那是服务端的强制项，
   * 前端再判一遍只会造成两处口径，而绕过前端（curl）时服务端仍然拦得住。
   */
  authorization: { confirmed: boolean; statement: string }
  /**
   * 任务背景（任务 12）。可选 —— 不写就是"没有额外上下文"。
   *
   * ⚠️ 它**可能含凭据**（用户会写测试账号）。所以：
   *    · 不进 console.log；
   *    · 后端把它写进**指令文件**而不是命令行（见 runner.py）。
   */
  brief?: string | null
  /** 成功判据。不传就是 `findVulns`（例行评估）。 */
  goal?: ScanGoal
  /**
   * 题目附件的暂存批次 id（任务 12）。
   *
   * ★ 必须走暂存，**不能"建完再传"**：扫描创建后毫秒级被调度器认领，
   *   而附件上传要求 `queued` 状态 —— 那个窗口根本不存在。
   *   见 `stageScanAttachments`。
   */
  attachmentStagingId?: string | null
}

/**
 * 新建扫描 —— `POST /scans`。
 *
 * 初始状态（`queued`）由**后端**决定，本地不猜 —— 猜一个 `running`
 * 会让"已经在打了"这件事看起来比实际更确定，而实际还要等调度器接单。
 *
 * 失败会抛出（不返回一个假的记录）：目标未授权、provider 不存在、
 * 并发已满都要让用户看到，而不是给他一条列表里有、实际不存在的扫描。
 */
export async function createScan(input: CreateScanInput): Promise<Scan> {
  const scan = await api.post<Scan>('/scans', {
    name: input.name,
    target: input.target,
    targetKind: input.targetKind,
    skills: input.skills,
    providerId: input.providerId,
    authorization: input.authorization,
    // 任务 12。空串归一成 null —— 后端也做一遍，但前端先归一
    // 能让"没写"与"写了个空的"在请求体里就是同一个东西。
    brief: input.brief?.trim() ? input.brief.trim() : null,
    goal: input.goal ?? 'findVulns',
    attachmentStagingId: input.attachmentStagingId ?? null,
  })

  // 新建的排在最前 —— 用户刚创建完，最关心的就是它
  setScans('default', [scan, ...slots.default.scans])
  return scan
}

/**
 * 把一个（或多个）题目附件传进**暂存区** —— `POST /scans/attachments/staging`。
 *
 * ## ★ 为什么必须"先暂存"
 *
 * 向导里的动作顺序是：填目标 → 贴题面 → **选附件** → 点启动。
 * 而扫描一旦创建就毫秒级被调度器认领，所以
 * 「建完再往 `/{id}/attachments` 传」这个窗口**根本不存在** ——
 * 那条路会稳定地拿到 `409 SCAN_ALREADY_STARTED`。
 *
 * 所以：选文件时就调这个函数拿到 `stagingId`，点启动时把它放进
 * `createScan({ attachmentStagingId })`，后端把暂存目录移到该扫描名下。
 *
 * ⚠️ 用 `api.upload`（multipart）而**不是** `api.post` ——
 * 后者会 `JSON.stringify` 请求体，而 `JSON.stringify(FormData)` 得到 `"{}"`，
 * 服务端收到一个**没有文件**的请求且**不会报错**。
 */
export async function stageScanAttachments(
  files: File[],
): Promise<{ stagingId: string; files: ScanAttachment[] }> {
  const form = new FormData()
  // 字段名 `files` 必须与后端 `File(...)` 参数名一致
  for (const file of files) form.append('files', file)
  return api.upload<{ stagingId: string; files: ScanAttachment[] }>(
    '/scans/attachments/staging',
    form,
  )
}

/** 题目附件的元信息（与后端 `ScanAttachmentOut` 逐字对应） */
export interface ScanAttachment {
  /** 文件名（已消毒）。agent 在沙箱里就是按这个名字找它 */
  name: string
  sizeBytes: number
}

/** 列出某次扫描的题目附件 —— `GET /scans/{id}/attachments` */
export async function loadScanAttachments(scanId: string): Promise<ScanAttachment[]> {
  return api.get<ScanAttachment[]>(`/scans/${scanId}/attachments`)
}

/** CTF 题解的响应体（与后端 `WriteupOut` 逐字对应） */
export interface ScanWriteup {
  /** 题解正文，Markdown 原文 —— 渲染交给前端，后端不掺 HTML */
  markdown: string
  /**
   * 下载用的文件名，**由后端决定**（如 `CTF-Writeup-tryre.md`）。
   *
   * ⚠️ 前端不自己按目标名拼一个：题目的身份在题解里（后端从报告里读出题目名），
   * 前端手上只有 `target`（一条 `host:port` 或空字符串）—— 拼出来的名字
   * 和正文标题里的题目名对不上，而用户会把两份文件按名字对上号。
   */
  filename: string
}

/**
 * CTF 题解（Writeup）—— `GET /scans/{id}/writeup`。
 *
 * ★ **一次性读取，不进 store 的快照。**
 *
 * 三个槽里放的是「一屏扫描的摘要」，每 2.5 秒轮询整体换引用；
 * 而题解是一份可能几十 KB 的文档，只在一个对话框打开时才需要。
 * 塞进快照的代价很实在：每次轮询都要重建一份大字符串，而所有订阅者
 * （侧栏的"运行中 N"、总览）都会因为这个与它们无关的变化重渲染一轮。
 *
 * ⚠️ **失败在这里不兜底**，`ApiError` 原样抛给调用方。两种失败都靠
 *    后端那句中文说清楚，前端换个说法只会把可行动的信息换成泛泛的失败：
 *      · `404` —— 扫描不存在；
 *      · `409 WRITEUP_UNAVAILABLE` —— 这次不是夺旗任务 / 扫描还没结束 /
 *        报告文件已不在。**这三种情况的下一步动作完全不同**，
 *        所以那句话必须原样送到用户眼前（见 `WriteupDialog` 的失败态）。
 */
export async function loadWriteup(scanId: string): Promise<ScanWriteup> {
  return api.get<ScanWriteup>(`/scans/${scanId}/writeup`)
}

/**
 * 终止正在跑的扫描 —— `POST /scans/{id}/abort`。
 *
 * ★ **不用本地改状态**：后端会真的杀掉整个进程树（含孙进程），
 *   并把终态写回来。本地先改成 `aborted` 会让界面在进程还活着的时候就
 *   显示"已终止" —— 而"它到底停了没有"正是用户按下这个按钮要知道的事。
 *
 * 对已完成的扫描，后端返 `409 SCAN_NOT_ABORTABLE`，直接透传那句中文。
 */
export async function abortScan(id: string): Promise<Scan> {
  const scan = await api.post<Scan>(`/scans/${id}/abort`)
  setScans('default', slots.default.scans.map((s) => (s.id === id ? scan : s)))
  return scan
}

/**
 * 重跑 —— `POST /scans/{id}/rerun`。
 *
 * ★ 后端**新建一条记录**，旧记录纹丝不动（契约 §3.3）。
 *   把已完成的记录改回 `running` 会抹掉那次历史，
 *   而"哪次发现了什么、修没修"正是扫描历史全部的价值。
 *
 * 同样要带授权确认：重跑是又一次真的发包。
 */
export async function rerunScan(
  id: string,
  authorization: { confirmed: boolean; statement: string },
): Promise<Scan> {
  const scan = await api.post<Scan>(`/scans/${id}/rerun`, { authorization })
  setScans('default', [scan, ...slots.default.scans])
  return scan
}

/**
 * 续跑 —— `POST /scans/{id}/resume`。
 *
 * ★ 与重跑的关键区别：**后端沿用同一条记录**，接回执行后端上次的会话
 *   （root 与未终结子 agent 的完整 LLM 历史与拓扑，外加工作目录里的
 *   notes / threat_model / todos / coverage）。
 *
 * 所以这里**不能**像 `rerunScan` 那样往列表头部塞一条新记录 ——
 * 回来的是**已经有位置的那一条**（状态回到 queued）。直接按 id 替换，
 * 否则列表里会出现两条一模一样的记录。
 *
 * 同样要带授权确认：续跑会继续对真实目标发包。
 */
export async function resumeScan(
  id: string,
  authorization: { confirmed: boolean; statement: string },
): Promise<Scan> {
  const scan = await api.post<Scan>(`/scans/${id}/resume`, { authorization })
  // ★ 就地替换，不是插入 —— 见上面的说明。
  const next = slots.default.scans.map((item) => (item.id === id ? scan : item))
  setScans('default', next)
  return scan
}

// ─────────────────────────────────────────────
// 批量操作（任务 9）
// ─────────────────────────────────────────────
//
// ## 为什么是**乐观更新**而不是"转圈等结果"
//
// 批量操作作用于**用户已经看得见的那些行**。让它们在点击后原地转圈几秒，
// 换来的信息量是零 —— 用户已经知道自己点了什么。
// 而乐观更新让列表**立刻**反映结果，同时用一条 toast 给出撤销窗口：
// "先做，再给后悔的机会"，比"先问一遍"少一次点击、也不更危险。
//
// ## 回滚为什么是"重新拉一次"而不是"记一份快照"
//
// 记快照（`previous = scans`）只适合**没有并发**的场景。而这一页有
// 2.5 秒轮询：请求失败时那份快照可能已经过期（轮询拿到了更新的数据），
// 用它覆盖会把**新数据换成旧数据**。
//
// 所以策略是：先立刻恢复到"操作前"（避免界面停在失败态），
// 然后**静默重新拉一次**，以服务端为准。

/**
 * 乐观地把这些 id 从当前列表移走。
 *
 * 归档 / 删除都让记录**离开当前视图**，所以两者的本地效果是一样的：
 * 先从列表里消失。返回的旧数组用于失败时立刻恢复。
 */
function optimisticRemove(view: ScanView, ids: string[]): Scan[] {
  const previous = slots[view].scans
  const removing = new Set(ids)
  setScans(
    view,
    previous.filter((scan) => !removing.has(scan.id)),
  )
  return previous
}

/** 失败时：先恢复上一帧，再以服务端为准重新拉一次。 */
function rollback(view: ScanView, previous: Scan[]): void {
  setScans(view, previous)
  /*
    ⚠️ `fresh: true` 是必需的：这是**失败回滚之后**的重拉，要读回服务端的
    真实现状。若被一个更早发出的轮询吞掉，拿回的就是"操作前那一刻"的列表，
    回滚等于白做 —— 界面停在旧数据上，而用户以为已经恢复了。
  */
  void loadScans({ silent: true, view, fresh: true })
}

/**
 * ★「这个操作会不会让记录**离开当前视图**」—— 一张表说清。
 *
 * 这个判断决定了能不能乐观更新。写成一个显式的表而不是散在各处的
 * 布尔表达式，是因为它最容易在加新视图/新操作时漏掉一处，
 * 而漏掉的表现是"记录消失得很顺，回来时却要等"或者
 * "取消了归档但它还在列表里"——两种都很难指认到这一行。
 */
function leavesView(view: ScanView, kind: 'archive' | 'unarchive' | 'delete' | 'restore' | 'purge'): boolean {
  switch (kind) {
    // 删除与永久删除：记录离开**任何**视图
    case 'delete':
    case 'purge':
      return true
    // 归档在默认视图里 = 离开；在归档视图里归档本身是幂等空操作
    case 'archive':
      return view === 'default'
    // 取消归档只在归档视图里做，做完成即离开
    case 'unarchive':
      return view === 'archived'
    // 恢复只在回收站里做，做完离开回收站
    case 'restore':
      return view === 'deleted'
  }
}

export interface BatchResult {
  changed: number
  skipped: number
}

/**
 * 这类操作会把记录**送进**哪个视图。
 *
 * ★ 有了这张表，「一次操作同时改变两个视图」才说得清：
 * `leavesView` 回答的是"它还留在我正看的这个视图里吗"，
 * 而这张表回答的是"它去哪儿了" —— 那个视图的数据同样过期了。
 *
 * 归档进归档视图、取消归档回默认视图、删除进回收站、恢复回默认视图。
 * 永久删除（`purgeScan`）不在这张表里：记录直接消失，没有"去的地方"。
 */
const ENTERS_VIEW: Record<'archive' | 'unarchive' | 'delete' | 'restore', ScanView> = {
  archive: 'archived',
  unarchive: 'default',
  delete: 'deleted',
  restore: 'default',
}

/**
 * ★ 乐观更新**只对"移出视图"成立**，对"移入视图"不成立。
 *
 * 这个区分很容易写漏，而漏了的表现是"取消归档之后它没回来"：
 *
 * | 操作 | 对当前（默认）视图 | 能乐观吗 |
 * | --- | --- | --- |
 * | 归档 | 记录**离开** | ✅ 本地 filter 掉即可 |
 * | 删除 | 记录**离开** | ✅ 同上 |
 * | 取消归档 | 记录**回来** | ❌ 插到哪一列？按什么排序？本地猜不出来 |
 * | 恢复 | 记录**回来** | ❌ 同上 |
 *
 * 所以"移入"类操作等服务端，然后静默重拉一次 ——
 * 猜一个位置再纠正，比等一下更糟：用户会看到它在列表里跳一下。
 *
 * ★ 但**只处理当前视图是不够的**：任何一次批量操作都让记录跨越了两个
 * 视图，对侧那个槽同样过期。漏掉它的症状比上面更隐蔽 —— 不报错，
 * 只是切过去时列表里少一条，切回来又在了。所以这里无条件把对侧标脏。
 */
async function runBatch(
  path: string,
  body: Record<string, unknown>,
  view: ScanView,
  kind: 'archive' | 'unarchive' | 'delete' | 'restore',
): Promise<BatchResult> {
  const leaves = leavesView(view, kind)
  const previous = leaves ? optimisticRemove(view, (body.ids as string[]) ?? []) : null

  /*
    对侧那个视图已经被这次操作改动了，标脏让下次进入时重拉。

    放在 `try` 之前而不是成功之后，是因为"标脏"是**保守**的：即使请求
    失败、数据其实没变，代价也只是下次进入时多拉一次。反过来漏标则是
    永久的不一致（直到用户手动刷新），两种错的代价不对等。
  */
  const other = ENTERS_VIEW[kind]
  if (other !== view) invalidateView(other)

  try {
    const result = await api.post<BatchResult>(path, body)
    if (!leaves) {
      /*
        记录回到视图里了 —— 以服务端为准重拉（排序与筛选都在服务端）。

        ⚠️ `fresh: true` **不能省**：这次重拉是"把刚写进去的那条读回来"，
        而在飞的 2.5 秒轮询是在这次写操作**之前**发出的，它的结果里没有那条。
        复用它 = 用户看到"已恢复 1 项"的 toast，列表里却没有那条记录，
        切走再切回来才出现 —— 正是本文件上面 `stale` 那段在防的同一类不一致。
      */
      await loadScans({ silent: true, view, fresh: true })
    }
    return result
  } catch (error) {
    if (previous) rollback(view, previous)
    throw error
  }
}

/** 批量归档 / 取消归档 —— `POST /scans/batch/archive`。 */
export function batchArchive(
  ids: string[],
  archived: boolean,
  view: ScanView = 'default',
): Promise<BatchResult> {
  return runBatch(
    '/scans/batch/archive',
    { ids, archived },
    view,
    archived ? 'archive' : 'unarchive',
  )
}

/** 批量软删除（进回收站）—— `POST /scans/batch/delete`。 */
export function batchDelete(ids: string[], view: ScanView = 'default'): Promise<BatchResult> {
  return runBatch('/scans/batch/delete', { ids }, view, 'delete')
}

/** 批量从回收站恢复 —— `POST /scans/batch/restore`。 */
export function batchRestore(ids: string[], view: ScanView = 'deleted'): Promise<BatchResult> {
  return runBatch('/scans/batch/restore', { ids }, view, 'restore')
}

/**
 * **永久删除**一次扫描 —— `DELETE /scans/{id}/purge`。
 *
 * 只在回收站里有意义。两种 `409` 由后端给出（不在回收站 / 被报告引用），
 * 这里**原样透传那句中文** —— 尤其是后者，它指名了是哪份报告，
 * 而那是用户唯一能采取行动的线索。
 */
export async function purgeScan(id: string, view: ScanView = 'deleted'): Promise<void> {
  const previous = optimisticRemove(view, [id])
  try {
    await api.del<void>(`/scans/${id}/purge`)
  } catch (error) {
    rollback(view, previous)
    throw error
  }
}

export interface BatchRerunResult {
  created: Scan[]
  failed: { scanId: string; error: string }[]
}

/**
 * 批量重跑 —— `POST /scans/batch/rerun`。
 *
 * ⚠️ 必须带**授权确认**：批量重跑是 N 次真的发包，
 * 不能因为"批量"就绕过契约 §5.3 的法律要求。
 *
 * 不做乐观更新：重跑是**新建**记录，没有"可以乐观预测的结果"
 * （id 由服务端分配）。猜一个假的 id 插进列表，之后还得替换掉它。
 */
export async function batchRerun(
  ids: string[],
  authorization: { confirmed: boolean; statement: string },
): Promise<BatchRerunResult> {
  const result = await api.post<BatchRerunResult>('/scans/batch/rerun', { ids, authorization })
  if (result.created.length > 0) {
    // 新建的排在最前 —— 与 `createScan` 同一个理由。
    // 注意：重跑出来的记录在**默认视图**里，不在归档/回收站。
    setScans('default', [...result.created, ...slots.default.scans])
  }
  return result
}

/**
 * 两条扫描的发现增减 —— `GET /scans/compare`。
 *
 * ★ **比对逻辑在后端，前端不算。**
 * 它要读两次扫描的**全部漏洞标题**，而 `GET /scans` 只回按严重度的计数，
 * `GET /findings?scanId=` 才是明细。在前端算意味着先拉两份明细再求差集 ——
 * 两次请求之间数据可能变化（扫描还在跑），于是"对比结果"来自两个不同时刻。
 * 一次请求一次快照，才谈得上"对比"。
 */
export interface ScanComparison {
  a: Scan
  b: Scan
  onlyInA: string[]
  onlyInB: string[]
  common: string[]
  verdict: string
}

export async function compareScans(a: string, b: string): Promise<ScanComparison> {
  const query = new URLSearchParams({ a, b })
  return api.get<ScanComparison>(`/scans/compare?${query.toString()}`)
}

// ─────────────────────────────────────────────
// 调度器状态（"为什么还在排队"）
// ─────────────────────────────────────────────

export interface QueueStatus {
  running: number
  limit: number
  queued: number
  runner: string
  runnerReady: boolean
  runnerError: string | null
}

/**
 * 读调度器状态 —— `GET /scans/queue`。
 *
 * 契约 §7 专门点了这个端点：没有它，界面只能显示"排队中"，
 * 而那看起来像坏了。用户需要知道的是**为什么**在排队 ——
 * 是并发上限满了，还是执行后端没就绪。
 *
 * 它不放进快照：这是"此刻的调度器状态"，不是扫描集合的一部分，
 * 混进快照会让每次轮询都重建整个列表的引用。
 */
export async function fetchQueueStatus(): Promise<QueueStatus> {
  return api.get<QueueStatus>('/scans/queue')
}

// ─────────────────────────────────────────────
// 订阅 hook
// ─────────────────────────────────────────────

/** 只要**默认视图**的列表。多数组件用它 —— 引用稳定，status 变化不会触发重渲染 */
export function useScans(): Scan[] {
  return useSyncExternalStore(
    subscribeScans,
    () => getScansSnapshot().scans,
    () => getScansServerSnapshot().scans,
  )
}

/** 要三态时用它（页面级组件）。返回的也是**默认视图**。 */
export function useScansState(): ScansSnapshot {
  return useSyncExternalStore(subscribeScans, getScansSnapshot, getScansServerSnapshot)
}

/**
 * 任意视图的三态（扫描列表页的「已归档」/「回收站」tab 用）。
 *
 * ⚠️ `getSnapshot` 必须返回**引用稳定**的对象：这里返回的是槽本身，
 * 而槽只在该视图被 commit 时才换新对象 —— 所以切 tab 不会让
 * 其它视图的订阅者重渲染。
 */
export function useScanView(view: ScanView): ScansSnapshot {
  return useSyncExternalStore(
    subscribeScans,
    () => getViewSnapshot(view),
    () => getViewSnapshot(view),
  )
}
