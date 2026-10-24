import { NAV_ITEMS } from '@/navigation'

/**
 * 命令面板（Cmd/Ctrl+K）的检索逻辑 —— 纯函数，不碰 React、不发请求。
 *
 * ## 这一层为什么单独存在
 *
 * 「全局搜索」最容易写成一件看起来很聪明、用起来很蠢的东西：一个框，
 * 什么都搜，结果按相关度乱排，用户打了三个字发现自己要的东西在第七行，
 * 于是再也不按第二次。命令面板的判据只有一条：
 *
 *   **第一次按 Cmd+K 之后，用户还会按第二次。**
 *
 * 要做到这一点，靠的不是排序算法，而是**可搜的东西少而准**：
 * 只收「能跳过去的地方」和「能立刻做的事」，不收正文、不收描述段落。
 *
 * ## 排序口径（有意固定，不引入"相关度分数"）
 *
 *   1. 类型优先级：动作 → 页面 → 扫描 → 漏洞 → 资产 → 定时计划
 *      （动作和页面是"随时可用"的，数据条目是"刚好有"的）
 *   2. 同一类型内：标题前缀命中 > 标题包含 > 仅副标题包含
 *   3. 仍然相同：按标题长度升序（短的更可能是用户要的），再按 id 字典序
 *      （保证同样输入两次得到同样顺序 —— 列表每次刷新换位置会让人不敢点）
 *
 * 没有"相关度分数"是因为**它没法被验证**：一个 0.87 分和一个 0.83 分
 * 在界面上没有任何区别，却会让测试只能写成"看起来对"。这里每一档
 * 都是一个能断言的布尔事实。
 */

export type CommandKind = 'action' | 'page' | 'scan' | 'finding' | 'asset' | 'schedule'

/**
 * 类型元信息。`order` 直接就是排序第一关键字。
 *
 * 动作排最前：按 Cmd+K 的人十次里有八次是想「做点什么」，
 * 而不是「跳到某个已有记录」。把页面放第一会让面板变成"导航替代品"，
 * 那它就没有存在价值了（侧栏已经在那儿）。
 */
export const COMMAND_KIND_META: Record<CommandKind, { label: string; order: number }> = {
  action: { label: '操作', order: 0 },
  page: { label: '页面', order: 1 },
  scan: { label: '扫描', order: 2 },
  finding: { label: '漏洞', order: 3 },
  asset: { label: '资产', order: 4 },
  schedule: { label: '定时计划', order: 5 },
}

export interface CommandItem {
  /** 全局唯一。用 `<kind>:<id>`，这样不同类型同 id 不会撞 key */
  id: string
  kind: CommandKind
  /** 主文案。用户就是靠它找东西 */
  title: string
  /** 副文案。补充区分度（目标、状态、时间） */
  subtitle: string
  /** 跳转路径 */
  to: string
  /**
   * 参与匹配的原文（小写化之前的原串）。
   * ★ 刻意**不等于** `title + subtitle`：扫描的 `id`、漏洞的分类这类
   *   用户记不住但偶尔会粘进来的东西也要能搜到，但它们不该显示在副标题里
   *   （副标题是给人扫读的，塞进 ID 会让它变成噪音）。
   */
  keywords: string
}

/** 命中强度。数字越小越强。 */
const MATCH_PREFIX = 0
const MATCH_TITLE = 1
const MATCH_KEYWORD = 2
const MATCH_NONE = 99

function matchStrength(item: CommandItem, needle: string): number {
  const title = item.title.toLowerCase()
  if (title.startsWith(needle)) return MATCH_PREFIX
  if (title.includes(needle)) return MATCH_TITLE
  if (item.keywords.toLowerCase().includes(needle)) return MATCH_KEYWORD
  return MATCH_NONE
}

/**
 * 按空白切词，**要求每个词都命中**（AND，不是 OR）。
 *
 * 为什么是 AND：输入 `扫描 acme` 的人想要的是"acme 的扫描"，
 * 不是"所有扫描 + 所有 acme 相关的东西"。OR 会让第二个词变成噪音放大器，
 * 而且结果集随词数**增长** —— 与人的直觉相反（打得越多应该越少）。
 */
