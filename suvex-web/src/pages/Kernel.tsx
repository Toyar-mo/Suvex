import { PageHeader } from '@/components/layout/AppShell'
import {
  ChallengeBoard,
  CompetitionPicker,
  FlagConfirmDialog,
  RacePanel,
  SandboxProfileSelect,
  WorkOrderDialog,
  WriteupDialog,
} from '@/components/kernel'
import ConfirmDialog from '@/components/ui/ConfirmDialog'
import ErrorState from '@/components/ui/ErrorState'
import { Button, IconButton } from '@/components/ui/Button'
import ToggleChip from '@/components/ui/ToggleChip'
import { SkeletonRows } from '@/components/ui/Skeleton'
import {
  CHALLENGE_STATUS_LABEL,
  CANONICAL_CATEGORIES,
  CANONICAL_CATEGORY_LABEL,
  canonicalCategoryOf,
  batchChallenges,
  bindKernelAccount,
  pullKernelChallenge,
  unbindKernelAccount,
  cancelChallenge,
  clearKernelNotice,
  hasActiveChallenges,
  loadKernel,
  prioritizeByScore,
  releaseEnvironment,
  setChallengePriority,
  solveChallenge,
  submitFlag,
  syncCompetition,
  syncKernel,
  useKernel,
  normalizeSandboxProfile,
  type KernelChallenge,
  type KernelChallengeStatus,
  type KernelSandboxProfile,
  type KernelSubmitMode,
  type CanonicalCategory,
} from '@/data/kernel'
import { openChallengeSession } from '@/data/chat'
import { showToast } from '@/data/toast-store'
import { errorMessage } from '@/lib/api'
import { useI18n } from '@/i18n'
import { T } from '@/i18n/T'
import { cn } from '@/lib/cn'
import {
  CheckCircle2,
  Link2,
  ListOrdered,
  RefreshCw,
  Search,
  TriangleAlert,
  Trophy,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

/**
 * CTF2 解题页。
 *
 * 骨架仿 Scans 页：**页面头 → 工具条 → 内容区 → 空/错误状态**。
 * 内容区是一张**一行一道题的看板表格**（见 `ChallengeBoard` 的说明：
 *   比赛时要扫的是「状态 + 分值」，卡片网格一屏只放得下 6–9 道）。
 *
 * ## 这一页回答的问题
 *
 * 「CTF2 上有哪些题、它们到哪一步了、我现在要不要做点什么。」
 *
 * ★ 最后那半句是这一页与扫描页最大的差别：扫描页是**观察**，
 *   而这里有一个必须由人做的动作 —— **确认提交**（safe 模式）。
 *   所以候选 flag 的呈现（卡片上的 flag 块、`待确认` 状态、两步确认弹窗）
 *   是这一页最重的部分，而不是一个附属按钮。
 *
 * ## 没有绑定账号时，整页换成引导
 *
 * 不渲染一个空列表 + 一个禁用按钮：那看起来像"功能坏了"。
 * 没有 PAT 就是没有 PAT，直接要它。
 */
/**
 * 后端题目的**全部**状态。
 *
 * ⚠️ 它与 `KernelChallengeStatus` 是一一对应的，改枚举时必须同步改这里 ——
 *    下面 `STATUS_FILTERS` 的完备性靠它保证（见那个常量上的说明）。
 */
const ALL_CHALLENGE_STATUSES: readonly KernelChallengeStatus[] = [
  'unsolved',
  'environment_ready',
  'solving',
  'candidate_ready',
  'submitting',
  'solved',
  'failed',
]

/**
 * 状态筛选桶。
 *
 * ## ★ 每个桶带一个 `match`（**一组**状态），而不是靠"桶名 == 状态名"
 *
 * 这是被一个真实 bug 逼出来的（2026-10-12 用户截图）：
 *
 * 后端有 **7** 个状态，而这里只有 5 个桶 —— `environment_ready`（环境就绪）
 * 与 `submitting`（提交中）**不属于任何桶**。而筛选与计数都用严格相等
 * （`c.status === item.value`）去比，于是那两类题：
 *
 *   · **不计入任何桶的计数** → 筛选栏加起来 ≠ 全部
 *     （实测：`94 + 0 + 0 + 2 + 0 = 96`，而「全部」明明是 98）；
 *   · **点开任何桶都看不到** → 2 道正在跑环境的题凭空消失，
 *     用户看到的是一句"这个状态下没有题目"。
 *
 * 这个 bug 的形状很好认：**各桶之和 ≠ 全部**。所以只要那个等式不成立，
 * 就说明又漏了状态。下面 `match` 的并集必须等于 `ALL_CHALLENGE_STATUSES`。
 *
 * ## 为什么「解题中」收纳三个状态，而「待确认」单独一桶
 *
 * 对用户来说，`environment_ready` / `solving` / `submitting` 是同一件事：
 * **"系统正在替我搞这道题"**。拆成三个 tab 只会让工具条变长，而用户
 * 并不区分"在起环境"和"在跑模型"。
 *
 * 但 `candidate_ready` **不并进去**：它是唯一需要**人动手**的桶，
 * 并进"解题中"会让它在计数里被淹没 —— 而用户一进页面最该看到的就是它。
 */
const STATUS_FILTERS: {
  value: KernelChallengeStatus | 'all'
  label: string
  /** 这个桶收纳哪些后端状态。`all` 收全部。 */
  match: readonly KernelChallengeStatus[]
}[] = [
  { value: 'all', label: '全部', match: ALL_CHALLENGE_STATUSES },
  { value: 'unsolved', label: CHALLENGE_STATUS_LABEL.unsolved, match: ['unsolved'] },
  {
    value: 'solving',
    label: CHALLENGE_STATUS_LABEL.solving,
    match: ['environment_ready', 'solving', 'submitting'],
  },
  // ★ 排在靠前：它是**唯一需要人动手**的状态，
  //   而它的位置决定了用户一进页面会不会看到它。
  {
    value: 'candidate_ready',
    label: CHALLENGE_STATUS_LABEL.candidate_ready,
    match: ['candidate_ready'],
  },
  { value: 'solved', label: CHALLENGE_STATUS_LABEL.solved, match: ['solved'] },
  { value: 'failed', label: CHALLENGE_STATUS_LABEL.failed, match: ['failed'] },
]

/**
 * 分类筛选（第二个维度）。
 *
 * ★ 为什么分类值来自 `canonicalCategoryOf()` 而不是平台原话：平台的
 *   `category` 是 `REVERSE` / `第01题 web入门` / `basic` 这种脏值，
 *   直接拿来当筛选键会出现"每个脏值一个按钮"的灾难。归一后的七类
 *   才是可筛选的稳定集合。**归一的真值在服务端**，这里是投影。
 */
const CATEGORY_FILTERS: { value: CanonicalCategory | 'all'; label: string }[] = [
  // 曾经叫「全部类」—— 挨着来源那排的「全部来源」，两个「全部」看起来像同一个
  // 控件的两种写法。现在两排各自有组名（状态 / 类型 / 来源），这里就用「全部」。
  { value: 'all', label: '全部' },
  ...CANONICAL_CATEGORIES.map((value) => ({
    value,
    label: CANONICAL_CATEGORY_LABEL[value],
  })),
]

/**
 * 来源筛选（第三个维度）：按「接入」分开看题。
 *
 * ★ 为什么不写死成两个固定按钮（CTF2 / DASCTF Pro）：接入是**可增删**的，
 *   写死会让"新识别一个平台"之后它在筛选栏里不存在，题只能并到「全部」里看不见。
 *   所以选项从**当前题目集合里真实出现过的 `platformSlug`** 现算 ——
 *   有哪个接入的题，就出现哪个按钮；没题的接入不必占一格。
 */
type SourceFilter = string // platformSlug，或 'all'

// ── ★ 开发期不变量：各桶的并集必须覆盖**全部**状态 ────────────────
//
// 漏掉一个状态的后果不是"少一个 tab"，而是**那类题在任何筛选下都看不见**，
// 并且筛选栏的数字对不上「全部」—— 而这两件事都不会抛异常，只会让人
// 以为"题目丢了"（用户 2026-10-12 的截图正是这样：98 道题只数得出 96 道，
// 2 道正在跑环境的题凭空消失）。
//
// 这里用一个显式检查把它变成一条**立刻可见的告警**，而不是留给下一个人
// 去数加法。判定形状很好记：**各桶之和 ≠ 全部**就是出了问题。
if (import.meta.env.DEV) {
  const covered = new Set(STATUS_FILTERS.flatMap((bucket) => bucket.match))
  const missing = ALL_CHALLENGE_STATUSES.filter((status) => !covered.has(status))
  if (missing.length > 0) {
    console.warn(
      `[ctf2] 状态筛选桶漏了：${missing.join('、')} —— ` +
        '这些题会在**所有**筛选下消失，且筛选栏之和 ≠ 全部。' +
        '请把它们补进某个桶的 `match`（见 STATUS_FILTERS 的说明）。',
    )
  }
}

export function KernelPage() {
  const { t, locale } = useI18n()
  const { account, challenges, settings, settingsError, queue, status, error, notice } = useKernel()

  const [filter, setFilter] = useState<KernelChallengeStatus | 'all'>('all')
  const [categoryFilter, setCategoryFilter] = useState<CanonicalCategory | 'all'>('all')
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>('all')
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState(false)
  const navigate = useNavigate()
  /** 正在确认提交的那道题 —— 存**整个对象**而不是 id */
  const [pending, setPending] = useState<KernelChallenge | null>(null)
  /** 待取消的那道题（取消会丢进度，所以要确认） */
  const [canceling, setCanceling] = useState<KernelChallenge | null>(null)
  /** 这次解题用哪个镜像（默认取设置里的默认值） */
  const [profile, setProfile] = useState<KernelSandboxProfile | null>(null)
  /** 赛事选择弹窗（同步**赛事**题目用 —— 练习集同步不需要选） */
  const [picking, setPicking] = useState(false)
  /**
   * 解绑确认弹窗。
   *
   * ★ 这个开关之前**不存在** —— `unbindKernelAccount()` 写在 data 层里，
   * 但没有任何页面调用它，于是界面上既退不出也换不了账号
   * （用户报的原话："这个账号也退出不了替换"）。
   * 后端端点一直是好的，缺的就是这一个入口。
   */
  const [unbinding, setUnbinding] = useState(false)
  /** 未绑定时，页内展开的绑定表单（不再整页顶掉题库） */
  const [binding, setBinding] = useState(false)
  /**
   * 单题拉取。
   *
   * ★ 为什么需要它：CTF2 的开放 API **没有**"列出练习集题目"的接口
   *   （实测 11 条候选路径全部 `resource=route`），"同步题目"因此只能拉回
   *   练习集的元信息 —— 界面上永远是 0 道题。而"按 id 取单题详情"是活的，
   *   所以给出这条"粘一条链接、拉一道题"的通路。
   */
  const [pulling, setPulling] = useState(false)
  const [pullInput, setPullInput] = useState('')

  /**
   * 正在看题解的那道题（v0.36）。
   *
   * ★ 存的是**整个 challenge 对象**，不是 id —— 与 ScansPage 的
   *   `writeupScan` 同一个理由，而且这里更明显：弹窗头部要显示题目名，
   *   而这一页**每 3 秒轮询整体替换** `challenges`。只存 id 的话，
   *   那道题在刷新后状态一变（比如刚跑完），标题就可能取不到 / 变化，
   *   而用户按的是**当时看到的那一行**。
   *
   * ⚠️ 题解属于"那一轮运行的结果"，名字就该是打开那一刻的那个。
   */
  const [writeupChallenge, setWriteupChallenge] = useState<KernelChallenge | null>(null)

  /*
    要展示**处置工单**的那道题（v0.41）。

    ★ 与 `writeupChallenge` 分开两个 state，不合并成一个
    `{challenge, kind}`：两者可以同时打开吗？技术上不会（都是模态），
    但合并会让"关闭时清哪一个"变成需要判断的事，而判断错的表现是
    "关了工单，题解还开着"。两个独立 state 各自清各自的，不需要想。
  */
  const [workOrderChallenge, setWorkOrderChallenge] = useState<KernelChallenge | null>(null)

  useEffect(() => {
    void loadKernel()
  }, [])

  /*
    轮询：只在**有在动的题**时开。

    ★ 与 ScansPage 同一条规矩：终态（solved / failed / unsolved）的题
      不会自己变化，为它们轮询是白烧请求。
  */
  const active = useMemo(() => hasActiveChallenges(challenges), [challenges])

  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => {
      // silent：轮询失败不该把页面打成错误态
      void loadKernel({ silent: true })
    }, 3000)
    return () => window.clearInterval(timer)
  }, [active])

  /** 提示条自动消失 —— 它只是"刚才那件事的结果"，不是页面状态 */
  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => clearKernelNotice(), 6000)
    return () => window.clearTimeout(timer)
  }, [notice])

  /**
   * 只按「状态」收窄后的集合 —— 分类/来源筛选的**计数**要基于它。
   *
   * ★ 为什么计数不直接用 `challenges`：如果分类标签的计数用全量算，
   *   用户在「未开始」下点「逆向」会看到"标签写 67、列表只 4 条"，
   *   看起来像坏了。用本状态桶算才和列表一致。
   */
  const challengesInStatusBucket = useMemo(() => {
    const bucket = STATUS_FILTERS.find((f) => f.value === filter) ?? STATUS_FILTERS[0]
    return challenges.filter((item) => bucket.match.includes(item.status))
  }, [challenges, filter])

  /**
   * 实际出现过的接入来源（去重、按出现顺序）。来源筛选项由此现算 ——
   * 见 `SourceFilter` 的说明：写死会让新接入的题无处可筛。
   */
  const sourceOptions: { value: string; label: string }[] = useMemo(() => {
    const seen = new Map<string, string>()
    for (const c of challenges) {
      if (!c.platformSlug) continue
      if (!seen.has(c.platformSlug)) seen.set(c.platformSlug, c.platformName || c.platformSlug)
    }
    return [...seen.entries()].map(([value, label]) => ({ value, label }))
  }, [challenges])

  const filtered = useMemo(() => {
    const text = query.trim().toLowerCase()
    // ★ 用桶的 `match` 而不是"桶名 == 状态名" —— 见 `STATUS_FILTERS` 的说明。
    //   严格相等会让 `environment_ready` / `submitting` 那两类题
    //   **从所有筛选下消失**（它们不属于任何一个桶）。
    return challengesInStatusBucket.filter((item) => {
      // 来源维度：与状态/分类都是「与」关系
      if (sourceFilter !== 'all' && (item.platformSlug || '') !== sourceFilter) {
        return false
      }
      // 分类维度：与状态是「与」关系
      if (
        categoryFilter !== 'all' &&
        canonicalCategoryOf(item.canonicalCategory) !== categoryFilter
      ) {
        return false
      }
      if (!text) return true
      return (
        item.title.toLowerCase().includes(text) ||
        item.category.toLowerCase().includes(text) ||
        // 也搜标准中文类名与来源 —— 用户按「逆向」或「DASCTF」搜时要搜得到
        CANONICAL_CATEGORY_LABEL[canonicalCategoryOf(item.canonicalCategory)]
          .toLowerCase()
          .includes(text) ||
        (item.platformName || '').toLowerCase().includes(text) ||
        item.remoteId.includes(text)
      )
    })
  }, [challengesInStatusBucket, sourceFilter, categoryFilter, query])

  // ── 动作 ──────────────────────────────────────────────
  //
  // 每个动作都遵守同一条：**失败用 toast，成功不弹窗**。
  // 理由是 ScansPage 那段实测记录的教训 —— 把失败渲染在某处的
  // 小字里等于"点了没反应"，而用户会去查服务端日志才发现有 409。

  /**
   * 同步。[competitionIds] 非空时同步的是**赛事**（那条链路要逐场指定）。
   *
   * ⚠️ 两条路径共用同一个函数：赛事侧的返回形状与练习集完全一样
   * （都是 `KernelSyncResult`），而且**成功/失败的判读方式也必须一样** ——
   * 抄一份的后果是"附件失败提示"只在其中一条路径上出现。
   */
  const runSync = useCallback((competitionIds: string[] = []) => {
    setBusy(true)
    void syncKernel(competitionIds)
      .then((result) => {
        /*
          ★ 附件失败要**单独说**，而且用风险色。
          它是"题面同步成功、但解题会失败得莫名其妙"的唯一可见线索 ——
          agent 手里根本没有附件，而它只会说"我找不到题面提到的文件"。
          只显示一句 `result.message` 的话，这个信号会淹没在
          "赛事 2 · 新增题目 17" 里。
        */
        const failed = result.attachmentsFailed
        /*
          ★ 赛事同步的失败（没加战队、赛事被删）也要说出来。

          它们**不会**让整次同步失败（后端逐场容错，见 `_fetch` 的说明），
          所以如果不专门显示，用户会看到"同步完成：赛事 0 · 新增题目 0"
          而完全不知道原因 —— 那正是这次排查的起点。
        */
        const problems = result.errors.slice(0, 1).join('')
        const suffix = problems ? ` ${problems}` : ''
        showToast({
          message:
            failed > 0
              ? t(
                  'kernel.toast.syncPartial',
                  { n: failed, msg: result.message },
                  `同步完成，但有 ${failed} 个附件没下下来（解题时 agent 会拿不到它们）。${result.message}`,
                )
              : t('kernel.toast.syncOk', { msg: result.message, suffix }, `同步完成：${result.message}${suffix}`),
          tone: failed > 0 || problems ? 'risk' : undefined,
        })
      })
      .catch((err) =>
        showToast({
          message: t('kernel.toast.syncFailed', { msg: errorMessage(err) }, `同步失败：${errorMessage(err)}`),
          tone: 'risk',
        }),
      )
      .finally(() => setBusy(false))
  }, [t])

  /**
   * 从**比赛平台**同步题目。与 `runSync` 分开一个函数：失败口径不同 ——
   * 这里最常见的失败是"accesskey 没开 agent 权限"，后端已把它翻成一句
   * 可行动的话，直接整句显示即可（不要拼"同步完成"）。
   */
  const runSyncCompetition = useCallback(() => {
    setBusy(true)
    void syncCompetition()
      .then((result) => {
        const problems = result.errors[0] ?? ''
        showToast({
          message: problems
            ? t(
                'kernel.toast.competitionPartial',
                { msg: result.message, problems },
                `比赛平台同步：${result.message} ${problems}`,
              )
            : t('kernel.toast.competitionOk', { msg: result.message }, `比赛平台同步完成：${result.message}`),
          tone: problems ? 'risk' : undefined,
        })
      })
      .catch((err) =>
        showToast({
          message: t(
            'kernel.toast.competitionFailed',
            { msg: errorMessage(err) },
            `比赛平台同步失败：${errorMessage(err)}`,
          ),
          tone: 'risk',
        }),
      )
      .finally(() => setBusy(false))
  }, [t])

  /**
   * 按链接拉取一道题。
   *
   * 成功时把弹窗关掉并用 toast 报"拉到了哪一道、挂在哪个练习集下" ——
   * 用户粘的是一条链接，他需要确认**拉到的就是他想要的那道**。
   */
  const runPull = useCallback(async () => {
    const raw = pullInput.trim()
    if (!raw) return
    setBusy(true)
    try {
      const result = await pullKernelChallenge(raw)
      setPulling(false)
      showToast({
        message: t(
          'kernel.toast.pulled',
          {
            action: result.created
              ? t('kernel.action.pulled', undefined, '拉取')
              : t('kernel.action.updated', undefined, '更新'),
            title: result.challenge.title,
            competition: result.competitionName,
          },
          `已${result.created ? '拉取' : '更新'}「${result.challenge.title}」（${result.competitionName}）`,
        ),
      })
    } catch (err) {
      showToast({
        message: t('kernel.toast.pullFailed', { msg: errorMessage(err) }, `拉取失败：${errorMessage(err)}`),
        tone: 'risk',
      })
    } finally {
      setBusy(false)
    }
  }, [pullInput, t])

  const runSolve = useCallback(
    (challenge: KernelChallenge, resume = false) => {
      setBusy(true)
      void solveChallenge(challenge.id, {
        // 不传 `mode` 时后端用设置里的值。这里显式传它，
        // 是为了让"这次解题用的什么策略"与界面上显示的一致。
        mode: (settings?.submitMode ?? 'safe') as KernelSubmitMode,
        sandboxProfile: profile ?? challenge.sandboxProfile,
        // ★ v0.26：接着上一次的进度跑。后端找不到上次的过程文件时
        //   会退回从头开始（并记一条日志）—— 所以这里不需要先确认。
        resume,
      })
        .then((result) => showToast({ message: result.message }))
        .catch((err) =>
          showToast({
            message: t('kernel.toast.solveFailed', { msg: errorMessage(err) }, `启动失败：${errorMessage(err)}`),
            tone: 'risk',
          }),
        )
        .finally(() => setBusy(false))
    },
    [profile, settings?.submitMode, t],
  )

  /**
   * 打开这道题的对话（CTF 页重做第 2 步）。
   *
   * ★ 会话是**服务端 find-or-create** 的：同一道题再点一次回到同一条
   *   时间线，不会多出空对话（见 `openChallengeSession`）。
   * ★ 不在这里 `setBusy`：它是一个跳转，不是"平台正忙"—— 按钮上
   *   转圈只会让人以为页面卡住了。
   */
  const runOpen = useCallback(
    (challenge: KernelChallenge) => {
      void openChallengeSession(challenge.id).then((session) => {
        if (!session) {
          showToast({
            message: t('kernel.toast.openFailed', undefined, '打开对话失败，请稍后重试'),
            tone: 'risk',
          })
          return
        }
        navigate(`/chat?session=${encodeURIComponent(session.id)}`)
      })
    },
    [navigate, t],
  )

  /**
   * 批量开始 / 停止（第 3 步）。
   *
   * ★ toast 里带**第一条失败原因**：16 道里 2 道没起来时，"2 道失败"
   *   不解决问题，用户要知道为什么（多数是"已经解出"或"正在跑"）。
   * ★ `resume: false`：批量是"把这些题开起来"，不是逐题接着上次跑。
   *   要继续某一道的进度，用行内那个「接着解」。
   */
  const runBatch = useCallback(
    (action: 'solve' | 'cancel', ids: string[]) => {
      if (ids.length === 0) return
      setBusy(true)
      void batchChallenges(action, ids, {
        mode: (settings?.submitMode ?? 'safe') as KernelSubmitMode,
        sandboxProfile: profile ?? undefined,
        resume: false,
      })
        .then((result) => {
          const firstFailure = result.results.find((item) => !item.ok)
          showToast({
            message:
              t(
                action === 'solve' ? 'kernel.toast.batchSolve' : 'kernel.toast.batchStop',
                { n: result.succeeded },
                `批量${action === 'solve' ? '开始' : '停止'}：${result.succeeded} 道成功`,
              ) +
              (result.failed > 0
                ? t(
                    'kernel.toast.batchFailedTail',
                    {
                      n: result.failed,
                      reason:
                        firstFailure?.message ?? t('kernel.reasonUnknown', undefined, '原因未知'),
                    },
                    `，${result.failed} 道失败（${firstFailure?.message ?? '原因未知'}）`,
                  )
                : ''),
            tone: result.failed > 0 ? 'risk' : undefined,
          })
        })
        .catch((err) =>
          showToast({
            message: t('kernel.toast.batchFailed', { msg: errorMessage(err) }, `批量操作失败：${errorMessage(err)}`),
            tone: 'risk',
          }),
        )
        .finally(() => setBusy(false))
    },
    [profile, settings?.submitMode, t],
  )

  const runSubmit = useCallback(
    (flag: string) => {
      if (!pending) return
      setBusy(true)
      void submitFlag(pending.id, flag, (settings?.submitMode ?? 'safe') as KernelSubmitMode)
        .then((result) => {
          setPending(null)
          // 判定错误用默认色而不是 risk：猜错在解题里是**常态**，
          // 每次都报红会让人不再看那个颜色（与 events 映射同一条理由）。
          showToast({
            message: result.message,
            tone:
              result.status === 'correct' ? 'ok' : result.status === 'wrong' ? undefined : 'risk',
          })
        })
        .catch((err) =>
          showToast({
            message: t('kernel.toast.submitFailed', { msg: errorMessage(err) }, `提交失败：${errorMessage(err)}`),
            tone: 'risk',
          }),
        )
        .finally(() => setBusy(false))
    },
    [pending, settings?.submitMode, t],
  )

  const runCancel = useCallback(() => {
    if (!canceling) return
    setBusy(true)
    void cancelChallenge(canceling.id)
      .then(() => {
        setCanceling(null)
        /*
          ★ 这句提示必须出现。取消**杀不掉已经在跑的 worker** ——
            那一轮会跑完。不说的话，用户点完取消看到进度还在动，
            会以为按钮坏了（见后端 `cancel_challenge` 的说明）。
        */
        showToast({
          message: t('kernel.toast.canceled', undefined, '已取消。若它已经在跑，那一轮会跑完但不会产出候选。'),
        })
      })
      .catch((err) =>
        showToast({
          message: t('kernel.toast.cancelFailed', { msg: errorMessage(err) }, `取消失败：${errorMessage(err)}`),
          tone: 'risk',
        }),
      )
      .finally(() => setBusy(false))
  }, [canceling, t])

  const runRelease = useCallback((challenge: KernelChallenge) => {
    setBusy(true)
    void releaseEnvironment(challenge.id)
      .catch((err) =>
        showToast({
          message: t('kernel.toast.releaseFailed', { msg: errorMessage(err) }, `释放失败：${errorMessage(err)}`),
          tone: 'risk',
        }),
      )
      .finally(() => setBusy(false))
  }, [t])

  /**
   * 调整排队顺序（v0.23）。
   *
   * ★ 比赛规则里"前三名额外加分"让**先做哪道题**直接换分，而同一条
   *   规则也明确允许"调整解题优先级"。
   *
   * ⚠️ 不弹确认框：这是**可撤销**的动作（再点一次"延后"就回去了），
   *   也不对外发任何东西。给一个可撤销的排序操作加确认框，会让
   *   "比赛现场连点几下调整顺序"变成一场点击马拉松。
   */
  const runSetPriority = useCallback((challenge: KernelChallenge, priority: number) => {
    setBusy(true)
    void setChallengePriority(challenge.id, priority)
      .catch((err) =>
        showToast({
          message: t('kernel.toast.priorityFailed', { msg: errorMessage(err) }, `调整失败：${errorMessage(err)}`),
          tone: 'risk',
        }),
      )
      .finally(() => setBusy(false))
  }, [t])

  /**
   * 按分值批量调整排队顺序。
   *
   * ★ 它**必须由人点** —— 后端不会自动做。「先做高分题」还是
   *   「先做容易题」是战术判断（高分题往往更难，开赛初期把并发
   *   压在几道硬题上可能一道都拿不下），系统替用户猜只会猜错。
   *
   * ⚠️ 不传 `competitionId`：这个页面没有"当前赛事"的概念
   *    （`challenges` 是全量列表），而"排队中的题"本来就是全局限定的。
   *    传一个猜出来的赛事 id 反而会让另一场赛事的排队题被漏掉。
   */
  const runPrioritizeByScore = useCallback(() => {
    setBusy(true)
    void prioritizeByScore()
      .then((changed) => {
        if (changed > 0) {
          showToast({
            message: t(
              'kernel.toast.priorityChanged',
              { n: changed },
              `已按分值调整 ${changed} 道排队题的优先级。`,
            ),
          })
        }
      })
      .catch((err) =>
        showToast({
          message: t('kernel.toast.priorityFailed', { msg: errorMessage(err) }, `调整失败：${errorMessage(err)}`),
          tone: 'risk',
        }),
      )
      .finally(() => setBusy(false))
  }, [t])

  // ── 渲染 ──────────────────────────────────────────────

  /*
    ★★ 没有账号时**不再**把整页顶掉（2026-11-08 用户反馈）。

    解绑对话框自己写着"已经同步下来的赛事与题目会保留"，而这一页却把
    题库整块藏了 —— 两句话互相拆台。更实际的是：题目可能来自**别的接入**
    （DASCTF Pro / BUUCTF …），它们根本不依赖 CTF2 的 PAT；一条账号解绑，
    不该连带把别人的题一起藏起来。

    所以只在"什么题都没有"的首次使用场景里整页引导，其余情况在页内
    挂一条提示 + 可展开的绑定表单。
  */
  if (status === 'ready' && !account && challenges.length === 0) {
    return (
      <>
        <PageHeader navId="kernel" />
        <div className="px-gutter py-bay">
          <BindPrompt />
        </div>
      </>
    )
  }

  return (
    <>
      <PageHeader navId="kernel" />

      <div className="px-gutter py-bay">
        {/*
          ★ 未绑定时**不隐藏**整场托管面板：它可能正跑着（解绑不会让
            已经起跑的赛跑停下），而停止按钮就在面板里 —— 藏掉它等于
            让人没法收场。只在开始那一侧禁用并说明原因。
        */}
        {!account && (
          <div className="mb-panel flex flex-col gap-2 rounded-sm border border-signal-warn/40 bg-signal-warn/[0.06] p-2.5">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs text-ink-muted">
              <TriangleAlert
                size={12}
                strokeWidth={2}
                className="shrink-0 text-signal-warn"
                aria-hidden="true"
              />
              <span className="text-ink"><T k="kernel.unbound" fallback="还没绑定 CTF2 账号" /></span>
              <span>
                <T
                  k="kernel.unbound.desc"
                  fallback="同步新题与发起解题需要它；下面这些题是之前同步下来的，仍可浏览、看题解与解题过程。"
                />
              </span>
              <span className="flex-1" />
              <Button variant="secondary" size="sm" onClick={() => setBinding((value) => !value)}>
                {binding
                  ? t('kernel.unbound.collapse', undefined, '收起')
                  : t('kernel.unbound.expand', undefined, '绑定账号')}
              </Button>
              <Link
                to="/settings#platform"
                className="text-2xs text-accent-strong underline hover:text-accent"
              >
                <T k="kernel.platformLink" fallback="平台接入" />
              </Link>
            </div>
            {binding && <BindPrompt />}
          </div>
        )}

        <div className="mb-panel">
          <RacePanel startDisabledReason={account ? null : t('kernel.unbound', undefined, '还没绑定 CTF2 账号')} />
        </div>

        {/* ── 账号状态条 ── */}
        {account && (
          <div className="mb-panel flex flex-wrap items-center gap-x-tight gap-y-1 rounded-sm border border-line bg-surface-sunken p-2.5">
            <span className="text-xs text-ink">{account.remoteUsername || account.label}</span>
            <span className="mono-ui text-2xs text-ink-faint">{account.tokenMask}</span>
            {account.lastSyncedAt && (
              <span className="text-2xs text-ink-faint">
                {t(
                  'kernel.account.lastSync',
                  { time: new Date(account.lastSyncedAt).toLocaleString(locale) },
                  `上次同步 ${new Date(account.lastSyncedAt).toLocaleString(locale)}`,
                )}
              </span>
            )}
            {queue && (
              /*
                ★ 这里的 limit 是**解题自己的配额**（v0.22 起不再等于
                扫描那个上限）。加 title 说清它是什么 —— 用户看到
                "在跑 2/3" 时会想知道那个 3 从哪来、去哪调，而答案在
                设置页的 CTF2 面板里，不在这里。
              */
              <span
                className="tabular text-2xs text-ink-faint"
                title={t(
                  'kernel.account.queueTitle',
                  undefined,
                  '同时解题的题目数上限（在「设置 → CTF2」里调）。它与扫描并发是两个独立配额。',
                )}
              >
                {t(
                  'kernel.account.running',
                  { running: queue.running, limit: queue.limit },
                  `在跑 ${queue.running}/${queue.limit}`,
                )}
                {queue.queued > 0
                  ? t('kernel.account.queued', { n: queue.queued }, ` · 排队 ${queue.queued}`)
                  : ''}
              </span>
            )}
            <span className="flex-1" />
            <Link
              to="/settings#ctf"
              className="text-2xs text-accent-strong underline hover:text-accent"
            >
              <T k="kernel.account.submitSettings" fallback="提交策略设置" />
            </Link>
            <Button
              variant="link-muted"
              size="xs"
              disabled={busy}
              /*
                禁用可见性由调用方补（`link` 族刻意不带禁用样式，见 Button.tsx）。
                但要的是 `cursor-not-allowed`（「先别动，平台正忙」）而不是 `busy`
                那档 `cursor-wait`。原来补的是 `opacity-50` —— 行内链接压一半会
                掉到 2.2:1，而它旁边没有任何替代入口。改成 `link-muted` 自己的
                禁用色（`ink-faint`，暗 5.21:1 / 亮 4.98:1）。
              */
              className={busy ? 'cursor-not-allowed text-ink-faint' : undefined}
              onClick={() => setUnbinding(true)}
            >
              <T k="kernel.account.unbind" fallback="解绑 / 换绑" />
            </Button>
          </div>
        )}

        {/*
          ★ 设置读不到时必须出声。
          `/kernel/settings` 是 admin 级，只有 read 的会话会拿到 403 ——
          那时界面回落到 `safe` 是**对的**，但"我改过的策略怎么不见了"
          必须有答案，否则它会变成一个无法解释的现象。
        */}
        {settingsError && (
          <div className="mb-panel flex items-start gap-2 rounded-sm border border-dashed border-line-strong p-2.5">
            <TriangleAlert
              size={12}
              strokeWidth={2}
              className="mt-0.5 shrink-0 text-ink-faint"
              aria-hidden="true"
            />
            <span className="text-2xs text-ink-muted">
              {t(
                'kernel.settingsError',
                { msg: settingsError.message },
                `读不到 CTF2 策略设置（${settingsError.message}），界面按默认值「先确认再提交」显示。当前凭据可能没有配置读取权限。`,
              )}
            </span>
          </div>
        )}

        {/* ── 工具条 ── */}
        <div className="mb-panel flex flex-wrap items-center gap-tight">
          <div
            role="group"
            aria-label={t('kernel.group.statusAria', undefined, '按状态筛选')}
            className="flex flex-wrap items-center gap-1"
          >
            {/* 三排胶囊各有一个可见组名：没有它，「全部」会出现两次而看不出
                它们属于不同维度。组名对读屏器是重复的（aria-label 已说），故 aria-hidden。 */}
            <span aria-hidden="true" className="mr-0.5 shrink-0 text-2xs text-ink-faint">
              <T k="kernel.group.status" fallback="状态" />
            </span>
            {STATUS_FILTERS.map((item) => {
              {
                /* ★ 计数与筛选**用同一份 `match`**：两处各写一遍必然漂移，
                  而漂移的表现就是"标签上的数字和点进去看到的条数不一致"。 */
              }
              const count = challenges.filter((c) => item.match.includes(c.status)).length
              const isActive = filter === item.value
              return (
                <ToggleChip
                  key={item.value}
                  active={isActive}
                  onToggle={() => setFilter(item.value)}
                >
                  {t(`kernel.status.${item.value}`, undefined, item.label)}
                  <span
                    className={cn(
                      'tabular text-2xs',
                      isActive ? 'text-accent-strong' : 'text-ink-faint',
                    )}
                    data-numeric
                  >
                    {count}
                  </span>
                </ToggleChip>
              )
            })}
          </div>

          {/*
            ── 分类筛选（第二个维度，与状态**同时**生效） ──
            ★ 与上面那排状态是"与"关系，不是替换关系：先按状态收窄到
              "未开始"，再在这批里点「逆向」，看到的才是"未开始的逆向题"。
              两者共用一个 `filtered`，所以计数与列表天然一致。
            ★ 计数按**当前状态筛选后**的集合算 —— 否则会出现"点了逆向
              显示 0 道题、但标签上写着 67"的错觉。
          */}
          <div
            role="group"
            aria-label={t('kernel.group.categoryAria', undefined, '按分类筛选')}
            className="flex flex-wrap items-center gap-1"
          >
            <span aria-hidden="true" className="mr-0.5 shrink-0 text-2xs text-ink-faint">
              <T k="kernel.group.category" fallback="类型" />
            </span>
            {CATEGORY_FILTERS.map((item) => {
              const count = challengesInStatusBucket.filter((c) =>
                item.value === 'all'
                  ? true
                  : canonicalCategoryOf(c.canonicalCategory) === item.value,
              ).length
              const isActive = categoryFilter === item.value
              return (
                <ToggleChip
                  key={item.value}
                  active={isActive}
                  onToggle={() => setCategoryFilter(item.value)}
                >
                  {t(`kernel.category.${item.value}`, undefined, item.label)}
                  <span
                    className={cn(
                      'tabular text-2xs',
                      isActive ? 'text-accent-strong' : 'text-ink-faint',
                    )}
                    data-numeric
                  >
                    {count}
                  </span>
                </ToggleChip>
              )
            })}
          </div>

          {/*
            ── 来源筛选（第三个维度：按「接入」分开看题） ──
            ★ 只要**有题目带着来源**就显示，哪怕只有一个来源：这一行本身
              就在回答"这些题是从哪个接入同步来的"（用户 2026-11-03 的原话：
              "接入之后题目在哪"）。只有全都没有来源信息时才隐藏 —— 那时
              每个按钮都等于「全部」，纯噪音。
            ★ 选项由 `sourceOptions` 从真实题目里现算 —— 新识别一个平台、
              它同步下来的题一进来，这里就自动多一格，不需要改代码。
            ★ 计数同样按当前状态桶算，和列表口径一致。
          */}
          {sourceOptions.length > 0 && (
            <div
              role="group"
              aria-label={t('kernel.group.sourceAria', undefined, '按接入来源筛选')}
              className="flex flex-wrap items-center gap-1"
            >
              <span aria-hidden="true" className="mr-0.5 shrink-0 text-2xs text-ink-faint">
                <T k="kernel.group.source" fallback="来源" />
              </span>
              {[
                { value: 'all', label: t('kernel.source.all', undefined, '全部来源') },
                ...sourceOptions,
              ].map((item) => {
                const count = challengesInStatusBucket.filter((c) =>
                  item.value === 'all' ? true : (c.platformSlug || '') === item.value,
                ).length
                const isActive = sourceFilter === item.value
                return (
                  <ToggleChip
                    key={item.value}
                    active={isActive}
                    onToggle={() => setSourceFilter(item.value)}
                  >
                    {item.label}
                    <span
                      className={cn(
                        'tabular text-2xs',
                        isActive ? 'text-accent-strong' : 'text-ink-faint',
                      )}
                      data-numeric
                    >
                      {count}
                    </span>
                  </ToggleChip>
                )
              })}
            </div>
          )}

          <div className="flex-1" />

          <div className="relative">
            <Search
              size={13}
              strokeWidth={1.75}
              className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-ghost"
              aria-hidden="true"
            />
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t('kernel.search.placeholder', undefined, '筛选题目')}
              aria-label={t('kernel.search.aria', undefined, '按标题、分类或编号筛选')}
              className={cn(
                'h-8 w-40 rounded-sm border border-line bg-surface-sunken pl-7 pr-2 text-xs text-ink',
                'transition-ui duration-fast placeholder:text-ink-faint',
                'hover:border-line-strong focus:outline-none focus:ring-2 focus:ring-accent/40',
              )}
            />
          </div>

          <Button
            variant="secondary"
            size="sm"
            disabled={busy || !account}
            onClick={() => {
              setPullInput('')
              setPulling(true)
            }}
            title={
              account
                ? t(
                    'kernel.pull.titleEnabled',
                    undefined,
                    '把平台上的题目链接粘进来，按 id 精确拉取这一道题',
                  )
                : t(
                    'kernel.pull.titleDisabled',
                    undefined,
                    '还没绑定 CTF2 账号 —— 拉取单题要用它访问平台',
                  )
            }
          >
            <Link2 size={12} strokeWidth={2} aria-hidden="true" />
            <T k="kernel.pull" fallback="拉取单题" />
          </Button>

          <Button
            variant="secondary"
            size="sm"
            disabled={busy || queue?.queued === 0}
            onClick={runPrioritizeByScore}
            /*
              ★ 这条 title 必须说清两件事：它做什么、以及**为什么不是
              默认行为**。否则用户会问"既然按分值排更合理，为什么还要
              我点一下"—— 而答案（高分题往往更难，先做哪个是战术判断）
              不写出来就没人知道。
            */
            title={
              queue?.queued === 0
                ? t('kernel.prioritize.titleEmpty', undefined, '当前没有排队中的题')
                : t(
                    'kernel.prioritize.title',
                    undefined,
                    '把排队中的题按分值从高到低重排优先级。它不会自动执行 —— 先做高分题还是先做容易题是战术选择，取决于当前比分与剩余时间，由你决定。',
                  )
            }
          >
            <ListOrdered size={12} strokeWidth={2} aria-hidden="true" />
            <T k="kernel.prioritize" fallback="按分值排队" />
          </Button>

          <Button
            variant="secondary"
            size="sm"
            disabled={busy || !account}
            onClick={() => setPicking(true)}
            title={
              account
                ? t(
                    'kernel.sync.competitionTitle',
                    undefined,
                    '同步指定赛事的阶段与题目（赛事题目只能查看，不能在这里解题）',
                  )
                : t(
                    'kernel.sync.competitionTitleDisabled',
                    undefined,
                    '还没绑定 CTF2 账号 —— 同步赛事题目要用它访问平台',
                  )
            }
          >
            <Trophy size={12} strokeWidth={2} aria-hidden="true" />
            <T k="kernel.sync.competition" fallback="同步赛事题目" />
          </Button>

          <Button
            variant="secondary"
            size="sm"
            disabled={busy || !account}
            onClick={() => runSync()}
            title={
              account
                ? undefined
                : t('kernel.sync.allTitleDisabled', undefined, '还没绑定 CTF2 账号 —— 同步要用它访问平台')
            }
          >
            <RefreshCw
              size={12}
              strokeWidth={2}
              className={busy ? 'animate-spin' : undefined}
              aria-hidden="true"
            />
            <T k="kernel.sync.all" fallback="同步题目" />
          </Button>

          <Button
            variant="secondary"
            size="sm"
            disabled={busy}
            onClick={() => runSyncCompetition()}
            title={t(
              'kernel.sync.matchTitle',
              undefined,
              '从比赛平台（羊城杯这类 slab-match 平台）把题目拉进本地库',
            )}
          >
            <Trophy size={12} strokeWidth={2} aria-hidden="true" />
            <T k="kernel.sync.match" fallback="同步比赛题" />
          </Button>
        </div>

        {/* ── 结果提示 ── */}
        {notice && (
          <div className="mb-panel flex animate-rise-in items-center justify-between gap-panel rounded-sm border border-signal-ok/40 bg-signal-ok/[0.06] px-panel py-2.5">
            <p className="flex items-center gap-2 text-sm text-ink">
              <CheckCircle2
                size={15}
                strokeWidth={2}
                className="shrink-0 text-signal-ok"
                aria-hidden="true"
              />
              {notice}
            </p>
            <IconButton
              label={t('kernel.notice.close', undefined, '关闭提示')}
              onClick={clearKernelNotice}
            >
              <X size={13} strokeWidth={2} aria-hidden="true" />
            </IconButton>
          </div>
        )}

        {/* ── 策略提示（auto 时出声）── */}
        {settings?.submitMode === 'auto' && (
          <div className="mb-panel flex items-start gap-2 rounded-sm border border-signal-warn/40 bg-signal-warn/[0.06] p-2.5">
            <TriangleAlert
              size={13}
              strokeWidth={2}
              className="mt-0.5 shrink-0 text-signal-warn"
              aria-hidden="true"
            />
            <span className="text-2xs text-ink-muted">
              <T k="kernel.auto.prefix" fallback="当前是" />
              <strong className="font-medium text-ink"><T k="kernel.auto.strong" fallback="自动提交" /></strong>
              <T
                k="kernel.auto.mid"
                fallback="策略： 解题产出候选 flag 后会直接提交，不等确认。仍受每题错误提交次数上限约束，猜错会消耗不可再生的机会。可在"
              />
              <Link
                to="/settings#ctf"
                className="mx-1 text-accent-strong underline hover:text-accent"
              >
                <T k="kernel.auto.settings" fallback="设置" />
              </Link>
              <T k="kernel.auto.suffix" fallback="里改回「先确认再提交」。" />
            </span>
          </div>
        )}

        {/* ── 内容区（三态）── */}
        {status === 'idle' || status === 'loading' ? (
          <SkeletonRows rows={8} rowClassName="h-8" />
        ) : status === 'error' ? (
          <ErrorState
            title={t('kernel.loadFailed', undefined, 'CTF2 数据加载失败')}
            error={error}
            onRetry={() => void loadKernel()}
          />
        ) : filtered.length === 0 ? (
          <KernelEmptyState hasAny={challenges.length > 0} onSync={() => runSync()} busy={busy} />
        ) : (
          <>
            {/*
              看板表格，不是卡片网格 —— 见 `ChallengeBoard` 的说明：
              比赛时要扫的是「状态 + 分值」两列，而卡片一屏只放得下 6–9 道。
            */}
            <ChallengeBoard
              challenges={filtered}
              busy={busy}
              onSolve={runSolve}
              onSubmit={setPending}
              onCancel={setCanceling}
              onRelease={runRelease}
              onSetPriority={runSetPriority}
              onWriteup={setWriteupChallenge}
              onWorkOrder={setWorkOrderChallenge}
              onOpen={runOpen}
              onBatch={runBatch}
              queue={queue}
              solveDisabledReason={account ? null : t('kernel.unbound', undefined, '还没绑定 CTF2 账号')}
            />

            <p className="mt-panel text-2xs text-ink-faint">
              {/* ★ 三个筛选维度任一收窄都要报"筛选出 N 道" —— 只看 filter
                  （状态）会漏掉只按分类/来源筛的情形（此时 tag 上写 67、页脚却
                  写"共 423"），用户会以为筛选没生效。 */}
              {filter === 'all' && categoryFilter === 'all' && sourceFilter === 'all'
                ? t('kernel.footer.countAll', { n: challenges.length }, `共 ${challenges.length} 道题`)
                : t(
                    'kernel.footer.countFiltered',
                    { n: filtered.length, total: challenges.length },
                    `筛选出 ${filtered.length} 道，共 ${challenges.length} 道`,
                  )}
              {challenges.some((c) => c.status === 'candidate_ready') && (
                <span className="ml-2 text-signal-warn">
                  {t(
                    'kernel.footer.candidates',
                    { n: challenges.filter((c) => c.status === 'candidate_ready').length },
                    `有 ${challenges.filter((c) => c.status === 'candidate_ready').length} 道题在等你确认`,
                  )}
                </span>
              )}
            </p>
          </>
        )}

        {/*
          镜像选择：放在页面底部（而不是工具条）是刻意的。
          它**只影响下一次开始解题**，而且是"通常不用碰"的项 ——
          放进工具条会和常用控件抢注意力。
        */}
        <div className="mt-bay max-w-md">
          <SandboxProfileSelect
            value={
              profile ??
              (normalizeSandboxProfile(
                settings?.defaultSandboxProfile ?? 'suvex-sandbox',
              ) as KernelSandboxProfile)
            }
            onChange={(next) => setProfile(next)}
          />
        </div>
      </div>

      {/* ── 确认提交（两步）── */}
      <FlagConfirmDialog
        open={pending !== null}
        challenge={pending}
        mode={(settings?.submitMode ?? 'safe') as KernelSubmitMode}
        busy={busy}
        onConfirm={runSubmit}
        onCancel={() => setPending(null)}
      />

      {/*
        ── 题解弹窗（v0.36）──

        ★ **条件挂载**，而不是常驻 + `open` 开关 —— 与 ScansPage 完全一致：
          每次打开都是全新实例，内部三态天然是干净的初值，不必在 effect 里
          "重置"（重置一旦漏写，打开第二道题时会先闪一眼上一道的题解）。
      */}
      {writeupChallenge && (
        <WriteupDialog challenge={writeupChallenge} onClose={() => setWriteupChallenge(null)} />
      )}

      {/*
        处置工单（v0.41）。与题解同样**条件挂载** —— 理由相同。

        ⚠️ 入口的显示判据在卡片那侧（`canHaveWorkOrder`，看分类前缀），
           这里**不再判一次**。判两遍的后果是两处规则漂移，而症状是
           "按钮露出来了但弹窗不渲染"（点了没反应）。
      */}
      {workOrderChallenge && (
        <WorkOrderDialog
          challenge={workOrderChallenge}
          onClose={() => setWorkOrderChallenge(null)}
        />
      )}

      {/* ── 按链接拉取单题 ── */}
      {pulling && (
        <div
          onClick={() => !busy && setPulling(false)}
          className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/70 p-gutter backdrop-blur-sm"
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="pull-challenge-title"
            onClick={(e) => e.stopPropagation()}
            className="flex w-full max-w-lg animate-rise-in flex-col gap-panel rounded-md border border-line bg-surface-overlay p-panel shadow-overlay"
          >
            <header className="flex flex-col gap-1">
              <h2 id="pull-challenge-title" className="text-sm text-ink">
                <T k="kernel.pullDialog.title" fallback="按链接拉取一道题" />
              </h2>
              <p className="text-xs text-ink-muted">
                <T k="kernel.pullDialog.descPrefix" fallback="把平台上那道题的页面地址" />
                <strong className="text-ink"><T k="kernel.pullDialog.descStrong" fallback="整条" /></strong>
                <T
                  k="kernel.pullDialog.descSuffix"
                  fallback="粘进来即可 —— 服务端会自己从里面认出题目 id，并在已知的练习集里找出它属于哪一个。 只粘那段 UUID 也行。"
                />
              </p>
            </header>

            <label htmlFor="pull-challenge-input" className="flex flex-col gap-1">
              <span className="text-2xs font-medium text-ink">
                <T k="kernel.pullDialog.label" fallback="题目链接或 id" />
              </span>
              <input
                id="pull-challenge-input"
                autoFocus
                value={pullInput}
                disabled={busy}
                onChange={(e) => setPullInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && pullInput.trim() && !busy) void runPull()
                }}
                placeholder="https://ctf2.dasctf.com/…/challenges/xxxxxxxx-xxxx-…"
                className={cn(
                  'h-9 rounded-sm border border-line bg-surface-sunken px-2 text-xs text-ink',
                  'placeholder:text-ink-faint focus:outline-none focus:ring-2 focus:ring-accent/40',
                )}
              />
            </label>

            <p className="rounded-sm border border-dashed border-line-strong px-2.5 py-2 text-2xs text-ink-muted">
              <T
                k="kernel.pullDialog.why"
                fallback="为什么是一次一道：平台的开放接口没有「列出练习集题目」的接口， 所以没法批量拉；但「按 id 取单题详情」是可用的。重复拉同一道题只会更新它，不会重复插入。"
              />
            </p>

            <footer className="flex items-center justify-end gap-2">
              <Button
                variant="secondary"
                size="sm"
                disabled={busy}
                onClick={() => setPulling(false)}
              >
                <T k="kernel.pullDialog.cancel" fallback="取消" />
              </Button>
              <Button
                variant="primary"
                size="sm"
                disabled={busy || !pullInput.trim()}
                onClick={() => void runPull()}
              >
                <T k="kernel.pullDialog.submit" fallback="拉取" />
              </Button>
            </footer>
          </div>
        </div>
      )}

      {/* ── 解绑 / 换绑 ── */}
      <ConfirmDialog
        open={unbinding}
        title={t('kernel.unbind.title', undefined, '解绑这个 CTF2 账号？')}
        description={t(
          'kernel.unbind.desc',
          undefined,
          '解绑会清空服务端保存的 PAT（密文与掩码一起置空），之后可以绑定另一个账号。已经同步下来的赛事与题目会保留 —— 它们挂在这次绑定下面，不会因为换绑而消失。',
        )}
        confirmLabel={t('kernel.unbind.confirm', undefined, '解绑')}
        busy={busy}
        onConfirm={() => {
          if (!account) return
          setBusy(true)
          void unbindKernelAccount(account.id)
            .then(() => {
              setUnbinding(false)
              showToast({
                message: t('kernel.toast.unbound', undefined, '已解绑。现在可以绑定另一个账号了。'),
              })
            })
            .catch((err) =>
              showToast({
                message: t('kernel.toast.unbindFailed', { msg: errorMessage(err) }, `解绑失败：${errorMessage(err)}`),
                tone: 'risk',
              }),
            )
            .finally(() => setBusy(false))
        }}
        onCancel={() => setUnbinding(false)}
      />

      {/* ── 选赛事 → 同步赛事题目 ── */}
      <CompetitionPicker
        open={picking}
        busy={busy}
        onConfirm={(remoteIds) => {
          // 选完先关弹窗再同步：同步可能要十几秒（逐场是几十次请求），
          // 留着一个"正在同步"的弹窗会让人以为还要在里面等。
          setPicking(false)
          runSync(remoteIds)
        }}
        onCancel={() => setPicking(false)}
      />

      {/* ── 取消确认 ── */}
      <ConfirmDialog
        open={canceling !== null}
        title={t(
          'kernel.cancel.title',
          { title: canceling?.title ?? '' },
          `取消「${canceling?.title ?? ''}」的解题？`,
        )}
        description={t(
          'kernel.cancel.desc',
          undefined,
          '队列里还没开始的会真正取消；已经在跑的那一轮会跑完，但不会产出候选答案。已经消耗的平台环境不会因此释放。',
        )}
        confirmLabel={t('kernel.cancel.confirm', undefined, '取消解题')}
        busy={busy}
        onConfirm={runCancel}
        onCancel={() => setCanceling(null)}
      />
    </>
  )
}

