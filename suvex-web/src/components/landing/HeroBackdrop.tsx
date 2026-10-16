import { useAfterPaint } from '@/hooks/useAfterPaint'
import { useMediaQuery } from '@/hooks/useMediaQuery'
import { usePrefersReducedMotion } from '@/hooks/usePrefersReducedMotion'
import { useTheme } from '@/data/theme-store'
import { Suspense, lazy, useMemo } from 'react'

/**
 * 首页 Hero 的背景「场」。
 *
 * ── 与站内 AgentField 的关系（不是重复，是分工）──────────────────
 * `AgentField` 是全站氛围底：压在 80% 遮罩下面，负责"这个系统一直在跑"。
 * Hero 需要的是**一眼记住的画面**，所以它单独用一套更强的配置：
 *   · 线场占比更高（100% 而不是 78% 配 0.55 透明度）；
 *   · 点阵保留（跟随光标 = 这是可交互的现场）；
 *   · 压角更轻，让线场真的看得见。
 *
 * 代价是 Hero 上**不能同时**出现站内背景 —— 首页独立于 AppShell 渲染，
 * 天然满足（见 LandingPage 的说明）。两个 WebGL 背景叠在同一视口
 * 会掉帧，这条在 design-system §5 里记过。
 *
 * ── ★ 首屏性能：整个背景层都是"可选的" ─────────────────────────
 * 这是 Hero 页面 FCP 的关键设计：
 *
 *   1. **用 `useAfterPaint` 推迟挂载**（双重 rAF）。WebGL 初始化 +
 *      着色器编译是几百毫秒级的同步开销，若和首帧挤在一起，
 *      LCP 会被拖长。等首帧真的落屏之后再挂，标题就永远不会等它。
 *   2. **`lazy()` 让 ogl / Threads / DotGrid 落在独立 chunk**。
 *      首屏 HTML+CSS 里根本不含这些代码 —— 实测背景 chunk 约 20 kB(gzip)，
 *      而主包因此**变小**（这些依赖原本在主包里）。
 *   3. 内容先出现、背景后淡入。顺序是对的：文字是信息，线场是氛围。
 *
 * 减少动效偏好下返回一张静态径向渐变 —— 没有任何动画循环。
 * （不挂载而不是挂载后隐藏：CSS 关动画停不掉 rAF，见 §5「减少动效」。）
 */

// 拆成独立 chunk。这些是 ReactBits 拷贝件 + ogl，都不进首屏关键路径。
const Threads = lazy(() => import('@/components/effects/Threads'))
const DotGrid = lazy(() => import('@/components/effects/DotGrid'))

/**
 * 从 CSS 变量里读主题色。
 *
 * ## ★ 为什么不在这里写死色值
 *
 * 这些颜色**两个主题下不一样**（`--suvex-accent` 暗色 #8B7CF6 / 亮色 #5B4BC4），
 * 而 WebGL 那边拿到的是 JS 数值，没法靠 CSS 自动跟随。写死一份就等于又开了
 * 一个颜色副本 —— 而副本一定漂移（这个项目刚从 `index.css` 与
 * `tailwind.config.js` 各存一份值漂移过一次）。
 *
 * 所以现读计算后的变量：token 存的是 RGB 通道三元组（`139 124 246`），
 * 拆开就是 ogl 要的归一值，包成 `rgb(...)` 就是 CSS 要的颜色。
 *
 * ⚠️ 读不到（SSR、极端沙箱、样式还没加载）时回**暗色**那套 —— 与
 *    `:root` 的兜底方向一致，见 `src/lib/theme.ts` 文件头。
 */
function readTriple(name: string, fallback: string): string {
  try {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
  } catch {
    return fallback
  }
}

function tripleToSrgb(triple: string): [number, number, number] {
  const parts = triple.split(/\s+/).map(Number)
  if (parts.length !== 3 || parts.some((n) => !Number.isFinite(n))) return [0.5451, 0.4863, 0.9647]
  return [parts[0] / 255, parts[1] / 255, parts[2] / 255]
}

