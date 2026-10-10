import type { Severity } from './findings'

/**
 * 报告（Report）领域模型。
 *
 * ⚠️ 这个文件和界面上的其它页面有一处根本差异：**报告的载体是纸，不是屏幕。**
 *
 *   站内所有页面都建立在近黑底（canvas #08090C）上，signal 色是按深色底配的。
 *   而报告要交付出去、可能被打印、被贴进邮件、被钉在会议投屏上 —— 它是白底黑字。
 *   实测过：现有 signal 色直接印在白纸上有 4/5 不达标（中危 #E8B454 只有 1.89:1，
 *   基本等于没有颜色）。所以报告**另立一套印刷色阶**，见 PRINT_SEVERITY。
 *
 *   这不是"两套配色不一致"的设计事故，而是两个介质各配一套 ——
 *   和深色模式/浅色模式是同一类问题，只是这里的分界是"屏幕/纸"。
 */

// ─────────────────────────────────────────────
// 数据范围
// ─────────────────────────────────────────────

/**
 * 报告要覆盖哪一批发现。
 *
 * 用 discriminated union 而不是"三个可选字段"：
 * 后者允许出现 (scanId 和 target 同时有值) 这种非法组合，
 * 到时候"到底按哪个过滤"要靠读实现猜。
 */
export type ReportScope =
  | { kind: 'scan'; scanId: string }
  | { kind: 'target'; target: string }
  | { kind: 'range'; from: string; to: string }
  /**
   * 手选的那几条（任务 10 P5）。
   *
   * ★ 这一支打通的是"漏洞发现"与"报告"两个模块：用户在漏洞页勾了 5 条，
   * 点「生成报告」直接带着它们跳过来，而不是让他去报告页重新勾一遍 ——
   * 重新勾意味着他要记住刚才选了什么，而那是产品在把自己的活儿推给用户。
   */
  | { kind: 'findings'; findingIds: string[] }

export type ReportScopeKind = ReportScope['kind']

export const REPORT_SCOPE_LABEL: Record<ReportScopeKind, string> = {
  scan: '某次扫描',
  target: '某个目标',
  range: '时间区间',
  findings: '手选的漏洞',
}

// ─────────────────────────────────────────────
// 内容选项
// ─────────────────────────────────────────────

/**
 * 报告里包含什么。
 *
 * 三个开关都是**默认关**（除了"只含已确认"默认关）——
 * 报告是给人看的，默认应该是"干净可交付"，而不是"把所有原始材料倒进去"。
 * 尤其是 agent 推理过程：它内部是给工程师看的，混进管理层报告会显得杂乱。
 */
export interface ReportOptions {
  /** 包含 PoC 代码细节。关掉后只保留"已通过 PoC 验证"这一结论 */
  includePoc: boolean
  /** 包含 agent 推理过程。关掉后只保留结论与影响 */
  includeReasoning: boolean
  /** 只导出已确认的漏洞（排除待确认与误报） */
  onlyConfirmed: boolean
  /** 包含修复建议 */
  includeRemediation: boolean
}

export const DEFAULT_REPORT_OPTIONS: ReportOptions = {
  includePoc: false,
  includeReasoning: false,
  onlyConfirmed: false,
  includeRemediation: true,
}

// ─────────────────────────────────────────────
// 报告对象
// ─────────────────────────────────────────────

export type ReportFormat = 'pdf' | 'markdown'

