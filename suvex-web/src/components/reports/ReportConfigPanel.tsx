import { cn } from '@/lib/cn'
import { SEVERITY_META, SEVERITY_ORDER } from '@/data/findings'
import {
  REPORT_SCOPE_LABEL,
  type ReportOptions,
  type ReportScope,
  type ReportScopeKind,
} from '@/data/report'
import { RANGE_PRESET_LABEL, type RangePreset } from '@/lib/report-build'
import { ListChecks, ScanSearch, Target, CalendarRange, Check, Minus } from 'lucide-react'

/**
 * 报告配置区：选范围 + 选内容。
 *
 * ★ 一条贯穿本文件的原则：**不做"配错了才知道"的表单。**
 *
 *   报告是交付物，配错的代价是"发出去才发现少了 PoC"或
 *   "给客户看了内部推理过程"。所以每一项都实时说明当前会得到什么：
 *     · 范围选完立刻显示"命中 N 条"；
 *     · 内容开关旁边写明"本范围内没有可复现脚本"并禁用。
 *
 *   实现依据是 lib/report-build.ts 的 availableContent ——
 *   如果范围内一条 PoC 都没有，还让用户勾"包含 PoC 细节"，
 *   勾完发现报告没变化，用户会以为功能坏了。
 *   禁用并说明原因才是诚实的做法。
 */

// ─────────────────────────────────────────────
// 范围选择
// ─────────────────────────────────────────────

export interface ScopePickerProps {
  scope: ReportScope
  onChange: (scope: ReportScope) => void
  /**
   * 范围**类型**切换。
   *
   * 为什么不在这里直接算出新 scope 的默认值：算 range 的默认值需要
   * 全量发现与"现在"（见 report-build 的 presetToRange），
   * 那是页面层的数据。早期版本在这里用一个本地函数拼默认值，
   * 结果 range 分支只能给出空日期 —— 用户切过去命中 0 条，
   * 看着像功能坏了。默认值的决策必须发生在拿得到数据的地方。
   */
  onKindChange: (kind: ReportScopeKind) => void
  /** 可选扫描（只列已经跑出结果的） */
  scans: { id: string; name: string; target: string; count: number }[]
  /** 可选目标 */
  targets: { name: string; count: number }[]
  /** 时间区间预设 */
  preset: RangePreset
  onPresetChange: (preset: RangePreset) => void
}

