import { cn } from '@/lib/cn'
import type { ScanView } from '@/data/scans-store'
import { Archive, ArchiveRestore, Download, GitCompare, RotateCcw, Trash2, Undo2, X } from 'lucide-react'

/**
 * 批量操作栏（任务 9）。选中 ≥1 行时出现在状态 tab 下方。
 *
 * ## 为什么是 sticky 而不是固定顶部
 *
 * `sticky top-0` 让它跟着列表滚动保持可见，同时**不脱离文档流** ——
 * 固定定位（`fixed`）需要给列表留出等高 padding，而那个高度会随
 * 窄屏折行变化：留少了会盖住最后一行，留多了会有一段空白。
 * sticky 让浏览器自己算这件事。
 *
 * ## 按钮组按视图切换（不要"一套按钮 + 到处禁用"）
 *
 * 三个视图下能做的事**根本不同**：
 *
 * | 视图 | 能做什么 |
 * | --- | --- |
 * | 默认 | 归档、重新扫描、导出、删除 |
 * | 已归档 | 取消归档、重新扫描、导出、删除 |
 * | 回收站 | 恢复、永久删除 |
 *
 * 用一套按钮再逐个 `disabled` 会让工具栏里一半是灰的，而用户要读
 * tooltip 才知道为什么。**只显示这个视图下真的能做的事**，
 * 按钮少一半、每个都可点。
 *
 * ## 为什么"取消选择"在最右且是图标按钮
 *
 * 它是这一栏里**唯一无后果**的动作。放进主按钮组会与"归档/删除"
 * 混在一起，而误点的代价不对称：点错"取消选择"只是重来一次，
 * 点错"永久删除"数据就没了。用位置和样式把它分出去。
 */

export interface BulkActionBarProps {
  selectedCount: number
  view: ScanView
  busy?: boolean
  /**
   * 能否对比。**只有恰好选中 2 条时**为真 ——
   * 对比的语义是两条记录之间的差异，一条没得比、三条没有定义
   * （是两两比还是并集？界面上说不清）。
   */
  canCompare?: boolean
  onCompare?: () => void
  onArchive: () => void
  onUnarchive: () => void
  onRerun: () => void
  onExport: () => void
  onDelete: () => void
  onRestore: () => void
  onPurge: () => void
  onClear: () => void
}

export function BulkActionBar({
  selectedCount,
  view,
  busy = false,
  canCompare = false,
  onCompare,
  onArchive,
  onUnarchive,
  onRerun,
  onExport,
  onDelete,
  onRestore,
  onPurge,
  onClear,
}: BulkActionBarProps) {
  if (selectedCount === 0) return null

  return (
    <div
      className={cn(
        'sticky top-0 z-30 -mx-gutter mb-panel border-y border-line',
        'bg-surface-overlay/95 px-gutter py-2 backdrop-blur-sm',
      )}
    >
      <div className="flex flex-wrap items-center gap-tight">
        <span className="text-xs text-ink">
          已选 <span className="tabular font-medium text-accent-strong">{selectedCount}</span> 项
        </span>
        {selectedCount === 2 && <span className="text-2xs text-ink-faint">可对比这两次扫描</span>}

        <span aria-hidden="true" className="mx-1 h-4 w-px bg-line" />

        {/*
          对比只在恰好选中 2 条时出现。
          常驻 + 禁用会让用户去猜"为什么点不了"，而它出现的条件
          （选两条）本身就是提示 —— 并且下方还写明了原因。
        */}
        {canCompare && onCompare && (
          <>
            <BulkButton icon={GitCompare} label="对比" onClick={onCompare} disabled={busy} />
            <span aria-hidden="true" className="mx-1 h-4 w-px bg-line" />
          </>
        )}

        {view === 'default' && (
          <>
            <BulkButton icon={Archive} label="归档" onClick={onArchive} disabled={busy} />
            <BulkButton icon={RotateCcw} label="重新扫描" onClick={onRerun} disabled={busy} />
            <BulkButton icon={Download} label="导出" onClick={onExport} disabled={busy} />
            <BulkButton icon={Trash2} label="删除" onClick={onDelete} disabled={busy} danger />
          </>
        )}

        {view === 'archived' && (
          <>
            <BulkButton
              icon={ArchiveRestore}
              label="取消归档"
              onClick={onUnarchive}
              disabled={busy}
            />
            <BulkButton icon={RotateCcw} label="重新扫描" onClick={onRerun} disabled={busy} />
            <BulkButton icon={Download} label="导出" onClick={onExport} disabled={busy} />
            <BulkButton icon={Trash2} label="删除" onClick={onDelete} disabled={busy} danger />
          </>
        )}

        {view === 'deleted' && (
          <>
            <BulkButton icon={Undo2} label="恢复" onClick={onRestore} disabled={busy} />
            {/*
              ★ 永久删除用实心危险色，其余批量按钮都是描边。
              它是整页唯一**不可逆**的动作，视觉权重必须不同 ——
              与"删除（进回收站，可恢复）"长得一样是不可接受的。
            */}
            <BulkButton icon={Trash2} label="永久删除" onClick={onPurge} disabled={busy} solidDanger />
          </>
        )}

        <button
          type="button"
          onClick={onClear}
          className={cn(
            'ml-auto inline-flex h-7 items-center gap-1 rounded-sm px-2 text-2xs',
            'text-ink-faint transition-ui duration-fast hover:bg-surface-raised hover:text-ink',
            'focus:outline-none focus:ring-2 focus:ring-accent/40',
          )}
        >
          <X size={12} strokeWidth={2} aria-hidden="true" />
          取消选择
        </button>
      </div>
    </div>
  )
}

function BulkButton({
  icon: Icon,
  label,
  onClick,
  disabled,
  danger,
  solidDanger,
}: {
  icon: typeof Archive
  label: string
  onClick: () => void
  disabled?: boolean
  /** 描边危险色：可恢复的破坏性动作（删除进回收站） */
  danger?: boolean
  /** 实心危险色：**不可逆**的动作（永久删除） */
  solidDanger?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        'inline-flex h-7 items-center gap-1.5 rounded-sm border px-2.5 text-2xs',
        'transition-ui duration-fast focus:outline-none focus:ring-2 focus:ring-accent/40',
        disabled && 'cursor-not-allowed border-line/50 text-ink-ghost',
        !disabled && solidDanger && 'border-signal-risk bg-signal-risk/90 text-canvas hover:bg-signal-risk',
        !disabled &&
          !solidDanger &&
          danger &&
          'border-line text-ink-muted hover:border-signal-risk/40 hover:bg-signal-risk/10 hover:text-signal-risk',
        !disabled &&
          !solidDanger &&
          !danger &&
          'border-line text-ink-muted hover:border-line-strong hover:bg-surface-raised hover:text-ink',
      )}
    >
      <Icon size={12} strokeWidth={2} aria-hidden="true" />
      {label}
    </button>
  )
}

export default BulkActionBar
