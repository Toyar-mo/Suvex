import { cn } from '@/lib/cn'
import type { ActivityEvent, ActivityEventType, AgentPhase } from '@/data/overview'
import { usePrefersReducedMotion } from '@/hooks/usePrefersReducedMotion'
import { Crosshair, FileText, Radar, Search, ShieldCheck, type LucideIcon } from 'lucide-react'
import { useEffect, useMemo, useRef } from 'react'

import { buildStageModel, PHASE_INDEX, PHASES, type StageModel } from './stage-model'

/**
 * 实时活动 · 舞台（v1.82）
 *
 * ## 为什么是 canvas，而不是又一条线
 *
 * 上一个版本用 DOM 画了一条阶段轨道。它信息没错，但"演出感"为零：
 * 静态看就是一条线加几个点。这一版把过程做成一块**舞台**：
 *
 *   · 一条从卡片左端贯穿到右端的流线，粒子持续从左往右流过；
 *   · 五个阶段节点立在流线上，当前阶段的节点呼吸；
 *   · 每个智能体是一颗**绕着自己的阶段节点缓慢公转**的光点；
 *   · 新事件到达时，一颗信号从左侧飞向目标节点，落地炸开一圈粒子；
 *   · 实时流连着时还有一道很淡的扫描光带缓慢掠过。
 *
 * ## 哪些动效是"真的"
 *
 * 这是这张图必须守住的东西 —— 演出不能变成撒谎：
 *
 * | 动效             | 触发源                          | 断线/无事件时       |
 * | ---------------- | ------------------------------- | ------------------- |
 * | 粒子流、扫描光带 | 实时连接存活                    | 立即停止，粒子排空  |
 * | 信号 + 爆炸      | **一条真实的新事件**            | 不动                |
 * | 智能体公转       | 智能体在当前阶段（最近 12 条）  | 降速为待机，不冻结  |
 *
 * 首次加载的历史（种子）不放信号；计数与签子都只取最近 12 条事件。
 *
 * ## 性能
 *
 * 2D canvas、约 160 颗粒子、26px 间距的点阵，DPR 上限 2。页面隐藏时
 * 浏览器自己会停 rAF；`prefers-reduced-motion` 下**整块 canvas 不挂载**，
 * 只留 DOM 的阶段节点与标签 —— 信息一个不少，动效一个不留。
 */

const ICONS: Record<AgentPhase, LucideIcon> = {
  recon: Search,
  scan: Radar,
  exploit: Crosshair,
  verify: ShieldCheck,
  report: FileText,
}

/** 每个节点下面最多挂几枚智能体签；再多折叠成 +N。 */
const MAX_CHIPS = 2

/** 舞台高度。节点圆心在 54px，下面是标签与签子。 */
const STAGE_HEIGHT = 156
const NODE_TOP = 34
const NODE_SIZE = 40
const PATH_Y = NODE_TOP + NODE_SIZE / 2
const WAVE_AMP = 10

const MAX_PARTICLES = 220
const MAX_COMETS = 8
const MAX_BURSTS = 10

const EMPTY_BATCH: ReadonlySet<string> = new Set()

// ─────────────────────────────────────────────
// 组件
// ─────────────────────────────────────────────

export interface ActivityFlowStageProps {
  /** 合并后的活动事件（已按时间倒序） */
  events: ActivityEvent[]
  /** 最近一批新到事件的 id。空集 = 不放信号 */
  latestBatchIds: ReadonlySet<string>
  /** 实时流是否真的连着。false 时粒子停止生成，舞台逐渐排空 */
  flowing?: boolean
  className?: string
}

