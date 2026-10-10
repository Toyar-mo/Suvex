/**
 * 触发浏览器下载。
 *
 * 用 Blob + ObjectURL 而不是 data: URL：
 *   data URL 在 Chromium 上对超过约 2 MB 的内容会静默失败（下载按钮没反应），
 *   而带 PoC 和原始证据的报告完全可能到这个量级。
 *
 * 两个必须做的清理，都不是洁癖：
 *   - `URL.revokeObjectURL`：不释放的话这份内容会一直占着内存直到页面关闭，
 *     连续生成几份报告就是几份的泄漏。
 *   - 移除 <a>：留在 DOM 里虽然看不见，但它带着 href，某些读屏器会念出来。
 * 用 `setTimeout(..., 0)` 而不是同步释放：下载是异步启动的，
 * 立刻 revoke 会让部分浏览器拿不到数据，得到 0 字节文件。
 */
export function downloadTextFile(filename: string, content: string, mime: string): void {
  const blob = new Blob([content], { type: mime })
  const url = URL.createObjectURL(blob)

  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.style.display = 'none'
  document.body.appendChild(anchor)
  anchor.click()

  document.body.removeChild(anchor)
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

/**
 * 打印。
 *
 * ★ 这是本阶段 PDF 导出的实现方式，边界如实写在这里：
 *   没有后端，因此调**浏览器自己的打印引擎**（对话框里选"另存为 PDF"），
 *   而不是假装生成了一个 PDF 文件。理由见 report-api.ts。
 *
 * 页面侧要靠 `@media print` 把界面上的一切（侧栏、状态条、按钮）
 * 藏掉，只留报告纸本身 —— 打印的是屏幕上那个 DOM，
 * 所以预览和 PDF 在**版式上也**天然一致。
 *
 * ⚠️ 打印完成/取消无法被可靠监听：`afterprint` 在部分浏览器上
 *    不触发，Chromium 也无法判断用户是打印还是取消。所以调用方不能
 *    靠它来收尾状态，只能"发起后即认为结束"（见 ReportsPage 的说明）。
 */
export function printReport(): void {
  window.print()
}
