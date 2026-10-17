import { cn } from '@/lib/cn'
import { copyText } from '@/lib/clipboard'
import { DangerButton, Field, TextInput, Toggle } from './Field'
import SecretField from './SecretField'
import { SaveBar, SettingsPanel, type SaveState } from './SaveBar'
import {
  TOKEN_EXPIRY_LABEL,
  TOKEN_SCOPE_LABEL,
  TOKEN_SCOPE_NOTE,
  checkPassword,
  expiryToIso,
  validatePasswordChange,
  type ApiToken,
  type TokenExpiry,
  type TokenScope,
} from '@/data/settings'
import { changePassword, createToken, revokeToken } from '@/data/settings-store'
import { formatRelativeTime } from '@/lib/datetime'
import { tokenState } from '@/lib/secret'
import { useRevealOnExpand } from '@/hooks/useRevealOnExpand'
import { AlertTriangle, Check, Copy, KeyRound, Plus, ShieldCheck, X } from 'lucide-react'
import { useMemo, useState } from 'react'

/**
 * 账号与安全面板。
 *
 * ★ 这一节最重要的安全设计是**令牌明文只出现一次**。
 *
 *   创建令牌后弹出的一次性展示框，是用户唯一能看到完整令牌的机会。
 *   关掉它就再也拿不到 —— 只能重新生成。
 *
 *   为什么坚持这个"不方便"：服务端只存哈希是行业标准
 *   （GitHub / Stripe 都如此），因为存明文意味着一次数据库泄露
 *   等于所有人的令牌泄露。而这个约束反过来要求界面必须**极其明确地**
 *   告诉用户"这是唯一一次，请现在复制走" —— 含糊的提示会造成真实损失：
 *   用户以为以后还能看，关掉之后只能重新配一遍所有集成。
 *
 *   所以这里做了三件事：
 *     1. 令牌值等宽大字 + 明确的"只显示这一次"警示；
 *     2. 提供一键复制，并在复制后给出确认；
 *     3. 关闭时若还没确认保存过，**就地追问一次**（而不是把关闭按钮锁死）——
 *        锁死会造成"剪贴板不可用就永远关不掉"的死锁，见 TokenReveal 的说明。
 *
 *   ⚠️ 与密钥字段一致的约束：**完整令牌不进入 store、不进入列表**。
 *      类型上就分开（ApiToken 没有 value 字段），
 *      见 data/settings-store.ts 的 CreatedToken。
 */

export interface AccountPanelProps {
  tokens: ApiToken[]
}

export function AccountPanel({ tokens }: AccountPanelProps) {
  return (
    <SettingsPanel
      id="account"
      title="账号与安全"
      description="登录密码与用于自动化集成的 API 令牌。令牌拥有你授予的权限，请按最小必要授予。"
    >
      <PasswordSection />
      <TokenSection tokens={tokens} />
    </SettingsPanel>
  )
}

// ─────────────────────────────────────────────
// 密码
// ─────────────────────────────────────────────

