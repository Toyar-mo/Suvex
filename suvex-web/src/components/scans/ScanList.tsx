import { cn } from '@/lib/cn'
import Checkbox from '@/components/ui/Checkbox'
import { FlagChip } from '@/components/ui/FlagChip'
import { StatusDot, type StatusTone } from '@/components/ui/StatusDot'
import { SEVERITY_META, SEVERITY_ORDER } from '@/data/findings'
import {
  SCAN_STATUS_LABEL,
  SCAN_STATUS_TONE,
  TARGET_KIND_LABEL,
  stallInfo,
  totalFindings,
  type Scan,
  type ScanStatus,
} from '@/data/scan'
import { formatDuration, formatRelativeTime } from '@/lib/datetime'
import {
  AlertTriangle,
  Ban,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  FileText,
  MoreHorizontal,
  Play,
  RotateCcw,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

/**
 * 扫描列表。
 *
 * 每条一行的横向布局在宽屏高效，但窄屏塞不下这么多列 —— 所以窄屏改成卡片式堆叠。
 * 两种形态共用同一份数据，只是排版方式不同（见 Row 的响应式类）。
 *
 * 状态语言与全站一致：running 用 accent 呼吸、failed/aborted 用 risk、
 * completed 用 ok、其余中性（SCAN_STATUS_TONE 就取自这里，不另立一套）。
 *
 * ## 选择列（任务 9）
 *
 * checkbox **常驻显示**，不做"悬停才出现"：
 * 悬停显隐在触屏上等于不存在，而这一页在平板上看是常见用法。
 * 代价是每行多占 24px —— 比"某些设备上根本没法多选"划算。
 *
 * ## 失败原因改成可展开的短标签（任务 9）
 *
 * 原来它**始终占一整行**。一条两条还行，十条失败记录就是十行灰字，
 * 而用户多数时候只想知道"为什么挂了"的**类别**，不是全文。
 * 现在折成一个标签（⚠️ 基础设施中断 / 🛑 用户终止），点开才看全文。
 *
 * 注意这里**没有**把原因藏起来：标签本身就在那儿，而且带着类别名。
 * 藏起来的只是"全文"，那是次要信息。
 *
 * ## 名称要看得见（2026-09-27 实测反馈）
 *
 * 向导里给扫描起了名（`pwn`），列表那一行的主行却仍然只有目标地址
 * `2a12c5aed2024b9cdce5cb80.tcp-ctf2.dasctf.com:9999` ——
 * 因为名称当时**只挂在 `title` 上**（悬停提示），不悬停就永远看不到。
 * 于是"起了名"和"没起名"在列表里长得一模一样，向导里那个名称输入框等于白填。
 *
 * 现在：名称与目标**不同**时，名称升为主行、目标降为副行；
 * 相同时维持原样。判据与理由都放在 `named` 那里。
 */

/** 失败 / 终止原因的类别。图标 + 短标签，点开看全文。 */
function failureBadge(scan: Scan): { icon: typeof Ban; label: string } | null {
  if (scan.status === 'aborted') {
    if (scan.terminatedBy === 'user') return { icon: Ban, label: '用户终止' }
    if (scan.terminatedBy === 'system') return { icon: AlertTriangle, label: '系统中断' }
    // 判不出来就说"已终止"，不编一个原因
    return { icon: Ban, label: '已终止' }
  }
  if (scan.status === 'failed') {
    return { icon: AlertTriangle, label: '基础设施中断' }
  }
  return null
}

/**
 * 终止方（"谁按的停止"）。
 *
 * ⚠️ 这是**类别**不是**身份**：后端 `terminated_by` 只存 `user` /
 * `system` 两个字面量，**不记录具体是哪个账号**。所以多用户之后
 * 这一栏仍然显示"用户本人"，而不是某个用户名 —— 这是当前实现的
 * 事实，不是还没改的遗留。
 *
 * 它存在的理由是**契约 §5.4 那条**：留痕要能被独立理解 ——
 * "是谁按的停止"与"是系统按的"在追责时是两件不同的事。
 * 系统侧已经有真实的 `system` 取值（超时、节点回收、重启孤儿回收
 * 三处都会写，见 `app/scans/scheduler.py`）。
 *
 * 要把"用户本人"细化到具体账号，需要后端加一列（比如
 * `terminated_by_user_id`）—— 那是一次带迁移的改动，而不是前端
 * 能单独完成的事。
 */
function terminatedByLabel(value: Scan['terminatedBy']): string | null {
  switch (value) {
    case 'user':
      return '用户本人'
    case 'system':
      return '系统'
    case 'unknown':
      // 如实说"无法判定"，不猜成用户本人 —— 一个看起来确定的错值
      // 在追责时比一个诚实的空白危险得多。
      return '无法判定'
    default:
      return null
  }
}

/*
  ─────────────────────────────────────────────────────────────
  状态胶囊的取色（2026-10-16）
  ─────────────────────────────────────────────────────────────

  ## 为什么是「胶囊」而不是「整行铺底」

  用户的原话是「运行中跟已完成跟失败都可以弄一个底色，好分别一点」，
  主管把范围收窄成**只有状态这一格**。这个收窄是对的，两个理由：

  1. **整行铺底会和行自身的两种底色打架**：选中态是 `bg-accent/[0.07]`，
     hover 是 `bg-surface-raised/50`（不透明）。整行铺底时，"选中 running 的行"
     要在同一个元素上放 `accent/[0.05]` 与 `accent/[0.07]` 两个背景色 ——
     只会互相覆盖；而 hover 的不透明底会把状态色整段盖掉。
     收窄到状态格之后，这三个问题**都不存在了**。
  2. **design-system §2 的允许范围**：signal 色「只出现在徽标、状态点、
     行头小图标上——不允许做背景填充或大色块」。**状态胶囊正好落在
     "徽标"这一项里**，而整行铺底不是。

  ## 取色不是挑的，是算出来的

  底色一律用 **tone 色 + 低透明度**（wash），不是实色铺底 ——
  实色底 + 文字在 §7 有实测记录：深紫底配浅字 4.06:1、hover 掉到 2.74:1，不达 AA。

  ⚠️ **这里没有沿用 `ChallengeCard` / `EnvironmentStatus` 的 `bg-*-ok/15`**。
  按那两个组件照抄，实测在**亮色主题**下 `text-signal-ok` 落在 `ok/15` 上只有
  **3.97:1**（`ok/12` 是 4.13:1，连 `ok/6` 都只有 4.48:1）—— 都不达 AA 4.5。
  原因是亮色的 `signal-ok #127A4B` 本身就只有 5.37:1（见 tailwind.config.js
  里那段的说明），再叠一层绿底就把余量吃光了。

  ## 所以文字**不跟着 tone 换色**，一律 `text-ink`

  这是这套 token 里唯一两个主题都能达标的组合。实测（对三种行背景 ——
  surface / 选中 accent.07 / hover raised.50 —— 取**最差**值）：

    tone      暗色(底色 alpha)          亮色(底色 alpha)
    accent    a=0.15  文字 11.84:1      a=0.15  文字 13.54:1
    ok        a=0.15  文字 10.63:1      a=0.15  文字 13.73:1
    risk      a=0.15  文字 11.63:1      a=0.15  文字 13.22:1
    idle      a=0.20  文字 13.40:1      a=0.20  文字 15.64:1

  「文字跟着底色换成 tone 色」听起来更讲究，但它把**颜色的对比度余量**
  和**语义色**绑在一起，于是 `ok` 在亮色下必然挂 —— 上面那组数字就是证据。

  ⚠️ 透明度只写**档位**（`/15`、`/20`），不要写 `/12` 这类不在刻度上的值：
  Tailwind 的 opacity 修饰符按刻度解析，**不在刻度上的数字不会报错，
  而是整个类不生成** —— 胶囊底色会静默变成透明。
  第一版写的 `/12` 就是这么挂的（浏览器实测读到 `rgba(0,0,0,0)` 才发现）。
  刻度（v3）是 5 的倍数：… 10 / 15 / 20 / 25 …。
  本文件与 `ChallengeCard` / `EnvironmentStatus` 用的都是刻度内的值。

  ## 那"不同结果不同颜色"靠什么区分？靠底色 + 状态点，文字保持中性

  这正是 design-system §7「颜色是冗余编码，不是唯一载体」的用法：
  颜色的任务是**加速扫视**（一眼分出哪几行在跑、哪几行挂了），
  而"这条到底是什么状态"由**文字标签**回答，两者都在，没有谁替代谁。
  所以文字标签（`SCAN_STATUS_LABEL`）和状态点（`StatusDot`）**都必须保留** ——
  色觉障碍用户靠前者读，不靠颜色猜。

  ## 为什么 idle（排队中 / 已终止）用 ink-ghost 而不是不加底色

  不加底色的胶囊会变成"只有文字飘在那里"，与其他四行**形状不一致** ——
  列表里一眼扫过去会以为这一格是空的。用 `ink-ghost/20` 给一个几乎中性的
  灰底，形状齐了、又不会抢注意力。

  ⚠️ `ink-ghost` 是「仅装饰、禁止承载信息」的色（design-system §2）——
  这里它只做**底色**，承载信息的文字是 `text-ink`，没有违反那一条。

  ## aborted 为什么不跟 failed 同色

  `SCAN_STATUS_TONE` 里两者就不同（failed=`risk`、aborted=`idle`），
  这里跟随它、不另立一套。语义上也对：「失败」是结论（出事了），
  「已终止」是人主动停的，不是问题 —— 给它一个红底会让用户以为出了故障。
*/
const STATUS_PILL: Record<ScanStatus, string> = {
  queued: 'bg-ink-ghost/20 text-ink',
  running: 'bg-accent/15 text-ink',
  completed: 'bg-signal-ok/15 text-ink',
  failed: 'bg-signal-risk/15 text-ink',
  aborted: 'bg-ink-ghost/20 text-ink',
}

export interface ScanListProps {
  scans: Scan[]
  /** 已选中的扫描 id。用 Set 而不是数组：判定 `has` 是 O(1)，而列表会长 */
  selectedIds: ReadonlySet<string>
  /**
   * 键盘焦点所在的行（j/k 移动）。
   *
   * ⚠️ 它是**独立的**状态，不复用 `selectedIds` ——
   * "我正在看哪一行"与"我选中了哪些"是两件事。
   * 合成一个的话，用 j/k 浏览列表就会一路把它们全选上，
   * 而那时点一下"删除"会删掉一整片。
   */
  focusedId?: string | null
  onToggleRow: (id: string, next: boolean) => void
  /** 全选/取消全选**当前列表里的**记录 */
  onToggleAll: (next: boolean) => void
  onAbort: (scan: Scan) => void
  onRerun: (scan: Scan) => void
  /**
   * 续跑（任务 16）：**同一条记录**接着执行后端上次的会话跑。
   *
   * 不传时行里不显示这一项 —— 与 `onAbort` 一样是按需能力，
   * 而不是"必须实现"的接口。
   */
  onResume?: (scan: Scan) => void
  /**
   * 查看 CTF 题解（Writeup）。
   *
   * ★ **只在 `goal === 'captureFlag'` 的行上出现** —— 判据写在菜单项那里，
   *   理由是"入口本身就是一句承诺"：其余两类任务根本没有题解可看，
   *   摆在那里就是让人点一次才知道没有。
   *
   * 不传时整项不渲染 —— 与 `onResume` 同一个约定（按需能力，
   * 而不是"必须实现"的接口）。
   */
  onWriteup?: (scan: Scan) => void
  /** 空态（没有扫描，或筛选后为空） */
  emptyState: React.ReactNode
}

export function ScanList({
  scans,
  selectedIds,
  focusedId = null,
  onToggleRow,
  onToggleAll,
  onAbort,
  onRerun,
  onResume,
  onWriteup,
  emptyState,
}: ScanListProps) {
  if (scans.length === 0) {
    return <div className="px-panel py-bay">{emptyState}</div>
  }

  const selectedCount = scans.filter((scan) => selectedIds.has(scan.id)).length
  const allSelected = selectedCount === scans.length && scans.length > 0
  /*
    图例只在**真的有发现**时显示。列表全空时摆一排"红=高危 橙=中危"
    是在解释不存在的东西 —— 那是噪音，不是帮助。
  */
  const anyFindings = scans.some((scan) => totalFindings(scan.findings) > 0)

  return (
    <ul className="flex flex-col">
      {/* 严重度图例 —— 圆点本身不带文字，没有图例时用户只能猜 */}
      {anyFindings && (
        <li className="flex flex-wrap items-center gap-x-panel gap-y-1 border-b border-line px-panel py-1.5 text-2xs text-ink-faint">
          <span>严重度：</span>
          {SEVERITY_ORDER.map((sev) => (
            <span key={sev} className="flex items-center gap-1">
              <span aria-hidden="true" className={cn('h-1.5 w-1.5 rounded-pill', SEVERITY_META[sev].bg)} />
              {SEVERITY_META[sev].label}
            </span>
          ))}
        </li>
      )}

      {/*
        表头（仅宽屏）。原来标了 `aria-hidden`，但现在里面有一个**可交互**的
        全选框 —— 对读屏器隐藏一个可聚焦控件是非法的，也会让它变成
        "Tab 能到但念不出来"的坑。所以去掉 aria-hidden。
      */}
      <li className="hidden items-center gap-panel border-b border-line px-panel py-2 text-2xs text-ink-faint lg:flex">
        <span className="flex w-6 shrink-0 items-center">
          <Checkbox
            id="scans-select-all"
            checked={allSelected}
            // 选了一部分时显示第三态。没有它，"选了 3/10" 会被显示成"没选"——
            // 那是在撒谎，而用户会据此以为全选没生效。
            indeterminate={selectedCount > 0 && !allSelected}
            onChange={onToggleAll}
            label={allSelected ? '取消全选' : '全选当前列表'}
          />
        </span>
        <span className="w-24 shrink-0">状态</span>
        <span className="min-w-0 flex-1">目标</span>
        <span className="w-40 shrink-0">发现</span>
        <span className="w-20 shrink-0 text-right">耗时</span>
        <span className="w-24 shrink-0 text-right">开始于</span>
        <span className="w-28 shrink-0 text-right">操作</span>
      </li>

      {scans.map((scan) => (
        <ScanRow
          key={scan.id}
          scan={scan}
          selected={selectedIds.has(scan.id)}
          focused={focusedId === scan.id}
          onToggleRow={onToggleRow}
          onAbort={onAbort}
          onRerun={onRerun}
          onResume={onResume}
          onWriteup={onWriteup}
        />
      ))}
    </ul>
  )
}

function ScanRow({
  scan,
  selected,
  focused,
  onToggleRow,
  onAbort,
  onRerun,
  onResume,
  onWriteup,
}: {
  scan: Scan
  selected: boolean
  focused: boolean
  onToggleRow: (id: string, next: boolean) => void
  onAbort: (scan: Scan) => void
  onRerun: (scan: Scan) => void
  onResume?: (scan: Scan) => void
  onWriteup?: (scan: Scan) => void
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [reasonOpen, setReasonOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)

  // 点击外部关闭菜单。没有这一段，菜单会一直挂着直到下次点击其它地方
  useEffect(() => {
    if (!menuOpen) return
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      if (menuRef.current?.contains(target) || buttonRef.current?.contains(target)) return
      setMenuOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setMenuOpen(false)
        buttonRef.current?.focus()
      }
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [menuOpen])

  const tone = SCAN_STATUS_TONE[scan.status] as StatusTone
  /*
    ★ v0.27：CTF2 借来挂事件流的壳 —— 三个操作入口全部要藏掉。

    为什么**藏**而不是"留着但点了报错"：见 `data/scan.ts` 里 `origin`
    那段。一句话 —— 这三个按钮对这条记录**必然失败**，而失败方式还很坏：
     重跑会得到一条 `exit_code=2` 却记成「已完成 · 0 秒」的假记录
     （strix 拒收 `ctf2:…` 这个 target，argparse 退 2 与
     `EXIT_FINDINGS` 撞车），用户看到成功而题目一个字没动。

    这也符合本文件既有的「入口即承诺」约定（见「题解」那一项上方的说明）：
    摆在那里就是让人点一次才知道不行。

    后端**同样**拦（`409 SCAN_IS_CTF2_SHELL`）—— 两层都要有，
    因为 API 可以被直接调。
  */
  const isCtf2Shell = scan.origin === 'ctf2'
  const canAbort = !isCtf2Shell && (scan.status === 'running' || scan.status === 'queued')
  const found = totalFindings(scan.findings)
  const badge = failureBadge(scan)
  /*
    ★「终止方」只在 **aborted** 下才有意义。

    一次 `failed` 的扫描**没有终止方** —— 它是自己挂掉的（Docker 不可用、
    认证失败、进程崩溃）。给它显示"终止方：无法判定"是把两个不同的概念
    混成一个词：用户会以为"有人停了它，但不知道是谁"，
    而事实是"根本没有人停它"。

    这与 `terminatedBy` 那个字段本身不冲突：它记录的是"这次运行是怎么结束的"，
    而"终止方"这个**标签**只在真的有人终止时才该出现。
  */
  const byWhom = scan.status === 'aborted' ? terminatedByLabel(scan.terminatedBy) : null

  /*
    ★「疑似卡死」（任务 11）。判定（含"必须还在跑"）都在 `stallInfo` 里，
    这里只负责渲染 —— 三个调用点共用同一个判据。
  */
  const stalled = stallInfo(scan)

  /*
    ★ 用户起的名字必须**看得见**。

    起因见文件头「名称要看得见」：名称原来只出现在 `title` 里，
    不悬停就看不到，于是起了名的扫描在列表里和没起名的一模一样。

    ★ 但**不能无条件**用名称当主行。

    名称留空时后端会拿目标地址顶上（向导的原话是"留空则用目标地址作为名称"），
    所以库里 `name === target` 的记录是多数（这条链路上 9 条扫描里 7 条如此）。
    无条件切换会让目标被打印两遍：主行是它、副行还是它。

    判据是 `name !== target` —— 只有用户**真的起了一个不同的名字**时才切换。
    这不需要后端再加一个"是否起过名"的标记：名称与目标相同，
    在语义上本来就等价于"没起名"。
  */
  const named = Boolean(scan.name) && scan.name !== scan.target

  // 主行与副行是同一个去处。抽出来，免得改一处漏一处。
  const activityHref = `/activity?scan=${encodeURIComponent(scan.id)}`

  return (
    <li
      // 焦点环用左侧竖条而不是整行描边：整行描边在"选中底色"之上会打架，
      // 而竖条与底色可以同时存在、各自可读。
      className={cn(
        'border-b border-line/60 last:border-b-0',
        /*
          ⚠️ 这里必须写 `rgb(var(--suvex-accent))`，**不能**只写 `var(--suvex-accent)`。

          token 值现在是 **RGB 通道三元组**（`139 124 246`）而不是 hex ——
          因为 tailwind 那边要靠 `<alpha-value>` 注入透明度（见
          `src/index.css` 文件头的说明）。于是把它直接当颜色用是**无效值**：
          `box-shadow` 整条不生效，键盘焦点的左侧竖条会**静默消失**。

          这条的症状最难发现：焦点条只在**用键盘**时才出现，
          鼠标用户永远看不到，测试也测不到。
        */
        focused && 'shadow-[inset_2px_0_0_0_rgb(var(--suvex-accent))]',
      )}
      data-focused={focused || undefined}
    >
      <div
        className={cn(
          'group flex flex-col gap-tight px-panel py-3 transition-ui duration-fast',
          'lg:flex-row lg:items-center lg:gap-panel lg:py-2.5',
          // 选中行给一层底色 —— 批量操作前必须能一眼看出选了什么
          selected ? 'bg-accent/[0.07]' : 'hover:bg-surface-raised/50',
        )}
      >
        {/* 选择框 */}
        <span className="flex shrink-0 items-center lg:w-6">
          <Checkbox
            id={`scan-select-${scan.id}`}
            checked={selected}
            onChange={(next) => onToggleRow(scan.id, next)}
            label={`选择 ${scan.target}`}
          />
        </span>

        {/*
          状态格 = 状态点 + 文字标签 + 胶囊底色。

          ⚠️ 三者**都要保留**，这是 design-system §7「颜色是冗余编码，
             不是唯一载体」的硬要求：
               · 文字标签（SCAN_STATUS_LABEL）—— 色觉障碍用户靠它读；
               · 状态点（StatusDot）—— 第二层冗余，且 running 靠它呼吸；
               · 胶囊底色 —— 加速扫视，让"哪几行在跑、哪几行挂了"一眼分开。
             把文字去掉只留颜色是**倒退**，不是简化。

          ⚠️ 这里刻意**不**用 `<StatusDot>` 的 tone 去驱动文字颜色：
             取色理由与实测数字见文件上方 STATUS_PILL 那段。
        */}
        <span className="flex shrink-0 items-center lg:w-24">
          <span
            className={cn(
              /*
                `rounded-xs` 而不是 `rounded-pill`：design-system §4 明确写
                「`pill` 只留给状态点，不给按钮用」—— 工具型界面要的是精确感。
                内距取 `gap-1 / px-1.5 / py-0.5`，与同类的状态徽标一致
                （SlaBadge.tsx:74 的 `gap-1 rounded-xs px-1`、
                 FlagChip.tsx:88 的 `gap-1 rounded-xs px-1.5`、
                 EnvironmentStatus.tsx:120 的状态徽标）。
              */
              'inline-flex items-center gap-1 rounded-xs px-1.5 py-0.5',
              STATUS_PILL[scan.status],
            )}
          >
            <StatusDot tone={tone} pulse={scan.status === 'running'} />
            <span className="text-xs">{SCAN_STATUS_LABEL[scan.status]}</span>
          </span>
        </span>

        {/* 名称 + 目标 —— 主信息，宽屏占剩余宽度 */}
        <div className="min-w-0 flex-1">
          {/* 起过名时名称才是主行（判据与理由在 `named` 那里） */}
          {named && (
            <Link
              to={activityHref}
              className="block truncate text-ink transition-ui duration-fast hover:text-accent-strong"
              title={`查看 ${scan.name} 的智能体活动`}
            >
              {scan.name}
            </Link>
          )}

          <Link
            to={activityHref}
            className={cn(
              'mono-ui block truncate transition-ui duration-fast hover:text-accent-strong',
              /*
                名称当主行时，目标降为副行 —— 但它**不能消失**：
                "这次扫的是谁"是排障时第一个要看的东西，比名字重要。
                降下来只是不抢视觉重心。
              */
              named ? 'text-2xs text-ink-muted' : 'text-ink',
            )}
            // 没起名时目标就是主行，悬停提示给"去处"；
            // 起名时目标可能被截断，提示改成它的全文，更实用。
            title={named ? scan.target : `查看 ${scan.name} 的智能体活动`}
          >
            {scan.target}
          </Link>

          <span className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs text-ink-faint">
            {/*
              ★ flag 排在这一行**最前面**（任务 18）。

              对一次夺旗扫描来说，它是整条记录里最要紧的一个值 ——
              用户扫一眼列表想知道的就是"哪道题解出来了"。
              排在目标类型/智能体数这些过程信息后面，等于让它去和
              "3 个智能体"抢注意力。
            */}
            {scan.flag && (
              <>
                <FlagChip flag={scan.flag} />
                <span aria-hidden="true">·</span>
              </>
            )}
            <span>{TARGET_KIND_LABEL[scan.targetKind]}</span>
            <span aria-hidden="true">·</span>
            <span>{scan.agentCount} 个智能体</span>
            {scan.skills.length > 0 && (
              <>
                <span aria-hidden="true">·</span>
                <span>{scan.skills.length} 项检测</span>
              </>
            )}
            {/* running 时给进度百分比 —— 比光转个圈有信息量 */}
            {scan.status === 'running' && scan.progress !== null && (
              <>
                <span aria-hidden="true">·</span>
                <span className="tabular text-accent-strong" data-numeric>
                  {Math.round(scan.progress * 100)}%
                </span>
              </>
            )}
            {/* 已归档 / 回收站的记录在列表里也要能认出来 */}
            {scan.archivedAt && (
              <>
                <span aria-hidden="true">·</span>
                <span className="text-ink-muted">已归档</span>
              </>
            )}
            {scan.deletedAt && (
              <>
                <span aria-hidden="true">·</span>
                <span className="text-signal-risk/80">在回收站</span>
              </>
            )}
            {/* 终止方（仅在确实有值时显示） */}
            {byWhom && (
              <>
                <span aria-hidden="true">·</span>
                <span title="谁终止了这次扫描">终止方：{byWhom}</span>
              </>
            )}
            {/*
              ★「疑似卡死」放在最后，且用 warn 色 —— 它是这一行里
              唯一需要用户**做决定**的信息（继续等 / 终止它）。
              措辞是"疑似"：判定是启发式的，说死了会误导。
            */}
            {stalled && (
              <>
                <span aria-hidden="true">·</span>
                <span className="inline-flex items-center gap-1 text-signal-warn" title={stalled.hint}>
                  <AlertTriangle size={10} strokeWidth={2.25} aria-hidden="true" />
                  疑似卡死 · {stalled.silentLabel}
                </span>
              </>
            )}
          </span>
        </div>

        {/* 发现摘要 */}
        <div className="flex shrink-0 items-center gap-1.5 lg:w-40">
          {found === 0 ? (
            /*
              ★ 零条目时的措辞要看**判据**，不能一律写"未发现漏洞"。

              踩到的场景（实测）：一次 CTF 夺旗跑完，agent 拿到了 flag、
              写了完整报告，但**故意没有产出漏洞条目** —— 题目考的就是
              "识别并解包配置"，那份"漏洞"就是题目本身，报它等于把
              题目设计包装成缺陷。于是 `findings` 是 {}，而列表写着
              "未发现漏洞"，用户读成"这次白跑了"，可真相是任务已完成。

              所以夺旗与复测这两类判据下，改成陈述**条目层面**的事实
              （"无漏洞条目"），而不是下"没发现问题"这个更强的结论 ——
              后者对这两类任务根本不适用。例行评估仍用原措辞，因为
              "未发现漏洞"对它正是准确的说法。
            */
            <span
              className="text-2xs text-ink-faint"
              title={
                scan.status !== 'completed'
                  ? undefined
                  : scan.goal === 'captureFlag'
                    ? '这次扫描没有产出漏洞条目。夺旗任务的成败看是否拿到 flag，不以漏洞条数衡量。'
                    : scan.goal === 'verifyFix'
                      ? '这次复测没有产出新的漏洞条目。复测的结论看已知问题是否已修复，不以漏洞条数衡量。'
                      : undefined
              }
            >
              {scan.status !== 'completed'
                ? '—'
                : scan.goal === 'captureFlag' || scan.goal === 'verifyFix'
                  ? '无漏洞条目'
                  : '未发现漏洞'}
            </span>
          ) : (
            <>
              {/*
                只显示有值的级别，按严重度从高到低 —— 一排 5 个徽标太吵。
                `title` 不是装饰：圆点本身不带文字，没有它就是"一个红点"
                和"一个橙点"，用户只能猜。列表上方的图例是第二道保险。
              */}
              {SEVERITY_ORDER.filter((sev) => (scan.findings[sev] ?? 0) > 0).map((sev) => {
                const meta = SEVERITY_META[sev]
                return (
                  <span
                    key={sev}
                    className="flex items-center gap-1 text-2xs"
                    title={`${meta.label} ${scan.findings[sev]} 条`}
                  >
                    <span aria-hidden="true" className={cn('h-1.5 w-1.5 rounded-pill', meta.bg)} />
                    <span className={cn('tabular', meta.text)} data-numeric>
                      {scan.findings[sev]}
                    </span>
                  </span>
                )
              })}
              {/*
                ★ 被中途终止/失败的扫描，这个数字是**不完整**的。
                不隐藏它（aborted 的含义是"没跑完"，不是"什么都没发现"，
                隐藏会丢掉"已经挖出过高危"这条真实信息），而是如实标注。
              */}
              {(scan.status === 'aborted' || scan.status === 'failed') && (
                <span
                  className="text-2xs text-ink-faint"
                  title="扫描被中途终止，这个数字只是已跑完部分的结果，不代表全部"
                >
                  · 不完整
                </span>
              )}
            </>
          )}
        </div>

        {/* 耗时 */}
        <span
          className="tabular shrink-0 text-2xs text-ink-muted lg:w-20 lg:text-right"
          data-numeric
        >
          {scan.status === 'running' ? '进行中' : formatDuration(scan.durationSec)}
        </span>

        {/* 开始时间 */}
        <time
          dateTime={scan.startedAt}
          className="tabular shrink-0 text-2xs text-ink-faint lg:w-24 lg:text-right"
          // 相对时间（"12 小时前"）看得快但答不了"到底是几点"，
          // 而排障时问的恰恰是后者。悬停给绝对时刻（本地时区）。
          title={new Date(scan.startedAt).toLocaleString()}
        >
          {formatRelativeTime(scan.startedAt)}
        </time>

        {/* 操作 */}
        <div className="flex shrink-0 items-center gap-1 lg:w-28 lg:justify-end">
          <Link
            to={`/activity?scan=${encodeURIComponent(scan.id)}`}
            className="inline-flex h-8 items-center gap-1 rounded-sm px-2 text-2xs text-ink-muted transition-ui duration-fast hover:bg-surface-overlay hover:text-accent-strong"
            title="查看该扫描的智能体活动"
          >
            <ExternalLink size={12} strokeWidth={2} aria-hidden="true" />
            详情
          </Link>

          <div className="relative">
            <button
              ref={buttonRef}
              type="button"
              onClick={() => setMenuOpen((v) => !v)}
              aria-expanded={menuOpen}
              aria-haspopup="menu"
              aria-label={`${scan.target} 的更多操作`}
              className="inline-flex h-8 w-8 items-center justify-center rounded-sm text-ink-muted transition-ui duration-fast hover:bg-surface-overlay hover:text-ink"
            >
              <MoreHorizontal size={14} strokeWidth={2} aria-hidden="true" />
            </button>

            {menuOpen && (
              <div
                ref={menuRef}
                role="menu"
                className="absolute right-0 top-full z-20 mt-1 w-44 animate-stream-in rounded-md border border-line bg-surface-overlay p-1 shadow-overlay"
              >
                {/*
                  ★「题解」只在**夺旗任务**下出现（任务 19）。

                  判据是 `goal === 'captureFlag'`，而不是"有没有 flag"：
                  flag 可能没拿到（题目没解出来），但题解依然存在 ——
                  它记的是**这次是怎么试的**，那对没解出来的题同样有价值。

                  其余两类任务（例行评估 / 复测）后端会回
                  `409 WRITEUP_UNAVAILABLE`，所以**不能让入口出现**：
                  菜单里其它每一项点了都真的会动，摆一个必然失败的项
                  等于让用户点一次才知道"这个没有"。

                  位置放在最前：对一次夺旗扫描来说，"看它怎么解出来的"
                  是用户回到列表最想做的事，而下面三项（重跑 / 续跑 / 终止）
                  都是过程性操作。
                */}
                {onWriteup && scan.goal === 'captureFlag' && (
                  <button
                    role="menuitem"
                    type="button"
                    onClick={() => {
                      setMenuOpen(false)
                      onWriteup(scan)
                    }}
                    className="flex w-full items-center gap-2 rounded-sm px-2.5 py-1.5 text-left text-xs text-ink-muted transition-ui duration-fast hover:bg-surface-raised hover:text-ink"
                    title="查看这次夺旗的题解（Markdown）"
                  >
                    <FileText size={12} strokeWidth={1.75} aria-hidden="true" />
                    题解
                  </button>
                )}

                {!isCtf2Shell && (
                  <button
                    role="menuitem"
                    type="button"
                    onClick={() => {
                      setMenuOpen(false)
                      onRerun(scan)
                    }}
                    className="flex w-full items-center gap-2 rounded-sm px-2.5 py-1.5 text-left text-xs text-ink-muted transition-ui duration-fast hover:bg-surface-raised hover:text-ink"
                  >
                    <RotateCcw size={12} strokeWidth={1.75} aria-hidden="true" />
                    重跑（新建一条记录）
                  </button>
                )}
                {/*
                  续跑：只在**失败 / 已终止**时出现。

                  正在跑的没什么可续（它有活着的进程）；
                  已完成的也不给 —— 它的子 agent 都已经终结，
                  续上去会立刻又结束。那两种情况放出来只会让用户点了没反应。
                */}
                {!isCtf2Shell &&
                  onResume &&
                  (scan.status === 'failed' || scan.status === 'aborted') && (
                  <button
                    role="menuitem"
                    type="button"
                    onClick={() => {
                      setMenuOpen(false)
                      onResume(scan)
                    }}
                    className="flex w-full items-center gap-2 rounded-sm px-2.5 py-1.5 text-left text-xs text-ink-muted transition-ui duration-fast hover:bg-surface-raised hover:text-ink"
                    title="接回上次的会话继续跑 —— 不重复已经做完的推理"
                  >
                    <Play size={12} strokeWidth={1.75} aria-hidden="true" />
                    续跑（接着上次的会话）
                  </button>
                )}

                {canAbort && (
                  <button
                    role="menuitem"
                    type="button"
                    onClick={() => {
                      setMenuOpen(false)
                      onAbort(scan)
                    }}
                    className="flex w-full items-center gap-2 rounded-sm px-2.5 py-1.5 text-left text-xs text-signal-risk transition-ui duration-fast hover:bg-signal-risk/10"
                  >
                    <Ban size={12} strokeWidth={1.75} aria-hidden="true" />
                    终止扫描
                  </button>
                )}
              </div>
            )}
          </div>
        </div>
      </div>

      {/*
        失败/终止原因：**折成一个可展开的短标签**，不再始终占一整行。

        为什么值得改：十条失败记录就是十行灰字，而用户多数时候只想知道
        "为什么挂了"的类别。标签本身就带着类别（⚠️ 基础设施中断 /
        🛑 用户终止），所以这**不是把信息藏起来**，藏起来的只是全文。
      */}
      {badge && (scan.failureReason || byWhom) && (
        <div className="bg-signal-risk/[0.04] px-panel pb-2.5 lg:pl-[9rem]">
          <button
            type="button"
            onClick={() => setReasonOpen((v) => !v)}
            aria-expanded={reasonOpen}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-sm text-2xs text-ink-muted',
              'transition-ui duration-fast hover:text-ink',
              'focus:outline-none focus:ring-2 focus:ring-accent/40',
            )}
          >
            <badge.icon size={12} strokeWidth={2} className="shrink-0 text-signal-risk" aria-hidden="true" />
            {badge.label}
            {reasonOpen ? (
              <ChevronUp size={11} strokeWidth={2} aria-hidden="true" />
            ) : (
              <ChevronDown size={11} strokeWidth={2} aria-hidden="true" />
            )}
          </button>

          {reasonOpen && (
            <div className="mt-1 flex flex-col gap-0.5 text-2xs text-ink-muted">
              {scan.failureReason && <p className="max-w-[92ch]">{scan.failureReason}</p>}
              {byWhom && <p className="text-ink-faint">终止方：{byWhom}</p>}
            </div>
          )}
        </div>
      )}
    </li>
  )
}

export default ScanList
