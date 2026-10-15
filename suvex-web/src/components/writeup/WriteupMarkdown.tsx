import CodeBlock from '@/components/ui/CodeBlock'
import { cn } from '@/lib/cn'
import {
  isValidElement,
  type ComponentPropsWithoutRef,
  type ReactNode,
} from 'react'
import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
/*
  ─────────────────────────────────────────────────────────────
  题解的 Markdown 渲染器（2026-10-14 从 `scans/WriteupDialog` 抽出）

  ## 为什么抽出来

  v0.36 之前只有**扫描**有题解，所以渲染规则长在 `WriteupDialog` 里。
  现在 CTF2 也要出题解（它不走 strix，所以不能复用扫描那条链路）——
  于是有两个入口要显示同一种东西。

  复制一份渲染规则是最省事的做法，也是**最坏**的：两份迟早漂移，
  而症状是"扫描的题解和 CTF2 的题解看起来不是同一个产品的东西"
  （字号、列表缩进、代码块配色各差一点）。那正是要避免的。

  ## 这里的规则都是**踩出来的**，别"顺手简化"

  每一条注释都记着它挡的是什么。几条最容易误删的：

    · `pre` 必须接管 —— react-markdown v10 取消了 `inline` prop，
      靠 `className` 判行内/块级会把**没写语言**的围栏块误判成行内；
    · 列表必须显式写 `list-style` + 左内距 —— Tailwind preflight
      清掉了它们，不写回来题解的步骤会变成一排没符号的段落；
    · 表格必须包一层 `overflow-x-auto` —— 题解表格的值列常是长
      hash/payload，不让滚就会把汉字压成竖排。

  ## 为什么 `components` 是模块级常量

  react-markdown 把 `components` 作为记忆化的依赖之一。内联对象
  **每次渲染都是新引用**，于是每敲一次键盘都要重建整棵 Markdown 子树
  （一份长题解就是几百个元素）。常量引用稳定。
  ─────────────────────────────────────────────────────────────
*/

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
 *   3. **字号不能太小。** 行内代码取 13px 而不是 11px（见下面的 `code`）。
 *
 * ⚠️ **不导出**：外部只需要 `WriteupMarkdown`。导出它会让这个文件
 *    同时导出常量与组件（`react-refresh/only-export-components` 会报
 *    —— 那会让 fast refresh 失效）。而它是对象字面量里含函数，
 *    `allowConstantExport` 也不覆盖。
 */
const WRITEUP_MARKDOWN_COMPONENTS: Components = {
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
  /*
    ★★ h4 / h5 / h6 必须**显式映射**（2026-10-15 补，审计发现的缺口）。

    在此之前它们没有被映射，于是走 react-markdown 的默认输出 ——
    而 **Tailwind preflight 把 heading 统一成了**

        h1,h2,h3,h4,h5,h6 { font-size: inherit; font-weight: inherit }
        h1,h2,h3,h4,h5,h6,hr,figure,p,pre { margin: 0 }

    （实测自 `dist/assets/*.css`）。两者相加的结果是：一个**未映射的 h4
    与正文完全同形**，连段落的那点上下边距都没有 —— 它渲染成一串紧贴
    上下文的裸文本，读者看不出那是一个标题。

    这个缺口平时不显形，因为**旧版式只用到 h3**（实测：库里 28 份题解
    里含 h4 的为 0 份）。而 Dev 正在做的新版式要多一级结构
    （"假设 / 命令 / 逐条命令"这类子块），一旦用上 `####`，
    那几个子标题就会全部隐形。

    字阶沿用本项目既有的中文层级（见 `tailwind.config.js` 的字阶注释：
    行高 ≥1.7、不加负字距、字号不过小）：
      h2 = text-lg / font-semibold，h3 = text-sm / font-medium，
      所以 h4 取 text-xs 收紧到 `ink`（比正文暗一档的层级靠字号与字重表达，
      不再继续放大字号——h5/h6 再小就到 11px 以下，中文会糊）。
  */
  h4: ({ children }: ComponentPropsWithoutRef<'h4'>) => (
    <h4 className="mb-1 mt-panel text-xs font-medium leading-[1.7] text-ink">{children}</h4>
  ),
  h5: ({ children }: ComponentPropsWithoutRef<'h5'>) => (
    <h5 className="mb-1 mt-2 text-2xs font-medium leading-[1.7] text-ink-muted">{children}</h5>
  ),
  h6: ({ children }: ComponentPropsWithoutRef<'h6'>) => (
    <h6 className="mb-1 mt-2 text-2xs leading-[1.7] text-ink-faint">{children}</h6>
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
  ul: ({ children, className }: ComponentPropsWithoutRef<'ul'>) => (
    <ul
      className={cn(
        'my-2 pl-5 marker:text-ink-faint',
        /*
          ★ GFM 任务列表：remark-gfm 会给 `ul` 打 `contains-task-list`、
          给 `li` 打 `task-list-item`（实测确认）。这一组的符号位已经被
          那个复选框占掉了，再画一个圆点会变成"• ☐ 已完成"两个符号。
          所以这一组**去掉 list-style**，但仍保留左内距 —— 否则复选框会
          贴到左边框上，与上面那些普通列表项错开。
        */
        className?.includes('contains-task-list') ? 'list-none' : 'list-disc',
        className,
      )}
    >
      {children}
    </ul>
  ),
  ol: ({ children }: ComponentPropsWithoutRef<'ol'>) => (
    <ol className="my-2 list-decimal pl-5 marker:text-ink-faint">{children}</ol>
  ),
  li: ({ children, className }: ComponentPropsWithoutRef<'li'>) => (
    <li className={cn('my-1 leading-[1.85] text-ink', className)}>{children}</li>
  ),

  /*
    ★ 任务列表的复选框（GFM 的 `- [x]`）。

    不映射它的话拿到的是**浏览器默认复选框**：在深色底上是一个白底方框，
    与这一页的配色完全不是一套东西（默认尺寸也偏大，会把行高顶开）。

    ⚠️ **不**复用 `components/ui/Checkbox` —— 那个是表格行用的**受控**
    组件：它要求 `id` / `onChange` / `label` 三个 prop，而且 `disabled`
    时把自己压到 `opacity-40`。题解里的复选框是**展示**语义
    （"这一步做过"），压到 40% 会让"已勾选"看起来像"不可用"，
    正是语义反了。所以这里直接用同样视觉 token 的裸 input：
    尺寸/圆角/描边/强调色都抄自那个组件，只是不接交互、不做淡化。
  */
  input: ({ type, checked }: ComponentPropsWithoutRef<'input'>) =>
    type === 'checkbox' ? (
      <input
        type="checkbox"
        checked={Boolean(checked)}
        readOnly
        disabled
        className="mr-1.5 h-3.5 w-3.5 shrink-0 translate-y-[2px] rounded-xs border-line bg-surface-sunken accent-[#8B7CF6]"
      />
    ) : null,

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

/** remark 插件列表也要引用稳定 —— 理由同 WRITEUP_MARKDOWN_COMPONENTS */
const REMARK_PLUGINS = [remarkGfm]

/**
 * 渲染一份题解 markdown。
 *
 * ⚠️ 两个入口（扫描 / CTF2）都用它，**不要各自包一层 Markdown** ——
 *    那样 `components` 会各自成为新引用，记忆化失效，长题解会卡。
 */
export function WriteupMarkdown({ markdown }: { markdown: string }) {
  return (
    <Markdown remarkPlugins={REMARK_PLUGINS} components={WRITEUP_MARKDOWN_COMPONENTS}>
      {markdown}
    </Markdown>
  )
}
