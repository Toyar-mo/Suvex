import { cn } from '@/lib/cn'
import EnvironmentStatus from './EnvironmentStatus'
import {
  AUTO_PRIORITY_BASE,
  CHALLENGE_STATUS_LABEL,
  CHALLENGE_STATUS_TONE,
  PRIORITY_MANUAL_BASE,
  SANDBOX_PROFILE_LABEL,
  canHaveWriteup,
  type Ctf2Challenge,
} from '@/data/ctf2'
import { showToast } from '@/data/toast-store'
import { copyText } from '@/lib/clipboard'
import { Check, ChevronDown, ChevronUp, Copy, ExternalLink, FileText, Flag, Percent, Play, ShieldCheck, X } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'

/**
 * 一道题的卡片。
 *
 * ## ★ 为什么是**卡片网格**，而扫描列表是**行**（而这一页仿的是 Scans 页）
 *
 * 两者的数据形状不同，正确的版面就不同：
 *
 * | | 扫描 | CTF2 题目 |
 * | --- | --- | --- |
 * | 本质 | **时序记录**（有开始、有结束、要按时间排） | **并列对象**（分类、分值、状态、额度、环境） |
 * | 一眼要看的 | 状态 + 目标 + 时间 | 五六**个并列**属性 |
 * | 正确的版面 | 每行一条，列对齐 | 每张卡一块，属性分块 |
 *
 * 硬套列表的代价是每行要塞五六个小标签，而它们在窄屏上会折行 ——
 * 折行之后列对齐就没了，列表唯一的优势（纵向扫读）也随之消失。
 *
 * 所以"仿 Scans 页"仿的是**骨架**（页面头 → 工具条 → 内容区 → 空状态），
 * 内容区按自己的数据形状来。
 *
 * ## ★ `确认提交` 不在卡片上直接提交
 *
 * 它只**打开确认弹窗**。flag 提交不可撤销且消耗不可再生额度，
 * 所以它绝不能是一个单击就生效的按钮 —— 见 `FlagConfirmDialog`。
 */
/**
 * 「提前」一步的结果。
 *
 * ★ 低于人工档时**一步跳到 `PRIORITY_MANUAL_BASE`**，而不是 +10。
 *
 * 原因是一个我们自己引入的回归：自动优先（v0.24）把新题推到
 * 1000..1999，而「提前」原本每次只加 10 —— 用户点一百多次也追不上一道
 * 1500 的新题，那个按钮会从"有用"变成"点了没反应"。
 *
 * 跳到 2000（= 自动档上限 +1）之后，再点就恢复 +10 的微调语义。
 */
function nextPriorityUp(current: number): number {
  return current >= PRIORITY_MANUAL_BASE ? current + 10 : PRIORITY_MANUAL_BASE
}

/**
 * 「延后」一步的结果。
 *
 * `PRIORITY_MANUAL_BASE`（刚用「提前」插过队）→ 0（退回默认排队），
 * 其余 -10。
 *
 * 为什么不做 `2000 - 10 = 1990`：那个值仍然排在**几乎所有自动题**前面
 * （自动题里分值最高的那道是 1999），于是"延后"看起来毫无效果。
 * 退回 0 是用户此刻真正想要的那件事 —— 别再插队了。
 */
function nextPriorityDown(current: number): number {
  return current > PRIORITY_MANUAL_BASE ? current - 10 : 0
}

