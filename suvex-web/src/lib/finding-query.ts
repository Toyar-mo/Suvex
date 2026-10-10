import { SEVERITY_ORDER } from '@/data/findings'
import type { Finding, FindingStatus } from '@/data/finding'

/**
 * Findings 列表的筛选与排序 —— 纯函数，放 lib 便于独立验证。
 * （放组件文件里会让 Fast Refresh 失效，也会和渲染逻辑混在一起。）
 *
 * ## 任务 7 之后这里为什么**没有**计数函数
 *
 * 原来有 `countBySeverity` / `countByStatus` / `countByCategory`，
 * 页面用它们在本地数一遍来填筛选 chip 的角标。
 *
 * 现在角标读 `GET /findings/stats`（契约 §3.4.2 指定的「唯一数据源」）——
 * 本地数一遍与后端数一遍在并发编辑下会得到不同快照，
 * 而**差值看起来像 bug**：用户会以为其中一个是错的。
 * 所以那两个函数删了，只留 `countBySeverity` 给报告快照用
 * （报告一旦生成就是冻结的历史，本来就该按当时的条目数算，见 report-build.ts）。
 */

export type FindingSortKey = 'severity' | 'recent' | 'status'

export const FINDING_SORT_LABEL: Record<FindingSortKey, string> = {
  severity: '按严重度',
  recent: '按发现时间',
  status: '按处置状态',
}

/**
 * 「未指派」在筛选集里的哨兵值（任务 10 P2）。
 *
 * ★ 用一个显式的常量，而不是空串 `''`。
 *
 * 空串看起来更省事，但它和"这个 Set 里有个空值"无法区分 ——
 * 而 `assigneeId` 在别处（后端参数、分组接口）是**真的可以为 null** 的。
 * 用空串会让"未指派"这个筛选值和"某个人恰好 id 是空"长得一模一样，
 * 将来任何一处把 null 规整成空串的改动都会静默改变筛选语义。
 *
 * 加了 `unassigned:` 前缀的字符串不可能与真实 id 冲突
 * （后端 id 是 `member_` 开头的 ULID）。
 */
export const UNASSIGNED = 'unassigned:__none__'

export interface FindingFilter {
  severities: Set<string>
  statuses: Set<string>
  categories: Set<string>
  /**
   * 负责人筛选（任务 10 P2）。
   *
   * 里面装的是 `member.id`，外加一个特殊值 `UNASSIGNED` 表示"还没人认领"
   * —— 它是同一个维度的另一个取值（"还没指派"），不是另一个维度。
   * 因此它和真实的人可以同时被勾选（"张三的 + 没人管的"一起看），
   * 那是合理的用法。
   */
  assignees: Set<string>
  /**
   * 修复时限筛选（任务 10 P3）。
   *
   * 装的是 `SlaState` 的取值（`overdue` / `dueSoon` / `onTrack`）。
   * ★ 判定的是**后端给的那个字段**，不是前端拿 `dueAt` 现算 ——
   * 现算会让"筛出来的"和"列表上标的"在边界上不一致（同一个判断
   * 做了两遍，两遍的"现在"差几毫秒）。
   */
  slaStates: Set<string>
  /** 标题 / 目标 / 受影响位置的关键字 */
  query: string
}

/** 空筛选器的工厂 —— 别在组件里手写四个 new Set() */
export function emptyFilter(): FindingFilter {
  return {
    severities: new Set(),
    statuses: new Set(),
    categories: new Set(),
    assignees: new Set(),
    slaStates: new Set(),
    query: '',
  }
}

/** 筛选器是否处于「有实际约束」的状态 —— 决定要不要显示"清除筛选" */
export function isFilterActive(filter: FindingFilter): boolean {
  return (
    filter.severities.size > 0 ||
    filter.statuses.size > 0 ||
    filter.categories.size > 0 ||
    filter.assignees.size > 0 ||
    filter.slaStates.size > 0 ||
    filter.query.trim().length > 0
  )
}

/**
 * 排序。
 *
 * 「按严重度」用 SEVERITY_ORDER 的位置作序（全站统一顺序）。
 * 同级别时用发现时间倒序作次级排序 —— 没有次级排序的话，
 * 同级内的相对顺序不稳定，列表看起来会"自己乱动"。
 */
