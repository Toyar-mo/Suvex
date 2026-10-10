import { cn } from '@/lib/cn'
import { copyText } from '@/lib/clipboard'
import { Eye, EyeOff, Lock } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

/**
 * 密钥输入字段 —— 本页信任感的落点。
 *
 * ★ 验收要求「涉及密钥的输入框必须做防误显示处理」，
 *   并明确要求「密钥类字段的安全交互符合常见后台产品标准」。
 *   按这两条拆成下面这些具体约束，每一条都有明确理由：
 *
 *   1. **输入过程就遮蔽。** 默认 `type="password"`，敲键盘时看到的是圆点。
 *      ⚠️ 这里改过一版：早先的做法是"输入时自动显示明文，方便用户确认"，
 *      那是错的 —— 那正好把密钥暴露在**最容易被看到的时刻**
 *      （用户正在输入，注意力在键盘上，旁边的人在看屏幕）。
 *      所有主流控制台都是输入即遮蔽，这是有原因的。
 *
 *   2. **显示是显式动作，且会自动收回。** 点眼睛图标切换明文，
 *      但会在 20 秒后自动恢复遮蔽，失焦（点到别处）也立刻恢复。
 *      不考虑"用户会记得关掉" —— 这类页面经常在共享屏幕、
 *      会议投屏时打开，暴露窗口必须由界面自己兜住，不能靠人。
 *
 *   3. **复制只在显示态可用，且 2 秒后清空剪贴板。**
 *      密钥不该长时间躺在系统剪贴板里 —— 那是所有应用都能读的地方。
 *      遮蔽态不提供复制：那等于把明文送进剪贴板而用户并不自知。
 *
 *   4. **已保存的密钥不回填。** 输入框永远从空开始。
 *      把已存的密钥回填进来，等于每次打开设置页都把明文搬进 DOM，
 *      而这正是"不要明文常驻"要防的事。
 *      代价是保存时必须把原值带上（见 ProviderPanel 的 payload 拼装）——
 *      否则用户只改个显示名就会把凭据清空。
 *
 *   5. **关掉浏览器的自动填充与自动纠正。** autoComplete 用
 *      "new-password" 而不是 "off"：后者在 Chrome 上常被忽略，
 *      而浏览器会热情地把登录密码填进这个 API key 输入框。
 *      移动端键盘的自动大写/智能引号会把 key 改坏，一并关掉。
 *
 *   6. **拼写检查关闭。** 否则 key 会被画上红色波浪线，
 *      而且某些浏览器扩展会把它当作可疑文本上传。
 */

/**
 * 明文自动收回的时间。
 *
 * 20 秒是个折中：够看清并核对一串 key，又不至于让它在屏幕上长期停留。
 * 再短会让人来不及看（尤其 key 很长时），再长就失去"自动兜底"的意义了。
 */
const AUTO_HIDE_MS = 20_000

/** 复制后清空剪贴板的延迟。立即清会让粘贴拿到空内容 */
const CLIPBOARD_CLEAR_MS = 2_000

export interface SecretFieldProps {
  id: string
  label: string
  value: string
  onChange: (value: string) => void
  /**
   * 服务端已有该密钥（输入框为空不代表没配）。
   *
   * 这个状态必须显示出来：否则用户看到空输入框会以为密钥丢了，
   * 于是重新粘贴一遍 —— 那会让"最后修改时间"之类的元信息失真。
   */
  savedHint?: string | null
  placeholder?: string
  required?: boolean
  /** 校验错误。非空时显示并标红 */
  error?: string | null
  help?: string
  /** 格式提示，如 "形如 sk-…"。与 error 互斥显示（error 优先） */
  hint?: string
}

