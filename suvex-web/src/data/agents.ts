/** Agent 配置（v1.83，`/agents`）。 */

import { api } from '@/lib/api'

export interface AgentProfile {
  id: string
  key: string
  name: string
  description: string
  builtin: boolean
  enabled: boolean
  mcpServers: string[]
  skills: string[]
  tools: string[]
  createdAt: string
  updatedAt: string
}

export interface NewAgentProfile {
  key: string
  name: string
  description?: string
  mcpServers?: string[]
  skills?: string[]
  tools?: string[]
}

export async function loadAgents(): Promise<AgentProfile[]> {
  return api.get<AgentProfile[]>('/agents')
}

export async function createAgent(input: NewAgentProfile): Promise<AgentProfile> {
  return api.post<AgentProfile>('/agents', input)
}

export async function deleteAgent(id: string): Promise<{ deleted: boolean }> {
  return api.del<{ deleted: boolean }>(`/agents/${encodeURIComponent(id)}`)
}
