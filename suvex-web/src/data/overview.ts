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
    `GET /ctf2/usage` 与 `GET /ctf2/challenges/{id}/usage`。

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
}

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
}
