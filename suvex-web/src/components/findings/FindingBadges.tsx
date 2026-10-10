import { cn } from '@/lib/cn'
import { useChangeSignal } from '@/hooks/useChangeSignal'
import { SEVERITY_META } from '@/data/findings'
import type { Severity } from '@/data/findings'
import {
  FINDING_STATUS_LABEL,
  FINDING_STATUS_TONE,
  VERIFICATION_LABEL,
  type FindingStatus,
  type Verification,
} from '@/data/finding'
import { CheckCheck, CheckCircle2, CircleDashed, RefreshCw, ShieldCheck, ShieldX } from 'lucide-react'

/**
 * 徽标组件组 —— Findings 页的视觉语言。
 *
 * ★ 关键约束（brief 明确要求）：**不在这里重新发明一套配色。**
 *   严重度色直接读全站唯一的 SEVERITY_META（Overview 的条形图、Activity 的
 *   事件标签用的是同一份）。任何"这一页看起来更协调"的微调都是在制造
 *   第三个事实来源 —— 用户在 Overview 看到红色是 critical，
 *   到 Findings 却变成另一种红，那是最难查的一类不一致。
 */

// ─────────────────────────────────────────────
// 严重度
// ─────────────────────────────────────────────

export interface SeverityBadgeProps {
  severity: Severity
  /** 显示成色点+文字（列表用）还是纯色块（紧凑处用） */
  variant?: 'text' | 'dot'
  className?: string
}

export function SeverityBadge({ severity, variant = 'text', className }: SeverityBadgeProps) {
  const meta = SEVERITY_META[severity]

  if (variant === 'dot') {
    return (
      <span
        className={cn('inline-block h-2 w-2 shrink-0 rounded-pill', meta.bg, className)}
        title={meta.label}
        aria-label={meta.label}
        role="img"
      />
    )
  }

  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center gap-1.5 rounded-xs border px-1.5 py-0.5 text-2xs',
        meta.text,
        meta.border,
        className,
      )}
      // title 给完整定义 —— 界面上只放级别名，但悬停能看到"这意味着什么"
      title={meta.definition}
    >
      <span aria-hidden="true" className={cn('h-1.5 w-1.5 rounded-pill', meta.bg)} />
      {meta.label}
    </span>
  )
}

// ─────────────────────────────────────────────
// 处置状态
// ─────────────────────────────────────────────

/**
 * 状态色映射。
 *
 * FINDING_STATUS_TONE 给出语义色调名，这里映射到具体类。
 * 分开的原因：数据层不该知道 Tailwind 类名。
 */
const STATUS_TONE_CLASS: Record<string, string> = {
  warn: 'border-signal-warn/40 text-signal-warn',
  info: 'border-signal-info/40 text-signal-info',
  ok: 'border-signal-ok/40 text-signal-ok',
  idle: 'border-line text-ink-faint',
}

const STATUS_ICON = {
  new: CircleDashed,
  confirmed: ShieldCheck,
  fixed: CheckCircle2,
  /*
    两个新状态（任务 10）的图标按"动作本身长什么样"选：

    * `retesting` → RefreshCw：复测就是"再跑一遍验证"。
    * `closed` → CheckCheck：双勾而不是单勾。`fixed` 已经用了单勾
      （CheckCircle2）表示"修完了"，关闭是"**验证过**确实好了" ——
      双勾正好表达这个多出来的那一步确认。
  */
  retesting: RefreshCw,
  closed: CheckCheck,
  false_positive: ShieldX,
} as const

export interface StatusBadgeProps {
  status: FindingStatus
  /** 是否显示图标。列表里显示，紧凑处可关 */
  withIcon?: boolean
  className?: string
}

/**
 * 处置状态徽标。
 *
 * ★ 任务 8 补的微反馈：**状态真的改变时，徽标重新落定一次**。
 *
 *   为什么需要它：状态流转是全站最有后果的一类操作（把一条漏洞标成
 *   "误报"或者"已修复"），但它原来的视觉反馈只有"文字换了 + 边框色换了" ——
 *   在满屏徽标的表格里，一个 10px 字的替换非常容易漏看。用户点完
 *   "标记为已修复"，不确定到底成没成。
 *
 *   实现用 `useChangeSignal`：它给出「变过没有」和「第几次变」。
 *   首次挂载时 changed=false → **不挂动画类**，所以一进列表页 13 行徽标
 *   不会同时缩放落定一遍（那正是 §5 禁止的"给每个区块配相同入场动画"）。
 *   只有真正发生流转的徽标会重播。
 *
 *   ★ 已实测确认（见 `useChangeSignal` 头部表格）：
 *     · 冷加载列表 = **0 个**徽标带此动画；
 *     · 流转一条 = 2 个（列表行 + 抽屉标题栏，同一条漏洞的两处渲染）；
 *     · 筛选/清筛选造成的重渲染**不会**让无关行拿到这个类。
 *
 *   动画选 settle-in（160ms，scale 0.86 → 1）而不是 fade：
 *   缩放读起来是"落定/盖章"，正好对应"这个决定生效了"。
 *   单纯淡入的话，在已经有内容的徽标上根本看不出来。
 *
 *   减少动效偏好下由 index.css 的全局兜底压到 0.001ms —— 不动。
 */
export function StatusBadge({ status, withIcon = true, className }: StatusBadgeProps) {
  const Icon = STATUS_ICON[status]
  const tone = FINDING_STATUS_TONE[status]
  const [revision, changed] = useChangeSignal(status)

  return (
    <span
      // 变化时换 key → React 重挂载 → CSS 动画重播。初始挂载不带动画类，所以不闪。
      key={revision}
      className={cn(
        'inline-flex shrink-0 items-center gap-1 rounded-xs border px-1.5 py-0.5 text-2xs',
        changed && 'animate-settle-in',
        STATUS_TONE_CLASS[tone],
        className,
      )}
    >
      {withIcon && <Icon size={10} strokeWidth={2} aria-hidden="true" />}
      {FINDING_STATUS_LABEL[status]}
    </span>
  )
}

// ─────────────────────────────────────────────
// 验证程度
// ─────────────────────────────────────────────

/**
 * 验证程度徽标。
 *
 * 与状态徽标**分开显示**是刻意的（见 data/finding.ts 的说明）：
 * 「验证程度」是 agent 认定的证据强度，「处置状态」是人的决定。
 * 两者合成一个字段会丢掉"已证明存在但判定为误报"这种情况。
 *
 * 视觉上刻意做得比状态弱（无边框、纯文字）：它是补充信息，
 * 不该和"待确认"这种需要行动的状态抢注意力。
 */
const VERIFICATION_CLASS: Record<Verification, string> = {
  verified: 'text-signal-ok',
  partial: 'text-signal-warn',
  none: 'text-ink-faint',
}

export interface VerificationBadgeProps {
  verification: Verification
  className?: string
}

export function VerificationBadge({ verification, className }: VerificationBadgeProps) {
  return (
    <span className={cn('shrink-0 text-2xs', VERIFICATION_CLASS[verification], className)}>
      {VERIFICATION_LABEL[verification]}
    </span>
  )
}