export interface Report {
  /** 主键。标识符，不翻译 */
  id: string
  /**
   * 报告编号 RPT-20260924-CAP8。**交付物的身份** —— 客户会拿它引用、对账
   * （"你们那份 RPT-…-CAP8 里说的"）。
   *
   * ★ 任务 7 之前它是前端从 `id` 派生出来的（见本文件原先的 `reportCode`）。
   *   后端也有自己的一套编号，两套算法**必然分叉**：同一条报告，
   *   前端算出 `RPT-20260924-KZE`，后端存的是 `RPT-20260924-CAP8`。
   *   而编号一旦有两个来源，就一定会出现"预览里一个号、下载文件里另一个号"。
   *
   *   现在编号**只由后端分配并下发**，前端原样显示、原样用作文件名。
   *   前端不再有任何编号算法。
   */
  code: string
  /** 用户可读的报告名 */
  title: string
  scope: ReportScope
  options: ReportOptions
  /** ISO 8601 */
  createdAt: string
  /** 生成时命中的漏洞数 —— 历史列表直接显示，不必重算 */
  findingCount: number
  /**
   * 生成时**被选项排除掉的**条数（命中数 − 收录数）。
   *
   * ★ 这个数字必须随报告一起冻结，不能等复现时重算。
   *
   *   踩到的场景：一份"只含已确认"的报告，范围内 2 条发现全是
   *   待确认与误报，于是收录 0 条。复现时如果按冻结的 findingIds
   *   （空数组）重算"排除了几条"，得到的当然是 0 ——
   *   报告于是会写着"本次范围内未发现问题"，**而真相是 2 条被筛掉了**。
   *   那不是"没问题"，那是"没看"。
   *
   *   一个诚实的安全报告，在给出"未发现"这个结论时，
   *   有义务同时说明有多少内容没被纳入。
   */
  excludedCount: number
  /** 生成时的严重度分布快照 */
  severityCounts: Partial<Record<Severity, number>>
  /**
   * 生成时使用的格式。
   *
   * 记录它是为了让"重新下载"能用**当时**的格式，
   * 而不是用当前界面上的选择 —— 那会让人以为下载的是别的东西。
   */
  format: ReportFormat
  /**
   * 报告内容在生成时**冻结**。
   *
   * 为什么冻结而不是每次读实时数据：报告是一份**交付物**，
   * 它必须和当时发出去的那份一致。如果历史报告随漏洞状态实时变化，
   * 三个月后回看会看到一份"和当初不一样"的报告 —— 那在合规场景里是灾难。
   * 这和快照/实时视图的区别是同一个道理。
   */
  findingIds: string[]
}

// ─────────────────────────────────────────────
// 印刷色阶
// ─────────────────────────────────────────────

export interface PrintSeverityMeta {
  /** 实色 hex，用于色块、表头、图表条 */
  ink: string
  /** 浅色衬底，用于徽标底色（配 ink 作文字色） */
  wash: string
}

/**
 * 报告专用严重度配色 —— **白底实测达标**，与站内 signal 色是两套。
 *
 * 解这组值的约束：
 *   1. 每个色对白底 >= 4.5:1（正文级）。实测值写在下面，别凭感觉调。
 *   2. 保持和站内相同的色相家族（红/橙红/赭/灰/蓝），
 *      这样同一份数据在屏幕和纸上"看起来还是那个颜色"。
 *   3. 严重度本来就带文字标签，颜色是**冗余编码**不是唯一载体 ——
 *      色觉障碍读者靠标签也能读，这是刻意保留的安全网。
 *
 * 为什么不能直接复用 signal 色：那些是按近黑底配的，
 * 印到白纸上中危只剩 1.89:1，等于没有。
 */
export const PRINT_SEVERITY: Record<Severity, PrintSeverityMeta> = {
  // 6.54:1
  critical: { ink: '#B3261E', wash: '#FBEAE8' },
  // 5.22:1 —— 橙红，与 critical 的血红在色相上分开
  high: { ink: '#B34E1A', wash: '#FCEFE6' },
  // 6.65:1 —— 压暗的赭黄（原 #E8B454 只有 1.89:1）
  medium: { ink: '#7A5600', wash: '#FBF3E0' },
  // 6.70:1 —— 中性灰，和站内 low 用中性色是同一个决定
  low: { ink: '#565C6A', wash: '#F0F1F4' },
  // 7.48:1
  info: { ink: '#1A5695', wash: '#E8F0FA' },
}

/**
 * 纸张灰阶。报告正文/次要/辅助三级，外加分隔线。
 * 全部按白底实测，值写在注释里。
 */
export const PRINT_INK = {
  /** 正文与标题 17.8:1 */
  body: '#16181D',
  /** 次要文字（日期、元信息） 6.3:1 */
  muted: '#5A6070',
  /** 分隔线。不是文字，不受 4.5 约束 */
  rule: '#D9DDE3',
  /** 浅衬底（表头、代码块底） */
  wash: '#F5F6F8',
} as const