export function ScopePicker({
  scope,
  onChange,
  onKindChange,
  scans,
  targets,
  preset,
  onPresetChange,
}: ScopePickerProps) {
  return (
    <section aria-labelledby="scope-heading" className="flex flex-col gap-tight">
      <h3 id="scope-heading" className="text-xs font-medium text-ink">
        数据范围
      </h3>

      {/* ── 三种范围类型（单选）──
          用 tab 语义而不是三个卡片：它们是**互斥的取值**，不是三个并列功能。
          卡片阵列会让人以为可以多选，而且等大卡片正是 design-system §9 禁止的。

          ★ "手选的漏洞"这一支**只在它已是当前取值时才出现**（任务 10 P5）——
          它不是用户能在报告页"选出来"的东西：勾选发生在漏洞页，
          在这里点它只会得到一个没有 id 的空范围。让它作为可选 tab 常驻，
          等于摆一个点了必然空的按钮。 */}
      <div role="radiogroup" aria-label="数据范围类型" className="flex gap-1">
        {(Object.keys(REPORT_SCOPE_LABEL) as ReportScopeKind[])
          .filter((kind) => kind !== 'findings' || scope.kind === 'findings')
          .map((kind) => {
          const active = scope.kind === kind
          const Icon = SCOPE_ICON[kind]
          return (
            <button
              key={kind}
              type="button"
              role="radio"
              aria-checked={active}
              onClick={() => onKindChange(kind)}
              className={cn(
                'inline-flex h-8 flex-1 items-center justify-center gap-1.5 rounded-sm border text-xs',
                'transition-ui duration-fast',
                active
                  ? 'border-accent/50 bg-accent-wash text-ink'
                  : 'border-line text-ink-muted hover:border-line-strong hover:text-ink',
              )}
            >
              <Icon size={13} strokeWidth={1.75} aria-hidden="true" />
              {REPORT_SCOPE_LABEL[kind]}
            </button>
          )
        })}
      </div>

      {/* ── 范围的具体取值 ── */}
      {/*
        手选的漏洞（任务 10 P5）。

        ★ 这里**不给选择器** —— 勾选发生在漏洞页，报告页只负责接收。
        给一个"选择漏洞"的多选框意味着要在报告页重建一套漏洞列表 UI，
        而那一页已经有它了。所以这里只说清楚"收到了几条"，
        以及"想改就去漏洞页重选"。
      */}
      {scope.kind === 'findings' && (
        <div className="rounded-sm border border-line bg-surface-raised px-panel py-2.5">
          <p className="text-xs text-ink">
            已接收 <span className="tabular" data-numeric>{scope.findingIds.length}</span> 条手选的漏洞。
          </p>
          <p className="mt-0.5 text-2xs text-ink-faint">
            想换一批，回「漏洞发现」勾选后重新点「生成报告」。
          </p>
        </div>
      )}

      {scope.kind === 'scan' && (
        <Field label="选择扫描任务" htmlFor="report-scan">
          <select
            id="report-scan"
            value={scope.scanId}
            onChange={(e) => onChange({ kind: 'scan', scanId: e.target.value })}
            className={SELECT_CLASS}
            disabled={scans.length === 0}
          >
            {scans.length === 0 && <option value="">没有可用的扫描结果</option>}
            {scans.map((scan) => (
              <option key={scan.id} value={scan.id} className="bg-surface-overlay">
                {scan.name}（{scan.count} 条）
              </option>
            ))}
          </select>
        </Field>
      )}

      {scope.kind === 'target' && (
        <Field label="选择目标" htmlFor="report-target">
          <select
            id="report-target"
            value={scope.target}
            onChange={(e) => onChange({ kind: 'target', target: e.target.value })}
            className={SELECT_CLASS}
            disabled={targets.length === 0}
          >
            {targets.length === 0 && <option value="">没有可用的目标</option>}
            {targets.map((target) => (
              <option key={target.name} value={target.name} className="bg-surface-overlay">
                {target.name}（{target.count} 条）
              </option>
            ))}
          </select>
        </Field>
      )}

      {scope.kind === 'range' && (
        <div className="flex flex-col gap-tight">
          {/* 预设在前、精确日期在后：多数人只想选"最近 7 天"，
              精确区间是少数场景。把日期输入放前面会让常见路径变长。 */}
          <Field label="时间范围" htmlFor="report-preset">
            <div className="flex gap-1" role="radiogroup" aria-label="时间范围预设">
              {(Object.keys(RANGE_PRESET_LABEL) as RangePreset[]).map((key) => (
                <button
                  key={key}
                  type="button"
                  role="radio"
                  aria-checked={preset === key}
                  onClick={() => onPresetChange(key)}
                  className={cn(
                    'h-8 flex-1 rounded-sm border text-2xs transition-ui duration-fast',
                    preset === key
                      ? 'border-accent/50 bg-accent-wash text-ink'
                      : 'border-line text-ink-muted hover:border-line-strong hover:text-ink',
                  )}
                >
                  {RANGE_PRESET_LABEL[key]}
                </button>
              ))}
            </div>
          </Field>

          <div className="grid grid-cols-2 gap-tight">
            <Field label="开始日期" htmlFor="report-from">
              <input
                id="report-from"
                type="date"
                value={scope.from.slice(0, 10)}
                max={scope.to.slice(0, 10)}
                onChange={(e) => onChange({ ...scope, from: toIso(e.target.value, scope.from) })}
                className={INPUT_CLASS}
              />
            </Field>
            <Field label="结束日期" htmlFor="report-to">
              <input
                id="report-to"
                type="date"
                value={scope.to.slice(0, 10)}
                min={scope.from.slice(0, 10)}
                onChange={(e) => onChange({ ...scope, to: toIso(e.target.value, scope.to) })}
                className={INPUT_CLASS}
              />
            </Field>
          </div>
        </div>
      )}
    </section>
  )
}

const SCOPE_ICON = {
  scan: ScanSearch,
  target: Target,
  range: CalendarRange,
  // 手选（任务 10 P5）—— 用「勾选」的语义，与另外三种「按条件圈定」区分开
  findings: ListChecks,
} as const

/**
 * 日期输入 → ISO。
 *
 * `<input type="date">` 给的是 `YYYY-MM-DD`，直接 new Date() 会按 **UTC 午夜**
 * 解析，在东八区会变成前一天 —— 用户选"2月14日"却得到 2月13日 16:00。
 * 时间区间过滤（见 report-build.ts）用的是本地时间比较，
 * 所以这里也必须按本地时间构造。
 */
