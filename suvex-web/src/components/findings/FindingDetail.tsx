import { cn } from '@/lib/cn'
import CodeBlock from '@/components/ui/CodeBlock'
import { FindingAttachments } from './FindingAttachments'
import {
  FINDING_CATEGORY_LABEL,
  FINDING_TRANSITION_INTENT,
  FINDING_TRANSITION_LABEL,
  FINDING_STATUS_TRANSITIONS,
  type Finding,
  type FindingStatus,
  type FindingTimelineEntry,
} from '@/data/finding'
import { SEVERITY_META } from '@/data/findings'
import { formatRelativeTime } from '@/lib/datetime'
import {
  CheckCircle2,
  CircleDashed,
  FlaskConical,
  Lightbulb,
  ShieldCheck,
  ShieldX,
  Sparkles,
  UserCheck,
} from 'lucide-react'

/**
 * 漏洞详情。
 *
 * ★ 信息层级（brief 明确的验收点：**先看结论，再看细节**）
 *
 * 版面顺序 = 读者的决策顺序，不是数据的字段顺序：
 *
 *   1. 结论区   它有多严重 / 现在什么状态 / 证据有多硬 / 影响哪里
 *               —— 这四件事决定"要不要继续读"
 *   2. 是什么   描述与影响
 *   3. 凭什么   agent 的推理 —— 这一条是 Suvex 与扫描器的分水岭：
 *               扫描器只给结论，这里要能看出判断依据
 *   4. 证据     原始响应，可验证
 *   5. 脚本     可复现的 PoC
 *   6. 怎么办   修复建议
 *   7. 经过     状态流转时间线
 *
 * 为什么把「推理」排在「证据」前面：证据是给不信任结论的人看的，
 * 推理是给所有读者看的（它解释了为什么这条值得占一个列表位置）。
 * 顺序反了会让每一条都变成"先读一大段日志再说结论"。
 *
 * 底部操作区常驻可见（见 Drawer 的 footer）—— 分诊时用户看完结论就要下判断，
 * 不该让他先滚到底。
 */

export interface FindingDetailProps {
  finding: Finding
}

