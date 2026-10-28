import { cn } from '@/lib/cn'
import {
  Archive,
  Bug,
  ChevronRight,
  Download,
  MoreHorizontal,
  Network,
  Pin,
  PinOff,
  RotateCcw,
  Trash2,
  Type,
} from 'lucide-react'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

/**
 * 会话条目的右键菜单。
 *
 * ## 为什么用 `position: fixed` + 手算坐标，而不是 `absolute` 挂在行里
 *
 * 侧栏是 `overflow-y-auto` 的滚动容器，`absolute` 定位的菜单会被它裁掉
 * （菜单比一行高得多，贴底部的会话一右键，菜单下半截直接看不见）。
 * `fixed` 相对视口定位，绕开所有祖先的裁剪 —— 代价是要自己算边界。
 *
 * ## 边界翻转（照抄 CyberStrikeAI `chat.js:10550-10625` 的做法）
 *
 * 右边缘放不下就把**子菜单翻到左边**（`right:100%`），下边缘放不下就把
 * 整个菜单上移。这不是"体验优化"而是"能不能用"：菜单 200px 宽，贴着
 * 右边缘时子菜单会整个跑到窗口外面，用户点不到「完整」。
 *
 * ## 关闭有三条路，缺一条都会让用户觉得"菜单甩不掉"
 *
 * 1. `pointerdown` 在菜单外（用 `contains` 判断）
 * 2. `Escape`
 * 3. 选中任意一项之后
 *
 * 用 `pointerdown` 而不是 `click`：`click` 要等抬起，而拖动侧栏滚动时
 * 抬起事件可能落在别的元素上，菜单就不会关。
 */

/** 菜单里一个可点的项。`submenu` 与 `onSelect` 二选一。 */
interface MenuItem {
  key: string
  label: string
  icon: React.ReactNode
  /** 危险操作（删除/彻底删除）用红色。 */
  danger?: boolean
  /** 有子菜单时点它只展开，不执行。 */
  submenu?: Array<{ key: string; label: string; onSelect: () => void }>
  onSelect?: () => void
}

export interface SessionContextMenuProps {
  /** 菜单锚点（右键位置，视口坐标）。`null` = 不显示。 */
  anchor: { x: number; y: number; sessionId: string } | null
  /** 被右键的那个会话是不是已归档（决定显示「恢复」还是「删除」）。 */
  archived: boolean
  pinned: boolean
  /** 可移动到的分组（项目名）。空数组时不显示子菜单项。 */
  projects: string[]
  currentProject: string | null
  onRename: () => void
  onTogglePin: () => void
  /** 只读的洞察视图：从这条对话的消息里还原步骤 / 汇总关联漏洞。 */
  onViewAttackChain: () => void
  onViewFindings: () => void
  onDownload: (detail: 'summary' | 'full') => void
  onMoveTo: (project: string | null) => void
  onBatchManage: () => void
  onArchive: () => void
  onUnarchive: () => void
  onPurge: () => void
  onClose: () => void
}

const MENU_WIDTH = 200
const SUBMENU_WIDTH = 176
/** 贴边留白。太小会让菜单压住窗口边框，看着像被裁掉了。 */
const EDGE = 8

