import type { Severity } from './findings'

/**
 * 漏洞发现的领域模型。
 *
 * ⚠️ 这里有一条贯穿全站的核心区分：**「验证程度」和「处置状态」是两个轴，
 *    不是一个。** 把它们合成一个状态字段看似省事，但会丢掉信息：
 *
 *      - verification = 证据有多硬（agent 能不能复现它）
 *      - status       = 人怎么处置（我认不认、修没修）
 *
 *    一条漏洞可以是"已用 PoC 证明存在，但我判定它不影响业务，标为误报"。
 *    如果只有一个字段，这种情况就没地方放，只能被迫二选一 ——
 *    而这恰恰是渗透测试里最常见的争议场景。
 *
 *    所以：verification 由 agent 产出（机器认定），status 由人流转（人工处置）。
 *    界面上它们分开显示，且形态不同（一个是徽标，一个是可操作的状态）。
 */

// ─────────────────────────────────────────────
// 两个轴
// ─────────────────────────────────────────────

/** 证据强度 —— agent 复现到什么程度 */
export type Verification = 'none' | 'partial' | 'verified'

export const VERIFICATION_LABEL: Record<Verification, string> = {
  none: '未验证',
  partial: '部分验证',
  verified: '已验证 PoC',
}

/**
 * 处置状态 —— 人工流转。
 *
 * 顺序是**生命周期**顺序，不是随意的枚举顺序：
 *   待确认 → 已确认 → 已修复
 *      ↘       ↘
 *       误报    误报
 * 列表默认按"需要我处理的程度"排序时用它。
 */
export type FindingStatus =
  | 'new'
  | 'confirmed'
  | 'fixed'
  // ── 任务 10 新增：把"修复完成"与"确认关闭"分开 ──
  | 'retesting'
  | 'closed'
  | 'false_positive'

export const FINDING_STATUS_LABEL: Record<FindingStatus, string> = {
  new: '待确认',
  confirmed: '已确认',
  fixed: '已修复',
  retesting: '复测中',
  closed: '已关闭',
  false_positive: '误报',
}

/**
 * 状态顺序 —— 用于排序、筛选分组、下拉框。别在组件里各写一份。
 *
 * ★ 顺序 = **处置流程的推进顺序**，不是字母序：
 * 待确认 → 已确认 → 已修复 → 复测中 → 已关闭，误报作为旁路终态排在最后。
 * 批量操作栏的按钮按同一个顺序渲染，于是用户从左往右点，走的就是流程本身。
 */
export const FINDING_STATUS_ORDER: FindingStatus[] = [
  'new',
  'confirmed',
  'fixed',
  'retesting',
  'closed',
  'false_positive',
]

/**
 * 状态的视觉语义。
 *
 * 「待确认」用 warn 而不是中性色：它是唯一需要人动手的状态，
 * 整页里最该被一眼扫到的就是它。已确认/已修复用中性，误报用灰。
 * 这样一屏里"还需要我做什么"是明确的。
 */
export const FINDING_STATUS_TONE = {
  new: 'warn',
  confirmed: 'info',
  fixed: 'ok',
  /*
    两个新状态的色调按**同一个判据**推：这个状态还需要我管吗？

    * `retesting` —— 正在验证修复效果，事情还在手上 → `info`（与 confirmed
      同类，它确实是 confirmed 的延续）。
    * `closed` —— 验证通过、彻底结束，不再需要任何人关注 → `idle`。
      与 `false_positive` 同色是刻意的：两者都是"不用管了"的终态，
      区分它们靠标签文字，不靠颜色 —— 颜色表达的是"要不要我动手"。
  */
  retesting: 'info',
  closed: 'idle',
  false_positive: 'idle',
} as const satisfies Record<FindingStatus, string>

// ─────────────────────────────────────────────
// 漏洞类型
// ─────────────────────────────────────────────

/**
 * 漏洞类别。用途是**筛选和分组**，不是展示关键词。
 * 与 OWASP 的分类保持可对应，但不照搬 —— 那套太细，筛选项会爆炸。
 */
export type FindingCategory =
  | 'injection'
  | 'broken_access_control'
  | 'auth'
  | 'xss'
  | 'ssrf'
  | 'misconfiguration'
  | 'info_disclosure'
  | 'crypto'

export const FINDING_CATEGORY_LABEL: Record<FindingCategory, string> = {
  injection: '注入',
  broken_access_control: '访问控制缺陷',
  auth: '认证与会话',
  xss: '跨站脚本',
  ssrf: '服务端请求伪造',
  misconfiguration: '配置缺陷',
  info_disclosure: '信息泄露',
  crypto: '加密问题',
}

// ─────────────────────────────────────────────
// 状态变更时间线
// ─────────────────────────────────────────────

