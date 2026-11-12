/**
 * 应急响应页（v0.43）。
 *
 * ## 这个页面为什么单独存在
 *
 * v0.41 把应急响应做成了 CTF2 里的一个题分类（`ir-root-cause` /
 * `ir-timeline` / `ir-scope`），功能全在，但入口藏在「CTF2 解题」页里。
 * 于是出现了一个具体的困惑：想复盘一次入侵的人打开 CTF2 页，看到的
 * 是一堆 CTF 比赛和题目 —— 他会以为这个系统只会打靶。
 *
 * 侧栏是「这个系统有哪几件事可做」的清单。渗透评估、CTF 解题、
 * **应急响应**、报告，是四件不同的事，它们该并列。判据不是重要性，
 * 而是「回答的问题是不是同一个」：
 *
 *   · 扫描任务 → 「这个目标有没有漏洞」
 *   · CTF2 解题 → 「这道题怎么解」
 *   · **应急响应 → 「昨天那台机器到底发生了什么」**
 *
 * 第三个问题与另两个既不同源（输入是一批本机日志，不是网络目标）
 * 也不同期（出事了才开，不是例行巡检）。所以它是一级入口。
 *
 * ## ★ 这个页面与「漏洞发现」页的根本区别：它读的是**过去**
 *
 * 漏洞发现回答"现在有什么风险"，应急响应回答"已经发生了什么"。
 * 前者可以重跑（再扫一次），后者不能 —— 证据包是那段时间唯一的一份
 * 记录。这个区别决定了本页的三个不变量：
 *
 * 1. **任务建了不等于跑起来了**：建任务只是把证据包摆好。用户必须
 *    有机会在建完之后看一眼"包里到底有哪几份文件"，因为传错包是最
 *    常见的一种错误，而它一旦开跑就烧掉一整轮 LLM 额度。
 * 2. **没有 flag，所以没有"解出"**：这类任务的终态是 `unsolved`
 *    （可重跑），产出在题解与工单文件里。界面上不能出现进度条走到
 *    100% 却写"失败"这种话 —— 见 `IrTaskStatus` 的措辞。
 * 3. **工单不是执行记录**：处置建议只到"建议"，系统没有执行通道。
 *    所以按钮叫「查看处置工单」，不叫「应用修复」。
 *
 * ## 为什么不做成"一个巨大的表单"
 *
 * 建一次复盘只有两个必填概念：**叫什么**、**想查什么**。其余全部由
 * 服务端定（分类、镜像、批次）。少让用户选，是因为他能选错的每一项
 * 都会变成一个看得懂的假象 —— 比如让他挑镜像，挑错的表现是"命令
 * 找不到"，而不是"你挑错了"。
 */