export function ActivityFlowStage({
  events,
  latestBatchIds,
  flowing = false,
  className,
}: ActivityFlowStageProps) {
  const reducedMotion = usePrefersReducedMotion()
  const model = useMemo(() => buildStageModel(events), [events])

  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const modelRef = useRef(model)
  const flowingRef = useRef(flowing)
  const incomingRef = useRef<Array<{ phase: AgentPhase; type: ActivityEventType }>>([])
  const lastBatchRef = useRef<ReadonlySet<string>>(EMPTY_BATCH)

  /** 模型与连接状态用 ref 喂给 rAF 循环，避免每次事件都重建动画。 */
  useEffect(() => {
    modelRef.current = model
  }, [model])
  useEffect(() => {
    flowingRef.current = flowing
  }, [flowing])

  /*
    新事件 → 入场队列。

    这里只写 ref，不 setState：循环下一帧就会消费它。用 `lastBatchRef`
    挡住重复处理 —— `events` 每 60 秒轮询也会换一次引用，但同一批事件
    不该再放一次信号。
  */
  useEffect(() => {
    if (latestBatchIds === lastBatchRef.current) return
    lastBatchRef.current = latestBatchIds
    if (latestBatchIds.size === 0) return

    for (const event of events) {
      if (!latestBatchIds.has(event.id)) continue
      incomingRef.current.push({ phase: event.agentPhase, type: event.type })
      if (incomingRef.current.length >= MAX_COMETS) break
    }
  }, [latestBatchIds, events])

  useEffect(() => {
    if (reducedMotion) return

    const canvas = canvasRef.current
    if (!canvas) return
    if (typeof requestAnimationFrame !== 'function') return

    // jsdom / 无 2D 上下文的环境：静默退出，DOM 层仍然完整可读。
    const ctx = get2dContext(canvas)
    if (!ctx) return

    let width = 0
    let height = 0
    let dpr = 1
    let raf = 0
    let stopped = false
    let palette = readPalette()

    type Rgb = [number, number, number]
    interface Particle {
      u: number
      speed: number
      size: number
      alpha: number
      seed: number
      /** 亮粒子：让流动里偶尔出现一颗"领头"的彗星，而不是一条均匀的虚线 */
      bright: boolean
    }
    interface Comet {
      u: number
      target: number
      speed: number
      color: Rgb
    }
    interface Burst {
      x: number
      y: number
      t0: number
      color: Rgb
      parts: Array<{ angle: number; speed: number; size: number }>
    }
    interface Orb {
      u: number
      target: number
      index: number
      count: number
      seed: number
    }

    const particles: Particle[] = []
    const comets: Comet[] = []
    const bursts: Burst[] = []
    const orbs = new Map<string, Orb>()

    let spawnAcc = 0

    const resize = () => {
      const rect = canvas.getBoundingClientRect()
      width = Math.max(1, rect.width)
      height = Math.max(1, rect.height)
      dpr = Math.min(window.devicePixelRatio || 1, 2)
      canvas.width = Math.round(width * dpr)
      canvas.height = Math.round(height * dpr)
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    }

    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(resize) : null
    ro?.observe(canvas)
    resize()

    /*
      主题切换时重读 token。监听 `data-theme` 而不是每次画都读计算样式 ——
      后者每帧触发样式计算，是这类 canvas 动画最常见的性能坑。
    */
    const themeObserver =
      typeof MutationObserver !== 'undefined'
        ? new MutationObserver(() => {
            palette = readPalette()
          })
        : null
    themeObserver?.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    })

    /** 流线：节点处为 0、节点之间上下摆动的正弦波。 */
    const point = (u: number): { x: number; y: number } => {
      const phase = (u - 0.1) / 0.2
      const wave = phase > 0 && phase < 4 ? Math.sin(phase * Math.PI) * WAVE_AMP : 0
      return { x: u * width, y: PATH_Y + wave }
    }

    const nodeU = (index: number) => 0.1 + index * 0.2

    /** 事件类型 → 爆点颜色。颜色承载语义，不只是好看。 */
    const colorOf = (type: ActivityEventType): Rgb => {
      switch (type) {
        case 'verified':
        case 'completed':
          return palette.ok
        case 'discovery':
          return palette.warn
        case 'blocked':
          return palette.risk
        case 'poc_generated':
          return palette.accentStrong
        default:
          return palette.accent
      }
    }

    const addBurst = (x: number, y: number, color: Rgb) => {
      const parts = Array.from({ length: 24 }, (_, i) => ({
        angle: (i / 24) * Math.PI * 2 + Math.random() * 0.3,
        speed: 42 + Math.random() * 86,
        size: 1.1 + Math.random() * 2.1,
      }))
      bursts.push({ x, y, t0: performance.now(), color, parts })
      if (bursts.length > MAX_BURSTS) bursts.shift()
    }

    /* 首帧先铺一层静态内容，再进循环 —— 打开页面时舞台不是空的。 */
    resize()
    drawStatic(ctx, width, height, palette, modelRef.current)

    let last = performance.now()
    let orbitPhase = 0

    const frame = (now: number) => {
      if (stopped) return
      const dt = Math.min(0.05, Math.max(0, (now - last) / 1000))
      last = now

      const modelNow = modelRef.current
      const live = flowingRef.current
      /*
        ★ 亮色主题要压强度。
          同一个 accent 在浅底上是深紫，同样的 alpha 叠 200 多颗粒子会糊成
          一条实心带；暗底上则正好。这不是"两套设计"，是同一个设计在不同
          背景亮度下的必要补偿。
      */
      const strength = palette.dark ? 1 : 0.45
      /*
        公转在断线时**降速待机**而不是冻结。
        数据含义没变（智能体仍在那个阶段），冻结只会让整块舞台看起来
        挂了；降速则明确表达"没有新事件，但画面还活着"。
      */
      orbitPhase += live ? dt : dt * 0.35

      // 背景点阵（很淡，只负责让舞台有"纵深"）
      ctx.clearRect(0, 0, width, height)
      drawDots(ctx, width, height, palette)

      // 流线的微弱波形
      ctx.beginPath()
      for (let i = 0; i <= 48; i += 1) {
        const p = point(i / 48)
        if (i === 0) ctx.moveTo(p.x, p.y)
        else ctx.lineTo(p.x, p.y)
      }
      ctx.strokeStyle = rgba(palette.accent, 0.1 * strength)
      ctx.lineWidth = 1
      ctx.stroke()

      /*
        扫描线（只在实时流连着时出现）。

        ★ 用一条 1px 的亮线 + 很窄的余晖，而不是一条 90px 宽的光带 ——
          宽光带在截图里会读成"一块面板边缘"，反而不像扫描。
      */
      if (live) {
        const sweep = (((now * 0.00006) % 1.25) - 0.12) * width
        const trail = ctx.createLinearGradient(sweep - 34, 0, sweep, 0)
        trail.addColorStop(0, rgba(palette.accent, 0))
        trail.addColorStop(1, rgba(palette.accent, 0.035 * strength))
        ctx.fillStyle = trail
        ctx.fillRect(sweep - 34, 0, 34, height)

        ctx.fillStyle = rgba(palette.accent, 0.1 * strength)
        ctx.fillRect(sweep, 0, 1, height)
      }

      /*
        ── 粒子流 ──
        只在实时流连着时生成；断线后不再补，已有的粒子自然排空。
      */
      if (live) {
        spawnAcc += dt * 64
        while (spawnAcc >= 1 && particles.length < MAX_PARTICLES) {
          spawnAcc -= 1
          const bright = Math.random() < 0.12
          particles.push({
            u: 0,
            speed: 0.1 + Math.random() * 0.12,
            size: bright ? 2.4 + Math.random() * 1.2 : 0.9 + Math.random() * 1.5,
            alpha: bright ? 0.75 : 0.22 + Math.random() * 0.38,
            seed: Math.random() * 1000,
            bright,
          })
        }
      } else {
        spawnAcc = 0
      }

      ctx.globalCompositeOperation = palette.dark ? 'lighter' : 'source-over'

      for (let i = particles.length - 1; i >= 0; i -= 1) {
        const p = particles[i]
        p.u += p.speed * dt
        if (p.u >= 1) {
          particles.splice(i, 1)
          continue
        }
        const head = point(p.u)
        const tail = point(Math.max(0, p.u - (p.bright ? 0.038 : 0.018)))
        const wobble = Math.sin(now * 0.002 + p.seed) * 1.8
        const color = p.bright ? palette.accentStrong : palette.accent
        ctx.strokeStyle = rgba(color, p.alpha * strength)
        ctx.lineWidth = p.size
        ctx.lineCap = 'round'
        ctx.beginPath()
        ctx.moveTo(tail.x, tail.y + wobble)
        ctx.lineTo(head.x, head.y + wobble)
        ctx.stroke()

        if (p.bright) {
          ctx.fillStyle = rgba(color, Math.min(1, p.alpha + 0.18) * strength)
          ctx.beginPath()
          ctx.arc(head.x, head.y + wobble, p.size * 0.55, 0, Math.PI * 2)
          ctx.fill()
        }
      }

      /*
        ── 新事件的信号与爆炸 ──
      */
      while (incomingRef.current.length > 0 && comets.length < MAX_COMETS) {
        const incoming = incomingRef.current.shift()
        if (!incoming) break
        const target = nodeU(PHASE_INDEX[incoming.phase])
        const distance = Math.max(0.2, target)
        comets.push({
          u: 0,
          target,
          // 不管目标在哪一站，飞行时间都在 0.85–1.05s 之间
          speed: distance / (0.85 + Math.random() * 0.2),
          color: colorOf(incoming.type),
        })
      }

      for (let i = comets.length - 1; i >= 0; i -= 1) {
        const comet = comets[i]
        comet.u += comet.speed * dt
        if (comet.u >= comet.target) {
          const at = point(comet.target)
          addBurst(at.x, at.y, comet.color)
          comets.splice(i, 1)
          continue
        }

        const head = point(comet.u)
        const tail = point(Math.max(0, comet.u - 0.05))
        const grad = ctx.createLinearGradient(tail.x, tail.y, head.x, head.y)
        grad.addColorStop(0, rgba(comet.color, 0))
        grad.addColorStop(1, rgba(comet.color, 0.75 * strength))
        ctx.strokeStyle = grad
        ctx.lineWidth = 2
        ctx.lineCap = 'round'
        ctx.beginPath()
        ctx.moveTo(tail.x, tail.y)
        ctx.lineTo(head.x, head.y)
        ctx.stroke()

        ctx.fillStyle = rgba(comet.color, 0.95 * strength)
        ctx.beginPath()
        ctx.arc(head.x, head.y, 2.4, 0, Math.PI * 2)
        ctx.fill()
      }

      /*
        ── 爆炸：16 颗放射粒子 + 一圈扩散环 ──
      */
      for (let i = bursts.length - 1; i >= 0; i -= 1) {
        const burst = bursts[i]
        const life = (now - burst.t0) / 900
        if (life >= 1) {
          bursts.splice(i, 1)
          continue
        }

        for (const part of burst.parts) {
          const distance = part.speed * life * 0.95
          const x = burst.x + Math.cos(part.angle) * distance
          const y = burst.y + Math.sin(part.angle) * distance
          ctx.fillStyle = rgba(burst.color, (1 - life) * 0.8 * strength)
          ctx.beginPath()
          ctx.arc(x, y, part.size * (1 - life * 0.4), 0, Math.PI * 2)
          ctx.fill()
        }

        ctx.strokeStyle = rgba(burst.color, (1 - life) * 0.55 * strength)
        ctx.lineWidth = 1.2
        ctx.beginPath()
        ctx.arc(burst.x, burst.y, 8 + life * 44, 0, Math.PI * 2)
        ctx.stroke()
      }

      /*
        ── 智能体：绕着自己的阶段节点公转；阶段变化时沿流线滑过去 ──
      */
      const seen = new Set<string>()
      for (const phase of PHASES) {
        const list = modelNow.marksByPhase[phase.key]

        // 公转轨道：让"智能体绕着这一站"这件事读得出来，而不是一颗孤点
        if (list.length > 0) {
          const base = point(nodeU(PHASE_INDEX[phase.key]))
          ctx.strokeStyle = rgba(palette.accent, 0.1 * strength)
          ctx.lineWidth = 1
          ctx.beginPath()
          ctx.ellipse(base.x, base.y, 30, 22, 0, 0, Math.PI * 2)
          ctx.stroke()
        }

        list.forEach((mark, index) => {
          seen.add(mark.agentId)
          const target = nodeU(PHASE_INDEX[phase.key])
          let orb = orbs.get(mark.agentId)
          if (!orb) {
            orb = {
              u: target,
              target,
              index,
              count: list.length,
              seed: hashString(mark.agentId),
            }
            orbs.set(mark.agentId, orb)
          } else {
            orb.target = target
            orb.index = index
            orb.count = list.length
          }

          const delta = orb.target - orb.u
          orb.u += delta * Math.min(1, dt * 4.5)

          let x: number
          let y: number
          const moving = Math.abs(delta) > 0.004

          if (moving) {
            const p = point(orb.u)
            x = p.x
            y = p.y
            // 迁移时拖一条短尾
            const tail = point(Math.max(0, orb.u - delta * 0.35))
            ctx.strokeStyle = rgba(palette.accentStrong, 0.35)
            ctx.lineWidth = 1.5
            ctx.beginPath()
            ctx.moveTo(tail.x, tail.y)
            ctx.lineTo(x, y)
            ctx.stroke()
          } else {
            const base = point(orb.target)
            const angle =
              orbitPhase * 0.55 +
              orb.seed * 0.37 +
              (orb.index / Math.max(1, orb.count)) * Math.PI * 2
            x = base.x + Math.cos(angle) * 30
            y = base.y + Math.sin(angle) * 22
          }

          const glow = ctx.createRadialGradient(x, y, 0, x, y, 17)
          glow.addColorStop(0, rgba(palette.accentStrong, 0.42 * strength))
          glow.addColorStop(1, rgba(palette.accentStrong, 0))
          ctx.fillStyle = glow
          ctx.beginPath()
          ctx.arc(x, y, 17, 0, Math.PI * 2)
          ctx.fill()

          ctx.strokeStyle = rgba(palette.accentStrong, 0.42 * strength)
          ctx.lineWidth = 1
          ctx.beginPath()
          ctx.arc(x, y, 6.5, 0, Math.PI * 2)
          ctx.stroke()

          ctx.fillStyle = rgba(palette.accentStrong, 1)
          ctx.beginPath()
          ctx.arc(x, y, 3.2, 0, Math.PI * 2)
          ctx.fill()
        })
      }
      for (const id of orbs.keys()) {
        if (!seen.has(id)) orbs.delete(id)
      }

      // 当前阶段的节点：一盏很淡的舞台灯 + 呼吸环
      if (modelNow.latestPhase !== null) {
        const node = point(nodeU(PHASE_INDEX[modelNow.latestPhase]))

        const stageLight = ctx.createRadialGradient(node.x, node.y, 0, node.x, node.y, 62)
        stageLight.addColorStop(0, rgba(palette.accent, 0.09 * strength))
        stageLight.addColorStop(1, rgba(palette.accent, 0))
        ctx.fillStyle = stageLight
        ctx.beginPath()
        ctx.arc(node.x, node.y, 62, 0, Math.PI * 2)
        ctx.fill()

        // 断线时呼吸继续，但更慢、更小 —— 待机，而不是"冻住"。
        const pulse = live
          ? Math.sin(now * 0.0022) * 2
          : Math.sin(now * 0.0011) * 1.2
        ctx.strokeStyle = rgba(palette.accent, 0.18 * strength)
        ctx.lineWidth = 1
        ctx.beginPath()
        ctx.arc(node.x, node.y, 24 + pulse, 0, Math.PI * 2)
        ctx.stroke()
      }

      ctx.globalCompositeOperation = 'source-over'
      raf = requestAnimationFrame(frame)
    }

    raf = requestAnimationFrame(frame)

    return () => {
      stopped = true
      cancelAnimationFrame(raf)
      ro?.disconnect()
      themeObserver?.disconnect()
    }
  }, [reducedMotion])

  const description =
    model.total > 0
      ? `最近 ${model.total} 条事件落在各阶段的数量，以及每个智能体当前所处阶段`
      : '各阶段最近事件数量与智能体分布'

  return (
    <div
      className={cn('relative overflow-hidden bg-surface-sunken/30', className)}
      style={{ height: STAGE_HEIGHT }}
    >
      {!reducedMotion && (
        <canvas ref={canvasRef} className="absolute inset-0 h-full w-full" aria-hidden="true" />
      )}

      {/* 舞台的中轴线。canvas 的粒子在这条线两侧摆动。 */}
      <div
        aria-hidden="true"
        className="absolute left-[10%] right-[10%] h-px bg-line-strong"
        style={{ top: PATH_Y }}
      />

      <ol className="absolute inset-0" aria-label={description}>
        {PHASES.map((phase) => {
          const Icon = ICONS[phase.key]
          const count = model.counts[phase.key]
          const marks = model.marksByPhase[phase.key]
          const isLatest = phase.key === model.latestPhase

          return (
            <li
              key={phase.key}
              className="absolute flex w-20 -translate-x-1/2 flex-col items-center sm:w-32"
              style={{ left: `${10 + PHASE_INDEX[phase.key] * 20}%`, top: NODE_TOP }}
              aria-label={`${phase.label}：最近 ${count} 条事件，${marks.length} 个智能体`}
            >
              <span
                aria-hidden="true"
                className={cn(
                  'flex h-10 w-10 items-center justify-center rounded-pill border transition-ui duration-base',
                  isLatest
                    ? 'border-accent bg-accent-wash text-accent-strong'
                    : marks.length > 0
                      ? 'border-line-strong bg-surface-raised text-ink-muted'
                      : 'border-line bg-surface text-ink-faint',
                )}
              >
                <Icon size={17} strokeWidth={1.75} />
              </span>

              <span
                className={cn(
                  'mt-1.5 whitespace-nowrap text-center text-2xs leading-none',
                  isLatest ? 'text-ink' : 'text-ink-muted',
                )}
              >
                {phase.label}
                <span
                  className={cn('tabular ml-1', isLatest ? 'text-accent-strong' : 'text-ink-faint')}
                >
                  {count}
                </span>
              </span>

              <div className="mt-1 flex w-full flex-col items-center gap-1">
                {marks.slice(0, MAX_CHIPS).map((mark) => (
                  <span
                    key={mark.agentId}
                    className="max-w-full animate-settle-in truncate rounded-xs border border-accent/30 bg-accent-wash px-1.5 py-0.5 text-2xs leading-none text-accent-strong"
                    title={`${mark.agentId}：${phase.label} · 最近 ${mark.count} 条事件`}
                  >
                    {shortAgentId(mark.agentId)}
                  </span>
                ))}
                {marks.length > MAX_CHIPS && (
                  <span className="text-2xs leading-none text-ink-faint">
                    +{marks.length - MAX_CHIPS}
                  </span>
                )}
              </div>
            </li>
          )
        })}
      </ol>
    </div>
  )
}

