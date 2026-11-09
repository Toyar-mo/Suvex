import { useEffect, useMemo } from 'react'

import { hasActiveScans, loadScans, useScansState } from '@/data/scans-store'

/**
 * 有活跃扫描时，每 2.5 秒静默刷新一次扫描列表。
 *
 * ## ★ 为什么它从页面级挪到了全局（v1.80）
 *
 * 原来只有扫描页自己起这个定时器。于是**别的页面看到的扫描状态是冻结的**：
 *
 *   · 活动页（`/activity?scan=…`）只在挂载时拉一次 —— 从「查看活动」进去时
 *     若那次扫描还是 `queued`，补线索框永远不出现、卡死提示永远不触发；
 *   · 侧栏「运行中 N」与总览脚注同样停在打开那一刻；
 *   · 而扫描页关掉之后，列表彻底不再更新。
 *
 * 现在挂在 `Layout` 上（全站在同一个 shell 里），谁在看都能拿到新状态；
 * 扫描页自己那个实例删掉，避免同一端点被两处同时轮询。
 *
 * ⚠️ 只在**确实有活跃扫描**时开：终态记录不会自己变化，为它们轮询是白烧请求。
 *    这与总览页 60 秒轮询是同一条判断（那里回答"积累了多少"，这里回答
 *    "此刻在跑什么"）。
 */
export function useScansPolling(intervalMs = 2500): void {
  const { scans } = useScansState()
  const active = useMemo(() => hasActiveScans(scans), [scans])

  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => {
      /*
        ★ 后台标签页不轮询（v1.79）。

        用户切走后页面还在，但没人看 —— 每 2.5 秒一次的请求 + store 提交
        + 订阅方重渲染都是纯浪费（一个开着的后台标签页可以连跑几小时）。
        切回来时立刻补一次，所以不会漏掉这段时间里的状态变化。
      */
      if (document.visibilityState !== 'visible') return
      // silent：轮询失败不该把列表打成错误页，也不该让页面闪骨架
      void loadScans({ silent: true })
    }, intervalMs)

    const onVisible = () => {
      if (document.visibilityState === 'visible') void loadScans({ silent: true })
    }
    document.addEventListener('visibilitychange', onVisible)

    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [active, intervalMs])
}
