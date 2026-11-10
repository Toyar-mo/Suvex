/** LLM token 按天序列（v1.82，`GET /overview/token-usage`）。 */

import { api } from '@/lib/api'

export interface TokenSeriesPoint {
  date: string
  prompt: number
  completion: number
  total: number
  calls: number
  cacheRead: number
}

export interface TokenUsageSeries {
  days: number
  series: TokenSeriesPoint[]
  totals: {
    prompt: number
    completion: number
    total: number
    calls: number
    unreportedCalls: number
  }
  cacheTracked: boolean
  cacheRead: number
  cacheRate: number | null
}

export async function loadTokenUsage(days: number): Promise<TokenUsageSeries> {
  return api.get<TokenUsageSeries>(`/overview/token-usage?days=${days}`)
}
