import { api, apiText } from '@/lib/api'

export interface AgentTask {
  id: string
  title: string
  objective: string
  scope: string
  status: 'planning' | 'running' | 'completed' | 'failed' | 'cancelled'
  createdAt: string
  updatedAt: string
}

export interface AgentGoal {
  id: string
  seq: number
  text: string
  status: 'open' | 'met' | 'abandoned'
  evidence: string
}

export interface AgentIntent {
  id: string
  goalId: string | null
  seq: number
  summary: string
  status: 'open' | 'running' | 'done' | 'exhausted' | 'blocked' | 'stopped'
  priority: number
  workerSessionId: string | null
  result: string
}

export interface AgentRun {
  id: string
  intentId: string | null
  role: string
  sessionId: string | null
  status: string
  output: string
}

export interface AgentGraph {
  task: AgentTask
  goals: AgentGoal[]
  intents: AgentIntent[]
  runs: AgentRun[]
}

export async function loadAgentTasks(): Promise<AgentTask[]> {
  return api.get<AgentTask[]>('/agent-tasks')
}

export async function loadAgentGraph(taskId: string): Promise<AgentGraph> {
  return api.get<AgentGraph>(`/agent-tasks/${taskId}`)
}

export async function createAgentTask(input: {
  title: string
  objective: string
  scope: string
}): Promise<AgentGraph> {
  return api.post<AgentGraph>('/agent-tasks', input)
}

export async function cancelAgentTask(taskId: string): Promise<AgentTask> {
  return api.post<AgentTask>(`/agent-tasks/${taskId}/cancel`, {})
}

export async function runAgentTask(taskId: string): Promise<number> {
  const result = await api.post<{ queued: number }>(`/agent-tasks/${taskId}/run`, {})
  return result.queued
}

export async function addAgentGoal(taskId: string, text: string): Promise<AgentGoal> {
  return api.post<AgentGoal>(`/agent-tasks/${taskId}/goals`, { text })
}

export async function addAgentIntent(
  taskId: string,
  summary: string,
  goalId?: string | null,
): Promise<AgentIntent> {
  return api.post<AgentIntent>(`/agent-tasks/${taskId}/intents`, {
    summary,
    goalId: goalId ?? null,
  })
}

export async function proveAgentGoal(goalId: string, taskId: string, evidence: string): Promise<AgentGoal> {
  return api.post<AgentGoal>(`/agent-tasks/${taskId}/goals/${goalId}/prove`, { evidence })
}

export async function loadTaskReport(taskId: string) {
  return apiText(`/agent-tasks/${taskId}/report`)
}