function PasswordSection() {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [touched, setTouched] = useState(false)
  const [state, setState] = useState<SaveState>({ kind: 'idle' })

  const validation = useMemo(
    () => validatePasswordChange(current, next, confirm),
    [current, next, confirm],
  )

  const strength = useMemo(() => checkPassword(next), [next])

  const handleSubmit = async () => {
    setTouched(true)
    if (!validation.ok) {
      setState({ kind: 'failed', error: validation.error })
      return
    }

    setState({ kind: 'saving' })
    try {
      const result = await changePassword({ current, next })
      if (result.status === 'ok') {
        setState({ kind: 'saved', at: result.at })
        // 成功后立刻清空全部输入 —— 密码不该在表单里多停留一秒
        setCurrent('')
        setNext('')
        setConfirm('')
        setTouched(false)
      } else if (result.status === 'failed') {
        setState({ kind: 'failed', error: result.error })
      } else {
        setState({ kind: 'unchanged' })
      }
    } catch {
      setState({ kind: 'failed', error: '请求失败，请稍后重试' })
    }
  }

  const dirty = current.length > 0 || next.length > 0 || confirm.length > 0

  return (
    <div className="flex flex-col gap-panel">
      <div className="flex items-center gap-1.5">
        <KeyRound size={13} strokeWidth={1.75} className="text-ink-faint" aria-hidden="true" />
        <span className="text-xs font-medium text-ink">登录密码</span>
      </div>

      <div className="grid gap-panel sm:grid-cols-3">
        {/* 三个密码框都走 SecretField —— 同样的遮蔽与按住显示规则，
            不给"当前密码"任何例外：输错时也不该把明文摊在屏幕上 */}
        <SecretField
          id="pwd-current"
          label="当前密码"
          value={current}
          onChange={setCurrent}
          required
          placeholder="输入当前密码"
        />
        <SecretField
          id="pwd-next"
          label="新密码"
          value={next}
          onChange={setNext}
          required
          placeholder="至少 12 个字符"
          error={touched && !validation.ok && next.length > 0 ? validation.error : null}
        />
        <SecretField
          id="pwd-confirm"
          label="确认新密码"
          value={confirm}
          onChange={setConfirm}
          required
          placeholder="再输入一次"
          error={confirm.length > 0 && confirm !== next ? '两次输入不一致' : null}
        />
      </div>

      {/* 强度与要求清单：只在开始输入后出现，空表单时不占地方 */}
      {next.length > 0 && (
        <div className="flex flex-col gap-tight rounded-sm border border-line bg-surface-sunken p-3">
          <div className="flex items-center gap-tight">
            <span className="text-2xs text-ink-muted">强度</span>
            <div className="flex gap-0.5" aria-hidden="true">
              {[0, 1, 2].map((i) => {
                const reached =
                  strength.strength === 'strong' ||
                  (strength.strength === 'fair' && i < 2) ||
                  (strength.strength === 'weak' && i < 1)
                const tone =
                  strength.strength === 'strong'
                    ? 'bg-signal-ok'
                    : strength.strength === 'fair'
                      ? 'bg-signal-warn'
                      : 'bg-signal-risk'
                return (
                  <span
                    key={i}
                    className={cn('h-1 w-8 rounded-pill', reached ? tone : 'bg-line-strong')}
                  />
                )
              })}
            </div>
            <span className="text-2xs text-ink-muted">
              {strength.strength === 'strong' ? '强' : strength.strength === 'fair' ? '中' : '弱'}
            </span>
          </div>

          {/* 要求清单直接渲染 checkPassword 的结果，不在组件里重写一遍规则 */}
          <ul className="flex flex-col gap-0.5">
            {strength.requirements.map((req) => (
              <li
                key={req.label}
                className={cn(
                  'flex items-center gap-1.5 text-2xs',
                  req.met ? 'text-signal-ok' : 'text-ink-faint',
                )}
              >
                {req.met ? (
                  <Check size={10} strokeWidth={3} aria-hidden="true" />
                ) : (
                  <span
                    aria-hidden="true"
                    className="h-2.5 w-2.5 rounded-pill border border-line-strong"
                  />
                )}
                {req.label}
              </li>
            ))}
          </ul>
        </div>
      )}

      <SaveBar
        state={state}
        dirty={dirty}
        onSave={handleSubmit}
        onReset={() => {
          setCurrent('')
          setNext('')
          setConfirm('')
          setTouched(false)
          setState({ kind: 'idle' })
        }}
        saveLabel="修改密码"
        note={<span>修改后其它设备上的登录会话会失效，需要重新登录。</span>}
      />
    </div>
  )
}

// ─────────────────────────────────────────────
// API 令牌
// ─────────────────────────────────────────────

