/**
 * 「查看攻击链」弹窗：把一条对话的消息序列按时间轴摊开。
 *
 * 设计取舍见 `@/lib/attackChain` 的模块注释：这里只渲染**有确定依据**的
 * 步骤，不猜攻击阶段、不做图布局。
 *
 * ⚠️ 数据由调用方传入（消息已在页面里），弹窗自己**不取数**。这样打开它是
 * 瞬时的，也不会出现"弹窗里看到 3 步、聊天记录里 5 条"这种两份数据源打架。
 */

import { cn } from '@/lib/cn'
import { Modal } from '@/components/ui/Modal'
import { buildAttackChain, summarizeChain, type AttackStep } from '@/lib/attackChain'
import type { ChatMessage } from '@/data/chat'

export interface SessionAttackChainModalProps {
  open: boolean
  onClose: () => void
  sessionTitle: string
  messages: readonly ChatMessage[]
  /** 消息还在路上。为空时用于区分"没内容"和"还没到"。 */
  loading?: boolean
  error?: string | null
}

const KIND_TONE: Record<AttackStep['kind'], { dot: string; text: string; label: string }> = {
  ask: { dot: 'bg-accent', text: 'text-ink', label: '提问' },
  plan: { dot: 'bg-ink-faint', text: 'text-ink-muted', label: '决策' },
  tool: { dot: 'bg-signal-info', text: 'text-ink', label: '工具' },
  answer: { dot: 'bg-signal-ok', text: 'text-ink', label: '回答' },
}

export function SessionAttackChainModal({
  open,
  onClose,
  sessionTitle,
  messages,
  loading = false,
  error = null,
}: SessionAttackChainModalProps) {
  const steps = buildAttackChain(messages)
  const summary = summarizeChain(steps)

  return (
    <Modal
      open={open}
      onClose={onClose}
      widthClass="max-w-3xl"
      /*
        ★ `title` 保持**纯字符串**：`Modal` 只在 title 是字符串时才设
          `aria-label`，换成 ReactNode 会让弹窗失去可访问名（读屏器读不出
          "这是什么弹窗"）。摘要就写在括号里，一行说清。
      */
      title={`攻击链（${summary.asks} 问 · ${summary.tools} 次工具${summary.failed > 0 ? ` · ${summary.failed} 次失败` : ''}）`}
    >
      <div className="space-y-3">
        <p className="text-2xs text-ink-faint">
          会话「{sessionTitle}」的步骤序列。按时间顺序，从聊天记录还原 —— 不做语义推断。
        </p>

        {loading ? (
          <p data-chain-loading className="py-8 text-center text-xs text-ink-faint">
            正在取这个会话的消息…
          </p>
        ) : error ? (
          <p data-chain-error className="py-8 text-center text-xs text-signal-risk">
            取消息失败：{error}
          </p>
        ) : steps.length === 0 ? (
          <p className="py-8 text-center text-xs text-ink-faint">
            这个对话还没有内容，没有可展示的步骤。
          </p>
        ) : (
          <ol data-attack-chain className="relative space-y-0 border-l border-line pl-4">
            {steps.map((step) => {
              const tone = KIND_TONE[step.kind]
              return (
                <li
                  key={step.seq}
                  data-step-kind={step.kind}
                  data-step-failed={step.failed ? 'true' : 'false'}
                  className="relative py-2"
                >
                  <span
                    aria-hidden="true"
                    className={cn(
                      'absolute -left-[21px] top-3 h-2 w-2 rounded-pill ring-2 ring-surface',
                      step.failed ? 'bg-signal-risk' : tone.dot,
                    )}
                  />
                  <div className="flex items-baseline gap-2">
                    <span
                      className={cn(
                        'text-2xs uppercase tracking-wide',
                        step.failed ? 'text-signal-risk' : 'text-ink-faint',
                      )}
                    >
                      {tone.label}
                    </span>
                    <span className={cn('text-xs font-medium', tone.text)}>{step.label}</span>
                    {step.failed && (
                      <span className="text-2xs text-signal-risk" title="这一步失败了">
                        失败
                      </span>
                    )}
                  </div>
                  <p className="mt-1 break-words text-2xs leading-relaxed text-ink-muted">
                    {step.detail || '（无内容）'}
                  </p>
                </li>
              )
            })}
          </ol>
        )}

        {summary.toolNames.length > 0 && (
          <p className="text-2xs text-ink-faint">用到的工具：{summary.toolNames.join('、')}</p>
        )}
      </div>
    </Modal>
  )
}
