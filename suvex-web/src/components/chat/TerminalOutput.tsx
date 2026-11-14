import { useMemo } from 'react'
import { cn } from '@/lib/cn'

/*
  Tool output is terminal text, not prose. OMP highlights the parts that carry
  meaning: keys, strings, numbers, errors, successes, URLs and HTTP status.
  Keep it line-local so a streaming append never changes earlier lines.
*/
const TOKEN =
  /(\[[^\]\n]{1,80}\]|\bflag\{[^}\n]*\}|\b[A-Za-z_][\w.-]*=|\bhttps?:\/\/[^\s]+|\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?|\b(?:error|failed|failure|exception|denied|invalid|timeout|refused)\b|\b(?:success|succeeded|ok|found|passed|completed)\b|\b(?:2\d\d|3\d\d|4\d\d|5\d\d)\b|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b(?:true|false|null)\b|\b[0-9a-f]{8,}\b|-?\b\d+(?:\.\d+)?\b)/gi

const JSON_TOKEN =
  /("(?:\\.|[^"\\])*")(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}[\],:]/g

function classOf(token: string): string {
  if (token.startsWith('[')) return 'text-accent'
  if (token.startsWith('flag{')) return 'text-signal-ok'
  if (token.endsWith('=')) return 'text-syntax-property'
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
  if (/^(success|succeeded|ok|found|passed|completed)$/i.test(token)) {
    return 'text-signal-ok'
  }
  if (/^(true|false|null)$/i.test(token)) return 'text-syntax-keyword'
  if (/^['"]/.test(token)) return 'text-syntax-string'
  if (/^\d/.test(token)) return 'text-syntax-number'
  return 'text-syntax-number'
}

function lineClass(line: string): string {
  if (/^\s*\+/.test(line)) return 'text-signal-ok'
  if (/^\s*-/.test(line)) return 'text-signal-risk'
  if (/^\s*!/.test(line)) return 'text-signal-warn'
  return ''
}

type Piece = { text: string; className: string }

function splitLine(line: string, json: boolean): Piece[] {
  const re = json ? JSON_TOKEN : TOKEN
  const parts: Piece[] = []
  let last = 0
  for (const match of line.matchAll(re)) {
    const index = match.index ?? 0
    if (index > last) parts.push({ text: line.slice(last, index), className: '' })
    const token = match[0]
    if (json) {
      // JSON key: the string is followed by a colon. `match[2]` holds `:`.
      if (match[1] && match[2]) {
        parts.push({ text: match[1], className: 'text-syntax-property' })
        parts.push({ text: match[2], className: 'text-syntax-operator' })
      } else if (/^["']/.test(token)) {
        parts.push({ text: token, className: 'text-syntax-string' })
      } else if (/^(true|false|null)$/i.test(token)) {
        parts.push({ text: token, className: 'text-syntax-keyword' })
      } else if (/^-?\d/.test(token)) {
        parts.push({ text: token, className: 'text-syntax-number' })
      } else {
        parts.push({ text: token, className: 'text-syntax-operator' })
      }
    } else {
      parts.push({ text: token, className: classOf(token) })
    }
    last = index + token.length
  }
  if (last < line.length) parts.push({ text: line.slice(last), className: '' })
  if (parts.length === 0) parts.push({ text: '\u00A0', className: '' })
  return parts
}

export function TerminalOutput({ text }: { text: string }) {
  const json = useMemo(() => {
    const trimmed = text.trim()
    if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return false
    try {
      JSON.parse(trimmed)
      return true
    } catch {
      return false
    }
  }, [text])

  const lines = useMemo(() => {
    return text.split('\n').map((line) => {
      const parts = splitLine(line, json)
      return { className: lineClass(line), parts }
    })
  }, [text, json])

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