import { ClipboardList, FileText, Siren, Terminal, Upload, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { ChallengeProcess } from '@/components/chat/ChallengeProcess'
import { PageHeader } from '@/components/layout/AppShell'
import { WorkOrderDialog } from '@/components/kernel'
import { WriteupDialog } from '@/components/kernel/WriteupDialog'
import { Button } from '@/components/ui/Button'
import { EmptyState } from '@/components/ui/EmptyState'
import { ErrorState } from '@/components/ui/ErrorState'
import { SkeletonRows } from '@/components/ui/Skeleton'
import { useI18n } from '@/i18n'
import { T } from '@/i18n/T'
import { showToast } from '@/data/toast-store'
import {
  canHaveWorkOrder,
  canHaveWriteup,
  createIrTask,
  loadKernel,
  solveChallenge,
  useKernel,
  type KernelChallenge,
} from '@/data/kernel'
import { stageScanAttachments } from '@/data/scans-store'
import { formatBytes } from '@/data/files'
import { cn } from '@/lib/cn'
import { errorMessage } from '@/lib/api'

/** 应急响应的分类前缀（与后端 `IR_CATEGORY_PREFIX` 逐字一致）。 */
const IR_CATEGORY_PREFIX = 'ir-'
/** 三类复盘的人话名字。界面不要直接显示 `ir-root-cause`。 */
const IR_CATEGORY_LABEL: Record<string, string> = {
  'ir-root-cause': '根因分析',
  'ir-timeline': '时间线还原',
  'ir-scope': '影响范围',
}

function categoryLabel(
  category: string,
  t: (key: string, vars?: Record<string, string | number>, fallback?: string) => string,
): string {
  const key = (category || '').trim().toLowerCase()
  return IR_CATEGORY_LABEL[key]
    ? t(`kernel2.irCategory.${key}`, undefined, IR_CATEGORY_LABEL[key])
    : category
}


/**
 * 这道题是不是一次应急响应复盘。
 *
 * ⚠️ 判据是**分类前缀**，不是"在哪一批里"。v0.41 时 IR 题混在用户
 * 自己的 practice 批次里（`run_ir_closed_loop.py` 就是这么建的），
 * 靠批次判会把它们全漏掉；而 v0.43 之后本地复盘在 `kind='local'`
 * 批次里，但**将来也可能有平台上的 IR 题**。分类是唯一稳定的判据。
 */
function isIrChallenge(challenge: KernelChallenge): boolean {
  return (challenge.category || '').trim().toLowerCase().startsWith(IR_CATEGORY_PREFIX)
}

/** 这道复盘正在跑（用于「过程」面板与列表轮询）。 */
function isRunningStatus(challenge: KernelChallenge): boolean {
  return challenge.status === 'solving' || challenge.status === 'environment_ready'
}

/**
 * 一道复盘任务的**人话状态**。
 *
 * ## 为什么不能直接用 `CHALLENGE_STATUS_LABEL`
 *
 * 那套标签是给 CTF 题写的：`solved` = 已解出、`unsolved` = 未开始。
 * 而复盘任务**没有 flag**，跑完之后状态也会回到 `unsolved`（可重跑），
 * 直接套用会让一次**成功的复盘**显示成"未开始"。
 *
 * 判据只能落在"有没有产物"上：有工单或有题解 = 跑过了。
 */
function irStatusText(
  challenge: KernelChallenge,
  ranBefore: boolean,
  t: (key: string, vars?: Record<string, string | number>, fallback?: string) => string,
): string {
  if (challenge.status === 'solving' || challenge.status === 'environment_ready') {
    return t('ir.status.running', undefined, '复盘中')
  }
  if (challenge.status === 'failed') {
    return t('ir.status.failed', undefined, '上一次复盘没跑完')
  }
  // ★ 关键分支：`unsolved` 对复盘任务有两种意思。
  if (ranBefore) return t('ir.status.ranBefore', undefined, '已复盘（可重跑）')
  return t('ir.status.pending', undefined, '待复盘')
}

/**
 * 列表卡片上要不要显示这条 `lastError`。
 *
 * ★ 背景（v1.63）：`lastError` 是后端写入的整段报错，例如
 *   「CTF2 拒绝了这次请求（400）。可能是参数不对，或平台接口有变更。
 *    平台标记：INVALID_ID（errors.validation.invalid_id）。」
 *   这类"平台标记：INVALID_ID"的提示对用户没有可操作性 ——
 *   `INVALID_ID` 多半只是某个 id 参数格式不对，卡片上挂着只会让人
 *   以为平台坏了。用户要求「暂时撤销列表卡片上的这行标识」。
 *
 * ⚠️ 只在**列表卡片**这一处隐藏；后端文案、会话日志、详情/工单里
 *   仍然保留完整报错，排查时看得到。别的错误（权限、限流、故障等）
 *   照常显示 —— 那些是有话可说的。
 */
function shouldShowCardError(lastError: string | null | undefined): boolean {
  if (!lastError) return false
  // INVALID_ID 及同源的校验/平台标记，列表上不显示
  if (/INVALID_ID|errors\.validation\.invalid_id/.test(lastError)) return false
  return true
}

/** 一个复盘任务条目需要的全部信息。 */
interface IrTaskView {
  challenge: KernelChallenge
  /** 跑过没有 —— 靠"有没有工单文件"判断，见 `irStatusText`。 */
  ranBefore: boolean
}

export function IrPage() {
  const { t } = useI18n()
  // ★ 复用 CTF2 的 store，而不是新建一个。
  //
  // 复盘任务与 CTF2 题目在数据库里**是同一张表同一行**
  // （`KernelChallenge`，靠 `category` 前缀区分）。另建一个 store 会让
  // 两个页面各自缓存一份同一批数据，症状是"在 CTF2 页跑完，切到
  // 应急响应页还显示待复盘"。
  const snapshot = useKernel()

  const [title, setTitle] = useState('')
  const [brief, setBrief] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [creating, setCreating] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [starting, setStarting] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)

  /** 点开的是哪一道题的哪个产物（两个独立 state，刻意不合并 —— 它们是两件事）。 */
  const [workOrderChallenge, setWorkOrderChallenge] = useState<KernelChallenge | null>(null)
  const [writeupChallenge, setWriteupChallenge] = useState<KernelChallenge | null>(null)

  /**
   * 哪些题**跑过**。
   *
   * ★ 为什么不从 `challenge.status` 推：复盘任务跑完后状态回到
   *   `unsolved`（`_record_outcome` 的分支），与"从没跑过"完全一样。
   *   唯一的区别在产物上 —— 有工单或题解就说明跑过。
   *
   * ⚠️ 这里不主动发请求探测（每道题一次 409 太贵）。用的是一个
   *   **乐观判据**：只要这道题不是初始状态、或曾经有过 `lastError`、
   *   或 `updatedAt != createdAt`，就当它跑过。它的作用是让状态文案
   *   更贴近真相，而不是做权限判断 —— 真要看有没有工单，点进去由
   *   后端回 409 或给文件，那才是权威答案。
   */
  const ranBeforeIds = useMemo(() => {
    const ran = new Set<string>()
    for (const challenge of snapshot.challenges) {
      if (
        challenge.updatedAt &&
        challenge.createdAt &&
        challenge.updatedAt !== challenge.createdAt
      ) {
        ran.add(challenge.id)
      }
    }
    return ran
  }, [snapshot.challenges])

  const tasks: IrTaskView[] = useMemo(() => {
    return snapshot.challenges
      .filter(isIrChallenge)
      .map((challenge) => ({ challenge, ranBefore: ranBeforeIds.has(challenge.id) }))
      .sort((a, b) => (b.challenge.createdAt || '').localeCompare(a.challenge.createdAt || ''))
  }, [snapshot.challenges, ranBeforeIds])

  /**
   * 哪些任务的「过程」是展开的。
   *
   * ★ 复盘正在跑时**默认展开**（见渲染处），因为用户点「开始复盘」之后
   *   留在这一页，能看到的只有一行"复盘中"—— 过程事件才是进度本身。
   *   跑完之后可以收起；也可以手动展开看「上一次的过程」。
   */
  const [openProcessIds, setOpenProcessIds] = useState<Set<string>>(new Set())

  // 首次进入拉一次快照。⚠️ 只在 idle 时拉，避免每次渲染都发请求。
  const loaded = useRef(false)
  useEffect(() => {
    if (loaded.current) return
    loaded.current = true
    if (snapshot.status !== 'idle') return
    void loadKernel()
  }, [snapshot.status])

  /**
   * 有复盘在跑 → 5 秒静默刷新一次快照。
   *
   * ★ 原来这一页**只拉一次**：点「开始复盘」后状态一直停在"复盘中"，
   *   跑完了也不变，直到用户刷新整页。而 `solveChallenge` 本身是异步的
   *   （后端排队、起容器），前端拿不到完成回调 —— 只能轮询。
   *   与 CTF 看板 / 对话页同一条口径：终态就停。
   */
  const anyRunning = tasks.some(({ challenge }) => isRunningStatus(challenge))
  useEffect(() => {
    if (!anyRunning) return
    const timer = window.setInterval(() => void loadKernel({ silent: true }), 5000)
    return () => window.clearInterval(timer)
  }, [anyRunning])

  const toggleProcess = useCallback((id: string) => {
    setOpenProcessIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const addFiles = useCallback((incoming: File[]) => {
    setFiles((previous) => {
      const seen = new Set(previous.map((file) => `${file.name}:${file.size}`))
      return [...previous, ...incoming.filter((file) => !seen.has(`${file.name}:${file.size}`))]
    })
  }, [])

  const onPickFiles = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      addFiles(Array.from(event.target.files ?? []))
      // 清空 input.value：否则选同一个文件第二次不触发 change。
      event.target.value = ''
    },
    [addFiles],
  )

  const onCreate = useCallback(async () => {
    const cleanTitle = title.trim()
    if (!cleanTitle) {
      setNotice(t('ir.notice.needTitle', undefined, '先给这次复盘起个名字 —— 它会出现在报告标题和工单文件名里。'))
      return
    }

    setCreating(true)
    setNotice(null)
    try {
      // ① 附件先暂存（没有附件就跳过这一步）
      let stagingId: string | null = null
      if (files.length > 0) {
        const staged = await stageScanAttachments(files)
        stagingId = staged.stagingId
      }

      // ② 建任务（后端在这一步认领附件）
      const task = await createIrTask({
        title: cleanTitle,
        brief: brief.trim(),
        attachmentStagingId: stagingId,
      })

      // ★ `notice` 必须显示：暂存批次过期时后端照建不误但会说一句，
      //   不说的话用户会得到一次"跑了半天说我没给证据"的复盘。
      if (task.notice) setNotice(task.notice)
      else {
        setNotice(
          task.attachments.length > 0
            ? t('ir.notice.createdWithFiles', {
                n: task.attachments.length,
                list: task.attachments.map((item) => item.name).join('、'),
              }, `已建好，证据包 ${task.attachments.length} 份：${task.attachments
                .map((item) => item.name)
                .join('、')}`)
            : t('ir.notice.createdNoFiles', undefined, '已建好，但这次没有证据包 —— 没有证据的复盘只能给出方法建议，不能给出结论。'),
        )
      }

      setTitle('')
      setBrief('')
      setFiles([])
      await loadKernel({ silent: true })
    } catch (error) {
      setNotice(errorMessage(error) || t('ir.notice.createFailed', undefined, '建任务失败，请稍后重试。'))
    } finally {
      setCreating(false)
    }
  }, [brief, files, title, t])

  const onStart = useCallback(async (challenge: KernelChallenge) => {
    setStarting(challenge.id)
    try {
      await solveChallenge(challenge.id, {})
      showToast({ message: t('ir.toast.started', undefined, '已开跑，过程会显示在这条任务下面。') })
    } catch (error) {
      showToast({ message: errorMessage(error) || t('ir.toast.startFailed', undefined, '启动失败，请稍后重试。'), tone: 'risk' })
    } finally {
      setStarting(null)
    }
  }, [t])

  return (
    <div className="flex flex-col">
      <PageHeader navId="ir" />

      <div className="flex flex-col gap-bay px-gutter py-bay">
        {/* ── 建任务 ────────────────────────────────────── */}
        <section className="rounded-lg border border-line bg-surface p-5">
          <h2 className="mb-1 text-sm font-medium text-ink"><T k="ir.form.title" fallback="开始一次复盘" /></h2>
          <p className="mb-4 text-xs text-ink-muted">
            <T k="ir.form.desc" fallback="把出事那台机器上能拿到的日志、命令历史、导出文件放进来。证据包里有什么， 智能体就只能看什么 —— 它不会替你上机去取。" />
          </p>

          <div className="flex flex-col gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-ink-muted"><T k="ir.form.titleLabel" fallback="这次复盘叫什么" /></span>
              <input
                type="text"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder={t('ir.form.titlePlaceholder', undefined, '例：web-01 入侵事件（10 月 19 日）')}
                maxLength={200}
                className="rounded border border-line bg-canvas px-3 py-2 text-sm text-ink outline-none focus:border-accent"
              />
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-xs text-ink-muted"><T k="ir.form.briefLabel" fallback="想查什么（可空）" /></span>
              <textarea
                value={brief}
                onChange={(event) => setBrief(event.target.value)}
                placeholder={t('ir.form.briefPlaceholder', undefined, '例：这台机器昨晚疑似被人登进来了，想知道他是怎么进来的、动了什么。')}
                maxLength={2000}
                rows={3}
                className="resize-y rounded border border-line bg-canvas px-3 py-2 text-sm text-ink outline-none focus:border-accent"
              />
            </label>

            <div
              onDragOver={(event) => {
                event.preventDefault()
                setDragging(true)
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(event) => {
                event.preventDefault()
                setDragging(false)
                addFiles(Array.from(event.dataTransfer.files))
              }}
              className={cn(
                'flex flex-col gap-2 rounded-md border border-dashed px-3 py-3 transition-colors duration-150',
                dragging
                  ? 'border-accent bg-accent/5'
                  : 'border-line bg-canvas hover:border-line-strong',
              )}
            >
              <div className="flex flex-wrap items-center gap-3">
                <label className="inline-flex cursor-pointer items-center gap-2 rounded border border-line bg-surface px-3 py-1.5 text-xs text-ink hover:border-accent">
                  <Upload className="h-3.5 w-3.5" />
                  <T k="ir.form.pickFiles" fallback="选择证据包" />
                  <input type="file" multiple className="hidden" onChange={onPickFiles} />
                </label>
                <span className="text-xs text-ink-muted">
                  {files.length === 0
                    ? t('ir.form.noFiles', undefined, '拖进来，或点左侧选择文件')
                    : t(
                        'ir.form.fileSummary',
                        { n: files.length, size: formatBytes(files.reduce((sum, file) => sum + file.size, 0)) },
                        `${files.length} 个文件 · ${formatBytes(files.reduce((sum, file) => sum + file.size, 0))}`,
                      )}
                </span>
                {files.length > 0 && (
                  <button
                    type="button"
                    onClick={() => setFiles([])}
                    className="text-2xs text-ink-faint underline-offset-2 hover:text-ink hover:underline"
                  >
                    {t('ir.form.clearFiles', undefined, '清空')}
                  </button>
                )}
              </div>
              {files.length > 0 && (
                <ul className="flex flex-wrap gap-1.5">
                  {files.map((file) => (
                    <li
                      key={`${file.name}:${file.size}`}
                      className="inline-flex max-w-full items-center gap-1 rounded-sm border border-line bg-surface px-1.5 py-0.5 text-2xs text-ink-muted"
                    >
                      <span className="truncate" title={file.name}>{file.name}</span>
                      <button
                        type="button"
                        aria-label={t('ir.form.removeFile', { name: file.name }, `移除 ${file.name}`)}
                        onClick={() =>
                          setFiles((previous) =>
                            previous.filter(
                              (item) => !(item.name === file.name && item.size === file.size),
                            ),
                          )
                        }
                        className="shrink-0 text-ink-faint hover:text-signal-risk"
                      >
                        <X size={10} strokeWidth={2} aria-hidden="true" />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {notice && (
              <p className="rounded border border-line bg-canvas px-3 py-2 text-xs text-ink">
                {notice}
              </p>
            )}

            <div className="flex items-center gap-2">
              <Button onClick={onCreate} disabled={creating}>
                {creating ? t('ir.form.creating', undefined, '正在建…') : t('ir.form.create', undefined, '建复盘任务')}
              </Button>
              <span className="text-xs text-ink-muted">
                <T k="ir.form.hint" fallback="建完之后还要点一次「开始复盘」—— 传错包是最常见的错误，所以中间留一步给你看一眼。" />
              </span>
            </div>
          </div>
        </section>

        {/* ── 任务列表 ──────────────────────────────────── */}
        <section className="flex flex-col gap-2">
          <h2 className="text-sm font-medium text-ink"><T k="ir.list.title" fallback="复盘任务" /></h2>

          {snapshot.status === 'loading' && <SkeletonRows rows={2} rowClassName="h-20" />}

          {snapshot.status === 'error' && (
            <ErrorState
              error={snapshot.error}
              onRetry={() => void loadKernel()}
              title={t('ir.list.loadFailed', undefined, '复盘任务列表加载失败')}
            />
          )}

          {snapshot.status === 'ready' && tasks.length === 0 && (
            <EmptyState
              icon={Siren}
              title={t('ir.list.emptyTitle', undefined, '还没有复盘任务')}
              description={t('ir.list.emptyDesc', undefined, '复盘任务与扫描任务是两件事：扫描回答「这个目标有没有漏洞」，复盘回答「已经发生了什么」。上面填个名字、选一批日志就能开始。')}
            />
          )}

          {tasks.map(({ challenge, ranBefore }) => (
            <article
              key={challenge.id}
              className="rounded-lg border border-line bg-surface p-4"
              data-testid="ir-task"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="truncate text-sm font-medium text-ink">
                    {challenge.title || t('ir.card.untitled', undefined, '未命名复盘')}
                  </h3>
                  <p className="mt-1 text-xs text-ink-muted">
                    {irStatusText(challenge, ranBefore, t)}
                    {' · '}
                    {categoryLabel(challenge.category, t)}
                    {' · '}
                    {t('ir.card.attachments', { n: challenge.attachments.length }, `证据包 ${challenge.attachments.length} 份`)}
                  </p>
                  {shouldShowCardError(challenge.lastError) && (
                    <p className="text-warn mt-1 text-xs">{challenge.lastError}</p>
                  )}
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  {/*
                    失败要看得出来（2026-11 设计审查）。

                    原来它只写在灰色副标题里 ——「上一次复盘没跑完」和
                    「已复盘（可重跑）」在扫视时几乎没有区别，而前者是
                    需要处置的状态。给一个和扫描页同语言的徽标，
                    副标题保留（它说的是"上一次"，不是"现在"）。
                  */}
                  {challenge.status === 'failed' && (
                    <span className="rounded-pill border border-signal-risk/40 bg-signal-risk/10 px-2 py-0.5 text-2xs text-signal-risk">
                      <T k="ir.card.lastFailed" fallback="上次失败" />
                    </span>
                  )}
                  <Button
                    variant="ghost"
                    onClick={() => void onStart(challenge)}
                    disabled={starting === challenge.id || isRunningStatus(challenge)}
                  >
                    {starting === challenge.id
                      ? t('ir.card.starting', undefined, '正在启动…')
                      : ranBefore
                        ? t('ir.card.restart', undefined, '重新复盘')
                        : t('ir.card.start', undefined, '开始复盘')}
                  </Button>
                  {ranBefore && canHaveWorkOrder(challenge) && (
                    <Button variant="ghost" onClick={() => setWorkOrderChallenge(challenge)}>
                      <ClipboardList className="h-4 w-4" />
                      <T k="ir.card.workOrder" fallback="处置工单" />
                    </Button>
                  )}
                  {ranBefore && canHaveWriteup(challenge) && (
                    <Button variant="ghost" onClick={() => setWriteupChallenge(challenge)}>
                      <FileText className="h-4 w-4" />
                      <T k="ir.card.writeup" fallback="复盘报告" />
                    </Button>
                  )}
                  {/*
                    过程入口。★ 只在真的跑过（有 scanId）时给 ——
                    从没开始过的任务点开只会说"还没有开始解题"，那是噪音。
                  */}
                  {challenge.scanId && (
                    <Button
                      variant="ghost"
                      onClick={() => toggleProcess(challenge.id)}
                      aria-expanded={isRunningStatus(challenge) || openProcessIds.has(challenge.id)}
                    >
                      <Terminal className="h-4 w-4" />
                      {isRunningStatus(challenge) || openProcessIds.has(challenge.id)
                        ? t('ir.card.hideProcess', undefined, '收起过程')
                        : t('ir.card.showProcess', undefined, '查看过程')}
                    </Button>
                  )}
                </div>
              </div>

              {/*
                ★ 正在跑的任务**默认展开**过程。

                点「开始复盘」后用户留在这页，本来只能看到一行"复盘中" ——
                而过程事件才是"它现在在做什么"。跑完的任务默认收起，
                需要时手动展开看上一次的记录。
              */}
              {challenge.scanId &&
                (isRunningStatus(challenge) || openProcessIds.has(challenge.id)) && (
                  <div className="mt-3 border-t border-line pt-3">
                    <ChallengeProcess
                      scanId={challenge.scanId}
                      running={isRunningStatus(challenge)}
                    />
                  </div>
                )}
            </article>
          ))}
        </section>
      </div>

      {/*
        ★ 两个 Dialog 分开挂载，且**按需条件挂载**（`challenge && ...`）：
          它们各自在挂载时发一次请求，无条件挂载等于每进一次页面就发
          两个必然 409 的请求。见 `WorkOrderDialog` 的说明。
      */}
      {workOrderChallenge && (
        <WorkOrderDialog
          challenge={workOrderChallenge}
          onClose={() => setWorkOrderChallenge(null)}
        />
      )}
      {writeupChallenge && (
        <WriteupDialog challenge={writeupChallenge} onClose={() => setWriteupChallenge(null)} />
      )}
    </div>
  )
}

export default IrPage
