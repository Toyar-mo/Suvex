/** 总览「最近发现」的轻量查询（v1.83，`GET /findings?limit=5`）。 */

import { api } from '@/lib/api'

export interface RecentFinding {
  id: string
  severity: string
  title: string
  target: string
  discoveredAt: string
}

export async function loadRecentFindings(limit = 5): Promise<RecentFinding[]> {
  return api.get<RecentFinding[]>(`/findings?limit=${limit}`)
}
