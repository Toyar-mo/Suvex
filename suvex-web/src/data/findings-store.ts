import { API_PREFIX, ApiError, api, toApiError } from '@/lib/api'
import { useCallback, useSyncExternalStore } from 'react'
import type {
  Finding,
  FindingCategory,
  FindingDetail,
  FindingListItem,
  FindingStatus,
  FindingTimelineEntry,
} from './finding'
// ⚠️ `Severity` 在 `data/findings.ts`（复数），而 `FindingCategory` 在
// `data/finding.ts`（单数）。两个文件名只差一个 s，导入时极其容易搞错 ——
// 而搞错的表现是 TS 报"模块没有导出成员"，看不出是文件名的锅。
import type { Severity } from './findings'

/**
 * 漏洞数据的**单一运行时事实来源**。
 *
 * 为什么需要它（brief 的硬要求：跨页面数据口径一致）：
 *
 *   Overview 的严重度图表和 Findings 列表如果各自 useState 一份快照，
 *   就会漂移 —— 用户在 Findings 把一条标成"误报"，回到 Overview 图表
 *   数字还是旧的。那不是"缓存没刷新"，是**两个事实来源**。
 *
 * 所以两边读同一个 store。用 useSyncExternalStore 而不是 Context：
 *   - 不需要在树上套 Provider；
 *   - 订阅粒度精确 —— 只有读过的字段变了才触发重渲染；
 *   - 它就是为"React 之外的可变数据源"设计的，语义完全吻合。
 *
 * ## 这一层管什么
 *
 *   1. `GET /findings`                   列表（摘要，**不含 detail / timeline**）
 *   2. `GET /findings/{id}`              单条详情，**按需**加载（抽屉打开时才发）
 *   3. `GET /findings/{id}/attachments`  附件，**按需**加载（同上）
 *
 * ## ⚠️ 角标不再读 `GET /findings/stats`（任务 10 P6-b）
 *
 * 原来这里还拉 `/findings/stats` 并把它的 `bySeverity` / `byStatus` /
 * `byCategory` 给筛选 chip 当角标。现在**不拉了** —— 角标改成从
 * 已加载的列表算 facet 计数（`lib/finding-query.facetCounts`）。
 *
 * 原因有两条，第二条是决定性的：
 *   1. 角标必须**跟随筛选**（契约定为 P6 的一项）。后端全量口径的角标
 *      在用户搜索"登录"之后仍然指着全量，点进去发现数量对不上。
 *   2. 界面的筛选是**多选**的（可同时勾「高危 + 中危」），而后端的
 *      `?facets=true` 用的是逗号分隔的多值 —— 走后端意味着每敲一个
 *      搜索字符发一次请求，且角标与表格会短暂对不上
 *      （"表格已经 3 条、角标还是 7"）。从已加载的列表算则零延迟、
 *      且与表格**必然一致**（同一个数组算出来的）。
 *
 * `/findings/stats` **本身仍然是活的**：`/overview` 在服务端调它
 * （契约 §3.4.1 的"两页数字来自同一处查询逻辑"讲的是那条路径），
 * 它也是 API 契约的一部分。
 *
 * ## ★ 列表项与详情项是两个不同的形状，这是刻意的
 *
 * 列表一次可能几百条，每条都带完整证据文本与时间线会让响应到几 MB。
 * 所以 store 里列表项带一个**空的 `detail` 占位**，`timeline` 为空数组 ——
 * 这样组件类型不用变（`FindingsTable` 仍可读 `finding.detail.affected[0]`，
 * 只是真实数据下它多半为空，那一行自然不渲染）。
 *
 * ⚠️ 别用列表项去渲染详情抽屉：它会把空描述、空证据当成"这条漏洞没有内容"
 *    显示给用户，而这与"还在加载"看起来一模一样 —— 是最糟的一种错。
 *    抽屉一律走 `useFindingDetail(id)`。
 */

type Listener = () => void

/** 加载状态。`ready` 之前页面显示骨架，`error` 显示可重试的错误 */
export type FindingsStatus = 'loading' | 'ready' | 'error'

