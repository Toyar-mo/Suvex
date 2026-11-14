import { useMemo } from 'react'
import { cn } from '@/lib/cn'

/*
  Tool output is terminal text, not prose. OMP highlights the parts that carry
  meaning: errors, successes, URLs/hosts and HTTP status. Keep it line-local so
  a streaming append never changes earlier lines.
*/
const MEANINGFUL =
  /(https?:\/\/[^\s]+|\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?|\b(?:error|failed|failure|exception|denied|invalid|timeout|refused)\b|\b(?:success|succeeded|ok|found|passed|completed)\b|\b(?:2\d\d|3\d\d|4\d\d|5\d\d)\b)/gi

function classOf(token: string): string {
  if (/^https?:\/\//i.test(token)) return 'text-accent'
  if (/^\d{1,3}(?:\.\d{1,3}){3}/.test(token)) return 'text-syntax-function'
  if (/^\d{3}$/.test(token)) {
    if (token.startsWith('2')) return 'text-signal-ok'
    if (token.startsWith('3')) return 'text-signal-info'
    if (token.startsWith('4')) return 'text-signal-warn'
    return 'text-signal-risk'
  }
  if (/^(error|failed|failure|exception|denied|invalid|timeout|refused)$/i.test(token)) {
    return 'text-signal-risk'
  }
  return 'text-signal-ok'
}

function lineClass(line: string): string {
  if (/^\s*\+/.test(line)) return 'text-signal-ok'
  if (/^\s*-/.test(line)) return 'text-signal-risk'
  if (/^\s*!/.test(line)) return 'text-signal-warn'
  return ''
}

export function TerminalOutput({ text }: { text: string }) {
  const lines = useMemo(() => {
    return text.split('\n').map((line) => {
      const parts: Array<{ text: string; className: string }> = []
      let last = 0
      for (const match of line.matchAll(MEANINGFUL)) {
        const index = match.index ?? 0
        if (index > last) parts.push({ text: line.slice(last, index), className: '' })
        parts.push({ text: match[0], className: classOf(match[0]) })
        last = index + match[0].length
      }
      if (last < line.length) parts.push({ text: line.slice(last), className: '' })
      if (parts.length === 0) parts.push({ text: '\u00A0', className: '' })
      return { text: line, className: lineClass(line), parts }
    })
  }, [text])

  return (
    <>
      {lines.map((line, index) => (
        <span key={index} className={cn('block', line.className)}>
          {line.parts.map((part, partIndex) => (
            <span key={partIndex} className={part.className}>
              {part.text}
            </span>
          ))}
        </span>
      ))}
    </>
  )
}

export default TerminalOutput
