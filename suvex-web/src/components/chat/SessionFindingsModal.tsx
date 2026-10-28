/**
 * 「查看漏洞」弹窗：列出**这条对话关联到的**漏洞。
 *
 * ⚠️ 它不取数。取数在 `data/session-insight.ts` 里，由调用方（ChatPage）
 * 在菜单被点时触发 —— 这样打开弹窗不会出现 loading 闪烁两遍，也避免了
 * 在 effect 里同步 setState（本项目 lint 明确禁止 `set-state-in-effect`）。
 *
 * 空态有三种，必须分开说，否则用户会把"没有"读成"坏了"：
 * 1. 这个对话里没建过扫描 → 「没有可关联的漏洞」（不是 bug，是没数据源）
 * 2. 有关联扫描但扫描没出结论 → 「关联的扫描还没有产出漏洞」
 * 3. 取数失败 → 明确报错 + 重试按钮
 */

import { Link } from 'react-router-dom'
import { Modal } from '@/components/ui/Modal'
import { SeverityBadge, StatusBadge } from '@/components/findings/FindingBadges'
import type { FindingListItem } from '@/data/finding'
import { scanFindingsHref } from '@/data/session-insight'

export interface SessionFindingsModalProps {
  open: boolean
  onClose: () => void
  sessionTitle: string
  scanIds: readonly string[]
  items: readonly FindingListItem[]
  loading: boolean
  error: string | null
  onRetry?: () => void
}

export function SessionFindingsModal({
  open,
  onClose,
  sessionTitle,
  scanIds,
  items,
  loading,
  error,
  onRetry,
}: SessionFindingsModalProps) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      widthClass="max-w-2xl"
      /* 同理保持纯字符串，弹窗才有可访问名。 */
      title={`关联漏洞（${loading ? '载入中…' : `${items.length} 条 · ${scanIds.length} 个扫描`}）`}
    >
      <div className="space-y-3">
        <p className="text-2xs text-ink-faint">
          会话「{sessionTitle}」里创建或提到过的扫描，所产生的漏洞结论。
        </p>

        {loading && (
          <p data-findings-loading className="py-8 text-center text-xs text-ink-faint">
            正在取关联漏洞…
          </p>
        )}

        {!loading && error && (
          <div data-findings-error className="space-y-2 rounded-sm bg-surface-raised p-3">
            <p className="text-xs text-signal-risk">取关联漏洞失败：{error}</p>
            {onRetry && (
              <button
                type="button"
                onClick={onRetry}
                className="rounded-xs border border-line px-2 py-1 text-2xs text-ink hover:bg-surface-raised"
              >
                重试
              </button>
            )}
          </div>
        )}

        {!loading && !error && scanIds.length === 0 && (
          <p data-findings-empty="no-scan" className="py-8 text-center text-xs text-ink-faint">
            这个对话里还没有创建过扫描，所以没有可关联的漏洞。
          </p>
        )}

        {!loading && !error && scanIds.length > 0 && items.length === 0 && (
          <p data-findings-empty="no-finding" className="py-8 text-center text-xs text-ink-faint">
            关联的 {scanIds.length} 个扫描还没有产出漏洞结论。
          </p>
        )}

        {!loading && !error && items.length > 0 && (
          <ul data-findings-list className="space-y-2">
            {items.map((item) => (
              <li key={item.id} className="rounded-sm border border-line p-2">
                <div className="flex items-center gap-2">
                  <SeverityBadge severity={item.severity} />
                  <StatusBadge status={item.status} />
                  <Link
                    to={scanFindingsHref(item.scanId)}
                    className="ml-auto text-2xs text-accent hover:underline"
                  >
                    查看
                  </Link>
                </div>
                <p className="mt-1 break-words text-xs text-ink">{item.title}</p>
                <p className="mt-0.5 break-words text-2xs text-ink-faint">{item.target}</p>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Modal>
  )
}