// ─────────────────────────────────────────────
// canvas 工具
// ─────────────────────────────────────────────

type RGB = [number, number, number]

interface Palette {
  dark: boolean
  accent: RGB
  accentStrong: RGB
  line: RGB
  ok: RGB
  warn: RGB
  risk: RGB
}

function get2dContext(canvas: HTMLCanvasElement): CanvasRenderingContext2D | null {
  try {
    return canvas.getContext('2d')
  } catch {
    return null
  }
}

function readRgb(name: string, fallback: RGB): RGB {
  if (typeof window === 'undefined') return fallback
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  const [r, g, b] = raw.split(/\s+/).map(Number)
  if (![r, g, b].every(Number.isFinite)) return fallback
  return [r, g, b]
}

function readPalette(): Palette {
  return {
    dark: document.documentElement.getAttribute('data-theme') !== 'light',
    accent: readRgb('--suvex-accent', [139, 124, 246]),
    accentStrong: readRgb('--suvex-accent-strong', [169, 155, 255]),
    line: readRgb('--suvex-line-strong', [44, 50, 61]),
    ok: readRgb('--suvex-signal-ok', [74, 222, 155]),
    warn: readRgb('--suvex-signal-warn', [232, 180, 84]),
    risk: readRgb('--suvex-signal-risk', [240, 122, 106]),
  }
}

