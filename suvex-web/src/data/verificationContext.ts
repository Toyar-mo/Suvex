/** 验证上下文（v1.83，`GET /findings/{id}/verification-context`）。 */

import { api } from '@/lib/api'

export interface VerificationFact {
  factKey: string
  category: string
  summary: string
  confidence: string
  trustLevel: string
  updatedAt: string
}

export interface VerificationContext {
  finding: {
    id: string
    title: string
    severity: string
    cwe: string | null
    target: string
    verification: string
    status: string
  }
  facts: VerificationFact[]
  relatedFindings: {
    id: string
    title: string
    severity: string
    verification: string
  }[]
  checks: { class: string; general: string[]; specific: string[] }
  note: string
}

export async function loadVerificationContext(
  findingId: string,
): Promise<VerificationContext> {
  return api.get<VerificationContext>(
    `/findings/${encodeURIComponent(findingId)}/verification-context`,
  )
}