export interface FindingsSnapshot {
  /** 列表。`detail` 是空占位，`timeline` 为空 —— 见文件头 */
  findings: Finding[]
  /** 各维度计数。与 findings 同一次请求返回，所以不会出现"列表有数据、角标全 0" */
  status: FindingsStatus
  /**
   * ★ 存**真的 `ApiError`** 而不是一句文案 —— `ErrorState` 要靠
   *   `isNetworkError` 区分「连不上后端」与「后端答了一个错误」，
   *   见 `lib/api.ts` 的 `toApiError`。
   */
  error: ApiError | null
  /**
   * 当前列表是**默认视图**还是**回收站**（任务 10 P6-a）。
   *
   * ★ 放进快照而不是留在页面里：轮询刷新（`loadFindings({silent:true})`）
   *   必须知道自己在刷哪一份 —— 少了它，在回收站里等自动刷新会把列表
   *   悄悄换成默认视图，而用户会以为"回收站自己清空了"。
   */
  scope: FindingScope
}

/**
 * 列表的范围（任务 10 P6-a）。
 *
 * 与扫描模块**同一套语义**（`?deleted=true` 那个视图）：
 *   · `active` —— 默认，**排除**回收站里的条目；
 *   · `trash`  —— 只看回收站里的。
 *
 * 没有"两者都要"这一档：把删掉的混进正常列表会让"我这里到底还有几条"
 * 变成一个需要逐条看状态才能回答的问题。
 */
export type FindingScope = 'active' | 'trash'

const listeners = new Set<Listener>()

/**
 * ★ **初始为空** —— 原来是 `MOCK_FINDINGS`。假数据在这里的危害不只是"占位"：
 *   新用户第一次打开会看到 13 条不存在的漏洞，而"我有 13 个漏洞"和
 *   "我还没扫过"是两种完全不同的处境，界面给的建议也完全不同。
 */
let findings: Finding[] = []
let dataStatus: FindingsStatus = 'loading'
let loadError: ApiError | null = null
/** 当前列表的范围。见 `FindingScope` */
let scope: FindingScope = 'active'

/**
 * 列表请求的序号，用来丢弃过期响应。
 *
 * ★ 与详情那边的 `detailEpoch` 是**同一个模式**，各管一条路径：
 *   · `detailEpoch` 管"详情"（切换打开的漏洞时旧请求后返回）；
 *   · `listEpoch`   管"列表"（切换范围时旧请求后返回）。
 *
 * 两个计数**刻意不共用**：它们是两条独立的请求路径，
 * 共用一个计数器会让"打开一个详情"把"正在飞的列表请求"判成过期 ——
 * 列表就永远加载不出来。这类"为了省一个变量而引入跨路径干扰"的错，
 * 症状是随机的、极难查。
 */
let listEpoch = 0

/**
 * 快照必须**引用稳定** —— useSyncExternalStore 每次渲染都会比较它，
 * 返回新对象会无限重渲染。所以所有变更都走 `commit()`。
 */
let snapshot: FindingsSnapshot = { findings, status: dataStatus, error: loadError, scope }

function emit() {
  for (const listener of listeners) listener()
}

function commit() {
  snapshot = { findings, status: dataStatus, error: loadError, scope }
  emit()
}

