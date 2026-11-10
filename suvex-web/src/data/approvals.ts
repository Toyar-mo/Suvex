/** 跨会话审批记录（v1.83，`GET /chat/approvals`）。 */

import { api } from '@/lib/api'

export interface ChatApproval {
  id: string
  sessionId: string
  sessionTitle: string
  toolName: string
  toolArgs: Record<string, unknown>
  status: string
  decidedBy: string | null
  decidedAt: string | null
  createdAt: string
}

export async function loadApprovals(limit = 100): Promise<ChatApproval[]> {
  return api.get<ChatApproval[]>(`/chat/approvals?limit=${limit}`)
}
