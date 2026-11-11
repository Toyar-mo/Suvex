import { useEffect, useMemo, useRef, useState } from 'react'
import { Minus, Plus, RotateCcw } from 'lucide-react'

import { ASSET_HEALTH_META } from '@/data/asset'
import type { HostAsset } from '@/data/asset-host'
import { cn } from '@/lib/cn'
import { useI18n } from '@/i18n'

/**
 * 资产测绘视图（v1.90，UI 移植阶段 3）。
 *
 * ## 为什么是"中心辐射"而不是力导向
 *
 * 力导向每次打开都不一样，而资产页的核心问题是**对比**（哪台更危险）。
 * 位置稳定才能形成肌肉记忆；放射布局把"所有主机都挂在同一个工作空间下"
 * 这条事实直接画出来，节点只按索引落位，同一份数据永远是同一张图。
 *
 * ## 数据边界
 *
 * 只有主机级数据（host / ports / health / 未处置计数）。端口与路径在
 * `assets` 里，节点暂不展开 —— 那是下一步，先把"有哪些主机、谁在出血"
 * 一眼看完。节点点击 = 回到列表并按该主机过滤，而不是再开一个面板。
 *
 * ## 交互
 *
 * 滚轮缩放（0.5–2 倍）、拖拽平移、重置。所有状态都在本地，
 * 不写库、不同步 —— 它是一次观看，不是一次配置。
 */

const MAP_WIDTH = 900
const MAP_HEIGHT = 560
const CENTER = { x: MAP_WIDTH / 2, y: MAP_HEIGHT / 2 }
const NODE_W = 168
const NODE_H = 52