/**
 * 未绑定账号时的引导。
 *
 * ## ★ 为什么要有 `type="password"`（而不是普通输入框）
 *
 * PAT 是**凭据**。普通输入框会把它渲染成明文，而用户在录屏、
 * 共享屏幕、或者只是被旁边的人瞄一眼时就泄漏了。
 * 与设置页的 `SecretField` 同一个理由，只是这里没有做
 * "按住查看"那套 —— 绑定是一次性动作，用户可以直接从剪贴板粘贴。
 *
 * ⚠️ 它**不进浏览器存储**（不写 localStorage、不写 URL query）。
 * 同源的任何脚本都能读 localStorage，而这是平台的凭据。
 */
function BindPrompt() {
  const { t } = useI18n()
  const [token, setToken] = useState('')
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = () => {
    if (!token.trim()) return
    setBusy(true)
    void bindKernelAccount(token.trim(), label.trim())
      .then(() => {
        // ★ 立刻清空本地状态：这个字符串没有理由在组件里多留一秒。
        setToken('')
        showToast({ message: t('kernel.toast.bindOk', undefined, '绑定成功。') })
      })
      .catch((err) =>
        showToast({
          message: t('kernel.toast.bindFailed', { msg: errorMessage(err) }, `绑定失败：${errorMessage(err)}`),
          tone: 'risk',
        }),
      )
      .finally(() => setBusy(false))
  }

  return (
    <div className="mx-auto flex max-w-lg flex-col gap-panel rounded-md border border-line bg-surface p-panel">
      <div className="flex flex-col gap-1">
        <h2 className="text-sm font-medium text-ink"><T k="kernel.bind.title" fallback="绑定 CTF2 账号" /></h2>
        <p className="text-xs text-ink-muted">
          <T
            k="kernel.bind.desc"
            fallback="需要一个 CTF2 的 Personal Access Token（PAT）。它会以**加密形式**存在服务端，界面任何位置都不会完整显示它。"
          />
        </p>
      </div>

      <div className="flex flex-col gap-1.5">
        <label htmlFor="ctf2-token" className="text-2xs font-medium text-ink">
          PAT
        </label>
        <input
          id="ctf2-token"
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submit()
          }}
          autoComplete="off"
          spellCheck={false}
          placeholder={t('kernel.bind.tokenPlaceholder', undefined, '粘贴平台的访问令牌')}
          className={cn(
            'mono-ui h-9 w-full rounded-sm border border-line bg-surface-sunken px-2.5 text-sm text-ink',
            'transition-ui duration-fast placeholder:text-ink-faint',
            'hover:border-line-strong focus:outline-none focus:ring-2 focus:ring-accent/40',
          )}
        />
      </div>

      <div className="flex flex-col gap-1.5">
        <label htmlFor="ctf2-label" className="text-2xs font-medium text-ink">
          <T k="kernel.bind.note" fallback="备注（可空）" />
        </label>
        <input
          id="ctf2-label"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder={t('kernel.bind.notePlaceholder', undefined, '例如「我的号」')}
          className={cn(
            'h-9 w-full rounded-sm border border-line bg-surface-sunken px-2.5 text-sm text-ink',
            'transition-ui duration-fast placeholder:text-ink-faint',
            'hover:border-line-strong focus:outline-none focus:ring-2 focus:ring-accent/40',
          )}
        />
      </div>

      <Button
        variant="primary"
        size="md"
        disabled={busy || !token.trim()}
        loading={busy}
        loadingLabel={t('kernel.bind.loadingLabel', undefined, '验证中…')}
        onClick={submit}
      >
        <T k="kernel.bind.submit" fallback="绑定并验证" />
      </Button>

      <p className="text-2xs text-ink-faint">
        <T
          k="kernel.bind.footer"
          fallback={'绑定时会真的调一次 CTF2 验证这个令牌 —— 验证不通过就不会保存，避免出现一个"显示已绑定但什么都做不了"的状态。'}
        />
      </p>
    </div>
  )
}

