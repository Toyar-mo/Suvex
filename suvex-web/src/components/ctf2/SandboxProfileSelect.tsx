import { cn } from '@/lib/cn'
import { SANDBOX_PROFILE_HINT, SANDBOX_PROFILE_LABEL, type Ctf2SandboxProfile } from '@/data/ctf2'
import { Boxes, Wrench } from 'lucide-react'

/**
 * 沙箱镜像选择器。
 *
 * ## ★ 为什么是"选一个"而不是"全自动判断"（自动判断也已经有了）
 *
 * 同步时后端会按题目分类**推导**一个镜像并落库（`profile_for_category`），
 * 所以通常用户不需要碰这里。但它必须能**手工覆盖**，理由是分类不可靠：
 *
 * * 平台的分类字符串是**别人给的**，可能有拼写差异或我们没见过的值；
 * * 更实际的是：一道 misc 题里躺着个 ELF 要逆向是很常见的，
 *   而它的分类写的是 `misc` —— 按分类推导会得到通用镜像，
 *   而 agent 在里面找不到 r2。
 *
 * 把判断权完全交给分类，会让这类题变成"看起来跑了、其实工具不够"，
 * 而那是最难归因的失败。所以：默认推导 + 允许覆盖。
 *
 * ## 两个选项的图标不同不是装饰
 *
 * 它们是两类**能力**（通用分析 / 二进制分析），用同一个图标会让人
 * 在两个几乎一样的条目之间反复读文字。
 */
export function SandboxProfileSelect({
  value,
  onChange,
  disabled = false,
  /** 紧凑模式：卡片上用，不带标题与底部说明 */
  compact = false,
  id = 'ctf2-sandbox-profile',
}: {
  value: Ctf2SandboxProfile
  onChange: (next: Ctf2SandboxProfile) => void
  disabled?: boolean
  compact?: boolean
  id?: string
}) {
  const options: { value: Ctf2SandboxProfile; icon: typeof Boxes }[] = [
    { value: 'ctf2-sandbox', icon: Boxes },
    { value: 'ctf2-sandbox-re', icon: Wrench },
  ]

  return (
    <div className="flex flex-col gap-1.5">
      {!compact && (
        <span id={`${id}-label`} className="text-2xs font-medium text-ink">
          解题沙箱
        </span>
      )}

      <div
        id={id}
        role="radiogroup"
        aria-label="解题沙箱镜像"
        aria-labelledby={compact ? undefined : `${id}-label`}
        className="flex flex-col gap-1"
      >
        {options.map((option) => {
          const Icon = option.icon
          const active = value === option.value
          return (
            <button
              key={option.value}
              type="button"
              role="radio"
              aria-checked={active}
              disabled={disabled}
              onClick={() => onChange(option.value)}
              className={cn(
                'flex items-start gap-2 rounded-sm border px-2.5 py-2 text-left',
                'transition-ui duration-fast',
                active ? 'border-accent/60 bg-accent/10' : 'border-line hover:border-line-strong',
                /*
                  禁用不压 opacity（那会把两行说明文字一起打薄，而"三个档位有什么区别"
                  恰恰要在这时候读）。只把边框压回常态 + 禁掉悬停反馈。

                  ★ 必须用 `disabled:` 前缀。写成条件拼的 `hover:border-line` 是无效覆写：
                    它与 `hover:border-line-strong` 特异性相同，胜负由 Tailwind 生成 CSS 的
                    先后决定（见 `Button.tsx` 的 `VARIANT_DISABLED` 注释）。加 prefix 后
                    特异性 (0,2,0) → (0,3,0)，顺序就无关了。
                */
                'disabled:cursor-not-allowed disabled:border-line disabled:hover:border-line',
              )}
            >
              <Icon
                size={13}
                strokeWidth={1.75}
                className={cn('mt-0.5 shrink-0', active ? 'text-accent-strong' : 'text-ink-ghost')}
                aria-hidden="true"
              />
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className={cn('text-xs', active ? 'text-ink' : 'text-ink-muted')}>
                  {SANDBOX_PROFILE_LABEL[option.value]}
                </span>
                <span className="text-2xs text-ink-faint">
                  {SANDBOX_PROFILE_HINT[option.value]}
                </span>
              </span>
            </button>
          )
        })}
      </div>

      {!compact && (
        <span className="text-2xs text-ink-faint">
          题目同步时会按分类自动选一个，这里改成你想要的。
        </span>
      )}
    </div>
  )
}

export default SandboxProfileSelect