function splitNeedles(query: string): string[] {
  return query
    .trim()
    .toLowerCase()
    .split(/\s+/)
    .filter((part) => part.length > 0)
}

/**
 * 在给定候选里检索。
 *
 * @param items 全部候选（调用方负责按当前已加载的数据拼出来）
 * @param query 用户输入
 * @param limit 最多返回多少条（默认 24）
 */
export function searchCommands(
  items: readonly CommandItem[],
  query: string,
  limit = 24,
): CommandItem[] {
  const needles = splitNeedles(query)

  /*
    ★ 空查询返回空数组，**不是**返回全部。

      这不是防御性代码，是一个真 bug 的修复：`needles` 为空时下面的
      `for` 一次都不跑，`hitAll` 保持 `true`，于是**每一条候选都被当成
      命中** —— 面板一打开就列出 200 条，用户要往下翻找。这条在
      `command.test.ts` 的「空查询返回空数组」用例里被抓出来。

      设计上也是对的：空输入时该由组件显示"输入关键字开始搜索"，
      而不是硬塞一个"全部"。
  */
  if (needles.length === 0) return []

  type Scored = { item: CommandItem; kind: number; strength: number }

  const scored: Scored[] = []

  for (const item of items) {
    let worst = MATCH_PREFIX
    let hitAll = true

    for (const needle of needles) {
      const strength = matchStrength(item, needle)
      if (strength === MATCH_NONE) {
        hitAll = false
        break
      }
      // 取最弱的那一次命中：`扫 acme` 里 `acme` 只命中副标题，整体就该按副标题档排
      if (strength > worst) worst = strength
    }

    if (!hitAll) continue
    scored.push({ item, kind: COMMAND_KIND_META[item.kind].order, strength: worst })
  }

  scored.sort((a, b) => {
    if (a.kind !== b.kind) return a.kind - b.kind
    if (a.strength !== b.strength) return a.strength - b.strength
    if (a.item.title.length !== b.item.title.length) {
      return a.item.title.length - b.item.title.length
    }
    return a.item.id < b.item.id ? -1 : a.item.id > b.item.id ? 1 : 0
  })

  return scored.slice(0, limit).map((entry) => entry.item)
}

/** 页面项 —— 由导航表派生，所以永远不会和侧栏漂移。 */
export function pageCommands(): CommandItem[] {
  return NAV_ITEMS.map((item) => ({
    id: `page:${item.id}`,
    kind: 'page' as const,
    title: item.label,
    subtitle: item.blurb,
    to: item.path,
    keywords: `${item.label} ${item.shortLabel ?? ''} ${item.id} ${item.path}`,
  }))
}

/**
 * 动作项。
 *
 * ★ 只收「在任何页面上按 Cmd+K 都成立」的动作。
 *   「导出这份报告」这种依赖当前上下文的动作**不能**放进来 ——
 *   用户在总览页按 Cmd+K 看到它、点了却不知道该导出哪份，比没有更糟。
 *   带上下文的动作应该留在各自页面里（那里有明确的宾语）。
 */
export function actionCommands(): CommandItem[] {
  return [
    {
      id: 'action:new-scan',
      kind: 'action',
      title: '发起新扫描',
      subtitle: '选目标、选技能，跑一次完整的智能体渗透',
      to: '/scans?new=1',
      keywords: '发起 新建 新 扫描 new scan 启动 开始',
    },
    {
      id: 'action:new-schedule',
      kind: 'action',
      title: '新建定时复查',
      subtitle: '排一条计划，到点自动建扫描',
      to: '/schedules?new=1',
      keywords: '定时 复查 计划 周期 schedule cron 自动',
    },
    {
      id: 'action:review-findings',
      kind: 'action',
      title: '处理未关闭的漏洞',
      subtitle: '只看还没处置的，按严重度排',
      to: '/findings?status=open',
      keywords: '漏洞 未关闭 处置 修复 findings open 待办',
    },
    {
      id: 'action:stale-assets',
      kind: 'action',
      title: '查看结论陈旧的资产',
      subtitle: '很久没复查过的目标',
      to: '/assets',
      keywords: '资产 陈旧 过期 没扫 复查 stale assets',
    },
  ]
}

