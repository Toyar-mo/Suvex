import { cn } from '@/lib/cn'
import { Loader2 } from 'lucide-react'
import { useEffect, useRef, type ButtonHTMLAttributes, type Ref } from 'react'

/**
 * 按钮基元。全站可点的地方只走这两个出口：`Button`（带文字）与 `IconButton`（只有图标）。
 *
 * ## 为什么要建这个
 *
 * 收编之前 `src/pages/` 下有 33 处手写 `<button>`，同一个「实心主按钮」被抄了 20 多遍，
 * 而且抄歪的地方不会被任何检查发现：有的漏了 `hover`，有的漏了 `transition`，
 * 有的把焦点环写成 `focus:ring-accent/40`（40% 透明度的紫环，远低于 UI 组件
 * 3:1 的下限），有的用 `text-white` 这种裸色而不是 token。
 * 类名散在 33 个地方时，**设计系统只能靠自觉执行**；收到基元里之后它才是强制的。
 *
 * ## 变体取值的出处
 *
 * 六个变体的类名不是新设计的，是从现网出现频次最高的写法里**逐字**抽出来的：
 *
 * | 变体        | 出处（收编前的代表位置）                                     |
 * | ----------- | ------------------------------------------------------------ |
 * | `primary`   | `WizardChrome.tsx:71`、`NoScansYet.tsx:84`、`ScansPage.tsx:958` |
 * | `secondary` | `Ctf2.tsx:564`、`ScansPage.tsx` 的描边工具按钮                |
 * | `ghost`     | `FindingsPage.tsx:1006`、`ReportsPage.tsx:1042`               |
 * | `danger`    | `ConfirmDialog.tsx:102`（删除确认）                           |
 * | `link`      | `ScansPage.tsx:935`/`:970`、`FindingsPage.tsx:1154`           |
 * | `link-muted`| `Ctf2.tsx:469`                                                |
 *
 * `primary` 的 `bg-accent text-canvas` 是 §7 实测过的唯一组合：
 * 「`accent` 底 + `canvas` 字」= 5.98:1，hover 到 `accent-strong` = 8.36:1。
 * 而最初写的「`accent-deep` 底 + `ink` 字」只有 4.06:1，hover 掉到 2.74:1 —— 不达 AA。
 *
 * ## ★ `loading` 为什么不能只靠 `disabled`
 *
 * `disabled` 是**渲染结果**：它要等 React 重新渲染、把属性提交到 DOM 之后才生效。
 * 而「防止重复提交」要挡的恰恰是**提交生效之前**的那次点击 —— 典型场景是
 * handler 里先 `await` 一次网络请求、再 `setPending(true)`：
 *
 * ```
 * 点击① → count++ → await …（pending 还是 false，按钮还没 disabled）
 * 点击② → count++   ← 拦不住，两次请求发出去了
 * ```
 *
 * 所以这里额外持一个 `pendingRef`：**在 click handler 的第一行同步置位**，
 * 第二次点击进到 handler 立刻被 `preventDefault` 掉，不等任何渲染。
 * `loading` 回到 `false` 时清位 —— 这条走 `useEffect`，因为那时已经渲染完了，
 * 早一点晚一点都无所谓。
 *
 * ⚠️ 契约：传了 `loading` 就必须真的把它翻成 `true`（哪怕晚一点）。
 *    永远传死值 `loading={false}` 会让按钮在第一次点击后自我锁定 ——
 *    守卫置位后再没有东西来清它。这是刻意的取舍：宁可锁定，也不要重复提交。
 *
 * ## ★ 两类禁用语义：`disabled` 与 `busy`
 *
 * 这两种灰不是一回事，压成一个会丢信息：
 *
 * | | 鼠标 | 语义 | 用户该做什么 |
 * | --- | --- | --- | --- |
 * | `disabled` | `cursor-not-allowed` | 这次**不许**点 | 先改别的东西 |
 * | `busy` | `cursor-wait` | 这次**等一等** | 什么都不用做，等它 |
 *
 * 具体压平会怎样：`ReportsPage` 的「重新下载」在请求中显示「正在生成…」，
 * 那正是 `wait` —— 用户要做的唯一一件事就是等。改成 `not-allowed` 之后，
 * 文案说"等"，光标说"不行"，两个信号互相打脸。
 *
 * 全站刻意保留 `cursor-wait` 的地方（本次都是未收编的老代码，口径一致）：
 * `components/reports/ReportHistory.tsx:190`、`components/scans/WizardChrome.tsx:73`、
 * `components/settings/NotifyPanel.tsx:617`、`components/settings/ProviderPanel.tsx:734`。
 *
 * `busy` 与 `loading` 是**两个维度**，可以同时给：`loading` 管"转圈 + 防重复提交"，
 * `busy` 管"光标怎么说"。只希望光标是 wait、但不想要转圈时（比如图标自己已经在转），
 * 单给 `busy` 就够。
 *
 * 与 `ToggleChip` 的禁用态又是第三条规矩：chip 的禁用常常是"这一项**永远**不能选"，
 * 要长期可读，所以它连 `opacity` 都不用 —— 见 `ToggleChip.tsx` 文件头。
 *
 * ## 焦点环为什么不在这个文件里
 *
 * 全站只有一套焦点环，在 `src/index.css:247` 的 `:focus-visible`。
 * 组件里再写 `focus:ring-*` 会有两个问题：① `focus:` 在**鼠标点击时也画环**，
 * 与 `src/index.css:254` 的 `:focus:not(:focus-visible)` 语义直接打架；
 * ② 手写环的颜色/透明度随手取，绕过了统一的一套。
 */

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'link' | 'link-muted'
export type ButtonSize = 'xs' | 'sm' | 'md' | 'lg'

