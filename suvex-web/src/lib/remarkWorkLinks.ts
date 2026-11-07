/**
 * remark 插件：把助手正文里的 `/work/persist/<路径>` 变成可下载链接。
 *
 * ## 为什么需要插件，而不是渲染后替换
 *
 * v1.36 的链接化是在**纯文本**上做的（见 `workFiles.splitWorkLinks`）。
 * 对话正文改成 Markdown 渲染之后，模型最常写的形态是反引号里的
 * `` `/work/persist/report.md` `` —— 它会被解析成 `inlineCode` 节点，
 * 再在渲染后的 DOM 里找文本就晚了（代码节点已经被特殊样式接管）。
 *
 * 所以在 mdast 阶段改写：text 节点里出现路径就拆成 text + link；
 * 整段就行内代码且内容是一个路径时，也换成 link。围栏代码块（`code`）
 * 一律不动 —— 那是用户要复制的原文。
 */

import { splitWorkLinks, workspaceFileUrl } from '@/lib/workFiles'

interface MdNode {
  type: string
  value?: string
  url?: string
  data?: { hProperties?: Record<string, unknown> }
  children?: MdNode[]
}

function linkNode(sessionId: string, path: string, label: string): MdNode {
  return {
    type: 'link',
    url: workspaceFileUrl(sessionId, path),
    children: [{ type: 'text', value: label }],
    data: { hProperties: { download: true, title: '下载这个产物' } },
  }
}

function transform(node: MdNode, sessionId: string): void {
  if (!node.children) return
  if (node.type === 'code') return // 围栏代码原样保留

  const next: MdNode[] = []
  for (const child of node.children) {
    if (child.type === 'text' && child.value) {
      const parts = splitWorkLinks(child.value)
      if (parts.some((part) => part.type === 'link')) {
        for (const part of parts) {
          if (part.type === 'text') {
            next.push({ type: 'text', value: part.value })
          } else {
            next.push(linkNode(sessionId, part.path, part.value))
          }
        }
        continue
      }
    }
    // 整段反引号就是一个路径 → 直接变成下载链接（而不是不可点的代码）。
    if (child.type === 'inlineCode' && child.value) {
      const parts = splitWorkLinks(child.value)
      if (parts.length === 1 && parts[0].type === 'link') {
        next.push(linkNode(sessionId, parts[0].path, parts[0].value))
        continue
      }
    }
    transform(child, sessionId)
    next.push(child)
  }
  node.children = next
}

/**
 * `remarkPlugins` 里的一项：闭包持有当前会话 id。
 *
 * ⚠️ 返回值必须是**插件**（`() => transformer`），不能直接是 transformer。
 *    unified 在 freeze 阶段会用**无参**调用一次 plugin，再把它的返回值当作
 *    transformer 使用；直接传 transformer 会让它在 freeze 时拿到 `undefined`
 *    的 tree 而崩掉（实测报 `Cannot read properties of undefined`）。
 */
export function remarkWorkLinks(sessionId: string) {
  return () => (tree: MdNode): void => {
    transform(tree, sessionId)
  }
}