export function subscribeFindings(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function getFindingsSnapshot(): FindingsSnapshot {
  return snapshot
}

/** 服务端/预渲染快照 —— 纯 CSR，用同一个即可 */
export function getFindingsServerSnapshot(): FindingsSnapshot {
  return snapshot
}

// ─────────────────────────────────────────────
// 列表项的空壳
// ─────────────────────────────────────────────

/**
 * 列表项共用的空 `detail`。
 *
 * ★ 共享**同一个对象引用**是刻意的：几百条列表项没必要各持一个内容相同的
 *   对象，而且真共享比"看起来共享"更难被误改。
 *
 * ⚠️ 因此这个对象必须当成**只读**。谁就地改它，所有列表项会一起变。
 */
const EMPTY_DETAIL: FindingDetail = {
  description: '',
  affected: [],
  reasoning: '',
  evidence: '',
  poc: null,
  remediation: null,
}

/** 同理：列表项的 `timeline` 共用一个空数组 */
const EMPTY_TIMELINE: FindingTimelineEntry[] = []

/** 后端摘要 → 前端领域对象（补上空壳） */
function toListItem(row: FindingListItem): Finding {
  return { ...row, detail: EMPTY_DETAIL, timeline: EMPTY_TIMELINE }
}

// ─────────────────────────────────────────────
// 读取
// ─────────────────────────────────────────────

/**
 * 读漏洞列表与统计 —— `GET /findings` + `GET /findings/stats`。
 *
 * 两个请求 **`Promise.all` 一起发**而不是先列表后统计：
 * 串行会让"表格已经出来了、角标还是 0"持续一个往返，
 * 而 0 与"真的没有"在界面上长得一模一样。
 *
 * ### `silent` 是给"已经有数据时刷新"用的
 *
 * 它**不能**把页面打回骨架态（每几秒闪一次骨架比不刷新更糟），
 * 失败也**不能**把用户正在看的列表换成错误页 —— 一次超时不代表列表没了。
 * 所以静默刷新失败时保持 `ready` 与旧数据，用户看到的是"暂时没更新"。
 */
export async function loadFindings(
  options: { silent?: boolean; scope?: FindingScope } = {},
): Promise<void> {
  const silent = options.silent ?? false
  /*
    ★ 请求序号：丢弃过期响应（2026-10-10 修）。

    ## 这个洞是怎么来的

    本文件的详情有 `detailEpoch`、总览有 `epoch`、报告预览有 `previewEpoch`
    —— **唯独列表加载没有**。而列表恰恰是最容易并发的那条路径：
    用户点「回收站」再点回「未删除」，两个请求同时在飞。

    ## 不修会怎样（完全静默）

    `query` 在 `await` **之前**就算好了，所以先发的那个请求带的是**旧范围**。
    它后返回时无条件覆盖 `findings`，于是：

      · 范围按钮高亮「未删除」；
      · 列表里是**回收站的内容**；
      · `dataStatus === 'ready'`、`loadError === null` —— 没有任何提示。

    这正是本文件开头（21-23 行）自己强调要避免的"两个事实来源"。

    ## 判据为什么是"序号不等就整个返回"

    而不是"只跳过数据赋值"：过期响应连**状态**都不该写。
    否则先发的 trash 请求失败时会把它自己的 error 写上去，
    覆盖掉后发的、已经成功的那一次 —— 用户看到一次假报错。
  */
  const current = ++listEpoch

  /*
    ★ 切换范围（默认视图 ↔ 回收站）时，`scope` 要先落定再发请求 ——
    否则请求回来的那一刻 `scope` 还是旧值，而 `commit()` 会把新数据
    配上旧范围，界面就会在错误的视图下显示正确的内容
    （表现为"点了回收站，列表没变"）。
  */
  if (options.scope && options.scope !== scope) {
    scope = options.scope
    // 换了范围就**清空列表**：留着上一個范围的数据会让用户在
    // 加载的那一瞬间看到"回收站里有 13 条"（其实是默认视图那些）。
    findings = []
    dataStatus = 'loading'
    loadError = null
    commit()
  }

  if (!silent) {
    dataStatus = 'loading'
    loadError = null
    commit()
  }

  const query = scope === 'trash' ? '?deleted=true' : ''
  try {
    /*
      ★ 只拉列表，**不再拉 `/findings/stats`**（任务 10 P6-b 的连带改动）。

      理由：角标改成从**已加载的列表**算 facet 计数（见
      `lib/finding-query.facetCounts` —— 那边解释了为什么必须这样）。
      于是这个 store 里的 `stats` 成了没人读的字段，而它每次加载
      还多发一个请求、每次变更再多发三个。

      `/findings/stats` **本身仍然是活的**：`/overview` 在服务端调它
      （契约 §3.4.1 的"两页数字来自同一处查询逻辑"讲的就是那条路径），
      而且它是 API 契约的一部分，其他客户端仍可用。
    */
    const list = await api.get<FindingListItem[]>(`/findings${query}`)
    // ★ 过期就整个返回 —— 连状态也不写（理由见函数开头那段）。
    if (current !== listEpoch) return
    findings = list.map(toListItem)
    dataStatus = 'ready'
    loadError = null
  } catch (error) {
    if (current !== listEpoch) return
    if (!silent) {
      dataStatus = 'error'
      loadError = toApiError(error)
    }
  }
  commit()
}

/** 当前列表范围（回收站视图要用它决定显示哪一套动作） */
export function getFindingScope(): FindingScope {
  return scope
}

// ─────────────────────────────────────────────
// 详情（按需加载）
// ─────────────────────────────────────────────

export interface FindingDetailState {
  status: 'idle' | 'loading' | 'ready' | 'error'
  /** 完整详情（含 detail 与 timeline）。未就绪时为 null */
  finding: Finding | null
  error: ApiError | null
}

/** 未加载过的 id 都返回它 —— 共享引用，保证快照稳定 */
const IDLE_DETAIL: FindingDetailState = { status: 'idle', finding: null, error: null }

const detailListeners = new Set<Listener>()
let detailStates: Record<string, FindingDetailState> = {}
let detailSnapshot: Record<string, FindingDetailState> = detailStates

/** 每个 id 一个序号，用来丢弃过期响应（快速连开两条时后发先至） */
const detailEpoch: Record<string, number> = {}

function emitDetail() {
  for (const listener of detailListeners) listener()
}

function commitDetail() {
  detailSnapshot = { ...detailStates }
  emitDetail()
}

export function subscribeFindingDetail(listener: Listener): () => void {
  detailListeners.add(listener)
  return () => detailListeners.delete(listener)
}

export function getFindingDetailSnapshot(id: string): FindingDetailState {
  return detailSnapshot[id] ?? IDLE_DETAIL
}

/**
 * 取一条漏洞的完整详情 —— `GET /findings/{id}`。
 *
 * 打开抽屉时调用。已经 `ready` 的直接返回缓存（**不重复请求**）：
 * 用户打开又关掉再打开同一条是常见操作，每次都发请求是白烧。
 *
 * 并发去重：同一条正在加载时再次调用直接返回，不会发第二个请求。
 */
export async function loadFindingDetail(id: string): Promise<void> {
  const current = detailSnapshot[id]
  if (current?.status === 'loading' || current?.status === 'ready') return
  await fetchFindingDetail(id)
}

/** 强制重取 —— 状态流转后用，因为时间线变了 */
export async function refreshFindingDetail(id: string): Promise<void> {
  if (detailSnapshot[id]?.status === 'loading') return
  await fetchFindingDetail(id)
}

async function fetchFindingDetail(id: string): Promise<void> {
  const epoch = (detailEpoch[id] ?? 0) + 1
  detailEpoch[id] = epoch

  detailStates = {
    ...detailStates,
    [id]: { status: 'loading', finding: detailSnapshot[id]?.finding ?? null, error: null },
  }
  commitDetail()

  try {
    const finding = await api.get<Finding>(`/findings/${id}`)
    // 期间又刷新过一次 —— 这次的结果已经过期，丢掉
    if (detailEpoch[id] !== epoch) return
    detailStates = { ...detailStates, [id]: { status: 'ready', finding, error: null } }
  } catch (error) {
    if (detailEpoch[id] !== epoch) return
    detailStates = { ...detailStates, [id]: { status: 'error', finding: null, error: toApiError(error) } }
  }
  commitDetail()
}

/**
 * 订阅一条漏洞的详情状态。
 *
 * `id` 为 null（抽屉关闭）时返回 idle —— 让调用方可以无条件调用这个 hook，
 * 不必在组件里写条件 hook（那违反 hooks 规则）。
 */
export function useFindingDetail(id: string | null): FindingDetailState {
  const subscribe = useCallback((listener: Listener) => subscribeFindingDetail(listener), [])
  const getSnapshot = useCallback(
    () => (id ? getFindingDetailSnapshot(id) : IDLE_DETAIL),
    [id],
  )
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}

// ─────────────────────────────────────────────
// 变更操作
// ─────────────────────────────────────────────

export interface StatusChangeResult {
  /** 真的改了的条数 */
  changed: number
  /** 本来就是目标状态、被跳过的条数 */
  skipped: number
}

/**
 * 批量流转状态 —— `POST /findings/status`。
 *
 * ★ **不再是本地改**。三条理由，每条都单独成立：
 *
 *   1. 幂等由服务端判定。本地判断"是不是已经是目标状态"只是提示，
 *      真正算数的是服务端那一次 —— 两个客户端同时操作时，
 *      本地那份快照可能已经过期。
 *   2. 时间线是**审计记录**。"谁在什么时候改的"必须由服务端落库，
 *      本地生成的时间线刷新一次就没了，而审计信息丢了就是丢了。
 *   3. 后端返回 `changed` / `skipped`，界面上"实际改了几条、跳过几条"
 *      这句话才有依据。本地猜一个会让用户对着错数字做判断。
 *
 * 抛错交给调用方 —— 批量操作失败必须显示在页面上，不能静默。
 */
export async function changeFindingStatus(
  ids: string[],
  status: FindingStatus,
  note?: string,
): Promise<StatusChangeResult> {
  const response = await api.post<{
    changed: number
    skipped: number
    findings: FindingListItem[]
  }>('/findings/status', { ids, status, note: note ?? null })

  /*
    后端只回**真正改了**的那些，而且是摘要（没有 detail / timeline）。
    所以只覆盖它确实带回来的那个字段，其余保持本地现状 ——
    整体替换会把这些条目的 detail 占位换成 undefined，而 undefined
    在 `detail.affected[0]` 那个位置会直接抛错。
  */
  const changedById = new Map(response.findings.map((row) => [row.id, row]))
  findings = findings.map((finding) => {
    const updated = changedById.get(finding.id)
    return updated ? { ...finding, status: updated.status } : finding
  })
  commit()

  // 角标与 Overview 图表读的是统计接口，改完必须让它追上

  /*
    分组里存着 `byStatus`（"这组里 1 个已确认、2 个待确认"），
    所以状态一变它就不准了 —— 但这里**只标脏、不立刻重拉**：
    用户可能正在列表视图连做十次流转，每次重拉就是十次没人看的全量聚合。
    等他真的切到分组视图时（`loadFindingGroups`）再拉。

    ⚠️ 指派**不需要**这一句 —— 分组结构里没有负责人字段，指派对它没影响。
    在那边顺手也写一次会变成"每次指派都白标一次脏"，虽然无害，
    但会让下一个人以为分组数据里确实有负责人信息。
  */
  invalidateGroups()

  // 已经加载过详情的那几条，时间线已经变了 —— 静默重取，抽屉里的操作记录会自己更新
  for (const id of changedById.keys()) {
    if (detailSnapshot[id]?.status === 'ready') void refreshFindingDetail(id)
  }

  return { changed: response.changed, skipped: response.skipped }
}

/**
 * 批量指派负责人 —— `POST /findings/assign`（任务 10 P2）。
 *
 * `assigneeId = null` 是**取消指派**：契约把它定成一个明确的操作
 * （必填字段、可以为 null），所以这里也用一个显式的 `null` 表达它，
 * 而不是给个可选参数 —— 那样"漏传"和"故意清空"在代码里长得一样，
 * 而线上表现是"点取消指派没反应"或者"不小心把负责人清了"。
 *
 * 响应带回**更新后的摘要**（含 `assigneeName`），所以直接合并进本地，
 * 不必再拉一次列表 —— 那会让整页闪一下。
 */
export async function assignFindings(
  ids: string[],
  assigneeId: string | null,
): Promise<StatusChangeResult> {
  const response = await api.post<{
    changed: number
    skipped: number
    findings: FindingListItem[]
  }>('/findings/assign', { ids, assigneeId })

  const changedById = new Map(response.findings.map((row) => [row.id, row]))
  findings = findings.map((finding) => {
    const updated = changedById.get(finding.id)
    return updated
      ? { ...finding, assigneeId: updated.assigneeId, assigneeName: updated.assigneeName }
      : finding
  })
  commit()

  // 时间线变了（每条各写一行 `assigned`），打开着的详情要跟上
  for (const id of changedById.keys()) {
    if (detailSnapshot[id]?.status === 'ready') void refreshFindingDetail(id)
  }

  return { changed: response.changed, skipped: response.skipped }
}

// ─────────────────────────────────────────────
// 回收站（任务 10 P6-a）
// ─────────────────────────────────────────────

/**
 * 软删除 —— `POST /findings/batch/delete`。
 *
 * ★ 与扫描模块**同一套交互**：立即执行 + toast 给一个撤销窗口，
 *   而不是先弹确认框。误删不会丢数据（数据一条不丢，只是打了标记），
 *   所以"先做再撤销"比"先问一遍"更顺 —— 而确认框点多了会被无脑确认，
 *   那时它就完全不起作用了。
 *
 * ★ **本地不删也不改状态**：调用方（页面）拿到结果后决定怎么处理列表。
 *   与服务端保持一致的做法是"删完让它从当前视图消失"，而那是**视图**
 *   层面的事，store 不该替它猜。
 */
export async function deleteFindings(ids: string[]): Promise<StatusChangeResult> {
  const response = await api.post<{
    changed: number
    skipped: number
    findings: FindingListItem[]
  }>('/findings/batch/delete', { ids })

  invalidateGroups()
  return { changed: response.changed, skipped: response.skipped }
}

/**
 * 从回收站恢复 —— `POST /findings/batch/restore`。
 *
 * ⚠️ 恢复后要**重新拉列表**而不是本地插回去：那条记录在回收站里期间
 * 可能被别处改过（比如被别的标签页指派了），而本地插回的是**删除那一刻**
 * 的旧副本。等服务器然后静默重拉，看到的就是当前真相。
 */
export async function restoreFindings(ids: string[]): Promise<StatusChangeResult> {
  const response = await api.post<{
    changed: number
    skipped: number
    findings: FindingListItem[]
  }>('/findings/batch/restore', { ids })

  await loadFindings({ silent: true })
  invalidateGroups()
  return { changed: response.changed, skipped: response.skipped }
}

/**
 * **永久删除** —— `DELETE /findings/{id}/purge`。
 *
 * ⚠️ 与软删除不同，这个**必须**先确认：它不可逆，而且被报告引用时
 * 服务端会 409 拒绝（那时把服务端那句中文原样透传，它说清了是哪几份报告）。
 *
 * 逐条调用而不是批量端点：契约里 purge 就是单条（与 `purge_scan` 一致）——
 * 用户在回收站里是一次处理一条的，而"一次永久删 200 条"不该是一个
 * 顺手能点的操作。
 */
export async function purgeFinding(id: string): Promise<void> {
  await api.del<void>(`/findings/${id}/purge`)
  findings = findings.filter((finding) => finding.id !== id)
  commit()
  invalidateGroups()
}

// ─────────────────────────────────────────────
// 附件（任务 10 P6-d）
// ─────────────────────────────────────────────

/** 与后端 `AttachmentOut` 逐字对应。**不含 `storedPath`** —— 那是服务端内部的事。 */
export interface FindingAttachment {
  id: string
  findingId: string
  filename: string
  contentType: string
  sizeBytes: number
  uploadedAt: string
  uploadedBy: string | null
}

export type AttachmentsStatus = 'idle' | 'loading' | 'ready' | 'error'

interface AttachmentsState {
  status: AttachmentsStatus
  items: FindingAttachment[]
  error: ApiError | null
}

const EMPTY_ATTACHMENTS: FindingAttachment[] = []

let attachmentsByFinding: Record<string, AttachmentsState> = {}
let attachmentsSnapshot: Record<string, AttachmentsState> = {}
const attachmentListeners = new Set<() => void>()

function commitAttachments() {
  attachmentsSnapshot = attachmentsByFinding
  for (const listener of attachmentListeners) listener()
}

export function subscribeAttachments(listener: () => void): () => void {
  attachmentListeners.add(listener)
  return () => attachmentListeners.delete(listener)
}

function getAttachmentsSnapshot() {
  return attachmentsSnapshot
}

/**
 * 「还没有这个 finding 的记录」这个状态。
 *
 * ★ **必须是模块级的同一个对象**，不能在使用处现写一个对象字面量。
 *
 * ⚠️ 这一条是实测抓出来的：第一版写成
 * `snapshot[findingId] ?? { status: 'idle', ... }` —— 也就是每次渲染
 * 都返回一个**新对象**。而 `useSyncExternalStore` 判定"快照变了"
 * 靠的是 `Object.is`，于是它在每次渲染后都认为值变了、再渲染一次，
 * 无限循环直到 React 抛 "Maximum update depth exceeded" ——
 * **整个应用白屏**（不是抽屉坏了，是 `#root` 被卸载成空的）。
 *
 * 症状极具误导性：它只在"某条漏洞还没有附件记录"时发生，
 * 而那恰恰是绝大多数漏洞的状态 —— 打开详情就白屏。
 *
 * 修法就是把它提成常量：同一个引用，`Object.is` 恒定相等。
 * 文件头那句"快照必须引用稳定"讲的就是这件事。
 */
const IDLE_ATTACHMENTS: AttachmentsState = {
  status: 'idle',
  items: EMPTY_ATTACHMENTS,
  error: null,
}

export function useFindingAttachments(findingId: string | null): AttachmentsState {
  const snapshot = useSyncExternalStore(
    subscribeAttachments,
    getAttachmentsSnapshot,
    getAttachmentsSnapshot,
  )
  if (!findingId) return IDLE_ATTACHMENTS
  return snapshot[findingId] ?? IDLE_ATTACHMENTS
}

/**
 * 拉一条漏洞的附件 —— `GET /findings/{id}/attachments`。
 *
 * `force` 用于上传/删除之后：那时本地状态已知是旧的。
 * 而打开抽屉时不必 force —— 附件很少变，多拉一次只是浪费一个往返。
 */
export async function loadFindingAttachments(
  findingId: string,
  options: { force?: boolean } = {},
): Promise<void> {
  const current = attachmentsByFinding[findingId]
  if (!options.force && current?.status === 'ready') return

  attachmentsByFinding = {
    ...attachmentsByFinding,
    [findingId]: { status: 'loading', items: current?.items ?? EMPTY_ATTACHMENTS, error: null },
  }
  commitAttachments()

  try {
    const items = await api.get<FindingAttachment[]>(`/findings/${findingId}/attachments`)
    attachmentsByFinding = {
      ...attachmentsByFinding,
      [findingId]: { status: 'ready', items, error: null },
    }
  } catch (error) {
    attachmentsByFinding = {
      ...attachmentsByFinding,
      [findingId]: {
        status: 'error',
        items: EMPTY_ATTACHMENTS,
        error: toApiError(error),
      },
    }
  }
  commitAttachments()
}

/**
 * 上传附件 —— `POST /findings/{id}/attachments`（**multipart**）。
 *
 * ⚠️ **不要手写 `Content-Type`**。设成 `multipart/form-data` 而不带
 * `boundary` 会让服务端解析失败；把它整个交给浏览器（`FormData`）
 * 才会带上正确的 boundary。
 *
 * ⚠️ 这里**不能**用 `api.post`（它默认按 JSON 发送）—— 走 `api.upload`，
 * 那条路径不加 JSON 头也不做 JSON 序列化。
 */
export async function uploadFindingAttachments(
  findingId: string,
  files: File[],
): Promise<FindingAttachment[]> {
  const form = new FormData()
  // 字段名 `files` 必须与后端 `File(...)` 参数名一致（契约 §3.15）
  for (const file of files) form.append('files', file)

  const created = await api.upload<FindingAttachment[]>(
    `/findings/${findingId}/attachments`,
    form,
  )
  await loadFindingAttachments(findingId, { force: true })
  return created
}

/** 删除一个附件 —— `DELETE /findings/{id}/attachments/{aid}`。 */
export async function deleteFindingAttachment(
  findingId: string,
  attachmentId: string,
): Promise<void> {
  await api.del<void>(`/findings/${findingId}/attachments/${attachmentId}`)
  await loadFindingAttachments(findingId, { force: true })
}

/**
 * 附件的下载地址。
 *
 * ⚠️ 它**不是** `api.get` —— 下载要走浏览器自己的导航/下载机制
 * （`Content-Disposition: attachment` 由服务端给），而不是 XHR。
 * 所以这里只拼一个 URL 交给 `<a download>` 或 `window.open`。
 *
 * 用相对路径（`/api/v1/...`）：前端与 API 同源时它能命中 Vite 代理，
 * 跨源时也要走同一个基址 —— 硬编码绝对地址会在换部署端口时坏掉。
 */
export function attachmentDownloadUrl(findingId: string, attachmentId: string): string {
  return `${API_PREFIX}/findings/${findingId}/attachments/${attachmentId}/content`
}

// ─────────────────────────────────────────────
// CSV 导出（任务 10 P6-c）
// ─────────────────────────────────────────────

/**
 * 把界面筛选器编译成 `GET /findings/export` 的查询参数（任务 10 P6-c）。
 *
 * ## ★ 它吃的是 `FindingFilter` 本身，不是一堆散参数
 *
 * 这是刻意的：导出**必须表达与列表完全相同的筛选**，否则用户筛出
 * 3 条高危、点导出拿到全部 200 条 —— 而他会把那份表发出去。
 * **错误的数据比没有数据更糟。**
 *
 * 让这个函数直接收 `FindingFilter`，就没人能"顺手少传一个维度"：
 * 将来筛选器加了维度，类型检查会在这里报错。
 *
 * ## 多选怎么表达
 *
 * 界面的 `severities` / `statuses` / `categories` / `assignees` /
 * `slaStates` 都是**多选 Set**，而查询参数用**逗号分隔**表达多值
 * （契约 §3.4，v0.18 给后端加的多值支持）。
 *
 * ⚠️ 负责人那一维有个坑：集合里可能同时有真实 id 和哨兵值
 * `UNASSIGNED`。后端的 `assigneeId` 与 `unassigned` 是**两个参数**
 * （"这个人名下的" vs "没人认领的"），逗号拼在一起是不合法的。
 * 这一维没法用逗号表达"或"，所以只在**恰好选了一个**时才传 ——
 * 选了多个时不传这一维，并让调用方去提示（见 `hasUnexpressableFilter`）。
 * 静默丢弃是最坏的选择：那会让导出的范围比用户看到的**大**。
 */
export interface FindingsExportQuery {
  severity?: string
  status?: string
  category?: string
  q?: string
  scanId?: string
  slaState?: string
  /** 只看未指派 */
  unassigned?: boolean
  /** 只看某一个人名下的 */
  assigneeId?: string
  deleted?: boolean
}

export function findingsExportUrl(params: FindingsExportQuery): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '' || value === false) continue
    query.set(key, String(value))
  }
  const suffix = query.toString()
  return `${API_PREFIX}/findings/export${suffix ? `?${suffix}` : ''}`
}