export function FindingDetail({ finding }: FindingDetailProps) {
  const meta = SEVERITY_META[finding.severity]
  const { detail } = finding

  return (
    <div className="flex flex-col gap-bay px-panel py-bay">
      {/*
        ── 1 · 结论区 ──

        ★ 这里刻意**不重复标题和徽标**。它们在抽屉头部（Drawer 的 title / titleAside），
          而头部是固定不滚动的 —— 读证据、读 PoC 时那几个徽标始终在屏幕上。
          第一版把标题与徽标在头部和正文各放了一遍，视觉评审直接指出
          「信息重复、分散注意力」。重复不等于强调，只是让人多读一遍同样的东西。

        正文的结论区因此只回答**下一个问题**：具体打在哪儿。
      */}
      <section aria-label="结论">
        {/* 影响位置。等宽、可多行 —— 这是要拿去复现的 */}
        <dl className="flex flex-col gap-1.5">
          <div className="flex gap-tight">
            <dt className="w-16 shrink-0 text-2xs text-ink-faint">目标</dt>
            <dd className="mono-ui min-w-0 break-all text-ink-muted">{finding.target}</dd>
          </div>
          <div className="flex gap-tight">
            <dt className="w-16 shrink-0 text-2xs text-ink-faint">受影响</dt>
            <dd className="flex min-w-0 flex-col gap-0.5">
              {detail.affected.map((location) => (
                <span key={location} className="mono-ui break-all text-ink-muted">
                  {location}
                </span>
              ))}
            </dd>
          </div>
          <div className="flex gap-tight">
            <dt className="w-16 shrink-0 text-2xs text-ink-faint">类型</dt>
            <dd className="text-2xs text-ink-muted">{FINDING_CATEGORY_LABEL[finding.category]}</dd>
          </div>
          <div className="flex gap-tight">
            <dt className="w-16 shrink-0 text-2xs text-ink-faint">发现于</dt>
            <dd className="text-2xs text-ink-muted">
              <time dateTime={finding.discoveredAt}>
                {formatRelativeTime(finding.discoveredAt)}
              </time>
              <span className="mono-ui ml-2 text-ink-faint">{finding.id}</span>
            </dd>
          </div>
        </dl>

        {/* 严重度的定义 —— 界面上只放级别名，但新人需要知道这一级意味着什么 */}
        <p className="mt-panel rounded-sm border border-line bg-surface-sunken px-3 py-2 text-xs leading-relaxed text-ink-muted">
          {meta.definition}
        </p>
      </section>

      {/* ── 2 · 描述 ── */}
      <Section title="漏洞描述">
        <p className="text-sm leading-relaxed text-ink-muted">{detail.description}</p>
      </Section>

      {/* ── 3 · 推理（Suvex 的分水岭）── */}
      <Section title="为什么判断有问题" icon={Sparkles} hint="智能体的推理过程">
        {/*
          推理用普通字体而不是等宽：它是散文，是给人读的论证过程。
          用等宽会让它看起来像日志，而日志是跳读的 —— 这段需要真读。
        */}
        <div className="rounded-sm border border-accent/25 bg-accent-wash/40 px-3 py-2.5">
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink-muted">
            {detail.reasoning}
          </p>
        </div>
      </Section>

      {/* ── 4 · 证据 ── */}
      <Section title="证据" hint="原始响应，未经修改">
        <CodeBlock code={detail.evidence} language="http" title="原始响应" />
      </Section>

      {/* ── 5 · PoC ── */}
      {detail.poc ? (
        <Section title="验证脚本" hint="可直接运行以复现">
          <CodeBlock
            code={detail.poc.code}
            language={detail.poc.language}
            title={`poc.${extensionFor(detail.poc.language)}`}
            maxHeight="26rem"
          />
        </Section>
      ) : (
        /*
          没有 PoC 时**明确说明原因**，而不是静默省略这一节。
          用户看到"这个漏洞没有验证脚本"会想知道为什么 ——
          通常是因为证据已足够、或环境不允许执行。
        */
        <Section title="验证脚本">
          <p className="rounded-sm border border-dashed border-line-strong px-3 py-2.5 text-xs text-ink-faint">
            这条发现没有附带可执行脚本
            {finding.verification === 'verified'
              ? '（证据已足以确认，无需脚本复现）。'
              : '。智能体尚未写出可复现的脚本，或当前环境不允许执行。'}
          </p>
        </Section>
      )}

      {/* ── 6 · 修复建议 ── */}
      {detail.remediation && (
        <Section title="修复建议">
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink-muted">
            {detail.remediation}
          </p>
        </Section>
      )}

      {/* ── 7 · 时间线 ── */}
      <Section title="操作记录">
        <Timeline entries={finding.timeline} />
      </Section>

      {/*
        ── 8 · 处置证据附件（任务 10 P6-d）──

        ★ 放在**最末**：它是"我做了什么"的存档，而详情页的阅读顺序是
        「先看结论 → 再看细节 → 最后看处置记录」。附件属于最后一档。

        ⚠️ 只有真正的详情才渲染它。列表项的 `Finding` 是**摘要**
        （`detail` 是空占位、`timeline` 是空数组），那种对象上渲染附件
        会去请求一条可能不存在的记录 —— 而列表项本来也不该出现在
        详情抽屉里（见 `FindingsPage` 的说明：正文永远不用列表项渲染）。
      */}
      <Section title="处置证据">
        <FindingAttachments findingId={finding.id} />
      </Section>
    </div>
  )
}

/**
 * 状态流转操作区。放到 Drawer 的 footer 里。
 *
 * 只渲染**合法**的流转（见 FINDING_STATUS_TRANSITIONS）：
 * 用户不会看到一个点了没反应的按钮，也不会看到"已确认 → 已确认"这种废话。
 */
