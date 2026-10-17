import { cn } from '@/lib/cn'
import { PageHeader } from '@/components/layout/AppShell'
import ReportSheet from '@/components/reports/ReportSheet'
import ReportHistory from '@/components/reports/ReportHistory'
import { ContentOptions, ScopePicker, ScopePreview } from '@/components/reports/ReportConfigPanel'
import ErrorState from '@/components/ui/ErrorState'
import { Button } from '@/components/ui/Button'
import NoScansYet from '@/components/ui/NoScansYet'
import { Skeleton, SkeletonRows } from '@/components/ui/Skeleton'
import { loadFindings, useFindingsState } from '@/data/findings-store'
import { loadScans, useScansState } from '@/data/scans-store'
import {
  createReport,
  loadReportDocument,
  loadReportPreview,
  loadReports,
  refreshReportDocument,
  removeReport,
  useReportDocument,
  useReportPreview,
  useReportsState,
} from '@/data/reports-store'
import {
  DEFAULT_REPORT_OPTIONS,
  describeScope,
  type Report,
  type ReportFormat,
  type ReportOptions,
  type ReportScope,
  type ReportScopeKind,
} from '@/data/report'
import { applyOptions, findingsInScope, presetToRange, type RangePreset } from '@/lib/report-build'
import { generateReportArtifact } from '@/lib/report-api'
import { downloadTextFile, printReport } from '@/lib/report-export'
import { errorMessage } from '@/lib/api'
import { AlertCircle, Download, Eye, FileText, Printer } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

/**
 * Reports 报告页。
 *
 * 版面：配置区（左）→ 报告预览（右）→ 历史列表（下方）。
 *
 * ★ 为什么是"配置在左、预览在右"的常驻两栏，而不是"填完点生成再看结果"：
 *   报告的核心体验是**试**。用户会反复调范围与内容开关，
 *   每次都想立刻看到"这样配出来长什么样"。做成两步流程的话，
 *   每次调整都要走一次"返回 → 改 → 生成"，试错成本直接乘以步数。
 *
 *   预览常驻的代价是首屏要渲染整份报告。这里可以接受 ——
 *   报告页本来就是重内容页（已按路由分包），而且用户来这里就是要看它的。
 *
 * ★ 一致性（本轮验收标准）是这样保证的：
 *   预览、Markdown 导出、PDF 导出**共用同一个 ReportDocument**
 *   （见 lib/report-document.ts）。三者不是三份内容，
 *   而是同一份 IR 的三个消费者，所以不可能分叉。
 */

