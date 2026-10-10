import AuthShell from '@/components/layout/AuthShell'
import { Field, TextInput } from '@/components/settings/Field'
import ErrorState from '@/components/ui/ErrorState'
import { bootstrapAuth, clearAuthError, login, useAuth } from '@/data/auth-store'
import { ApiError } from '@/lib/api'
import { Loader2, LogIn } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'

/**
 * 登录页（契约 §8）。
 *
 * 单口令框 —— 没有用户名。契约 §0.3 定的是**单管理员**，
 * 加一个用户名框只会让人以为可以有多账号，而实际它恒等于同一个值。
 *
 * ## 两个状态在这里汇合，处理方式不同
 *
 * | 状态 | 界面 | 为什么 |
 * | --- | --- | --- |
 * | `login`（后端在，没登录） | 表单 | 正常路径 |
 * | `offline`（后端根本没应答） | **不显示表单**，显示连接失败 + 重试 | 显示表单是误导：用户会去输口令，然后拿到一个和口令毫无关系的失败 |
 *
 * 第二种在开发态极常见（忘了起后端）。把它和"口令错了"混在一起，
 * 排查方向会被完全带偏 —— 这也是 `auth-store` 单独给它一个 phase 的原因。
 */
export function LoginPage() {
  const { phase, error, pending } = useAuth()
  const [password, setPassword] = useState('')
  const formRef = useRef<HTMLDivElement>(null)

  // 自动聚焦：这一页唯一要做的事就是输口令。
  // ⚠️ 不通过 ref 透传是因为 `TextInput` 是设置页的共享组件，
  //    为登录页单独给它加一个 ref 转发会把改动面扩大到那一页。
  useEffect(() => {
    if (phase !== 'login') return
    formRef.current?.querySelector('input')?.focus()
  }, [phase])

  // 切换账号时不该留着上一次的报错
  useEffect(() => {
    if (password.length > 0) clearAuthError()
  }, [password])

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    if (pending || password.length === 0) return
    await login(password)
    // 成功后 store 变 `ready`，路由守卫会完成跳转 —— 这里**不**手动 navigate，
    // 否则"谁负责跳转"就有两处，而它们会在会话过期时打架。
  }

  if (phase === 'offline') {
    return (
      <AuthShell
        title="连不上后端服务"
        description="界面已经就绪，但它需要 Suvex 后端在运行。"
      >
        {/*
          ★ 这里要传一个**真的 ApiError**（`status: 0`），不能用普通 Error：
            `ErrorState` 靠 `isNetworkError` 区分"连不上"与"后端说不行"，
            而这两者的图标、标题、下一步动作都不同。传普通 Error 会退化成
            「加载失败」+ 一个对解决问题毫无帮助的重试按钮。
        */}
        <ErrorState
          error={new ApiError('NETWORK_ERROR', error ?? '连接不上后端服务。', 0)}
          onRetry={() => void bootstrapAuth()}
        />
      </AuthShell>
    )
  }

  return (
    <AuthShell
      title="登录"
      description="输入管理员口令继续。系统只有一个账号，没有用户名。"
    >
      <div ref={formRef}>
        <form onSubmit={handleSubmit} className="flex flex-col gap-panel">
          <Field label="口令" htmlFor="login-password" required>
            <TextInput
              id="login-password"
              type="password"
              value={password}
              onChange={setPassword}
              placeholder="输入管理员口令"
              // `current-password` 而不是 `new-password`：这是登录，
              // 让密码管理器正确地"填入已存的口令"而不是"提议生成一个新的"
              autoComplete="current-password"
              error={error}
            />
          </Field>

          <button
            type="submit"
            disabled={pending || password.length === 0}
            aria-busy={pending}
            className="press inline-flex h-9 items-center justify-center gap-tight rounded-sm bg-accent px-3 text-sm font-medium text-canvas transition-ui duration-fast hover:bg-accent-strong disabled:cursor-not-allowed disabled:opacity-50"
          >
            {pending ? (
              <>
                <Loader2 size={14} strokeWidth={2} className="animate-spin" aria-hidden="true" />
                正在登录…
              </>
            ) : (
              <>
                <LogIn size={14} strokeWidth={2} aria-hidden="true" />
                登录
              </>
            )}
          </button>
        </form>
      </div>

      {/*
        ⚠️ 说明限流。契约 §3.1 要求登录必须限流（同一来源 5 次/分钟），
        而**被限流时用户会看到 429**。提前说一句，那条错误才不像故障。
      */}
      <p className="mt-panel text-2xs text-ink-faint">
        连续输错会被限流（每分钟 5 次），这是防止口令被在线爆破。
      </p>
    </AuthShell>
  )
}

export default LoginPage
