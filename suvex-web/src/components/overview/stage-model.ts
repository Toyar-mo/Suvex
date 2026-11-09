import type { ActivityEvent, AgentPhase } from '@/data/overview'

/**
 * 总览「实时流程舞台」的纯数据模型。
 *
 * 它从活动事件里算出三件舞台上要画的东西：
 *
 *   1. 每个阶段最近落了多少条事件；
 *   2. 每个智能体最近一次出现在哪个阶段；
 *   3. 整个窗口里最新的一条落在哪个阶段。
 *
 * 单拎出来一个文件，有两个实际原因：
 *   · 纯函数可以直接单测，不必去 jsdom 里模拟 canvas；
 *   · 组件文件只导出组件，`react-refresh` 的 Fast Refresh 才不会失效。
 *
 * ## 口径
 *
 * 只看**最近 `WINDOW` 条事件**，不是全量历史。总览的活动流本来就只保留
 * 最近一段（种子 8 条 + 实时增量），把它标成全量会是假的。
 * 口径写进舞台的 `aria-label`。
 */

/** 与 `AgentPhase` 一一对应，顺序就是扫描流程的顺序。 */
export const PHASES: ReadonlyArray<{ key: AgentPhase; label: string }> = [
  { key: 'recon', label: '信息收集' },
  { key: 'scan', label: '扫描' },
  { key: 'exploit', label: '利用' },
  { key: 'verify', label: '验证' },
  { key: 'report', label: '报告' },
]

export const PHASE_INDEX: Record<AgentPhase, number> = {
  recon: 0,
  scan: 1,
  exploit: 2,
  verify: 3,
  report: 4,
}

/** 计数窗口。与总览活动流显示条数接近，读起来是"最近这一屏"。 */
export const WINDOW = 12

export interface StageMark {
  agentId: string
  phase: AgentPhase
  latestAt: string
  count: number
}

export interface StageModel {
  counts: Record<AgentPhase, number>
  marksByPhase: Record<AgentPhase, StageMark[]>
  latestPhase: AgentPhase | null
  total: number
}

/**
 * 事件 → 舞台模型。
 *
 * `events` 必须已按时间倒序：第一次遇到某个 agent 的那条就是它最近的事件，
 * 直接拿来当"当前阶段"，不需要再比较时间。
 */
export function buildStageModel(events: ActivityEvent[], limit = WINDOW): StageModel {
  const windowEvents = events.slice(0, limit)

  const counts: Record<AgentPhase, number> = {
    recon: 0,
    scan: 0,
    exploit: 0,
    verify: 0,
    report: 0,
  }
  for (const event of windowEvents) counts[event.agentPhase] += 1

  const marksByPhase: Record<AgentPhase, StageMark[]> = {
    recon: [],
    scan: [],
    exploit: [],
    verify: [],
    report: [],
  }

  const byAgent = new Map<string, StageMark>()
  for (const event of windowEvents) {
    const mark = byAgent.get(event.agentId)
    if (mark) {
      mark.count += 1
    } else {
      byAgent.set(event.agentId, {
        agentId: event.agentId,
        phase: event.agentPhase,
        latestAt: event.at,
        count: 1,
      })
    }
  }
  for (const mark of byAgent.values()) marksByPhase[mark.phase].push(mark)
  for (const phase of PHASES) {
    marksByPhase[phase.key].sort((a, b) => b.latestAt.localeCompare(a.latestAt))
  }

  return {
    counts,
    marksByPhase,
    latestPhase: windowEvents[0]?.agentPhase ?? null,
    total: windowEvents.length,
  }
}
