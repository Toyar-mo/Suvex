import { SEVERITY_META, SEVERITY_ORDER, type Severity } from '@/data/findings'
import {
  FINDING_CATEGORY_LABEL,
  FINDING_STATUS_LABEL,
  VERIFICATION_LABEL,
  type Finding,
  type FindingTimelineEntry,
} from '@/data/finding'
import { SKILLS, type Scan } from '@/data/scan'
import {
  describeScope,
  formatDate,
  type Report,
  type ReportOptions,
  type ReportScope,
} from '@/data/report'
import {
  applyOptions,
  findingsInScope,
  riskVerdict,
  sortForReport,
  summarize,
} from './report-build'
import type { ReportStats } from './report-build'

/**
 * 报告文档的**中间表示（IR）**。
 *
 * ★ 这是整条导出链路的地基，理由只有一条：**预览和导出必须是同一份东西。**
 *
 *   朴素做法是写两个渲染器 —— 一个 React 组件做预览，一个字符串拼接做 Markdown。
 *   它们会立刻开始漂移：预览里改了措辞、加了一行"已修复"、调了排序，
 *   Markdown 里没跟上，于是"预览效果和导出内容一致"这条验收标准
 *   只能靠人肉逐字比对来维持，且每次改动都要重比一遍。
 *
 *   改成一个 IR、两个消费者之后，一致性变成**结构保证**：
 *   `buildReportDocument` 是唯一的真相，预览组件和 Markdown 渲染器都只是
 *   把它翻译成各自的介质。两者不可能在内容上分叉 —— 它们读的是同一批字段。
 *   同时 Markdown 渲染器是纯函数（不含 React），可以被独立脚本直接调用验证。
 *
 *   代价是多一层抽象。相对于"两份会漂移的渲染逻辑"，这层抽象是划算的。
 */

// ─────────────────────────────────────────────
// 条目
// ─────────────────────────────────────────────

/**
 * 报告里的一条漏洞，已按选项裁剪完毕。
 *
 * 注意这里**不是** Finding 的透传：
 *   - 推理和 PoC 可能被选项裁成 null；
 *   - 编号（index）是报告内的，不是全局的；
 *   - 各标签已经翻成人话，渲染器不需要再 import 标签表。
 * 渲染层因此拿不到"不该显示的东西"—— 裁剪发生在数据层，不靠渲染时小心翼翼。
 */
export interface ReportEntry {
  /** 报告内序号，从 1 开始。附录的交叉引用读它 */
  index: number
  findingId: string
  title: string
  severity: Severity
  severityLabel: string
  categoryLabel: string
  target: string
  /** 受影响位置（接口、文件） */
  affected: string[]
  statusLabel: string
  verificationLabel: string
  /** 「2026年2月14日」 */
  discoveredAt: string
  description: string
  /** 已被选项裁掉时为 null，而不是空串 —— 渲染器靠 null 决定「整个小节不出现」 */
  reasoning: string | null
  /** 支撑证据原文。不裁剪：它是结论的依据，去掉后报告就只剩下断言 */
  evidence: string
  poc: { language: string; code: string } | null
  remediation: string | null
  /**
   * 人工处置记录（时间线中 actor === 'user' 的部分）。
   *
   * 只收人工动作，不收 agent 的过程事件：这里回答的是
   * 「这个结论被人怎么处理过」，那是交付报告里客户真正要看的一栏。
   * agent 的推理轨迹属于「推理过程」选项的范畴，已由 reasoning 承载。
   */
  dispositions: { at: string; summary: string }[]
}

// ─────────────────────────────────────────────
// 文档
// ─────────────────────────────────────────────

