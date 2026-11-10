/** 受管暂存（v1.83，`/stage`）。 */

import { api } from '@/lib/api'

export interface StageResult {
  id: string
  token: string
  name: string
  url: string
  expiresAt: string
}

export async function createStage(input: {
  name: string
  content: string
  ttlMinutes: number
}): Promise<StageResult> {
  return api.post<StageResult>('/stage/text', input)
}

export async function deleteStage(id: string): Promise<{ deleted: boolean }> {
  return api.del<{ deleted: boolean }>(`/stage/${encodeURIComponent(id)}`)
}


export async function loadStages(): Promise<StageResult[]> {
  return api.get<StageResult[]>('/stage')
}
