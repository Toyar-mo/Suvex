import CodeBlock from '@/components/ui/CodeBlock'
import { cn } from '@/lib/cn'
import { errorMessage } from '@/lib/api'
import { copyText } from '@/lib/clipboard'
import { loadWriteup } from '@/data/scans-store'
import type { Scan } from '@/data/scan'
import { AlertTriangle, Check, Copy, Download, Loader2, X } from 'lucide-react'
import {
  isValidElement,
  useEffect,
  useRef,
  useState,
  type ComponentPropsWithoutRef,
  type ReactNode,
} from 'react'
import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

/**
 * CTF 题解（Writeup）对话框。
 *
 * 一次夺旗扫描解出 flag 之后，后端能产出一份 Markdown 题解 ——
 * 这个弹窗把它显示出来，并给两个出口：**下载 .md** 与 **复制全文**。
 *
 * ## 为什么是弹窗，不是详情页
 *
 * 题解是**一次扫描的附属产物**，看它的动作发生在列表上（"这道题是怎么解出来的"），
 * 看完就走。为它开一个路由意味着：多一个页面、多一套"从列表怎么进来/怎么回去"
 * 的导航状态，而用户并不会把题解当成一个"要去的地方"。
 *
 * ## 三态，且失败态**不许吞掉原因**
 *
 * `409 WRITEUP_UNAVAILABLE` 的 message 是后端写好的中文，它区分了三种处境：
 * 这次不是夺旗任务 / 扫描还没结束 / 报告文件已不在。三者的下一步动作完全不同
 * （换个任务看 / 等它跑完 / 去查文件为什么没了），所以**原样显示**。
 * 换成一句"题解不可用"，等于把唯一可行动的信息扔掉。
 *
 * ## 挂载方式：由调用方**条件挂载**（`{writeupScan && <WriteupDialog …/>}`）
 *
 * 这样每次打开都是一个全新实例：初始态就是"加载中"，不需要在 effect 里
 * 同步重置一次状态（那会多渲染一轮，而且"重置"这件事一旦漏写，
 * 打开第二条扫描时会先闪一眼上一条的题解）。
 */

/** 三态用**一个**联合类型，而不是 `loading` + `error` + `data` 三个 state */
type WriteupState =
  | { phase: 'loading' }
  | { phase: 'ready'; markdown: string; filename: string }
  | { phase: 'error'; message: string }

/**
 * 把 React 子节点拍平成纯文本。
 *
 * 代码块的 children 通常就是一个字符串，但 remark 在有些情况下会把它包成数组，
 * 所以走一遍递归 —— 代价是几行，收益是"某些题解的代码块不会渲染成 `[object Object]`"。
 */
function flattenText(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(flattenText).join('')
  if (isValidElement<{ children?: ReactNode }>(node)) return flattenText(node.props.children)
  return ''
}

/**
 * ★ 代码块：在 `pre` 这一层接管，行内代码与块级代码就此**彻底分流**。
 *
 * 起因是 react-markdown v10 **取消了 `inline` 这个 prop**（v8 之前有）。
 * 也就是说 `code` 映射拿不到"我此刻在不在 pre 里面"，而 hast 节点又不带父指针 ——
 * 靠 `className` 里有没有 `language-` 来判断是行不通的：
 * 一个**没写语言**的围栏代码块渲染出来是 `<pre><code>`（没有 class），
 * 会被误判成行内代码，于是一整段 payload 被塞进一个圆角小方块里。
 *
 * 这里绕开这个判断：`pre` 的 children **就是**那个 `<code>` 元素，
 * 我们不去渲染它，而是从它的 props 里取出语言与文本，交给项目自己的
 * `CodeBlock` —— 于是：
 *
 *   1. 行内代码只会走到下面的 `code` 映射，块级代码只会走到这里；
 *   2. 块级代码块拿到的是**项目自己的**高亮规则、行号与复制按钮，
 *      而不是一个"套了样式的裸 pre"，看起来才像这个项目的一部分。
 *
 * ⚠️ 这里读的是**未经渲染的 React 元素**的 props，所以要防御形状不符的情况
 *    （例如题解正文里直接写了一段原始 HTML `<pre>`）—— 形状不对就退回普通
 *    `<pre>`，不抛错：一份题解里的一个怪块，不该让整个弹窗白屏。
 */
