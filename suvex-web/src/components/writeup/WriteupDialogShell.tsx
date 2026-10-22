import { cn } from '@/lib/cn'
import { copyText } from '@/lib/clipboard'
import { downloadMarkdown } from '@/lib/writeup'
import { AlertTriangle, Check, Copy, Download, Loader2, X, type LucideIcon } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { WriteupMarkdown } from '@/components/writeup/WriteupMarkdown'

/*
  ─────────────────────────────────────────────────────────────
  题解弹窗的外壳（2026-10-14 从 `scans/WriteupDialog` 抽成通用件）

  ## 为什么抽出来

  v0.36 之前只有**扫描**有题解。现在 CTF2 也要出（它不走 strix，
  所以不能复用扫描那条链路），于是有两个入口显示同一种东西。

  复制一份弹窗是最省事的做法，也是最坏的：两份迟早漂移，而症状是
  "扫描的题解和 CTF2 的题解看起来不是同一个产品"（按钮位置、
  加载态、失败态的措辞各差一点）。

  ## 这个组件只管**外壳**，不管数据从哪来

  它收的话是 `loading | ready | error` 三态 —— 而"怎么拿到题解"
  由调用方决定（扫描走 `loadWriteup(scanId)`，CTF2 走
  `loadKernelWriteup(challengeId)`）。

  ⚠️ 这样分层的理由：两个入口的**取数逻辑不同**（端点不同、
  错误文案不同），但**呈现必须相同**。把取数塞进外壳会让它
  必须知道两种调用方式 —— 那就又变回了"两份逻辑写在一个文件里"。

  ## 三态而不是二态

  `loading` 与 `error` 是**两件事**，不能被合并成"还没好"：

    · 加载中 → 用户该等；
    · 出错 → 用户该读那句话并决定下一步（重跑？换题？）。

  所以失败态**必须把后端那句中文原样显示** —— 它区分了
  "题目没解出" / "不是夺旗任务" / "报告被清理"三种处境，
  是用户唯一能据以行动的线索。换成一句笼统的"题解不可用"，
  等于把唯一可行动的信息扔掉。
  ─────────────────────────────────────────────────────────────
*/

/** 弹窗的三态。由调用方在 `useEffect` 里推进。 */
export type WriteupDialogState =
  | { phase: 'loading' }
  | { phase: 'ready'; markdown: string; filename: string }
  | { phase: 'error'; message: string }

export interface WriteupDialogShellProps {
  /** 副标题（题目名或扫描名）。可能很长，会单行截断并给悬停全文 */
  title: string
  /** 下载文件名的兜底（后端没给时用）。**不含扩展名以外的路径** */
  fallbackFilename: string
  state: WriteupDialogState
  onClose: () => void
  /** 失败态的标题。默认"这次没有可看的题解" */
  errorTitle?: string
  /**
   * 标题左边那颗图标。
   *
   * ★ 为什么是"传进来"而不是写死一个：两个入口的**图标语言本就不同**
   *   （扫描侧是 `FileText`，CTF2 侧也是 `FileText`，但将来若有一侧
   *   换成别的，也不该让这个外壳被动跟着变）。外壳只管版式，
   *   不认识业务 —— 与它"只管三态、不管取数"是同一条分工。
   */
  icon?: LucideIcon
}

