import { cn } from '@/lib/cn'
import { copyText } from '@/lib/clipboard'
import { tokenize, type TokenKind } from '@/lib/highlight'
import { Check, Copy } from 'lucide-react'
import { useMemo, useState } from 'react'

/**
 * 代码 / 日志 / 报文的展示块。
 *
 * 三个设计决定：
 *
 * 1. **渲染成 React 元素，不用 dangerouslySetInnerHTML。**
 *    内容来自 agent 的产出（PoC 代码、HTTP 响应），是**不可信数据**。
 *    用 innerHTML 拼字符串等于把 XSS 引进来 —— 在一个安全测试工具里
 *    尤其讽刺。用元素数组则天然转义。
 *
 * 2. **不软换行**（whitespace-pre + overflow-x-auto）。
 *    日志的换行位置携带信息，软换行会把一行折成多行，看起来像多条记录。
 *    宁可横向滚动。
 *
 * 3. **行号可选，默认按长度自动决定。** 短片段加行号只是噪音；
 *    超过 8 行时行号才有助于定位。
 */

/**
 * 每种 token 的颜色，全部来自 tailwind.config.js 的 `syntax` 色板。
 *
 * 刻意**不**复用 signal 色：signal 是状态语义（绿=通过、琥珀=待确认），
 * 拿它标语法会让同一个绿色在两处表示不同东西。syntax 是按
 * 「连续文本里快速分词」解出来的独立色板，选值理由写在 config 里。
 */
const TOKEN_CLASS: Record<TokenKind, string> = {
  plain: 'text-syntax-plain',
  comment: 'text-syntax-comment italic',
  keyword: 'text-syntax-keyword',
  string: 'text-syntax-string',
  number: 'text-syntax-number',
  function: 'text-syntax-function',
  variable: 'text-syntax-variable',
  operator: 'text-syntax-operator',
  property: 'text-syntax-property',
  meta: 'text-syntax-comment',
}

export interface CodeBlockProps {
  code: string
  /** 语言标识，决定高亮规则。不认识则退化为纯文本 */
  language?: string
  /** 是否显示行号。不传时按行数自动决定 */
  showLineNumbers?: boolean
  /** 是否显示复制按钮 */
  copyable?: boolean
  /** 头部左侧的标题，如文件名或说明 */
  title?: string
  className?: string
  /** 最大高度，超出后内部滚动 */
  maxHeight?: string
}

export function CodeBlock({
  code,
  language,
  showLineNumbers,
  copyable = true,
  title,
  className,
  maxHeight,
}: CodeBlockProps) {
  const [copied, setCopied] = useState(false)

  const lines = useMemo(() => tokenize(code, language ?? 'text'), [code, language])
  const withNumbers = showLineNumbers ?? lines.length > 8

  const handleCopy = async () => {
    // ★ 不能只调 `navigator.clipboard`：它在非安全上下文里**不存在**
    //   （本系统按「局域网 IP + http」部署，实测 `isSecureContext === false`、
    //   `typeof navigator.clipboard === 'undefined'`），直接调会抛
    //   `TypeError: Cannot read properties of undefined (reading 'writeText')`。
    //   `copyText` 自带 `execCommand` 退路，见 `@/lib/clipboard`。
    if (!(await copyText(code))) {
      // 降级链都失败。这里仍然不弹错：代码正文就在眼前且**完整可见**，
      // 用户可以自己选中复制 —— 这一点与 `FlagChip` 不同（那个是截断
      // 显示的，用户无法确认拿全了，所以它必须自己给出出路）。
      return
    }
    setCopied(true)
    // 2 秒后恢复 —— 短暂到够确认，又不会一直占着视觉
    window.setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div
      className={cn('overflow-hidden rounded-sm border border-line bg-surface-sunken', className)}
    >
      {(title || copyable) && (
        <div className="flex items-center justify-between gap-tight border-b border-line px-3 py-1.5">
          <span className="mono-ui truncate text-ink-faint">{title ?? language ?? 'text'}</span>
          {copyable && (
            <button
              type="button"
              onClick={handleCopy}
              aria-label={copied ? '已复制' : '复制内容'}
              className="inline-flex h-6 shrink-0 items-center gap-1 rounded-xs px-1.5 text-2xs text-ink-faint transition-ui duration-fast hover:bg-surface-raised hover:text-ink"
            >
              {copied ? (
                <>
                  <Check size={11} strokeWidth={2.5} aria-hidden="true" />
                  已复制
                </>
              ) : (
                <>
                  <Copy size={11} strokeWidth={1.75} aria-hidden="true" />
                  复制
                </>
              )}
            </button>
          )}
        </div>
      )}

      <div
        className="overflow-x-auto"
        style={maxHeight ? { maxHeight, overflowY: 'auto' } : undefined}
      >
        <pre className="px-3 py-2 text-xs leading-relaxed">
          <code className="font-mono">
            {lines.map((tokens, lineIndex) => (
              <span key={lineIndex} className="flex">
                {withNumbers && (
                  // 行号不可选中 —— 否则复制时会把行号一起带走
                  <span
                    aria-hidden="true"
                    className="tabular mr-3 w-7 shrink-0 select-none text-right text-ink-faint"
                    data-numeric
                  >
                    {lineIndex + 1}
                  </span>
                )}
                <span className="whitespace-pre">
                  {/* 空行也要占高度，否则行号会错位 */}
                  {tokens.length === 0 ? '\u00A0' : null}
                  {tokens.map((token, tokenIndex) => (
                    <span key={tokenIndex} className={TOKEN_CLASS[token.kind]}>
                      {token.text}
                    </span>
                  ))}
                </span>
              </span>
            ))}
          </code>
        </pre>
      </div>
    </div>
  )
}

export default CodeBlock
