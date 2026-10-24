import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { CornerDownLeft, Search } from 'lucide-react'

import { cn } from '@/lib/cn'
import { useScans } from '@/data/scans-store'
import { loadFindings, useFindings } from '@/data/findings-store'
import { loadAssets, useAssetsState } from '@/data/assets-store'
import { loadSchedules, useSchedulesState } from '@/data/schedules-store'
import { SEVERITY_META, type Severity } from '@/data/findings'
import { SCAN_STATUS_LABEL, type ScanStatus } from '@/data/scan'
import {
  COMMAND_KIND_META,
  actionCommands,
  assetCommands,
  fallbackCommand,
  findingCommands,
  pageCommands,
  scanCommands,
  scheduleCommands,
  searchCommands,
  type CommandItem,
} from '@/data/command'

/**
 * 全局命令面板（Cmd/Ctrl+K）。
 *
 * ## 它解决什么问题
 *
 * 侧栏只有十来个入口，但用户真正想找的东西是**数据**：「上次扫 acme 那次」
 * 「那个 CORS 的漏洞」「哪些目标很久没动了」。这些都没有固定入口 ——
 * 要么在列表里翻，要么靠记路径。命令面板把「我知道我要什么，但不想找」
 * 变成一次按键。
 *
 * ## 三个刻意的取舍
 *
 * **1. 只搜已加载的数据；输入不触发请求**
 *    数据全部来自各 store 的快照。面板打开时对**还没加载过的** store
 *    补一次 silent 拉取，但**输入框每敲一个字都不发请求**。理由：打字
 *    有延迟会让人不再用它。代价是"库里有很多、但当前没加载"的条目不
 *    在结果里 —— 这一点由 `fallbackCommand` 明说并给出出口。
 *
 * **2. 键盘先行**
 *    ↑↓ 移动、Enter 执行、Esc 关闭。鼠标悬停**同步高亮**而不是另做样式，
 *    因为回车执行的是高亮项：鼠标停在第 3 条、高亮在第 1 条时按回车，
 *    执行的是第 1 条 —— 那是无声的错误执行。
 *
 * **3. 不做模糊匹配**
 *    子串前缀命中 > 子串包含 > 关键词包含，三档布尔事实（见 `command.ts`）。
 *    模糊匹配在中文上召回率不高，而误报代价高（打出 4 个字的精准词，
 *    第一位却是不相关的东西）。
 *
 * ## ★ 这里没有「effect 里同步 setState」
 *
 * 打开时清空输入、结果变化时把高亮拉回第一条 —— 这两件事最自然的写法
 * 都是 `useEffect(() => setQuery(''), [open])`，而项目有一条
 * `react-hooks/set-state-in-effect` 规则禁止它。那条规则挡的不是风格：
 * 那种写法会让 state 变成"需要手动保持一致的副本"，同步永远差一帧。
 *
 * 这里的做法与 `ScanRetentionPanel` 同源 —— **用 key 重置 + 派生**：
 *   · 输入框内容与高亮下标都存在**以打开次数为 key 的内部组件**里，
 *     面板一关就卸载、一开就是全新的 state（不需要清空）；
 *   · 高亮下标的越界用**派生裁切**兜住（`Math.min`），不在 effect 里纠正。
 */
export interface CommandPaletteProps {
  open: boolean
  onClose: () => void
}

/**
 * 外壳只做一件事：开的时候挂载内部组件，关的时候卸载。
 *
 * ★ 这就是「打开时清空输入」的实现方式 —— 靠**卸载重建**，不靠 effect 里
 *   `setQuery('')`（那会被 `react-hooks/set-state-in-effect` 拦下，而且
 *   那条规则是对的：effect 里同步 setState 会让 state 变成需要手动同步的副本）。
 *
 *   上一版试过"计数器 + key"和"ref 记 open 变化"，两者都得在**渲染期**
 *   碰 ref 或 setState —— lint 同样拦（`react-hooks/refs`）。这里最简单的
 *   写法恰好也是最难写错的：父组件不传 key，只决定挂不挂。
 */
export function CommandPalette({ open, onClose }: CommandPaletteProps) {
  if (!open) return null
  return <PaletteBody onClose={onClose} />
}


interface PaletteBodyProps {
  onClose: () => void
}

