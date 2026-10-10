import { cn } from '@/lib/cn'
import { FINDING_CATEGORY_LABEL, type Finding } from '@/data/finding'
import { formatRelativeTime } from '@/lib/datetime'
import { SeverityBadge, StatusBadge, VerificationBadge } from './FindingBadges'
import SlaBadge from './SlaBadge'
import { ChevronRight } from 'lucide-react'

/**
 * 漏洞列表。
 *
 * 列的选择依据「扫视时按什么排序」：
 *   严重度 → 漏洞类型 → 所在目标 → 状态 → 发现时间
 * 严重度放最左是因为它是**唯一决定"要不要现在处理"的维度**。
 *
 * 宽屏是表格（列对齐便于纵向比较），窄屏退化成卡片堆叠 ——
 * 七列在 390px 上不可能排下，硬排的结果是每格两个字。
 * 两种形态共用同一份数据，只是排版方式不同。
 */

export interface FindingsTableProps {
  findings: Finding[]
  selectedIds: Set<string>
  onToggleSelect: (id: string) => void
  onToggleSelectAll: () => void
  onOpen: (finding: Finding) => void
  /** 当前打开的那条（高亮显示） */
  activeId?: string | null
  emptyState: React.ReactNode
}

export function FindingsTable({
  findings,
  selectedIds,
  onToggleSelect,
  onToggleSelectAll,
  onOpen,
  activeId,
  emptyState,
}: FindingsTableProps) {
  if (findings.length === 0) {
    return <div className="px-panel py-bay">{emptyState}</div>
  }

  const allSelected = findings.length > 0 && findings.every((f) => selectedIds.has(f.id))
  const someSelected = findings.some((f) => selectedIds.has(f.id)) && !allSelected

  return (
    <div className="flex flex-col">
      {/* ── 表头（仅宽屏）── */}
      <div className="hidden items-center gap-panel border-b border-line px-panel py-2 lg:flex">
        <Checkbox
          checked={allSelected}
          indeterminate={someSelected}
          onChange={onToggleSelectAll}
          label={allSelected ? '取消全选' : '全选当前列表'}
        />

        <span className="w-16 shrink-0 text-2xs text-ink-faint">严重度</span>
        <span className="w-28 shrink-0 text-2xs text-ink-faint">类型</span>
        <span className="min-w-0 flex-1 text-2xs text-ink-faint">漏洞 / 目标</span>
        <span className="w-32 shrink-0 text-2xs text-ink-faint">状态</span>
        {/*
          负责人（任务 10 P2）。
          放在"状态"之后、"发现于"之前：它和状态一样是**处置属性**
          （"这条现在归谁管"），而不是漏洞本身的性质。
        */}
        <span className="w-24 shrink-0 text-2xs text-ink-faint">负责人</span>
        {/* 修复时限（任务 10 P3）—— 与负责人同类：都是"这条该谁管、急不急" */}
        <span className="w-24 shrink-0 text-2xs text-ink-faint">时限</span>
        <span className="w-20 shrink-0 text-right text-2xs text-ink-faint">发现于</span>
        <span className="w-4 shrink-0" aria-hidden="true" />
      </div>

      {/* ── 行 ── */}
      <ul aria-label="漏洞列表">
        {findings.map((finding) => (
          <FindingRow
            key={finding.id}
            finding={finding}
            selected={selectedIds.has(finding.id)}
            active={activeId === finding.id}
            onToggleSelect={() => onToggleSelect(finding.id)}
            onOpen={() => onOpen(finding)}
          />
        ))}
      </ul>
    </div>
  )
}

