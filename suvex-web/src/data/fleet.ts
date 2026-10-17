import { useMemo } from 'react'
import { useOverviewState } from './overview-store'
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
 * | `tokens` | `overview-store` | **真实**（v0.36 起）。取 `ctf2TotalTokens` |
 *
 * ## ★ `tokens` 曾经恒为 `null`，2026-10-17 改成真实值
 *
 * 这里原来写着"契约里没有 token 用量端点"，那句话在写下的当天（10-10）
 * 是对的，但 10-15 加的 `GET /overview` 已经带 `ctf2TotalTokens` /
 * `ctf2LlmCalls` / `ctf2UsageSince` 三个字段了 —— 于是状态条上那句
 * 「用量统计未接入」变成了一句**假话**：数据就在同一个端点里，
 * 总览页的 LLM 消耗卡也一直在用。
 *
 * 顺手保留了 `null` 这一支：它现在表达的是**"还没加载到"**，
 * 而不是"没有这个数据"。两者的区别见下面 `tokens` 字段的注释。
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
   * token 用量。`null` = **还没加载到**（不是"没有这个数据"）。
   *
   * 数据来自 `GET /overview` 的 `ctf2TotalTokens`，与总览页的
   * LLM 消耗卡是**同一个数**（同一个端点、同一个事务），两处不会打架。
   *
   * `budget` **恒为 null**，这是设计而非缺口：契约里没有"预算 / 配额"
   * 这个概念 —— 平台不替用户管账户余额，单价又随模型、供应商、时段变，
   * 写死一个额度会误导。所以状态条只显示"花了多少"，不显示"还剩多少"。
   * 原来的 `184.3k / 1M · 18%` 里那个 `1M` 是编的，随它一起删掉了。
   */
  tokens: { used: number; budget: number | null; windowLabel: string } | null
}

export function useFleetStatus(): FleetStatus {
  // 只订阅，不加载 —— 理由见文件头那段（它会 401 掉落地页）
  const { scans } = useScansState()
  const { accounts } = useSettings()
  const overview = useOverviewState()

  /*
    useMemo 是必须的：这个对象会成为消费方的渲染依赖。
    每次渲染返回新对象会让 Sidebar / TopStatusBar / LandingPage
    都跟着重渲染一遍 —— 而它们挂在每个页面上。
  */
  return useMemo<FleetStatus>(() => {
    const defaultAccount = accounts.find((account) => account.isDefault) ?? accounts[0] ?? null
    const metrics = overview.data?.metrics ?? null

    /*
      `ctf2UsageSince === null` 表示**一条 usage 记录都没有**
      （v0.36 之前的解题不写 usage）。这时显示 `0 token` 是错的 ——
      "消耗 0"与"没记录到"在界面上必须能分开，所以整段不显示。
    */
    const hasUsage = metrics !== null && metrics.ctf2UsageSince !== null

    return {
      agents: {
        running: scans.filter((scan) => scan.status === 'running').length,
        queued: scans.filter((scan) => scan.status === 'queued').length,
      },
      provider: defaultAccount
        ? { id: defaultAccount.id, label: defaultAccount.label, model: defaultAccount.model }
        : null,
      tokens: hasUsage
        ? {
            used: metrics.ctf2TotalTokens,
            budget: null,
            // §口径写进标签：这个数是 CTF2 解题的累计消耗，
            // 不是"整个平台的 LLM 花费"—— 后者目前没有独立记账
            windowLabel: 'CTF2 解题累计',
          }
        : null,
    }
  }, [scans, accounts, overview.data])
}
