import type { Severity } from './findings'

/**
 * Overview 页的数据模型。
 *
 * 字段设计贴着 Suvex 实际会产出的东西，而不是先想 UI 再倒推字段：
 *
 *   - 扫描（scan）是 Suvex 的一等对象：有目标、有状态、有起止时间、有耗时。
 *     「平均单次扫描耗时」因此是从已完成扫描里算出来的，不是一个独立指标。
 *   - agent 是扫描的执行者，一个扫描可能跑多个 agent，所以事件里带 agentId
 *     而不是把 agent 当顶层对象。
 *   - 漏洞（finding）挂在扫描上 —— **类型定义在 data/finding.ts**，
 *     与 Findings 页共用同一个模型，不在这里另立一份。
 *   - 「已验证 PoC」是 finding 的 verification 字段，不是独立集合 ——
 *     这是 Suvex 的核心主张（证据可复现），所以它值得单独计数。
 *
 * ⚠️ 这个文件曾经写着「全部为 MOCK」——**那句话已经过期了**（2026-10-10 修）。
 *    现在它的数据来自 `GET /overview`（见 `overview-store.ts`），
 *    这里只剩类型定义与派生纯函数。
 *
 *    留这句不是洁癖：数据源接上之后，注释里还写着 MOCK 会让人
 *    **不敢改**（以为改了会破坏一个演示态），或者反过来
 *    **不去查真接口**（以为字段还没定）。注释过期比没有注释更坏。
 */

// ─────────────────────────────────────────────
// 枚举
// ─────────────────────────────────────────────

// Scan / ScanStatus 已移到 data/scan.ts —— 那里是完整的扫描模型
// （目标类型、技能范围、漏洞摘要、进度）。Overview 从那里 import，
// 不再自己留一份，避免两个定义漂移。
export type { Scan, ScanStatus } from './scan'

export type AgentPhase = 'recon' | 'scan' | 'exploit' | 'verify' | 'report'

/** 事件类型 —— 对应 agent 在做的事，不是日志级别 */
export type ActivityEventType =
  | 'discovery' // 发现潜在漏洞点
  | 'tool_call' // 调用工具
  | 'poc_generated' // 生成 PoC
  | 'verified' // 验证通过
  | 'blocked' // 被沙箱/策略拦下
  | 'completed' // 任务完成

// ─────────────────────────────────────────────
// 领域对象
// ─────────────────────────────────────────────

export interface ActivityEvent {
  id: string
  /** 关联的扫描，用于跳转到 Agent Activity 页对应上下文 */
  scanId: string
  /** agent 标识，如 "agent-03" */
  agentId: string
  agentPhase: AgentPhase
  type: ActivityEventType
  /** 一句话动作描述 —— 这是给扫视用的，不是给归档用的 */
  summary: string
  /** 涉及的端点/参数等具体对象，没有则 null */
  subject: string | null
  /** ISO 8601 */
  at: string
}

// 注：Finding 类型已移到 data/finding.ts —— 那里有完整定义（状态轴、时间线、
// 详情载荷），并且是 Findings 页与 Overview 共用的同一个模型。
// 这里刻意不再保留一份，避免两个定义漂移。

// ─────────────────────────────────────────────
// 派生视图模型 —— 组件消费的是这些，不是原始集合
// ─────────────────────────────────────────────

export interface OverviewMetrics {
  activeScans: number
  /**
   * 本周新增的 high + critical。
   *
   * ★ **不含误报** —— 而这个排除由**后端**做（`findings.overview_metrics()`），
   *   前端只负责显示。所以界面上那句"已排除 N 条"必须说出来
   *   （见下面的 `falsePositiveCount`）：排除是可以的，**悄悄排除不行**。
   */
  newHighSeverityThisWeek: number
  /** 与上周对比的增减，null 表示没有可比数据（第一次跑） */
  newHighSeverityDelta: number | null
  verifiedPocCount: number
  /** 已完成扫描的平均耗时，秒。没有已完成扫描时为 null */
  avgScanDurationSec: number | null
  /**
   * 被判为误报、**未计入上述指标**的条数。
   *
   * 为什么必须单独暴露：指标排除了误报，如果不说明，
   * 用户看到"共 11 条"而列表里有 13 条会以为漏了。
   * 排除是可以的，**悄悄排除不行**。
   */
  falsePositiveCount: number

