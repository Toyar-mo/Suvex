import { useMemo } from 'react'
import { useScansState } from './scans-store'
import { useSettings } from './settings-store'

/**
 * 顶部全局状态条的数据源。
 *
 * ## 任务 7：从「一份写死的 mock」改成「从真实 store 推」
 *
 * 原来这里导出 `MOCK_FLEET_STATUS`（3 个 agent 在跑、DeepSeek、184.3k token），
 * `useFleetStatus` 直接返回它。现在三段数据的处境**各不相同**，
 * 所以分开交代 —— 把三种不同的确定性混在一句"演示数据"里是不诚实的：
 *
 * | 段 | 来源 | 说明 |
 * | --- | --- | --- |
 * | `agents` | `scans-store` | **真实**。有扫描处于 running/queued 时计数 |
 * | `provider` | `settings-store` | **真实**。取默认 provider 的短名与 model |
 * | `tokens` | 无 | 契约里**没有** token 用量端点，如实返回 `null` |
 *
 * ## `agents` 的语义要写清楚：它是**扫描数**，不是 agent 实例数
 *
 * 契约里唯一能问到 agent 的端点是 `GET /scans/{id}/agents` —— 一次扫描一个请求。
 * 为了状态条上那个数字去轮询所有活跃扫描是不划算的，而且状态条在每个页面都渲染。
 *
 * 所以这里数的是**扫描**：`running` / `queued` 的任务数。
 * 对用户来说"有几个任务在跑"与"有几个智能体在跑"在这一条上要回答的是
 * 同一个问题（"现在忙不忙"），而扫描数是**服务端权威值**且零额外请求。
 *
 * ## ★ 这个 hook **不触发任何加载**
 *
 * 它是唯一被**公开页面**（落地页）用到的数据 hook。如果它在这里
 * `void loadScans()`，未登录的访客会打出一个 401 —— 而 `lib/api.ts` 的
 * 401 处理会把他**踢到登录页**。落地页因此会变得无法访问。
 *
 * 加载由各页面的 effect 负责（它们本来就要加载），这里只订阅结果。
 * 数据没到就是 0 / null，那不是"假数据"，那是"还没加载"。
 */

/** 状态条上的模型信息。`id` 用真实主键（`prov_…`），不再是三个固定值之一 */
export interface ProviderInfo {
  id: string
  /** 显示名。品牌名不翻译 —— "DeepSeek" 译成中文反而不可检索 */
  label: string
  /** 模型标识符，同样不翻译（要和配置文件里的值对得上） */
  model: string
}

export interface FleetStatus {
  agents: {
    /** 正在跑的**扫描**数（见文件头的语义说明） */
    running: number
    queued: number
  }
  /** 默认 provider。一个都没配时为 null */
  provider: ProviderInfo | null
  /**
   * token 用量。
   *
   * ⚠️ **恒为 null** —— 契约里没有任何 token 用量端点（§9 的端点表里没有，
   *    `ProviderOut` 也不带用量字段）。这里如实标注"没有这个数据"，
   *    而不是编一个看起来很像的（原来的 `184.3k / 1M · 18%` 就是编的，
   *    而它会被当成"这个月还能跑多少"的依据）。
   *
   * 端点交付后把这一支改成真实数据即可，消费方按 null 分支已经写好了。
   */
  tokens: { used: number; budget: number | null; windowLabel: string } | null
}

export function useFleetStatus(): FleetStatus {
  // 只订阅，不加载 —— 理由见文件头那段（它会 401 掉落地页）
  const { scans } = useScansState()
  const { accounts } = useSettings()

  /*
    useMemo 是必须的：这个对象会成为消费方的渲染依赖。
    每次渲染返回新对象会让 Sidebar / TopStatusBar / LandingPage
    都跟着重渲染一遍 —— 而它们挂在每个页面上。
  */
  return useMemo<FleetStatus>(() => {
    const defaultAccount = accounts.find((account) => account.isDefault) ?? accounts[0] ?? null

    return {
      agents: {
        running: scans.filter((scan) => scan.status === 'running').length,
        queued: scans.filter((scan) => scan.status === 'queued').length,
      },
      provider: defaultAccount
        ? { id: defaultAccount.id, label: defaultAccount.label, model: defaultAccount.model }
        : null,
      tokens: null,
    }
  }, [scans, accounts])
}