/**
 * 空状态。
 *
 * ★ 区分"一道题都没有"与"筛选后为空" —— 两者的下一步动作完全不同。
 *   混成一句"没有题目"会让改了筛选条件的用户以为题目被删了。
 *
 * ⚠️ 名字带 `Kernel` 前缀是刻意的：项目里已有一个
 * `components/ui/EmptyState`，同名会遮蔽它（而这一次只是恰好没用到它，
 * 下次有人在这页引它就会撞上一个"指向本地函数"的坑）。
 */
function KernelEmptyState({
  hasAny,
  onSync,
  busy,
}: {
  hasAny: boolean
  onSync: () => void
  busy: boolean
}) {
  if (hasAny) {
    return (
      <div className="flex flex-col items-center gap-tight rounded-md border border-line bg-surface py-bay text-center">
        <p className="text-sm text-ink-muted"><T k="kernel.empty.filtered" fallback="这个状态下没有题目" /></p>
        <p className="text-xs text-ink-faint"><T k="kernel.empty.filteredHint" fallback="换个筛选条件看看。" /></p>
      </div>
    )
  }
  return (
    <div className="flex flex-col items-center gap-tight rounded-md border border-line bg-surface py-bay text-center">
      <p className="text-sm text-ink-muted"><T k="kernel.empty.none" fallback="还没有从 CTF2 同步到题目" /></p>
      <p className="max-w-[46ch] text-xs text-ink-faint">
        <T
          k="kernel.empty.noneDesc"
          fallback="点「同步题目」把平台上的练习集与赛事拉下来。同步是一次外部请求， 会真的访问 CTF2 —— 所以它需要扫描级权限。"
        />
      </p>
      <Button variant="primary" size="md" disabled={busy} loading={busy} onClick={onSync}>
        {/* busy 时由 `loading` 自己渲染转圈，这里再画一个就有两个圈了 */}
        {!busy && <RefreshCw size={14} strokeWidth={2.5} aria-hidden="true" />}
        <T k="kernel.sync.all" fallback="同步题目" />
      </Button>
    </div>
  )
}

export default KernelPage
