import { cn } from '@/lib/cn'
import { normalizeFlag, validateFlag, type Ctf2Challenge, type Ctf2SubmitMode } from '@/data/ctf2'
import { AlertTriangle, ChevronLeft, Flag, ShieldCheck } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

/**
 * ★★ 提交 flag 的确认弹窗 —— **这是整个前端最要紧的一处交互。**
 *
 * ## 为什么必须"两次点击"，而不是一个复选框
 *
 * 提交 flag 有三个性质叠在一起，任何一个都足以要求强确认：
 *
 * 1. **不可撤销**。发出去就是发出去了，平台没有"撤回提交"这个动作。
 * 2. **有配额代价**。猜错消耗一次**不可再生**的错误提交机会
 *    （一道题通常只有 3 次，用光只能等下一场或换号）。
 * 3. **发出方是"我们"**。用户点这一下之后，平台侧看到的是他的账号
 *    提交了一个可能是垃圾的字符串 —— 有些比赛对错误提交有额外惩罚。
 *
 * 一个复选框挡不住第 2、3 条：勾选框是**一次肌肉记忆的动作**，
 * 而它旁边那句话在第三次之后就再也没人读了。
 *
 * 所以做成两步，而且**第二步显示的是代价而不是信息**：
 *
 * ```
 * 第 1 步「核对」：题目、候选 flag、题面摘要、还剩几次
 * 第 2 步「确认」：这次会消耗 1 次机会（还剩 N-1 次）→ 提交
 * ```
 *
 * 第二步的存在让"点快了"这个操作**没法一次完成** —— 而它的内容
 * （剩余次数从 N 变 N-1）是把抽象风险变成一个具体数字。
 *
 * ## 焦点管理不是可选项
 *
 * 打开时焦点进取消按钮（不是危险按钮），回车误触时什么都不发生。
 * Escape 关掉。与 `ConfirmDialog` 同一套。
 *
 * ## ★★ 为什么重置靠 `key` 重挂载，而不是一个 `useEffect`（v2）
 *
 * 原来是这样做的：
 *
 * ```js
 * useEffect(() => {
 *   if (!open) return
 *   setStep('review')
 *   setFlag(challenge?.candidateFlag ?? '')
 *   cancelRef.current?.focus()
 *   ...
 * }, [open, challenge?.id, onCancel])
 * ```
 *
 * 它有三个问题，而中间那个**是真的会伤到用户的**：
 *
 * ① **lint 会报错**（`react-hooks/set-state-in-effect`）：在 effect 里同步
 *    `setState` 会引发级联渲染，React 明确不推荐。
 *
 * ② **每次父组件重渲染都会重跑它** —— 因为 `onCancel` 在依赖里，而调用方
 *    传的是内联箭头函数（`onCancel={() => setPending(null)}`），
 *    **每次渲染都是新引用**。于是："用户在第 2 步点「确认提交」→
 *    `runSubmit` 第一行就是 `setBusy(true)` → 父组件重渲染 → 本 effect
 *    重跑 → `setStep('review')` 把他**踢回第 1 步**、`setFlag` 把他编辑过的
 *    输入**重置回候选值**。"
 *
 *    那一刻他看不到"提交中…"，会以为没点上 —— 而这是个**不可撤销、
 *    消耗额度**的动作，让他再点一次是我们最不该制造的局面。
 *    （原注释只防了 `challenge.candidateFlag`，漏了 `onCancel` 同样不稳定。）
 *
 * ③ 那条 `eslint-disable-next-line` **从来没生效过**：它只作用于紧邻的
 *    下一行，而下一行是注释 —— 真正报错的 `}, [...]` 在更下面。所以它
 *    既没抑制 warning，自己还成了一条 unused directive。
 *
 * 现在的做法是 React 对"打开时重置"的推荐解：**让打开变成一次重新挂载**。
 * 外层只判断"该不该渲染"，内层用 `key={challenge.id}` —— 状态用
 * `useState` 的初始值表达，天然就是我们要的重置语义，一个 effect 都不需要。
 * 顺带把 `onCancel` 收进 ref，effect 依赖变空，上面那条链整根断掉。
 */
