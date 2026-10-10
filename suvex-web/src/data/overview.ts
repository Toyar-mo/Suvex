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
 * ⚠️ 全部为 MOCK。接口形状按最终形态设计，接真实 API 时只换数据源实现
 *    （见各 use* hook），组件不动。
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
