import { useLocalStorage } from '@/hooks/useLocalStorage'

const STORAGE_KEY = 'suvex.nav.sidebarCollapsed'

/**
 * 侧栏折叠状态，记忆到 localStorage。
 *
 * 默认值不是简单按宽度切 —— 必须考虑**输入方式**：
 * 图标轨道（56px）只显示图标，含义靠悬停提示补足。而触摸设备没有悬停，
 * 提示永远不出现，用户只能靠猜图标。所以：
 *
 *   有鼠标 + 宽度 < 1024  → 折叠（笔记本/小屏，能悬停，省下的宽度给表格和日志）
 *   触摸设备              → 展开（无论多宽，因为提示不会出现）
 *   宽度 ≥ 1024           → 展开（有位置）
 *
 * 这只在**用户从未手动切换过**时生效。一旦存过值，宽度和输入方式都不再影响它，
 * 否则用户手动展开后一改窗口大小就被推翻，会很烦。
 */
export function useSidebarCollapsed() {
  const [collapsed, setCollapsed] = useLocalStorage(STORAGE_KEY, () => {
    if (typeof window === 'undefined') return false
    // 触摸优先：没有悬停就没有提示，展开是唯一安全的默认
    const coarsePointer = window.matchMedia('(pointer: coarse)').matches
    if (coarsePointer) return false
    return window.innerWidth < 1024
  })

  return { collapsed, setCollapsed, toggle: () => setCollapsed((prev) => !prev) }
}