export function FlagConfirmDialog({
  challenge,
  mode,
  open,
  busy = false,
  onConfirm,
  onCancel,
}: {
  challenge: Ctf2Challenge | null
  /** 当前提交策略 —— 决定第 2 步的一句附注 */
  mode: Ctf2SubmitMode
  open: boolean
  busy?: boolean
  onConfirm: (flag: string) => void
  onCancel: () => void
}) {
  // 关闭时**不挂载内层**：这样"打开"天然就是一次重新挂载，
  // 内层 `useState` 的初始值就是重置后的值。
  if (!open || !challenge) return null
  return (
    <FlagConfirmBody
      // ★ 换一道题才重置。列表是轮询刷新的，若把 `candidateFlag` 之类
      //   放进 key，每轮刷新都会把用户正在编辑的输入覆盖掉。
      key={challenge.id}
      challenge={challenge}
      mode={mode}
      busy={busy}
      onConfirm={onConfirm}
      onCancel={onCancel}
    />
  )
}

/**
 * 弹窗的实体。**由外层用 `key` 控制重挂载** —— 状态重置全部由
 * `useState` 的初始值表达，见 `FlagConfirmDialog` 的说明。
 */
function FlagConfirmBody({
  challenge,
  mode,
  busy,
  onConfirm,
  onCancel,
}: {
  challenge: Ctf2Challenge
  mode: Ctf2SubmitMode
  busy: boolean
  onConfirm: (flag: string) => void
  onCancel: () => void
}) {
  const [step, setStep] = useState<'review' | 'confirm'>('review')
  /*
    ⚠️ 初始值取 `challenge.candidateFlag`（**候选**那一列，不是已确认的），
    但**它是可编辑的**。
    用户经常需要手工修正：模型可能把一个尾随的引号带进来、
    或者把两个候选拼在一起。

    ★ 为什么明确写 `candidateFlag`：这个弹窗的语义是"确认一个**候选**"，
    而 `confirmedFlag` 是平台已经判过对的那个 —— 拿它当初值等于
    让人去"确认"一件已经确定的事（而且那道题本来也不该走到这里）。
  */
  const [flag, setFlag] = useState(challenge.candidateFlag ?? '')
  const cancelButtonRef = useRef<HTMLButtonElement>(null)

  /*
    ★ `onCancel` 走 ref，让下面那个 effect 的依赖是空数组。

    调用方传的是内联箭头函数，引用每次渲染都变 —— 直接放进依赖会让
    "聚焦取消按钮 + 重绑 Escape"每 3 秒（父组件被轮询带着重渲染）跑一次，
    把用户此刻在页面上别处点的焦点硬抢回取消按钮。
  */
  const onCancelRef = useRef(onCancel)
  useEffect(() => {
    onCancelRef.current = onCancel
  }, [onCancel])

  useEffect(() => {
    cancelButtonRef.current?.focus()

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCancelRef.current()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
    // 空依赖是**刻意**的：这个 effect 只负责"挂载时聚焦 + 绑 Escape"，
    // 而这两件事的正确触发时机就是"弹窗被挂载"。
  }, [])

  const cleaned = normalizeFlag(flag)
  const formatError = flag.length > 0 ? validateFlag(flag) : null
  const remaining = challenge.remainingWrongSubmissions
  const outOfQuota = remaining <= 0

  return (
    <div
      onClick={onCancel}
      className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/70 p-gutter backdrop-blur-sm"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="flag-confirm-title"
        onClick={(e) => e.stopPropagation()}
        className="flex w-full max-w-lg animate-rise-in flex-col gap-panel rounded-md border border-line bg-surface-overlay p-panel shadow-overlay"
      >
        <header className="flex items-start gap-2">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-pill bg-signal-warn/15 text-signal-warn">
            <Flag size={14} strokeWidth={1.75} aria-hidden="true" />
          </span>
          <div className="flex min-w-0 flex-col gap-0.5">
            <h2 id="flag-confirm-title" className="text-sm font-medium text-ink">
              {step === 'review' ? '核对要提交的答案' : '确认提交'}
            </h2>
            {/*
              ★ 题目名必须出现。用户可能同时开着好几道题，
                而"提交"是个不可撤销的动作 —— 提交错题的代价
                是白白扣掉**另一道题**的机会。
            */}
            <p className="text-2xs text-ink-muted">
              {challenge.title}
              {challenge.category ? ` · ${challenge.category}` : ''}
            </p>
          </div>
        </header>

        {step === 'review' ? (
          <>
            {/* ── flag 输入（可编辑，默认填当前候选）── */}
            <div className="flex flex-col gap-1.5">
              <label htmlFor="flag-input" className="text-2xs font-medium text-ink">
                要提交的答案
              </label>
              {/*
                ⚠️ 提示里**不能只提 flag**：比赛允许提交命令行 / 文件名 /
                   时间戳 / IP，而用户看到"flag"就会以为自己贴错了。
              */}
              <p className="text-2xs text-ink-faint">
                题目要什么就填什么 —— flag、命令行、文件名、时间戳、IP 都可以。
              </p>
              <input
                id="flag-input"
                value={flag}
                onChange={(e) => setFlag(e.target.value)}
                spellCheck={false}
                autoComplete="off"
                className={cn(
                  'mono-ui h-9 w-full rounded-sm border bg-surface-sunken px-2.5 text-sm text-ink',
                  'transition-ui duration-fast focus:outline-none focus:ring-2 focus:ring-accent/40',
                  formatError ? 'border-signal-risk' : 'border-line hover:border-line-strong',
                )}
              />
              {/*
                ⚠️ 这是**本地**的形状校验（复刻后端的正则）。
                它挡的是"明显不是 flag 的东西"，而**不比后端更严** ——
                被前端拦下的真 flag 用户完全没有补救路径。
              */}
              {formatError && <span className="text-2xs text-signal-risk">{formatError}</span>}
              {!formatError && cleaned !== flag && flag.length > 0 && (
                <span className="text-2xs text-ink-faint">
                  会按 <code className="mono-ui">{cleaned}</code> 提交（去掉了包裹的引号）。
                </span>
              )}
            </div>

            {/* ── 题面摘要 ── */}
            <EvidenceSummary challenge={challenge} />

            {/* ── 额度：第 1 步就把数字摆出来 ── */}
            <QuotaLine remaining={remaining} max={challenge.maxWrongSubmissions} />
          </>
        ) : (
          <>
            {/*
              ★★ 第二步：这里显示的是**代价**，不是"再确认一次"。
                把剩余次数从 N 变成 N-1 是这一步的全部价值 ——
                它把"我可能会猜错"这个抽象风险变成一个具体数字。
            */}
            <div
              className={cn(
                'flex items-start gap-2 rounded-sm border p-2.5',
                outOfQuota
                  ? 'border-signal-risk/40 bg-signal-risk/[0.06]'
                  : 'border-signal-warn/40 bg-signal-warn/[0.06]',
              )}
            >
              <AlertTriangle
                size={13}
                strokeWidth={2}
                className={cn(
                  'mt-0.5 shrink-0',
                  outOfQuota ? 'text-signal-risk' : 'text-signal-warn',
                )}
                aria-hidden="true"
              />
              <div className="flex min-w-0 flex-col gap-1 text-2xs text-ink-muted">
                <span className="text-xs text-ink">
                  即将向 CTF2 提交 <code className="mono-ui break-all">{cleaned}</code>
                </span>
                <span>
                  这一步<strong className="font-medium text-ink">不可撤销</strong>。
                  若平台判定错误，这道题的错误提交机会会从{' '}
                  <span className="tabular text-ink">{remaining}</span> 变成{' '}
                  <span className="tabular text-ink">{Math.max(0, remaining - 1)}</span> 次。
                </span>
                {mode === 'auto' && (
                  <span className="text-ink-faint">
                    （当前策略是自动提交，但这次是你手工发起的。）
                  </span>
                )}
                {outOfQuota && (
                  <span className="text-signal-risk">机会已经用尽，后端会拒绝这次提交。</span>
                )}
              </div>
            </div>

            <details className="rounded-sm border border-line bg-surface-sunken p-2.5">
              <summary className="cursor-pointer text-2xs text-ink-muted">再核对一遍答案</summary>
              <code className="mono-ui mt-1.5 block break-all text-xs text-ink">{cleaned}</code>
            </details>
          </>
        )}

        {/* ── 动作 ── */}
        <footer className="flex items-center justify-end gap-tight">
          <button
            ref={cancelButtonRef}
            type="button"
            onClick={onCancel}
            className={cn(
              'inline-flex h-8 items-center rounded-sm border border-line px-3 text-xs text-ink-muted',
              'transition-ui duration-fast hover:border-line-strong hover:text-ink',
              'focus:outline-none focus:ring-2 focus:ring-accent/40',
            )}
          >
            取消
          </button>

          {step === 'review' ? (
            <button
              type="button"
              disabled={Boolean(formatError) || cleaned.length === 0}
              onClick={() => setStep('confirm')}
              className={cn(
                'inline-flex h-8 items-center gap-1.5 rounded-sm bg-accent px-3 text-xs font-medium text-canvas',
                'transition-ui duration-fast hover:bg-accent-strong',
                'focus:outline-none focus:ring-2 focus:ring-accent/40',
                (Boolean(formatError) || cleaned.length === 0) && 'cursor-not-allowed opacity-50',
              )}
            >
              下一步
            </button>
          ) : (
            <>
              <button
                type="button"
                onClick={() => setStep('review')}
                className="inline-flex h-8 items-center gap-1 rounded-sm px-2 text-xs text-ink-muted transition-ui duration-fast hover:text-ink"
              >
                <ChevronLeft size={12} strokeWidth={2} aria-hidden="true" />
                返回
              </button>
              <button
                type="button"
                disabled={busy || outOfQuota}
                onClick={() => onConfirm(cleaned)}
                className={cn(
                  'inline-flex h-8 items-center gap-1.5 rounded-sm px-3 text-xs font-medium',
                  'transition-ui duration-fast focus:outline-none focus:ring-2 focus:ring-accent/40',
                  outOfQuota
                    ? 'bg-ink-ghost/30 text-ink-faint'
                    : 'bg-signal-risk/90 text-canvas hover:bg-signal-risk',
                  busy && 'cursor-not-allowed opacity-50',
                )}
              >
                <ShieldCheck size={12} strokeWidth={2} aria-hidden="true" />
                {busy ? '提交中…' : '确认提交'}
              </button>
            </>
          )}
        </footer>
      </div>
    </div>
  )
}

