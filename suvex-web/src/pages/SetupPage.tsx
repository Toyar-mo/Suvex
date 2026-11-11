import AuthShell from '@/components/layout/AuthShell'
import { Field, TextInput } from '@/components/settings/Field'
import PasswordStrength from '@/components/ui/PasswordStrength'
import { Button } from '@/components/ui/Button'
import { checkPassword } from '@/data/settings'
import { clearAuthError, setupAdmin, useAuth } from '@/data/auth-store'
import { useI18n } from '@/i18n'
import { T } from '@/i18n/T'
import { ShieldCheck } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'

/**
 * 初始化页（契约 §8）—— **只会出现一次**。
 *
 * 系统还没有任何账号时显示。建好**第一个管理员**后，`POST /auth/setup`
 * 会自动登录（契约 §3.1），并且这个端点在已初始化后**彻底关闭** ——
 * 否则它就是一个"任意人可重设管理员凭据"的后门。
 *
 * ## v0.24：第一个账号是**管理员**，且要起个用户名
 *
 * 多用户之后账号分 `admin` / `viewer` 两档，而这一页建的**固定是管理员** ——
 * 刚装好的系统必须有人能改设置、能加别的账号，否则是个自锁。
 * 界面得把这件事说清楚：用户在这一步并不知道"第一个"意味着什么权限，
 * 事后才发现自己建了个只能看的账号会更困惑。
 *
 * 用户名预填 `admin`：这一步的默认选择就是"直接用 admin"，
 * 让人对着一个空框猜"该叫什么"是没必要的摩擦。它是个普通输入框，
 * 想改成别的（`ops`、自己的名字）直接改 —— 用户名只用于登录与留痕。
 *
 * ## 为什么这一页要显示强度条
 *
 * 这是整个系统唯一一次设置凭据的机会，而这个口令就是全部的安全性：
 * 管理员、无二次验证。弱口令 = 平台被接管 =
 * **攻击者可以拿它替自己发包**（契约 §0.3 讲了这个平台真正的危害）。
 *
 * 所以强度不是"建议"，是硬门槛：`checkPassword().acceptable` 为假时
 * 提交按钮直接禁用，并说明差哪一条。校验逻辑来自 `checkPassword`
 * 那**唯一一处实现**，这里不重写规则。
 */
export function SetupPage() {
  const { t } = useI18n()
  const { error, pending } = useAuth()
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [showHint, setShowHint] = useState(false)
  const formRef = useRef<HTMLDivElement>(null)

  const check = useMemo(() => checkPassword(password), [password])

  useEffect(() => {
    // 焦点落在**用户名**框（第一个输入框），不是口令框。
    // 用户名的默认值已填好，改不改由用户决定 —— 但要先把焦点给他。
    formRef.current?.querySelector('input')?.focus()
  }, [])

  useEffect(() => {
    if (username.length > 0 || password.length > 0) clearAuthError()
  }, [username, password])

  const mismatch = confirm.length > 0 && confirm !== password
  const canSubmit = username.length > 0 && check.acceptable && confirm === password && !pending

  /** 按钮为什么点不了 —— 这个项目要求禁用必须给出原因（见 WizardChrome） */
  const disabledReason = pending
    ? t('setup.submitting')
    : username.length === 0
      ? t('setup.needUsername')
      : password.length === 0
        ? t('setup.needPassword')
        : !check.acceptable
          ? t('setup.weakPassword')
          : confirm.length === 0
            ? t('setup.needConfirm')
            : mismatch
              ? t('setup.mismatch')
              : null

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setShowHint(true)
    if (!canSubmit) return
    // 用户名原样转发（不做 trim / toLowerCase），归一化由后端一处完成
    await setupAdmin(username, password)
    // 成功后 store 变 `ready`，路由守卫完成跳转
  }

  return (
    <AuthShell
      title={t('setup.title')}
      description={t('setup.desc')}
    >
      <div ref={formRef}>
        <form onSubmit={handleSubmit} className="flex flex-col gap-panel">
          {/*
            用户名排在口令前面，与登录页同一个顺序 —— 两页的字段次序不一致
            会让"先填哪个"变成每次都要重新判断的事。
          */}
          <Field label={t('setup.username')} htmlFor="setup-username" required help={t('setup.usernameHelp')}>
            <TextInput
              id="setup-username"
              value={username}
              onChange={setUsername}
              placeholder="admin"
              // 这里**不**传 `autoComplete="username"`：这一页是**创建**账号，
              // 让密码管理器把某个已存的登录名填进来，会覆盖掉预填的 `admin`。
              // （`TextInput` 的默认值就是 `off`。）
            />
          </Field>

          <Field
            label={t('setup.password')}
            htmlFor="setup-password"
            required
            error={showHint && !check.acceptable && password.length > 0 ? t('setup.passwordError') : null}
          >
            <TextInput
              id="setup-password"
              type="password"
              value={password}
              onChange={setPassword}
              placeholder={t('setup.passwordPlaceholder')}
              autoComplete="new-password"
            />
          </Field>

          {/* 强度与要求清单：只在开始输入后出现，空表单时不占地方 */}
          {password.length > 0 && <PasswordStrength check={check} />}

          <Field
            label={t('setup.confirm')}
            htmlFor="setup-confirm"
            required
            error={mismatch ? t('setup.mismatch') : null}
          >
            <TextInput
              id="setup-confirm"
              type="password"
              value={confirm}
              onChange={setConfirm}
              placeholder={t('setup.confirmPlaceholder')}
              autoComplete="new-password"
            />
          </Field>

          {error && (
            <p role="alert" className="text-2xs text-signal-risk">
              {error}
            </p>
          )}

          <div className="flex flex-col gap-tight">
            <Button
              type="submit"
              variant="primary"
              size="md"
              disabled={!canSubmit}
              loading={pending}
              loadingLabel={t('setup.loadingLabel')}
              title={disabledReason ?? undefined}
            >
              <ShieldCheck size={14} strokeWidth={2} aria-hidden="true" />
              <T k="setup.submit" />
            </Button>

            {/* 禁用必须给原因：`title` 在触屏上读不到，所以另有一行文字 */}
            {disabledReason && !pending && (
              <p className="text-2xs text-ink-faint">{disabledReason}</p>
            )}
          </div>
        </form>
      </div>

      <p className="mt-panel text-2xs text-ink-faint">
        <T k="setup.footer" />
      </p>
    </AuthShell>
  )
}

export default SetupPage
