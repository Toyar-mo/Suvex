import { ApiError, api, toApiError } from '@/lib/api'
import { useSyncExternalStore } from 'react'

/**
 * 技能库的状态与读写（v0.88）。
 *
 * ## 为什么从 `data/tools.ts` 里拆出来
 *
 * 工具页只是**顺带列一下**技能（它回答"能力边界"，技能是其中一块）；
 * 技能页要管的是**这些文本本身** —— 新建、编辑、删除、看调用统计。
 * 两者的读写面完全不同：
 *
 *   | | 工具页 | 技能页 |
 *   |---|---|---|
 *   | 写 | 只改"关掉哪些" | 写文件（增/改/删正文） |
 *   | 数据 | 13 条固定 + MCP 连接 | 31 篇文本 + 每篇的调用次数 |
 *   | 危险面 | 关错了能力变弱 | 正文直接进模型上下文 |
 *
 * 挤在一个 store 里的话，技能页每存一次都要重新拉一遍 MCP 连接
 * （那是外部进程的试连，慢）；而工具页每次开都会拉一份用不到的统计。
 * 分开之后各自只拉自己需要的。
 *
 * ## ⚠️ 内置技能只读
 *
 * `source === 'builtin'` 的技能随代码分发、部署时整目录覆盖。界面上
 * **不给**编辑/删除入口（不是点了才报错）—— 让用户先看到一个禁用按钮
 * 再告诉他不许，不如一开始就不画那个按钮。后端仍然独立校验一遍。
 */

export type SkillSource = 'builtin' | 'custom'

/** 一篇技能（清单里的一条，不含正文）。 */
export interface SkillSummary {
  name: string
  category: string
  description: string
  source: SkillSource
}

/** `GET /kernel/skills` 的响应（按分类分组）。 */
export interface SkillListSnapshot {
  total: number
  categories: { category: string; skills: SkillSummary[] }[]
}

/** `GET /kernel/skills/{name}` 的响应（自定义的含正文）。 */
export interface SkillDetail {
  name: string
  category: string
  description: string
  source: SkillSource
  body: string
  lineCount: number
}

/** 一篇技能的调用统计。 */
export interface SkillUsage {
  name: string
  loads: number
  lastLoadedAt: string | null
}

/** `GET /kernel/skills/usage` 的响应。 */
export interface SkillUsageSnapshot {
  /**
   * 统计实际扫过的事件条数。
   *
   * ★ 用它判断"这个数是全量还是截断的"：后端有上限（`MAX_USAGE_EVENTS`），
   *   到了上限就只统计了最近那一批。界面必须**说出来**，
   *   否则用户会把"最近 2 万条里被加载 0 次"读成"从来没被加载过"。
   */
  sampledEvents: number
  since: string | null
  items: SkillUsage[]
}

export type SkillsStatus = 'loading' | 'ready' | 'error'

export interface SkillsState {
  list: SkillListSnapshot
  usage: SkillUsageSnapshot
  status: SkillsStatus
  error: ApiError | null
  /**
   * 统计是否因为后端上限而**不是**全量。
   *
   * ★ 单独一个布尔而不是让页面自己比：判断依据（上限常量）在后端，
   *   前端不该有第二份 —— 那会在后端调大上限之后静默失效。
   */
  usageTruncated: boolean
}

const EMPTY_LIST: SkillListSnapshot = { total: 0, categories: [] }
const EMPTY_USAGE: SkillUsageSnapshot = { sampledEvents: 0, since: null, items: [] }

let list: SkillListSnapshot = EMPTY_LIST
let usage: SkillUsageSnapshot = EMPTY_USAGE
let status: SkillsStatus = 'loading'
let loadError: ApiError | null = null
let truncated = false

type Listener = () => void
const listeners = new Set<Listener>()
function emit() {
  for (const listener of listeners) listener()
}

/**
 * 「首次加载是否已经发起过」。
 *
 * ★ 必须与 `status` 分开：`status` 初值就是 `'loading'`，拿它判断
 *   "是不是正在加载"会把**第一次**调用也挡掉，于是永远没人加载。
 *   `data/tools.ts` 里同一个坑记过一次。
 */
let loadStarted = false

/** 快照必须引用稳定 —— `useSyncExternalStore` 每次渲染都比较它。 */
let snapshot: SkillsState = {
  list,
  usage,
  status,
  error: loadError,
  usageTruncated: truncated,
}

function commit() {
  snapshot = { list, usage, status, error: loadError, usageTruncated: truncated }
  emit()
}

function getSnapshot(): SkillsState {
  return snapshot
}

