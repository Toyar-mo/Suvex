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

import { ClipboardList, FileText, Siren, Upload } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { PageHeader } from '@/components/layout/AppShell'
import { WorkOrderDialog } from '@/components/ctf2'
import { WriteupDialog } from '@/components/ctf2/WriteupDialog'
import { Button } from '@/components/ui/Button'
import { EmptyState } from '@/components/ui/EmptyState'
import { ErrorState } from '@/components/ui/ErrorState'
import { SkeletonRows } from '@/components/ui/Skeleton'
import { useToast } from '@/components/ui/Toast'
import {
  canHaveWorkOrder,
  canHaveWriteup,
  createIrTask,
  loadCtf2,
  solveChallenge,
  useCtf2,
  type Ctf2Challenge,
} from '@/data/ctf2'
import { stageScanAttachments, type ScanAttachment } from '@/data/scans-store'
import { errorMessage } from '@/lib/api'

/** 应急响应的分类前缀（与后端 `IR_CATEGORY_PREFIX` 逐字一致）。 */
const IR_CATEGORY_PREFIX = 'ir-'

/**
 * 这道题是不是一次应急响应复盘。
 *
 * ⚠️ 判据是**分类前缀**，不是"在哪一批里"。v0.41 时 IR 题混在用户
 * 自己的 practice 批次里（`run_ir_closed_loop.py` 就是这么建的），
 * 靠批次判会把它们全漏掉；而 v0.43 之后本地复盘在 `kind='local'`
 * 批次里，但**将来也可能有平台上的 IR 题**。分类是唯一稳定的判据。
 */
