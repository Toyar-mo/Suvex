import { ApiError, api, toApiError } from '@/lib/api'
import { useSyncExternalStore } from 'react'
import type { AssetList } from './asset'

/**
 * 资产列表的运行时事实来源。
 *
 * 与 scans-store / findings-store 同一套模式（`useSyncExternalStore` +
 * 整体替换快照）。为什么不能各页 `useState` 一份：
 * 这一页将来要联动"发起扫描"（扫完回来列表要变），而扫描页也会显示
 * "这个目标上次扫是什么时候" —— 两份快照一定漂移。
 *
 * ## 只有两个状态要区分：还没问过 / 问过
 *
 * `idle` 与 `loading` 是**两件不同的事**：`idle` 表示"还没人问过这个列表"。
 * 混成 `loading` 会让侧栏刚挂载就闪一下骨架屏，而用户根本没进这一页。
 *
 * ## 不轮询
 *
 * 扫描列表需要轮询（有 running 要盯），**资产列表不需要** —— 它是一份
 * 台账，变化来自"有人发起了一次扫描"，而那件事在扫描页自己会推。
 * 在这一页轮询只会让一个"看一眼就走"的页面持续烧请求。
 * 要刷新就显式刷新（页面提供"刷新"按钮 + 切回页面时拉一次）。
 */

type Listener = () => void

export type AssetsStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface AssetsSnapshot {
  /** 当前这一页的目标。`null` = 还没加载过。 */
  items: AssetList['items'] | null
  /** 服务端给的总数（可能大于 `items.length`，见 `truncated`）。 */
  total: number
  /** 目标数超过上限 —— 界面**必须**显示出来。 */
  truncated: boolean
  /** 顶部统计条。随列表一起返回，不单独请求。 */
  summary: AssetList['summary'] | null
  status: AssetsStatus
  /** ★ 真的 `ApiError`，不是一句文案 —— `ErrorState` 要靠它区分失败种类。 */
  error: ApiError | null
}

const listeners = new Set<Listener>()

let snapshot: AssetsSnapshot = {
  items: null,
  total: 0,
  truncated: false,
  summary: null,
  status: 'idle',
  error: null,
}

function emit() {
  for (const listener of listeners) listener()
}

function setSnapshot(patch: Partial<AssetsSnapshot>) {
  snapshot = { ...snapshot, ...patch }
  emit()
}

/** 在飞的请求 —— 同一时刻只发一个。 */
let inFlight: Promise<void> | null = null

/**
 * 拉取资产列表。
 *
 * `silent` 用于"后台刷新"：不切到 `loading`（否则会把已经显示的表格
 * 换成骨架屏，用户会以为页面闪了一下）。失败时也不覆盖已有的 `error`
 * —— 后台刷新失败不该把页面上好好的数据变成一个错误页。
 */
export async function loadAssets(options: { silent?: boolean } = {}): Promise<void> {
  if (inFlight) return inFlight

  const silent = options.silent === true
  if (!silent && snapshot.items === null) {
    // 第一次加载才显示骨架屏；已有数据时重拉不打扰用户。
    setSnapshot({ status: 'loading', error: null })
  }

  inFlight = (async () => {
    try {
      const data = await api.get<AssetList>('/assets')
      setSnapshot({
        items: data.items,
        total: data.total,
        truncated: data.truncated,
        summary: data.summary,
        status: 'ready',
        error: null,
      })
    } catch (error) {
      const apiError = toApiError(error)
      if (!silent) {
        setSnapshot({ status: 'error', error: apiError })
      }
    } finally {
      inFlight = null
    }
  })()

  return inFlight
}

/** 清空到 idle（登出、切库时用）。 */
export function resetAssets(): void {
  inFlight = null
  snapshot = {
    items: null,
    total: 0,
    truncated: false,
    summary: null,
    status: 'idle',
    error: null,
  }
  emit()
}

function subscribe(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

function getSnapshot(): AssetsSnapshot {
  return snapshot
}

export function useAssetsState(): AssetsSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}

/** 供测试用：直接读当前快照，不经过 React。 */
export function getAssetsSnapshot(): AssetsSnapshot {
  return snapshot
}