export function SessionContextMenu(props: SessionContextMenuProps) {
  const { anchor, archived, pinned, projects, currentProject, onClose } = props
  const menuRef = useRef<HTMLDivElement>(null)
  const [openSubmenu, setOpenSubmenu] = useState<string | null>(null)
  /*
    ★ 悬停意图（hover intent）：离开父项后**不要立刻**收起子菜单。

      真实鼠标从父项移到右侧子菜单时要跨过 4px 的空隙，途中会先触发父项的
      `mouseleave`。立刻收起的话，用户看到的就是「鼠标还没放到子菜单上，它就
      没了」—— 这是菜单类控件最常见的坏味道。给 140ms 宽限，并在进入子菜单
      时撤销这个定时器，就能让斜向移动也进得去。
  */
  const closeTimerRef = useRef<number | null>(null)
  const cancelScheduledClose = () => {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current)
      closeTimerRef.current = null
    }
  }
  const scheduleSubmenuClose = () => {
    cancelScheduledClose()
    closeTimerRef.current = window.setTimeout(() => {
      closeTimerRef.current = null
      setOpenSubmenu(null)
    }, 140)
  }
  useEffect(() => cancelScheduledClose, [])
  /** 子菜单向左翻（右边缘放不下时）。 */
  const [submenuLeft, setSubmenuLeft] = useState(false)
  /** 算好的菜单坐标。首帧用锚点坐标，`useLayoutEffect` 里再纠正。 */
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null)

  // ── 坐标纠正：拿真实 `offsetHeight` 才能算出会不会溢出 ──
  useLayoutEffect(() => {
    if (!anchor || !menuRef.current) {
      setPosition(null)
      return
    }
    const height = menuRef.current.offsetHeight
    const viewportW = window.innerWidth
    const viewportH = window.innerHeight
    const left = Math.min(anchor.x, Math.max(EDGE, viewportW - MENU_WIDTH - SUBMENU_WIDTH - EDGE))
    const top = Math.min(anchor.y, Math.max(EDGE, viewportH - height - EDGE))
    setPosition({ left, top })
    setSubmenuLeft(left + MENU_WIDTH + SUBMENU_WIDTH > viewportW)
    // 换一个会话就把子菜单收起：否则它"记得"上一次展开的是哪一项，
    // 在新会话的菜单上会出现一个没人点过的子菜单。
    // 放在这个 effect 里而不是单独一个 —— 一条 useEffect 只做 setState
    // 会被 `react-hooks/set-state-in-effect` 判成级联渲染。
    setOpenSubmenu(null)
  }, [anchor])

  // ── 外部点击 / Esc 关闭 ──
  useEffect(() => {
    if (!anchor) return
    const onPointerDown = (event: PointerEvent) => {
      if (menuRef.current?.contains(event.target as Node)) return
      onClose()
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('pointerdown', onPointerDown, true)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [anchor, onClose])

  // 换一个会话就收起子菜单 —— 已并入上面的布局 effect（避免在 effect 里
  // 只做一次 setState 触发级联渲染的告警）。

  if (!anchor) return null

  const insightItems: MenuItem[] = [
    {
      key: 'chain',
      label: '查看攻击链',
      icon: <Network size={14} strokeWidth={2} />,
      onSelect: props.onViewAttackChain,
    },
    {
      key: 'vulns',
      label: '查看漏洞',
      icon: <Bug size={14} strokeWidth={2} />,
      onSelect: props.onViewFindings,
    },
  ]

  const items: MenuItem[] = archived
    ? [
        ...insightItems,
        { key: 'unarchive', label: '恢复此对话', icon: <RotateCcw size={14} strokeWidth={2} />, onSelect: props.onUnarchive },
        {
          key: 'download',
          label: '下载 Markdown',
          icon: <Download size={14} strokeWidth={2} />,
          submenu: [
            { key: 'md-summary', label: '精简', onSelect: () => props.onDownload('summary') },
            { key: 'md-full', label: '完整（含工具详情）', onSelect: () => props.onDownload('full') },
          ],
        },
        { key: 'purge', label: '彻底删除', icon: <Trash2 size={14} strokeWidth={2} />, danger: true, onSelect: props.onPurge },
      ]
    : [
        ...insightItems,
        {
          key: 'download',
          label: '下载 Markdown',
          icon: <Download size={14} strokeWidth={2} />,
          submenu: [
            { key: 'md-summary', label: '精简', onSelect: () => props.onDownload('summary') },
            { key: 'md-full', label: '完整（含工具详情）', onSelect: () => props.onDownload('full') },
          ],
        },
        { key: 'rename', label: '重命名', icon: <Type size={14} strokeWidth={2} />, onSelect: props.onRename },
        {
          key: 'pin',
          label: pinned ? '取消置顶' : '置顶此对话',
          icon: pinned ? <PinOff size={14} strokeWidth={2} /> : <Pin size={14} strokeWidth={2} />,
          onSelect: props.onTogglePin,
        },
        { key: 'batch', label: '批量管理', icon: <MoreHorizontal size={14} strokeWidth={2} />, onSelect: props.onBatchManage },
        {
          key: 'move',
          label: '移动到分组',
          icon: <Archive size={14} strokeWidth={2} />,
          /*
            ★ 分组就是 `project` 字段，没有独立的 project 表（见 ChatPage 的注释）。
              所以子菜单 = 已有分组 + 「无分组」。已有的项上打勾，让用户看得出
              自己现在在哪一组 —— 否则"移动到分组"这个动作没有反馈。
          */
          submenu: [
            ...projects.map((name) => ({
              key: `mv-${name}`,
              label: name === currentProject ? `✓ ${name}` : name,
              onSelect: () => props.onMoveTo(name),
            })),
            {
              key: 'mv-none',
              label: currentProject ? '无分组' : '✓ 无分组',
              onSelect: () => props.onMoveTo(null),
            },
          ],
        },
        { key: 'archive', label: '删除此对话', icon: <Trash2 size={14} strokeWidth={2} />, danger: true, onSelect: props.onArchive },
      ]

  return createPortal(
    <div
      ref={menuRef}
      role="menu"
      aria-label="对话操作"
      data-session-context-menu=""
      className="fixed z-50 flex min-w-[200px] flex-col gap-0.5 rounded-md border border-line bg-surface-raised p-1 shadow-xl"
      style={{ left: position?.left ?? anchor.x, top: position?.top ?? anchor.y }}
      /*
        ★ 右键菜单自己不该再触发浏览器的右键菜单（在菜单上再点右键会冒出
          系统菜单，把我们的盖住） —— 但也不要 `preventDefault` 掉左键。
      */
      onContextMenu={(event) => event.preventDefault()}
    >
      {items.map((item) => {
        const hasSubmenu = Boolean(item.submenu?.length)
        return (
          <div
            key={item.key}
            className="relative"
            /*
              ★ 用 hover 展开子菜单（不是 click）：菜单类控件里 click 展开会让
                "点一下没反应（只是展开了）"变得很困惑。键盘用户走的是
                item 本身的按钮（focus 也能开）。
            */
            onMouseEnter={() => {
              if (!hasSubmenu) return
              cancelScheduledClose()
              setOpenSubmenu(item.key)
            }}
            onMouseLeave={() => {
              if (!hasSubmenu) return
              scheduleSubmenuClose()
            }}
          >
            <button
              type="button"
              role="menuitem"
              aria-haspopup={hasSubmenu ? 'menu' : undefined}
              aria-expanded={hasSubmenu ? openSubmenu === item.key : undefined}
              data-menu-item={item.key}
              onClick={() => {
                if (hasSubmenu) {
                  setOpenSubmenu((current) => (current === item.key ? null : item.key))
                  return
                }
                item.onSelect?.()
                props.onClose()
              }}
              onFocus={() => hasSubmenu && setOpenSubmenu(item.key)}
              className={cn(
                'flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-xs',
                'transition-ui duration-fast',
                'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
                item.danger ? 'text-signal-risk hover:bg-signal-risk/10' : 'text-ink hover:bg-surface-sunken',
              )}
            >
              <span className="shrink-0 text-ink-faint">{item.icon}</span>
              <span className="flex-1 truncate">{item.label}</span>
              {hasSubmenu && <ChevronRight size={12} strokeWidth={2} className="shrink-0 text-ink-faint" />}
            </button>

            {hasSubmenu && openSubmenu === item.key && (
              /*
                ★ 过桥：父项与子菜单之间原本用 `margin` 让开 4px，那 4px 是
                  **死区** —— 鼠标斜向移过去时先离开父项（mouseleave 排队收起），
                  再进入子菜单；真实鼠标移动够快时，收起会先发生，用户看到的就是
                  「鼠标还没放上去它就没了」。改成用 `padding` 把这段距离变成
                  **可悬停的子树**，并在进入时撤销收起。
              */
              <div
                className={cn('absolute top-0 z-10', submenuLeft ? 'pr-1' : 'pl-1')}
                style={submenuLeft ? { right: '100%' } : { left: '100%' }}
                onMouseEnter={cancelScheduledClose}
              >
                <div
                  role="menu"
                  aria-label={item.label}
                  className="flex max-h-72 min-w-[176px] flex-col gap-0.5 overflow-y-auto rounded-md border border-line bg-surface-raised p-1 shadow-xl"
                >
                {item.submenu?.map((sub) => (
                  <button
                    key={sub.key}
                    type="button"
                    role="menuitem"
                    data-menu-item={sub.key}
                    onClick={() => {
                      sub.onSelect()
                      props.onClose()
                    }}
                    className={cn(
                      'flex w-full items-center rounded-sm px-2 py-1.5 text-left text-xs text-ink',
                      'transition-ui duration-fast hover:bg-surface-sunken',
                      'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
                    )}
                  >
                    <span className="truncate">{sub.label}</span>
                  </button>
                ))}
                </div>
              </div>
            )}
          </div>
        )
      })}
    </div>,
    document.body,
  )
}
