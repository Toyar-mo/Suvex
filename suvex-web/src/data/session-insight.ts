/**
 * 会话洞察的两个只读取数器：**消息** 与 **关联漏洞**。
 *
 * 为什么单独一个模块而不是塞进 `data/chat.ts`：
 * `chat.ts` 管的是"当前打开的会话 + 它的消息流"这个**有状态的**东西；
 * 这里的函数是**无状态的按需查询**（右侧菜单点了才调），不写 store、
 * 不改 `active`。混在一起会让"点别人家会话的菜单"变成一次隐式的
 * 切换会话 —— 用户会看到自己正看着的会话被换掉。
 */

import { API_PREFIX, api } from '@/lib/api'
import type { ChatMessage, ChatSessionDetail } from '@/data/chat'
import type { FindingListItem } from '@/data/finding'
import { collectScanIds } from '@/lib/attackChain'
import { SEVERITY_ORDER, type Severity } from '@/data/findings'

/** 拉一个会话的全部消息。不复用 `openSession()` —— 那个会改当前会话。 */
export async function fetchSessionMessages(sessionId: string): Promise<ChatMessage[]> {
  const detail = await api.get<ChatSessionDetail>(`/chat/sessions/${encodeURIComponent(sessionId)}`)
  return detail.messages ?? []
}

export interface SessionFindings {
  /** 这个会话里出现过的扫描 id（去重、按首次出现顺序） */
  scanIds: string[]
  items: FindingListItem[]
}

/**
 * 汇总一个会话**关联到的**漏洞。
 *
 * 关联依据是可复核的：扫描 id 出现在工具结果文本里（`list_scans`/`create_scan`
 * 都会写反引号包住的 `scan_xxx`），再从 `/findings?scanId=` 把结论取回来。
 * 所以这里的语义是"**这个对话里创建或提到过的扫描所产生的结论**"，
 * 不是"模型认为相关的漏洞"—— 后者无法验证，写进界面会变成猜测。
 *
 * ⚠️ 并发而不是串行：一个会话通常只关联 1~3 个扫描，串行也不会慢多少，
 * 但批量管理那种场景可能十几条。并发上限由 `Promise.all` 自然限制在
 * scanIds.length（一个会话的工具调用数量本来就有限）。
 *
 * 单个 scanId 查询失败不让整体失败：把失败计入 `items` 之外由调用方提示，
 * 这里返回能拿到的部分 —— 一条扫描的结论页面 500 了，不该让另外两条也看不到。
 */
export async function fetchSessionFindings(sessionId: string): Promise<SessionFindings> {
  const messages = await fetchSessionMessages(sessionId)
  const scanIds = collectScanIds(messages)
  if (scanIds.length === 0) return { scanIds: [], items: [] }

  const responses = await Promise.allSettled(
    scanIds.map((scanId) => api.get<FindingListItem[]>(`/findings?scanId=${encodeURIComponent(scanId)}`)),
  )

  const seen = new Set<string>()
  const items: FindingListItem[] = []
  for (const response of responses) {
    if (response.status !== 'fulfilled') continue
    for (const item of response.value) {
      if (seen.has(item.id)) continue
      seen.add(item.id)
      items.push(item)
    }
  }

  const rank = (severity: Severity): number => {
    const index = SEVERITY_ORDER.indexOf(severity)
    return index === -1 ? SEVERITY_ORDER.length : index
  }
  items.sort((left, right) => rank(left.severity) - rank(right.severity))
  return { scanIds, items }
}

/** 从会话 id 拼出"去漏洞页看这个扫描"的深链。 */
export function scanFindingsHref(scanId: string): string {
  return `/findings?scanId=${encodeURIComponent(scanId)}`
}

/** 只给测试和调试用：把 API 前缀拼出来，避免各处硬编码。 */
export function findingsPath(scanId: string): string {
  return `${API_PREFIX}/findings?scanId=${encodeURIComponent(scanId)}`
}
