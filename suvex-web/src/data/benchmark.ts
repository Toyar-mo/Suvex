/**
 * 评测快照（v1.81，`/benchmark/*`）。
 *
 * 快照只存指标截面；对比返回 `delta`（after - before）。
 * 正数不代表"更好"：误报率上升、耗时上升都是坏事 —— 好坏由指标语义决定。
 */

import { api } from '@/lib/api'

export interface QualityPayload {
  scans: {
    total: number
    completed: number
    failed: number
    aborted: number
    avgDurationSec: number | null
  }
  findings: {
    total: number
    verified: number
    needsFollowUp: number
    none: number
    falsePositive: number
    falsePositiveRate: number | null
  }
  challenges: {
    total: number
    solved: number
    solveRate: number | null
    candidateCount: number
    wrongSubmissions: number
  }
  coverage: { surfaces: number; targets: number }
}

export interface BenchmarkSnapshot {
  id: string
  label: string
  capturedAt: string
  payload: QualityPayload
}

/** 对比的一侧：后端返回 `{id,label,capturedAt,metrics}`（不是完整快照）。 */
export interface BenchmarkSide {
  id: string
  label: string
  capturedAt: string
  metrics: QualityPayload
}

export interface BenchmarkCompare {
  before: BenchmarkSide
  after: BenchmarkSide
  delta: Record<string, number | null>
}

export async function loadSnapshots(limit = 50): Promise<BenchmarkSnapshot[]> {
  const out = await api.get<{ items: BenchmarkSnapshot[] }>(
    `/benchmark/snapshots?limit=${limit}`,
  )
  return out.items
}

export async function captureSnapshot(label: string): Promise<BenchmarkSnapshot> {
  return api.post<BenchmarkSnapshot>('/benchmark/snapshots', { label })
}

export async function compareSnapshots(
  before: string,
  after: string,
): Promise<BenchmarkCompare> {
  return api.get<BenchmarkCompare>(
    `/benchmark/compare?before=${encodeURIComponent(before)}&after=${encodeURIComponent(after)}`,
  )
}