function isIrChallenge(challenge: Ctf2Challenge): boolean {
  return (challenge.category || '').trim().toLowerCase().startsWith(IR_CATEGORY_PREFIX)
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
function irStatusText(challenge: Ctf2Challenge, ranBefore: boolean): string {
  if (challenge.status === 'solving' || challenge.status === 'environment_ready') {
    return '复盘中'
  }
  if (challenge.status === 'failed') {
    return '上一次复盘没跑完'
  }
  // ★ 关键分支：`unsolved` 对复盘任务有两种意思。
  if (ranBefore) return '已复盘（可重跑）'
  return '待复盘'
}

/** 一个复盘任务条目需要的全部信息。 */
interface IrTaskView {
  challenge: Ctf2Challenge
  /** 跑过没有 —— 靠"有没有工单文件"判断，见 `irStatusText`。 */
  ranBefore: boolean
}

export function IrPage() {
  // ★ 复用 CTF2 的 store，而不是新建一个。
  //
  // 复盘任务与 CTF2 题目在数据库里**是同一张表同一行**
  // （`Ctf2Challenge`，靠 `category` 前缀区分）。另建一个 store 会让
  // 两个页面各自缓存一份同一批数据，症状是"在 CTF2 页跑完，切到
  // 应急响应页还显示待复盘"。
  const snapshot = useCtf2()
  const toast = useToast()

  const [title, setTitle] = useState('')
  const [brief, setBrief] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [creating, setCreating] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [starting, setStarting] = useState<string | null>(null)

  /** 点开的是哪一道题的哪个产物（两个独立 state，刻意不合并 —— 它们是两件事）。 */
  const [workOrderChallenge, setWorkOrderChallenge] = useState<Ctf2Challenge | null>(null)
  const [writeupChallenge, setWriteupChallenge] = useState<Ctf2Challenge | null>(null)

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
      if (challenge.updatedAt && challenge.createdAt && challenge.updatedAt !== challenge.createdAt) {
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

  // 首次进入拉一次快照。⚠️ 只在 idle 时拉，避免每次渲染都发请求。
  const loaded = useRef(false)
  useEffect(() => {
    if (loaded.current) return
    loaded.current = true
    if (snapshot.status !== 'idle') return
    void loadCtf2()
  }, [snapshot.status])

  const onPickFiles = useCallback((event: React.ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(event.target.files ?? [])
    if (picked.length > 0) setFiles(picked)
    // 清空 input.value：否则选同一个文件第二次不触发 change。
    event.target.value = ''
  }, [])

  const onCreate = useCallback(async () => {
    const cleanTitle = title.trim()
    if (!cleanTitle) {
      setNotice('先给这次复盘起个名字 —— 它会出现在报告标题和工单文件名里。')
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
            ? `已建好，证据包 ${task.attachments.length} 份：${task.attachments
                .map((item) => item.name)
                .join('、')}`
            : '已建好，但这次没有证据包 —— 没有证据的复盘只能给出方法建议，不能给出结论。',
        )
      }

      setTitle('')
      setBrief('')
      setFiles([])
      await loadCtf2({ silent: true })
    } catch (error) {
      setNotice(errorMessage(error) || '建任务失败，请稍后重试。')
    } finally {
      setCreating(false)
    }
  }, [brief, files, title])

  const onStart = useCallback(
    async (challenge: Ctf2Challenge) => {
      setStarting(challenge.id)
      try {
        await solveChallenge(challenge.id)
        toast.success('已开跑，进度在下方条目的日志里。')
      } catch (error) {
        toast.error(errorMessage(error) || '启动失败，请稍后重试。')
      } finally {
        setStarting(null)
      }
    },
    [toast],
  )

  return (
    <div className="flex flex-col">
      <PageHeader navId="ir" />

      <div className="flex flex-col gap-bay px-gutter py-bay">
        {/* ── 建任务 ────────────────────────────────────── */}
        <section className="rounded-lg border border-line bg-surface p-5">
          <h2 className="mb-1 text-sm font-medium text-ink">开始一次复盘</h2>
          <p className="mb-4 text-xs text-ink-muted">
            把出事那台机器上能拿到的日志、命令历史、导出文件放进来。证据包里有什么，
            智能体就只能看什么 —— 它不会替你上机去取。
          </p>

          <div className="flex flex-col gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-ink-muted">这次复盘叫什么</span>
              <input
                type="text"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder="例：web-01 入侵事件（10 月 19 日）"
                maxLength={200}
                className="rounded border border-line bg-canvas px-3 py-2 text-sm text-ink outline-none focus:border-accent"
              />
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-xs text-ink-muted">想查什么（可空）</span>
              <textarea
                value={brief}
                onChange={(event) => setBrief(event.target.value)}
                placeholder="例：这台机器昨晚疑似被人登进来了，想知道他是怎么进来的、动了什么。"
                maxLength={2000}
                rows={3}
                className="resize-y rounded border border-line bg-canvas px-3 py-2 text-sm text-ink outline-none focus:border-accent"
              />
            </label>

            <div className="flex flex-wrap items-center gap-3">
              <label className="inline-flex cursor-pointer items-center gap-2 rounded border border-line px-3 py-2 text-xs text-ink hover:border-accent">
                <Upload className="h-4 w-4" />
                选择证据包
                <input type="file" multiple className="hidden" onChange={onPickFiles} />
              </label>
              <span className="text-xs text-ink-muted">
                {files.length === 0
                  ? '还没选文件'
                  : `${files.length} 个文件：${files.map((file) => file.name).join('、')}`}
              </span>
            </div>

            {notice && (
              <p className="rounded border border-line bg-canvas px-3 py-2 text-xs text-ink">
                {notice}
              </p>
            )}

            <div className="flex items-center gap-2">
              <Button onClick={onCreate} disabled={creating}>
                {creating ? '正在建…' : '建复盘任务'}
              </Button>
              <span className="text-xs text-ink-muted">
                建完之后还要点一次「开始复盘」—— 传错包是最常见的错误，所以中间留一步给你看一眼。
              </span>
            </div>
          </div>
        </section>

        {/* ── 任务列表 ──────────────────────────────────── */}
        <section className="flex flex-col gap-2">
          <h2 className="text-sm font-medium text-ink">复盘任务</h2>

          {snapshot.status === 'loading' && <SkeletonRows rows={2} rowClassName="h-20" />}

          {snapshot.status === 'error' && (
            <ErrorState
              error={snapshot.error}
              onRetry={() => void loadCtf2()}
              title="复盘任务列表加载失败"
            />
          )}

          {snapshot.status === 'ready' && tasks.length === 0 && (
            <EmptyState
              icon={Siren}
              title="还没有复盘任务"
              description="复盘任务与扫描任务是两件事：扫描回答「这个目标有没有漏洞」，复盘回答「已经发生了什么」。上面填个名字、选一批日志就能开始。"
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
                    {challenge.title || '未命名复盘'}
                  </h3>
                  <p className="mt-1 text-xs text-ink-muted">
                    {irStatusText(challenge, ranBefore)}
                    {' · '}
                    {challenge.category}
                    {' · '}
                    证据包 {challenge.attachments.length} 份
                  </p>
                  {challenge.lastError && (
                    <p className="mt-1 text-xs text-warn">{challenge.lastError}</p>
                  )}
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    variant="ghost"
                    onClick={() => void onStart(challenge)}
                    disabled={starting === challenge.id || challenge.status === 'solving'}
                  >
                    {starting === challenge.id ? '正在启动…' : ranBefore ? '重新复盘' : '开始复盘'}
                  </Button>
                  {canHaveWorkOrder(challenge) && (
                    <Button variant="ghost" onClick={() => setWorkOrderChallenge(challenge)}>
                      <ClipboardList className="h-4 w-4" />
                      处置工单
                    </Button>
                  )}
                  {canHaveWriteup(challenge) && (
                    <Button variant="ghost" onClick={() => setWriteupChallenge(challenge)}>
                      <FileText className="h-4 w-4" />
                      复盘报告
                    </Button>
                  )}
                </div>
              </div>
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
        <WriteupDialog
          challenge={writeupChallenge}
          onClose={() => setWriteupChallenge(null)}
        />
      )}
    </div>
  )
}

export default IrPage