// ─────────────────────────────────────────────
// 分组视图（任务 10 P4）
// ─────────────────────────────────────────────

/**
 * 按漏洞类型分组的一组。
 *
 * 与后端 `FindingGroupOut` 逐字对应。`findingIds` 是关键字段 ——
 * 展开这一组时用它去筛列表，或对整组做批量操作，**不必再发请求**
 * （按标题重查会多带出同名的其他条目）。
 */
export interface FindingGroup {
  title: string
  /** 组内最高严重度 */
  severity: Severity
  category: FindingCategory
  instanceCount: number
  /** 影响面：分组视图存在的理由 */
  targetCount: number
  targets: string[]
  findingIds: string[]
  byStatus: Record<string, number>
}

export type GroupsStatus = 'idle' | 'loading' | 'ready' | 'error'

let groups: FindingGroup[] = []
let groupsStatus: GroupsStatus = 'idle'
let groupsError: ApiError | null = null
let groupsSnapshot: { groups: FindingGroup[]; status: GroupsStatus; error: ApiError | null } = {
  groups,
  status: groupsStatus,
  error: groupsError,
}

function commitGroups() {
  groupsSnapshot = { groups, status: groupsStatus, error: groupsError }
  for (const listener of listeners) listener()
}

export function getGroupsSnapshot() {
  return groupsSnapshot
}

