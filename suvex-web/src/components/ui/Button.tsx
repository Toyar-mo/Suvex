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
}

export function Button({
  ref,
  variant = 'primary',
  size = 'md',
  loading,
  loadingLabel,
  disabled,
  type = 'button',
  className,
  children,
  onClick,
  ...rest
}: ButtonProps) {
  const pendingRef = useRef(false)

  /*
    锁的两端各在一个地方：

      · **上锁**在 click handler 第一行（同步），因为要挡的正是「提交生效之前」
        的那次点击 —— 等渲染再上锁就已经晚了。
      · **解锁**在这里，跟随 `loading` 变 false。

    ⚠️ 契约：传了 `loading` 就必须让它真的翻成 `true` 再翻回 `false`。
       永远传死值 `loading={false}` 会让按钮在第一次点击后自我锁定
       （锁上了，但再没有东西来清它）。这是刻意的取舍：宁可锁死，
       也不要重复提交 —— 重复提交会真的发出第二条请求。
  */
  useEffect(() => {
    pendingRef.current = loading === true
  }, [loading])

  const isLink = variant === 'link' || variant === 'link-muted'
  const isPending = loading === true

  return (
    <button
      {...rest}
      ref={ref}
      // 全站只有 2 处需要 submit（登录、初始化），其余都不该触发表单提交。
      // 默认 `button` 而不是 HTML 的默认值 `submit`，是这里最容易踩的坑。
      type={type}
      disabled={disabled || isPending}
      aria-busy={isPending || undefined}
      onClick={(event) => {
        // ★ 同步守卫：必须是 handler 的第一件事，早于 onClick 调用方
        if (pendingRef.current) {
          event.preventDefault()
          return
        }
        // 只有「受控加载」的按钮才上锁 —— 纯按钮传死 loading={false} 也不至于被锁，
        // 因为受控判据是 loading !== undefined
        if (loading !== undefined) pendingRef.current = true
        onClick?.(event)
      }}
      className={cn(VARIANT[variant], isLink ? LINK_SIZE[size] : SIZE[size], className)}
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