const VARIANT: Record<ButtonVariant, string> = {
  primary:
    'press inline-flex items-center justify-center gap-1.5 rounded-sm bg-accent font-medium text-canvas transition-ui duration-fast hover:bg-accent-strong disabled:cursor-not-allowed disabled:opacity-50',
  secondary:
    'press inline-flex items-center gap-1.5 rounded-sm border border-line text-ink-muted transition-ui duration-fast hover:border-line-strong hover:text-ink disabled:cursor-not-allowed disabled:opacity-50',
  ghost:
    'press inline-flex items-center gap-1.5 rounded-sm text-ink-faint transition-ui duration-fast hover:bg-surface-raised hover:text-ink disabled:cursor-not-allowed disabled:opacity-50',
  danger:
    'press inline-flex items-center justify-center gap-1.5 rounded-sm bg-signal-risk/90 font-medium text-canvas transition-ui duration-fast hover:bg-signal-risk disabled:cursor-not-allowed disabled:opacity-50',
  link: 'text-accent-strong underline transition-ui duration-fast hover:text-accent',
  'link-muted': 'text-ink-muted underline transition-ui duration-fast hover:text-ink',
}

/**
 * 高 4 档：`xs` 24 → `sm` 32 → `md` 36 → `lg` 40。
 * 一档一个用途，不按"我觉得这个按钮该多高"临时取数。
 */
const SIZE: Record<ButtonSize, string> = {
  xs: 'h-7 px-2 text-2xs',
  sm: 'h-8 px-3 text-xs',
  md: 'h-9 px-3 text-sm',
  lg: 'h-10 px-3 text-sm',
}

/**
 * `link` 族是**行内文字**，没有高度也没有内距 —— 给它 `h-8 px-3` 会让
 * 一段句子里嵌的链接把行撑开。所以它只取字号，`md`/`lg` 一律降到 `text-xs`
 * （现网 6 处下划线链接全是 `text-xs`，只有 `Ctf2.tsx:469` 是 `text-2xs`）。
 */
const LINK_SIZE: Record<ButtonSize, string> = {
  xs: 'text-2xs',
  sm: 'text-xs',
  md: 'text-xs',
  lg: 'text-xs',
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /**
   * React 19 的 ref-as-prop。不用 `forwardRef` —— 全仓库 `forwardRef` 用量是 0，
   * 引入两套写法只会让后来的人猜。`ConfirmDialog` 的初始焦点就靠这个接。
   */
  ref?: Ref<HTMLButtonElement>
  variant?: ButtonVariant
  size?: ButtonSize
  /** 有请求在飞。真时强制 `disabled` + `aria-busy`，并在左侧换成转圈 */
  loading?: boolean
  /** 加载时的文字。不传就沿用 children */
  loadingLabel?: string
  /**
   * "等一等"而不是"不许点"。真时同样不可点，但光标是 `cursor-wait`、
   * **不做 `opacity` 压暗** —— 等待态不是错误态，文案（"正在生成…"）已经说明了
   * 该做什么，再把它灰掉只会让人以为出问题了。与 `loading` 可以同时给。
   */
  busy?: boolean
}