/**
 * 拉分组。
 *
 * **按需加载**：只在用户切到分组视图时调。默认列表视图不需要它，
 * 而它是一次全量聚合 —— 每次进页面都拉等于让"最需要看全貌"的那个视图
 * 成为最慢的那个。
 */
export async function loadFindingGroups(options: { force?: boolean } = {}): Promise<void> {
  if (!options.force && groupsStatus === 'ready') return
  groupsStatus = 'loading'
  groupsError = null
  commitGroups()
  try {
    groups = await api.get<FindingGroup[]>('/findings/groups')
    groupsStatus = 'ready'
    groupsError = null
  } catch (err) {
    groupsStatus = 'error'
    groupsError = toApiError(err)
  }
  commitGroups()
}

/**
 * 让分组失效 —— 状态流转/指派之后调用。
 *
 * 不在这里重拉：用户可能正在列表视图里连做十次操作，每次重拉分组
 * 就是十次没人看的全量聚合。标脏、等他真的切过去时再拉。
 */
export function invalidateGroups(): void {
  if (groupsStatus === 'idle') return
  groupsStatus = 'idle'
  commitGroups()
}

export function useFindingGroups() {
  return useSyncExternalStore(subscribeFindings, getGroupsSnapshot, getGroupsSnapshot)
}

