import { cn } from '@/lib/cn'
import { copyText } from '@/lib/clipboard'
import {
  CHALLENGE_STATUS_LABEL,
  CHALLENGE_STATUS_TONE,
  loadKernel,
  submitFlag,
  useKernel,
  type KernelChallengeStatus,
  type KernelSubmitMode,
} from '@/data/kernel'
import type { ChatChallengeRef } from '@/data/chat'
import { showToast } from '@/data/toast-store'
import { errorMessage } from '@/lib/api'
import { FlagConfirmDialog } from '@/components/kernel'
import { Check, Copy, Flag, ShieldCheck, TriangleAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

/**
 * 对话页顶部的**题目条**（CTF 页重做第 2 步）。
 *
 * ## 为什么它常驻在消息流上方
 *
 * 从看板点「查看」进来的人是带着一个问题来的：「这道题现在什么状态、
 * 有没有候选、要不要我确认」。翻回看板去查答案，会把他从正在读的过程里
 * 拽走。所以这条窄条固定在顶部，滚动消息流时不动。
 *
 * ## 为什么提交在这里，而不是只给一个"回看板"链接
 *
 * 候选 flag 是跟着**过程**一起出现的：模型跑完、消息流里写着它怎么算出来的，
 * 底下这条就冒出候选。此刻"确认提交"就在手边，才不用切页面再找那道题。
 * 提交仍然走 `FlagConfirmDialog` 的两步确认 —— 这里只是入口换了位置。
 *
 * ## 数据来源
 *
 * 会话详情只带五项只读投影（见 `ChatChallengeRef`）；确认弹窗需要
 * 额度/题面等更多字段，所以这里订阅看板的 store，按 id 找回完整对象。
 * 刚打开对话页时 store 可能是空的 —— 补一次 `loadKernel`。
 * `submitFlag` 自己会再刷新一次 store，所以提交完这条的状态会跟着变。
 */
export function ChallengeBar({ challenge }: { challenge: ChatChallengeRef }) {
  const { challenges, settings } = useKernel()
  const [pending, setPending] = useState(false)
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)

  const full = challenges.find((item) => item.id === challenge.id) ?? null

  /*
    补一次列表：直接打开/刷新对话页时 store 里还没有题目。
    依赖写 `full?.id` 而不是 `full`：后者每次刷新都是新对象引用，
    会让这个 effect 跟着每 3 秒的轮询重跑一遍。
  */
  useEffect(() => {
    if (!full) void loadKernel({ silent: true })
  }, [full?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const status = (full?.status ?? challenge.status) as KernelChallengeStatus
  const tone = CHALLENGE_STATUS_TONE[status]
  /* 按状态取**不同的列**，与看板同一条规矩：数据异常时表现为
     "已解出却没有 flag"，那是看得见的，而不是把候选冒充成答案。 */
  const flag =
    status === 'solved'
      ? full?.confirmedFlag ?? challenge.confirmedFlag
      : full?.candidateFlag ?? challenge.candidateFlag
  const score = full?.score ?? challenge.score
  const canSubmit = Boolean(flag) && status !== 'solved' && full !== null

  const copyFlag = () => {
    if (!flag) return
    void copyText(flag).then((ok) => {
      if (!ok) {
        showToast({ message: '复制失败，请手动选中后按 Ctrl+C', tone: 'risk' })
        return
      }
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    })
  }

  const runSubmit = (value: string) => {
    if (!full) return
    setBusy(true)
    void submitFlag(full.id, value, (settings?.submitMode ?? 'safe') as KernelSubmitMode)
      .then((result) => {
        setPending(false)
        showToast({
          message: result.message,
          tone:
            result.status === 'correct' ? 'ok' : result.status === 'wrong' ? undefined : 'risk',
        })
      })
      .catch((err) => showToast({ message: `提交失败：${errorMessage(err)}`, tone: 'risk' }))
      .finally(() => setBusy(false))
  }

  return (
    <>
      <div className="flex shrink-0 items-center gap-2 border-b border-line bg-surface-sunken px-3 py-1.5">
        <span className="flex shrink-0 items-center gap-1.5">
          <span
            className={cn(
              'h-1.5 w-1.5 rounded-pill',
              tone === 'ok' && 'bg-signal-ok',
              tone === 'risk' && 'bg-signal-risk',
              tone === 'warn' && 'bg-signal-warn',
              tone === 'accent' && 'bg-accent',
              tone === 'info' && 'bg-signal-info',
              tone === 'idle' && 'bg-ink-ghost',
            )}
            aria-hidden="true"
          />
          <span className="text-2xs text-ink-muted">{CHALLENGE_STATUS_LABEL[status]}</span>
        </span>

        <span className="min-w-0 flex-1 truncate text-xs text-ink" title={challenge.title}>
          {challenge.title}
        </span>

        {score !== null && (
          <span className="tabular shrink-0 text-2xs text-ink-faint">{score} 分</span>
        )}

        {flag && (
          <span className="flex min-w-0 max-w-[38%] items-center gap-1">
            {status === 'solved' ? (
              <ShieldCheck size={11} strokeWidth={2} aria-hidden="true" className="shrink-0 text-signal-ok" />
            ) : (
              <Flag size={11} strokeWidth={2} aria-hidden="true" className="shrink-0 text-signal-warn" />
            )}
            <code className="mono-ui truncate text-2xs text-ink" title={flag}>
              {flag}
            </code>
            <button
              type="button"
              onClick={copyFlag}
              aria-label="复制答案"
              className="shrink-0 rounded-xs p-0.5 text-ink-faint transition-ui duration-fast hover:text-ink"
            >
              {copied ? (
                <Check size={11} strokeWidth={2} aria-hidden="true" className="text-signal-ok" />
              ) : (
                <Copy size={11} strokeWidth={2} aria-hidden="true" />
              )}
            </button>
          </span>
        )}

        {canSubmit && (
          <button
            type="button"
            disabled={busy}
            onClick={() => setPending(true)}
            title="打开确认弹窗，核对候选答案后再提交（提交消耗平台额度）"
            className={cn(
              'press inline-flex h-6 shrink-0 items-center gap-1 rounded-sm bg-ink px-2 text-2xs font-medium text-canvas',
              'transition-ui duration-fast hover:bg-ink/85',
              'disabled:cursor-not-allowed disabled:bg-ink-ghost/30 disabled:text-ink-faint',
            )}
          >
            <ShieldCheck size={11} strokeWidth={2} aria-hidden="true" />
            确认提交
          </button>
        )}

        <Link
          to="/kernel"
          className="shrink-0 text-2xs text-accent-strong underline hover:text-accent"
        >
          看板
        </Link>
      </div>

      {full && (
        <FlagConfirmDialog
          open={pending}
          challenge={full}
          mode={(settings?.submitMode ?? 'safe') as KernelSubmitMode}
          busy={busy}
          onConfirm={runSubmit}
          onCancel={() => setPending(false)}
        />
      )}
    </>
  )
}

/**
 * 关联的题目已被删（重新同步 / 换接入）时的题目条。
 *
 * ★ 不把整条藏掉：藏掉之后，这段对话看起来就是一条**没有上下文的**聊天，
 *   而用户记得自己是从某道题点进来的。"题目已不存在"至少解释了这个落差。
 */
export function MissingChallengeBar() {
  return (
    <div className="flex shrink-0 items-center gap-2 border-b border-line bg-surface-sunken px-3 py-1.5 text-2xs text-ink-faint">
      <TriangleAlert size={12} strokeWidth={2} aria-hidden="true" />
      关联的题目已不存在（可能已重新同步或换了接入）—— 这段对话仍然保留。
      <Link to="/kernel" className="ml-auto text-accent-strong underline hover:text-accent">
        看板
      </Link>
    </div>
  )
}

export default ChallengeBar
