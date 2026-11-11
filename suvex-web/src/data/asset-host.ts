/**
 * 把「按目标」的资产清单聚合成「按主机」的资产视图。
 *
 * 后端 `/assets` 的口径是 **target**：一个 URL 就是一个资产，所以
 * `http://127.0.0.1:9001/.env`、`/api/users`、`/app.py.bak` 会各占一行。
 * 对扫描统计这是对的（每个 target 独立跑），但对"我有几台机器、每台机器
 * 暴露了什么"不是 —— 那一屏会被同一台主机的路径刷满。
 *
 * 这里只在**前端**做一次确定性聚合，不改后端口径：
 *   · host 相同 → 一台主机；
 *   · 端口、路径、健康度、漏洞数都从原 target 派生；
 *   · 主机的健康度取**最差**的路径（有任何一个没被验证，整台机器就不能信）。
 */

import { assetHealth, type Asset, type AssetHealth } from '@/data/asset'

export interface TargetParts {
  host: string
  port: string | null
  path: string
}

/**
 * 从 target 拆出 host / 显式端口 / 路径。
 *
 * target 的形态不止 URL：还有 `10.20.158.101:11311`、`example.com:8080`、
 * 以及带空格说明的 `http://... (后端 ...)`。所以先取第一个空白分隔的
 * token，再补一个 scheme 交给 `URL`；解析失败时退化为"整串就是 host"，
 * 不吞掉任何目标。
 */
export function parseTargetParts(target: string): TargetParts {
  const trimmed = target.trim()
  const token = trimmed.split(/\s+/)[0] ?? trimmed
  // CIDR 是网络而不是主机，整体作为 host，避免 `10.0.0.0/24` 被拆成
  // host `10.0.0.0` + path `/24`。
  if (/^\d{1,3}(?:\.\d{1,3}){3}\/\d{1,2}$/.test(token)) {
    return { host: token, port: null, path: '' }
  }
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(token) ? token : `http://${token}`
  try {
    const url = new URL(withScheme)
    const host = url.hostname.replace(/^\[|\]$/g, '') || token
    const path = `${url.pathname || '/'}${url.search}`
    return { host, port: url.port || null, path }
  } catch {
    return { host: token, port: null, path: '' }
  }
}

const HEALTH_RANK: Record<AssetHealth, number> = {
  'never-completed': 0,
  failing: 1,
  'never-scanned': 2,
  stale: 3,
  ok: 4,
}

export interface HostAsset {
  host: string
  assets: Asset[]
  /** 显式端口，去重后按数字/字典序排。 */
  ports: string[]
  openFindingCount: number
  openCritical: number
  openHigh: number
  /** 最近一次真正拿到结论（任一端口）。 */
  lastCompletedAt: string | null
  /** 主机下最差的那档健康度。 */
  health: AssetHealth
}

function latest(a: string | null, b: string | null): string | null {
  if (a === null) return b
  if (b === null) return a
  const aTs = new Date(a).getTime()
  const bTs = new Date(b).getTime()
  if (!Number.isFinite(aTs)) return b
  if (!Number.isFinite(bTs)) return a
  return aTs >= bTs ? a : b
}

/** 纯函数：同样的输入永远得到同样的顺序（host 做最后 tie-break）。 */
export function buildHostAssets(assets: readonly Asset[], now: number = Date.now()): HostAsset[] {
  const groups = new Map<string, HostAsset>()

  for (const asset of assets) {
    const { host, port } = parseTargetParts(asset.target)
    let group = groups.get(host)
    if (!group) {
      group = {
        host,
        assets: [],
        ports: [],
        openFindingCount: 0,
        openCritical: 0,
        openHigh: 0,
        lastCompletedAt: null,
        health: 'ok',
      }
      groups.set(host, group)
    }

    const health = assetHealth(asset, now)
    group.assets.push(asset)
    group.openFindingCount += asset.openFindingCount
    group.openCritical += asset.openCritical
    group.openHigh += asset.openHigh
    group.lastCompletedAt = latest(group.lastCompletedAt, asset.lastCompletedAt)
    if (HEALTH_RANK[health] < HEALTH_RANK[group.health]) group.health = health
    if (port && !group.ports.includes(port)) group.ports.push(port)
  }

  for (const group of groups.values()) {
    group.ports.sort((a, b) => {
      const aNum = Number(a)
      const bNum = Number(b)
      if (Number.isFinite(aNum) && Number.isFinite(bNum) && aNum !== bNum) return aNum - bNum
      return a.localeCompare(b)
    })
    // 路径按"风险优先"排，展开时不把 `/favicon.ico` 放最前面。
    group.assets.sort((a, b) => {
      const aBleeding = a.openCritical > 0 || a.openHigh > 0 ? 1 : 0
      const bBleeding = b.openCritical > 0 || b.openHigh > 0 ? 1 : 0
      if (aBleeding !== bBleeding) return bBleeding - aBleeding
      return a.target.localeCompare(b.target)
    })
  }

  return [...groups.values()].sort((a, b) => {
    const aBleeding = a.openCritical > 0 || a.openHigh > 0 ? 1 : 0
    const bBleeding = b.openCritical > 0 || b.openHigh > 0 ? 1 : 0
    if (aBleeding !== bBleeding) return bBleeding - aBleeding
    if (a.health !== b.health) return HEALTH_RANK[a.health] - HEALTH_RANK[b.health]
    const aTs = a.lastCompletedAt === null ? Number.NEGATIVE_INFINITY : new Date(a.lastCompletedAt).getTime()
    const bTs = b.lastCompletedAt === null ? Number.NEGATIVE_INFINITY : new Date(b.lastCompletedAt).getTime()
    if (aTs !== bTs) return aTs - bTs
    return a.host.localeCompare(b.host)
  })
}

export function hostMatchesQuery(host: HostAsset, query: string): boolean {
  const needle = query.trim().toLowerCase()
  if (!needle) return true
  if (host.host.toLowerCase().includes(needle)) return true
  return host.assets.some((asset) => asset.target.toLowerCase().includes(needle))
}
