/** 内网会话台账（v1.83，`/intranet/sessions`）。 */

import { api } from '@/lib/api'

export interface IntranetSession {
  id: string
  kind: 'webshell' | 'reverse' | 'ssh'
  host: string
  username: string
  status: 'alive' | 'dead' | 'unknown'
  notes: string
  createdBy: string
  createdAt: string
  updatedAt: string
  hasSecret: boolean
}

export async function loadIntranetSessions(): Promise<IntranetSession[]> {
  return api.get<IntranetSession[]>('/intranet/sessions')
}

export async function createIntranetSession(input: {
  kind: string
  host: string
  username?: string
  secret: string
  notes?: string
  status?: string
}): Promise<IntranetSession> {
  return api.post<IntranetSession>('/intranet/sessions', input)
}

export async function patchIntranetSession(
  id: string,
  patch: { status?: string; notes?: string; username?: string },
): Promise<IntranetSession> {
  return api.patch<IntranetSession>(`/intranet/sessions/${encodeURIComponent(id)}`, patch)
}

export async function deleteIntranetSession(id: string): Promise<{ deleted: boolean }> {
  return api.del<{ deleted: boolean }>(`/intranet/sessions/${encodeURIComponent(id)}`)
}