export function SecretField({
  id,
  label,
  value,
  onChange,
  savedHint,
  placeholder,
  required,
  error,
  help,
  hint,
}: SecretFieldProps) {
  const [revealed, setRevealed] = useState(false)
  const [copied, setCopied] = useState(false)
  const groupRef = useRef<HTMLDivElement>(null)

  const hasValue = value.length > 0

  /*
    自动收回。

    用 effect 而不是在 onClick 里 setTimeout：
    effect 的清理函数会在 revealed 变回 false 时取消计时器，
    于是"用户自己关掉"和"到点自动关"不会叠加出两次状态变更。
    手写 setTimeout 很容易忘记清理，留下一个改已卸载组件状态的定时器。
  */
  useEffect(() => {
    if (!revealed) return
    const timer = window.setTimeout(() => setRevealed(false), AUTO_HIDE_MS)
    return () => window.clearTimeout(timer)
  }, [revealed])

  const handleCopy = async () => {
    // `navigator.clipboard` 在非安全上下文里**不存在**（本系统按 http +
    // 局域网 IP 部署，实测），走降级链。见 `@/lib/clipboard` 的说明。
    if (!(await copyText(value))) {
      // 降级链都失败。这里不弹错：密钥就在输入框里且**完整可选中**
      // （用户是自己点"显示"才看到它的），可以手动复制。
      return
    }
    setCopied(true)
    window.setTimeout(() => setCopied(false), CLIPBOARD_CLEAR_MS)
    window.setTimeout(() => {
      // 尽力而为的清理：把剪贴板里的密钥抹掉。
      // ⚠️ 这里**只用**异步 API，不走 `copyText` —— 清空的目标是"让剪贴板
      //    不再持有密钥"，而 `execCommand` 清空需要先造一个空 textarea 再
      //    选中它，反而会在我们自己的页面里短暂留下一个空的输入区。
      //    清不掉就算了（非安全上下文下必然清不掉），不该打断用户。
      navigator.clipboard?.writeText('').catch(() => {})
    }, CLIPBOARD_CLEAR_MS)
  }

  return (
    <div className="flex flex-col gap-tight">
      <div className="flex items-baseline gap-1.5">
        <label htmlFor={id} className="text-sm font-medium text-ink">
          {label}
        </label>
        {required && (
          <span className="text-2xs text-signal-risk" aria-label="必填">
            必填
          </span>
        )}
      </div>

      {help && <p className="text-xs text-ink-faint">{help}</p>}

      {/*
        失焦即收回。

        用容器级的 onBlur + relatedTarget 判断，而不是给 input 单独挂 onBlur：
        点眼睛按钮时焦点会从 input 移到 button，若只看 input 的 blur，
        会在用户刚点开明文的瞬间又把它关上 —— 那个 bug 表现为"显示按钮没用"。

        relatedTarget 落在容器外，才说明用户真的离开了这一组控件。
      */}
      <div
        ref={groupRef}
        onBlur={(e) => {
          const next = e.relatedTarget as Node | null
          if (!groupRef.current?.contains(next)) setRevealed(false)
        }}
        className="flex items-stretch gap-1.5"
      >
        <div className="relative min-w-0 flex-1">
          {/* 遮蔽态的锁图标：给出"这个字段受保护"的即时信号。
              显示态不出现 —— 那时内容本身就是信号，再加图标是噪音 */}
          {!revealed && hasValue && (
            <Lock
              size={13}
              strokeWidth={1.75}
              className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-ghost"
              aria-hidden="true"
            />
          )}

          <input
            id={id}
            /*
              ★ 默认 password：输入过程即遮蔽（约束 1）。
              revealed 时切到 text —— 这是唯一的明文态，且会自动收回。
            */
            type={revealed ? 'text' : 'password'}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            placeholder={placeholder}
            // 约束 5：见文件头。new-password 是唯一被 Chrome 稳定尊重的"别填"信号
            autoComplete="new-password"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
            aria-invalid={error ? true : undefined}
            aria-describedby={`${id}-feedback`}
            className={cn(
              'h-9 w-full rounded-sm border bg-surface-sunken pr-2.5 text-sm text-ink',
              !revealed && hasValue ? 'pl-8' : 'pl-2.5',
              // 密钥一律等宽：它是标识符不是散文，且要能看出无害的空格
              'font-mono',
              'transition-ui duration-fast placeholder:text-ink-faint',
              'focus:outline-none focus:ring-2 focus:ring-accent/40',
              error ? 'border-signal-risk' : 'border-line hover:border-line-strong',
            )}
          />
        </div>

        {/* 显示切换。只在有内容时出现 —— 空字段没什么可显示的 */}
        {hasValue && (
          <button
            type="button"
            onClick={() => setRevealed((v) => !v)}
            aria-label={revealed ? '隐藏密钥' : '显示密钥'}
            aria-pressed={revealed}
            title={revealed ? '隐藏（20 秒后也会自动隐藏）' : '显示明文'}
            className={cn(
              'inline-flex w-9 shrink-0 items-center justify-center rounded-sm border transition-ui duration-fast',
              revealed
                ? 'border-accent/50 bg-accent-wash text-accent-strong'
                : 'border-line text-ink-faint hover:border-line-strong hover:text-ink',
            )}
          >
            {revealed ? (
              <Eye size={14} strokeWidth={1.75} aria-hidden="true" />
            ) : (
              <EyeOff size={14} strokeWidth={1.75} aria-hidden="true" />
            )}
          </button>
        )}

        {/* 约束 3：复制只在显示态出现 */}
        {hasValue && revealed && (
          <button
            type="button"
            onClick={handleCopy}
            className="inline-flex h-9 shrink-0 items-center rounded-sm border border-line px-2 text-2xs text-ink-muted transition-ui duration-fast hover:border-line-strong hover:text-ink"
          >
            {copied ? '已复制' : '复制'}
          </button>
        )}
      </div>

      {/* 明文态提示自动收回 —— 否则用户会以为它会一直亮着 */}
      {revealed && (
        <p className="text-2xs text-signal-warn" role="status">
          明文将在 20 秒后自动隐藏，点击别处也会立即隐藏。
        </p>
      )}

      {/* 反馈行：错误 > 已保存提示 > 格式提示，只显示一条，避免堆叠 */}
      <p id={`${id}-feedback`} className="flex flex-col gap-0.5 text-xs">
        {error ? (
          <span role="alert" className="text-signal-risk">
            {error}
          </span>
        ) : savedHint ? (
          /*
            "已保存"用中性 muted 而不是绿色：
            它是**状态说明**（这里本来就有值），不是成功结果。
            用绿色会和"刚保存成功"的反馈混在一起，用户分不清。
          */
          <span className="flex items-center gap-1 text-ink-faint">
            <Lock size={10} strokeWidth={2} aria-hidden="true" />
            {savedHint}
          </span>
        ) : hint ? (
          <span className="text-ink-faint">{hint}</span>
        ) : null}
      </p>
    </div>
  )
}

export default SecretField