function toIso(dateValue: string, fallback: string): string {
  if (!dateValue) return fallback
  const [y, m, d] = dateValue.split('-').map(Number)
  if (!y || !m || !d) return fallback
  return new Date(y, m - 1, d).toISOString()
}

// ─────────────────────────────────────────────
// 内容选项
// ─────────────────────────────────────────────

export interface ContentOptionsProps {
  options: ReportOptions
  onChange: (options: ReportOptions) => void
  /** 本范围内实际能提供什么 —— 决定哪些开关该被禁用 */
  available: { poc: boolean; reasoning: boolean; remediation: boolean; unconfirmed: boolean }
  /** 命中条数，供文案使用 */
  hitCount: number
}

export function ContentOptions({ options, onChange, available, hitCount }: ContentOptionsProps) {
  return (
    <section aria-labelledby="content-heading" className="flex flex-col gap-tight">
      <h3 id="content-heading" className="text-xs font-medium text-ink">
        包含内容
      </h3>

      <div className="flex flex-col gap-1.5">
        <Toggle
          id="opt-remediation"
          label="修复建议"
          hint="每条问题附上可执行的修复方向"
          checked={options.includeRemediation}
          disabled={!available.remediation}
          disabledHint="本范围内没有可用的修复建议"
          onChange={(v) => onChange({ ...options, includeRemediation: v })}
        />

        <Toggle
          id="opt-poc"
          label="验证脚本（PoC）"
          hint="附上可复现问题的脚本与请求原文"
          checked={options.includePoc}
          disabled={!available.poc}
          disabledHint="本范围内没有可复现的验证脚本"
          onChange={(v) => onChange({ ...options, includePoc: v })}
        />

        <Toggle
          id="opt-reasoning"
          label="判断依据"
          hint="智能体判定该问题存在的过程说明"
          /*
            推理过程的措辞刻意不用"推理过程"作为标签名。
            「推理」会让非技术读者以为里面是模型的思考噪音；
            实际内容是"凭什么判断这里有问题"。标签写"判断依据"，
            含义对读者是可理解的 —— 这是文案层面的专业性，
            和它下面那句 hint 一起把真实内容说清楚。
          */
          checked={options.includeReasoning}
          disabled={!available.reasoning}
          disabledHint="本范围内没有可用的判断依据"
          onChange={(v) => onChange({ ...options, includeReasoning: v })}
        />

        <Toggle
          id="opt-confirmed"
          label="只导出已确认的问题"
          hint="排除待确认与已判定为误报的条目"
          checked={options.onlyConfirmed}
          disabled={!available.unconfirmed}
          disabledHint={
            // 两种"禁用"的原因不同，文案必须分开
            hitCount === 0 ? '本范围内没有可导出的条目' : '本范围内没有待确认或误报的条目'
          }
          onChange={(v) => onChange({ ...options, onlyConfirmed: v })}
        />
      </div>
    </section>
  )
}

function Toggle({
  id,
  label,
  hint,
  checked,
  disabled,
  disabledHint,
  onChange,
}: {
  id: string
  label: string
  hint: string
  checked: boolean
  disabled: boolean
  disabledHint: string
  onChange: (value: boolean) => void
}) {
  return (
    <div
      className={cn(
        'flex items-start gap-tight rounded-sm border p-2.5 transition-ui duration-fast',
        disabled ? 'border-line/50' : 'border-line hover:border-line-strong',
      )}
    >
      {/*
        真 checkbox 而不是开关滑块。
        滑块看起来更"现代"，但它在无障碍上是更差的控件：
        读屏器念不出可靠的状态，键盘用户也拿不到标准行为。
        而且这里要同时表达"禁用 + 原因"，原生 checkbox 的 disabled
        语义是现成的，滑块要手写。
      */}
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className={cn(
          'mt-0.5 h-3.5 w-3.5 shrink-0 rounded-xs border-line bg-surface-sunken',
          'accent-accent',
          // 与 `components/ui/Checkbox.tsx` 同一条规矩：禁用不压 opacity，
          // 改成三处显式降级（选中色 dim / 边框半淡 / 底色退一档）
          disabled
            ? 'cursor-not-allowed accent-accent-dim border-line/50 bg-surface'
            : 'cursor-pointer',
        )}
      />

      <label
        htmlFor={id}
        className={cn('min-w-0 flex-1', disabled ? 'cursor-not-allowed' : 'cursor-pointer')}
      >
        <span className={cn('block text-xs', disabled ? 'text-ink-faint' : 'text-ink')}>
          {label}
        </span>
        {/* 禁用时把 hint 换成禁用原因 —— 这比保留原 hint 更有用：
            用户此刻的问题是"为什么点不了"，不是"它是干什么的" */}
        <span className="mt-0.5 block text-2xs text-ink-faint">
          {disabled ? disabledHint : hint}
        </span>
      </label>
    </div>
  )
}

