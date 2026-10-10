import { ApiError, api, toApiError } from '@/lib/api'
import { useSyncExternalStore } from 'react'

/**
 * 扫描保留规则 —— 单独一个小 store，**不并进 `settings-store`**。
 *
 * ## 为什么不并进去
 *
 * `settings-store` 是一个 900 行的聚合（accounts / sandbox / notices / tokens），
 * 它的一次 `loadSettings()` 打的是 `GET /settings` 那一个聚合端点。
 * 而保留规则是**独立端点**（`GET /settings/scan-retention`），
 * 并且只有设置页的这一个面板用得到。
 *
 * 并进去的代价是：打开任何页面（Layout 会 ensure 设置）都要多打一个
 * 与它无关的请求，而那个请求的失败还要在那个大 store 的错误状态里
 * 与别的东西混在一起 —— 面板无法知道自己为什么空着。
 *
 * 独立 store 的判据很简单：**它有自己的端点、自己的加载状态、
 * 自己的失败原因**。三者都独立时，合并只会让它们互相拖累。
 */

export type RetentionStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface ScanRetention {
  /** 失败/已终止 N 天后自动归档。**0 = 关闭该规则** */
  archiveAfterDays: number
  trashRetentionDays: number
  /** 上次清理时刻。没有它，"我设了 30 天为什么没生效"无法回答 */
  lastSweepAt: string | null
}

export interface RetentionSnapshot {
  config: ScanRetention | null
  status: RetentionStatus
  error: ApiError | null
}

const listeners = new Set<() => void>()

let config: ScanRetention | null = null
let status: RetentionStatus = 'idle'
let error: ApiError | null = null
let snapshot: RetentionSnapshot = { config, status, error }

function commit() {
  snapshot = { config, status, error }
  for (const listener of listeners) listener()
}

export function subscribeRetention(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function getRetentionSnapshot(): RetentionSnapshot {
  return snapshot
}

export async function loadScanRetention(): Promise<void> {
  status = 'loading'
  error = null
  commit()
  try {
    config = await api.get<ScanRetention>('/settings/scan-retention')
    status = 'ready'
    error = null
  } catch (err) {
    status = 'error'
    error = toApiError(err)
  }
  commit()
}

/**
 * 保存。
 *
 * ★ 响应**带回 `lastSweepAt`** —— 后端在保存时会顺手跑一次清理
 * （否则用户会盯着"30 天"等下一次启动，而"我改了规则但什么都没发生"
 * 看起来就是没生效）。所以这里用响应覆盖本地，而不是只改那两个字段。
 */
export async function saveScanRetention(next: {
  archiveAfterDays: number
  trashRetentionDays: number
}): Promise<void> {
  const saved = await api.put<ScanRetention>('/settings/scan-retention', next)
  config = saved
  status = 'ready'
  error = null
  commit()
}

export function useScanRetention(): RetentionSnapshot {
  return useSyncExternalStore(subscribeRetention, getRetentionSnapshot, getRetentionSnapshot)
}
