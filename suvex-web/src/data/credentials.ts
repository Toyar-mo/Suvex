/** 凭据台账（v1.83，`/credentials`）。 */

import { api } from '@/lib/api'

export interface Credential {
  id: string
  kind: string
  username: string
  target: string
  scanId: string | null
  findingId: string | null
  source: string
  notes: string
  createdAt: string
  hasSecret: boolean
}

export interface NewCredential {
  kind: string
  username: string
  secret: string
  target: string
  source?: string
  notes?: string
}

export interface ReusePlan {
  score: number
  basis: string[]
  sameTarget: number
  sameHost: number
  sameScan: number
}

export async function loadCredentials(target?: string): Promise<Credential[]> {
  const query = target ? `?target=${encodeURIComponent(target)}` : ''
  return api.get<Credential[]>(`/credentials${query}`)
}

export async function createCredential(input: NewCredential): Promise<Credential> {
  return api.post<Credential>('/credentials', input)
}

export async function revealCredential(id: string): Promise<{ secret: string }> {
  return api.post<{ secret: string }>(`/credentials/${encodeURIComponent(id)}/reveal`)
}

export async function loadReusePlan(id: string): Promise<ReusePlan> {
  return api.get<ReusePlan>(`/credentials/${encodeURIComponent(id)}/reuse-plan`)
}