function FindingRow({
  finding,
  selected,
  active,
  onToggleSelect,
  onOpen,
}: {
  finding: Finding
  selected: boolean
  active: boolean
  onToggleSelect: () => void
  onOpen: () => void
}) {
  return (
    <li className="border-b border-line/60 last:border-b-0">
      <div
        className={cn(
          'group flex flex-col gap-tight px-panel py-3 transition-ui duration-fast lg:flex-row lg:items-center lg:gap-panel lg:py-2.5',
          // 选中态与打开态用不同视觉：选中是"待批量操作"，打开是"正在看这条"
          selected && 'bg-accent-wash/50',
          active && !selected && 'bg-surface-raised',
          !selected && !active && 'hover:bg-surface-raised/50',
        )}
      >
        {/* 选择框 */}
        <div className="flex items-center gap-tight">
          <Checkbox
            checked={selected}
            onChange={onToggleSelect}
            label={`选择「${finding.title}」`}
          />

          {/* 窄屏：严重度就地显示在选择框旁 */}
          <span className="lg:hidden">
            <SeverityBadge severity={finding.severity} />
          </span>
        </div>

        {/* 严重度（宽屏列） */}
        <span className="hidden w-16 shrink-0 lg:block">
          <SeverityBadge severity={finding.severity} />
        </span>

        {/* 类型 */}
        <span className="w-28 shrink-0 truncate text-2xs text-ink-muted">
          {FINDING_CATEGORY_LABEL[finding.category]}
        </span>

        {/* 主信息：标题 + 目标 */}
        <div className="min-w-0 flex-1">
          {/*
            标题做成按钮而不是整行可点：整行可点会和选择框打架 ——
            用户想勾选却打开了详情。明确的可点区域比隐式整行更可靠。
            但按钮撑满可用宽度，所以视觉上仍然像"点标题这一行"。
          */}
          <button
            type="button"
            onClick={onOpen}
            className="block w-full truncate py-1 text-left text-sm text-ink transition-ui duration-fast hover:text-accent-strong"
          >
            {finding.title}
          </button>

          <span className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs text-ink-faint">
            {/*
              目标主机比接口路径高一级。
              扫列表时先问的是"哪台机器出问题"，再问"哪个接口" ——
              两者同色会让这一行读起来是一团灰。
              三级层次：标题(ink) > 主机(ink-muted) > 接口/时间(ink-faint)。
            */}
            <span className="mono-ui truncate text-ink-muted">{finding.target}</span>
            {finding.detail.affected[0] && (
              <>
                <span aria-hidden="true">·</span>
                <span className="mono-ui truncate" title={finding.detail.affected.join('\n')}>
                  {finding.detail.affected[0]}
                </span>
              </>
            )}
          </span>
        </div>

        {/* 状态 + 验证程度 */}
        <span className="flex shrink-0 items-center gap-tight lg:w-32">
          <StatusBadge status={finding.status} />
          <VerificationBadge verification={finding.verification} />
        </span>

        {/*
          负责人（任务 10 P2）。

          ★ 未指派显示成灰色的「未指派」，而不是留空 ——
          空单元格读起来像"这一列坏了"或者"还没加载出来"，
          而"还没人认领"本身就是要传达的信息（它正是"未指派"筛选的依据）。

          窄屏也显示：谁负责在这两类屏幕上都一样重要，
          而它只占几个字。
        */}
        <span
          className="w-24 shrink-0 truncate text-2xs lg:w-24"
          title={finding.assigneeName ? `负责人：${finding.assigneeName}` : '还没有指派负责人'}
        >
          {finding.assigneeName ? (
            <span className="text-ink-muted">{finding.assigneeName}</span>
          ) : (
            <span className="text-ink-ghost">未指派</span>
          )}
        </span>

        {/*
          修复时限（任务 10 P3）。

          ★ 数据全部来自后端（`slaState` / `slaDaysLeft`），这里只负责画 ——
          前端自己拿 `dueAt` 和当前时间比的话，"临期"的判定窗口就得在
          前后端各写一份，而两处独立判断会在刷新前后给出不同答案。

          `state === 'none'` 时它渲染成 `null`（见 `SlaBadge` 的说明）：
          不计时不需要用户做任何事，而每行都挂一个"无期限"会把真正的
          红黄标记淹掉。
        */}
        <span className="flex shrink-0 lg:w-24">
          <SlaBadge
            state={finding.slaState}
            daysLeft={finding.slaDaysLeft}
            dueAt={finding.dueAt}
          />
        </span>

        {/* 发现时间 */}
        <time
          dateTime={finding.discoveredAt}
          className="tabular shrink-0 text-2xs text-ink-faint lg:w-20 lg:text-right"
        >
          {formatRelativeTime(finding.discoveredAt)}
        </time>

        {/* 展开指示 */}
        <ChevronRight
          size={14}
          strokeWidth={2}
          className="hidden shrink-0 text-ink-ghost transition-transform duration-fast group-hover:translate-x-0.5 lg:block"
          aria-hidden="true"
        />
      </div>
    </li>
  )
}

/**
 * 三态选择框：未选 / 已选 / 部分选。
 *
 * 原生 checkbox 有 indeterminate 属性但只能通过 JS 设，所以用 ref 同步。
 * 不用自定义图标模拟 —— 原生控件的键盘与读屏支持是免费的，
 * 自己画一遍很容易漏掉空格键切换和 aria-checked。
 */
function Checkbox({
  checked,
  indeterminate = false,
  onChange,
  label,
}: {
  checked: boolean
  indeterminate?: boolean
  onChange: () => void
  label: string
}) {
  return (
    /*
      用 <label> 包住原生 input 来扩大触摸目标：
        input 本身只有 16px，达不到触摸标准；直接给它加 padding
        在部分浏览器里不扩大可点区域，而 label 包裹则整块都能触发。
        `-m-2 p-2` 把视觉尺寸拉回原位，不撑开行高。
    */
    <label
      className="-m-2 flex shrink-0 cursor-pointer items-center justify-center p-2"
      title={label}
    >
      <input
        type="checkbox"
        checked={checked}
        ref={(el) => {
          // 半选态只能这样设 —— React 没有对应的 prop
          if (el) el.indeterminate = indeterminate
        }}
        onChange={onChange}
        aria-label={label}
        className="h-4 w-4 shrink-0 cursor-pointer accent-[#8B7CF6]"
      />
    </label>
  )
}

export default FindingsTable
