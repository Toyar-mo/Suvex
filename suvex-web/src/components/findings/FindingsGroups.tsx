import { cn } from '@/lib/cn'
import { FINDING_STATUS_LABEL, type FindingStatus } from '@/data/finding'
import type { FindingGroup } from '@/data/findings-store'
import { SeverityBadge } from '@/components/findings/FindingBadges'
import { ChevronRight, Layers, Target } from 'lucide-react'
import { useState } from 'react'

/**
 * 分组视图 —— 按漏洞类型折叠（任务 10 P4）。
 *
 * ## 它回答的问题
 *
 * 列表视图回答"有哪些漏洞"，分组视图回答"**哪一类问题影响面最大**"。
 * 同一个配置错误出现在 20 台机器上时，列表里是 20 行几乎一样的东西，
 * 而人真正要判断的是"先修这一类，还是先修那一个高危"。
 *
 * ## 为什么按标题而不是类别
 *
 * 见 `service.group_findings` 的说明：类别太粗（`misconfiguration` 下
 * 有几十种不同的东西），id 太细（每次扫描都重来）。**标题才是"人眼里
 * 这是同一个问题"的那个粒度** —— 与扫描对比模块同一个判据。
 *
 * ## 展开是"就地展开"而不是跳转
 *
 * 展开时列出这一组影响的目标与各自的处置状态，不跳去列表视图。
 * 跳转会让用户丢掉"我在看哪一组"的上下文，而分组视图的价值恰恰是
 * 横向比较各组。
 */
export interface FindingsGroupsProps {
  groups: FindingGroup[]
  /**
   * 按 id 取那一条的**当前状态与目标**。
   *
   * 返回整条而不是只返回状态：展开一组时用户要看的正是"哪个目标
   * 处理到哪一步了"—— 只给状态的话，那一列数字对应不上任何机器，
   * 而分组视图的全部意义就是"这一类问题影响了哪些目标"。
   */
  findingOf: (findingId: string) => { status: FindingStatus; target: string } | null
  /** 对一整组做批量操作 —— 这是分组视图最省事的地方 */
  onGroupAction: (group: FindingGroup, status: FindingStatus) => void
  /** 选中整组（把这一组的 id 交给页面的选择集） */
  onSelectGroup: (group: FindingGroup) => void
}

export function FindingsGroups({
  groups,
  findingOf,
  onGroupAction,
  onSelectGroup,
}: FindingsGroupsProps) {
  return (
    <ul aria-label="按漏洞类型分组">
      {groups.map((group) => (
        <GroupRow
          key={group.title}
          group={group}
          findingOf={findingOf}
          onGroupAction={onGroupAction}
          onSelectGroup={onSelectGroup}
        />
      ))}
    </ul>
  )
}