/**
 * 印刷版语法高亮色板 —— **与站内 syntax 色板是两套，理由同 PRINT_SEVERITY。**
 *
 * 站内那套是按近黑底（surface-sunken #0A0B0F）解出来的：
 * 关键字 #A99BFF 在白纸上只有 1.9:1，字符串 #4ADE9B 更低。
 * 直接搬到纸上，PoC 会变成一片读不清的浅色 —— 那正是"报告给人看"最忌讳的。
 *
 * 解这组值的约束（与站内同源，只是换了底）：
 *   1. 底色取代码块的浅衬底 #F5F6F8，每个 token ≥ 4.5:1（正文级）；
 *   2. **保持与站内相同的色相家族**，让同一段代码在屏幕和纸上"看起来还是那个颜色"；
 *   3. 共现频繁且语义易混的对要拉开**明度**：number↔string 1.44、
 *      function↔property 1.42、keyword↔variable 1.31。
 *
 * ⚠️ 这组值是**搜出来的，不是挑出来的**，别凭手感改：
 *    第一版我按直觉填了一组并在注释里断言"number 与 string 在明度上分开"，
 *    实测明度比是 **1.00** —— 两者根本没分。property 甚至只有 3.99:1。
 *    复现脚本：_tmp/solve-print-syntax.mjs（在色相/饱和度固定下，
 *    搜出落在目标对比度区间内的明度，再验算易混对）。
 *
 *    纸面和站内有一个本质差异：站内是浅字深底，纸面是深字浅底。
 *    白底上可用的对比度区间只有约 4.5~16:1，比暗底窄得多，
 *    所以"拉开明度"的余量很小，只能靠"一个贴下限、一个压深"成对分离 ——
 *    这也是为什么 number/property 刻意贴近 4.5 这条线。
 *
 * 它是独立色板而不是复用 PRINT_SEVERITY 的原因：严重度是**状态语义**
 * （红=严重），语法是**文本分词**，两者目标不同，混用会让同一个红色
 * 在两处表示不同东西 —— 这条理由站内已经写过一次，纸上同样成立。
 */
export const PRINT_SYNTAX: Record<string, string> = {
  // 15.25:1
  plain: '#1C1F26',
  // 5.02:1
  comment: '#636A79',
  // 5.31:1 —— violet 蓝紫
  keyword: '#664FD6',
  // 6.72:1 —— 深绿，压深以与 number 分离
  string: '#17633D',
  // 4.65:1 —— 赭棕，贴近下限
  number: '#98641B',
  // 6.66:1 —— 中蓝，压深以与 property 分离
  function: '#1757A1',
  // 4.71:1 —— 浅蓝，贴近下限
  property: '#206EC8',
  // 6.98:1 —— 紫品红
  variable: '#8328A1',
  // 5.49:1 —— 运算符中性灰蓝
  operator: '#5C6475',
  // meta 与注释同级
  meta: '#636A79',
}

// ─────────────────────────────────────────────
// 范围的人话描述
// ─────────────────────────────────────────────

/**
 * 把 scope 渲染成一句人话，给预览页和历史列表共用。
 *
 * 传入已解析好的显示名（扫描名 / 目标名），而不是在这里查数据 ——
 * 这个模块是纯数据层，不该依赖 store。
 */
export function describeScope(scope: ReportScope, resolve: { scanName?: string } = {}): string {
  switch (scope.kind) {
    case 'scan':
      return `扫描任务「${resolve.scanName ?? scope.scanId}」`
    case 'findings':
      // 这一支没有"名字"可解析 —— 它是用户手选的一批，所以直接报条数。
      // 报 id 列表读起来像一串乱码，而条数正好回答"这份报告覆盖了多少"。
      return `手选的 ${scope.findingIds.length} 条漏洞`
    case 'target':
      return `目标 ${scope.target}`
    case 'range':
      return `${formatDate(scope.from)} 至 ${formatDate(scope.to)}`
  }
}

/** ISO → 「2026年2月14日」。报告里不用 2026-02-14，那是机器格式 */
export function formatDate(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`
}

/** ISO → 「2026年2月14日 15:30」 */
export function formatDateTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${formatDate(iso)} ${hh}:${mm}`
}

/*
  ── 关于报告编号 ──

  这里原先有一个 `reportCode(report)`，从 `id` + `createdAt` 派生编号
  （`RPT-YYYYMMDD-` + id 末 4 位）。任务 7 接入后端后**删掉了**：

  后端 `reports` 表有自己的 `code` 列，由服务端分配并随 `ReportOut` 下发。
  两个算法必然分叉 —— 实测同一条报告，前端派生得 `RPT-20260924-KZE`，
  后端存的是 `RPT-20260924-CAP8`。而编号是交付物的身份，客户拿它对账：
  两个来源就等于同一份报告有两个身份。

  现在编号只从 `Report.code` 读（见上面的字段说明）。
*/