export interface ReportDocument {
  meta: {
    /**
     * 报告编号 RPT-20260214-A1B2。交付物身份，客户会用这个编号引用。
     *
     * ⚠️ 草稿（还没按下生成）时它是**空串** —— 不是"暂时随便给一个"。
     *    编号由生成动作分配，在生成之前它不存在。渲染器见到空串
     *    要显示"生成后分配"，而不是编一个出来。
     *    详见下方 provisional 字段的说明。
     */
    code: string
    /**
     * 是否是尚未生成的草稿。
     *
     * ★ 这个字段是被实测逼出来的，理由是"预览与导出一致"这条验收标准：
     *
     *   早先草稿也显示一个真编号（由页面挂载时刻派生）。但真正生成时，
     *   编号由落库那一刻重新分配，于是出现：
     *     预览显示 RPT-20260923-3253 → 下载得到的文件是 RPT-20260923-3310。
     *   用户看到的和拿到的对不上，而编号恰恰是客户用来对账的东西。
     *
     *   有两种修法：
     *     (a) 让草稿预知将来的编号 —— 做不到，除非把 id 生成提前到挂载时刻，
     *         那样两份报告可能撞号，且"生成时间"会变成"打开页面时间"；
     *     (b) **草稿不显示编号**，如实说"生成后分配"。
     *   选了 (b)：它既诚实，又让"编号"在唯一有意义的地方保持严格一致 ——
     *   生成之后的报告，其编号在历史列表、预览、导出文件名、正文里
     *   全部来自同一个冻结对象，不可能不同。
     */
    provisional: boolean
    title: string
    /** 范围的人话描述 */
    scope: string
    generatedAt: string
    /** 生成时用的选项 —— 报告自称包含什么，就要真的包含什么 */
    options: ReportOptions
    /** 是否有条目因为「只含已确认」被排除。排除必须写在报告里 */
    excludedCount: number
  }
  /** 一句话风险结论 */
  verdict: string
  stats: ReportStats
  /** 严重度分布，已按 SEVERITY_ORDER 排好，只含有值的项 */
  distribution: { severity: Severity; label: string; count: number }[]
  entries: ReportEntry[]
  /** 本次检测的范围与方法 —— 附录 A */
  method: MethodNote
  /** 严重度分级依据 —— 附录 C */
  severityDefinitions: { severity: Severity; label: string; definition: string }[]
}

export interface MethodNote {
  /** 覆盖的目标 */
  targets: string[]
  /** 涉及的扫描任务 */
  scans: {
    id: string
    name: string
    target: string
    finishedAt: string | null
    /** 扫描状态。附录 A 用它标注"未跑完"，见 buildMethod 的说明 */
    status: string
  }[]
  /** 用到的检测能力 */
  skills: string[]
  /** 时间跨度（有发现时才会算出） */
  span: { from: string; to: string } | null
}

/**
 * 组装报告文档 —— 整条链路的唯一入口。
 *
 * 纯函数：不读 store、不读 Date.now()、不读 localStorage。
 * `now` 显式传入，这样同一次生成的元信息与条目编号稳定，
 * 也让这个函数能被脚本直接调用做验证（预览/导出比对的测试就靠它）。
 */
/**
 * 扫描状态 → 覆盖完整性的说明。
 *
 * 放在 IR 层而不是各渲染器里：预览和 Markdown 必须给出**同一句话**，
 * 各写一份就是两个事实来源的起点（这个判断在本项目已经写过好几次了）。
 */
export function coverageNote(status: string): string {
  switch (status) {
    case 'completed':
      return '完整'
    case 'aborted':
      return '不完整（已终止）'
    case 'running':
      return '不完整（进行中）'
    case 'failed':
      return '未产出结果'
    default:
      return '未知'
  }
}

