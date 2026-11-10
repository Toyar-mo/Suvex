/**
 * 修复方案（v1.81，`GET /findings/{id}/remediation`）。
 *
 * 与 `Finding.detail.remediation` 的区别：后者是模型在报告里写的一句话，
 * 这里是按 CWE 生成的**可执行步骤 + 验证方式**，并明确 readiness
 * （证据是否已达 L2）。两者都给，读者可以对照。
 */

import { api } from '@/lib/api'

export interface RemediationPatch {
  format: string
  /** 恒为 false：草案不能直接 apply。 */
  appliesDirectly: boolean
  diff: string
  caveats: string[]
}

export interface RemediationPlan {
  findingId: string
  cwe: string
  category: string
  /** `ready` = 证据已达 L2；`needs_verification` = 先补验证再修。 */
  readiness: 'ready' | 'needs_verification' | string
  title: string
  steps: string[]
  verification: string[]
  /** 落 finding 前的反证清单。类目未知时 specific 为空。 */
  verifierChecks?: {
    class: string
    general: string[]
    specific: string[]
  }
  /** 已知 CWE 的结构示意；未知 CWE 为 null。 */
  patch?: RemediationPatch | null
  note: string
}

export async function loadRemediation(findingId: string): Promise<RemediationPlan> {
  return api.get<RemediationPlan>(`/findings/${encodeURIComponent(findingId)}/remediation`)
}
