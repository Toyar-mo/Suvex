import type { ChatMessage } from '@/data/chat'

/**
 * 折叠态那行工具参数摘要：只在白名单键里找第一个非空字符串（v1.30）。
 *
 * ## ★ 为什么不再 `slice(0, 80)`
 *
 * 老实现把命令硬切 80 字符、**什么都不说**。用户看到的是一条断在中间的
 * 命令（`export KEY=… ; H=https://pro.dasctf.com; c`），第一反应是
 * 「命令写了一半 / 卡住了」，而去点「第 4 轮 · 正在执行 run_shell…」看
 * 到底哪里错了 —— 可其实后端数据完好、展开就能看全文。
 *
 * 现在两件事分开做：
 * 1. **视觉省略交给 CSS**（渲染处那个 `truncate` 会按容器宽度出省略号）；
 * 2. **"还有内容"这件事必须说出来** —— 命令超过一行或超过下面这个长度时，
 *    末尾附 `…（共 N 字符）`，`N` 是首行之外也算在内的总长度。
 *
 * 判据用「总长」而不是「首行超没超」：多行脚本的首行往往很短，但整段
 * 命令有几十行，那才是用户需要知道"点开看"的理由。
 *
 * ★ 单独放一个文件（而不是留在 ToolCard.tsx 里）：ToolCard 是组件文件，
 *   从组件文件里额外 `export` 纯函数会触发 `react-refresh/only-export-
 *   components` 警告（本仓库 lint 是 `--max-warnings 0`）。纯 helper 搬到
 *   这里既不破坏 Fast Refresh，也让它可以被单测直接 import。
 */
const DIGEST_INLINE_LIMIT = 80

export function digestToolArgs(args: ChatMessage['toolArgs']): string {
  if (!args) return ''
  for (const key of ['command', 'cmd', 'path', 'name', 'query', 'url', 'target']) {
    const value = args[key]
    if (typeof value === 'string' && value.trim()) {
      // 首行原文（把行内连续空白折成一个空格，避免制表符把行撑开）。
      const headline = value.trim().split('\n')[0].replace(/\s+/g, ' ').trim()
      const total = value.trim().length
      const multiline = value.trim().includes('\n')
      if (!multiline && total <= DIGEST_INLINE_LIMIT) return headline
      // `total` 是整段命令的字符数 —— 说「共 N 字符」比「共 N 行」更能
      // 让用户判断"值不值得点开"，也覆盖"一行但很长"这种情形。
      return `${headline}…（共 ${total} 字符）`
    }
  }
  return ''
}