export function WriteupDialogShell({
  title,
  fallbackFilename,
  state,
  onClose,
  errorTitle = '这次没有可看的题解',
  icon: Icon,
}: WriteupDialogShellProps) {
  const [copied, setCopied] = useState(false)
  /** 复制态的定时器。要能取消 —— 用户在 2 秒内关掉弹窗时它还在跑 */
  const copyTimer = useRef<number | null>(null)

  // Esc 关闭。与 ConfirmDialog / CompareDialog 一致：三条关法（Esc / 遮罩 / 右上角）
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  // 卸载时清掉复制态的定时器，避免它在组件已经不在时还去 setState
  useEffect(
    () => () => {
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current)
    },
    [],
  )

  const handleCopy = async () => {
    if (state.phase !== 'ready') return
    // `navigator.clipboard` 在非安全上下文里**不存在**（本系统按 http +
    // 局域网 IP 部署，实测），必须走 `copyText` 的降级链。见 `@/lib/clipboard`。
    if (!(await copyText(state.markdown))) {
      /*
        降级链都失败。这里仍然不弹红条：**正文就在眼前、完整可选中**，
        用户可以直接手动复制 —— 这个理由在这个组件里是成立的
        （`FlagChip` 上不成立，那个是截断显示的，所以它自己给出出路）。
      */
      return
    }
    setCopied(true)
    if (copyTimer.current !== null) window.clearTimeout(copyTimer.current)
    // 2 秒后恢复 —— 与 CodeBlock 的复制按钮同一个节拍
    copyTimer.current = window.setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div
      // 遮罩点击 = 关闭（通用预期，与 ConfirmDialog / CompareDialog 一致）
      onClick={onClose}
      className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/70 p-gutter backdrop-blur-sm"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="writeup-title"
        // 阻止冒泡：否则点弹窗内部也会触发遮罩的 onClick
        onClick={(e) => e.stopPropagation()}
        /*
          ★ 高度用 `max-h-[85vh]` + flex 纵向布局：头部固定、正文区自己滚。

          一份题解可能有几千行（逆向题常把整段反汇编贴进附录）。
          没有上限的话，弹窗会长到超出视口 —— 顶部的标题和操作按钮
          被推到屏幕外，而用户以为"这个弹窗没有关闭按钮"。
        */
        className="flex max-h-[85vh] w-full max-w-3xl animate-rise-in flex-col overflow-hidden rounded-md border border-line bg-surface-overlay shadow-overlay"
      >
        <header className="flex items-start justify-between gap-2 border-b border-line px-panel py-3">
          <div className="min-w-0">
            <h2 id="writeup-title" className="flex items-center gap-1.5 text-sm font-medium text-ink">
              {Icon && <Icon size={13} strokeWidth={1.75} className="shrink-0 text-ink-faint" aria-hidden="true" />}
              题解
            </h2>
            {/* 题目名可能很长（CTF 题目名有时带一长串后缀），单行截断 + 悬停给全文 */}
            <p className="mt-0.5 truncate text-xs text-ink-muted" title={title}>
              {title}
            </p>
          </div>

          <div className="flex shrink-0 items-center gap-tight">
            {/*
              两个出口只在**真的有内容**时出现。加载中/失败时摆着它们，
              点下去什么也不会发生 —— 而"点了没反应"比"按钮不存在"更费解。
            */}
            {state.phase === 'ready' && (
              <>
                <button
                  type="button"
                  onClick={handleCopy}
                  className={cn(
                    'inline-flex h-8 items-center gap-1.5 rounded-sm border border-line px-2.5 text-xs',
                    'transition-ui duration-fast hover:border-line-strong hover:text-ink',
                    'focus:outline-none focus:ring-2 focus:ring-accent/40',
                    copied ? 'text-accent-strong' : 'text-ink-muted',
                  )}
                >
                  {copied ? (
                    <>
                      <Check size={12} strokeWidth={2.5} aria-hidden="true" />
                      已复制
                    </>
                  ) : (
                    <>
                      <Copy size={12} strokeWidth={1.75} aria-hidden="true" />
                      复制全文
                    </>
                  )}
                </button>

                <button
                  type="button"
                  onClick={() => downloadMarkdown(state.filename || fallbackFilename, state.markdown)}
                  className={cn(
                    'inline-flex h-8 items-center gap-1.5 rounded-sm bg-accent px-2.5 text-xs font-medium text-canvas',
                    'transition-ui duration-fast hover:bg-accent-strong',
                    'focus:outline-none focus:ring-2 focus:ring-accent/40',
                  )}
                >
                  <Download size={12} strokeWidth={2.25} aria-hidden="true" />
                  下载 .md
                </button>
              </>
            )}

            <button
              type="button"
              onClick={onClose}
              aria-label="关闭"
              className="rounded-sm p-1 text-ink-faint transition-ui duration-fast hover:text-ink"
            >
              <X size={14} strokeWidth={2} aria-hidden="true" />
            </button>
          </div>
        </header>

        {/* 正文区：`min-h-0` 是必需的 —— flex 子项的默认 min-height:auto 会让它拒绝收缩，于是长文档把弹窗撑破 */}
        <div className="min-h-0 flex-1 overflow-y-auto px-panel py-3">
          {state.phase === 'loading' ? (
            <p className="flex items-center gap-2 text-xs text-ink-faint">
              <Loader2 size={13} className="animate-spin" aria-hidden="true" />
              正在读取题解…
            </p>
          ) : state.phase === 'error' ? (
            /*
              ★ 失败态用 signal-warn 而不是 signal-risk。

              "这次没有题解"不是故障：题目没解出来、任务不是夺旗、报告文件
              被清理掉，都是**正常的结果**。用红色会把它读成"系统坏了"，
              而用户下一步该做的是看那句话、决定要不要重跑。
            */
            <div
              role="alert"
              className="flex items-start gap-2 rounded-sm border border-signal-warn/40 bg-signal-warn/[0.06] p-3"
            >
              <AlertTriangle
                size={13}
                strokeWidth={2}
                className="mt-0.5 shrink-0 text-signal-warn"
                aria-hidden="true"
              />
              <div className="min-w-0">
                <p className="text-xs text-signal-warn">{errorTitle}</p>
                {/* 后端那句中文原样显示 —— 它区分了三种处境，是用户唯一能据以行动的线索 */}
                <p className="mt-1 max-w-[64ch] text-2xs text-ink-muted">{state.message}</p>
              </div>
            </div>
          ) : (
            <div className="text-ink">
              <WriteupMarkdown markdown={state.markdown} />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export default WriteupDialogShell
