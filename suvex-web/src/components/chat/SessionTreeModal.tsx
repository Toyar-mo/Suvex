/**
 * 分叉树弹窗：会话级树，不是 OMP 的消息级 `/tree`。
 *
 * 当前数据模型是「fork 时复制到截止点的前缀」，所以树的节点是**会话**，
 * 边是 `forkedFromSessionId`。每个节点标出分叉点与分支摘要，点击直接切过去。
 */

import { Modal } from '@/components/ui/Modal'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/cn'
import type { ChatSessionTreeNode } from '@/data/chat'

export interface SessionTreeModalProps {
  open: boolean
  onClose: () => void
  nodes: readonly ChatSessionTreeNode[]
  loading?: boolean
  error?: string | null
  onSelect: (sessionId: string) => void
}

function depths(nodes: readonly ChatSessionTreeNode[]): Map<string, number> {
  const byId = new Map(nodes.map((node) => [node.id, node]))
  const result = new Map<string, number>()
  for (const node of nodes) {
    let depth = 0
    let cursor = node
    const seen = new Set<string>()
    while (cursor.forkedFromSessionId && !seen.has(cursor.id)) {
      seen.add(cursor.id)
      const parent = byId.get(cursor.forkedFromSessionId)
      if (!parent) break
      depth += 1
      cursor = parent
    }
    result.set(node.id, depth)
  }
  return result
}

export function SessionTreeModal({
  open,
  onClose,
  nodes,
  loading = false,
  error = null,
  onSelect,
}: SessionTreeModalProps) {
  const { t } = useI18n()
  const depthOf = depths(nodes)

  return (
    <Modal
      open={open}
      onClose={onClose}
      widthClass="max-w-xl"
      title={t('chat.tree.title', undefined, '分支树')}
    >
      <div className="space-y-3">
        <p className="text-2xs leading-relaxed text-ink-faint">
          {t(
            'chat.tree.desc',
            undefined,
            '同一段历史分出来的时间线。点击任意节点切过去；分叉点上方的消息不会被复制。',
          )}
        </p>

        {loading ? (
          <p data-tree-loading className="py-8 text-center text-xs text-ink-faint">
            {t('chat.tree.loading', undefined, '正在取分支树…')}
          </p>
        ) : error ? (
          <p data-tree-error className="py-8 text-center text-xs text-signal-risk">
            {t('chat.tree.error', { error }, '取分支树失败：{error}')}
          </p>
        ) : nodes.length <= 1 ? (
          <p className="py-8 text-center text-xs text-ink-faint">
            {t('chat.tree.empty', undefined, '还没有其他分支。在任意消息上点“从这里分叉”会出现在这里。')}
          </p>
        ) : (
          <ul data-session-tree className="space-y-1">
            {nodes.map((node) => {
              const depth = depthOf.get(node.id) ?? 0
              return (
                <li key={node.id}>
                  <button
                    type="button"
                    data-tree-node={node.id}
                    data-current={node.current ? 'true' : 'false'}
                    onClick={() => onSelect(node.id)}
                    aria-current={node.current ? 'page' : undefined}
                    className={cn(
                      'group flex w-full items-start gap-2 rounded-sm border px-2.5 py-2 text-left',
                      'transition-ui duration-fast focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
                      node.current
                        ? 'border-accent/35 bg-accent/8'
                        : 'border-transparent hover:border-line hover:bg-surface-raised',
                    )}
                    style={{ marginLeft: `${Math.min(depth, 6) * 14}px` }}
                  >
                    <span
                      aria-hidden="true"
                      className={cn(
                        'mt-1.5 h-1.5 w-1.5 shrink-0 rounded-pill',
                        node.current ? 'bg-accent' : 'bg-ink-faint/60',
                      )}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-baseline gap-2">
                        <span
                          className={cn(
                            'truncate text-xs',
                            node.current ? 'font-medium text-ink' : 'text-ink-muted',
                          )}
                        >
                          {node.title || t('chat.a.untitledChat', undefined, '对话')}
                        </span>
                        <span className="shrink-0 text-2xs tabular-nums text-ink-faint">
                          {node.messageCount}
                        </span>
                      </span>
                      <span className="mt-0.5 block truncate text-2xs text-ink-faint">
                        {node.forkedFromSeq
                          ? t(
                              'chat.tree.forkedAt',
                              { seq: String(node.forkedFromSeq) },
                              '从第 {seq} 条消息分叉',
                            )
                          : t('chat.tree.root', undefined, '根会话')}
                        {node.branchSummary
                          ? t('chat.tree.hasSummary', undefined, ' · 含分支摘要')
                          : ''}
                      </span>
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </Modal>
  )
}