export function buildReportDocument(input: {
  /** 全量发现（未筛选） */
  allFindings: Finding[]
  /** 扫描集合，用于附录里的方法说明与扫描名解析 */
  scans: Scan[]
  scope: ReportScope
  options: ReportOptions
  title: string
  /** 生成时刻。同时用于报告编号与"生成于" */
  now: string
  /**
   * 报告编号（`Report.code`）。**只有历史复现路径才传。**
   *
   * ★ 编号**由后端分配**，前端不派生它 —— 前端原先那套 `reportCode`
   *   已被删除，理由见 `data/report.ts`：同一条报告两套算法必然分叉，
   *   而编号是客户用来对账的交付物身份。
   *
   *   草稿路径不传：`meta.code` 留空，渲染器据此显示"生成后分配"。
   *   编号由生成动作分配，在按下生成之前它不存在。
   */
  code?: string
  /**
   * 冻结的条目集合（历史报告复现时传入）。
   *
   * ★ 传了它就是**跳过范围过滤**的唯一开关。
   *
   *   为什么不能走 `allFindings` + scope 让函数自己重筛：
   *   历史报告的条目集合是生成时定死的。若按 scope 重算，
   *   期间新增的漏洞会凭空出现在旧报告里、删掉的会消失 ——
   *   同一份交付物于是有了两个版本。scope 在复现路径里
   *   只负责**描述**（"本报告覆盖扫描任务 X"），不再负责筛选。
   */
  frozenEntries?: Finding[]
  /**
   * 草稿态：还没真正生成，因此**没有编号**。
   *
   * 唯一的效果是 meta.code 留空，渲染器据此显示"生成后分配"。
   * 见 ReportDocument.meta.provisional 的完整说明 ——
   * 核心是"编号由生成动作分配，生成之前它不存在"。
   */
  provisional?: boolean
  /**
   * 已冻结的"被排除条数"（复现历史报告时必传）。
   *
   * ★ 为什么复现时必须由外部传入，而不能自行重算：
   *   冻结快照里只有**被收录的** id，被排除的那些已经不在快照里。
   *   因此 `inScope.length - selected.length` 在复现路径上恒为 0，
   *   报告就会在"收录 0 条"时宣称"本次范围内未发现问题" ——
   *   而真相是命中 2 条、全被选项筛掉了。**那不是没问题，那是没看。**
   *   所以这个数字必须在生成时冻结（见 Report.excludedCount）。
   */
  excludedCount?: number
}): ReportDocument {
  const { allFindings, scans, scope, options, title, now, frozenEntries, provisional } = input

  // ── 1. 范围 ──
  const inScope = frozenEntries ?? findingsInScope(allFindings, scope)

  // ── 2. 选项 ──
  const selected = applyOptions(inScope, options)
  // 冻结值优先：复现路径下重算出来的是 0，而真实排除数记在报告里
  const excludedCount = input.excludedCount ?? inScope.length - selected.length

  // ── 3. 排序：永远按严重度降序 ──
  const ordered = sortForReport(selected)

  // ── 4. 裁剪与编号 ──
  const entries = ordered.map((finding, i) => toEntry(finding, i + 1, options))

  const stats = summarize(selected)

  const scanName =
    scope.kind === 'scan' ? scans.find((s) => s.id === scope.scanId)?.name : undefined

  return {
    meta: {
      // 草稿没有编号：它还没被分配。空串是**如实**的表达，不是占位符
      code: provisional ? '' : (input.code ?? ''),
      provisional: provisional === true,
      title,
      scope: describeScope(scope, { scanName }),
      generatedAt: now,
      options,
      excludedCount,
    },
    verdict: riskVerdict(stats),
    stats,
    distribution: SEVERITY_ORDER.filter((s) => (stats.bySeverity[s] ?? 0) > 0).map((severity) => ({
      severity,
      label: SEVERITY_META[severity].label,
      count: stats.bySeverity[severity] ?? 0,
    })),
    entries,
    method: buildMethod(selected, scans, scope),
    severityDefinitions: SEVERITY_ORDER.map((severity) => ({
      severity,
      label: SEVERITY_META[severity].label,
      definition: SEVERITY_META[severity].definition,
    })),
  }
}

/**
 * 把一条发现裁成报告条目。
 *
 * 两个开关的行为差异值得写下来，因为它们容易被做成同一个：
 *   - `includePoc` 关掉 → poc 置 null，但 `verificationLabel` **保留**。
 *     报告仍会写"已验证 PoC"，只是不附脚本。结论不因附带材料而消失。
 *   - `includeReasoning` 关掉 → reasoning 置 null。判断依据被拿掉后，
 *     描述和证据仍在，读者仍能理解问题是什么。
 */
function toEntry(finding: Finding, index: number, options: ReportOptions): ReportEntry {
  return {
    index,
    findingId: finding.id,
    title: finding.title,
    severity: finding.severity,
    severityLabel: SEVERITY_META[finding.severity].label,
    categoryLabel: FINDING_CATEGORY_LABEL[finding.category],
    target: finding.target,
    affected: finding.detail.affected,
    statusLabel: FINDING_STATUS_LABEL[finding.status],
    verificationLabel: VERIFICATION_LABEL[finding.verification],
    discoveredAt: formatDate(finding.discoveredAt),
    description: finding.detail.description,
    reasoning: options.includeReasoning ? finding.detail.reasoning : null,
    evidence: finding.detail.evidence,
    poc: options.includePoc ? finding.detail.poc : null,
    remediation: options.includeRemediation ? finding.detail.remediation : null,
    dispositions: finding.timeline
      .filter((entry) => entry.actor === 'user')
      .map((entry) => ({ at: formatDate(entry.at), summary: dispoSummary(entry) })),
  }
}

/**
 * 处置记录的措辞。
 *
 * 时间线里的 summary 是站内用的（"状态由「待确认」改为「已确认」"），
 * 直接搬进报告会带着界面引号「」，读起来像把 GUI 贴进了文档。
 * 这里改写成报告口吻的完整句。
 */
function dispoSummary(entry: FindingTimelineEntry): string {
  if (entry.kind === 'status_changed' && entry.to) {
    return `处置状态更新为「${FINDING_STATUS_LABEL[entry.to]}」`
  }
  return entry.summary
}

// ─────────────────────────────────────────────
// 附录 A：范围与方法
// ─────────────────────────────────────────────

/**
 * 方法说明。
 *
 * 为什么必须有一节：报告里全是结论，读者无从判断这些结论的分量。
 * "用哪些能力测了什么、覆盖多久"决定了"没写的问题是不是真的没有"。
 * 真实交付物里这一节通常是免责与可信度的边界所在，缺失会让报告显得比实际更权威。
 */
