/**
 * 资产（asset）类型与展示元数据。
 *
 * 字段名与后端 `app/assets/schemas.py` 的 `AssetOut` 逐字一致（camelCase），
 * 接线时不该需要改名。
 *
 * ## 为什么 `lastScanAt` 与 `lastCompletedAt` 是两个字段
 *
 * 这不是"多返回一个字段"，而是两个不同的问题：
 *
 *   · `lastScanAt`      —— 「最近**动过**没有」。连续失败 3 次的目标这里
 *                          是刚才，因为它今天确实被扫了 3 次。
 *   · `lastCompletedAt` —— 「上次真正**拿到结论**是什么时候」。同样的目标
 *                          这里可能是上周，因为它 3 次全挂了。
 *
 * 合成一个字段时，两种口径各有致命错法：取"最近成功"会让天天在扫却
 * 全失败的目标显示"一周没人管"；取"最近任何状态"会让从未成功过的目标
 * 显示"今天扫过"——明明是**从来没被检查过**。后者比前者更危险。
 *
 * 所以两个都给，并配一个 `consecutiveFailures` 让"差多远"能排序。
 */

/** 一个目标的聚合视图。 */
export interface Asset {
  target: string

  // ── 扫描 ──────────────────────────────────────────────
  scanCount: number
  /** 最近一次扫描的**发生时刻**（任何状态）；从未扫过为 null。 */
  lastScanAt: string | null
  /** 最近一次扫描的状态；从未扫过为 null。 */
  lastScanStatus: string | null
  /** 最近一次**成功**（completed）的时刻；从未成功过为 null。 */
  lastCompletedAt: string | null
  /** 从最近一次往前数，连续失败了几次（遇到非失败即停）。 */
  consecutiveFailures: number

  // ── 漏洞 ──────────────────────────────────────────────
  /** 历史全部发现（含已修复 / 误报）。 */
  findingCount: number
  /** **仍然开着**的发现数（排除 fixed / closed / false_positive）。 */
  openFindingCount: number
  openCritical: number
  openHigh: number
  /** 按严重度分档的总数（含已终结的）。 */
  bySeverity: Record<string, number>
}

/** 顶部统计条。 */
export interface AssetSummary {
  targetCount: number
  findingCount: number
  openFindingCount: number
  /**
   * 带着**开着的高危**的资产**个数**（不是漏洞条数）。
   *
   * 「高危漏洞有几条」在漏洞页的 stats，两个数不能互相冒充 ——
   * 一个是"有几台机器现在带病"，一个是"病有几个"。
   */
  targetsWithOpenHigh: number
  /** 只有 findings、一次扫描都没有的目标数（历史导入造成的）。 */
  neverScanned: number
  /**
   * 有扫描但**一次都没成功过**的目标数。
   *
   * 这组目标"看起来干净"只是因为**没被检查过** —— 界面上必须能看见它，
   * 否则用户会把它读成"已确认无问题"。
   */
  neverCompleted: number
  generatedAt: string
}

export interface AssetList {
  items: Asset[]
  total: number
  /** 目标数超过上限时为 true —— 界面必须显示，否则用户会当成"总共就这些"。 */
  truncated: boolean
  summary: AssetSummary
}

/**
 * 一个资产的**健康度判读** —— 列表里那一列"状态"。
 *
 * 为什么需要这个而不直接看扫描状态：
 * 用户真正要判断的是「这台机器现在能不能信」。三个来源都是必要的：
 *
 *   · `never-scanned`     —— 只有历史导入的漏洞，一次没扫过
 *   · `never-completed`   —— 扫过，但没有一次跑完（"看起来干净"是最危险的）
 *   · `failing`           —— 最近连续失败，上次结论可能早就过期
 *   · `stale`             —— 上次成功在很久以前（默认 30 天）
 *   · `ok`                —— 最近有成功结论
 */
export type AssetHealth = 'ok' | 'never-scanned' | 'never-completed' | 'failing' | 'stale'

/** "上次成功"超过这么多天就算陈旧。 */
export const ASSET_STALE_DAYS = 30

const DAY_MS = 24 * 60 * 60 * 1000

/**
 * 判读一个资产的健康度。
 *
 * 判定顺序是**有意的**，不是随便排的 —— 上面的档位一个比一个宽：
 * 依次问"有没有扫过 → 有没有成功过 → 最近是否在失败 → 结论是否过期"，
 * 每一档都只在上一档不成立时才问。反过来排会把
 * "从没成功过"的目标一路落到 `ok`（因为它没有连续失败）。
 */
export function assetHealth(asset: Asset, now: number = Date.now()): AssetHealth {
  if (asset.scanCount === 0 || asset.lastScanAt === null) return 'never-scanned'
  if (asset.lastCompletedAt === null) return 'never-completed'
  if (asset.consecutiveFailures > 0) return 'failing'

  const completed = new Date(asset.lastCompletedAt).getTime()
  if (!Number.isFinite(completed)) return 'ok'
  // `now - completed` 为负（时钟回拨 / 服务端时间略超前）时按不陈旧处理：
  // 那种情况不该被报成"过期 30 天"。
  if (now - completed > ASSET_STALE_DAYS * DAY_MS) return 'stale'

  return 'ok'
}

/** 健康度的展示文案与配色。 */
export const ASSET_HEALTH_META: Record<
  AssetHealth,
  { label: string; hint: string; className: string; dot: string }
> = {
  ok: {
    label: '正常',
    hint: '最近一次扫描跑完了',
    className: 'text-ink-muted',
    dot: 'bg-signal-ok',
  },
  'never-scanned': {
    label: '未扫描',
    hint: '只有历史导入的漏洞，从未真正扫描过这个目标',
    className: 'text-signal-warn',
    dot: 'bg-signal-warn',
  },
  'never-completed': {
    label: '从未跑完',
    hint: '扫过，但没有一次跑到出结论 —— 「看起来干净」只是因为没被检查过',
    className: 'text-signal-risk',
    dot: 'bg-signal-risk',
  },
  failing: {
    label: '连续失败',
    hint: '最近几次扫描都没跑完，上一次的结论可能已经过期',
    className: 'text-signal-risk',
    dot: 'bg-signal-risk',
  },
  stale: {
    label: '结论陈旧',
    hint: `上次跑完已超过 ${ASSET_STALE_DAYS} 天`,
    className: 'text-signal-warn',
    dot: 'bg-signal-warn',
  },
}

/** 搜索框用它做大小写不敏感的子串匹配。 */
export function assetMatchesQuery(asset: Asset, query: string): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return asset.target.toLowerCase().includes(q)
}
