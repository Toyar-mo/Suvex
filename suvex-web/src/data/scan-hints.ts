import { api } from '@/lib/api'

/**
 * 运行中的人机协同：给智能体补线索（P2，v0.57）。
 *
 * ## 为什么单独一个模块而不是塞进 `scan.ts`
 *
 * `scan.ts` 是纯数据投影（类型 + 派生函数），**不 import `api`**。
 * 这里要发请求，混进去会把它从一个可被任何地方安全引用的纯模块变成
 * 一个带网络依赖的模块 —— 而 `stallInfo` 这类纯函数被三处调用点引用着，
 * 不该因为"同一页还需要发个请求"就背上网络依赖。
 */

/** 一条线索（对应后端 `HintOut`）。 */
export interface ScanHint {
  id: string
  text: string
  author: string | null
  /**
   * 是否已被某一轮注入进模型上下文。
   *
   * ★ 它是这个功能**最关键的可见性**：用户输入后如果一直显示「排队中」，
   * 他会以为没生效而反复输入。只有区分「已送达」与「还在排队」，界面才诚实。
   */
  consumed: boolean
  consumedTurn: number | null
  createdAt: string
}

/** 给一条正在跑的扫描补线索。 */
export function addScanHint(scanId: string, text: string): Promise<ScanHint> {
  return api.post<ScanHint>(`/scans/${scanId}/hints`, { text })
}

/** 列出这条扫描的全部线索（含已送达 / 待送达）。 */
export function loadScanHints(scanId: string): Promise<ScanHint[]> {
  return api.get<ScanHint[]>(`/scans/${scanId}/hints`)
}
