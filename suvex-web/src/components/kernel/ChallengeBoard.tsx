import { cn } from '@/lib/cn'
import { copyText } from '@/lib/clipboard'
import { showToast } from '@/data/toast-store'
import {
  AUTO_PRIORITY_BASE,
  CANONICAL_CATEGORY_LABEL,
  CHALLENGE_STATUS_LABEL,
  CHALLENGE_STATUS_TONE,
  ENVIRONMENT_STATUS_LABEL,
  PRIORITY_MANUAL_BASE,
  canHaveWorkOrder,
  canHaveWriteup,
  canonicalCategoryOf,
  type KernelChallenge,
} from '@/data/kernel'
import {
  Check,
  ChevronRight,
  ClipboardList,
  Copy,
  ExternalLink,
  FileText,
  Flag,
  Play,
  ShieldCheck,
  TriangleAlert,
  X,
} from 'lucide-react'
import { Fragment, useState } from 'react'
import { Link } from 'react-router-dom'

/**
 * 题目**看板**：一行一道题。
 *
 * ## 为什么不再是卡片网格（2026-11-08）
 *
 * 比赛现场要做的第一件事是「扫一遍：哪些还没解、哪些快出来了」。
 * 卡片网格一屏只放得下 6–9 道，30 道题要滚很久；而每张卡同时挂着
 * 标题、分类、分值、状态、环境、额度、七个按钮 —— 真正要扫的
 * 两列（状态、分值）被埋在中间。
 *
 * 所以这里换了版面：**一行一道题、列对齐**，一屏能扫完一批。
 * 卡片上那些"偶尔才用"的操作（题解、工单、调优先级、释放环境）
 * 收进**展开行**，不进主视线。
 *
 * ⚠️ 判定/文案与旧卡片保持一致（`canHaveWriteup` / `canHaveWorkOrder` /
 *    `canSolve` 等），换的是版面不是规则。
 *
 * ## 为什么用 `<table>` 而不是 div 网格
 *
 * 列要**对齐**才扫得快，而列对齐正是表格的原生语义。读屏器也会把
 * 行/列关系念出来（`<caption>` + `scope="col"`），这是 div 拼的
 * 网格给不了的。
 */

/** 状态点：颜色只是冗余编码，文字标签永远在（design-system §7） */
const STATUS_DOT: Record<string, string> = {
  idle: 'bg-ink-ghost',
  info: 'bg-signal-info',
  accent: 'bg-accent',
  warn: 'bg-signal-warn',
  ok: 'bg-signal-ok',
  risk: 'bg-signal-risk',
}

/** 排成一行的小动作按钮共用这一份（与主按钮区分：不带底色） */
const QUIET_BUTTON =
  'inline-flex h-6 items-center gap-1 rounded-sm px-1.5 text-2xs text-ink-muted transition-ui duration-fast hover:bg-ink-ghost/15 hover:text-ink disabled:cursor-not-allowed disabled:text-ink-faint disabled:hover:bg-transparent disabled:hover:text-ink-faint'

/**
 * 表头单元格：吸在状态条下方（`top-11` = 状态条 h-11）。
 *
 * ⚠️ `position: sticky` 写在 `th` 上才生效（写在 `tr` 上浏览器不认），
 *    边框也要在这一层画 —— `tr` 的边框会随内容滚走。
 */
const STICKY_TH =
  'sticky top-11 z-10 border-b border-line bg-surface-sunken px-2 py-1.5 font-normal'

const OUTLINE_BUTTON =
  'inline-flex h-6 items-center gap-1 rounded-sm border border-line px-1.5 text-2xs text-ink-muted transition-ui duration-fast hover:border-line-strong hover:text-ink disabled:cursor-not-allowed disabled:border-line/50 disabled:text-ink-faint disabled:hover:border-line/50 disabled:hover:text-ink-faint'

function nextPriorityUp(current: number): number {
  return current >= PRIORITY_MANUAL_BASE ? current + 10 : PRIORITY_MANUAL_BASE
}

function nextPriorityDown(current: number): number {
  return current > PRIORITY_MANUAL_BASE ? current - 10 : 0
}

/** 能不能开始/接着解（与后端 `_assert_resolvable` 同一套判断，见旧卡片说明） */
function canSolve(challenge: KernelChallenge): boolean {
  return ['unsolved', 'failed', 'candidate_ready', 'environment_ready'].includes(
    challenge.status,
  )
}