export function HeroBackdrop() {
  const prefersReducedMotion = usePrefersReducedMotion()
  const isDesktop = useMediaQuery('(min-width: 1024px)')
  const afterPaint = useAfterPaint()
  const { resolved } = useTheme()

  /*
    配色随主题重算。依赖 `resolved` 是**必须**的：
    WebGL 侧（Threads / DotGrid）拿的是 props，主题一换就得把新值传下去，
    靠 CSS 是传不进去的。
  */
  const palette = useMemo(() => {
    const accent = readTriple('--suvex-accent', '139 124 246')
    const canvas = readTriple('--suvex-canvas', '8 9 12')
    return {
      accentSrgb: tripleToSrgb(accent),
      accentSoft: `rgb(${accent} / 0.16)`,
      accentClear: `rgb(${accent} / 0)`,
      // 底色点阵用描边色、高亮点用强调色 —— 这两个映射在两个主题下都成立
      dotBase: `rgb(${readTriple('--suvex-line-strong', '44 50 61')})`,
      dotActive: `rgb(${readTriple('--suvex-accent-strong', '169 155 255')})`,
      // 压角用画布色：暗色下压暗、亮色下压回页面底色，语义一致
      vignette: canvas,
    }
    /*
      ⚠️ `resolved` 在回调里**没有被引用**，但它不是多余的依赖。

      它是**失效信号**，不是输入：颜色是从 CSS 变量（DOM）现读的，
      而主题一换 `data-theme` 就变了 —— 我们必须重读一次才能拿到新值。
      lint 只能看到"回调里没用它"，看不到这层因果，所以显式豁免。
      删掉这个依赖的后果很具体：切换主题后首页背景**还是旧配色**，
      而它看起来不像 bug，只像"这个背景本来就这样"。
    */
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolved])

  // 减少动效：一张静态渐变收工。不含任何动画循环。
  if (prefersReducedMotion) {
    return (
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0"
        style={{
          background: `radial-gradient(120% 85% at 50% 8%, ${palette.accentSoft} 0%, ${palette.accentClear} 62%)`,
        }}
      />
    )
  }

  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
      {/*
        背景层整体淡入 —— 420ms 是全站唯一允许用 slow 档的地方（§5.1）。
        这里是"画面层次的出现"，不是交互反馈，慢一点才对。
        在 afterPaint 之前这一层是 opacity-0，且**子树还没挂载**
        （下面的条件渲染），所以 WebGL 的初始化不会和首帧抢主线程。

        用 `fade-in`（动 opacity）而不是 `page-in`（动 transform）：
        这一层是**纯装饰** —— 它晚出现或不出现，用户该读到的文字一个都不少。
        所以这里可以用"不可见的初态"；内容层则绝对不行，见 §5.1。
      */}
      {afterPaint && (
        <div className="absolute inset-0 animate-fade-in">
          <Suspense fallback={null}>
            {/* 线场：整层 0.7 透明度。Hero 比站内（0.55）高一点，
                因为这里它要承重"一眼记住"，站内它只是氛围。
                但不给到满 —— 满强度的紫罗兰波浪读起来是"AI 落地页模板"，
                而我们要的是"一个正在跑的工具"。 */}
            <div className="absolute inset-0 opacity-70">
              <Threads
                color={palette.accentSrgb}
                amplitude={1.1}
                distance={0}
                enableMouseInteraction={isDesktop}
              />
            </div>

            {/* 点阵：只在大屏挂。shockStrength=0 —— 见 AgentField 里的长注释：
                冲击波监听挂在 window 上，会让每次点按钮都在背景里炸一次。 */}
            {isDesktop && (
              <div className="absolute inset-0">
                <DotGrid
                  dotSize={3}
                  gap={34}
                  baseColor={palette.dotBase}
                  activeColor={palette.dotActive}
                  proximity={170}
                  speedTrigger={80}
                  shockRadius={220}
                  shockStrength={0}
                  resistance={900}
                  returnDuration={1.6}
                />
              </div>
            )}
          </Suspense>
        </div>
      )}

      {/*
        压角：中心留亮给标题，四角压暗。
        比站内（0.15 → 0.44 → 0.86）轻一档 —— Hero 的线场是要被看到的，
        压太重就等于花了几百毫秒初始化一个看不见的东西。
        但中心仍保留 0.10，避免线场从标题底下穿过导致读不清。
      */}
      <div
        className="absolute inset-0"
        style={{
          background: `radial-gradient(120% 95% at 50% 4%, rgb(${palette.vignette} / 0.10) 0%, rgb(${palette.vignette} / 0.34) 60%, rgb(${palette.vignette} / 0.80) 100%)`,
        }}
      />
    </div>
  )
}

export default HeroBackdrop