function GroupRow({
  group,
  findingOf,
  onGroupAction,
  onSelectGroup,
}: {
  group: FindingGroup
  findingOf: (findingId: string) => { status: FindingStatus; target: string } | null
  onGroupAction: (group: FindingGroup, status: FindingStatus) => void
  onSelectGroup: (group: FindingGroup) => void
}) {
  const [open, setOpen] = useState(false)

  // 全组状态一致时可以直接一键处置；否则只显示分布，让用户展开看。
  // 用 `findingOf` 而不是 group.byStatus —— 后者是**拉分组那一刻**的快照，
  // 而用户可能在列表视图里刚改过状态。以当前列表数据为准才不会显示过时的东西。
  const live = group.findingIds.map((id) => findingOf(id))
  const known = live.filter((item): item is { status: FindingStatus; target: string } => !!item)
  const uniformStatus =
    known.length > 0 && new Set(known.map((item) => item.status)).size === 1
      ? known[0].status
      : null
  const actionable = uniformStatus !== null && uniformStatus !== 'false_positive'

  return (
    <li className="border-b border-line/60 last:border-b-0">
      <div className="flex flex-col gap-tight px-panel py-3 lg:flex-row lg:items-center lg:gap-panel">
        {/* 折叠开关 */}
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex min-w-0 flex-1 items-center gap-tight text-left"
        >
          <ChevronRight
            size={14}
            strokeWidth={2}
            aria-hidden="true"
            className={cn(
              'shrink-0 text-ink-ghost transition-transform duration-fast',
              open && 'rotate-90',
            )}
          />
          <span className="min-w-0 flex-1 truncate text-sm text-ink">{group.title}</span>
        </button>

        {/* 影响面 —— 分组视图存在的理由，所以它是仅次于标题的信息 */}
        <span className="flex shrink-0 items-center gap-1 text-2xs text-ink-muted lg:w-32">
          <Target size={11} strokeWidth={2} aria-hidden="true" className="text-ink-ghost" />
          影响 {group.targetCount} 个目标
        </span>

        <span className="shrink-0 lg:w-20">
          <SeverityBadge severity={group.severity} />
        </span>

        {/*
          状态分布。**不做成单一徽标**（"3 条待确认"）—— 组内各条状态
          本来就可能不同，那是"各自独立处置"的直接体现。用紧凑的
          "待确认 2 · 已确认 1"反而更准，也让人看出这组还没处理完。
        */}
        <span className="flex shrink-0 flex-wrap items-center gap-1 lg:w-56">
          {Object.entries(group.byStatus).map(([status, count]) => (
            <span
              key={status}
              className="rounded-xs border border-line px-1 text-2xs text-ink-muted"
            >
              {FINDING_STATUS_LABEL[status as FindingStatus] ?? status} {count}
            </span>
          ))}
        </span>
        {/* 组级操作 —— 分组视图最省事的地方 */}
        <span className="flex shrink-0 items-center gap-1 lg:w-40 lg:justify-end">
          <button
            type="button"
            onClick={() => onSelectGroup(group)}
            className={cn(
              'inline-flex h-7 items-center gap-1 rounded-sm px-2 text-2xs',
              'border border-line text-ink-muted transition-ui duration-fast',
              'hover:border-accent/50 hover:bg-accent-wash hover:text-ink',
            )}
            title={`把这一组的 ${group.instanceCount} 条全部选中`}
          >
            <Layers size={11} strokeWidth={2} aria-hidden="true" />
            选中整组
          </button>
          {/*
            一键确认只在**全组状态一致**时给。
            否则它就变成了"把这一组里状态各异的条目统一刷成已确认"——
            那会覆盖掉别人已经做过的判断（比如某条已经确认过、另一条已修）。
            状态不一致时按钮不显示，让用户展开后逐条或选中整组再决定。
          */}
          {actionable && (
            <button
              type="button"
              onClick={() => onGroupAction(group, 'confirmed')}
              className={cn(
                'inline-flex h-7 items-center rounded-sm px-2 text-2xs',
                'border border-line text-ink-muted transition-ui duration-fast',
                'hover:border-accent/50 hover:bg-accent-wash hover:text-ink',
              )}
              title={`把这一组的 ${group.instanceCount} 条一起标为已确认`}
            >
              整组确认
            </button>
          )}
        </span>
      </div>

      {/* 展开：这一组各条落点与各自的处置状态 */}
      {open && (
        <div className="bg-surface-sunken px-panel pb-3 pt-1">
          {/*
            ★ 展开看的是**逐条的行**，不是"目标列表"。

            用户展开一组，想知道的正是"这 20 台机器分别处理到哪一步了"——
            只列目标名等于把最该看的信息（状态）藏起来。
            这也让"同一类问题在不同目标上的处置进度不一样"变得可见，
            而那正是分组视图与列表视图的分工：
            列表答"有哪些"，分组答"哪一类最该先动"。
          */}
          <ul className="flex flex-col gap-1">
            {group.findingIds.map((id) => {
              const item = findingOf(id)
              return (
                <li key={id} className="flex items-center gap-1.5 text-2xs text-ink-muted">
                  <span className="mono-ui min-w-0 flex-1 truncate">
                    {item?.target ?? '（这条不在当前列表里）'}
                  </span>
                  <span className="shrink-0 rounded-xs border border-line px-1">
                    {item ? (FINDING_STATUS_LABEL[item.status] ?? item.status) : '未知'}
                  </span>
                </li>
              )
            })}
          </ul>
          <p className="mt-1.5 text-2xs text-ink-faint">
            这一组共 {group.instanceCount} 条，分布在 {group.targetCount} 个目标上。
            它们的处置状态各自独立 —— 修好一个目标不会自动改掉其余的。
          </p>
        </div>
      )}
    </li>
  )
}

export default FindingsGroups