export function ReportsPage() {
  /*
    ★ 这一页**依赖另外两个领域的数据**（扫描 + 漏洞）：范围选择器的可选项
      是从它们推出来的。而 store 是模块级的、由各自的页面负责加载 ——
      用户如果直接打开 `/reports`（从书签、从刷新、从分享的链接），
      Scans 页和 Findings 页的 effect 都不会跑，这里就会看到
      「没有可用的扫描结果」—— 而那与"真的没有扫描"长得一模一样。

      所以这一页必须自己触发加载，并且**显示它们的三态**（见下面的左栏）。
  */
  const scansState = useScansState()
  const findingsState = useFindingsState()
  const scans = scansState.scans
  const findings = findingsState.findings

  const { reports, status, error: loadError } = useReportsState()

  // ── 视图模式 ──
  /**
   * 'compose' = 正在编排一份新报告；'preview' = 在看某份历史报告。
   *
   * 用两个模式而不是"当前文档"一个状态：历史报告是**只读**的，
   * 它没有范围选择器也没有内容开关（那些是生成时定死的）。
   * 混在一起会出现"这份是历史报告，但界面还让我改选项"的错乱。
   */
  const [mode, setMode] = useState<'compose' | 'history'>('compose')

  // ── 编排状态 ──
  /**
   * ★ 从 URL 读"手选的漏洞"（任务 10 P5）。
   *
   * 漏洞页的「生成报告」把选中项的 id 拼成 `?findings=f1,f2` 跳过来，
   * 这里把它们接住并作为初始 scope —— 用户不必在报告页重新勾一遍。
   *
   * 用**惰性初始化**（`useState(() => ...)`）而不是在 effect 里 setState：
   * 后者会先渲染一帧默认 scope（scan 分支）再跳到手选分支，
   * 而那一帧会触发一次针对错误 scope 的预览请求。
   */
  const [searchParams] = useSearchParams()
  const [scope, setScope] = useState<ReportScope>(() => {
    const picked = searchParams.get('findings')
    if (picked) {
      const ids = picked
        .split(',')
        .map((id) => id.trim())
        .filter(Boolean)
      // 空串要排除：`?findings=` 会 split 出一个空项，
      // 而带着 `['']` 的 scope 会让 union 校验认为"有成员"，
      // 于是发出一份必然查不到任何东西的请求。
      if (ids.length > 0) return { kind: 'findings', findingIds: ids }
    }
    return {
      kind: 'scan',
      // 默认选最近一次**有结果的**扫描，而不是列表第一条。
      // 第一条可能是正在跑的（结果不全）或失败的（没有结果），
      // 用户一进来就看到空报告会以为功能坏了。
      scanId: '',
    }
  })
  const [preset, setPreset] = useState<RangePreset>('30d')
  const [options, setOptions] = useState<ReportOptions>(DEFAULT_REPORT_OPTIONS)
  const [title, setTitle] = useState('')
  const [format, setFormat] = useState<ReportFormat>('pdf')

  // ── 运行的异步动作 ──
  const [busy, setBusy] = useState<'generate' | 'download' | null>(null)
  const [downloadingId, setDownloadingId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // ── 历史预览 ──
  const [openedId, setOpenedId] = useState<string | null>(null)

  /**
   * 「等预览就绪后打印」的挂起标志。
   *
   * ★ 存在的理由是一处真实的时序坑：选了 PDF 格式时，导出动作是
   *   **打印屏幕上那份 DOM**（见 lib/report-api.ts 的说明）。而任务 7 之后
   *   历史预览的内容来自 `GET /reports/{id}/document` —— 一次网络往返。
   *
   *   早先的写法是切换预览后 `setTimeout(printReport, 60)`。60ms 在本机
   *   常常不够，于是打印出来的可能是**骨架屏**，或者上一份报告的残留 ——
   *   而用户拿到的 PDF 会是一张灰条。这种"内容还没到就交付"的错，
   *   在纸张上无法撤回。
   *
   *   所以改成由 effect 盯着文档状态：只有 `ready` 才打印。
   */
  const [pendingPrint, setPendingPrint] = useState(false)

  const documentState = useReportDocument(openedId)
  /** 草稿预览（后端构建）—— 见 reports-store 里那一大段说明 */
  const preview = useReportPreview()

  // 历史列表、以及范围选择器依赖的扫描与漏洞：进页面各拉一次
  useEffect(() => {
    void loadReports()
    void loadScans()
    void loadFindings()
  }, [])

  // 打开某一份历史报告时才取它的文档（按需加载，见 reports-store 的文件头）
  useEffect(() => {
    if (openedId) void loadReportDocument(openedId)
  }, [openedId])

  /*
    挂起的打印：等预览真正就绪（或明确失败）再动手。

    ⚠️ `setPendingPrint` 一律放在 rAF / 定时器回调里，不在 effect body 里同步调用 ——
       同步在 effect 里 setState 会触发 eslint 的 `set-state-in-effect`
       （它禁止的是"渲染后又立刻触发一次渲染"这种级联）。
  */
  useEffect(() => {
    if (!pendingPrint) return

    if (documentState.status === 'error') {
      const timer = window.setTimeout(() => {
        setPendingPrint(false)
        setError('报告已生成，但预览内容没能加载出来，请在历史列表里重新下载。')
      }, 0)
      return () => window.clearTimeout(timer)
    }

    if (documentState.status !== 'ready' || !documentState.doc) return

    /*
      ⚠️ 这里用 `setTimeout(0)` 而不是 `requestAnimationFrame`。

      rAF 的直觉是"等下一帧"，但它在**后台标签页里根本不触发** ——
      用户切到别的标签页、或把窗口最小化时，这个回调就永远不来，
      而 `window.print()` 也就永远不会被调用。症状是"点了生成 PDF 没反应"。

      effect 本身已经在 React 提交**之后**执行，DOM 已经在了；
      而 `window.print()` 会自己强制一次布局重排，不需要我们等一帧。
      所以这里要的只是"退出当前调用栈"，宏任务就够了。
    */
    const timer = window.setTimeout(() => {
      setPendingPrint(false)
      printReport()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [pendingPrint, documentState])

  /**
   * 本次会话的参考时刻。
   *
   * ★ 为什么不能直接在渲染期取 `Date.now()`：
   *   渲染必须是纯的。报告编号（RPT-20260214-XXXX）由 `now` 派生，
   *   而 `now` 来自 Date.now() —— 每次重渲染都变，于是**预览上的编号
   *   会跟着用户每一次点击跳动**。这正好是 report.ts 里
   *   "不能用随机数，否则预览和下载里的编号不一样"所警告的那类不一致，
   *   只是换了个来源：不是随机，而是"永远在变的现在"。
   *
   *   固定在挂载时刻之后，编号在整个会话里稳定，用户能把它抄下来
   *   并与下载到的文件对上；而**真正生成**时仍取当时的真实时刻
   *   （见 handleGenerate）—— 那才是这份报告诞生的一刻。
   *
   * 用惰性初始化的 useState 而不是 useRef + 赋值：前者只在首次渲染求值一次，
   * 语义就是"一个只算一次的稳定值"，且对 lint 的纯度规则是干净的。
   */
  const [referenceNow] = useState(() => Date.now())

  // ─────────────────────────────────────────────
  // 可选范围（从真实数据推，不硬编码）
  // ─────────────────────────────────────────────

  /**
   * 范围选择器依赖的**两个**数据源的状态合并为一个。
   *
   * 合并的理由：对用户来说它们是同一件事（"我那些数据到了没"）。
   * 分开显示会出现"扫描好了但漏洞还在转"这种中间态，而范围选择器
   * 两边都读 —— 缺任何一边，"命中几条"就是错的。
   *
   * 错误优先于加载：一边失败、一边还在转时，应该立刻告诉他失败的那一边，
   * 而不是让他继续等一个注定不完整的加载。
   */
  const scopeDataStatus =
    scansState.status === 'error' || findingsState.status === 'error'
      ? 'error'
      : scansState.status === 'loading' || findingsState.status === 'loading'
        ? 'loading'
        : 'ready'
  const scopeDataError = scansState.error ?? findingsState.error

  /**
   * 可选扫描：只列**已经跑完**的扫描。
   *
   * 为什么排除 running：对一次正在跑的扫描生成"完整报告"是语义错误的 ——
   * 报告是一份结论，而结论必须建立在跑完的数据上。列出来只会让用户
   * 生成一份内容随时会变的报告（一次跑到 62%、已经有 5 条发现的扫描
   * 看着"有内容"，但它还会继续变，而报告一旦交付就不再更新）。
   *
   * 为什么排除 failed：那一次没有产出任何结果，选它只会得到空报告。
   *
   * 为什么**保留** aborted：被用户终止的扫描已经跑出了一部分结果，
   * 那部分发现是真实的、经过验证的，而且"跑了一半"这件事本身
   * 就应该出现在报告的附录 A 里（那里会标注扫描未完成）。
   * 把它一并排除，等于把已经拿到的证据丢掉 —— 漏报比多报更糟。
   */
  const scanOptions = useMemo(() => {
    return (
      scans
        // ★ 先按状态过滤，再按"有没有发现"过滤。两步的顺序不能反：
        //   运行中的扫描往往**已经**发现了不少东西，所以只按 count > 0
        //   是筛不掉它的 —— 注释与实现曾经在这里不一致。
        .filter((scan) => scan.status === 'completed' || scan.status === 'aborted')
        .map((scan) => ({
          id: scan.id,
          name: scan.name,
          target: scan.target,
          count: findings.filter((f) => f.scanId === scan.id).length,
        }))
        .filter((scan) => scan.count > 0)
    )
  }, [scans, findings])

  const targetOptions = useMemo(() => {
    const counts = new Map<string, number>()
    for (const finding of findings) {
      counts.set(finding.target, (counts.get(finding.target) ?? 0) + 1)
    }
    return [...counts.entries()]
      .map(([name, count]) => ({ name, count }))
      .sort((a, b) => a.name.localeCompare(b.name))
  }, [findings])

  /**
   * 首次渲染时把默认范围落到真实的第一个可选项上。
   *
   * 用"派生 + 兜底"而不是 useEffect 去 setState：
   * effect 会在挂载后多渲染一次，而且当数据异步到达时（真实后端）
   * 还要处理"用户已经改过选择"的情况。派生则永远是最新的，
   * 且没有中间的空状态。
   */
  const effectiveScope: ReportScope = useMemo(() => {
    if (scope.kind === 'scan') {
      const valid = scope.scanId && scanOptions.some((s) => s.id === scope.scanId)
      if (valid) return scope
      return { kind: 'scan', scanId: scanOptions[0]?.id ?? '' }
    }
    if (scope.kind === 'target') {
      const valid = scope.target && targetOptions.some((t) => t.name === scope.target)
      if (valid) return scope
      return { kind: 'target', target: targetOptions[0]?.name ?? '' }
    }
    // findings：手选的那一批（任务 10 P5）
    if (scope.kind === 'findings') {
      /*
        ★ 保留用户的勾选，但**滤掉已经不在列表里的 id**。

        为什么需要滤：从漏洞页跳过来之后，用户可能删掉了某条、
        或者那次扫描的记录被清掉了。留着失效 id 会让报告静默地
        少几条（后端按 `in_` 查，查不到的就不出现），而用户看到的
        是"我明明选了 5 条，报告里只有 3 条" —— 却没有任何提示。

        滤空之后 `scopeIsEmpty` 会接管（它已经处理了"范围为空"），
        于是界面显示的是"请先选要包含的漏洞"，而不是一份空报告。
      */
      const alive = scope.findingIds.filter((id) => findings.some((f) => f.id === id))
      return alive.length === scope.findingIds.length
        ? scope
        : { kind: 'findings', findingIds: alive }
    }
    // range：空日期时用预设算一个真实区间
    if (!scope.from || !scope.to) {
      const range = presetToRange(preset, findings, referenceNow)
      return { kind: 'range', ...range }
    }
    return scope
  }, [scope, scanOptions, targetOptions, preset, findings, referenceNow])

  /** 范围类型切换 —— 默认值的决策在这里，因为这里拿得到数据 */
  const handleKindChange = useCallback(
    (kind: ReportScopeKind) => {
      switch (kind) {
        case 'scan':
          setScope({ kind: 'scan', scanId: scanOptions[0]?.id ?? '' })
          break
        case 'target':
          setScope({ kind: 'target', target: targetOptions[0]?.name ?? '' })
          break
        case 'range':
          // 立刻给出一个真实区间，不让用户先看到"命中 0 条"
          setScope({ kind: 'range', ...presetToRange(preset, findings, referenceNow) })
          break
      }
    },
    [scanOptions, targetOptions, preset, findings, referenceNow],
  )

  const handlePresetChange = useCallback(
    (next: RangePreset) => {
      setPreset(next)
      setScope({ kind: 'range', ...presetToRange(next, findings, referenceNow) })
    },
    [findings, referenceNow],
  )

  // ─────────────────────────────────────────────
  // 命中与预览
  // ─────────────────────────────────────────────

  /** 范围内的发现（未应用内容选项）—— 用于配置区的实时反馈 */
  const inScope = useMemo(
    () => findingsInScope(findings, effectiveScope),
    [findings, effectiveScope],
  )

  /** 应用选项后的条数 */
  const selectedCount = useMemo(() => applyOptions(inScope, options).length, [inScope, options])

  /**
   * 本范围内能提供什么 —— 决定配置区哪些开关禁用。
   *
   * ★ 四项来自**两个**源，这是任务 7 之后才如此的：
   *
   *   · `unconfirmed` 从**摘要**算 —— 摘要里有 `status` 原值。
   *   · 其余三项从**后端探测**算（`preview.available`）—— 它们取决于
   *     `detail` 里有没有 PoC / 推理 / 修复建议，而摘要**不含 detail**
   *     （契约 §3.4：列表可能几百条，带上完整证据文本会让响应到几 MB）。
   *
   *   ⚠️ 早先四项都从 `availableContent(inScope)` 算。任务 7 之后
   *      `inScope` 是摘要，`detail` 是空壳，于是每一项都算成 `false` ——
   *      界面显示"本范围内没有可用的修复建议"，**而实际每一条都有**。
   *      一个说反了的提示比没有提示更糟：它会让用户不去勾那个开关。
   */
  const available = useMemo(
    () => ({
      poc: preview.available?.poc ?? false,
      reasoning: preview.available?.reasoning ?? false,
      remediation: preview.available?.remediation ?? false,
      unconfirmed: inScope.some((f) => f.status === 'new' || f.status === 'false_positive'),
    }),
    [preview.available, inScope],
  )

  /**
   * 当前范围是否**还是空的**（一个可选扫描/目标都没有）。
   *
   * ★ 这时候绝不能发预览请求：把一个 `scanId: ''` 送上去，后端会老老实实
   *   按空范围算出一份「本次范围内未发现安全问题」的报告，然后它就一直
   *   显示在右侧 —— 而那是一份**看起来像结论**的东西，用户只是刚进页面。
   *   "还没有数据"和"扫过了、没问题"在屏幕上必须长得不一样。
   *
   *   ⚠️ 这条是被实测抓到的：空库走查时 Reports 页在什么都没配的情况下
   *      渲染出了一整份「未发现问题」的报告，连"覆盖范围 扫描任务「」"
   *      这种半截文案都显示出来了。
   */
  const scopeIsEmpty =
    (effectiveScope.kind === 'scan' && !effectiveScope.scanId) ||
    (effectiveScope.kind === 'target' && !effectiveScope.target)

  /**
   * 整个系统里有没有**可出报告**的数据。
   *
   * 与 `scopeIsEmpty` 不是一回事：范围为空可能只是筛选问题，
   * 而这里说的是"还没有任何跑出结果的扫描" —— 那需要的是行动引导，
   * 不是"换一个数据范围"这种提示（新用户没有别的范围可换）。
   */
  const hasReportableData = scanOptions.length > 0 || targetOptions.length > 0

  /**
   * 当前要渲染的草稿。
   *
   * ★ 范围为空时**一律不渲染**，包括 store 里可能残留的上一次结果。
   *   在渲染期判断而不是在 effect 里清空：后者会多一次渲染，
   *   而这件事本来就是纯派生的。
   */
  const draftDoc = scopeIsEmpty ? null : preview.doc

  /**
   * 草稿预览的请求参数。
   *
   * ★ 打包成一个对象再用作 effect 依赖，而不是把 scope/options/title/scans
   *   逐个列进去：`defaultTitle` 要读 scope 与 scans，逐个列会让 effect 的
   *   依赖列表与闭包里实际用到的变量对不上（那是 exhaustive-deps 最容易
   *   漏报的一类）。打包之后"用到什么 == 依赖什么"。
   *
   * ⚠️ 这里**没有** `now` —— 预览的编号由服务端留空（`meta.provisional`），
   *    报告编号在生成那一刻才分配。前端不再自己算时间、算编号。
   */
  const previewInput = useMemo(
    () => ({
      scope: effectiveScope,
      options,
      title: title.trim() || defaultTitle(effectiveScope, scans),
    }),
    [effectiveScope, options, title, scans],
  )

  /*
    拉草稿预览 —— `POST /reports/preview`。

    ★ debounce 350ms：用户拖动日期、连点开关时会连续改变依赖，
      不去抖就是一串请求 + 一串竞态。store 内部还有 epoch 丢弃过期响应，
      两道保险都要有 —— 只有 debounce 挡不住"最后一次请求先返回"。

    ⚠️ 数据没到时**不要**发请求：范围还是空的（`scanId: ''`），
      后端会按空范围算出一份"未发现问题"的报告，然后它会在数据到达前
      一直显示在右侧 —— 那正是"空报告是一份结论，不是一种加载状态"的坑。
  */
  useEffect(() => {
    if (scopeDataStatus !== 'ready') return
    if (scopeIsEmpty) return
    const timer = window.setTimeout(() => {
      void loadReportPreview(previewInput)
    }, 350)
    return () => window.clearTimeout(timer)
  }, [previewInput, scopeDataStatus, scopeIsEmpty])

  /*
    历史报告的文档不再在前端重放 —— 走 `GET /reports/{id}/document`
    （见上面 `documentState` 与 reports-store 的说明）。

    前端那份 `rebuildReportDocument` 仍留在 lib 里：
    它与后端 `service.document_for` 是同一件事的两种实现，
    而"两者产出必须一致"正是对账脚本要验的东西（契约 §3.5.1）。
  */

  // ─────────────────────────────────────────────
  // 动作
  // ─────────────────────────────────────────────

  /**
   * 提示条的定时器 id。
   *
   * ★ 必须存下来 —— 原来是在 `showNotice` 里直接
   *   `window.setTimeout(() => setNotice(null), 3200)`，id 一丢就有两个后果：
   *
   *   · **后一条提示会被前一条的 timer 提前清掉**。用户点「生成报告」看到
   *     "已生成并下载 …"，紧接着去历史列表点某条的「下载」，第二条提示可能
   *     只显示几百毫秒就没了 —— 而"点了没反应"正是本页最忌讳的失败形态
   *     （见 `handleGenerate` 里那句"点了没反应是最难排查的一类问题"）；
   *   · 组件卸载后那个 timer 照旧触发 `setNotice`。React 19 不再对卸载后
   *     setState 报警，所以它是**静默**的浪费 —— 不会有人发现它。
   *
   * ⚠️ 这里用 ref 记 id，而不是 `Ctf2.tsx` 那种"`notice` 一变就重置计时"的
   *    effect：那个写法在**两条提示文字完全一样**时不会重跑（`notice` 的引用
   *    没变），于是第二次提示仍按第一次的剩余时间提前消失。而本页连续两次
   *    "已打开打印窗口 —— …"是完全可能的（先生成一份 PDF、再下载一份历史）。
   *   存 id 则每一次提示都真的重新计时。
   */
  const noticeTimerRef = useRef<number | null>(null)

  const showNotice = useCallback((message: string) => {
    setNotice(message)
    // 新提示到来时先取消上一条的定时器，否则它会提前把这一条清掉
    if (noticeTimerRef.current !== null) window.clearTimeout(noticeTimerRef.current)
    noticeTimerRef.current = window.setTimeout(() => {
      // 清掉时把 id 也归零，避免清理函数去 clear 一个已经触发过的 id
      noticeTimerRef.current = null
      setNotice(null)
    }, 3200)
  }, [])

  /**
   * 卸载时清掉还挂着的 timer。
   *
   * 空依赖是**刻意**的：这个 effect 只负责"离开时别留下未触发的定时器"，
   * 而 `noticeTimerRef` 是稳定引用，清理函数里读到的永远是最新那个 id。
   */
  useEffect(
    () => () => {
      if (noticeTimerRef.current !== null) {
        window.clearTimeout(noticeTimerRef.current)
        noticeTimerRef.current = null
      }
    },
    [],
  )

  /**
   * 生成报告。
   *
   * 生成 = ① 冻结当时的条目集合与选项 → ② 落进历史 → ③ 按所选格式导出。
   *
   * ★ 顺序很重要：**先落库再导出**。
   *   导出可能被用户取消（打印对话框），但那不影响"这份报告已经生成过"这个事实 ——
   *   历史列表里应当有它，用户可以随时回来重新下载。
   *   反过来（先导出再落库）会让取消打印等于什么都没发生，
   *   用户会以为"没生成成功"。
   */
  const handleGenerate = useCallback(async () => {
    if (selectedCount === 0) {
      setError('当前范围与选项下没有可导出的条目，请调整后再生成。')
      return
    }

    setError(null)
    setBusy('generate')

    try {
      const reportTitle = title.trim() || defaultTitle(effectiveScope, scans)

      /*
        ★ 顺序是**先落库、再据它导出**，不能反过来。

        为什么：报告编号由服务端在落库那一刻分配（由 `created_at` 派生）。
        早先前端自己构建文档、再让 store 另分配一个编号，
        实测到的现象正是"预览显示 RPT-…-3253、下载得到 RPT-…-3310"。
        现在导出用到的 id 与编号**完全来自那条已落库的记录**，因此
        "历史列表里的编号 = 预览里的编号 = 文件名 = 正文里的编号"
        是同一个冻结对象的四次引用，不存在漂移的可能。

        也不再由前端算 findingIds 传上去：服务端在**同一个事务里**
        按 scope 查库、应用选项、冻结收录集合与排除数。
        前端算出来的集合与服务端查到的不可能总是一致（并发扫描会在这两次读
        之间写入新发现），而报告冻结的必须是服务端**实际收录**的那一批 ——
        否则历史列表的条数与报告正文对不上。
      */
      const saved = await createReport({
        title: reportTitle,
        scope: effectiveScope,
        options,
        format,
      })

      // ── 导出：内容由后端按那条记录的冻结快照渲染 ──
      const artifact = await generateReportArtifact({
        reportId: saved.id,
        code: saved.code,
        format,
      })

      // 生成后切到历史视图：用户刚产出一份东西，最可能想看它在列表里的样子。
      // 必须**在交付之前**切 —— PDF 分支打印的是屏幕上那份 DOM。
      setOpenedId(saved.id)
      setMode('history')

      if (artifact.kind === 'file') {
        downloadTextFile(artifact.filename, artifact.content, artifact.mime)
        showNotice(`已生成并下载 ${artifact.filename}`)
      } else {
        /*
          打印分支。⚠️ 两件事必须做对：

          1. **不能声称"PDF 已生成"** —— 浏览器无法告知用户是打印了还是取消了。
             提示语给的是**下一步该做什么**（在对话框里选另存为 PDF），
             这才是用户此刻需要的信息。
          2. **不能立刻 printReport()** —— 上面刚切换的预览还在从
             `GET /reports/{id}/document` 拉内容，立刻打印会印出一张骨架屏。
             交给 pendingPrint，由它盯着文档状态就绪后再动手。
        */
        setPendingPrint(true)
        showNotice('已打开打印窗口 —— 在目标处选择「另存为 PDF」即可导出。')
      }
    } catch (err) {
      // 真实接口会超时、会被限流。错误必须落到界面上 ——
      // "点了没反应"是最难排查的一类问题。
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }, [selectedCount, title, effectiveScope, scans, options, format, showNotice])

  /**
   * 重新下载历史报告。
   *
   * ★ 用报告**当时**的格式与冻结内容，而不是界面上当前的选择。
   *   列表里点"下载"却得到另一种格式，会让人以为下载错了东西。
   */
  const handleDownload = useCallback(
    async (report: Report) => {
      setDownloadingId(report.id)
      setError(null)
      try {
        const artifact = await generateReportArtifact({
          reportId: report.id,
          code: report.code,
          /*
            用报告**当时**的格式，不是界面上当前的选择。
            列表里点"下载"却得到另一种格式，会让人以为下载错了东西。
          */
          format: report.format,
        })

        if (artifact.kind === 'file') {
          downloadTextFile(artifact.filename, artifact.content, artifact.mime)
          showNotice(`已下载 ${artifact.filename}`)
        } else {
          // 打印前先把预览切到这份报告 —— 否则打印出来的是当前编排的那份。
          // 真正的打印交给 pendingPrint：等这份报告的文档加载完再动手。
          setOpenedId(report.id)
          setMode('history')
          setPendingPrint(true)
          showNotice('已打开打印窗口 —— 在目标处选择「另存为 PDF」即可导出。')
        }
      } catch (err) {
        setError(errorMessage(err))
      } finally {
        setDownloadingId(null)
      }
    },
    [showNotice],
  )

  /**
   * 删除一份历史报告。
   *
   * ★ 现在真的要等后端应答。删不掉时**必须说出来** ——
   *   删除是不可逆的操作，用户按下它之后会默认"没了"。
   *   静默失败会让他在下一次打开历史时发现那份报告还在，
   *   而那时他已经不记得自己删过。
   */
  const handleDelete = useCallback(
    async (report: Report) => {
      setError(null)
      try {
        await removeReport(report.id)
        // 删掉的正是当前预览的那份时，退出历史预览 ——
        // 否则会停留在一份已经不存在的报告上
        setOpenedId((current) => (current === report.id ? null : current))
        showNotice(`已删除「${report.title}」`)
      } catch (err) {
        setError(errorMessage(err))
      }
    },
    [showNotice],
  )

  // ─────────────────────────────────────────────
  // 渲染
  // ─────────────────────────────────────────────

  const activeReport = openedId ? (reports.find((r) => r.id === openedId) ?? null) : null

  return (
    <>
      <PageHeader navId="reports" />

      <div className="px-gutter py-bay">
        {/* ── 视图切换 ──
            两个视图是"编排"和"历史"两种任务，用 tab 明示当前在哪一侧。
            不做成"历史在下方另一个区块"：那样用户在编排时
            会被下方的历史干扰（这块页面本来就重）。 */}
        <div role="tablist" aria-label="报告视图" className="mb-panel flex gap-1">
          <ViewTab
            active={mode === 'compose'}
            onClick={() => setMode('compose')}
            icon={<FileText size={13} strokeWidth={1.75} aria-hidden="true" />}
            label="生成报告"
          />
          <ViewTab
            active={mode === 'history'}
            onClick={() => setMode('history')}
            icon={<Eye size={13} strokeWidth={1.75} aria-hidden="true" />}
            label={`历史报告${reports.length > 0 ? `（${reports.length}）` : ''}`}
          />
        </div>

        {/* ── 反馈 ── */}
        {error && (
          <div
            role="alert"
            className="mb-panel flex items-start gap-tight rounded-sm border border-signal-risk/40 bg-signal-risk/[0.06] px-panel py-2.5 text-sm text-ink"
          >
            <AlertCircle
              size={14}
              strokeWidth={2}
              className="mt-0.5 shrink-0 text-signal-risk"
              aria-hidden="true"
            />
            {error}
          </div>
        )}
        {notice && (
          <div
            role="status"
            className="mb-panel flex items-start gap-tight rounded-sm border border-accent/40 bg-accent-wash/60 px-panel py-2.5 text-sm text-ink"
          >
            <Printer
              size={14}
              strokeWidth={2}
              className="mt-0.5 shrink-0 text-accent-strong"
              aria-hidden="true"
            />
            {notice}
          </div>
        )}

        <div className="grid gap-bay lg:grid-cols-[minmax(17rem,22rem)_minmax(0,1fr)]">
          {/* ══ 左栏：配置 或 历史详情信息 ══ */}
          <div className="flex min-w-0 flex-col gap-bay">
            {mode === 'compose' ? (
              /*
                ★ 配置区的三态（任务 7 要求）。

                范围选择器的可选项是从扫描与漏洞推出来的，所以它**也是**
                一个数据展示区域 —— 数据没到时它必须显示"在加载"，
                而不是一排"没有可用的扫描结果"。后者是**结论**，
                而用户会拿它做判断（"我明明跑过扫描啊"）。
              */
              scopeDataStatus === 'error' ? (
                <div className="rounded-md border border-line bg-surface p-panel">
                  <ErrorState
                    title="数据范围加载失败"
                    error={scopeDataError}
                    onRetry={() => {
                      void loadScans()
                      void loadFindings()
                    }}
                  />
                </div>
              ) : scopeDataStatus === 'loading' ? (
                <div className="rounded-md border border-line bg-surface p-panel">
                  <SkeletonRows rows={3} rowClassName="h-20" />
                </div>
              ) : (
              <div className="flex flex-col gap-panel rounded-md border border-line bg-surface p-panel">
                <ScopePicker
                  scope={effectiveScope}
                  onChange={setScope}
                  onKindChange={handleKindChange}
                  scans={scanOptions}
                  targets={targetOptions}
                  preset={preset}
                  onPresetChange={handlePresetChange}
                />

                <div className="h-px w-full bg-line" />

                <ContentOptions
                  options={options}
                  onChange={setOptions}
                  available={available}
                  hitCount={inScope.length}
                />

                <div className="h-px w-full bg-line" />

                {/* 报告标题可改，默认按范围给一个 —— 空着不写会让历史列表出现无名条目 */}
                <div className="flex flex-col gap-1.5">
                  <label htmlFor="report-title" className="text-2xs text-ink-faint">
                    报告标题
                  </label>
                  <input
                    id="report-title"
                    type="text"
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                    placeholder={defaultTitle(effectiveScope, scans)}
                    className="h-8 w-full rounded-sm border border-line bg-surface-sunken px-2 text-xs text-ink transition-ui duration-fast placeholder:text-ink-faint hover:border-line-strong focus:outline-none focus:ring-2 focus:ring-accent/40"
                  />
                </div>

                <div className="flex flex-col gap-1.5">
                  <span className="text-2xs text-ink-faint">导出格式</span>
                  <div role="radiogroup" aria-label="导出格式" className="flex gap-1">
                    {(['pdf', 'markdown'] as ReportFormat[]).map((key) => {
                      const checked = format === key
                      return (
                        <Button
                          key={key}
                          variant="secondary"
                          size="sm"
                          role="radio"
                          aria-checked={checked}
                          onClick={() => setFormat(key)}
                          /*
                            `role="radio"` 的选中态不是按钮的禁用/启用二态，所以
                            基元不给它：`secondary` 的边框与文字色在这里被
                            tailwind-merge 覆盖成「选中泛紫」，未选中沿用基元的
                            灰底描边。`flex-1` 让两项平分列宽。
                          */
                          className={cn(
                            'flex-1',
                            checked && 'border-accent/50 bg-accent-wash text-ink',
                          )}
                        >
                          {key === 'pdf' ? 'PDF' : 'Markdown'}
                        </Button>
                      )
                    })}
                  </div>
                </div>

                <ScopePreview
                  hitCount={inScope.length}
                  severityCounts={countSeverities(applyOptions(inScope, options))}
                  excludedCount={inScope.length - selectedCount}
                />

                {/*
                  ★ 用 `loading` 而不是手写文案分支。

                  这样一次拿到三样东西：转圈图标、`aria-busy`、以及那个 `pendingRef`
                  同步守卫（挡住"提交生效之前"的第二次点击）。手写
                  `{busy === 'generate' ? '正在生成…' : …}` 只有文字，后两样都拿不到。
                */}
                <Button
                  variant="primary"
                  size="md"
                  onClick={handleGenerate}
                  disabled={selectedCount === 0 || busy === 'download'}
                  loading={busy === 'generate'}
                  loadingLabel="正在生成…"
                >
                  <Download size={14} strokeWidth={2} aria-hidden="true" />
                  生成并导出
                </Button>

                {format === 'pdf' && (
                  <p className="text-2xs text-ink-faint">
                    将调用浏览器打印，在目标处选择「另存为 PDF」。生成的版本与右侧预览一致。
                  </p>
                )}
              </div>
              )
            ) : (
              /* 历史模式下左栏显示这份报告的生成口径 —— 只读 */
              activeReport && (
                <ReportMetaCard
                  report={activeReport}
                  scans={scans}
                  onDownload={handleDownload}
                  downloadingId={downloadingId}
                />
              )
            )}
          </div>

          {/* ══ 右栏：报告预览 ══ */}
          <div className="min-w-0">
            {mode === 'history' ? (
              /*
                ⚠️ 「还没选中任何一份」必须先判 —— 它不是加载态。
                   `documentState.status` 在没打开任何报告时是 `idle`，
                   fallthrough 到骨架分支会让右栏一直显示"正在加载报告内容"，
                   **而它永远不会加载完**（没有 id 可加载）。
                   用户看到的是一个卡住的加载动画，而不是"请从下面选一份"。
              */
              openedId === null ? (
                <EmptyPreview mode="history" />
              ) : (
                <>
                  <PreviewCaption mode="history" />

                  {/*
                    已选中之后的三态（任务 7 要求）。

                    ★ 内容来自 `GET /reports/{id}/document` —— 与"下载到的
                      Markdown"是同一个服务端渲染器的产物，所以两者不可能分叉。
                      前端本地重放（`rebuildReportDocument`）读的是**前端 store
                      里那一次快照**的 findings，可能已经落后于服务端。

                    ⚠️ 加载态**不能**退化成一个空白的纸面：那看起来像
                      "这份报告没有内容"，而真相是内容还在路上 ——
                      报告页最不能出的错就是让人以为结论是空的。
                  */}
                  {documentState.status === 'error' ? (
                    <ErrorState
                      title="报告内容加载失败"
                      error={documentState.error}
                      onRetry={() => {
                        if (openedId) void refreshReportDocument(openedId)
                      }}
                    />
                  ) : documentState.status === 'ready' && documentState.doc ? (
                    <PaperSheet>
                      <ReportSheet
                        doc={documentState.doc}
                        format={activeReport?.format ?? 'pdf'}
                      />
                    </PaperSheet>
                  ) : (
                    <ReportSheetSkeleton />
                  )}
                </>
              )
            ) : preview.status === 'error' ? (
              <div className="rounded-md border border-line bg-surface p-panel">
                <ErrorState
                  title="报告预览生成失败"
                  error={preview.error}
                  onRetry={() => void loadReportPreview(previewInput)}
                />
              </div>
            ) : draftDoc ? (
              <>
                <PreviewCaption mode="compose" />
                {/*
                  ★ 刷新期间**保留旧文档**，只在角上给一个小标记。
                    每调一次开关就把右侧打回骨架屏，会让整页闪个不停 ——
                    而用户此刻正盯着报告看自己那一改的效果，
                    闪烁恰恰破坏了他要对比的东西。
                */}
                <div className="relative">
                  {preview.status === 'loading' && (
                    <span
                      role="status"
                      className="absolute right-2 top-2 z-10 rounded-pill border border-line bg-surface-overlay/90 px-2 py-0.5 text-2xs text-ink-muted"
                    >
                      正在更新…
                    </span>
                  )}
                  <PaperSheet>
                    <ReportSheet doc={draftDoc} format={format} />
                  </PaperSheet>
                </div>
              </>
            ) : !hasReportableData ? (
              /*
                一次都没跑出结果 —— 这是**新用户的路径**（空库走查抓到的）。
                `EmptyPreview` 那句"换一个数据范围，或减少筛选条件"对他没有意义：
                他没有别的范围可换。这里要给的是下一步。
              */
              <NoScansYet
                bordered={false}
                title="还没有可出报告的扫描"
                description="报告来自已经跑完的扫描。发起并完成一次扫描之后，就能在这里把它整理成可交付的报告。"
              />
            ) : preview.status === 'loading' ? (
              <>
                <PreviewCaption mode="compose" />
                <ReportSheetSkeleton />
              </>
            ) : (
              <EmptyPreview mode="compose" />
            )}
          </div>
        </div>

        {/* ══ 历史列表（历史模式下占满下方的整行）══ */}
        {mode === 'history' && (
          <div className="mt-bay rounded-md border border-line bg-surface">
            {/*
              历史列表的三态。与其它页一致：loading 必须先于空状态 ——
              反过来的话进页面会先闪一下「还没有生成过报告」，
              而真相是列表还在路上。
            */}
            {status === 'loading' ? (
              <div className="p-panel">
                <SkeletonRows rows={3} rowClassName="h-16" />
              </div>
            ) : status === 'error' ? (
              <div className="p-panel">
                <ErrorState
                  title="报告历史加载失败"
                  error={loadError}
                  onRetry={() => void loadReports()}
                />
              </div>
            ) : (
              <ReportHistory
                reports={reports}
                onOpen={(report) => setOpenedId(report.id)}
                onDownload={handleDownload}
                onDelete={handleDelete}
                downloadingId={downloadingId}
                describeScopeOf={(report) => describeScopeOf(report, scans)}
                emptyState={
                  <div className="flex flex-col items-center gap-tight text-center">
                    <FileText
                      size={20}
                      strokeWidth={1.5}
                      className="text-ink-faint"
                      aria-hidden="true"
                    />
                    <p className="text-sm text-ink-muted">还没有生成过报告</p>
                    <p className="max-w-[46ch] text-xs text-ink-faint">
                      在「生成报告」里选择范围与内容即可产出第一份。已生成的报告会保留当时的条目与口径，
                      不会随漏洞状态的变化而改变。
                    </p>
                  </div>
                }
              />
            )}
          </div>
        )}
      </div>
    </>
  )
}

// ─────────────────────────────────────────────
// 小组件
// ─────────────────────────────────────────────

function ViewTab({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean
  onClick: () => void
  icon: React.ReactNode
  label: string
}) {
  return (
    <Button
      variant="ghost"
      size="sm"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      /*
        `role="tab"` 的选中态用「浮起底色」表达（与右侧预览区同一层级）。
        基元 `ghost` 的默认文字色是 `ink-faint`（更淡），而这一排 tab 原来用的是
        `ink-muted` —— 不改回去会整体变淡一档，所以用 className 覆盖回来。
      */
      className={cn('text-ink-muted', active && 'bg-surface-raised text-ink')}
    >
      {icon}
      {label}
    </Button>
  )
}

/** 历史报告的口径卡片（只读）—— 回答"这份是怎么生成的" */
function ReportMetaCard({
  report,
  scans,
  onDownload,
  downloadingId,
}: {
  report: Report
  scans: { id: string; name: string }[]
  onDownload: (report: Report) => void
  downloadingId: string | null
}) {
  const o = report.options
  return (
    <div className="flex flex-col gap-tight rounded-md border border-line bg-surface p-panel">
      <h3 className="text-xs font-medium text-ink">生成口径</h3>
      <p className="text-2xs text-ink-faint">
        这份报告在生成时固定了条目与选项，之后不会改变。重新下载得到的是当初那一份。
      </p>

      <dl className="mt-1 flex flex-col gap-1.5 text-2xs">
        <MetaRow label="覆盖范围" value={describeScopeOf(report, scans)} />
        <MetaRow label="条目数" value={`${report.findingCount} 条`} />
        {/* 有排除就写出来 —— 与报告正文摘要里的那句话同源同口径 */}
        {report.excludedCount > 0 && (
          <MetaRow label="已排除" value={`${report.excludedCount} 条（按当时的选项）`} />
        )}
        <MetaRow label="导出格式" value={report.format === 'pdf' ? 'PDF' : 'Markdown'} />
        <MetaRow label="修复建议" value={o.includeRemediation ? '包含' : '未包含'} />
        <MetaRow label="验证脚本" value={o.includePoc ? '包含' : '未包含'} />
        <MetaRow label="判断依据" value={o.includeReasoning ? '包含' : '未包含'} />
        <MetaRow label="条目筛选" value={o.onlyConfirmed ? '仅已确认' : '全部状态'} />
      </dl>

      {/*
        ★ 这里必须是 `busy` 而不是普通 `disabled`。

        请求中的文案是「正在生成…」，那是在说**等一等**；而 `disabled` 的默认
        光标是 `cursor-not-allowed`（不许点）。两个信号互相打脸 ——
        用户看到"等"，指针却告诉他"不行"。`busy` 把光标换成 `cursor-wait`
        且不压暗，与文案一致。依据见 `components/ui/Button.tsx` 文件头
        「两类禁用语义」。
      */}
      <Button
        variant="secondary"
        size="sm"
        onClick={() => onDownload(report)}
        busy={downloadingId === report.id}
        className="mt-1"
      >
        <Download size={12} strokeWidth={2} aria-hidden="true" />
        {downloadingId === report.id ? '正在生成…' : '重新下载'}
      </Button>
    </div>
  )
}

function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-tight">
      <dt className="shrink-0 text-ink-faint">{label}</dt>
      <dd className="m-0 min-w-0 text-right text-ink-muted">{value}</dd>
    </div>
  )
}

function EmptyPreview({ mode }: { mode: 'compose' | 'history' }) {
  return (
    <div className="flex flex-col items-center justify-center gap-tight rounded-md border border-dashed border-line-strong px-panel py-bay text-center">
      <FileText size={20} strokeWidth={1.5} className="text-ink-faint" aria-hidden="true" />
      <p className="text-sm text-ink-muted">
        {mode === 'history' ? '从下方列表中选择一份报告' : '当前范围下没有可预览的内容'}
      </p>
      <p className="max-w-[46ch] text-xs text-ink-faint">
        {mode === 'history'
          ? '选中后会在这里显示它与当初生成时一致的内容。'
          : '换一个数据范围，或减少筛选条件。'}
      </p>
    </div>
  )
}

/**
 * 白纸外围的一层浅底。
 *
 * 报告是白底（它是要交付、要打印的东西），直接贴在近黑画布上
 * 会像一块发光的板子。垫一层过渡，视觉上才读得出"这是一张纸"。
 */
function PaperSheet({ children }: { children: React.ReactNode }) {
  return <div className="rounded-md border border-line bg-[#E8EAEE] p-3 sm:p-5">{children}</div>
}

/**
 * 说明这条预览是哪一份。
 *
 * 历史报告与草稿的**排版完全一样**，不标出来用户分不清在看
 * "将生成的"还是"已生成的" —— 而这两者的差别恰恰是他最关心的：
 * 前者还能改，后者改不了了。
 */
function PreviewCaption({ mode }: { mode: 'compose' | 'history' }) {
  return (
    <div className="mb-tight flex flex-wrap items-center gap-x-tight gap-y-1 text-2xs text-ink-faint">
      <span
        className={cn(
          'h-1.5 w-1.5 rounded-pill',
          mode === 'history' ? 'bg-signal-ok' : 'bg-accent',
        )}
      />
      {mode === 'history' ? (
        <span>历史报告预览 · 内容在生成时固定，不随漏洞状态变化</span>
      ) : (
        <span>草稿预览 · 与导出的 PDF / Markdown 内容一致；报告编号在生成时分配</span>
      )}
    </div>
  )
}

/**
 * 报告预览的骨架。
 *
 * ★ 刻意做成"一张纸"的形状（白色纸面 + 纸灰的条块），而不是站内那套
 *   深色骨架。理由不是好看：报告页的加载态如果看起来像"这里什么都没有"，
 *   它会被读成**"这份报告是空的"** —— 而"空报告"是一份结论，
 *   不是一种加载状态。两者的后果完全不同，视觉上就不能像。
 *
 * ⚠️ 骨架块的底色必须换成纸灰（`PRINT_INK.rule`）。站内的
 *   `bg-surface-raised` 是近黑色，铺在白纸上会变成一排黑条。
 */
function ReportSheetSkeleton() {
  const bar = 'bg-[#D9DDE3]'
  return (
    <PaperSheet>
      <div
        className="flex flex-col gap-bay bg-white p-6 sm:p-8"
        role="status"
        aria-live="polite"
        aria-busy="true"
      >
        <span className="sr-only">正在加载报告内容</span>
        <Skeleton className={cn('h-5 w-1/3', bar)} />
        <Skeleton className={cn('h-3.5 w-1/5', bar)} />
        <div className="flex flex-col gap-tight">
          <Skeleton className={cn('h-3.5 w-full', bar)} />
          <Skeleton className={cn('h-3.5 w-11/12', bar)} />
          <Skeleton className={cn('h-3.5 w-4/5', bar)} />
        </div>
        <Skeleton className={cn('h-24 w-full rounded-sm', bar)} />
        <div className="flex flex-col gap-tight">
          <Skeleton className={cn('h-3.5 w-2/5', bar)} />
          <Skeleton className={cn('h-3.5 w-full', bar)} />
          <Skeleton className={cn('h-3.5 w-3/4', bar)} />
        </div>
      </div>
    </PaperSheet>
  )
}

// ─────────────────────────────────────────────
// 派生辅助
// ─────────────────────────────────────────────

/**
 * 报告标题的默认值。
 *
 * 用「目标/扫描名 + 报告类型」而不是"未命名报告"：
 * 历史列表里一屏可能出现多份，标题必须能区分它们。
 * 用户不填时也不该出现一串无法辨认的名字。
 */
function defaultTitle(scope: ReportScope, scans: { id: string; name: string }[]): string {
  switch (scope.kind) {
    case 'scan': {
      const name = scans.find((s) => s.id === scope.scanId)?.name
      return name ? `${name} 安全检测报告` : '安全检测报告'
    }
    case 'target':
      return scope.target ? `${scope.target} 安全检测报告` : '安全检测报告'
    case 'range': {
      // 范围报告用日期区间命名 —— 它没有单一目标可指
      const from = scope.from.slice(0, 10)
      const to = scope.to.slice(0, 10)
      return `${from} 至 ${to} 安全检测报告`
    }
    case 'findings':
      // 手选的没有单一目标或日期可指，用条数 —— 它正好说明这份报告的规模
      return `手选 ${scope.findingIds.length} 条漏洞的安全报告`
  }
}

/**
 * 范围的人话描述（历史列表与口径卡片共用）。
 *
 * ★ 必须解析扫描**名**，不能直接把 scanId 印出来。
 *   早先这里自己写了一份 switch，扫描分支直接返回 `扫描任务 scan_01J8W9`，
 *   而报告预览走的是数据层的 describeScope（会解析出「acme-web」）——
 *   同一份报告在列表里和预览里成了两种说法。
 *
 *   现在两处都走 describeScope，并统一喂入扫描名。
 *   一份实现、一个口径，这类不一致才不会重新长出来。
 */
function describeScopeOf(report: Report, scans: { id: string; name: string }[]): string {
  const scope = report.scope
  // 先取出 scope 再判断：在闭包里 TS 会丢失对 report.scope 的联合类型收窄
  const scanName =
    scope.kind === 'scan' ? scans.find((s) => s.id === scope.scanId)?.name : undefined
  return describeScope(scope, { scanName })
}

/** 严重度计数投影，供配置区的实时反馈用 */
function countSeverities(findings: { severity: string }[]): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const finding of findings) {
    counts[finding.severity] = (counts[finding.severity] ?? 0) + 1
  }
  return counts
}

export default ReportsPage