function buildMethod(findings: Finding[], scans: Scan[], scope: ReportScope): MethodNote {
  const scanIds = new Set(findings.map((f) => f.scanId))

  // 范围内没有发现时，扫描集合退回成"范围指向的那一次扫描"，
  // 否则附录会出现"覆盖 0 次扫描"这种看着像出错的表述
  const relevant =
    scanIds.size > 0
      ? scans.filter((s) => scanIds.has(s.id))
      : scope.kind === 'scan'
        ? scans.filter((s) => s.id === scope.scanId)
        : []

  const skillIds = new Set(relevant.flatMap((s) => s.skills))

  const times = findings.map((f) => f.discoveredAt).sort()

  /*
    覆盖目标：**以"测了什么"为准，而不是"从中发现了什么"**。

    这两个在多数报告里恰好相等，于是很容易写成从 findings 里取 ——
    但一旦报告没有收录任何条目（比如"只含已确认"把待确认和误报全筛掉了），
    从 findings 取就会得到空，封面于是写着"涉及目标 0 个"，
    而实际上那个目标**被测过、且这次确实是干净的**。

    "0 个目标"传达的是"这次什么都没测"，与事实完全相反。
    所以找不到发现时退回扫描自身的目标 —— 那才是这次检测的真实覆盖面。
  */
  const targetsFromFindings = [...new Set(findings.map((f) => f.target))].sort()
  const targets =
    targetsFromFindings.length > 0
      ? targetsFromFindings
      : [...new Set(relevant.map((s) => s.target))].sort()

  return {
    targets,
    scans: relevant.map((s) => ({
      id: s.id,
      name: s.name,
      target: s.target,
      finishedAt: s.finishedAt,
      /*
        带上状态，让附录 A 能如实标注"这次扫描未跑完"。

        为什么这件事必须写进报告：被终止（aborted）的扫描会产出真实的
        发现，那些发现值得纳入报告 —— 但读者有权知道**覆盖是不完整的**。
        一个只写"覆盖目标 X"而不提"扫描中断"的附录，会让读者以为
        整个目标都被测过了，那是把漏报伪装成"没问题"。
      */
      status: s.status,
    })),
    skills: SKILLS.filter((skill) => skillIds.has(skill.id)).map((skill) => skill.label),
    span:
      times.length > 0
        ? { from: formatDate(times[0]), to: formatDate(times[times.length - 1]) }
        : null,
  }
}

// ─────────────────────────────────────────────
// 历史报告的复现
// ─────────────────────────────────────────────

/**
 * 复现一份历史报告 —— **离线路径**。
 *
 * ★ 任务 7 起，生产链路的历史预览走 `GET /reports/{id}/document`
 *   （后端按冻结的 `report_findings` 顺序重建）。这个函数保留下来是因为
 *   它和后端 `service.document_for` 是同一件事的两种实现，
 *   而"两者产出必须一致"正是对账脚本要验的东西（契约 §3.5.1）：
 *   留着一份可离线调用的实现，对账才有参照物。
 *
 * ★ 关键：**用报告里冻结的 findingIds 重建，而不是按 scope 重新过滤。**
 *
 *   如果按 scope 重算，三个月后回看历史报告会看到"和当初不一样"的内容 ——
 *   期间修好的漏洞消失了、新发现的漏洞凭空出现。那就是同一份交付物
 *   有了两个版本，在合规场景里是致命的。冻结的 id 列表是报告的一部分。
 *
 *   同时 options 也用**当时**的：用户后来在界面上把开关改回默认，
 *   不能让昨天签出去的报告跟着变。
 */
export function rebuildReportDocument(
  report: Report,
  allFindings: Finding[],
  scans: Scan[],
): ReportDocument {
  const byId = new Map(allFindings.map((f) => [f.id, f]))

  // 按**冻结的顺序**取回，而不是按当前数据的排序重排 ——
  // 生成时的严重度排序是报告的一部分，不该随当前数据变化。
  // 漏洞被删除时该条目会消失：如实少几条，比造一条假数据补位要诚实。
  const frozen = report.findingIds
    .map((id) => byId.get(id))
    .filter((f): f is Finding => f !== undefined)

  return buildReportDocument({
    // 全量传入：附录里的扫描名解析等仍需要完整的上下文
    allFindings,
    scans,
    scope: report.scope,
    options: report.options,
    title: report.title,
    now: report.createdAt,
    code: report.code,
    // ★ 条目集合用冻结的快照，scope 只负责描述
    frozenEntries: frozen,
    // ★ 排除数用冻结的值：复现路径上重算恒为 0，
    //   会让"收录 0 条"的报告谎称"未发现问题"（见 Report.excludedCount）
    excludedCount: report.excludedCount,
  })
}