/**
 * 题面摘要。
 *
 * ⚠️ 这里**只有题面与附件数** —— 完整解题过程在活动流里。
 * 理由：弹窗是"我要不要按这个按钮"的决策界面，它需要的是
 * **够判断的最小信息**，不是一份文档。把几十行推理塞进来会让
 * 真正要看的那个 flag 被淹掉。
 */
function EvidenceSummary({ challenge }: { challenge: Ctf2Challenge }) {
  const description = challenge.description?.trim()
  return (
    <div className="flex flex-col gap-1 rounded-sm border border-line bg-surface-sunken p-2.5">
      <span className="text-2xs font-medium text-ink">这道题在说什么</span>
      {description ? (
        <p className="line-clamp-3 text-2xs text-ink-muted">{description}</p>
      ) : (
        <p className="text-2xs text-ink-faint">（平台没有给题面。）</p>
      )}
      {challenge.attachments.length > 0 && (
        <span className="text-2xs text-ink-faint">{challenge.attachments.length} 个附件</span>
      )}
      {challenge.candidateCount > 1 && (
        <span className="text-2xs text-ink-faint">
          本次解题共产出 {challenge.candidateCount} 个候选，这里显示的是其中一个。
        </span>
      )}
    </div>
  )
}

/** 额度提示。数字用 `tabular` 对齐，因为它是这一屏里最需要被读准的东西。 */
function QuotaLine({ remaining, max }: { remaining: number; max: number }) {
  const out = remaining <= 0
  return (
    <p
      className={cn(
        'flex items-center gap-1.5 text-2xs',
        out ? 'text-signal-risk' : 'text-ink-muted',
      )}
    >
      <AlertTriangle
        size={11}
        strokeWidth={2}
        className={cn('shrink-0', out ? 'text-signal-risk' : 'text-ink-faint')}
        aria-hidden="true"
      />
      {out ? (
        <span>错误提交机会已用尽（上限 {max} 次）—— 后端会拒绝这次提交。</span>
      ) : (
        <span>
          提交后若判定错误，还剩{' '}
          <span className="tabular text-ink">{Math.max(0, remaining - 1)}</span>/
          <span className="tabular">{max}</span> 次机会。
        </span>
      )}
    </p>
  )
}

export default FlagConfirmDialog
