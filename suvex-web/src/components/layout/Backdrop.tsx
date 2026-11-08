import { lazy, Suspense } from 'react'

import { useBackdrop } from '@/data/backdrop-store'

/**
 * 全站画布氛围层（v1.75）：按用户选的档位画不同的底。
 *
 * ## 为什么"纯色"是 `null` 而不是一层透明的 div
 *
 * 什么都不画时，最诚实的实现就是不挂节点 —— 一层 `opacity: 0` 的 div
 * 仍然占一次合成，而这一档存在的意义恰恰是"关掉背景"。
 *
 * ## 为什么点阵场保持懒加载
 *
 * 它是唯一带 canvas 与动画的一档（见 `AppShell` 文件头）。其余三档都是
 * 纯 CSS 背景，直接渲染没有成本；把懒加载留在原处，是为了让**默认档**
 * 的首屏路径与今天完全一致。
 */
const AgentField = lazy(() => import('@/components/effects/AgentField'))

export function Backdrop() {
  const backdrop = useBackdrop()

  if (backdrop === 'plain') return null

  if (backdrop === 'field') {
    return (
      <Suspense fallback={null}>
        <AgentField />
      </Suspense>
    )
  }

  return (
    <div
      aria-hidden="true"
      className={
        backdrop === 'aurora' ? 'backdrop-aurora h-full w-full' : 'backdrop-grain h-full w-full'
      }
    />
  )
}

export default Backdrop