export function AssetMap({
  hosts,
  onSelectHost,
  className,
}: {
  hosts: HostAsset[]
  onSelectHost?: (host: string) => void
  className?: string
}) {
  const { t } = useI18n()
  const [zoom, setZoom] = useState(1)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const dragRef = useRef<{ x: number; y: number } | null>(null)
  const hostRef = useRef<HTMLDivElement | null>(null)
  const [box, setBox] = useState({ w: MAP_WIDTH, h: MAP_HEIGHT })

  /*
   * 画布固定 900×560，容器却随视口变窄。不缩放的话窄屏只能看到图的一角，
   * 而且节点被裁掉一半 —— 看起来像"图画坏了"。这里按容器尺寸等比缩到放得下，
   * 再把缩放后的图居中；用户自己的 zoom/pan 叠加在这层之上。
   */
  useEffect(() => {
    const el = hostRef.current
    if (!el) return
    const update = () => setBox({ w: el.clientWidth, h: el.clientHeight })
    update()
    const observer = new ResizeObserver(update)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  // jsdom / 首次测量为 0 时按 1 处理，否则整张图会被缩成 0 宽。
  const rawFit = Math.min(1, box.w / MAP_WIDTH, box.h / MAP_HEIGHT)
  const fit = Number.isFinite(rawFit) && rawFit > 0 ? rawFit : 1
  const scale = zoom * fit
  const offsetX = (box.w - MAP_WIDTH * scale) / 2
  const offsetY = (box.h - MAP_HEIGHT * scale) / 2

  const nodes = useMemo(() => {
    const radius = Math.max(190, Math.min(250, 90 + hosts.length * 14))
    return hosts.map((host, index) => {
      const angle = (index / Math.max(hosts.length, 1)) * Math.PI * 2 - Math.PI / 2
      return {
        host,
        x: CENTER.x + Math.cos(angle) * radius - NODE_W / 2,
        y: CENTER.y + Math.sin(angle) * radius - NODE_H / 2,
      }
    })
  }, [hosts])

  const bleeding = hosts.filter((host) => host.openCritical + host.openHigh > 0).length

  return (
    <section
      className={cn('relative h-[380px] overflow-hidden rounded-md border border-line bg-surface sm:h-[460px] lg:h-[560px]', className)}
      data-asset-map=""
    >
      <div className="absolute left-3 top-3 z-10 flex items-center gap-2 text-2xs text-ink-faint">
        <span>{hosts.length} {t('assetMap.hosts')}</span>
        <span aria-hidden="true">·</span>
        <span className={bleeding > 0 ? 'text-signal-risk' : undefined}>{bleeding} {t('assetMap.bleeding')}</span>
      </div>

      <div className="absolute right-3 top-3 z-10 flex items-center gap-1">
        <MapButton label={t('assetMap.zoomOut')} onClick={() => setZoom((value) => Math.max(0.5, value - 0.15))}>
          <Minus size={13} strokeWidth={2} aria-hidden="true" />
        </MapButton>
        <span
          className="mono-ui w-10 text-center text-2xs text-ink-faint"
          title="相对「适应视图」的缩放"
        >
          {Math.round(zoom * 100)}%
        </span>
        <MapButton label={t('assetMap.zoomIn')} onClick={() => setZoom((value) => Math.min(2, value + 0.15))}>
          <Plus size={13} strokeWidth={2} aria-hidden="true" />
        </MapButton>
        <MapButton
          label={t('assetMap.reset')}
          onClick={() => {
            setZoom(1)
            setPan({ x: 0, y: 0 })
          }}
        >
          <RotateCcw size={13} strokeWidth={2} aria-hidden="true" />
        </MapButton>
      </div>

      <div
        ref={hostRef}
        className="h-full w-full cursor-grab active:cursor-grabbing"
        onWheel={(event) => {
          event.preventDefault()
          setZoom((value) => Math.min(2, Math.max(0.5, value + (event.deltaY < 0 ? 0.1 : -0.1))))
        }}
        onPointerDown={(event) => {
          dragRef.current = { x: event.clientX - pan.x, y: event.clientY - pan.y }
          event.currentTarget.setPointerCapture(event.pointerId)
        }}
        onPointerMove={(event) => {
          if (!dragRef.current) return
          setPan({ x: event.clientX - dragRef.current.x, y: event.clientY - dragRef.current.y })
        }}
        onPointerUp={() => {
          dragRef.current = null
        }}
      >
        <div
          className="relative origin-top-left"
          style={{
            width: MAP_WIDTH,
            height: MAP_HEIGHT,
            transform: `translate(${offsetX + pan.x}px, ${offsetY + pan.y}px) scale(${scale})`,
          }}
        >
          <svg
            aria-hidden="true"
            width={MAP_WIDTH}
            height={MAP_HEIGHT}
            className="absolute inset-0"
          >
            {nodes.map((node) => (
              <line
                key={node.host.host}
                x1={CENTER.x}
                y1={CENTER.y}
                x2={node.x + NODE_W / 2}
                y2={node.y + NODE_H / 2}
                stroke="currentColor"
                className="text-line"
                strokeWidth={1}
              />
            ))}
          </svg>

          <div
            className="absolute flex -translate-x-1/2 -translate-y-1/2 items-center gap-2 rounded-full border border-line-strong bg-surface-overlay px-4 py-2 text-xs text-ink shadow-sm"
            style={{ left: CENTER.x, top: CENTER.y }}
          >
            {t('assetMap.center')} · {hosts.length}
          </div>

          {nodes.map((node) => {
            const meta = ASSET_HEALTH_META[node.host.health]
            const danger = node.host.openCritical + node.host.openHigh
            return (
              <button
                key={node.host.host}
                type="button"
                onClick={() => onSelectHost?.(node.host.host)}
                title={`${node.host.host} · ${meta.label} · ${node.host.openFindingCount} 条未处置`}
                className="absolute flex flex-col justify-center gap-0.5 rounded-md border border-line bg-surface px-2.5 py-1.5 text-left transition-colors hover:border-line-strong hover:bg-surface-overlay"
                style={{ left: node.x, top: node.y, width: NODE_W, height: NODE_H }}
              >
                <span className="flex items-center gap-1.5">
                  <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', meta.dot)} aria-hidden="true" />
                  <span className="truncate text-xs text-ink">{node.host.host}</span>
                </span>
                <span className="pl-3 text-2xs text-ink-faint">
                  {node.host.ports.length} {t('assetMap.ports')}
                  {danger > 0 && <span className="ml-1.5 text-signal-risk">{danger} {t('assetMap.high')}</span>}
                </span>
              </button>
            )
          })}
        </div>
      </div>
    </section>
  )
}

function MapButton({
  label,
  onClick,
  children,
}: {
  label: string
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      className="flex h-7 w-7 items-center justify-center rounded-sm border border-line bg-surface text-ink-muted transition-colors hover:bg-surface-sunken hover:text-ink"
    >
      {children}
    </button>
  )
}

export default AssetMap
