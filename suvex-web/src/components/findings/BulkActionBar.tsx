import { cn } from '@/lib/cn'
import {
  FINDING_STATUS_LABEL,
  FINDING_STATUS_ORDER,
  FINDING_TRANSITION_LABEL,
  type Finding,
  type FindingStatus,
} from '@/data/finding'
import { ArchiveRestore, FileText, Trash2, UserCheck, X } from 'lucide-react'

/**
 * 批量操作栏。
 *
 * 出现时机：**选中至少一条才出现**。没有选中时它不该占位置 ——
 * 常驻的多选工具条会让用户以为"必须多选才能操作"。
 *
 * 一个刻意的处理：批量流转**不套用单条的流转规则**。
 *   单条时我知道它当前是什么状态，所以能精确列出合法目标；
 *   批量时选中的若干条状态各不相同，逐条算交集的结果往往只剩一两个选项，
 *   反而更难用。所以这里列出全部状态，由**数据层幂等跳过**已经处于
 *   目标状态的条目（见 findings-store 的 setFindingStatus），
 *   并在操作后告诉用户"实际改了几条、跳过几条"。
 *
 * 但有一件事必须做：**全部选中项都已是目标状态时，禁用该按钮**。
 * 让用户点一个必然什么都不做的按钮是最差的体验。
 */

export interface BulkActionBarProps {
  /** 选中的条目（不是 id，需要读它们的当前状态来判断可操作性） */
  selected: Finding[]
  onApply: (status: FindingStatus) => void
  /** 打开指派弹窗（任务 10 P2）。指派不走 `onApply` —— 它的目标是**人**，不是状态 */
  onAssign: () => void
  /**
   * 带着选中项去报告页（任务 10 P5）。
   *
   * ★ 它是**跳转**而不是就地生成：报告的编排（标题、选项、格式、
   * 预览）在报告页，那里已经有完整的一套。在这里再做一个"直接出报告"
   * 等于把同一件事做两遍，而两边的选项迟早会漂移。
   *
   * 用户的心智模型也是"去报告页出报告" —— 他要看一眼预览、
   * 可能改个标题再下载。
   */
  onExportReport: () => void
  /**
   * 移入回收站（任务 10 P6-a）。
   *
   * ⚠️ 与状态流转分开一个回调：它改变的是"这条记录还在不在默认视图里"，
   * 而不是"它的处置状态"。合成一个回调会让撤销逻辑无从区分两者。
   */
  onDelete: () => void
  /** 从回收站恢复（任务 10 P6-a） */
  onRestore: () => void
  /** 永久删除（任务 10 P6-a）。**不可逆**，调用方必须先确认 */
  onPurge: () => void
  /**
   * 当前是不是「回收站」视图（任务 10 P6-a）。
   *
   * ★ 它决定这一栏**整体换一套动作**：回收站里的条目已经"不在"了，
   * 给它显示"标记为已确认"是荒谬的 —— 那等于鼓励用户对一条已被删除的
   * 记录做处置。所以回收站模式下只留两个动作：恢复 / 永久删除。
   */
  inTrash?: boolean
  onClear: () => void
}

