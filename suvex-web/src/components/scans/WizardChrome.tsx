import { cn } from '@/lib/cn'
import { AlertTriangle, Check, Loader2, Rocket } from 'lucide-react'

/**
 * 启动扫描按钮 —— 这是全站**风险最高**的一个操作。
 *
 * 为什么需要专门做它的四态：
 *   点下去会真的对目标发起主动扫描（发包、尝试利用）。误触的代价不是
 *   "多一次点击"，而是**可能触发目标的告警、甚至影响生产环境**。
 *   所以这个按钮的视觉必须明确回答"我现在能点吗、点了会怎样"。
 *
 * 四态的视觉区分（不是只换个颜色）：
 *   默认   : 实心主色 + 明确动词「启动扫描」
 *   hover  : 提亮 + 轻微上浮（收到"响应了"的反馈）
 *   loading: 旋转图标 + 文案改成进行时「正在启动…」+ 禁用重复点击
 *   disabled: 中性灰底 + 无 hover 反馈 + 光标 not-allowed
 *             **且旁边必须有一句话说明为什么不能点** ——
 *             一个灰掉的按钮不说原因，是表单里最让人恼火的东西。
 *
 * 另外：从"确认"到"真的启动"中间隔了一次显式确认（见 NewScanWizard），
 * 而不是按下即执行。高风险操作值得多一步。
 */

export interface LaunchButtonProps {
  /** 不能点时的原因。有值 = disabled；null = 可点 */
  disabledReason: string | null
  loading?: boolean
  onClick: () => void
  /** 已完成的步骤数，用于确认页显示摘要 */
  label?: string
}

export function LaunchButton({
  disabledReason,
  loading = false,
  onClick,
  label = '启动扫描',
}: LaunchButtonProps) {
  const disabled = disabledReason !== null || loading

  return (
    <div className="flex flex-col items-end gap-1.5">
      <button
        type="button"
        onClick={onClick}
        disabled={disabled}
        aria-disabled={disabled}
        aria-busy={loading}
        className={cn(
          'press-lg inline-flex h-11 items-center gap-2 rounded-sm px-4 text-sm font-medium',
          /*
            ★ 任务 8 改：`transition-all` → `transition-ui`，并去掉 hover 上浮。

            原先写的是 `transition-all duration-fast` + `hover:-translate-y-px`。
            两处都不对：

            1. **`transition-all` 会把 width/height/margin 一起纳入过渡。**
               这是个宽度由内容决定的按钮，文案会在「启动扫描」和「正在启动…」
               之间切换 —— 于是按钮宽度变化也被动画化，在实时反馈上多出一截
               看不清的抖动。`transition-ui` 只过渡颜色/透明度/transform，
               精确命中这里真正要动的东西。

            2. **hover 上浮是炫技，而且方向和语义相反。**
               上浮传达的是"轻快、可随意点"，但这是全站风险最高的操作
               （点下去真的会对目标发包）。它需要的反馈是"按下去有分量"，
               不是"飘起来"。改成**按压下沉**（press-lg 的 scale 0.985）——
               方向正确，且不需要用户先悬停上去才感觉得到。
          */
          'transition-ui duration-fast',
          // 默认：实心主色 + 深色字（对比度 5.98:1，见 design-system §7）
          !disabled && 'bg-accent text-canvas hover:bg-accent-strong',
          // loading：保持主色但停止响应，光标等待
          loading && 'cursor-wait bg-accent-deep text-ink',
          // disabled：中性灰，且**去掉所有 hover 反馈** ——
          // 有反馈但点不动，比完全没反馈更让人困惑
          disabled && !loading && 'cursor-not-allowed bg-surface-raised text-ink-faint',
        )}
      >
        {loading ? (
          <>
            <Loader2 size={15} strokeWidth={2} className="animate-spin" aria-hidden="true" />
            正在启动…
          </>
        ) : (
          <>
            <Rocket size={15} strokeWidth={1.75} aria-hidden="true" />
            {label}
          </>
        )}
      </button>

      {/* 为什么不能点 —— 必须给出原因，而不是让用户猜 */}
      {disabledReason && !loading && (
        <p className="flex items-center gap-1 text-2xs text-signal-warn">
          <AlertTriangle size={11} strokeWidth={2} aria-hidden="true" />
          {disabledReason}
        </p>
      )}
    </div>
  )
}

/**
 * 分步进度指示。
 *
 * 形态选择：**横向的编号 + 标签 + 连接线**，不是进度条。
 *   进度条只表达"完成百分之多少"，而分步表单里用户需要知道的是
 *   "一共几步、现在第几步、已经填好的是哪几步" —— 那需要离散的标记。
 *
 * 已完成的步骤可点击回退（改前面的选择不用一路点回去），
 * 未到的步骤不可点（跳过去会跳过必填项）。
 */
export interface StepIndicatorProps {
  steps: { id: string; label: string }[]
  currentIndex: number
  /** 已完成的步骤索引集合 */
  completed: Set<number>
  onJump: (index: number) => void
}

export function StepIndicator({ steps, currentIndex, completed, onJump }: StepIndicatorProps) {
  return (
    <ol className="flex items-center gap-1" aria-label="新建扫描步骤">
      {steps.map((step, index) => {
        const isCurrent = index === currentIndex
        const isDone = completed.has(index)
        // 只能回到已完成的步骤 —— 往前的步骤有依赖，跳过去会缺内容
        const canJump = isDone && !isCurrent

        return (
          <li key={step.id} className="flex min-w-0 items-center">
            {index > 0 && (
              <span
                aria-hidden="true"
                className={cn(
                  'mx-1 h-px w-4 shrink-0 sm:w-8',
                  isDone || isCurrent ? 'bg-accent/50' : 'bg-line',
                )}
              />
            )}

            <button
              type="button"
              onClick={() => canJump && onJump(index)}
              disabled={!canJump && !isCurrent}
              aria-current={isCurrent ? 'step' : undefined}
              className={cn(
                'flex min-w-0 items-center gap-1.5 rounded-sm px-1.5 py-1 text-xs',
                'press transition-ui duration-fast',
                canJump && 'hover:bg-surface-raised',
                !canJump && !isCurrent && 'cursor-default',
              )}
            >
              <span
                aria-hidden="true"
                className={cn(
                  'tabular flex h-5 w-5 shrink-0 items-center justify-center rounded-pill border text-2xs',
                  isDone && 'border-accent bg-accent text-canvas',
                  isCurrent && !isDone && 'border-accent text-accent-strong',
                  !isDone && !isCurrent && 'border-line text-ink-faint',
                )}
              >
                {isDone ? <Check size={11} strokeWidth={3} /> : index + 1}
              </span>

              {/* 窄屏只显示当前步的标签 —— 四个都显示会被压成不可读 */}
              <span
                className={cn(
                  'truncate',
                  isCurrent ? 'text-ink' : isDone ? 'text-ink-muted' : 'text-ink-faint',
                  // 非当前步在窄屏隐藏文字，只留圆点
                  !isCurrent && 'hidden sm:inline',
                )}
              >
                {step.label}
              </span>
            </button>
          </li>
        )
      })}
    </ol>
  )
}
