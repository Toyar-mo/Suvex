import { ThemeToggle } from '@/components/layout/ThemeToggle'
import type { ReactNode } from 'react'

/**
 * 认证页的外壳（登录 / 初始化共用）。
 *
 * ## 为什么这里**没有** Hero
 *
 * 契约 §8 的原话：
 *
 * > 登录页是这个产品给人的第一印象之一，但它**不需要** Hero ——
 * > 那会让"赶紧进去干活"的用户多等一屏。
 *
 * 这话是对的，而且理由比"简洁好看"更硬：来这个页面的人**已经决定要用它了**，
 * 他们要做的是输一个口令。全屏品牌叙事在这一步是纯粹的阻力。
 *
 * 所以这里只有：产品名 → 这次要做什么 → 表单。三屏信息压成一屏。
 *
 * ## 为什么用 `min-h-dvh` 而不是 `h-dvh`
 *
 * 手机上键盘弹出会把可视高度压到很小。`h-dvh` + `justify-center` 会让
 * 表单被挤到看不见，而 `min-h-dvh` 允许它溢出并自然滚动 ——
 * 用户能滚到输入框。矮屏笔记本（768px 高）同理。
 */

export interface AuthShellProps {
  title: string
  description?: ReactNode
  children: ReactNode
}

export function AuthShell({ title, description, children }: AuthShellProps) {
  return (
    <div className="relative flex min-h-dvh flex-col items-center justify-center bg-canvas px-gutter py-bay">
      {/*
        ★ 主题切换放右上角（与站内 `TopStatusBar` 上的那个同源，同一个组件）。

        为什么这里非得单独挂一个：登录页与初始化页**不渲染 AppShell**，
        所以拿不到状态条上那个按钮 —— 而它们恰恰是用户看到的**第一屏**。
        亮色是他上次选的、这次系统/环境变了想切回去，却没有入口，
        只能先登录再说。这一屏本来就只有"输一个口令"一件事，
        多一个角落按钮不构成阻力（§8 反对的是全屏品牌叙事，不是这个）。

        用绝对定位而不是塞进那个居中列：这一列是"产品名 → 做什么 → 表单"
        的一条竖向叙事，把切换器插进去会打断它。
      */}
      <div className="absolute right-gutter top-gutter">
        <ThemeToggle />
      </div>

      <div className="w-full max-w-[380px]">
        {/* 产品名 —— 用等宽字，和全站的技术调性一致 */}
        <div className="mono-ui text-2xs uppercase tracking-[0.18em] text-ink-faint">Suvex</div>

        <h1 className="mt-tight text-xl font-medium text-ink">{title}</h1>

        {description && (
          <p className="mt-tight text-sm text-ink-muted">{description}</p>
        )}

        <div className="mt-bay">{children}</div>
      </div>
    </div>
  )
}

export default AuthShell