const SERVER_SNAPSHOT: SkillsState = {
  list: EMPTY_LIST,
  usage: EMPTY_USAGE,
  status: 'loading',
  error: null,
  usageTruncated: false,
}

function getServerSnapshot(): SkillsState {
  return SERVER_SNAPSHOT
}

export function subscribeSkills(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** 订阅技能库状态。 */
export function useSkills(): SkillsState {
  return useSyncExternalStore(subscribeSkills, getSnapshot, getServerSnapshot)
}

function messageOf(error: unknown): ApiError {
  return error instanceof ApiError ? error : toApiError(error)
}

// ─────────────────────────────────────────────
// 读取
// ─────────────────────────────────────────────

/**
 * 拉一次完整状态（清单 + 统计）。
 *
 * ## 为什么统计失败**不该**让清单也变空
 *
 * 两者回答不同问题：清单是"有哪些技能"（读文件，一定成功），统计是
 * "哪几篇被用过"（扫事件表，可能超时）。用 `all` 的话统计一慢，
 * 整页就白 —— 而用户只是想知道有哪些技能。
 *
 * 所以统计失败时保留清单、把错误挂在 `error` 上，页面显示一句
 * "统计没拿到"而不是"整页都坏了"。
 */
export async function loadSkills(): Promise<void> {
  loadStarted = true
  status = 'loading'
  loadError = null
  commit()

  const [listResult, usageResult] = await Promise.allSettled([
    api.get<SkillListSnapshot>('/kernel/skills'),
    api.get<SkillUsageSnapshot>('/kernel/skills/usage'),
  ])

  const errors: ApiError[] = []

  if (listResult.status === 'fulfilled') {
    list = listResult.value
  } else {
    errors.push(messageOf(listResult.reason))
  }

  if (usageResult.status === 'fulfilled') {
    usage = usageResult.value
    /*
      截断判定：后端扫到的事件数正好等于它的上限，说明很可能还有更早的
      没扫到。`>=` 而不是 `>`：正好打满时同样是截断的。

      ⚠️ 这个 20000 是**后端的常量**（`skills_router.MAX_USAGE_EVENTS`）。
      复制到这里是因为 HTTP 响应里没有它（只有 `sampledEvents`）。
      如果后端调整了上限，这里也要改 —— 所以它是一处**已知的重复**，
      注释留在两边。将来若要消除，应当让后端把上限也放进响应。
    */
    truncated = usage.sampledEvents >= 20_000
  } else {
    errors.push(messageOf(usageResult.reason))
  }

  if (listResult.status === 'rejected') {
    // ★ 清单都拿不到 = 这一页没有可看的东西。
    status = 'error'
    loadError = errors[0]
  } else {
    status = 'ready'
    loadError = errors[0] ?? null
  }
  commit()
}

/**
 * 首次进入页面时加载（只发一次）。
 *
 * ★ 不放在 store 模块顶层自动执行：那样被打包时的副作用会提前发请求，
 *   而此刻鉴权可能还没完成（`bootstrapAuth` 在 App 里跑），
 *   于是拿一个 401 把页面标成 error。
 */
export function ensureSkillsLoaded(): void {
  if (loadStarted) return
  loadStarted = true
  void loadSkills()
}

/** 测试用：把 store 复位到初始状态。 */
export function resetSkillsStore(): void {
  list = EMPTY_LIST
  usage = EMPTY_USAGE
  status = 'loading'
  loadError = null
  truncated = false
  loadStarted = false
  commit()
}

// ─────────────────────────────────────────────
// 写
// ─────────────────────────────────────────────

export interface SkillDraft {
  name: string
  category: string
  description: string
  body: string
}

/**
 * 新建一篇自定义技能。
 *
 * ★ 成功后**重新拉清单**，不把新技能手工拼进本地状态。
 *   理由：后端会做规范化（trim、目录名小写），本地拼出来的那份
 *   和真实落盘的可能不一致 —— 那种不一致只会在下次刷新时暴露，
 *   而那时用户已经以为建好了。
 */
export async function createSkill(draft: SkillDraft): Promise<SkillDetail> {
  const detail = await api.post<SkillDetail>('/kernel/skills', draft)
  await loadSkills()
  return detail
}

/**
 * 改一篇自定义技能。
 *
 * ⚠️ **分类不可改**（后端也不接受）：它决定文件在哪，改分类等于
 * "删一个建一个"，会让"编辑"产生副作用（原文件消失）。
 */
export async function updateSkill(
  name: string,
  draft: { description: string; body: string },
): Promise<SkillDetail> {
  const detail = await api.put<SkillDetail>(`/kernel/skills/${encodeURIComponent(name)}`, draft)
  await loadSkills()
  return detail
}

/** 删一篇自定义技能。 */
export async function deleteSkill(name: string): Promise<void> {
  await api.del<void>(`/kernel/skills/${encodeURIComponent(name)}`)
  await loadSkills()
}

/** 读一篇技能的正文（编辑回填用）。 */
export async function fetchSkill(name: string): Promise<SkillDetail> {
  return api.get<SkillDetail>(`/kernel/skills/${encodeURIComponent(name)}`)
}

// ─────────────────────────────────────────────
// 纯函数（可单测）
// ─────────────────────────────────────────────

/**
 * 按关键词筛选技能（匹配 name / description / category）。
 *
 * 与工具页那个同名函数的行为一致，但这里独立一份：技能页的分类集合
 * 与工具页不同（这边含自定义建的分类），共用一个会逼两边互相 import。
 *
 * ★ 分类取的是**所属分组名**（`group.category`），不是每条 skill 自带的
 *   `category` 字段。两者按契约永远相同（后端按分类分组后才下发），
 *   但既然是"永远相同"，就只该有一个来源 —— 读两边的话，哪天某条
 *   skill 的 category 与分组名不一致（后端 bug 或手工构造的数据），
 *   "搜分类"与"显示分类"就会给出不同结果，而这种不一致极难复现。
 */
export function filterSkillGroups(
  categories: { category: string; skills: SkillSummary[] }[],
  query: string,
): { category: string; skills: SkillSummary[] }[] {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  if (terms.length === 0) return categories

  return categories
    .map((group) => ({
      category: group.category,
      skills: group.skills.filter((skill) => {
        const haystack =
          `${skill.name} ${group.category} ${skill.description}`.toLowerCase()
        return terms.every((term) => haystack.includes(term))
      }),
    }))
    // 筛选后空分类**丢掉**（与"清单本身有空分类要保留"不同：
    // 那是打包问题，这是筛选的必然结果，留着只会让界面空一片）
    .filter((group) => group.skills.length > 0)
}

/** 把一个技能名对应到它的调用次数（没有记录 = 0 次）。 */
export function loadsOf(usage: SkillUsageSnapshot, name: string): SkillUsage | null {
  return usage.items.find((item) => item.name === name) ?? null
}

/**
 * 统计摘要：多少篇被用过 / 最常用的是哪一篇。
 *
 * ★ 只统计**清单里存在的**技能。统计表里可能留着已经删掉的技能名
 * （历史事件不会因为删技能而消失）—— 把它们算进"用过几篇"会让
 * 分母和分子对不上（分母是现在的技能数）。
 */
export function usageSummary(
  listSnapshot: SkillListSnapshot,
  usageSnapshot: SkillUsageSnapshot,
): { usedCount: number; totalLoads: number; topName: string | null; topLoads: number } {
  const names = new Set<string>()
  for (const group of listSnapshot.categories) {
    for (const skill of group.skills) names.add(skill.name)
  }

  let usedCount = 0
  let totalLoads = 0
  let topName: string | null = null
  let topLoads = 0
  for (const item of usageSnapshot.items) {
    if (!names.has(item.name)) continue
    if (item.loads <= 0) continue
    usedCount += 1
    totalLoads += item.loads
    if (item.loads > topLoads) {
      topLoads = item.loads
      topName = item.name
    }
  }
  return { usedCount, totalLoads, topName, topLoads }
}

/**
 * 校验新建的草稿，返回第一条错误（没有错误返回 null）。
 *
 * ★ 前后端**都**要校验，且规则要一致（见后端 `SkillCreateIn` 的 pattern）。
 *   前端这份不是为了安全（真正的边界在后端与文件层），是为了**不让人
 *   白填一遍表单**才被告知名字里不能有大写。
 */
export function validateDraft(draft: {
  name: string
  category: string
  description: string
  body: string
}): string | null {
  if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(draft.name)) {
    return '技能名只能用小写字母、数字、下划线和连字符，且必须以字母或数字开头（最长 64 字符）。'
  }
  if (!/^[a-z0-9][a-z0-9_-]{0,31}$/.test(draft.category)) {
    return '分类只能用小写字母、数字、下划线和连字符，且必须以字母或数字开头（最长 32 字符）。'
  }
  if (draft.description.length > 2000) {
    return '描述最长 2000 字符。'
  }
  if (!draft.body.trim()) {
    return '正文不能为空 —— 空技能拉下来什么也没有，模型会以为查到了。'
  }
  if (draft.body.length > 200_000) {
    return '正文最长 200000 字符。'
  }
  return null
}