function PaletteBody({ onClose }: PaletteBodyProps) {
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const [rawActive, setRawActive] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const listId = useId()

  // ── 数据源：全部来自 store 快照 ──
  const scans = useScans()
  const findings = useFindings()
  const assetsState = useAssetsState()
  const schedulesState = useSchedulesState()

  /*
    挂载时补一次数据（面板打开 → PaletteBody 挂载 → 这里跑一次）。

    ★ 只对**还没加载过**的发（`items === null` 且 `idle`）。
    ★ 全部 silent：命令面板是"顺便搜一下"，某个接口 500 不该
      把它变成错误页 —— 用户真正去那一页时再看错误。
  */
  useEffect(() => {
    if (assetsState.items === null && assetsState.status === 'idle') {
      void loadAssets({ silent: true })
    }
    if (schedulesState.items === null && schedulesState.status === 'idle') {
      void loadSchedules({ silent: true })
    }
    void loadFindings({ silent: true })
    // 刻意只跑一次：这段的语义是"打开那一刻补什么"，
    // 把 store 状态放进依赖会让每次快照变化都重发一轮请求。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 打开后聚焦输入框：Cmd+K 之后直接打字是唯一符合直觉的动线
  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  // 滚动锁定（与 Drawer 同一套：记下原值再还回去）
  useEffect(() => {
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previousOverflow
    }
  }, [])

  const allItems = useMemo<CommandItem[]>(() => {
    const severityLabel = (s: string) => SEVERITY_META[s as Severity]?.label ?? s
    const statusLabel = (s: string) => SCAN_STATUS_LABEL[s as ScanStatus] ?? s
    return [
      ...actionCommands(),
      ...pageCommands(),
      ...scanCommands(scans, statusLabel),
      ...findingCommands(findings, severityLabel),
      ...assetCommands(assetsState.items ?? []),
      ...scheduleCommands(schedulesState.items ?? []),
    ]
  }, [scans, findings, assetsState.items, schedulesState.items])

  const results = useMemo(() => {
    const matched = searchCommands(allItems, query)
    /*
      兜底项只在「一条都没命中」且输入非空时出现。

      ★ 它不参与排序 —— 命中了说明用户要的东西就在列表里，
        再塞一条"去别处搜"只是噪音。
    */
    if (matched.length === 0) {
      const fallback = fallbackCommand(query)
      return fallback === null ? [] : [fallback]
    }
    return matched
  }, [allItems, query])

  /*
    ★ 高亮下标**派生裁切**，不存"纠正后"的值。

      不能用 effect 在结果变化时 setActiveIndex(0)（那是被禁止的
      同步 setState，而且会让列表在渲染后闪一下）。这里存的是用户
      最后一次移动到的下标，渲染时按当前结果数裁切：

      结果从 8 条缩到 3 条时，`rawActive=7` 被裁到 2 —— 恰好是"最后一条"，
      连续按 ↓ 再输入字符的动线是自然的（而不是突然跳回第 1 条）。
      输入框清空 → 用户在打字 → 下标也可能超界，同样被裁住。
  */
  const activeIndex = results.length === 0 ? 0 : Math.min(rawActive, results.length - 1)

  // 高亮项滚进视口（只在看不见时滚最少距离）
  useEffect(() => {
    const active = listRef.current?.querySelector<HTMLElement>('[data-active="true"]')
    active?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex])

  const run = useCallback(
    (item: CommandItem) => {
      onClose()
      navigate(item.to)
    },
    [navigate, onClose],
  )

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLInputElement>) => {
      if (event.key === 'ArrowDown') {
        event.preventDefault()
        setRawActive((index) => (results.length === 0 ? 0 : (Math.min(index, results.length - 1) + 1) % results.length))
        return
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault()
        setRawActive((index) => {
          if (results.length === 0) return 0
          const clamped = Math.min(index, results.length - 1)
          return (clamped - 1 + results.length) % results.length
        })
        return
      }
      if (event.key === 'Enter') {
        event.preventDefault()
        const picked = results[activeIndex]
        if (picked !== undefined) run(picked)
        return
      }
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
      }
    },
    [activeIndex, onClose, results, run],
  )

  const activeId = results[activeIndex] === undefined ? undefined : `${listId}-${activeIndex}`

  /*
    portal 挂到 body，与 Drawer 同一理由（见 Drawer.tsx 的长注释）：
    祖先上的任何 `transform` 都会让 `position: fixed` 的包含块变成那个祖先，
    于是整屏遮罩盖不住、面板位置错乱 —— 这类问题在截图上看不出来。

    命令面板比 Drawer 更必须这么做：它要从**任何页面**唤起，
    包括带入场动画的那些。
  */
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-start justify-center p-gutter pt-[12vh]">
      <button
        type="button"
        onClick={onClose}
        aria-label="关闭命令面板"
        className="absolute inset-0 cursor-default bg-canvas/70 backdrop-blur-[2px]"
      />
      {/*
        role="combobox" + aria-expanded + aria-controls 是 WAI-ARIA 里
        「带下拉列表的输入框」的标准组合。焦点**留在输入框**，
        用 aria-activedescendant 表达高亮项 —— 比把焦点移进列表简单，
        也不会出现"焦点跑进列表里、打字打不进去"。
      */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label="命令面板"
        className="relative flex w-full max-w-xl flex-col overflow-hidden rounded-md border border-line bg-surface-overlay shadow-overlay"
      >
        <div className="flex items-center gap-2 border-b border-line px-3">
          <Search className="size-4 shrink-0 text-ink-muted" aria-hidden="true" />
          <input
            ref={inputRef}
            type="text"
            role="combobox"
            aria-expanded={results.length > 0}
            aria-controls={listId}
            aria-activedescendant={activeId}
            aria-autocomplete="list"
            aria-label="搜索页面、扫描、漏洞、资产、定时计划"
            autoComplete="off"
            placeholder="搜索，或输入要执行的操作…"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value)
              // 输入变化时把高亮拉回第一条 —— 这是**用户事件**里的
              // setState，不是 effect 里，所以规则允许且语义正确：
              // 敲了新字符，之前选的第几条已经不再有意义。
              setRawActive(0)
            }}
            onKeyDown={onKeyDown}
            className="h-11 w-full bg-transparent text-sm text-ink outline-none placeholder:text-ink-muted"
          />
          <kbd className="shrink-0 rounded-sm border border-line px-1.5 py-0.5 text-[10px] text-ink-muted">
            Esc
          </kbd>
        </div>

        {results.length === 0 ? (
          <p className="px-3 py-6 text-center text-xs text-ink-muted">
            {query.trim().length === 0 ? '输入关键字开始搜索' : '没有匹配的页面或记录'}
          </p>
        ) : (
          <div
            ref={listRef}
            id={listId}
            role="listbox"
            aria-label="搜索结果"
            className="max-h-[52vh] overflow-y-auto p-1"
          >
            {results.map((item, index) => {
              const active = index === activeIndex
              return (
                /*
                  ★ 用 `div role="option"` 而不是 `button`：
                    combobox 的选项在 ARIA 里本来就不是按钮，
                    而且真按钮会进 Tab 序列 —— 焦点一进去，
                    键盘用户就再也打字不进输入框了。
                    键盘可达性由输入框上的 ↑↓/Enter 承担，
                    鼠标可达性由 onClick 承担，两者等价。
                */
                <div
                  key={item.id}
                  id={`${listId}-${index}`}
                  role="option"
                  aria-selected={active}
                  data-active={active}
                  onClick={() => run(item)}
                  onMouseMove={() => setRawActive(index)}
                  className={cn(
                    'flex cursor-pointer items-center gap-2.5 rounded-sm px-2.5 py-2',
                    active ? 'bg-accent/15' : 'hover:bg-surface',
                  )}
                >
                  <span className="w-12 shrink-0 text-[10px] uppercase tracking-wide text-ink-muted">
                    {COMMAND_KIND_META[item.kind].label}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm text-ink">{item.title}</span>
                    <span className="block truncate text-[11px] text-ink-muted">
                      {item.subtitle}
                    </span>
                  </span>
                  {active && (
                    <CornerDownLeft className="size-3.5 shrink-0 text-ink-muted" aria-hidden="true" />
                  )}
                </div>
              )
            })}
          </div>
        )}

        <div className="flex items-center gap-3 border-t border-line px-3 py-1.5 text-[10px] text-ink-muted">
          <span>↑↓ 选择</span>
          <span>↵ 执行</span>
          <span>Esc 关闭</span>
        </div>
      </div>
    </div>,
    document.body,
  )
}

export default CommandPalette