function PreOrCodeBlock({ children }: ComponentPropsWithoutRef<'pre'>) {
  const child = Array.isArray(children) ? children[0] : children

  if (!isValidElement<{ className?: string; children?: ReactNode }>(child)) {
    return (
      <pre className="my-panel overflow-x-auto rounded-sm border border-line bg-surface-sunken px-3 py-2 text-xs">
        {children}
      </pre>
    )
  }

  const language = /language-([\w+#.-]+)/.exec(child.props.className ?? '')?.[1]

  return (
    <CodeBlock
      code={flattenText(child.props.children)}
      language={language}
      // 代码块自己有最大高度 —— 题解里常见几十行的 exploit，
      // 让它把整个弹窗顶长不如让它自己滚（外层滚动条仍然保留）。
      maxHeight="24rem"
      className="my-panel"
    />
  )
}

/**
 * Markdown 元素 → 本项目设计 token 的映射。
 *
 * ## 三条中文排版铁律（与拉丁惯例相反，见 tailwind.config.js 的字阶注释）
 *
 *   1. **不加负字距。** 汉字是满宽方块，收紧会让笔画互撞。所以这里一个
 *      `tracking-*` 都不写，全靠字阶里的正字距。
 *   2. **行高 ≥ 1.7。** 汉字没有 x-height 的下伸空间，1.5 倍行距下中文会糊成一片。
 *      项目的字阶本身是按这条调的，但正文段落与列表项仍显式给了
 *      `leading-[1.85]` —— 字阶给的是固定 rem 值（13px 档只有 1.69），
 *      在长文里差这一点点就能读出来。
 *   3. **≤13px 的小字字重提到 450。** 这一条**不用在这里写**：
 *      `index.css` 里 `:where(.text-2xs, .text-xs, .text-sm)` 已经统一处理，
 *      用 `:where()` 让权重为 0 —— 所以 `font-medium` 之类的强调类照样能覆盖它。
 *
 * ## 为什么是模块级常量，不是内联对象
 *
 * react-markdown 把 `components` 作为记忆化的依赖之一。内联对象**每次渲染
 * 都是新引用**，于是每敲一次键盘、每切一次 state 都要重建整棵 Markdown 子树
 * （一份长题解就是几百个元素）。常量引用稳定，重建只发生在真正需要时。
 */
const MARKDOWN_COMPONENTS: Components = {
  /*
    h1 是题解自己的文档标题（后端生成的是 "# CTF Writeup · 题目名"）。
    它会和弹窗头部的那行标题重复一次 —— 但这是**文档的一部分**，
    删掉就等于擅自改写了产物（下载到的 .md 里仍然有它，屏幕上看不到会是
    另一种不一致）。所以保留，只是压小一档。

    `first:mt-0`：它几乎总是第一个元素，顶部再留一段空白就白留了。
  */
  h1: ({ children }: ComponentPropsWithoutRef<'h1'>) => (
    <h1 className="mb-panel mt-bay text-xl font-semibold leading-[1.7] text-ink first:mt-0">
      {children}
    </h1>
  ),
  h2: ({ children }: ComponentPropsWithoutRef<'h2'>) => (
    <h2 className="mb-2 mt-bay text-lg font-semibold leading-[1.7] text-ink">{children}</h2>
  ),
  h3: ({ children }: ComponentPropsWithoutRef<'h3'>) => (
    <h3 className="mb-1.5 mt-panel text-sm font-medium leading-[1.7] text-ink">{children}</h3>
  ),
  p: ({ children }: ComponentPropsWithoutRef<'p'>) => (
    <p className="my-2 leading-[1.85] text-ink">{children}</p>
  ),

  /*
    ★ 列表必须**显式**写 list-style 与左内距。

    Tailwind 的 preflight 把 ul/ol 的 `list-style: none` 和 `padding: 0`
    一起清掉了（那是给"导航菜单也用 ul"的场景让路）。不写回来的话，
    题解里的步骤列表会变成一排没有符号、也没有缩进的段落 ——
    "第 1 步 / 第 2 步"的层级全丢，而步骤正是题解的骨架。

    marker 用 ink-faint：符号本身不承载信息，不该和正文抢注意力。
  */
  ul: ({ children }: ComponentPropsWithoutRef<'ul'>) => (
    <ul className="my-2 list-disc pl-5 marker:text-ink-faint">{children}</ul>
  ),
  ol: ({ children }: ComponentPropsWithoutRef<'ol'>) => (
    <ol className="my-2 list-decimal pl-5 marker:text-ink-faint">{children}</ol>
  ),
  li: ({ children }: ComponentPropsWithoutRef<'li'>) => (
    <li className="my-1 leading-[1.85] text-ink">{children}</li>
  ),

  /*
    ★ **行内**代码。块级代码根本走不到这里（被上面的 `pre` 接管了，见 PreOrCodeBlock）。

    字号取 13px（`text-sm`），与正文 14px 只差一档 —— 行内代码在中文句子里
    出现得很密（命令、路径、参数），掉到 11px 会在一行里"凹"下去。
    字重不用管：index.css 的 :where 已经把这档提到 450。
  */
  code: ({ children }: ComponentPropsWithoutRef<'code'>) => (
    <code className="rounded-xs border border-line bg-surface-sunken px-1 py-0.5 font-mono text-sm text-accent-strong">
      {children}
    </code>
  ),
  pre: PreOrCodeBlock,

  /*
    表格：**必须能横向滚**。

    题解里的表格是「参数 / 值」这种形状，值那一列常常是一长串 hash、
    payload 或 cookie。不让它滚，浏览器就会把单元格压成窄条 ——
    汉字一个一行地竖着排，表格彻底不可读。
    外层包一层 overflow-x-auto，是为了让**表头跟着表体一起**滚（一致），
    而不是只给表体加滚动条。

    圆角与描边加在外层：直接加在 table 上，横向滚动时会看到边框被截断。
  */
  table: ({ children }: ComponentPropsWithoutRef<'table'>) => (
    <div className="my-panel overflow-x-auto rounded-sm border border-line">
      {/* 13px：表格是密集信息，比正文小一档更好扫；字重 450 同样由 index.css 兜住 */}
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  ),
  th: ({ children }: ComponentPropsWithoutRef<'th'>) => (
    <th className="border-b border-line bg-surface-raised px-2 py-1.5 text-left font-medium text-ink">
      {children}
    </th>
  ),
  td: ({ children }: ComponentPropsWithoutRef<'td'>) => (
    <td className="border-b border-line/60 px-2 py-1.5 align-top text-ink-muted">{children}</td>
  ),

  blockquote: ({ children }: ComponentPropsWithoutRef<'blockquote'>) => (
    <blockquote className="my-panel border-l-2 border-accent/40 bg-surface-sunken/70 px-3 py-1.5 text-ink-muted">
      {children}
    </blockquote>
  ),

  /*
    链接一律**开新标签**。

    题解里的外链多是参考文章、CVE 页面、靶场地址。在同一个标签页里跳走，
    用户回来时那个弹窗已经关了、列表也刷新过 —— "我刚才看的是哪次扫描的题解"
    这个上下文就丢了。`noreferrer` 顺带挡住 window.opener。
  */
  a: ({ children, href }: ComponentPropsWithoutRef<'a'>) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className="text-accent-strong underline decoration-accent/40 transition-ui duration-fast hover:text-accent"
    >
      {children}
    </a>
  ),

  /* hr 在 preflight 之后宽度为 0（border-width 被清零），必须显式给上边框 */
  hr: () => <hr className="my-bay border-t border-line" />,

  /*
    加粗：题解里的 `**关键结论**` 落在正文与本项目都偏亮的底上，
    光靠 weight 600 在深色背景里不够跳 —— 同时把颜色提到最亮一级（ink）。
    ⚠️ 不写负字距，也不改行高：它在行内，动行高会让整段的行距忽松忽紧。
  */
  strong: ({ children }: ComponentPropsWithoutRef<'strong'>) => (
    <strong className="font-semibold text-ink">{children}</strong>
  ),

  /*
    斜体：后端的题解**结尾恒定有一行** —— `*由 Suvex 生成 · 扫描 `scan_…`*`。
    不映射的话它会以正文色（ink）出现，看起来像正文的最后一句；
    这里压到 ink-muted，并显式写 `italic`（preflight 不会去掉 em 的斜体，
    但显式写出来，将来谁改了 preflight 也不会静默丢掉这层语义）。
  */
  em: ({ children }: ComponentPropsWithoutRef<'em'>) => (
    <em className="italic text-ink-muted">{children}</em>
  ),
}

/** remark 插件列表也要引用稳定 —— 理由同 MARKDOWN_COMPONENTS */
const REMARK_PLUGINS = [remarkGfm]

/**
 * 触发一次浏览器下载。
 *
 * 与 `lib/scan-export.ts` 的 `downloadCsv` 同一套做法（Blob + 临时 `<a download>`），
 * 不新增依赖。`scan-export` 那份是扫描列表的 CSV 导出，职责不同，所以没有合并。
 *
 * ⚠️ **不带 BOM**（CSV 那份必须带，因为 Excel 会用本地代码页解 UTF-8）。
 *    而 .md 是给 Markdown 编辑器/渲染器读的，它们默认按 UTF-8 解析，
 *    带上 BOM 反而会在文件头留一个不可见字符 —— 某些解析器会把它当成
 *    正文第一行的内容，于是"# 标题"不再是标题。
 */
function downloadMarkdown(filename: string, markdown: string): void {
  const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  // 立刻释放：不释放的话每次下载都会漏一份 blob 在内存里
  URL.revokeObjectURL(url)
}

export interface WriteupDialogProps {
  /** 要展示题解的那次扫描。**由调用方条件挂载**，所以这里不会是 null */
  scan: Scan
  onClose: () => void
}

export function WriteupDialog({ scan, onClose }: WriteupDialogProps) {
  const [state, setState] = useState<WriteupState>({ phase: 'loading' })
  const [copied, setCopied] = useState(false)
  /** 复制态的定时器。要能取消 —— 用户在 2 秒内关掉弹窗时它还在跑 */
  const copyTimer = useRef<number | null>(null)

  /*
    拉题解。**初始态已经是 loading**（组件每次打开都是新实例），
    所以这里不写 `setState({ phase: 'loading' })` ——
    在 effect 里同步改状态会多渲染一轮，而且那是"重置"，不是"加载"。

    `alive` 守卫：请求回来时弹窗可能已经关了（用户按了 Esc）。
    React 18 起对卸载后 setState 不再警告，但这里的状态变化会触发
    一次真实的渲染尝试 —— 挡住它，代价是两行。
  */
  useEffect(() => {
    let alive = true
    void loadWriteup(scan.id)
      .then((data) => {
        if (alive) setState({ phase: 'ready', markdown: data.markdown, filename: data.filename })
      })
      .catch((err) => {
        // ★ 不吞：后端那句中文要原样到用户眼前（见文件头的三态说明）
        if (alive) setState({ phase: 'error', message: errorMessage(err) })
      })
    return () => {
      alive = false
    }
  }, [scan.id])

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

  /*
    ★ 题目名怎么取。

    优先 `scan.name`，但它**可能只是目标地址的复制品** —— 向导里名称留空时
    后端拿 target 顶上，所以库里 `name === target` 是多数。判据与 ScanList
    里的 `named` 完全一致：只有用户真的起了一个不同的名字，才用名称。

    ⚠️ 两处判据必须一致：不一致的话，同一个扫描在列表里叫 A、在题解弹窗里叫 B，
       而用户是**按列表那一行**点进来的。

    最后那个兜底是给 `artifact` 目标的：它没有地址，`target` 是空串，
    名称也可能为空 —— 那时显示的是一句"未命名题目"，而不是一个空标题。
  */
  const displayName = scan.name && scan.name !== scan.target ? scan.name : scan.target
  const title = displayName || '未命名题目'

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
            <h2 id="writeup-title" className="text-sm font-medium text-ink">
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
                  onClick={() => downloadMarkdown(state.filename || `CTF-Writeup-${scan.id}.md`, state.markdown)}
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
                <p className="text-xs text-signal-warn">这次没有可看的题解</p>
                {/* 后端那句中文原样显示 —— 它区分了三种处境，是用户唯一能据以行动的线索 */}
                <p className="mt-1 max-w-[64ch] text-2xs text-ink-muted">{state.message}</p>
              </div>
            </div>
          ) : (
            <div className="text-ink">
              <Markdown remarkPlugins={REMARK_PLUGINS} components={MARKDOWN_COMPONENTS}>
                {state.markdown}
              </Markdown>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export default WriteupDialog