export function BulkActionBar({
  selected,
  onApply,
  onAssign,
  onExportReport,
  onDelete,
  onRestore,
  onPurge,
  inTrash = false,
  onClear,
}: BulkActionBarProps) {
  if (selected.length === 0) return null

  const count = selected.length
  // 每条目标是"是否全部已处于该状态" —— 全都在了就没必要点
  const allInStatus = (status: FindingStatus) => selected.every((f) => f.status === status)

  // 顺带统计待确认的条数：这是最需要处理的一批，值得提示
  const pendingCount = selected.filter((f) => f.status === 'new').length

  return (
    <div
      role="region"
      aria-label="批量操作"
      className={cn(
        'animate-rise-in',
        /*
          ★ 用 sticky 而不是 fixed。

          两个原因：
          1. **fixed 曾经在这里真的失效过。** 页面被旧版 PageTransition 的
             包裹层套着，那层留下了 `transform: matrix(...)`（恒等矩阵，看不出来），
             它会让 fixed 后代相对**包裹层**而非视口定位 —— 底部栏被放到
             整个页面内容的最底端，滚到那里才看得见。
             （任务 8 已把包裹层换成纯 CSS 透明度动画，不再产生 transform。
              但下面第 2 条与那个坑无关，它本身就是更稳的做法，所以不回调。）
          2. sticky 天然留在内容列里，**不需要按侧栏宽度算左偏移**。
             之前写的 `md:left-rail` 是错的：侧栏展开时是 13rem 而不是 3.5rem，
             底部栏会压住侧栏。

          sticky 的停靠逻辑正好是想要的：元素的自然位置在内容末尾，
          用户往上滚时它贴住视口底部；滚到最底则回到自然位置。
        */
        'sticky z-30',
        // 窄屏要停在底部导航之上；宽屏没有导航，直接贴底。
        // +1px 抵消亚像素舍入 —— 底栏是 fixed 定位，实测会差 1px 被判定为重叠。
        'bottom-[calc(3.5rem+1px)] md:bottom-1',
        'border-t border-line bg-surface-overlay px-gutter py-2.5 shadow-overlay',
      )}
    >
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-tight">
        <span className="tabular text-sm text-ink" data-numeric>
          已选 {count} 条
        </span>
        {pendingCount > 0 && (
          <span className="text-2xs text-signal-warn">其中 {pendingCount} 条待确认</span>
        )}

        <div className="flex-1" />

        {inTrash ? (
          /*
            ── 回收站模式（任务 10 P6-a）──

            ★ 只留两个动作，把状态流转/指派/出报告**全部藏起来**。

            理由不是"少放几个按钮"，而是那几个动作在回收站里**没有意义**：
            给一条已删除的记录标"已确认"，等于鼓励用户对不存在的东西做处置 ——
            而服务器也根本收不到它（列表默认排除已删除项，那些按钮背后
            的批量接口拿这些 id 会找不到记录）。
          */
          <>
            <button
              type="button"
              onClick={onRestore}
              className={cn(
                'press inline-flex h-9 items-center gap-1.5 rounded-sm px-3 text-xs font-medium',
                'transition-ui duration-fast',
                'border border-line text-ink-muted hover:border-accent/50 hover:bg-accent-wash hover:text-ink',
              )}
              title="把这些漏洞恢复到默认列表"
            >
              <ArchiveRestore size={13} strokeWidth={2} aria-hidden="true" />
              恢复
            </button>

            {/*
              ⚠️ 永久删除做成**危险色 + 二次确认**（确认在调用方做）：
              它不可逆，而且被报告引用时服务端会 409 拒绝。
            */}
            <button
              type="button"
              onClick={onPurge}
              className={cn(
                'press inline-flex h-9 items-center gap-1.5 rounded-sm px-3 text-xs font-medium',
                'transition-ui duration-fast',
                'border border-signal-risk/40 text-signal-risk hover:bg-signal-risk/10',
              )}
              title="永久删除，不可撤销。被报告引用的条目会被拒绝。"
            >
              <Trash2 size={13} strokeWidth={2} aria-hidden="true" />
              永久删除
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              onClick={onExportReport}
              className={cn(
                'press inline-flex h-9 items-center gap-1.5 rounded-sm px-3 text-xs font-medium',
                'transition-ui duration-fast',
                'border border-line text-ink-muted hover:border-accent/50 hover:bg-accent-wash hover:text-ink',
              )}
              title="带着选中的这些漏洞去报告页（在那边可以预览、改标题、下载）"
            >
              <FileText size={13} strokeWidth={2} aria-hidden="true" />
              生成报告
            </button>

            {/*
              指派（任务 10 P2）。

              ★ 放在状态按钮**之前**，因为它和它们不是一类东西：
              右边那五个是"这条漏洞现在是什么状态"，而指派是"它归谁管"。
              混在一起排会让人以为指派也是一种状态。

              ★ 它**永远可用**，不像状态按钮那样会"全部已是目标状态就禁用"：
              改派给另一个人、或者从张三改成李四，都是有效操作。
            */}
            <button
              type="button"
              onClick={onAssign}
              className={cn(
                'press inline-flex h-9 items-center gap-1.5 rounded-sm px-3 text-xs font-medium',
                'transition-ui duration-fast',
                'border border-line text-ink-muted hover:border-accent/50 hover:bg-accent-wash hover:text-ink',
              )}
            >
              <UserCheck size={13} strokeWidth={2} aria-hidden="true" />
              指派处理人
            </button>

            {/* 竖线：左边是"归谁管"，右边是"什么状态" —— 两类不是一回事 */}
            <span aria-hidden="true" className="mx-0.5 h-5 w-px bg-line" />

            {FINDING_STATUS_ORDER.map((status) => {
              const disabled = allInStatus(status)
              return (
                <button
                  key={status}
                  type="button"
                  onClick={() => onApply(status)}
                  disabled={disabled}
                  // 禁用的原因放在 title 里，而不是让用户猜
                  title={disabled ? `选中的条目都已是「${FINDING_STATUS_LABEL[status]}」` : undefined}
                  className={cn(
                    'press inline-flex h-9 items-center rounded-sm px-3 text-xs font-medium',
                    'transition-ui duration-fast',
                    disabled
                      ? // 禁用态靠「无边框 + 实底 + 禁止光标」表达，不靠把字压到看不清。
                        // 原来用 text-ink-ghost，实测 2.05:1 —— 虽然 WCAG 豁免
                        // 禁用控件，但用户连"这个按钮本来能干什么"都读不出来，
                        // 那就不是禁用而是消失了。
                        'cursor-not-allowed bg-surface-raised text-ink-faint'
                      : 'border border-line text-ink-muted hover:border-accent/50 hover:bg-accent-wash hover:text-ink',
                  )}
                >
                  {FINDING_TRANSITION_LABEL[status]}
                </button>
              )
            })}

            {/*
              移入回收站（任务 10 P6-a）。

              ★ 放在最后，且**不与状态按钮同色**：它是"把这条从视图里拿走"，
              与"它处于什么状态"是两件事。用中性的垃圾桶图标而不是红色 ——
              软删除是可撤销的，红色留给真正不可逆的"永久删除"。
            */}
            <button
              type="button"
              onClick={onDelete}
              className={cn(
                'press inline-flex h-9 items-center gap-1.5 rounded-sm px-3 text-xs font-medium',
                'transition-ui duration-fast',
                'border border-line text-ink-muted hover:border-signal-warn/50 hover:bg-signal-warn/10 hover:text-ink',
              )}
              title="移入回收站（可以再恢复）"
            >
              <Trash2 size={13} strokeWidth={2} aria-hidden="true" />
              删除
            </button>
          </>
        )}

        <button
          type="button"
          onClick={onClear}
          aria-label="清除选择"
          className="ml-1 inline-flex h-9 items-center gap-1 rounded-sm px-2 text-xs text-ink-faint transition-ui duration-fast hover:text-ink"
        >
          <X size={13} strokeWidth={2} aria-hidden="true" />
          取消选择
        </button>
      </div>
    </div>
  )
}

export default BulkActionBar
