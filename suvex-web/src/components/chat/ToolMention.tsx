import { cn } from '@/lib/cn'
import type { ChatTool } from '@/data/chat'
import { useMemo } from 'react'

/**
 * 「@工具」补全弹层（v0.94，第 4 项）。
 *
 * ## 为什么要单独一个组件，而不是塞进 ChatPage
 *
 * 它的全部状态（要不要显示、过滤出哪几条、高亮第几个）都由**输入框里的
 * 光标前那一段**决定。把这些塞进页面，就要在 `onChange` 里写二十行
 * 解析逻辑，而那一区已经很长了。抽出来之后，判断"现在该不该弹出"
 * 变成一个纯函数（`parseMentionQuery`），可以单独测。
 *
 * ## 判据：`@` 前面必须是空白或行首
 *
 * 不这么卡的话，邮箱 `a@b.com`、URL 里的 `@`、代码片段 `user@host`
 * 都会触发补全 —— 用户在正文里写一个邮件地址就弹出一个工具列表。
 *
 * ## 为什么不把 `@name` 从正文里删掉
 *
 * 用户敲进去的字原样发出去。`@run_shell` 留在正文里对模型是**额外**
 * 线索（和系统提示词里那段"用户点名了工具"互为印证），删掉反而让
 * 用户看到自己打的字消失在空气里。
 */

/** 光标前那一小段里，正在输入的 `@xxx` 部分。`null` = 不该弹层。 */
export interface MentionQuery {
  /** `@` 的位置（正文索引）。用于替换。 */
  start: number
  /** `@` 之后已经敲的字符。 */
  query: string
}

/**
 * 从正文里读出"光标前正在输入的 @ 提及"。
 *
 * @param text 正文
 * @param caret 光标位置（`selectionStart`）
 */
export function parseMentionQuery(text: string, caret: number): MentionQuery | null {
  // 只看光标之前。光标之后的内容不参与 —— 用户可能把光标移到中间补一个字，
  // 那时不该按"结尾的 @"去弹层。
  const head = text.slice(0, caret)
  const at = head.lastIndexOf('@')
  if (at < 0) return null

  // `@` 前面必须是行首或空白（含换行）。
  if (at > 0) {
    const before = head[at - 1]
    if (before !== ' ' && before !== '\n' && before !== '\t') return null
  }

  const query = head.slice(at + 1)
  // 提及里不允许空白：敲了空格就是"这一句写完了"，不再补全。
  // 也挡住 `@a b@c` 这种一行两个 @ 的歧义。
  if (/[\s]/.test(query)) return null
  // 长度上限：工具名最长 20 字上下；超过就没有匹配的可能了。
  if (query.length > 24) return null

  return { start: at, query }
}

/**
 * 从正文里解析出**已确认的点名工具**。
 *
 * 与 `parseMentionQuery` 的分工：那个管"正在输入"，这个管"发出去时
 * 带上哪些"。判据是「`@` + 一个真实存在的工具名 + 后面不再是工具名
 * 的合法字符」—— 即整词匹配，`@run_shell_x` 不算命中 `run_shell`。
 *
 * ★ 只认 `available === true` 的工具：点一个当前身份没权限的工具，
 *   在后端会被拦下来，前端还把它写进提示词，模型会以为自己能用。
 */