function canCancel(challenge: KernelChallenge): boolean {
  return ['environment_ready', 'solving', 'submitting'].includes(challenge.status)
}

/**
 * 上一次是「回合用尽」收场 → 可以接着上次的进度跑。
 *
 * 判据只看 `lastError` 的文案（后端 `runner` 写死的格式）。刻意不加
 * 后端字段：那要在列表里为每道题 stat 一次过程文件。语义上"有就接着跑、
 * 没有就从头"本来就是安全的。
 */
function isResumable(challenge: KernelChallenge): boolean {
  return challenge.status === 'failed' && (challenge.lastError ?? '').includes('回合用尽')
}

function solveLabel(challenge: KernelChallenge): string {
  if (isResumable(challenge)) return '接着解'
  return challenge.status === 'failed' ? '再解' : '开始'
}

export function ChallengeBoard({
  challenges,
  busy = false,
  onSolve,
  onSubmit,
  onCancel,
  onRelease,
  onSetPriority,
  onWriteup,
  onWorkOrder,
}: {
  challenges: KernelChallenge[]
  busy?: boolean
  onSolve: (challenge: KernelChallenge, resume?: boolean) => void
  /** 打开确认弹窗（**不是**直接提交） */
  onSubmit: (challenge: KernelChallenge) => void
  onCancel: (challenge: KernelChallenge) => void
  onRelease: (challenge: KernelChallenge) => void
  onSetPriority: (challenge: KernelChallenge, priority: number) => void
  /** 不传时整项不渲染（按需能力） */
  onWriteup?: (challenge: KernelChallenge) => void
  onWorkOrder?: (challenge: KernelChallenge) => void
}) {
  /** 展开的行。同时只开一行 —— 看板的意义是不让详情挤占扫读。 */
  const [openId, setOpenId] = useState<string | null>(null)

  /*
    窄屏才允许横向滚动。⚠️ 不能让 `overflow-x-auto` 一直挂着：
    它会建出一个**滚动容器**，而 `position: sticky` 的表头只在这个
    容器里吸顶 —— 容器本身不纵向滚动，于是表头跟着内容一起滚走
    （实测 `top` 变成 -256px）。桌面端表格放得下，就不需要它。
  */
  return (
    <div className="overflow-x-auto rounded-md border border-line bg-surface md:overflow-visible">
      <table className="w-full table-fixed border-collapse text-xs">
        <caption className="sr-only">
          题目看板：一行一道题。展开某一行查看题面、候选答案与更多操作。
        </caption>
        <thead>
          {/*
            ★ 表头 sticky 在状态条下方（`top-11` = 状态条的高 h-11 = 44px）。
            562 道题的看板要滚很久，没有表头第二屏起就不知道哪列是什么 ——
            而"列对齐"正是这张表存在的理由。sticky 只能写在 `th` 上
            （写在 `tr` 上不生效），边框也得跟着 `th` 走。
          */}
          <tr className="text-2xs font-normal text-ink-faint">
            <th scope="col" className={STICKY_TH + ' w-[84px] text-left'}>
              状态
            </th>
            <th scope="col" className={STICKY_TH + ' text-left'}>
              题目
            </th>
            <th scope="col" className={STICKY_TH + ' hidden w-[68px] text-left md:table-cell'}>
              类型
            </th>
            <th scope="col" className={STICKY_TH + ' w-[52px] text-right'}>
              分值
            </th>
            <th scope="col" className={STICKY_TH + ' hidden w-[26%] text-left xl:table-cell'}>
              候选 / 最后错误
            </th>
            <th scope="col" className={STICKY_TH + ' w-[150px] text-right'}>
              操作
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {challenges.map((challenge) => {
            const open = openId === challenge.id
            const tone = CHALLENGE_STATUS_TONE[challenge.status]
            const flag =
              challenge.status === 'solved' ? challenge.confirmedFlag : challenge.candidateFlag
            const canSubmit = Boolean(challenge.candidateFlag) && challenge.status !== 'solved'
            const outOfQuota = challenge.remainingWrongSubmissions <= 0
            const missingAttachments =
              challenge.attachments.length > 0 && challenge.attachmentsReady === false
            const queued = challenge.status === 'solving'

            return (
              <Fragment key={challenge.id}>
                <tr
                  className={cn(
                    'transition-ui duration-fast hover:bg-surface-sunken',
                    open && 'bg-surface-sunken',
                  )}
                >
                  {/* ── 状态 ── */}
                  <td className="px-2 py-1 align-middle">
                    <span className="flex items-center gap-1.5">
                      <span
                        className={cn('h-1.5 w-1.5 shrink-0 rounded-pill', STATUS_DOT[tone])}
                        aria-hidden="true"
                      />
                      <span className="truncate text-2xs text-ink-muted">
                        {CHALLENGE_STATUS_LABEL[challenge.status]}
                      </span>
                    </span>
                  </td>

                  {/* ── 题目（点标题展开详情） ── */}
                  <td className="px-2 py-1 align-middle">
                    <button
                      type="button"
                      onClick={() => setOpenId(open ? null : challenge.id)}
                      aria-expanded={open}
                      title={challenge.title}
                      className="flex w-full min-w-0 items-center gap-1 text-left"
                    >
                      <ChevronRight
                        size={12}
                        strokeWidth={2}
                        aria-hidden="true"
                        className={cn(
                          'shrink-0 text-ink-ghost transition-transform duration-fast',
                          open && 'rotate-90',
                        )}
                      />
                      <span className="truncate text-xs text-ink">{challenge.title}</span>
                      {missingAttachments && (
                        <TriangleAlert
                          size={11}
                          strokeWidth={2}
                          aria-hidden="true"
                          className="shrink-0 text-signal-warn"
                        />
                      )}
                    </button>
                  </td>

                  {/* ── 类型 ── */}
                  <td className="hidden px-2 py-1 align-middle md:table-cell">
                    <span className="truncate text-2xs text-ink-faint">
                      {CANONICAL_CATEGORY_LABEL[canonicalCategoryOf(challenge.canonicalCategory)]}
                    </span>
                  </td>

                  {/* ── 分值 ── */}
                  <td className="tabular px-2 py-1 text-right align-middle">
                    <span
                      className={cn('text-2xs', challenge.score === null ? 'text-ink-ghost' : 'text-ink-muted')}
                      data-numeric
                    >
                      {challenge.score ?? '—'}
                    </span>
                  </td>

                  {/* ── 候选 / 最后错误（最宽的一列信息） ── */}
                  <td className="hidden px-2 py-1 align-middle xl:table-cell">
                    <span className="flex min-w-0 items-center gap-1">
                      {flag ? (
                        <>
                          {challenge.status === 'solved' ? (
                            <ShieldCheck
                              size={11}
                              strokeWidth={2}
                              aria-hidden="true"
                              className="shrink-0 text-signal-ok"
                            />
                          ) : (
                            <Flag
                              size={11}
                              strokeWidth={2}
                              aria-hidden="true"
                              className="shrink-0 text-signal-warn"
                            />
                          )}
                          <code
                            className={cn(
                              'mono-ui truncate',
                              challenge.status === 'solved'
                                ? 'text-ink-muted'
                                : 'text-ink',
                            )}
                            title={flag}
                          >
                            {flag}
                          </code>
                        </>
                      ) : challenge.lastError ? (
                        <span className="truncate text-2xs text-ink-faint" title={challenge.lastError}>
                          {challenge.lastError}
                        </span>
                      ) : (
                        <span className="text-2xs text-ink-ghost">—</span>
                      )}
                    </span>
                  </td>

                  {/* ── 操作（主操作留在行内，其余进展开行） ── */}
                  <td className="px-2 py-1 align-middle">
                    <div className="flex items-center justify-end gap-1">
                      {canSubmit && (
                        <button
                          type="button"
                          disabled={busy || outOfQuota}
                          onClick={() => onSubmit(challenge)}
                          title={
                            outOfQuota
                              ? '错误提交次数已用尽'
                              : '打开确认弹窗，核对候选答案后再提交（提交消耗平台额度）'
                          }
                          className={cn(
                            'press inline-flex h-6 items-center gap-1 rounded-sm bg-ink px-2 text-2xs font-medium text-canvas',
                            'transition-ui duration-fast hover:bg-ink/85',
                            'disabled:cursor-not-allowed disabled:bg-ink-ghost/30 disabled:text-ink-faint',
                          )}
                        >
                          <ShieldCheck size={11} strokeWidth={2} aria-hidden="true" />
                          确认
                        </button>
                      )}

                      {canSolve(challenge) && (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => onSolve(challenge, isResumable(challenge))}
                          title={
                            isResumable(challenge)
                              ? '接着上一次的进度继续 —— 上次跑过的命令不会重跑'
                              : '开始解这道题'
                          }
                          className={cn(
                            'press inline-flex h-6 items-center gap-1 rounded-sm px-2 text-2xs font-medium',
                            canSubmit
                              ? 'border border-line text-ink-muted hover:border-line-strong hover:text-ink'
                              : 'bg-accent text-canvas hover:bg-accent-strong',
                            'transition-ui duration-fast',
                            'disabled:cursor-not-allowed disabled:bg-accent-dim disabled:text-ink-muted',
                          )}
                        >
                          <Play size={10} strokeWidth={2.5} aria-hidden="true" />
                          {solveLabel(challenge)}
                        </button>
                      )}

                      {canCancel(challenge) && (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => onCancel(challenge)}
                          title="取消这道题（已经在跑的那一轮会跑完，但不会产出候选）"
                          className={QUIET_BUTTON}
                        >
                          <X size={11} strokeWidth={2} aria-hidden="true" />
                          取消
                        </button>
                      )}
                    </div>
                  </td>
                </tr>

                {open && (
                  <tr className="bg-surface-sunken">
                    <td colSpan={6} className="px-3 py-2.5">
                      <ChallengeRowDetail
                        challenge={challenge}
                        busy={busy}
                        queued={queued}
                        outOfQuota={outOfQuota}
                        onSolve={onSolve}
                        onRelease={onRelease}
                        onSetPriority={onSetPriority}
                        onWriteup={onWriteup}
                        onWorkOrder={onWorkOrder}
                      />
                    </td>
                  </tr>
                )}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/**
 * 展开行：题面 + flag 全文 + 那些"偶尔才用"的动作。
 *
 * ★ 动作的**可见性判据**与旧卡片逐字相同 —— 换版面不该改规则。
 *   `题解` 看 `scanId`、`处置工单` 看 `category`，两者刻意不同。
 */
function ChallengeRowDetail({
  challenge,
  busy,
  queued,
  outOfQuota,
  onSolve,
  onRelease,
  onSetPriority,
  onWriteup,
  onWorkOrder,
}: {
  challenge: KernelChallenge
  busy: boolean
  queued: boolean
  outOfQuota: boolean
  onSolve: (challenge: KernelChallenge, resume?: boolean) => void
  onRelease: (challenge: KernelChallenge) => void
  onSetPriority: (challenge: KernelChallenge, priority: number) => void
  onWriteup?: (challenge: KernelChallenge) => void
  onWorkOrder?: (challenge: KernelChallenge) => void
}) {
  const [copied, setCopied] = useState(false)
  const flag =
    challenge.status === 'solved' ? challenge.confirmedFlag : challenge.candidateFlag

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

  return (
    <div className="flex flex-col gap-2">
      {challenge.description && (
        <p className="line-clamp-2 text-xs text-ink-muted">{challenge.description}</p>
      )}

      {challenge.attachments.length > 0 && (
        <p
          className={cn(
            'flex items-center gap-1.5 text-2xs',
            challenge.attachmentsReady === false ? 'text-signal-warn' : 'text-ink-faint',
          )}
        >
          {challenge.attachmentsReady === false && (
            <TriangleAlert size={11} strokeWidth={2} aria-hidden="true" />
          )}
          附件 {challenge.attachments.length} 个
          {challenge.attachmentsReady === false && ' · 还没下载到服务器，模型看不到这些文件'}
        </p>
      )}

      {flag && (
        <div
          className={cn(
            'flex items-center gap-2 rounded-sm border px-2 py-1.5',
            challenge.status === 'solved'
              ? 'border-signal-ok/40 bg-signal-ok/[0.06]'
              : 'border-signal-warn/40 bg-signal-warn/[0.06]',
          )}
        >
          {challenge.status === 'solved' ? (
            <ShieldCheck size={12} strokeWidth={2} aria-hidden="true" className="shrink-0 text-signal-ok" />
          ) : (
            <Flag size={12} strokeWidth={2} aria-hidden="true" className="shrink-0 text-signal-warn" />
          )}
          <span className="shrink-0 text-2xs text-ink-muted">
            {challenge.status === 'solved' ? '已确认的答案' : '候选答案（待确认）'}
          </span>
          <code className="mono-ui min-w-0 flex-1 break-all text-xs text-ink">{flag}</code>
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
        </div>
      )}

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs text-ink-faint">
        <span>
          错误提交 {challenge.wrongSubmissionCount}/{challenge.maxWrongSubmissions}
          {outOfQuota && <span className="ml-1 text-signal-risk">已用尽</span>}
        </span>
        {challenge.environment && (
          <span className="flex items-center gap-1">
            环境 {ENVIRONMENT_STATUS_LABEL[challenge.environment.status]}
            <button
              type="button"
              disabled={busy}
              onClick={() => onRelease(challenge)}
              className="text-ink-muted underline hover:text-ink disabled:cursor-not-allowed disabled:text-ink-faint"
            >
              释放
            </button>
          </span>
        )}
        {challenge.platformName && <span>{challenge.platformName}</span>}
        <span className="mono-ui">{challenge.remoteId}</span>
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        {onWriteup && canHaveWriteup(challenge) && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onWriteup(challenge)}
            title="查看这道题的题解（Markdown）：关键步骤、过程纪要、token 消耗"
            className={OUTLINE_BUTTON}
          >
            <FileText size={11} strokeWidth={1.75} aria-hidden="true" />
            题解
          </button>
        )}

        {onWorkOrder && canHaveWorkOrder(challenge) && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onWorkOrder(challenge)}
            title="查看这次复盘产出的处置建议工单：动作、依据、风险、需要的权限"
            className={OUTLINE_BUTTON}
          >
            <ClipboardList size={11} strokeWidth={1.75} aria-hidden="true" />
            处置工单
          </button>
        )}

        {/* 只有"可以接着解"时才单独给一个从头开始的入口 */}
        {canSolve(challenge) && isResumable(challenge) && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onSolve(challenge, false)}
            title="丢掉上次的进度，从头开始（比如你想让它换个思路）"
            className={OUTLINE_BUTTON}
          >
            重新开始
          </button>
        )}

        {/*
          优先级只在**排队中**显示：影响的是"谁先被认领"，未开始或已解出的
          题根本不在队列里，给个点了没反应的按钮比不给更困惑。
        */}
        {queued && (
          <span className="inline-flex items-center gap-1">
            <button
              type="button"
              disabled={busy}
              onClick={() => onSetPriority(challenge, nextPriorityUp(challenge.priority))}
              title="让这道题在队列里更靠前。低于自动档时一步插到最前 —— 不影响正在跑的"
              className={OUTLINE_BUTTON}
            >
              提前
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onSetPriority(challenge, nextPriorityDown(challenge.priority))}
              title="让这道题在队列里更靠后"
              className={OUTLINE_BUTTON}
            >
              延后
            </button>
            {challenge.priority !== 0 && (
              <span
                className="tabular rounded-xs border border-accent/40 px-1 text-2xs text-accent-strong"
                title={
                  challenge.priority >= AUTO_PRIORITY_BASE &&
                  challenge.priority < PRIORITY_MANUAL_BASE
                    ? '「新题自动优先」给的排队加成（数值 = 1000 + 题目分值）。点「提前」可以插到所有自动题前面。'
                    : '调度优先级，越大越先做。同优先级按先入先出。'
                }
              >
                {challenge.priority >= AUTO_PRIORITY_BASE &&
                challenge.priority < PRIORITY_MANUAL_BASE
                  ? `自动 +${challenge.priority - AUTO_PRIORITY_BASE}`
                  : `+${challenge.priority}`}
              </span>
            )}
          </span>
        )}

        {challenge.scanId && (
          <Link
            to={`/activity?scan=${encodeURIComponent(challenge.scanId)}`}
            className="ml-auto inline-flex items-center gap-1 text-2xs text-accent-strong underline hover:text-accent"
          >
            查看解题过程
            <ExternalLink size={10} strokeWidth={2} aria-hidden="true" />
          </Link>
        )}
      </div>
    </div>
  )
}

export default ChallengeBoard
