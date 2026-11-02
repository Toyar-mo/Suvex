/**
 * 把助手正文里的 `/work/persist/<路径>` 变成可点下载（v1.36）。
 *
 * ## 为什么在前端做链接化
 *
 * 后端已经能把会话工作区（沙箱 `/work/persist`，宿主上一份）的文件流式
 * 下载出来（`GET /chat/sessions/{id}/files/download`）。但模型在回复里
 * 只会写"结果在 `/work/persist/report.md`"这种**纯文本**。用户复制一段
 * 路径去拼 URL 太别扭，所以这里把它识别成下载链接。
 *
 * ## 只认 `/work/persist/`
 *
 * 只给持久区做链接 —— `/work` 其它位置（`/work/out`、`/work/.browser`）
 * 的文件容器回收就没了、后端也取不到，链接化只会给一个 404。
 */

import { API_BASE_URL, API_PREFIX } from '@/lib/api'

/** 沙箱里持久工作区的挂载点。与后端 `workspaces.PERSIST_MOUNT_DIR` 同源。 */
export const PERSIST_PREFIX = '/work/persist/'

export type WorkLinkSegment =
  | { type: 'text'; value: string }
  | { type: 'link'; value: string; path: string }

/**
 * 匹配 `/work/persist/<路径>`。
 *
 * ★ 路径字符集刻意排除中文标点与空白/引号/括号：模型常写成
 *   "结果在 `/work/persist/report.md`。" —— 末尾的句号、反引号不属于路径。
 */
const PERSIST_RE = /\/work\/persist\/([^\s`'"<>()（）[\]【】《》，。；：、！？…]+)/g

/**
 * 下载 URL（带 `/api/v1` 前缀；同源部署时浏览器会带上登录 Cookie）。
 */
export function workspaceFileUrl(sessionId: string, path: string): string {
  const query = `?path=${encodeURIComponent(path)}`
  return `${API_BASE_URL}${API_PREFIX}/chat/sessions/${encodeURIComponent(sessionId)}/files/download${query}`
}

/**
 * 把一段文本切成"普通文字 / 下载链接"片段。没有命中就返回单个文字段。
 */
export function splitWorkLinks(text: string): WorkLinkSegment[] {
  if (!text) return []
  const segments: WorkLinkSegment[] = []
  let cursor = 0
  PERSIST_RE.lastIndex = 0
  let match: RegExpExecArray | null
  while ((match = PERSIST_RE.exec(text)) !== null) {
    const start = match.index
    const end = start + match[0].length
    if (start > cursor) {
      segments.push({ type: 'text', value: text.slice(cursor, start) })
    }
    segments.push({ type: 'link', value: match[0], path: match[1] })
    cursor = end
  }
  if (cursor < text.length) {
    segments.push({ type: 'text', value: text.slice(cursor) })
  }
  return segments.length > 0 ? segments : [{ type: 'text', value: text }]
}
