/**
 * 技能包文件树的摊平逻辑（纯函数，无 React 依赖）。
 *
 * ## 为什么单独一个文件
 *
 * 它原本和 `SkillEditor` 放在一起，但那个文件导出的是组件，而
 * `react-refresh` 要求"只导出组件"才能热更新 —— 混进一个普通函数
 * 会让整个模块失去 fast refresh（改一行就整页重载，编辑器里填到
 * 一半的内容会没）。
 *
 * 搬到独立文件后两边都不用将就：组件文件只导出组件，纯函数可以
 * 被单测直接 import。
 */

/** 文件树里的一行。`path === null` 表示"正文"那一行。 */
export interface TreeRow {
  /** 完整相对路径；`SKILL.md` 用 `null` 表示（它不在 files 里） */
  path: string | null
  label: string
  depth: number
  isDir: boolean
  /** 目录下的**直属**文件数（仅 isDir 有意义） */
  count?: number
}

/**
 * 把「正文 + 附件路径」摊成一棵可渲染的扁平树。
 *
 * ★ 目录不单独存状态，每次从路径现算。
 *   若把展开/折叠后的目录也塞进 state，删光某目录下最后一个文件后
 *   那个目录还会留在树上 —— 一个点不开、也没内容的空壳。
 *
 * ★ 目录行显示的是**直属**文件数，不是递归总数。
 *   递归数会让「scripts/(1)」里明明只有一个可执行文件的目录显示成 12 ——
 *   用户点开后数不对，就会开始怀疑整个列表。
 */
export function buildTree(files: Record<string, string>): TreeRow[] {
  const bodyRow: TreeRow = { path: null, label: 'SKILL.md', depth: 0, isDir: false }

  const paths = Object.keys(files).sort((a, b) => a.localeCompare(b))

  const seen = new Set<string>()
  const rows: TreeRow[] = []

  for (const path of paths) {
    const parts = path.split('/')
    // 逐级登记祖先目录
    for (let i = 1; i < parts.length; i += 1) {
      const dirPath = parts.slice(0, i).join('/')
      if (!seen.has(dirPath)) {
        seen.add(dirPath)
        rows.push({
          path: dirPath,
          label: parts[i - 1],
          depth: i - 1,
          isDir: true,
          count: 0,
        })
      }
    }
    rows.push({
      path,
      label: parts[parts.length - 1],
      depth: parts.length - 1,
      isDir: false,
    })
  }

  // 回填每个目录下的**直属文件数**
  for (const row of rows) {
    if (!row.isDir || row.path === null) continue
    const prefix = row.path + '/'
    row.count = paths.filter((p) => {
      if (!p.startsWith(prefix)) return false
      // 只数直属：去掉前缀后不再含 `/`
      return !p.slice(prefix.length).includes('/')
    }).length
  }

  return [bodyRow, ...rows]
}
