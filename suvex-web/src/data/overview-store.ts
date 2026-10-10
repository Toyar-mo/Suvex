import { ApiError, api, toApiError } from '@/lib/api'
import { useSyncExternalStore } from 'react'
import type { OverviewData } from './overview'

/**
 * 总览数据的**单一运行时事实来源**。
 *
 * ## 为什么它不像其它 store 那样有"本地派生"
 *
 * 任务 7 之前，Overview 的数字是前端从 findings-store + scans-store
 * **现场算**出来的（`buildOverviewData`）。那个做法有一个隐蔽的问题：
 * 它要求前端手里有**全量**漏洞与扫描。
 *
 * 而 `GET /findings` 返回的是摘要（契约 §3.4），`GET /scans` 也是 ——
 * 前端算出来的"本周新增高危""平均耗时"在某些边界上会与后端不一致，
 * 而**两个数字对不上时用户没法判断哪个是对的**。
 *
 * 现在整页读 `GET /overview` 一个端点：后端在**同一个事务**里算出
 * 全部指标、活动流与分布。要么全对，要么全错。
 *
 * ⚠️ 一个必须知道的取舍：**这一页不再是实时联动的。**
 *   在 Findings 页把一条标成误报，切回 Overview 时数字会更新
 *   （挂载时重新拉取），但**停在 Overview 页不动时不会自己变**。
 *   原来的本地派生是实时的，代价是数字可能与后端不一致 ——
 *   而"总览和明细页对不上"比"晚几秒更新"严重得多。
 *
 *   需要真实时性的是活动流，那件事由 `Agent Activity` 页的 SSE 负责。
 */

type Listener = () => void

export type OverviewStatus = 'loading' | 'ready' | 'error'

export interface OverviewSnapshot {
  data: OverviewData | null
  status: OverviewStatus
  /** ★ 真的 `ApiError`，不是一句文案 —— ErrorState 要靠它区分失败种类 */
  error: ApiError | null
}

const listeners = new Set<Listener>()

/** ★ 初始为 null —— 原来是 `buildOverviewData(MOCK_...)` 现场算的一份 */
let data: OverviewData | null = null
let dataStatus: OverviewStatus = 'loading'
let loadError: ApiError | null = null

/** 快照必须引用稳定，否则 useSyncExternalStore 会无限重渲染 */
let snapshot: OverviewSnapshot = { data, status: dataStatus, error: loadError }

/** 丢弃过期响应（StrictMode 双挂载、快速切页都会并发两次） */
let epoch = 0

function emit() {
  for (const listener of listeners) listener()
}

function commit() {
  snapshot = { data, status: dataStatus, error: loadError }
  emit()
}

export function subscribeOverview(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function getOverviewSnapshot(): OverviewSnapshot {
  return snapshot
}

export function getOverviewServerSnapshot(): OverviewSnapshot {
  return snapshot
}

/**
 * 读总览 —— `GET /overview`。
 *
 * ### `silent` 的默认值不是 `false`，而是"已经有数据就静默"
 *
 * 与其它 store 不同：这一页是**首屏**，用户从别的页面切回来时
 * 数据几乎总是已经在 store 里了。默认重拉就会让首屏每切一次闪一次骨架 ——
 * 而骨架出现在首屏比出现在任何地方都刺眼。
 *
 * 所以默认行为是"有数据就静默刷新"（stale-while-revalidate）：
 * 页面继续显示旧数字，新的到了再替换。真正需要骨架的只有**第一次**。
 */
export async function loadOverview(options: { silent?: boolean } = {}): Promise<void> {
  const silent = options.silent ?? (data !== null && dataStatus === 'ready')
  const current = ++epoch

  if (!silent) {
    dataStatus = 'loading'
    loadError = null
    commit()
  }

  try {
    const next = await api.get<OverviewData>('/overview')
    if (current !== epoch) return
    data = next
    dataStatus = 'ready'
    loadError = null
  } catch (error) {
    if (current !== epoch) return
    // 静默刷新失败时保持 `ready` 与旧数据：一次超时不代表数据没了，
    // 而把已经显示的看板换成一个错误页，比"数字暂时没更新"糟得多
    if (!silent) {
      dataStatus = 'error'
      loadError = toApiError(error)
    }
  }
  commit()
}

export function useOverviewState(): OverviewSnapshot {
  return useSyncExternalStore(
    subscribeOverview,
    getOverviewSnapshot,
    getOverviewServerSnapshot,
  )
}

/** 仅供测试与开发：清空 store（**不再复位到 mock**） */
export function resetOverview(): void {
  data = null
  dataStatus = 'loading'
  loadError = null
  commit()
}
