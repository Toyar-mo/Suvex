/**
 * 修复方案（v1.81，`GET /findings/{id}/remediation`）。
 *
 * 与 `Finding.detail.remediation` 的区别：后者是模型在报告里写的一句话，
 * 这里是按 CWE 生成的**可执行步骤 + 验证方式**，并明确 readiness
 * （证据是否已达 L2）。两者都给，读者可以对照。
 */

import { api } from '@/lib/api'

export interface RemediationPlan {
  findingId: string
  cwe: string
  category: string
  /** `ready` = 证据已达 L2；`needs_verification` = 先补验证再修。 */
  readiness: 'ready' | 'needs_verification' | string
  title: string
  steps: string[]
  verification: string[]
  note: string
}

export async function loadRemediation(findingId: string): Promise<RemediationPlan> {
  return api.get<RemediationPlan>(`/findings/${encodeURIComponent(findingId)}/remediation`)
}