export function ChallengeCard({
  challenge,
  onSubmit,
  onSolve,
  onCancel,
  onRelease,
  onSetPriority,
  onWriteup,
  busy = false,
}: {
  challenge: Ctf2Challenge
  /** 打开确认弹窗（**不是**直接提交） */
  onSubmit: (challenge: Ctf2Challenge) => void
  /**
   * 开始/继续解题。
   *
   * ★ v0.26 多了 `resume`：上一次是「回合用尽」收场时，可以**接着上次
   * 的进度**跑，而不是从零开始。实测（`[FlareOn3]GUI`）：那道 .NET 逆向题
   * 跑满 40 轮、发出 37 条各不相同的命令仍未做完 —— 那 37 步是真金白银
   * 换来的，重来一遍太亏。
   *
   * ⚠️ 但"重来"也是真实需求（有时就是想换个思路），所以两个动作都留着。
   */
  onSolve: (challenge: Ctf2Challenge, resume?: boolean) => void
  onCancel: (challenge: Ctf2Challenge) => void
  onRelease: (challenge: Ctf2Challenge) => void
  /**
   * 调整调度优先级（v0.23）。
   *
   * ★ 只在题**排队中**（`solving`）时才会被调用 —— 优先级影响的是
   *   "谁先被认领"，而没在队列里的题点了不会有任何效果。
   */
  onSetPriority: (challenge: Ctf2Challenge, priority: number) => void
  /**
   * 查看题解（v0.36）。
   *
   * ★ **只在跑完过的题上出现**（判据是 `canHaveWriteup`）—— 入口本身
   *   就是一句承诺，而题解要到解题**收尾**时才生成。给一道从没跑过的
   *   题摆这个按钮，等于让用户点一次才知道后端会回 409。
   *
   * 不传时整项不渲染 —— 与 `ScanList` 的 `onWriteup` 同一个约定
   * （按需能力，而不是"必须实现"的接口）。
   */
  onWriteup?: (challenge: Ctf2Challenge) => void
  busy?: boolean
}) {
  const [copied, setCopied] = useState(false)
  const tone = CHALLENGE_STATUS_TONE[challenge.status]

  /**
   * 这道题**在不在队列里**（决定要不要给优先级控件）。
   *
   * ⚠️ `solving` 既包含"排队等认领"也包含"已经在跑"。两者的区别在
   *    服务端（认领之后就不受排序影响了），而前端拿不到那个状态 ——
   *    所以这里显示控件，靠后端保证"改优先级不会打断正在跑的"。
   *    这是刻意的：宁可在"已经跑起来"的那道题上多显示一个按钮，
   *    也不要因为区分不出来而让真正排队中的题没法插队。
   */
  const queued = challenge.status === 'solving'

  /**
   * 卡片上展示的那个 flag。
   *
   * ★ 按状态取**不同的列**，而不是"哪个有值就取哪个"。
   *   服务端保证这两列互斥（判定一出候选就被清掉），所以在正常数据上
   *   两种写法结果相同 —— 但"哪个有值取哪个"会在数据异常时**掩盖**
   *   异常（把候选当成答案显示出来）。按状态取的话，异常表现为
   *   "已解出却没有 flag"，那是看得见的。
   */
  const shownFlag =
    challenge.status === 'solved' ? challenge.confirmedFlag : challenge.candidateFlag

  const copyFlag = () => {
    if (!shownFlag) return
    // `navigator.clipboard` 在非安全上下文里**不存在**（本系统按 http +
    // 局域网 IP 部署，实测），走降级链。见 `@/lib/clipboard` 的说明。
    void copyText(shownFlag).then((ok) => {
      if (!ok) {
        showToast({ message: '复制失败，请手动选中后按 Ctrl+C', tone: 'risk' })
        return
      }
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    })
  }

  /**
   * 能不能开始解题。
   *
   * ⚠️ 与后端的 `_assert_resolvable` 是**同一套判断**，但这里只用来
   *   **禁用按钮并说明原因** —— 真正生效的是后端（前端校验不是安全边界）。
   *   两处都写是刻意的：这里让用户不必点一次才知道不行，
   *   而那边保证"绕开前端也拦得住"。
   */
  /*
    ★ v0.26 补上 `environment_ready`。

    它**不是**一个瞬间的中间态：worker 起完环境就设它，然后跑几分钟到
    几十分钟的解题循环 —— 也就是说它是**解题过程的主状态**。

    而它原来不在这里，于是（2026-10-02 实测）一道 worker 因为服务重启
    被杀掉的题会永远卡住：后端认为可解、前端不给按钮、用户只剩「取消」。

    ⚠️ 重复点击是**安全**的：`start_solve` 把状态推回 `solving`，而调度器
       认领时发现这道题已经在跑就会跳过（`_claim` 里那个判断）。
  */
  const canSolve = [
    'unsolved',
    'failed',
    'candidate_ready',
    'environment_ready',
  ].includes(challenge.status)
  const canCancel = ['environment_ready', 'solving', 'submitting'].includes(challenge.status)
  // ★ 只有**候选**才让"确认提交"可用：已解出的题不该再提交（后端也会拒）。
  //   用 `candidateFlag` 而不是展示用的那个值，是为了让"按钮可以点"
  //   与"有一个待确认的候选"逐字对应。
  const canSubmit = Boolean(challenge.candidateFlag) && challenge.status !== 'solved'
  const outOfQuota = challenge.remainingWrongSubmissions <= 0
  /*
    ★ 上一次是「回合用尽」收场 → 可以**接着那次**跑（v0.26）。

    判据只看 `lastError` 的文案，它是后端 `runner` 写死的格式
    （`回合用尽（N 轮）仍未给出结论。`）。

    ⚠️ 这里刻意**不**为它加一个后端字段：那个字段要在列表里为每道题
       判断一次"过程文件在不在"，而 101 道题就是 101 次磁盘 stat。
       而"有就接着跑、没有就从头"在语义上本来就是安全的 ——
       后端找不到过程文件时会退回从头开始并记一条日志。
  */
  const resumable =
    challenge.status === 'failed' && (challenge.lastError ?? '').includes('回合用尽')

  return (
    <article className="flex flex-col gap-panel rounded-md border border-line bg-surface p-panel">
      {/* ── 头部：标题 + 状态 ── */}
      <header className="flex items-start justify-between gap-tight">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h3 className="text-sm text-ink">{challenge.title}</h3>
          <div className="flex flex-wrap items-center gap-x-tight gap-y-0.5 text-2xs text-ink-faint">
            <span>{challenge.category || '未分类'}</span>
            {challenge.score !== null && (
              <>
                <span aria-hidden="true">·</span>
                <span className="tabular">{challenge.score} 分</span>
              </>
            )}
            <span aria-hidden="true">·</span>
            <span className="mono-ui">{SANDBOX_PROFILE_LABEL[challenge.sandboxProfile]}</span>
          </div>
        </div>

        <span
          className={cn(
            'shrink-0 rounded-pill px-2 py-0.5 text-2xs',
            /*
              ★ 2026-10-16：`ok` 与 `warn` 两档的文字色从 tone 色改成 `text-ink`。

              这两档在**亮色主题**下不达 AA 4.5:1（实测，对三种承载面取最差）：
                · ok   原 `text-signal-ok`   = 3.86:1（sunken 承载面）
                · warn 原 `text-signal-warn` = 3.90:1
              原因是亮色的 `signal-ok #127A4B` / `signal-warn #9A5B00` 本身
              就只有 5.37 / 5.43:1（见 `tailwind.config.js` 里 signal 那段），
              再叠一层同色 wash 就把余量吃光了 —— 这是**结构性**的，
              不是取值没调好：把 wash 调到 `ok/6` 也才 4.48:1。

              所以文字改用 `text-ink`（最差 9.92:1 暗 / 12.84:1 亮）。
              **底色一律没动** —— 语义仍由底色、状态点、文字标签三者共同承载
              （design-system §7「颜色是冗余编码，不是唯一载体」）。

              ⚠️ 另外三档（risk / info / accent）与 idle 保持原样：它们实测
              全部 ≥4.5:1，而「signal 色出现在徽标上」正是 §2 认可的用法，
              没必要为了一致性去动通过的分支。
              `EnvironmentStatus` 的徽标做了**完全相同**的两处改动，
              于是全站两个带文字的状态徽标共用同一条规则。
            */
            tone === 'ok' && 'bg-signal-ok/15 text-ink',
            tone === 'risk' && 'bg-signal-risk/15 text-signal-risk',
            tone === 'warn' && 'bg-signal-warn/15 text-ink',
            tone === 'accent' && 'bg-accent-wash text-accent-strong',
            tone === 'info' && 'bg-signal-info/15 text-signal-info',
            tone === 'idle' && 'bg-ink-ghost/20 text-ink-faint',
          )}
        >
          {CHALLENGE_STATUS_LABEL[challenge.status]}
        </span>
      </header>

      {/* ── 题面（截断到三行：完整题面在详情里，卡片只需要够判断）── */}
      {challenge.description && (
        <p className="line-clamp-3 text-xs text-ink-muted">{challenge.description}</p>
      )}

      {/* ── 环境 ── */}
      <EnvironmentStatus
        environment={challenge.environment}
        compact
        onRelease={() => onRelease(challenge)}
      />

      {/* ── flag：候选（待确认）或已确认，两种状态在视觉上分开 ── */}
      {shownFlag && (
        <div
          className={cn(
            'flex flex-col gap-1 rounded-sm border p-2.5',
            challenge.status === 'solved'
              ? 'border-signal-ok/40 bg-signal-ok/[0.06]'
              : 'border-signal-warn/40 bg-signal-warn/[0.06]',
          )}
        >
          <div className="flex items-center gap-1.5">
            {challenge.status === 'solved' ? (
              <ShieldCheck
                size={12}
                strokeWidth={2}
                className="text-signal-ok"
                aria-hidden="true"
              />
            ) : (
              <Flag size={12} strokeWidth={2} className="text-signal-warn" aria-hidden="true" />
            )}
            <span className="text-2xs text-ink-muted">
              {challenge.status === 'solved' ? '已确认的答案' : '候选答案（待确认）'}
            </span>
            <button
              type="button"
              onClick={copyFlag}
              aria-label="复制答案"
              className="ml-auto rounded-xs p-0.5 text-ink-faint transition-ui duration-fast hover:text-ink"
            >
              {copied ? (
                <Check size={11} strokeWidth={2} className="text-signal-ok" aria-hidden="true" />
              ) : (
                <Copy size={11} strokeWidth={2} aria-hidden="true" />
              )}
            </button>
          </div>
          <code className="mono-ui break-all text-xs text-ink">{shownFlag}</code>
        </div>
      )}

      {/* ── 额度（只在有消耗或已用尽时显示，避免常态噪音）── */}
      {(challenge.wrongSubmissionCount > 0 || outOfQuota) && (
        <p className="flex items-center gap-1.5 text-2xs">
          <Percent
            size={11}
            strokeWidth={2}
            className={outOfQuota ? 'text-signal-risk' : 'text-ink-faint'}
            aria-hidden="true"
          />
          <span className={outOfQuota ? 'text-signal-risk' : 'text-ink-muted'}>
            {outOfQuota
              ? '错误提交次数已用尽 —— 继续提交只会浪费平台额度'
              : `错误提交还剩 ${challenge.remainingWrongSubmissions} 次（已用 ${challenge.wrongSubmissionCount}/${challenge.maxWrongSubmissions}）`}
          </span>
        </p>
      )}

      {/* ── 失败原因原样显示 ── */}
      {challenge.lastError && challenge.status === 'failed' && (
        <p className="text-2xs text-ink-muted">{challenge.lastError}</p>
      )}

      {/* ── 动作 ── */}
      <footer className="mt-auto flex flex-wrap items-center gap-tight">
        {/*
          ── 题解（v0.36）──

          ★ 放在动作区**最前**，与 `ScanList` 的菜单项同一个位置理由：
            对一道跑完过的题，"它是怎么解出来的"是用户最想先看的东西，
            而下面那些（再解、取消、调优先级）都是**过程性操作**。

          ★ 只在 `canHaveWriteup` 为真时渲染 —— 见那个函数的说明。
            判据是 **`scanId` 非空**（至少开始过一次解题），而**不是**
            状态：真实数据里那道 `[ACTF2020 新生赛]Upload` 收尾是
            `unsolved`，却**有**题解 —— 按状态判断会把它的入口藏掉。
            这里也**不**用 `challenge.confirmedFlag` 之类的判据：题解对
            **没解出**的题同样有价值（记的是"我们试过什么"）。
        */}
        {onWriteup && canHaveWriteup(challenge) && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onWriteup(challenge)}
            title="查看这道题的题解（Markdown）：关键步骤、过程纪要、token 消耗"
            className={cn(
              'inline-flex h-8 items-center gap-1.5 rounded-sm border border-line px-3 text-xs text-ink-muted',
              'transition-ui duration-fast hover:border-line-strong hover:text-ink',
              busy && 'cursor-not-allowed opacity-50',
            )}
          >
            <FileText size={12} strokeWidth={1.75} aria-hidden="true" />
            题解
          </button>
        )}

        {canSolve && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onSolve(challenge, resumable)}
            title={
              resumable
                ? '接着上一次的进度继续 —— 上次跑过的命令不会重跑'
                : undefined
            }
            className={cn(
              'press inline-flex h-8 items-center gap-1.5 rounded-sm bg-accent px-3 text-xs font-medium text-canvas',
              'transition-ui duration-fast hover:bg-accent-strong',
              busy && 'cursor-not-allowed opacity-50',
            )}
          >
            <Play size={12} strokeWidth={2.5} aria-hidden="true" />
            {resumable ? '接着解' : challenge.status === 'failed' ? '再解一次' : '开始解题'}
          </button>
        )}

        {/*
          ★ 只有"可以接着解"时才显示这个 —— 否则它和主按钮是同一个动作，
          并排放两个同样的按钮只会让人犹豫点哪个。
        */}
        {canSolve && resumable && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onSolve(challenge, false)}
            title="丢掉上次的进度，从头开始（比如你想让它换个思路）"
            className={cn(
              'inline-flex h-8 items-center gap-1 rounded-sm border border-line px-2 text-2xs text-ink-muted',
              'transition-ui duration-fast hover:border-line-strong hover:text-ink',
              busy && 'cursor-not-allowed opacity-50',
            )}
          >
            重新开始
          </button>
        )}

        {/* ★ 提交按钮**打开弹窗**，不是直接提交。见文件头那段。 */}
        {canSubmit && (
          <button
            type="button"
            disabled={busy || outOfQuota}
            onClick={() => onSubmit(challenge)}
            title={outOfQuota ? '错误提交次数已用尽' : undefined}
            className={cn(
              'inline-flex h-8 items-center gap-1.5 rounded-sm border border-line px-3 text-xs text-ink-muted',
              'transition-ui duration-fast hover:border-line-strong hover:text-ink',
              (busy || outOfQuota) && 'cursor-not-allowed opacity-50',
            )}
          >
            <ShieldCheck size={12} strokeWidth={2} aria-hidden="true" />
            确认提交…
          </button>
        )}

        {canCancel && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onCancel(challenge)}
            className={cn(
              'inline-flex h-8 items-center gap-1.5 rounded-sm px-2.5 text-xs text-ink-muted',
              'transition-ui duration-fast hover:text-ink',
              busy && 'cursor-not-allowed opacity-50',
            )}
          >
            <X size={12} strokeWidth={2} aria-hidden="true" />
            取消
          </button>
        )}

        {/*
          ── 调度优先级（v0.23）──

          ★ 只在**排队中**（`solving`）显示。

          理由：优先级影响的是"谁先被认领"，而一道未开始或已解出的题
          根本不在队列里 —— 给它显示一个"提优先级"的按钮，用户点了
          什么都不会发生，那比不显示更让人困惑。

          ★ 用"提前/延后"两个动作而不是一个数字输入框。

          比赛现场要的是"把这道题插到前面"，而不是"填一个整数"——
          而且整数需要用户先知道别人填了多少才判断得出插到哪。
          步长取 10 让"连点几次"能拉开明确的档位，同时留出中间值。
        */}
        {queued && (
          <span className="inline-flex items-center gap-1">
            <button
              type="button"
              disabled={busy}
              onClick={() => onSetPriority(challenge, nextPriorityUp(challenge.priority))}
              title="让这道题在队列里更靠前。低于自动档时一步插到最前 —— 只影响排队中的题，不打断正在跑的"
              className={cn(
                'inline-flex h-8 items-center gap-1 rounded-sm border border-line px-2 text-xs text-ink-muted',
                'transition-ui duration-fast hover:border-line-strong hover:text-ink',
                busy && 'cursor-not-allowed opacity-50',
              )}
            >
              <ChevronUp size={12} strokeWidth={2.2} aria-hidden="true" />
              提前
            </button>
            {challenge.priority !== 0 && (
              <>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => onSetPriority(challenge, nextPriorityDown(challenge.priority))}
                  title="让这道题在队列里更靠后"
                  className={cn(
                    'inline-flex h-8 items-center gap-1 rounded-sm border border-line px-2 text-xs text-ink-muted',
                    'transition-ui duration-fast hover:border-line-strong hover:text-ink',
                    busy && 'cursor-not-allowed opacity-50',
                  )}
                >
                  <ChevronDown size={12} strokeWidth={2.2} aria-hidden="true" />
                  延后
                </button>
                {/*
                  只在**真的调过**时显示这个徽标 —— 全 0 是默认值，
                  给每一道排队题都挂一个"+0"只是噪音。
                */}
                {/*
                  徽标分两种：**自动档**（1000..1999，值是"1000 + 分值"）
                  与人工档。分开显示是因为 `+1050` 这种数字对用户毫无意义 ——
                  它看起来像一个他设过的值，而其实是后台线程写的。

                  ★ 自动档的文案写成 `自动 +500`（500 = 该题分值），
                    用户一眼能看出"这是按分值排的"，而不是一串魔法数字。
                */}
                {challenge.priority >= AUTO_PRIORITY_BASE &&
                challenge.priority < PRIORITY_MANUAL_BASE ? (
                  <span
                    className="tabular rounded-xs border border-accent/40 px-1 text-2xs text-accent-strong"
                    title="「新题自动优先」给的排队加成（数值 = 1000 + 题目分值）。点「提前」可以插到所有自动题前面。"
                  >
                    自动 +{challenge.priority - AUTO_PRIORITY_BASE}
                  </span>
                ) : (
                  <span
                    className="tabular rounded-xs border border-accent/40 px-1 text-2xs text-accent-strong"
                    title="调度优先级，越大越先做。同优先级的题按先入先出。"
                  >
                    {challenge.priority > 0 ? `+${challenge.priority}` : challenge.priority}
                  </span>
                )}
              </>
            )}
          </span>
        )}

        {/*
          解题过程在扫描列表与活动流里 —— 复用既有的两个页面，
          不为 CTF2 再造一个进度页（已定设计决策 7 的意图：
          前端不改代码就能看到进度）。
        */}
        {challenge.scanId && (
          <Link
            to={`/activity?scan=${encodeURIComponent(challenge.scanId)}`}
            className="ml-auto inline-flex items-center gap-1 text-2xs text-accent-strong underline hover:text-accent"
          >
            查看解题过程
            <ExternalLink size={10} strokeWidth={2} aria-hidden="true" />
          </Link>
        )}
      </footer>
    </article>
  )
}

export default ChallengeCard
