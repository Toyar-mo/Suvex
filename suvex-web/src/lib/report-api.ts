import { apiText } from './api'
import type { ReportFormat } from '@/data/report'

/**
 * 报告导出 —— 内容由**后端**渲染，前端只负责交付。
 *
 * ## 任务 7 之后这里的分工
 *
 *   · **Markdown** → `GET /reports/{id}/download`
 *     后端按冻结的 `report_findings` 重建文档、渲染 Markdown、用报告编号
 *     命名附件（`Content-Disposition`）。前端连文件名都不拼 ——
 *     文件名里的编号是交付物身份的一部分，只该有一个来源。
 *
 *   · **PDF** → 仍然是**浏览器打印引擎**。
 *     后端 `GET /reports/{id}/download` 对 pdf 报告会如实回一句
 *     "PDF 由前端打印产出"（契约 §3.5.1 的决策），不产出文件流。
 *     所以这一支不请求下载，直接把 `kind: 'browser-print'` 交给调用方去
 *     `printReport()`。这不是"没做完"，是契约定下的本轮边界。
 *
 * ## 为什么把 PDF 的真实情况写进类型里
 *
 * `ReportArtifact` 是 discriminated union，两个分支的**下一步动作完全不同**：
 * 一个是"保存这个文件"，另一个是"打开打印对话框"。
 * 用一个可选字段（`blob?` / `print?`）就允许出现"既没内容也没有打印指令"
 * 的非法状态，调用方只能靠读实现猜怎么处理。
 *
 * 而 PDF 分支带 `serverReady: null` —— 它如实标注"服务端渲染还没接"。
 * 将来后端能出真 PDF 时，只需在这一支返回 `{ kind: 'file' }`，
 * 调用方按 kind 分发即可，不必推翻重写。
 */

// ─────────────────────────────────────────────
// 产物
// ─────────────────────────────────────────────

export type ReportArtifact =
  | {
      kind: 'file'
      filename: string
      mime: string
      /** 文本内容 */
      content: string
    }
  | {
      kind: 'browser-print'
      filename: string
      /** 服务端渲染 PDF 的就绪信息。当前为 null —— 如实标注未接入 */
      serverReady: null
    }

export interface GenerateReportInput {
  /** 已落库的报告 id。内容由后端按**冻结的快照**渲染，不接受前端传文档 */
  reportId: string
  /**
   * 报告编号，如 `RPT-20260924-CAP8`。
   *
   * 只在 PDF 分支用到（那一支没有响应头可读文件名）。
   * 编号是纯 ASCII，适合做文件名；标题是中文、可能带空格和斜杠，
   * 直接当文件名会出现编码差异甚至被截断（契约 §3.5）。
   */
  code: string
  format: ReportFormat
}

/**
 * 取得一份导出产物。
 *
 * 失败会真的抛错 —— 网络、鉴权、报告已被别人删掉都会走到这里。
 * 调用方**必须**有 catch 分支：`GET /reports/{id}/download` 会回
 * `404 NOT_FOUND`（报告被删），而"点了下载没反应"是最难排查的一类问题。
 */
export async function generateReportArtifact(input: GenerateReportInput): Promise<ReportArtifact> {
  const { reportId, code, format } = input

  if (format === 'pdf') {
    return { kind: 'browser-print', filename: `${code}.pdf`, serverReady: null }
  }

  const download = await apiText(`/reports/${reportId}/download`)

  return {
    kind: 'file',
    // 优先用后端给的文件名：它才是编号的权威来源。
    // 兜底那一条只是为了"后端没带 Content-Disposition"时不至于没名字。
    filename: download.filename ?? `${code}.md`,
    mime: download.mime,
    content: download.text,
  }
}