export interface FindingTimelineEntry {
  id: string
  /** ISO 8601 */
  at: string
  /** 谁做的。agent 产出的事件是 'agent'，人工流转是 'user' */
  actor: 'agent' | 'user'
  /**
   * 变更类型。用 discriminated 值而不是一句自由文本：
   * 时间线要能按类型上色，自由文本做不到。
   */
  kind: 'detected' | 'poc_generated' | 'verified' | 'status_changed' | 'note'
  /** 一句话说明这一步发生了什么 */
  summary: string
  /** 状态变更时记录前后值，其他类型为 null */
  from: FindingStatus | null
  to: FindingStatus | null
}

// ─────────────────────────────────────────────
// 完整的漏洞对象
// ─────────────────────────────────────────────

export interface FindingDetail {
  /** 漏洞描述：这是什么问题、影响是什么。给交付报告用的人话 */
  description: string
  /** 受影响位置。可能多个（同一类问题的多处实例） */
  affected: string[]
  /**
   * agent 的推理摘要 —— **回答"为什么判断这里有问题"**。
   * 这不是描述的同义反复：描述说"是什么"，这里说"凭什么"。
   */
  reasoning: string
  /** 支撑证据：真实响应片段。原样保留，不翻译 */
  evidence: string
  /** PoC 代码。null = 这一条没有可复现的脚本 */
  poc: { language: string; code: string } | null
  /** 修复建议。为空表示 agent 没有足够把握给出建议 */
  remediation: string | null
}

export interface Finding {
  /** 主键。标识符，不翻译 */
  id: string
  scanId: string
  severity: Severity
  category: FindingCategory
  title: string
  /** 受影响的目标，如 "api.acme.internal" */
  target: string
  verification: Verification
  status: FindingStatus
  /** 发现时间（ISO）。列表按它排序，Overview 的"本周新增"也读它 */
  discoveredAt: string
  /** 详情。列表视图不读它 —— 按需加载是真实后端的形态 */
  detail: FindingDetail
  /** 状态变更与关键事件的时间线，倒序 */
  timeline: FindingTimelineEntry[]
  /**
   * 负责人（任务 10 P2）。
   *
   * ★ 两个都带：id 给筛选与后续操作，名字给显示。
   * **不让前端拿 id 再去查名单** —— 那会让列表渲染依赖第二个请求，
   * 而中间那一刻负责人那一列是空的（看起来像"没人负责"）。
   *
   * 未指派时两者都是 `null`（不是空串 —— 空串会被显示成"负责人："）。
   */
  assigneeId: string | null
  assigneeName: string | null
  /**
   * SLA 到期时刻（任务 10 P3）。`null` = 不计时
   * （该严重度档设了 0 天，或这条已到终态）。
   */
  dueAt: string | null
  /**
   * 到期状态 —— ★ **由后端判定**，前端不自己算。
   *
   * 理由与"角标读 `/findings/stats` 而不是本地数一遍"是同一条：
   * 两处独立判断会取到不同时刻的快照，而"这条到底超期没有"
   * 在列表刷新前后给出不同答案是很难解释的。而且前端算的话，
   * "临期"的判定窗口（3 天）就要在前后端各写一份。
   */
  slaState: SlaState
  /** 剩余天数（负 = 已超期）。后端已经算好 —— 前端不要再从 dueAt 推 */
  slaDaysLeft: number | null
  /**
   * 回收站标记（任务 10 P6-a）。非空 = 在回收站。
   *
   * ★ **与 `Scan.archivedAt` / `Scan.deletedAt` 逐字同构**（契约 §2.1）：
   *   用时间戳不用布尔 —— 布尔丢掉"什么时候删的"，
   *   而界面要能说"3 天前删的"。
   *
   * 它只在 `?deleted=true` 的响应里非空 —— 列表**默认排除**回收站条目。
   */
  deletedAt: string | null
}

export type SlaState = 'none' | 'onTrack' | 'dueSoon' | 'overdue'

/**
 * 列表接口（`GET /findings`）返回的投影 —— **不含 `detail` 与 `timeline`**。
 *
 * 契约 §3.4 的列表返回的是摘要：一次可能拉几百条，每条都带上完整证据文本
 * 会让响应到几 MB，而列表根本不渲染它们。要完整内容就按 id 取详情。
 *
 * ⚠️ 这个类型必须与后端 `FindingSummaryOut` **逐字对应**。别把它当成
 *    "Finding 的省略版"随手加字段 —— 前端多要的字段在后端不存在，
 *    拿到的就是 `undefined`，而 `undefined` 在渲染时的表现是
 *    "这一列偶尔是空的"，那是全站最难查的一类症状。
 */
export type FindingListItem = Pick<
  Finding,
  | 'id'
  | 'scanId'
  | 'severity'
  | 'category'
  | 'title'
  | 'target'
  | 'verification'
  | 'status'
  | 'discoveredAt'
  // 指派（任务 10 P2）：列表上的"负责人"那一列读的就是这两个
  | 'assigneeId'
  | 'assigneeName'
  // SLA（任务 10 P3）：列表上的倒计时标签读这三个
  | 'dueAt'
  | 'slaState'
  | 'slaDaysLeft'
  // 回收站（任务 10 P6-a）：`?deleted=true` 时非空，列表据此显示「在回收站」
  | 'deletedAt'
