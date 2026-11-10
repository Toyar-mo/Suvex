/**
 * 紫队检测规则（v1.82，`/purple/*`）。
 *
 * 规则是数据：cwe / titleContains / targetContains 三个条件，
 * 空条件不约束；全都为空视为误配（不命中）。
 */

import { api, apiBlob, type BlobDownload } from '@/lib/api'

export interface PurpleRule {
  id: string
  name: string
  severity: string
  cwe: string | null
  titleContains: string | null
  targetContains: string | null
  enabled: boolean
  createdAt: string
}

export interface PurpleSample {
  id: string
  title: string
  severity: string
  target: string
  cwe: string | null
}

export interface PurpleHit {
  rule: PurpleRule
  count: number
  samples: PurpleSample[]
}

export interface EvaluateResult {
  totalFindings: number
  hits: PurpleHit[]
}

export interface NewPurpleRule {
  name: string
  severity?: string
  cwe?: string
  titleContains?: string
  targetContains?: string
}

export async function loadRules(): Promise<PurpleRule[]> {
  return api.get<PurpleRule[]>('/purple/rules')
}

export async function createRule(input: NewPurpleRule): Promise<PurpleRule> {
  return api.post<PurpleRule>('/purple/rules', input)
}

export async function deleteRule(ruleId: string): Promise<{ deleted: boolean }> {
  return api.del<{ deleted: boolean }>(`/purple/rules/${encodeURIComponent(ruleId)}`)
}

export async function evaluateRules(): Promise<EvaluateResult> {
  return api.post<EvaluateResult>('/purple/evaluate')
}

export async function exportHits(format: 'jsonl' | 'cef'): Promise<BlobDownload> {
  return apiBlob(`/purple/export?format=${format}`)
}