  /*
    ── CTF2 token 消耗（v0.36）────────────────────────────
    
    需求是"总览页要写一个统计 token 消耗，细分到每一次任务的消耗，
    和总消耗"。这里给**总消耗**；细分（按题 / 按模型 / 逐轮）在
    `GET /kernel/usage` 与 `GET /kernel/challenges/{id}/usage`。

    ⚠️ 放在 overview 而不只留在 CTF2 页面：token 是**成本**，
       而成本该与"扫了多少、发现了多少"同屏 —— 与
       `avgScanDurationSec` 同一条理由，那张卡回答的是
       "这套东西烧掉了多少资源"。
  */

  /**
   * LLM 调用总次数。
   *
   * ★ 它**永远准确**（后端从行数数得出来），不依赖接入点给不给用量。
   */
  ctf2LlmCalls: number
  /**
   * 其中**接入点真的返回了用量**的次数。
   *
   * ★★ 与 `ctf2LlmCalls` 分开显示（后端也分开返回）：
   *   两者不等时 `ctf2TotalTokens` 是**下限**。
   *
   * 合成一个的后果很具体：接入点没给 usage 时 token 记 0，而
   * "消耗 0"与"没记录到"在界面上完全一样 —— 用户会以为某次解题
   * 没跑过，或者以为那次特别省。两种都是错的。
   *
   * 这与 `hasKey`/`apiKey` 分两列、`candidateFlag`/`confirmedFlag`
   * 分两列是同一条纪律：**两种语义不能共用一个"空"**。
   */
  ctf2RecordedCalls: number
  /**
   * 总 token 数（prompt + completion）。
   *
   * ⚠️ **不做金额换算**：单价随模型、供应商、时段变化，
   *    写死在代码里会误导。只给 token 数，换算由用户自己乘。
   */
  ctf2TotalTokens: number
  /**
   * 统计从哪一刻开始。**null = 一条记录都没有**。
   *
   * ⚠️ v0.36 **之前**的解题没有 usage 记录。不给这个字段的话，
   *    用户会以为"总共就花了这么点"，而实际是**没记**。
   *    所以界面在它为 null 时要说"还没有数据"，而不是显示"0 token"。
   */
  ctf2UsageSince: string | null

  /*
    ── 待处置口径的风险分（v0.95）────────────────────────

    学自 CyberStrikeAI 的 `renderSeverityInsights`，但口径是我们自己钉的：
    只算**还没处置完**的发现（`new` / `confirmed` / `retesting`）。

    ★ 为什么不用 `severityBreakdown`（全部发现）来算：
      CyberStrikeAI 的 dashboard.js 那行注释写得很清楚 —— 用全量时
      把漏洞全修完之后 by_severity 不变，风险分居高不下，而"待处理严重"
      已经归零，界面同时显示「风险极高」与「没有待处理的严重漏洞」。
      修复之后分数必须降，否则这个数字永远不会给人正反馈。

    ⚠️ 与 `severityBreakdown` 是**两份数据**，不是同一份的两种算法：
      前者含已修复项，后者不含。看到两个数字不一致是正常的。
  */

  /**
   * 待处置发现按严重度的分布。**固定 5 档、保留 0 值**。
   *
   * 保留 0 与 `severityBreakdown` 同一条理由：它是加权分的输入，
   * 少一档会让"这一级有没有东西"变得不可判断。
   */
  unresolvedBreakdown: SeverityBreakdownEntry[]
  /** 待处置总数（上面五档之和）。0 是"确实没有"，不是"算不出来"。 */
  unresolvedTotal: number
  /**
   * 加权风险分。严重×10 / 高危×5 / 中危×2 / 低危×0.5，info 不计。
   *
   * 直接当 0-100 的百分比用（进度条宽度）。后端已 `round(,1)`。
   */
  riskScore: number
  /**
   * 风险等级：`safe` / `low` / `medium` / `high` / `severe`。
   *
   * ★ 前端**不要**拿 `riskScore` 自己重新分档。分档阈值是后端的
   *   `RISK_LEVEL_BANDS`，抄一份到前端就会出现"徽章写高风险、
   *   进度条按中危宽度画"这种自相矛盾的画面。
   */
  riskLevel: RiskLevel
}