function rgba(color: RGB, alpha: number): string {
  return `rgba(${color[0]}, ${color[1]}, ${color[2]}, ${alpha})`
}

function drawDots(ctx: CanvasRenderingContext2D, width: number, height: number, palette: Palette) {
  const gap = 26
  ctx.fillStyle = rgba(palette.line, 0.32)
  for (let y = 14; y < height; y += gap) {
    for (let x = 12; x < width; x += gap) {
      ctx.beginPath()
      ctx.arc(x, y, 0.8, 0, Math.PI * 2)
      ctx.fill()
    }
  }
}

/** 减少动效时的静态一帧（当前也在 DOM 层有等价信息，这里只做底图）。 */
function drawStatic(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  palette: Palette,
  model: StageModel,
) {
  const strength = palette.dark ? 1 : 0.45
  ctx.clearRect(0, 0, width, height)
  drawDots(ctx, width, height, palette)

  if (model.latestPhase !== null) {
    const x = width * (0.1 + PHASE_INDEX[model.latestPhase] * 0.2)
    ctx.strokeStyle = rgba(palette.accent, 0.18 * strength)
    ctx.lineWidth = 1
    ctx.beginPath()
    ctx.arc(x, PATH_Y, 24, 0, Math.PI * 2)
    ctx.stroke()
  }
}

/** 稳定的字符串哈希，给每个智能体一个固定的公转相位。 */
function hashString(value: string): number {
  let hash = 2166136261
  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i)
    hash = Math.imul(hash, 16777619)
  }
  return ((hash >>> 0) % 1000) / 1000
}

/**
 * agent id 只留尾部。
 *
 * `agent_01M6QS0K57C8KWBBH0HV3T0EC` 有 32 个字符，整段摆出来会把节点挤掉；
 * 人真正用来对照的只是尾部几位。与 `ActivityFeedItem` 同一规则。
 */
function shortAgentId(id: string): string {
  return id.length <= 14 ? id : `…${id.slice(-10)}`
}

export default ActivityFlowStage
