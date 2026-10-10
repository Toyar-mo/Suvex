import { ApiError } from '@/lib/api'
import { cn } from '@/lib/cn'
import { AlertTriangle, RotateCw, WifiOff } from 'lucide-react'

/**
 * 请求失败态 —— 每个数据展示区域失败时都要落到这里。
 *
 * ## 为什么必须区分「连不上」和「后端说不行」
 *
 * 这两种失败的**下一步动作完全不同**，用同一句文案会把人引向错误的方向：
 *
 * | 处境 | `status` | 用户该做什么 |
 * | --- | --- | --- |
 * | 后端没起 / 断网 / 超时 | `0` | 去启动服务 —— 重试按钮是没用的 |
 * | 后端答了 4xx/5xx | 非 0 | 看提示改输入，或稍后重试 |
 *
 * 尤其开发态第一条极常见（忘了起后端），而"加载失败，请重试"会让人
 * 一直点重试。所以网络错误这一支**直接把地址写出来**，那是最快的排查路径。
 *
 * ## 为什么把错误码也显示出来
 *
 * 用户看不懂 `VALIDATION_FAILED`，但**报障时能原样念出来**。
 * 而中文文案可能被改；错误码是契约冻结的，全天候可检索。
 * 它放在最次要的位置（等宽小字、低对比度），不抢主文案。
 */

export interface ErrorStateProps {
  /** 捕获到的任何东西。是 `ApiError` 时信息最全 */
  error: unknown
  /** 重试回调。**不传就不渲染按钮** —— 没有重试路径时给个按钮是骗人的 */
  onRetry?: () => void
  /** 覆盖标题（如"漏洞列表加载失败"） */
  title?: string
  className?: string
  /** 紧凑模式：用于卡片内部，去掉外框和内边距 */
  compact?: boolean
}

export function ErrorState({ error, onRetry, title, className, compact = false }: ErrorStateProps) {
  const apiError = error instanceof ApiError ? error : null
  const isNetwork = apiError?.isNetworkError ?? false

  const Icon = isNetwork ? WifiOff : AlertTriangle
  const heading = title ?? (isNetwork ? '连接不上后端服务' : '加载失败')
  const message =
    error instanceof Error && error.message
      ? error.message
      : '发生了未知错误，请稍后重试。'

  return (
    <div
      // `role="alert"` 而不是 status：失败是需要立刻被告知的事，
      // 而 status 是礼貌的旁白（读屏器可能等到空闲才念）。
      role="alert"
      className={cn(
        'flex flex-col items-start',
        // 非紧凑时给一个虚线框，和空状态同一套视觉语言
        !compact && 'rounded-md border border-dashed border-line-strong p-panel',
        compact && 'py-tight',
        className,
      )}
    >
      <Icon
        size={compact ? 14 : 18}
        strokeWidth={1.75}
        className={cn('shrink-0 text-signal-risk', compact ? 'mt-0.5' : 'mb-tight')}
        aria-hidden="true"
      />

      <h3 className={cn('font-medium text-ink', compact ? 'text-2xs' : 'text-sm')}>{heading}</h3>

      <p className={cn('mt-1 max-w-[68ch] text-ink-muted', compact ? 'text-2xs' : 'text-sm')}>
        {message}
      </p>

      <div className="mt-panel flex flex-wrap items-center gap-panel">
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="press inline-flex items-center gap-tight rounded-sm border border-line-strong bg-surface px-2.5 py-1.5 text-2xs text-ink transition-ui duration-fast hover:bg-surface-raised"
          >
            <RotateCw size={13} strokeWidth={1.75} aria-hidden="true" />
            重试
          </button>
        )}

        {apiError && !isNetwork && (
          <span className="mono-ui text-ink-ghost">{apiError.code}</span>
        )}
      </div>
    </div>
  )
}

export default ErrorState