/** 风险等级（v0.95，与后端 `RISK_LEVEL_BANDS` 一一对应）。 */
export type RiskLevel = 'safe' | 'low' | 'medium' | 'high' | 'severe'

/**
 * 「该做什么」的一项（v0.95）。
 *
 * ★ 字段里**没有跳转地址**。`kind` 是稳定的语义标识，
 *   前端用它在自己的路由表里查目标 —— 后端不该知道 `/findings?status=...`
 *   这种前端路由，否则改一次路由要发布后端。
 *
 * 排序由后端完成（先 level 再数量）。前端只按收到的顺序渲染 ——
 * 两边都排会让"为什么第 2 条在第 1 条上面"变成没法解释的事。
 */
export interface AttentionItem {
  /**
   * 语义标识：`unresolved_critical` / `pending_approval` /
   * `failing_schedule` / `unresolved_high`。
   *
   * 前端按它选图标、文案与跳转。**认不出的 kind 要被忽略**而不是
   * 报错：后端加一种新的建议时，旧前端不应该整页崩掉。
   */
  kind: string
  /**
   * 有多急：`urgent` / `warning` / `info`。
   *
   * ⚠️ **不是**漏洞的 severity。"一条挂了 30 天的中危"比"刚发现的严重"
   *    更急，这两个概念混用会让界面出现没法解释的红黄搭配。
   */
  level: AttentionLevel
  /** 一句话说清要做什么（含数量），由后端生成 */
  title: string
  /** 为什么要做 / 不做的后果。空串 = 没有补充说明，不渲染空段 */
  detail: string
  /** 涉及几条 */
  count: number
}

export type AttentionLevel = 'urgent' | 'warning' | 'info'

/**
 * 「可用能力」的一格（v0.96，学 CyberStrikeAI dashboard 的六宫格）。
 *
 * ## 与 `metrics` 的分工
 *
 * `metrics` 是**产出**（扫了多少、发现了多少），这里是**家底**
 * （多少工具、多少篇技能、几条 MCP 连接）。产出随使用变化，
 * 家底只在部署者配置时变。两者放一起会让人误以为"技能 31 篇"
 * 是一个成就 —— 而它是安装时就有的。
 *
 * ## ★ 只有三格，不是六格
 *
 * CyberStrikeAI 还有「知识」「角色」「Agents」三格，我们**没有对应物**：
 * 这个代码库里没有 knowledge 模块；`CHAT_PERSONAS` 是写死的常量；
 * `agents.scan_id` 是级联外键，agent 属于某一次扫描而不是常驻名册。
 * 摆三个需要加注解才能看懂的数字，会让旁边三个真数字也被怀疑。
 *
 * ## 形状是数组而不是固定字段
 *
 * 前端按 `kind` 查图标与路由。将来补上知识库时，加一格不需要
 * 同时改两侧类型 —— 固定字段会逼着改两处。
 */
export interface ResourceCount {
  /** 稳定语义标识：`mcp_server` / `mcp_tool` / `skill` */
  kind: string
  /** 格子标题 */
  label: string
  count: number
  /** 一句话补充口径。空串 = 不需要补充，不渲染空段 */
  detail: string
  /**
   * 影响颜色的语气：`ok` / `attention` / `muted`。
   *
   * ⚠️ 什么时候算 `attention` 由**后端**定（比如"一条 MCP 都没配"）。
   *    前端自己判断会出现"总览标黄、工具页标正常"的矛盾。
   */
  tone: ResourceTone
}

export type ResourceTone = 'ok' | 'attention' | 'muted'

export interface SeverityBreakdownEntry {
  severity: Severity
  count: number
}

export interface OverviewData {
  metrics: OverviewMetrics
  activity: ActivityEvent[]
  severityBreakdown: SeverityBreakdownEntry[]
  /** 有没有跑过任何扫描 —— 决定空状态与正常状态 */
  hasAnyScan: boolean
  /**
   * 现在最该动的几件（v0.95），最多 5 条，已由后端排序。
   *
   * ⚠️ 与 `activity` 不是一回事，别合并：
   *   `activity` 是「正在发生什么」—— 过去时，只增不改，给扫视用；
   *   这个是「该做什么」—— 将来时，随处置而消失，给决策用。
   */
  attention: AttentionItem[]
  /**
   * 可用能力三格（v0.96）。家底，与扫描无关 —— 新装系统也有内容。
   */
  resources: ResourceCount[]
}
