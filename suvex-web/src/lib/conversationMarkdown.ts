/**
 * 把一个会话导出成 Markdown 文本。
 *
 * ★ 为什么在前端拼而不做一个后端导出端点：
 *   拼字符串是纯函数，没有 IO、没有权限判断、没有分页 ——
 *   放在后端只是多一个必须与 `ChatMessageOut` 同步的 DTO。
 *   而"导出的内容长什么样"本来就属于展示层的决定。
 *
 * ★ 为什么不复用后端的 `_narration_before_action`：
 *   `ChatMessage.content` 落到前端时**已经**剥过动作协议了（见 data/chat.ts
 *   的注释），这里拿到的就是能直接给人看的正文。再剥一次会误伤正文里
 *   恰好出现的 `ACTION:` 字样。
 *
 * 版式参照 CyberStrikeAI 的 `formatConversationAsMarkdown`
 * （`web/static/js/chat.js:11425`）：顶部元信息 + `---` + 逐条消息，
 * 工具详情放在 `### 过程详情` 下，`完整` 版才附 JSON 参数。
 */

import type { ChatMessage, ChatSession, ChatSessionDetail } from '../data/chat'

/** 导出粒度。`完整` 附带每个工具调用的参数与输出。 */
export type MarkdownDetail = 'summary' | 'full'

/** 角色 → 中文标签。与后端 `CHAT_ROLES` 同源，未知就原样回显。 */
function roleLabel(role: ChatMessage['role']): string {
  if (role === 'user') return '用户'
  if (role === 'assistant') return '助手'
  if (role === 'tool') return '工具'
  return role
}

/**
 * 时间戳 → 本地可读时间。
 *
 * ★ 固定 `hour12: false`：导出文件会被贴进别的工具里，`下午 3:04` 那种
 *   写法在英文环境里会被当成两段文字。ISO 太啰嗦、原始时间戳没人读得懂，
 *   所以取中间：`YYYY-MM-DD HH:mm:ss`。
 */
function formatTime(value: string | null | undefined): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  )
}

/**
 * 文件名安全化。
 *
 * ★ 只替换真正会破坏路径的字符（Windows 与 POSIX 的并集），其余原样保留：
 *   把中文、空格之外的字符也一律洗掉，会让用户认不出自己导出的是哪个对话。
 *   与 CyberStrikeAI 的 `buildConversationMarkdownFileName`
 *   （`chat.js:10743`）同一套字符集。
 */
export function buildConversationMarkdownFileName(
  session: Pick<ChatSession, 'id' | 'title'>,
  detail: MarkdownDetail,
): string {
  const raw = (session.title || '').trim() || '对话'
  const safeTitle = raw
    .replace(/[\\/:*?"<>|]/g, '_')
    .replace(/\s+/g, '_')
    .slice(0, 60) || '对话'
  // 取 id 前 8 位：同一标题导出多个对话时靠它区分，又不至于把文件名撑长。
  const idPart = (session.id || '').slice(0, 8) || 'export'
  return `${safeTitle}_${idPart}_${detail === 'full' ? '完整' : '精简'}.md`
}

/** 一条工具消息的 `### 过程详情` 段。`完整` 版才附参数与输出正文。 */
function renderToolSection(message: ChatMessage, detail: MarkdownDetail): string {
  const lines: string[] = []
  const name = message.toolName || '(未知工具)'
  const status = message.toolFailed ? '失败' : '成功'
  lines.push(`**工具**：\`${name}\`（${status}）`)
  if (detail !== 'full') return lines.join('\n')

  if (message.toolArgs && Object.keys(message.toolArgs).length > 0) {
    lines.push('', '参数：', '```json', JSON.stringify(message.toolArgs, null, 2), '```')
  }
  if (message.content.trim()) {
    // 输出常常本身含 ``` 围栏（比如读到的 Markdown 文件），所以用四个反引号
    // 包住，避免提前闭合。
    lines.push('', '输出：', '````text', message.content.replace(/\s+$/, ''), '````')
  }
  return lines.join('\n')
}

/**
 * 主函数：会话（含消息）→ Markdown 文本。
 *
 * 消息按 `seq` 升序输出。**不**过滤任何角色 —— 用户要看的是"这轮到底
 * 发生了什么"，把工具消息去掉会让导出的记录读起来像模型在自言自语。
 */
export function buildConversationMarkdown(
  session: ChatSessionDetail,
  detail: MarkdownDetail = 'summary',
): string {
  const messages = [...(session.messages ?? [])].sort((a, b) => a.seq - b.seq)
  const out: string[] = []

  out.push(`# ${session.title || '未命名对话'}`)
  out.push('')
  out.push(`- 对话 ID：\`${session.id}\``)
  out.push(`- 创建时间：${formatTime(session.createdAt) || '—'}`)
  out.push(`- 更新时间：${formatTime(session.updatedAt) || '—'}`)
  out.push(`- 消息条数：${messages.length}`)
  out.push(`- 导出粒度：${detail === 'full' ? '完整（含工具参数与输出）' : '精简'}`)
  out.push('', '---', '')

  messages.forEach((message, index) => {
    const time = formatTime(message.createdAt)
    out.push(`## ${index + 1}. ${roleLabel(message.role)}${time ? ` (${time})` : ''}`)
    out.push('')

    if (message.role === 'tool') {
      out.push(renderToolSection(message, detail))
    } else {
      const body = message.content.trim()
      out.push(body || '_（这条消息没有正文）_')
      if (detail === 'full' && message.reasoning?.trim()) {
        out.push('', '<details><summary>思考过程</summary>', '', message.reasoning.trim(), '', '</details>')
      }
    }
    out.push('')
  })

  return `${out.join('\n').replace(/\n{3,}/g, '\n\n').trimEnd()}\n`
}

/**
 * 触发浏览器下载。
 *
 * ★ 用 `URL.createObjectURL` + 一次性 `<a>`，不依赖任何后端端点，
 *   也不需要 `Blob` 落盘到用户机器以外的地方。`revokeObjectURL` 必须
 *   在 click 之后调，但**不能**同步紧跟 click —— 部分浏览器会在同一次
 *   任务里还没开始读就把 URL 撤掉（下载变成 0 字节）。所以放 `setTimeout`。
 */
export function downloadMarkdownFile(filename: string, text: string): void {
  const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