export function FindingTransitionBar({
  finding,
  onTransition,
}: {
  finding: Finding
  onTransition: (to: FindingStatus) => void
}) {
  const targets = FINDING_STATUS_TRANSITIONS[finding.status]

  return (
    <div className="flex flex-wrap items-center gap-tight">
      <span className="mr-1 text-2xs text-ink-faint">标记为</span>

      {targets.map((target) => {
        const intent = FINDING_TRANSITION_INTENT[target]
        return (
          <button
            key={target}
            type="button"
            onClick={() => onTransition(target)}
            className={cn(
              'press inline-flex h-9 items-center gap-1.5 rounded-sm px-3 text-xs font-medium',
              'transition-ui duration-fast',
              // 主色 = 推进流程（确认/修复）；中性 = 退回；提示色 = 有后果的判断（误报）
              intent === 'primary' && 'bg-accent text-canvas hover:bg-accent-strong',
              intent === 'neutral' &&
                'border border-line text-ink-muted hover:border-line-strong hover:text-ink',
              intent === 'caution' &&
                'border border-signal-warn/40 text-signal-warn hover:bg-signal-warn/10',
            )}
          >
            {FINDING_TRANSITION_LABEL[target]}
          </button>
        )
      })}
    </div>
  )
}

// ─────────────────────────────────────────────
// 时间线
// ─────────────────────────────────────────────

/**
 * 时间线条目的图标与配色，按 `kind` 取。
 *
 * ## ⚠️ 这里曾经漏过一个 kind，而代价是**整个应用白屏**
 *
 * `assigned`（任务 10 P2 的指派）被后端写进了时间线，而这张表没跟上。
 * 后果不是"那个图标不显示"，而是：
 *
 *     const Icon = TIMELINE_ICON[entry.kind]   // undefined
 *     <Icon />                                  // React 抛错
 *
 * React 遇到无效组件会抛 "Element type is invalid"，
 * 而**没有任何错误边界**接住它 → 整棵组件树被卸载 → `#root` 变空。
 * 表现是"点开一条被指派过的漏洞，整个页面变成白屏"，
 * 而错误信息指向的是 `Timeline`，与"少了一个图标键"看起来毫无关系。
 *
 * ## 所以这里有两道防线
 *
 * 1. **补齐已知的 kind**。后端的 `_add_timeline` 一共就写这几种
 *    （见 `app/findings/service.py` 的调用点）。
 * 2. **未知 kind 一律降级成 `note` 的图标与配色**，而不是 `undefined`。
 *    第 2 条才是真正的保险：将来后端再加一种 kind，
 *    漏更新这张表的后果应该是"图标不太好认"，而不是"整个后台打不开"。
 *
 * 这两条都必要：只做 1 的话，下次加 kind 会重演同一个事故；
 * 只做 2 的话，常用条目会全部退化成一个通用图标，信息量丢掉。
 */
const UNKNOWN_TIMELINE_ICON = CircleDashed

const TIMELINE_ICON: Record<string, typeof CircleDashed> = {
  detected: Lightbulb,
  poc_generated: FlaskConical,
  verified: CheckCircle2,
  status_changed: ShieldCheck,
  // ★ 任务 10 P2 的指派。漏了它 = 打开被指派过的漏洞就白屏（见上）
  assigned: UserCheck,
  note: CircleDashed,
}

const TIMELINE_TONE: Record<string, string> = {
  detected: 'text-signal-warn',
  poc_generated: 'text-accent-strong',
  verified: 'text-signal-ok',
  status_changed: 'text-ink-muted',
  assigned: 'text-accent-strong',
  note: 'text-ink-faint',
}

/** 取图标 —— **永不返回 undefined**（见 TIMELINE_ICON 的说明） */
function timelineIcon(kind: string) {
  return TIMELINE_ICON[kind] ?? UNKNOWN_TIMELINE_ICON
}

/** 取配色。未知 kind 走中性色 —— 宁可素，不可崩 */
function timelineTone(kind: string): string {
  return TIMELINE_TONE[kind] ?? 'text-ink-muted'
}

