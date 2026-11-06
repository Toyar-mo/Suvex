/**
 * CTF 赛事自动托管（`ctf-race`）的前端数据层。
 *
 * 后端端点（`app/ctf_race/router.py`）：
 *   · `POST /ctf-race/start`  —— 起一场自动托管（scan 权限）
 *   · `POST /ctf-race/stop`   —— 请求停止（scan 权限）
 *   · `GET  /ctf-race/status` —— 进度 + 成本（read 权限）
 *
 * 这里只写 `/ctf-race/...`，**不出现任何后端主机名或端口** —— 由
 * `lib/api` 统一前缀（与 `data/kernel.ts` 同一约定）。
 *
 * ## 为什么不复用 `useKernel`
 *
 * 赛事托管是自己的状态机（running/report/error、按题 stage、成本快照），
 * 与 CTF2 题卡列表的数据形状不同。硬塞进 `useKernel` 会让两边都变难读。
 * 但它**共用** `api` 与 `errorMessage`，所以调用层没有重复造轮子。
 */

import { api, errorMessage } from '@/lib/api'

/** 一道题在托管里的阶段（对应后端 `coordinator.ChallengeStage`）。 */
export type RaceStage = 'pending' | 'running' | 'solved' | 'failed' | 'skipped'

export const RACE_STAGE_LABEL: Record<RaceStage, string> = {
  pending: '排队',
  running: '解题中',
  solved: '已解出',
  failed: '失败',
  skipped: '已跳过',
}

/** 单题进度（对应后端 `_report_to_dict` 的 `by_challenge` 项）。 */
export interface RaceChallengeState {
  remote_id: string
  title: string
  stage: RaceStage
  winner_model: string | null
  flag: string | null
  attempts: number
  last_error: string | null
}

/** 收场报告（对应后端 `MatchReport`）。 */
export interface RaceReport {
  total: number
  solved: number
  failed: number
  skipped: number
  rounds: number
  stopped_reason: string | null
  elapsed_seconds: number
  by_challenge: Record<string, RaceChallengeState>
}

/** 成本快照（对应后端 `CostTracker.summary()`）。 */
export interface RaceCost {
  models?: Record<string, unknown>
  totalCostUsd?: number
  totalTokens?: number
  unknownPricedModels?: string[]
}

/** `GET/POST /ctf-race/*` 的统一响应。 */
export interface RaceStatus {
  running: boolean
  models: string[]
  challenges: RaceReport | Record<string, never>
  cost: RaceCost
  report: RaceReport | null
  error: string | null
}

export interface RaceStartOptions {
  models?: string[]
  maxConcurrent?: number
  maxBudgetUsd?: number | null
  noSubmit?: boolean
  maxBumps?: number
}

/** 起一场托管。返回起手时的状态（running 通常为 true）。 */
export function startRace(opts: RaceStartOptions = {}): Promise<RaceStatus> {
  return api.post<RaceStatus>('/ctf-race/start', opts)
}

/** 请求停止（当前轮/工具返回后停）。 */
export function stopRace(): Promise<RaceStatus> {
  return api.post<RaceStatus>('/ctf-race/stop')
}

/** 查进度。 */
export function getRaceStatus(): Promise<RaceStatus> {
  return api.get<RaceStatus>('/ctf-race/status')
}

/** 把后端错误翻成人话（同别处的 `errorMessage` 契约）。 */
export function raceErrorMessage(err: unknown): string {
  return errorMessage(err)
}

/** 报告是否为空（后端 challenges 在未跑时是 `{}`）。 */
export function reportOf(status: RaceStatus | null): RaceReport | null {
  if (!status) return null
  if (status.report) return status.report
  const ch = status.challenges
  if (ch && typeof ch === 'object' && 'by_challenge' in ch) {
    return ch as RaceReport
  }
  return null
}
