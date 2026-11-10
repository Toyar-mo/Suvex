/**
 * 资产覆盖度与事实图（v1.81）。
 *
 * 与 `data/asset.ts` 的区别：那个是"资产现在什么样"（扫描/漏洞聚合），
 * 这里是"我们到底在它上面认知了什么"——测过哪些面、排除了什么、
 * 事实之间怎么连。两个接口都按 `target` 查询，与 `/assets` 同一口径。
 */

import { api } from '@/lib/api'

export interface CoverageItem {
  factKey: string
  summary: string
  detail: string
  confidence: string
  trustLevel: string
  recordedBy: string
  updatedAt: string
  version: number
}

export interface CoverageGap {
  key: string
  label: string
  /** 只表示"有记录"，不表示"测过"。 */
  recorded: boolean
  matches: string[]
}

export interface CoverageSnapshot {
  target: string
  tested: CoverageItem[]
  ruledOut: CoverageItem[]
  gaps: CoverageGap[]
  truncated: boolean
  stats: { tested: number; ruledOut: number }
}

export interface SurfaceNode {
  factKey: string
  category: string
  summary: string
  detail: string
  confidence: string
  trustLevel: string
  recordedBy: string
  updatedAt: string
  version: number
}

export interface SurfaceEdge {
  from: string
  type: string
  to: string
}

export interface SurfaceMap {
  target: string
  nodes: SurfaceNode[]
  edges: SurfaceEdge[]
  truncated: boolean
}

export async function loadCoverage(target: string): Promise<CoverageSnapshot> {
  return api.get<CoverageSnapshot>('/assets/coverage', { query: { target } })
}

export async function loadSurfaceMap(target: string): Promise<SurfaceMap> {
  return api.get<SurfaceMap>('/assets/surface', { query: { target } })
}