/**
 * 状态变更 + 关键事件的时间线。
 *
 * 倒序（最新在上）：用户打开详情最想知道的是"最近一次是谁改的、改成了什么"，
 * 而不是"最早是谁发现的"。
 *
 * from → to 显式画出箭头，而不是只写"状态已变更" ——
 * 审计场景里"从什么改成什么"和"改成了什么"是两个不同的问题。
 */
function Timeline({ entries }: { entries: FindingTimelineEntry[] }) {
  if (entries.length === 0) {
    return <p className="text-xs text-ink-faint">暂无操作记录。</p>
  }

  return (
    <ol className="flex flex-col">
      {entries.map((entry, index) => {
        const Icon = timelineIcon(entry.kind)
        const isLast = index === entries.length - 1

        return (
          <li key={entry.id} className="flex gap-tight">
            {/* 左侧：图标 + 连接线 */}
            <div className="flex shrink-0 flex-col items-center">
              <Icon
                size={14}
                strokeWidth={1.75}
                className={cn('mt-0.5', timelineTone(entry.kind))}
                aria-hidden="true"
              />
              {!isLast && <span aria-hidden="true" className="mt-1 w-px flex-1 bg-line" />}
            </div>

            <div className={cn('min-w-0 flex-1', !isLast && 'pb-panel')}>
              <p className="text-xs leading-snug text-ink-muted">{entry.summary}</p>

              <div className="mono-ui mt-1 flex flex-wrap items-center gap-x-2 text-ink-faint">
                {/* 谁做的 —— 审计信息，必须有 */}
                <span className={entry.actor === 'user' ? 'text-accent-strong' : 'text-ink-muted'}>
                  {entry.actor === 'agent' ? '智能体' : '人工'}
                </span>
                <span aria-hidden="true">·</span>
                <time dateTime={entry.at} className="tabular">
                  {formatRelativeTime(entry.at)}
                </time>

                {/* 状态变更画出 from → to */}
                {entry.from && entry.to && (
                  <>
                    <span aria-hidden="true">·</span>
                    <span className="flex items-center gap-1">
                      <span>{STATUS_SHORT[entry.from]}</span>
                      <span aria-hidden="true">→</span>
                      <span className="text-ink-muted">{STATUS_SHORT[entry.to]}</span>
                    </span>
                  </>
                )}
              </div>
            </div>
          </li>
        )
      })}
    </ol>
  )
}

/** 时间线里的短状态名 —— 完整的「待确认」在紧凑的一行里太长 */
const STATUS_SHORT: Record<FindingStatus, string> = {
  new: '待确认',
  confirmed: '已确认',
  fixed: '已修复',
  retesting: '复测中',
  closed: '已关闭',
  false_positive: '误报',
}

// ─────────────────────────────────────────────
// 小组件
// ─────────────────────────────────────────────

function Section({
  title,
  hint,
  icon: Icon,
  children,
}: {
  title: string
  hint?: string
  icon?: typeof Sparkles
  children: React.ReactNode
}) {
  return (
    <section className="flex flex-col gap-tight">
      <div className="flex items-baseline gap-tight">
        {Icon && (
          <Icon size={12} strokeWidth={1.75} className="text-ink-ghost" aria-hidden="true" />
        )}
        <h4 className="text-xs font-medium text-ink">{title}</h4>
        {hint && <span className="text-2xs text-ink-faint">{hint}</span>}
      </div>
      {children}
    </section>
  )
}

/** PoC 文件名里的扩展名 —— 让代码块标题看起来像真的文件 */
function extensionFor(language: string): string {
  switch (language.toLowerCase()) {
    case 'python':
    case 'py':
      return 'py'
    case 'bash':
    case 'sh':
      return 'sh'
    case 'javascript':
    case 'js':
      return 'js'
    case 'html':
      return 'html'
    case 'http':
      return 'http'
    default:
      return 'txt'
  }
}

export { ShieldX }
export default FindingDetail
