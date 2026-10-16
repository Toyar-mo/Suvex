import { useMediaQuery } from '@/hooks/useMediaQuery'
import { usePrefersReducedMotion } from '@/hooks/usePrefersReducedMotion'
import { useEffect, useState } from 'react'
import DotGrid from './DotGrid'
import Threads from './Threads'

/**
 * 页面底层：agent 的「场」。
 *
 * 四层，各自负责一件事，顺序不能换：
 *   1. 底色      近黑画布
 *   2. Threads   缓慢流过的线 = 数据正在通过，是背景里唯一的持续动效
 *   3. DotGrid   跟随光标的点阵 = 这是可交互的现场，不是一张图
 *   4. vignette  把中心留亮、四角压暗，让内容层（后面加上来的）有地方站
 *
 * ★ 第 3 层曾在 2026-10-15 因许可证原因移除（它当时是全仓唯一依赖 `gsap`
 *   的地方，而 gsap 用的是 GreenSock 自有许可、非 OSI 开源）。
 *   2026-10-16 恢复 —— **物理改成自研、不依赖 gsap**，所以许可仍然干净。
 *   效果与调用参数都保留，见 `DotGrid.tsx` 的「本地改动 7」。
 *
 * ── 关于 Threads 的色值（踩过一次）─────────────────────────────
 * Threads 的 uColor 是 ogl 的 Color，而 ogl **不做色彩空间转换**
 * （Renderer 里只处理 premultiplied alpha，没有 outputColorSpace），
 * 着色器算完直接写进 sRGB 后备缓冲。所以这里必须传 sRGB 归一值。
 *
 * 我最初按 three.js 的习惯传了线性空间的值，紫被压掉一档、绿被压掉五档，
 * 线场在近黑底色上暗到几乎看不见。
 *
 * 结论：这里填的就是 token 颜色的 r/255，不做任何转换。
 * 换主色时同步 tailwind.config.js 与下面这两个常量。
 */

/** accent DEFAULT #8B7CF6 → (139,124,246)/255 */
const THREADS_ACCENT_SRGB: [number, number, number] = [0.5451, 0.4863, 0.9647]

/** accent deep #6D5CE0 → (109,92,224)/255 —— 窄屏用深一档，线条更安静 */
const THREADS_ACCENT_DEEP_SRGB: [number, number, number] = [0.4275, 0.3608, 0.8784]

export function AgentField() {
  const prefersReducedMotion = usePrefersReducedMotion()
  const isDesktop = useMediaQuery('(min-width: 1024px)')

  const [threadsReady, setThreadsReady] = useState(false)

  // 背景逐层淡入，避免首帧硬闪。减少动效时直接返回静态层，不参与过渡。
  useEffect(() => {
    if (prefersReducedMotion) return
    const id = window.setTimeout(() => setThreadsReady(true), 220)
    return () => window.clearTimeout(id)
  }, [prefersReducedMotion])

  // 减少动效：一张静态图就够，不挂任何动画循环。
  if (prefersReducedMotion) {
    return (
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-canvas"
        style={{
          backgroundImage:
            'radial-gradient(120% 90% at 50% 0%, rgba(139,124,246,0.10) 0%, rgba(139,124,246,0) 60%)',
        }}
      />
    )
  }

  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
      {/* 2 · 线场：全场唯一的持续运动。
          整层压到 0.55 透明度 —— 满强度的紫罗兰波浪会变成「发光装饰」，
          读起来像 AI 产品落地页，而不是一个正在运行的工具。
          它是氛围，内容才是主角；要再收先看 §5 的动效纪律。
          底部对齐 + 高度 78%：线场默认走在画布正中，正好压在标题那一行上。
          把它沉到下方，线就成了托住内容的「地平线」，标题落在它上方的空处。 */}
      <div
        className="absolute inset-x-0 bottom-0 h-[78%] transition-opacity duration-slow ease-enter"
        style={{ opacity: threadsReady ? 0.55 : 0 }}
      >
        <Threads
          color={isDesktop ? THREADS_ACCENT_SRGB : THREADS_ACCENT_DEEP_SRGB}
          amplitude={isDesktop ? 1 : 1.35}
          distance={0}
          enableMouseInteraction={isDesktop}
        />
      </div>

      {/* 3 · 点阵：只在大屏挂载，移动端省电省内存。
          baseColor 比 line token 高一档、点也放大到 1.5px —— 2px 的点在
          近黑底上会被抗锯齿抹掉，扫不出这是网格。

          ★ shockStrength={0}（任务 8 改）：上游的点击冲击波监听挂在 `window` 上，
            意味着页面上每一次真实交互（点按钮、勾选、展开行）都会在背景里炸一次。
            用户点「启动扫描」时背景震一下，这不是反馈而是噪音 ——
            而且每次点击都要遍历全部点阵（约 2000 个点）算距离。
            关掉之后**连监听都不注册**（见 DotGrid 内的改动），光晕跟随保留：
            那才是"这是可交互的现场"的有效信号，而且它节流到 50ms。 */}
      {isDesktop && (
        <div
          className="absolute inset-0 transition-opacity duration-slow ease-enter"
          style={{ opacity: threadsReady ? 1 : 0 }}
        >
          <DotGrid
            dotSize={3}
            gap={30}
            baseColor="#333B48"
            activeColor="#A99BFF"
            proximity={150}
            speedTrigger={80}
            shockRadius={220}
            shockStrength={0}
            resistance={900}
            returnDuration={1.6}
          />
        </div>
      )}

      {/* 4 · 压角：让中心成为内容可以落上去的区。
          中心也压了一点（0 → 0.15）—— 完全透明的中场会让波浪从标题底下穿过，
          文字压在流线上就读不清了。 */}
      <div
        className="absolute inset-0"
        style={{
          background:
            'radial-gradient(130% 100% at 50% 6%, rgba(8,9,12,0.15) 0%, rgba(8,9,12,0.44) 62%, rgba(8,9,12,0.86) 100%)',
        }}
      />
    </div>
  )
}

export default AgentField