>

/**
 * 各维度计数 —— `GET /findings/stats` 的响应，**Findings 页与 Overview 页
 * 共用的唯一数据源**（契约 §3.4.2）。
 *
 * ★ 为什么要有这个类型而不是让两页各自 `countBySeverity(findings)`：
 *   两次独立计数在并发编辑下会取到不同快照，而差值看起来像 bug ——
 *   用户会以为其中一个是错的。所以角标与图表都读后端这一份。
 *
 * `bySeverity` 是 `Partial`：契约明确要求**为 0 的档其键不存在**，
 * 而不是 0 或 null（后端用 `response_model_exclude_none=True` 保证）。
 * 读取时一律用 `stats.bySeverity[sev] ?? 0`。
 *
 * `byStatus` / `byCategory` 相反，是**全键**：这两个维度是闭合集合，
 * 缺键会让"确认一下没有误报"变成"这个键在不在"的判断。
 */
export interface FindingStats {
  total: number
  bySeverity: Partial<Record<Severity, number>>
  byStatus: Record<FindingStatus, number>
  byCategory: Record<FindingCategory, number>
  /**
   * 被判为误报的条数。
   *
   * ★ Overview 的两张指标卡要**排除**它（误报不是问题，计进去会让
   *   "待处理"虚高），但排除这件事**必须写在屏幕上** —— 悄悄减掉
   *   会让用户核对不上总数，而"数字对不上"会直接摧毁对整页的信任。
   */
  falsePositiveCount: number
}

// ─────────────────────────────────────────────
// 状态流转规则
// ─────────────────────────────────────────────

/**
 * 从某个状态可以流转到哪些状态。
 *
 * 为什么要有这张表，而不是让界面无条件列出全部四个按钮：
 *   1. **不允许原地踏步。** 一条「已确认」的漏洞再点「确认为漏洞」是无意义操作。
 *      数据层会幂等跳过它（见 findings-store），界面也不该提供。
 *   2. **不允许非法跳跃。** 从「误报」直接到「已修复」没有意义 ——
 *      既然判定不是漏洞，就无所谓修不修。想改主意应该先「重新打开」。
 *   3. 界面据此只渲染**合法且有意义的**操作，用户不会对着一个点了没反应的按钮。
 *
 * 终态不是死的：误报和已修复都允许退回。判断可能被推翻 ——
 * 这正是「证据强度」与「处置结论」分开的价值：证据还在，结论可以改。
 */
export const FINDING_STATUS_TRANSITIONS: Record<FindingStatus, FindingStatus[]> = {
  new: ['confirmed', 'false_positive'],
  confirmed: ['fixed', 'false_positive', 'new'],
  // 已修复有三个出口（任务 10）：提交复测 / 直接关闭 / 退回已确认。
  // 允许直连关闭是因为需求原文写的是"如需要复测环节" —— 复测不该是唯一路径。
  fixed: ['retesting', 'closed', 'confirmed'],
  // 复测通过则关闭；不通过退回已确认（不能直接判误报，理由同 fixed）。
  retesting: ['closed', 'confirmed'],
  // 已关闭可以重新打开：漏洞会复发，没有回路的终态会逼用户新建记录，切断历史。
  closed: ['confirmed'],
  false_positive: ['new', 'confirmed'],
}

/**
 * 流转动作的文案。
 *
 * 写成动词短语（「确认为漏洞」）而不是目标状态名（「已确认」）——
 * 按钮上写的是**点了会发生什么**，不是点完之后它叫什么。
 * 这是操作区最常见的一处措辞错误。
 */
export const FINDING_TRANSITION_LABEL: Record<FindingStatus, string> = {
  new: '退回待确认',
  confirmed: '确认为漏洞',
  fixed: '标记已修复',
  retesting: '提交复测',
  closed: '关闭',
  false_positive: '标记误报',
}

/** 流转动作的风险等级 —— 决定按钮用主色、中性还是提示色 */
export const FINDING_TRANSITION_INTENT: Record<FindingStatus, 'primary' | 'neutral' | 'caution'> = {
  new: 'neutral',
  confirmed: 'primary',
  fixed: 'primary',
  // 提交复测是正向推进（流程在走），用 primary。
  retesting: 'primary',
  // ★ 关闭用 neutral 而不是 primary（任务 10）。
  //
  // 关闭之后这条漏洞就不再出现在视野里了，把它做成最显眼的按钮
  // 会让人误点 —— 而误点的代价是"一条还没处理完的漏洞被静默关掉"。
  // 它是收尾动作，不是主推动作。
  closed: 'neutral',
  // 误报会让这条从统计里消失，值得用提示色提醒这是个有后果的判断
  false_positive: 'caution',
}