export function sortFindings(
  findings: Finding[],
  key: FindingSortKey,
  dir: 'asc' | 'desc',
): Finding[] {
  const severityRank = (f: Finding) => SEVERITY_ORDER.indexOf(f.severity)

  /**
   * 处置状态的排序权重：**需要人动手的排前面。**
   * 「待确认」是唯一需要行动的状态，所以它最前；
   * 「误报」已经处理完且没有价值，放最后。
   * 这个顺序是工作流顺序，不是字母序也不是枚举声明序。
   */
  const statusRank = (status: FindingStatus): number => {
    switch (status) {
      case 'new':
        return 0
      case 'confirmed':
        return 1
      case 'fixed':
        return 2
      // 新状态插在中间而不是追加在末尾（任务 10）：
      // 复测中 = 事情还在手上，只是不需要"再修一次"；已关闭 = 彻底结束。
      // 两者都排在「误报」之前 —— 误报是"这条不算数"，价值最低。
      case 'retesting':
        return 3
      case 'closed':
        return 4
      case 'false_positive':
        return 5
    }
  }

  const compare = (a: Finding, b: Finding): number => {
    switch (key) {
      case 'severity': {
        const diff = severityRank(a) - severityRank(b)
        return diff !== 0 ? diff : b.discoveredAt.localeCompare(a.discoveredAt)
      }
      case 'recent':
        return b.discoveredAt.localeCompare(a.discoveredAt)
      case 'status': {
        const diff = statusRank(a.status) - statusRank(b.status)
        // 同级时用严重度作次级 —— 同为「待确认」时，先看严重的
        return diff !== 0 ? diff : severityRank(a) - severityRank(b)
      }
      default:
        return 0
    }
  }

  const sorted = [...findings].sort(compare)
  return dir === 'asc' ? sorted.reverse() : sorted
}

/**
 * 筛选。四个条件是 AND 关系；空集合表示"不限制"。
 *
 * query 覆盖标题与目标 —— 用户找一条漏洞时，记得的可能是
 * "那个 SQL 注入"或"api.acme.internal"。
 *
 * ⚠️ 原来还搜 `detail.affected`（受影响位置）。任务 7 之后搜不到了：
 *    `GET /findings` 返回的是**摘要**，不含 `detail`（契约 §3.4 ——
 *    列表可能几百条，带完整证据文本会让响应到几 MB，而列表不渲染它）。
 *    这里保留那一段是**防御性**的：将来若有调用方传入带 detail 的对象
 *    （比如从报告快照里筛），行为不变；真实列表数据下它恒为空数组，
 *    等价于不参与匹配。
 */
export function filterFindings(findings: Finding[], filter: FindingFilter): Finding[] {
  const query = filter.query.trim().toLowerCase()

  return findings.filter((finding) => {
    if (filter.severities.size > 0 && !filter.severities.has(finding.severity)) return false
    if (filter.statuses.size > 0 && !filter.statuses.has(finding.status)) return false
    if (filter.categories.size > 0 && !filter.categories.has(finding.category)) return false

    /*
      负责人（任务 10 P2）。

      ★ 「未指派」是**同一个维度里的另一个取值**，所以它和真实的人
      一起放在 `assignees` 里判断 —— 不是"先按人筛、再按有没有筛"两步。
      这样"张三的 + 没人管的"可以同时勾选，而那是一个合理的用法
      （"这几条该我管，那几条还没人管，我一起看看"）。
    */
    if (filter.assignees.size > 0) {
      const key = finding.assigneeId ?? UNASSIGNED
      if (!filter.assignees.has(key)) return false
    }

    if (filter.slaStates.size > 0 && !filter.slaStates.has(finding.slaState)) return false

    if (query) {
      const haystack = [finding.title, finding.target, ...finding.detail.affected]
        .join(' ')
        .toLowerCase()
      if (!haystack.includes(query)) return false
    }

    return true
  })
}

/** 统计各严重度数量。**仅报告快照用** —— 页面的角标读 `/findings/stats` */
export function countBySeverity(findings: Finding[]): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const finding of findings) {
    counts[finding.severity] = (counts[finding.severity] ?? 0) + 1
  }
  return counts
}

/**
 * 筛选角标（任务 10 P6-b「统计随筛选联动」）。
 *
 * ## 这是 facet 计数，不是"当前结果的计数"
 *
 * 算某一个维度时，**忽略那一维度自己的筛选条件**，其余条件照旧生效。
 *
 * 为什么必须这样：用户勾了「高危」，想改看「中危」时，如果中危角标是 0
 * 且按钮被禁用，他**根本切不过去** —— 必须先取消高危。这不是"数字准不准"
 * 的问题，而是筛选器还能不能用的问题。
 *
 * 而其余条件（搜索词、负责人、SLA）必须生效：搜索"登录"时角标就该只统计
 * 命中的那些，否则角标会指着一批被搜索排除掉的条目，点进去发现数量对不上。
 *
 * ## ★ 为什么在**前端**算，而不是调后端的 `?facets=true`
 *
 * 后端的 `facets` 参数（契约 §3.12.2）算的是同一个东西，但它收的是
 * **单值**参数（`?severity=high`），而这一页的筛选是**多选**
 * （`severities: Set`，可以同时勾「高危 + 中危」）。
 * 单值 API 表达不了"severity ∈ {high, low}"，所以这一页没法把筛选
 * 委托给后端。
 *
 * 从已经加载的列表算还有个实际好处：**零延迟且与表格必然一致** ——
 * 每敲一个搜索字符就发一次请求既慢又会让角标与表格短暂对不上
 * （"表格已经 3 条、角标还是 7"）。
 *
 * ⚠️ 因此这里是**前端唯一**的计数实现。不要再在组件里另写一份
 * `findings.filter(...).length` —— 那就会和它漂移。
 */