export function resolveMentions(text: string, tools: ChatTool[]): string[] {
  const usable = tools.filter((tool) => tool.available).map((tool) => tool.name)
  if (usable.length === 0) return []
  // 长的优先：`@list_scans` 与 `@list_scan` 同时存在时，先匹配长的，
  // 否则短的会把长的截成两半（`@list_scan` 命中后剩下 `s`）。
  const ordered = [...usable].sort((a, b) => b.length - a.length)

  // 先收集"命中位置 + 名字"，最后按**在正文里出现的先后**排序去重。
  //
  // ★ 不用"按名字长度顺序"输出：那段提示词是给模型看的，
  //   `@create_scan 然后 @list_scans` 与 `@list_scans 然后 @create_scan`
  //   如果都输出成同一个顺序，模型就丢了用户的先后意图。留原序没有成本。
  //
  // ★ `at` 必须是**全文偏移**，不能是行内偏移：行内偏移在多行正文里会
  //   撞车（第 1 行的 offset 3 与第 2 行的 offset 3 是两处不同的地方），
  //   排序与去重都会算错。
  const lines = text.split('\n')
  const hits: Array<{ at: number; name: string }> = []
  const taken = new Set<number>()
  let lineStart = 0

  for (const line of lines) {
    for (const name of ordered) {
      const needle = `@${name}`
      let from = 0
      while (from <= line.length) {
        const idx = line.indexOf(needle, from)
        if (idx < 0) break
        const at = lineStart + idx
        const before = idx === 0 ? '' : line[idx - 1]
        const after = line.slice(idx + needle.length, idx + needle.length + 1)
        const startsOk = before === '' || /\s/.test(before)
        // 后面必须是词边界：空白、行尾、或标点。不能是 `_`/字母/数字。
        const endsOk = after === '' || !/[A-Za-z0-9_]/.test(after)
        // `taken` 挡住长名字内部被短名字重复匹配的情况。
        if (startsOk && endsOk && !taken.has(at)) {
          taken.add(at)
          hits.push({ at, name })
        }
        from = idx + needle.length
      }
    }
    lineStart += line.length + 1 // +1 补回被 split 吃掉的换行
  }

  hits.sort((a, b) => a.at - b.at)
  const out: string[] = []
  for (const hit of hits) if (!out.includes(hit.name)) out.push(hit.name)
  return out
}

interface ToolMentionProps {
  tools: ChatTool[]
  query: MentionQuery
  onPick: (name: string) => void
  onClose: () => void
}

/** 补全弹层本体。没有匹配项时渲染一行"没有匹配的工具"，而不是什么都不显示。 */
export function ToolMention({ tools, query, onPick }: ToolMentionProps) {
  const matches = useMemo(() => {
    const lower = query.query.toLowerCase()
    return tools
      .filter((tool) => tool.available)
      .filter((tool) => (lower === '' ? true : tool.name.toLowerCase().includes(lower)))
      .slice(0, 8)
  }, [tools, query.query])

  if (matches.length === 0) {
    return (
      <div
        role="listbox"
        aria-label="可用工具"
        className="mx-2 mb-1 rounded-sm border border-line bg-surface px-2.5 py-2 text-xs text-ink-faint"
      >
        没有匹配的工具
      </div>
    )
  }

  return (
    <div
      role="listbox"
      aria-label="可用工具"
      className="mx-2 mb-1 max-h-56 overflow-y-auto rounded-sm border border-line bg-surface shadow-pop"
    >
      {matches.map((tool) => (
        <button
          key={tool.name}
          type="button"
          role="option"
          aria-selected={false}
          // ★ `onMouseDown` 而不是 `onClick`：`onClick` 之前输入框会先 blur，
          //   而 blur 时页面可能已经把 draft 清掉（比如触发了发送）。
          //   在 mousedown 阶段取值并阻止默认行为，光标留在输入框里。
          onMouseDown={(e) => {
            e.preventDefault()
            onPick(tool.name)
          }}
          className={cn(
            'flex w-full flex-col items-start gap-0.5 px-2.5 py-1.5 text-left',
            'transition-ui duration-fast hover:bg-surface-raised focus:outline-none focus-visible:bg-surface-raised',
          )}
        >
          <span className="flex items-center gap-1.5 text-xs font-medium text-ink">
            <code className="rounded-xs bg-surface-sunken px-1 py-0.5 font-mono text-[11px]">
              {tool.name}
            </code>
            <span className="font-normal text-ink-faint">{tool.label}</span>
          </span>
          <span className="line-clamp-2 text-[11px] text-ink-faint">{tool.description}</span>
        </button>
      ))}
    </div>
  )
}