function TokenSection({ tokens }: { tokens: ApiToken[] }) {
  const [creating, setCreating] = useState(false)
  /** 刚创建的令牌明文 —— 只在这一个组件实例里存在，不写进 store */
  const [revealed, setRevealed] = useState<string | null>(null)

  /*
    参考时刻：挂载时固定一次，整页渲染期间不再变。

    过期判断需要一个"现在"，但渲染期取 Date.now() 是不纯的
    （每次重渲染结果不同，标签会跳变），eslint 也会拦。
    固定在挂载时刻后，"已过期"这个标签在本次访问里是稳定的 ——
    这也是用户预期的：他不指望盯着页面看的时候标签自己翻面。
  */
  const [now] = useState(() => Date.now())

  return (
    <div className="flex flex-col gap-2 border-t border-line pt-panel">
      <div className="flex flex-wrap items-center justify-between gap-tight">
        <div className="flex items-center gap-1.5">
          <ShieldCheck size={13} strokeWidth={1.75} className="text-ink-faint" aria-hidden="true" />
          <span className="text-xs font-medium text-ink">API 令牌</span>
        </div>
        {!creating && revealed === null && (
          <button
            type="button"
            onClick={() => setCreating(true)}
            className="inline-flex h-7 items-center gap-1 rounded-sm border border-line px-2 text-2xs text-ink-muted transition-ui duration-fast hover:border-accent/50 hover:bg-accent-wash hover:text-ink"
          >
            <Plus size={10} strokeWidth={2} aria-hidden="true" />
            创建令牌
          </button>
        )}
      </div>

      <p className="text-2xs text-ink-faint">
        用于 CI、脚本等自动化场景。令牌等同于账号凭据，请只授予必要权限并妥善保存。
      </p>

      {/* 一次性明文展示：放在列表上方抢占注意力，且必须先处理掉才能继续 */}
      {revealed !== null && <TokenReveal plaintext={revealed} onDone={() => setRevealed(null)} />}

      {creating && (
        <TokenCreator
          onCreated={(plaintext) => {
            setCreating(false)
            setRevealed(plaintext)
          }}
          onCancel={() => setCreating(false)}
        />
      )}

      {tokens.length === 0 && !creating && revealed === null ? (
        <p className="rounded-sm border border-dashed border-line-strong p-3 text-center text-2xs text-ink-faint">
          还没有创建过令牌。
        </p>
      ) : (
        tokens.length > 0 && (
          <ul className="flex flex-col">
            {/* 表头仅宽屏 —— 没有表头的话右侧几个定宽列不知道是什么 */}
            <li
              aria-hidden="true"
              className="hidden items-center gap-panel border-b border-line py-2 text-2xs text-ink-faint lg:flex"
            >
              <span className="min-w-0 flex-1">用途</span>
              <span className="w-40 shrink-0">权限</span>
              <span className="w-28 shrink-0">最后使用</span>
              <span className="w-28 shrink-0">有效期至</span>
              <span className="w-16 shrink-0 text-right">操作</span>
            </li>
            {tokens.map((token) => (
              <TokenRow key={token.id} token={token} now={now} />
            ))}
          </ul>
        )
      )}
    </div>
  )
}

function TokenRow({ token, now }: { token: ApiToken; now: number }) {
  const [confirming, setConfirming] = useState(false)
  const [revoking, setRevoking] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /*
    过期/轮换判断走 lib/secret 的纯函数，`now` 由父组件传入。

    ★ 为什么不在这一行里直接 Date.now()：
      渲染必须是纯的 —— 每次重渲染都得到不同的"现在"，
      同一个令牌的"已过期"标签会在两次渲染之间跳变。
      eslint 的 react-hooks/purity 会拦下它，而它确实是个真 bug。
  */
  const state = tokenState(token, now)
  const expired = state === 'expired'
  const stale = state === 'stale'

  return (
    <li className="border-b border-line/60 last:border-b-0">
      <div className="flex flex-col gap-tight py-2.5 lg:flex-row lg:items-center lg:gap-panel lg:py-2">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="flex flex-wrap items-center gap-1.5">
            <span className="text-sm text-ink">{token.label}</span>
            {/* 只显示前缀 —— 完整值在类型上就不存在于这里 */}
            <span className="mono-ui text-ink-faint">{token.prefix}…</span>
            {expired && (
              <span className="rounded-xs border border-signal-risk/40 px-1 text-2xs text-signal-risk">
                已过期
              </span>
            )}
            {stale && !expired && (
              <span className="rounded-xs border border-signal-warn/40 px-1 text-2xs text-signal-warn">
                建议轮换
              </span>
            )}
          </span>
          <span className="text-2xs text-ink-faint lg:hidden">
            创建于 {formatRelativeTime(token.createdAt)}
          </span>
        </div>

        <span className="flex shrink-0 flex-wrap gap-1 lg:w-40">
          {token.scopes.map((scope) => (
            <span
              key={scope}
              title={TOKEN_SCOPE_NOTE[scope]}
              className="rounded-xs border border-line px-1 text-2xs text-ink-muted"
            >
              {TOKEN_SCOPE_LABEL[scope]}
            </span>
          ))}
        </span>

        <span className="shrink-0 text-2xs text-ink-faint lg:w-28">
          {token.lastUsedAt ? formatRelativeTime(token.lastUsedAt) : '从未使用'}
        </span>

        <span className="shrink-0 text-2xs text-ink-faint lg:w-28">
          {token.expiresAt ? new Date(token.expiresAt).toLocaleDateString('zh-CN') : '永不过期'}
        </span>

        <span className="flex shrink-0 items-center justify-end gap-1 lg:w-16">
          {confirming ? (
            <>
              <button
                type="button"
                disabled={revoking}
                onClick={async () => {
                  setRevoking(true)
                  const result = await revokeToken(token.id)
                  setRevoking(false)
                  if (result.status === 'failed') setError(result.error)
                }}
                /*
                  禁用改逐层换色：撤掉风险底 + 文字降到 `ink-faint`
                  （暗 5.94:1 / 亮 5.68:1）。按钮里实时写着「吊销中…」，
                  信息在文字里，不需要靠一块压暗的红底来提示状态。
                */
                className="inline-flex h-7 items-center rounded-sm bg-signal-risk/15 px-2 text-2xs text-signal-risk transition-ui duration-fast hover:bg-signal-risk/25 disabled:cursor-not-allowed disabled:bg-transparent disabled:text-ink-faint disabled:hover:bg-transparent"
              >
                {revoking ? '吊销中…' : '确认吊销'}
              </button>
              <button
                type="button"
                onClick={() => setConfirming(false)}
                className="inline-flex h-7 items-center rounded-sm px-1.5 text-2xs text-ink-faint transition-ui duration-fast hover:text-ink"
              >
                取消
              </button>
            </>
          ) : (
            <DangerButton onClick={() => setConfirming(true)}>吊销</DangerButton>
          )}
        </span>
      </div>

      {/* 吊销不可逆，后果要写在按钮旁边 */}
      {confirming && (
        <p className="pb-2.5 text-2xs text-signal-warn">
          吊销后使用这个令牌的集成会立即开始失败，且无法恢复。
        </p>
      )}

      {error && (
        <p role="alert" className="pb-2.5 text-2xs text-signal-risk">
          {error}
        </p>
      )}
    </li>
  )
}