export function Button({
  ref,
  variant = 'primary',
  size = 'md',
  loading,
  loadingLabel,
  busy,
  disabled,
  type = 'button',
  className,
  children,
  onClick,
  ...rest
}: ButtonProps) {
  const pendingRef = useRef(false)
  /*
    释锁回调要读「此刻的 loading」，而它发生在 handler 落定之后，晚于任何一次
    渲染 —— 所以从 ref 读。**只能在 effect 里写**：渲染期写 ref 会被
    `react-hooks/refs` 拦下（它是对的：渲染可能被打断重来，留半截状态比慢一拍坏）。
  */
  const loadingRef = useRef(loading)

  /*
    锁的两端各在一个地方：

      · **上锁**在 click handler 第一行（同步），因为要挡的正是「提交生效之前」
        的那次点击 —— 等渲染再上锁就已经晚了。
      · **解锁**在 handler 这一趟结束之后：它返回了 Promise 就等它落定，只返回
        一个值就等当前同步栈清空。两种情况都只在 `loading` 仍不是 true 时才松手。

    ⚠️ 为什么必须有这条解锁路：原来只在 `loading === true` 时置位、`false` 时
       清除，于是任何传死值 `loading={false}` 的调用方，点一次就再也解锁不了 ——
       按钮从此点不动，而且没有任何检查能发现（实测：隔 50ms 再点，
       `onClick` 计数停在 1）。在 docstring 里写「必须传受控值」不算保障。

    ⚠️ 为什么是「本趟结束」而不是 setTimeout(0)：宏任务会跑到 await 之后，
       「handler 里先 await、之后才 setState」那种调用方会在请求还在飞的时候被
       松锁，等于把守卫删了。而 Promise 落定 / 同步栈清空这两个时机精确覆盖了
       守卫真正要管的窗口（点击 → 状态生效），同一同步块里的第二次点击之间
       两者都不会发生，所以仍然被挡 —— 那才是重复提交真正发生的地方。
  */
  useEffect(() => {
    loadingRef.current = loading
    pendingRef.current = loading === true
  }, [loading])

  const isLink = variant === 'link' || variant === 'link-muted'
  const isPending = loading === true
  const isBusy = busy === true

  return (
    <button
      {...rest}
      ref={ref}
      // 全站只有 2 处需要 submit（登录、初始化），其余都不该触发表单提交。
      // 默认 `button` 而不是 HTML 的默认值 `submit`，是这里最容易踩的坑。
      type={type}
      disabled={disabled || isPending || isBusy}
      aria-busy={isPending || isBusy || undefined}
      onClick={(event) => {
        // ★ 同步守卫：必须是 handler 的第一件事，早于 onClick 调用方
        if (pendingRef.current) {
          event.preventDefault()
          return
        }
        /*
          不传 `loading` 的纯按钮完全不上锁 —— 它没有"在飞"的概念，
          拦它只会让按钮莫名其妙点不动。
        */
        if (loading === undefined) {
          onClick?.(event)
          return
        }

        pendingRef.current = true
        const releaseLock = () => {
          // 状态已经翻起来了：按钮自己 disabled，锁交给上面那个 effect 管
          if (loadingRef.current !== true) pendingRef.current = false
        }
        let returned: unknown
        try {
          returned = onClick?.(event)
        } catch (error) {
          // handler 自己炸了就不会有状态变化，锁必须当场松开，否则永久卡住
          pendingRef.current = false
          throw error
        }
        if (returned && typeof (returned as { then?: unknown }).then === 'function') {
          // 异步 handler：请求落定之后再判。注意只挂 then，不吞异常
          void (returned as Promise<unknown>).then(releaseLock, releaseLock)
        } else {
          // 同步 handler：等当前同步栈清空（微任务），此时那次 setState 的渲染已排队
          queueMicrotask(releaseLock)
        }
      }}
      className={cn(
        VARIANT[variant],
        isLink ? LINK_SIZE[size] : SIZE[size],
        /*
          busy 覆盖 disabled 的两条：光标换成 wait，并撤掉 50% 压暗。
          用 `disabled:` 前缀写而不是靠 className 的层叠顺序，是因为变体里那条
          也是 `disabled:` 前缀 —— twMerge 会把同前缀同组的后写者留下，但依赖
          "谁后写"太脆，显式同前缀更稳。
        */
        isBusy && 'disabled:cursor-wait disabled:opacity-100',
        className,
      )}
    >
      {isPending && (
        <Loader2 size={14} strokeWidth={2} className="animate-spin" aria-hidden="true" />
      )}
      {isPending && loadingLabel ? loadingLabel : children}
    </button>
  )
}

/**
 * 纯图标按钮。**必须给 `label`** —— 图标本身对读屏器是一片空白，
 * 没有 `aria-label` 的话它只会被念成「按钮」。
 *
 * 尺寸只有两档：`xs`(24×24) 用在行内（排序方向、关闭提示条），
 * `sm`(32×32) 用在工具条。24px 是触摸目标的下限，再小就只能用鼠标了。
 */
export type IconButtonSize = 'xs' | 'sm'
export type IconButtonVariant = 'ghost' | 'secondary'

const ICON_VARIANT: Record<IconButtonVariant, string> = {
  ghost:
    'press inline-flex items-center justify-center rounded-sm text-ink-faint transition-ui duration-fast hover:bg-surface-raised hover:text-ink disabled:cursor-not-allowed disabled:opacity-50',
  secondary:
    'press inline-flex items-center justify-center rounded-sm border border-line text-ink-muted transition-ui duration-fast hover:border-line-strong hover:text-ink disabled:cursor-not-allowed disabled:opacity-50',
}

const ICON_SIZE: Record<IconButtonSize, string> = {
  xs: 'h-6 w-6 rounded-xs',
  sm: 'h-8 w-8 rounded-sm',
}

export interface IconButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'aria-label'> {
  ref?: Ref<HTMLButtonElement>
  /** 无障碍名称，落到 `aria-label`。必填，不给就是 bug */
  label: string
  variant?: IconButtonVariant
  size?: IconButtonSize
}

export function IconButton({
  ref,
  label,
  variant = 'ghost',
  size = 'xs',
  type = 'button',
  className,
  children,
  ...rest
}: IconButtonProps) {
  return (
    <button
      {...rest}
      ref={ref}
      type={type}
      aria-label={label}
      className={cn(ICON_VARIANT[variant], ICON_SIZE[size], className)}
    >
      {children}
    </button>
  )
}

export default Button
