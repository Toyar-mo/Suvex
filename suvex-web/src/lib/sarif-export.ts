/**
 * SARIF 导出的**前端侧**：把筛选器编译成 URL，把拿到的正文变成下载。
 *
 * ## 为什么单独一个文件，而不是塞进 `findings-store.ts`
 *
 * `findings-store.ts` 已经有 CSV 那一套（`findingsExportUrl`）。SARIF
 * 走的是**同一个筛选语义**，但产物是另一种格式 —— 两者的共同点只有
 * "URL 怎么拼"和"筛选器怎么编"，而那部分已经被 CSV 那边实现了。
 *
 * 这里**复用** `findingsExportUrl` 的逻辑而不是复制一份：复制出来的
 * 第二份迟早会漂移，而漂移的表现是「导 CSV 是 3 条、导 SARIF 是 200 条」——
 * 用户会把那份 SARIF 直接喂进 CI。所以本文件只做两件事：
 *   ① 在 CSV 那个 URL 后面换掉路径段（`/export` → `/export/sarif`）；
 *   ② 给浏览器一个能下载 `.sarif` 的函数。
 *
 * ⚠️ **不能让 `downloadCsv` 去下 SARIF。** 它把 Blob 的 MIME 写死成
 *    `text/csv`，并且是**同步** revoke —— 对 CSV 够用（内容小、单次），
 *    但对 SARIF 有两个后果：MIME 错了会让某些 CI 侧的"导入"按钮不亮；
 *    而且同步 revoke 在部分浏览器上会得到 0 字节文件（见 report-export.ts
 *    里 `downloadTextFile` 的说明，那里用的是 `setTimeout`）。
 */

import { findingsExportUrl, type FindingsExportQuery } from '@/data/findings-store'

/** SARIF 2.1.0 的正式 MIME。`application/json` 也能被认，但规范的这个更稳。 */
export const SARIF_MIME = 'application/sarif+json;charset=utf-8'

/** 前端兜底文件名。优先用后端 `Content-Disposition` 给的那个。 */
export function sarifFallbackFilename(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
  return `suvex-findings-${stamp}.sarif`
}

/**
 * 把界面筛选器编译成 `GET /findings/export/sarif` 的完整路径。
 *
 * 实现是「拿 CSV 那个 URL，把最后那段 `/export` 换成 `/export/sarif`」。
 * 看着绕，但这是**唯一能保证两边筛选项永远一致**的写法：
 * 将来给 CSV 加一个筛选维度（或改多选怎么串联），这里自动跟上，
 * 不需要有人记得"还有一处也要改"。
 *
 * 用 `replace(/\/export(?=$|\?)/, '/export/sarif')` 而不是
 * `replace('/export', ...)`：后者会在路径里出现两次 `export` 时
 * 换错位置。当前不会（前缀是 `/api/v1/findings`），但一个正则能
 * 让这个前提不用被记住。
 */
export function findingsSarifUrl(params: FindingsExportQuery): string {
  return findingsExportUrl(params).replace(/\/export(?=$|\?)/, '/export/sarif')
}

/**
 * 把后端回来的 SARIF 正文变成浏览器下载。
 *
 * 与 `downloadTextFile`（report-export.ts）同一套写法：Blob + ObjectURL +
 * `setTimeout` 延迟 revoke。理由那里写过了，这里不重复 —— 只说明为什么
 * **不能**直接调用它：它的签名是 `(filename, content, mime)`，语义上
 * 是"报告导出"，而本文件是"漏洞导出"；两处共用会让将来改其中一侧的
 * 清理时机时波及另一侧。真正相同的只有那 10 行，不值得为此多一层间接。
 */
export function downloadSarif(filename: string, content: string): void {
  const blob = new Blob([content], { type: SARIF_MIME })
  const url = URL.createObjectURL(blob)

  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.style.display = 'none'
  document.body.appendChild(anchor)
  anchor.click()

  document.body.removeChild(anchor)
  // 延迟释放：下载是异步启动的，立刻 revoke 会让部分浏览器拿到 0 字节。
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}
