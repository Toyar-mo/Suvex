/**
 * 评测快照（v1.81，`/benchmark/*`）。
 *
 * 快照只存指标截面；对比返回 `delta`（after - before）。
 * 正数不代表"更好"：误报率上升、耗时上升都是坏事 —— 好坏由指标语义决定。
 */

import { api } from '@/lib/api'

export interface QualityPayload {
  cost?: {
    calls: number
    unreportedCalls: number
    promptTokens: number
    completionTokens: number
    totalTokens: number
    usd: number
    unpricedTokens: number
  }
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

export interface BenchmarkCase {
  id: string
  suite: string
  name: string
  kind: 'ctf' | 'scan'
  challengeId: string | null
  scanId: string | null
  expect: Record<string, unknown>
  enabled: boolean
  createdAt: string
}

export interface CaseRun {
  id: string
  caseId: string
  status: 'passed' | 'failed' | 'pending' | 'error'
  detail: Record<string, unknown>
  collectedAt: string
}

export interface SuiteRun {
  suite: string
  runs: CaseRun[]
  passed: number
  failed: number
  pending: number
  error: number
}

export interface NewBenchmarkCase {
  suite: string
  name: string
  kind: 'ctf' | 'scan'
  challengeId?: string
  scanId?: string
  expect?: Record<string, unknown>
  enabled?: boolean
}

export async function loadCases(suite?: string): Promise<BenchmarkCase[]> {
  const query = suite ? `?suite=${encodeURIComponent(suite)}` : ''
  return api.get<BenchmarkCase[]>(`/benchmark/cases${query}`)
}

export async function createCase(input: NewBenchmarkCase): Promise<BenchmarkCase> {
  return api.post<BenchmarkCase>('/benchmark/cases', input)
}

export async function deleteCase(caseId: string): Promise<{ deleted: boolean }> {
  return api.del<{ deleted: boolean }>(`/benchmark/cases/${encodeURIComponent(caseId)}`)
}

export async function runSuite(suite: string): Promise<SuiteRun> {
  return api.post<SuiteRun>(`/benchmark/suites/${encodeURIComponent(suite)}/run`)
}

export async function loadRuns(suite?: string): Promise<CaseRun[]> {
  const query = suite ? `?suite=${encodeURIComponent(suite)}` : ''
  return api.get<CaseRun[]>(`/benchmark/runs${query}`)
}