/** 仅供测试与开发：清空 store（**不再复位到 mock**） */
export function resetFindings(): void {
  findings = []
  dataStatus = 'loading'
  loadError = null
  detailStates = {}
  detailSnapshot = {}
  // 分组也要清 —— 它和列表读的是同一批数据，留着旧分组会让
  // 视图切换后看到"列表是新的、分组是旧的"这种自相矛盾
  groups = []
  groupsStatus = 'idle'
  groupsError = null
  commit()
  commitGroups()
  emitDetail()
}

// ─────────────────────────────────────────────
// 订阅 hook
// ─────────────────────────────────────────────

/**
 * 订阅漏洞列表快照（含状态与统计）。
 *
 * 返回的对象在 store 未变更时**引用稳定**，所以可以直接用作 useMemo 的依赖 ——
 * 这是 getSnapshot 必须返回同一引用的原因（见文件头说明）。
 */
export function useFindingsState(): FindingsSnapshot {
  return useSyncExternalStore(
    subscribeFindings,
    getFindingsSnapshot,
    getFindingsServerSnapshot,
  )
}

/**
 * 只要列表本身。
 *
 * 保留这个 hook 是因为两个调用方（Reports 的生成入参、Overview 的图表）
 * 只关心数据，不关心加载态 —— 让它们各自解构一次反而更长。
 */
export function useFindings(): Finding[] {
  return useFindingsState().findings
}