export interface FacetCounts {
  bySeverity: Record<string, number>
  byStatus: Record<string, number>
  byCategory: Record<string, number>
  /** 当前筛选下**实际**的条数（用于"共 N 条"这类文案，不是角标） */
  total: number
}

export function facetCounts(findings: Finding[], filter: FindingFilter): FacetCounts {
  /** 忽略 `skip` 那一维之后的筛选器 */
  const without = (skip: keyof FindingFilter): FindingFilter => ({
    ...filter,
    [skip]: skip === 'query' ? '' : new Set<string>(),
  })

  const count = (
    rows: Finding[],
    pick: (finding: Finding) => string,
  ): Record<string, number> => {
    const out: Record<string, number> = {}
    for (const finding of rows) {
      const key = pick(finding)
      out[key] = (out[key] ?? 0) + 1
    }
    return out
  }

  return {
    bySeverity: count(
      filterFindings(findings, without('severities')),
      (finding) => finding.severity,
    ),
    byStatus: count(filterFindings(findings, without('statuses')), (finding) => finding.status),
    byCategory: count(
      filterFindings(findings, without('categories')),
      (finding) => finding.category,
    ),
    // total 用**全部**条件 —— 它就是"筛出来几条"
    total: filterFindings(findings, filter).length,
  }
}

/**
 * 把界面筛选器编译成导出的查询参数（任务 10 P6-c）。
 *
 * ★ 与 `findingsExportUrl` 配合，保证**导出表达的就是用户看到的筛选**。
 * 两者缺一个都会出问题：只给 URL 不给映射，每个调用点就会自己拼一遍
 * （迟早漏一个维度）；只给映射不给 URL 同理。
 *
 * ## ⚠️ 负责人那一维表达不了"多选"

 * 后端的 `assigneeId` 与 `unassigned` 是**两个参数**，不是同一个参数的
 * 两个取值，所以逗号分隔表达不了"张三的 **或** 没人管的"。
 *
 * 因此这一维只在**恰好选中一个**时才传；选了多个时：
 *   · 不传这一维；
 *   · 并由 `unexpressableDimensions` 告诉调用方"这一维没传进去"。
 *
 * ★ **绝不静默丢弃**。不传这一维意味着导出的范围比用户看到的大 ——
 * 而用户会把那份表发出去。所以调用方必须据此拦住并提示他
 * （见 `FindingsPage` 的导出处理）。
 */
export interface ExportQuery {
  severity?: string
  status?: string
  category?: string
  q?: string
  slaState?: string
  unassigned?: boolean
  assigneeId?: string
}

export interface ExportMapping {
  query: ExportQuery
  /**
   * 因为接口形状而**没能**表达出去的维度（人话，用于提示）。
   * 非空时调用方应当拦住导出，而不是导出一份范围更大的表。
   */
  unexpressableDimensions: string[]
}

/** 多选 Set → 逗号分隔（空集合得到 `undefined`，即不传） */
function joined(values: Set<string>): string | undefined {
  return values.size > 0 ? [...values].join(',') : undefined
}

export function filterToExportQuery(filter: FindingFilter): ExportMapping {
  const query: ExportQuery = {}
  const blocked: string[] = []

  const severity = joined(filter.severities)
  if (severity) query.severity = severity
  const status = joined(filter.statuses)
  if (status) query.status = status
  const category = joined(filter.categories)
  if (category) query.category = category
  const slaState = joined(filter.slaStates)
  if (slaState) query.slaState = slaState

  const trimmed = filter.query.trim()
  if (trimmed) query.q = trimmed

  /*
    负责人：只有"恰好一个"时才能表达。
      · 单个真实 id  → `assigneeId=<id>`
      · 单个未指派    → `unassigned=true`
      · 多个          → 表达不了，交给调用方提示
  */
  if (filter.assignees.size === 1) {
    const only = [...filter.assignees][0]
    if (only === UNASSIGNED) query.unassigned = true
    else query.assigneeId = only
  } else if (filter.assignees.size > 1) {
    blocked.push('负责人')
  }

  return { query, unexpressableDimensions: blocked }
}