// ─────────────────────────────────────────────
// 创建令牌
// ─────────────────────────────────────────────

function TokenCreator({
  onCreated,
  onCancel,
}: {
  onCreated: (plaintext: string) => void
  onCancel: () => void
}) {
  const [label, setLabel] = useState('')
  const [scopes, setScopes] = useState<Set<TokenScope>>(new Set(['read']))
  const [expiry, setExpiry] = useState<TokenExpiry>('90d')
  const [touched, setTouched] = useState(false)
  // 接入真实后端后新增：创建是一次真实网络请求，需要 pending 与失败态
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  // 挂载即展开，带进视野（同 provider / 通知渠道编辑器）
  const creatorRef = useRevealOnExpand<HTMLDivElement>(true)

  const labelError = label.trim().length === 0 ? '请填写用途，将来要靠它辨认是哪一个令牌' : null
  const scopeError = scopes.size === 0 ? '至少选择一项权限' : null
  const hasError = labelError !== null || scopeError !== null

  const handleCreate = async () => {
    setTouched(true)
    if (hasError || creating) return

    setCreating(true)
    setCreateError(null)
    try {
      // ★ 完整令牌只在这一跳的响应里存在（后端只存哈希）。
      //   拿到就立刻交给上层做一次性展示，**不写进 store**。
      const { plaintext } = await createToken({
        label: label.trim(),
        scopes: [...scopes],
        expiresAt: expiryToIso(expiry, Date.now()),
      })
      onCreated(plaintext)
    } catch (error) {
      // 草稿保留：用户填的用途和权限不该因为一次网络失败被清空
      setCreateError(error instanceof Error ? error.message : '创建令牌失败，请稍后重试。')
    } finally {
      setCreating(false)
    }
  }

  return (
    <div
      className="flex flex-col gap-panel rounded-sm border border-accent/40 bg-accent-wash/40 p-3"
      ref={creatorRef}
    >
      <div className="flex items-center justify-between gap-tight">
        <span className="text-xs text-ink">创建 API 令牌</span>
        <button
          type="button"
          onClick={onCancel}
          aria-label="取消"
          className="rounded-sm p-1 text-ink-faint transition-ui duration-fast hover:text-ink"
        >
          <X size={13} strokeWidth={2} aria-hidden="true" />
        </button>
      </div>

      <Field label="用途" htmlFor="token-label" required error={touched ? labelError : null}>
        <TextInput
          id="token-label"
          value={label}
          onChange={setLabel}
          placeholder="如：CI 流水线"
          error={touched ? labelError : null}
        />
      </Field>

      <Field
        label="权限范围"
        help="按最小必要授予 —— 令牌能做的事，等于拿到它的人能做的事。"
        error={touched ? scopeError : null}
      >
        <div className="flex flex-col gap-1.5">
          {(['read', 'scan', 'admin'] as TokenScope[]).map((scope) => (
            <Toggle
              key={scope}
              id={`scope-${scope}`}
              label={TOKEN_SCOPE_LABEL[scope]}
              hint={TOKEN_SCOPE_NOTE[scope]}
              checked={scopes.has(scope)}
              onChange={(checked) => {
                setScopes((prev) => {
                  const nextScopes = new Set(prev)
                  if (checked) nextScopes.add(scope)
                  else nextScopes.delete(scope)
                  return nextScopes
                })
              }}
            />
          ))}
        </div>
      </Field>

      <Field label="有效期" htmlFor="token-expiry" help="短期令牌泄露后的暴露窗口更小。">
        <div className="flex flex-wrap gap-1" id="token-expiry">
          {(['30d', '90d', '1y', 'never'] as TokenExpiry[]).map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => setExpiry(key)}
              aria-pressed={expiry === key}
              className={cn(
                'h-7 rounded-sm border px-2 text-2xs transition-ui duration-fast',
                expiry === key
                  ? 'border-accent/50 bg-accent-wash text-ink'
                  : 'border-line text-ink-muted hover:border-line-strong hover:text-ink',
              )}
            >
              {TOKEN_EXPIRY_LABEL[key]}
            </button>
          ))}
        </div>
      </Field>

      {expiry === 'never' && (
        <p className="flex items-start gap-1.5 text-2xs text-signal-warn">
          <AlertTriangle size={11} strokeWidth={2} className="mt-0.5 shrink-0" aria-hidden="true" />
          永不过期的令牌一旦泄露就长期有效，且往往会被遗忘。仅在自动化流水线无法处理轮换时使用。
        </p>
      )}

      {/* 创建失败要留住草稿并说清原因 —— 这一跳是真实的网络请求 */}
      {createError && (
        <p role="alert" className="text-2xs text-signal-risk">
          {createError}
        </p>
      )}

      <div className="flex items-center gap-tight">
        <button
          type="button"
          onClick={handleCreate}
          disabled={creating}
          aria-busy={creating}
          /*
            禁用配色与 `Button` 的 primary 一致（`bg-accent-dim` + `ink-muted`，
            暗 4.94:1 / 亮 5.70:1）。这里不用基元是因为它自带 `text-white` +
            `h-9`、而设置页这一排的其它按钮也是 h-9 手写的 —— 换基元会连正常态
            颜色一起变（`accent-deep` → `accent`），那是另一件事。
          */
          className="press inline-flex h-9 items-center rounded-sm bg-accent-deep px-3.5 text-sm text-white transition-ui duration-fast hover:bg-accent disabled:cursor-not-allowed disabled:bg-accent-dim disabled:text-ink-muted disabled:hover:bg-accent-dim"
        >
          {creating ? '正在创建…' : '创建令牌'}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="inline-flex h-9 items-center rounded-sm px-2.5 text-sm text-ink-muted transition-ui duration-fast hover:text-ink"
        >
          取消
        </button>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────
