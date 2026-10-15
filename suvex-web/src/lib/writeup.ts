import { errorMessage } from '@/lib/api'

/*
  ─────────────────────────────────────────────────────────────
  题解的**非组件**辅助（2026-10-14）

  ## 为什么单独一个文件

  `react-refresh/only-export-components` 要求：一个文件如果导出组件，
  就**只导出组件** —— 混着导出普通函数会让 fast refresh 失效
  （改一次代码整页刷新，而不是热替换）。

  项目的既有做法就是"把普通函数挪出组件文件"（见 `data/scan.ts` 里
  `isApplicable` 的注释：它专门说明了为什么放在数据层而不是组件里）。
  这里照做 —— 两个入口（扫描 / CTF2）都要用这两个函数，
  放 `lib/` 比放在任一个组件文件里都合理。
  ─────────────────────────────────────────────────────────────
*/

/**
 * 触发一次浏览器下载。
 *
 * 与 `lib/scan-export.ts` 的 `downloadCsv` 同一套做法（Blob + 临时 `<a download>`），
 * 不新增依赖。`scan-export` 那份是扫描列表的 CSV 导出，职责不同，所以没有合并。
 *
 * ⚠️ **不带 BOM**（CSV 那份必须带，因为 Excel 会用本地代码页解 UTF-8）。
 *    而 .md 是给 Markdown 编辑器/渲染器读的，它们默认按 UTF-8 解析，
 *    带上 BOM 反而会在文件头留一个不可见字符 —— 某些解析器会把它当成
 *    正文第一行的内容，于是"# 标题"不再是标题。
 */
export function downloadMarkdown(filename: string, markdown: string): void {
  const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  // 立刻释放：不释放的话每次下载都会漏一份 blob 在内存里
  URL.revokeObjectURL(url)
}

/**
 * 把任意异常收敛成弹窗能显示的一句话。
 *
 * 两个入口（扫描 / CTF2）用**同一套**兜底：`errorMessage` 已经处理了
 * `ApiError` 与未知异常两种形状，这里只是给它一个默认值。
 *
 * ⚠️ 默认值刻意**不是**"加载失败" —— 那个说法会让用户以为系统坏了，
 *    而这三种情况（题目没解出 / 不是夺旗任务 / 报告被清理）都是正常结果。
 *    与外壳里失败态用 `signal-warn` 而不是 `signal-risk` 是同一条判断。
 */
export function writeupErrorMessage(err: unknown): string {
  return errorMessage(err) || '题解暂时不可用，请稍后重试。'
}
