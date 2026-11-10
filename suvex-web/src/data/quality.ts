/**
 * 质量指标（v1.81，`GET /overview/quality`）。
 *
 * 与 `data/overview.ts` 的区别：总览回答"积累了什么"，这里回答"做得怎么样"。
 * 两个接口分开，是因为质量指标的刷新节奏与总览不同（它变化慢），
 * 且失败时不该影响总览的首屏。
 */

import { api } from '@/lib/api'

export interface QualityScanMetrics {
  total: number
  completed: number
  failed: number
  aborted: number
  /** 只有已完成扫描参与平均；没有样本时为 null。 */
  avgDurationSec: number | null
}

export interface QualityFindingMetrics {
  total: number
  verified: number
  needsFollowUp: number
  none: number
  falsePositive: number
  /** 分母为 0 时为 null（无法计算 ≠ 0%）。 */
  falsePositiveRate: number | null
}

export interface QualityChallengeMetrics {
  total: number
  solved: number
  solveRate: number | null
  candidateCount: number
  wrongSubmissions: number
}

export interface QualityCoverageMetrics {
  surfaces: number
  targets: number
}

export interface QualitySnapshot {
  scans: QualityScanMetrics
  findings: QualityFindingMetrics
  challenges: QualityChallengeMetrics
  coverage: QualityCoverageMetrics
  generatedAt: string
}

export async function loadQuality(): Promise<QualitySnapshot> {
  return api.get<QualitySnapshot>('/overview/quality')
}