// 一次性明文展示
// ─────────────────────────────────────────────

/**
 * 令牌明文展示 —— **只出现一次**。
 *
 * ★ 关于"防止误关"的取舍，这里踩过一次坑，记下来：
 *
 *   最初的做法是：关闭按钮在复制成功前一直禁用。意图是好的
 *   （防止用户随手关掉、永远失去令牌），但它造成了一个**死锁** ——
 *   实测中剪贴板写入失败时（非 HTTPS、浏览器权限被拒、iframe 沙箱，
 *   这些在真实环境里都会发生），关闭按钮永远点不了，面板无法关闭。
 *   把唯一的出口锁死，比"可能误关"严重得多：用户被卡在一个没有出路的界面上。
 *
 *   改成**二次确认**：关闭时若还没确认保存过，先就地追问一句，
 *   确认后才关。保护还在（不会一碰就关），但永远留了一条出路 ——
 *   与项目里删除令牌/删除报告用的是同一套就地确认模式。
 *
 *   另外，剪贴板失败时要**告诉用户怎么办**（手动选中复制），
 *   而不是默默无反应让他反复点复制按钮。
 */
function TokenReveal({ plaintext, onDone }: { plaintext: string; onDone: () => void }) {
  const [copied, setCopied] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)
  const [confirmingClose, setConfirmingClose] = useState(false)

  const handleCopy = async () => {
    // `navigator.clipboard` 在非安全上下文里**不存在**（本系统按 http +
    // 局域网 IP 部署，实测），走降级链。见 `@/lib/clipboard` 的说明。
    if (await copyText(plaintext)) {
      setCopied(true)
      setCopyFailed(false)
      return
    }
    /*
      降级链都失败。这不是异常路径 —— 非安全上下文（http）、权限被拒、
      iframe 沙箱都会走到这里。**不能静默失败**：用户会以为复制成功了，
      然后关掉面板丢掉令牌（这个令牌只显示这一次）。
    */
    setCopyFailed(true)
  }

  const handleCloseClick = () => {
    if (copied) {
      onDone()
      return
    }
    // 还没确认保存过 —— 追问一次，而不是直接关掉
    setConfirmingClose(true)
  }

  return (
    <div
      role="alertdialog"
      aria-labelledby="token-reveal-title"
      className="flex flex-col gap-panel rounded-sm border border-signal-warn/50 bg-signal-warn/[0.07] p-3"
    >
      <div className="flex items-start gap-1.5">
        <AlertTriangle
          size={14}
          strokeWidth={2}
          className="mt-0.5 shrink-0 text-signal-warn"
          aria-hidden="true"
        />
        <div className="flex flex-col gap-0.5">
          <h3 id="token-reveal-title" className="text-xs font-medium text-ink">
            令牌已创建 —— 这是唯一一次显示完整值
          </h3>
          <p className="text-2xs text-ink-muted">
            离开这个面板后就无法再次查看。请现在复制并保存到安全的地方；若遗失，只能吊销后重新创建。
          </p>
        </div>
      </div>

      <div className="flex flex-col gap-1.5">
        <code
          // break-all：令牌很长，窄屏不换行会被截断，而截断的令牌是没法用的
          className="block select-all break-all rounded-sm border border-line bg-surface-sunken px-3 py-2.5 font-mono text-sm text-ink"
        >
          {plaintext}
        </code>

        {confirmingClose ? (
          /* 二次确认：说清后果，给两条出路 */
          <div className="flex flex-col gap-tight rounded-sm border border-signal-risk/40 bg-signal-risk/[0.06] p-2.5">
            <p className="text-2xs text-ink">
              尚未确认已保存。关闭后这个令牌无法再次查看，只能吊销后重新创建。
            </p>
            <div className="flex flex-wrap items-center gap-tight">
              <button
                type="button"
                onClick={() => setConfirmingClose(false)}
                className="press inline-flex h-8 items-center rounded-sm bg-accent-deep px-3 text-xs text-white transition-ui duration-fast hover:bg-accent"
              >
                返回，我先复制
              </button>
              <button
                type="button"
                onClick={onDone}
                className="inline-flex h-8 items-center rounded-sm px-2.5 text-xs text-ink-muted transition-ui duration-fast hover:text-ink"
              >
                我知道，仍要关闭
              </button>
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-tight">
            <button
              type="button"
              onClick={handleCopy}
              className="press inline-flex h-8 items-center gap-1.5 rounded-sm bg-accent-deep px-3 text-xs text-white transition-ui duration-fast hover:bg-accent"
            >
              {copied ? (
                <>
                  <Check size={12} strokeWidth={2.5} aria-hidden="true" />
                  已复制
                </>
              ) : (
                <>
                  <Copy size={12} strokeWidth={2} aria-hidden="true" />
                  复制令牌
                </>
              )}
            </button>

            <button
              type="button"
              onClick={handleCloseClick}
              className="inline-flex h-8 items-center rounded-sm border border-line px-3 text-xs text-ink-muted transition-ui duration-fast hover:border-line-strong hover:text-ink"
            >
              我已保存，关闭
            </button>
          </div>
        )}

        {copyFailed && !confirmingClose && (
          /* 复制失败时必须给出下一步动作，否则用户只会反复点按钮 */
          <p role="alert" className="text-2xs text-signal-warn">
            浏览器拒绝了剪贴板访问。请在上方文字上三击选中，用 Ctrl/Cmd + C 手动复制。
          </p>
        )}
      </div>
    </div>
  )
}

export default AccountPanel