/**
 * 把 store 里的实体转成命令项。
 *
 * 每个转换函数都**只读已有数据**，不触发加载 —— 加载由面板打开时统一发起
 * （见 `CommandPalette`），否则每敲一个字都会打一次接口。
 */

export interface ScanLike {
  id: string
  name: string
  target: string
  status: string
  startedAt: string
}

export function scanCommands(scans: readonly ScanLike[], statusLabel: (s: string) => string): CommandItem[] {
  return scans.map((scan) => ({
    id: `scan:${scan.id}`,
    kind: 'scan' as const,
    title: scan.name || scan.target,
    subtitle: `${statusLabel(scan.status)} · ${scan.target}`,
    to: `/scans/${scan.id}`,
    keywords: `${scan.name} ${scan.target} ${scan.id} ${scan.status}`,
  }))
}

export interface FindingLike {
  id: string
  title: string
  target: string
  severity: string
}

export function findingCommands(
  findings: readonly FindingLike[],
  severityLabel: (s: string) => string,
): CommandItem[] {
  return findings.map((finding) => ({
    id: `finding:${finding.id}`,
    kind: 'finding' as const,
    title: finding.title,
    subtitle: `${severityLabel(finding.severity)} · ${finding.target}`,
    // 漏洞详情是抽屉而不是独立路由，用查询参数表达「打开哪一条」
    to: `/findings?finding=${encodeURIComponent(finding.id)}`,
    keywords: `${finding.title} ${finding.target} ${finding.id} ${finding.severity}`,
  }))
}

export interface AssetLike {
  target: string
  openFindingCount: number
  scanCount: number
}

export function assetCommands(assets: readonly AssetLike[]): CommandItem[] {
  return assets.map((asset) => ({
    id: `asset:${asset.target}`,
    kind: 'asset' as const,
    title: asset.target,
    subtitle:
      asset.openFindingCount > 0
        ? `${asset.openFindingCount} 条未处置 · 扫过 ${asset.scanCount} 次`
        : `无未处置漏洞 · 扫过 ${asset.scanCount} 次`,
    to: `/scans?target=${encodeURIComponent(asset.target)}`,
    keywords: `${asset.target}`,
  }))
}

export interface ScheduleLike {
  id: string
  name: string
  target: string
  enabled: boolean
}

export function scheduleCommands(schedules: readonly ScheduleLike[]): CommandItem[] {
  return schedules.map((schedule) => ({
    id: `schedule:${schedule.id}`,
    kind: 'schedule' as const,
    title: schedule.name,
    subtitle: `${schedule.enabled ? '启用中' : '已停用'} · ${schedule.target}`,
    to: '/schedules',
    keywords: `${schedule.name} ${schedule.target} ${schedule.id}`,
  }))
}

/**
 * 把用户输入原样变成一个「去某页搜索它」的兜底项。
 *
 * ★ 这一条是命令面板**不能省**的：
 *   用户打了一个词、面板里一条都没命中时，如果面板只是空着，
 *   他得到的结论是「系统里没有这个东西」。但真相往往只是
 *   「这一条不在我已加载的 24 条里」。兜底项把这句话说清楚，
 *   并给一个能继续找的出口 —— 空结果和"没找对地方"是两件事。
 */
export function fallbackCommand(query: string): CommandItem | null {
  const trimmed = query.trim()
  if (trimmed.length === 0) return null
  return {
    id: `action:search:${trimmed}`,
    kind: 'action',
    title: `在漏洞页搜索「${trimmed}」`,
    subtitle: '命令面板只搜已加载的条目，这里可以按全文再找一遍',
    to: `/findings?q=${encodeURIComponent(trimmed)}`,
    keywords: trimmed,
  }
}