// ─────────────────────────────────────────────
// 命中预览条
// ─────────────────────────────────────────────

/**
 * 命中摘要。
 *
 * 这是配置区最重要的一块反馈：用户改任何一项，**立刻能看到会得到什么**。
 * 没有它，用户只能靠"生成 → 看结果 → 回去改"来试错，
 * 而报告的生成是有成本的（且概念上是一次交付）。
 */
export function ScopePreview({
  hitCount,
  severityCounts,
  excludedCount,
}: {
  hitCount: number
  severityCounts: Partial<Record<string, number>>
  excludedCount: number
}) {
  const shown = hitCount - excludedCount

  return (
    <div
      role="status"
      aria-live="polite"
      className="flex flex-col gap-tight rounded-sm border border-line bg-surface-sunken p-2.5"
    >
      <div className="flex items-baseline gap-1.5">
        <span className="text-xs text-ink-muted">报告将包含</span>
        <span className="tabular text-sm font-medium text-ink" data-numeric>
          {shown}
        </span>
        <span className="text-xs text-ink-muted">条发现</span>
      </div>

      {/* 严重度分布预览。有排除时同时显示原始命中数，避免"数字对不上" */}
      {hitCount > 0 && (
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
          {SEVERITY_ORDER.filter((sev) => (severityCounts[sev] ?? 0) > 0).map((sev) => (
            <span key={sev} className="flex items-center gap-1 text-2xs">
              <span
                aria-hidden="true"
                className={cn('h-1.5 w-1.5 rounded-pill', SEVERITY_META[sev].bg)}
              />
              <span className={SEVERITY_META[sev].text}>{SEVERITY_META[sev].label}</span>
              <span className="tabular text-ink-faint" data-numeric>
                {severityCounts[sev]}
              </span>
            </span>
          ))}
        </div>
      )}

      {excludedCount > 0 && (
        <p className="text-2xs text-ink-faint">
          已按「只导出已确认」排除 {excludedCount} 条（命中 {hitCount} 条）
        </p>
      )}

      {shown === 0 && (
        <p className="text-2xs text-signal-warn">
          {/* 空报告是可生成的（"本次未发现问题"本身是有效结论），
              但用户要能分清"确实没问题"和"筛错了"。 */}
          {hitCount === 0
            ? '该范围内没有发现，报告将说明「本次未发现问题」。'
            : '当前条件下没有条目会被包含，请检查上面的选项。'}
        </p>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────
// 小组件
// ─────────────────────────────────────────────

/** 标签在上、控件在下 —— 与 Findings 页的筛选区约定一致（design-system §4） */
function Field({
  label,
  htmlFor,
  children,
}: {
  label: string
  htmlFor: string
  children: React.ReactNode
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={htmlFor} className="text-2xs text-ink-faint">
        {label}
      </label>
      {children}
    </div>
  )
}

const SELECT_CLASS =
  'h-8 w-full rounded-sm border border-line bg-surface-sunken px-2 text-xs text-ink transition-ui duration-fast hover:border-line-strong focus:outline-none focus:ring-2 focus:ring-accent/40'

const INPUT_CLASS =
  'h-8 w-full rounded-sm border border-line bg-surface-sunken px-2 text-xs text-ink transition-ui duration-fast hover:border-line-strong focus:outline-none focus:ring-2 focus:ring-accent/40'

/** 供页面复用的勾选标记（历史列表里表示"包含/不包含"） */
export function IncludeMark({ included }: { included: boolean }) {
  return included ? (
    <Check size={11} strokeWidth={2.5} className="text-signal-ok" aria-label="包含" />
  ) : (
    <Minus size={11} strokeWidth={2.5} className="text-ink-ghost" aria-label="不包含" />
  )
}

export default ScopePicker