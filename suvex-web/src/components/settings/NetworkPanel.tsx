import { useEffect, useState } from 'react'

import { SettingsPanel } from './SaveBar'
import ErrorState from '@/components/ui/ErrorState'
import { SkeletonRows } from '@/components/ui/Skeleton'
import { loadNetworkRestrictions, type NetworkRestrictions } from '@/data/network'
import { ApiError } from '@/lib/api'
import { cn } from '@/lib/cn'

/**
 * 网络限制（v1.90，UI 移植阶段 4）。
 *
 * ## 为什么这里没有"保存"按钮
 *
 * 白名单的额外项来自环境变量，内置项来自代码常量 —— 两者都不该在网页上
 * 点一下就改：它们决定"沙箱能访问哪里"，属于部署时的审查对象。
 * 页面的职责是把**当前生效值**和**来源**说清楚，让"为什么这个目标被拒"
 * 变成一次可查的对照，而不是让用户对着一个空列表猜。
 *
 * 早期版本（v1.89）只把白名单写在 README 与 `.env.example` 里 ——
 * 部署后没人会回去翻。这一节补的就是"运行时可见"。
 */
export function NetworkPanel() {
  const [data, setData] = useState<NetworkRestrictions | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    loadNetworkRestrictions()
      .then((value) => {
        if (!cancelled) setData(value)
      })
      .catch((cause: unknown) => {
        if (cancelled) return
        setError(cause instanceof ApiError ? cause : new ApiError('NETWORK_LOAD_FAILED', '网络限制加载失败', 0))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <SettingsPanel
      id="network"
      title="网络限制"
      description="对话沙箱的出站目标守卫。命令里出现的目标必须落在这里列出的范围内，否则执行前就被拒绝。"
    >
      {loading ? (
        <SkeletonRows rows={2} rowClassName="h-16" />
      ) : error ? (
        <ErrorState
          error={error}
          title="网络限制加载失败"
          onRetry={() => window.location.reload()}
        />
      ) : data ? (
        <div className="flex flex-col gap-panel">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={cn(
                'rounded-xs px-2 py-0.5 text-2xs',
                data.guardEnabled ? 'bg-signal-ok/10 text-signal-ok' : 'bg-signal-warn/10 text-signal-warn',
              )}
            >
              {data.guardEnabled ? '守卫已启用' : '守卫已关闭'}
            </span>
            <span className="text-2xs text-ink-faint">
              关闭方式：`SUPEX_CHAT_SHELL_SCOPE_GUARD=0`（改完重启 API）
            </span>
          </div>

          <div className="grid gap-panel lg:grid-cols-2">
            <RangeBlock
              label="运维额外放行"
              hint="来自 SUPEX_CHAT_SHELL_ALLOWED_HOSTS，只该放固定设施（镜像站、内部包仓）"
              hosts={data.allowedHosts}
              cidrs={data.allowedCidrs}
              empty="没有额外放行项"
            />
            <RangeBlock
              label="内置基础设施"
              hint="包管理镜像、代码托管、公共 DNS 与环回 —— 代码常量，不可在此修改"
              hosts={data.builtinHosts}
              cidrs={data.builtinCidrs}
            />
          </div>

          <p className="text-2xs text-ink-faint">
            ⚠️ 这是**文本级**守卫：目标藏在变量/base64 里、或域名解析到内网仍能绕过。
            网络层边界需要按容器源 IP 下发防火墙规则，尚未实现。
          </p>
        </div>
      ) : null}
    </SettingsPanel>
  )
}

function RangeBlock({
  label,
  hint,
  hosts,
  cidrs,
  empty,
}: {
  label: string
  hint: string
  hosts: string[]
  cidrs: string[]
  empty?: string
}) {
  const total = hosts.length + cidrs.length
  return (
    <div className="rounded-md border border-line bg-surface-raised/40 p-panel">
      <p className="text-xs text-ink">{label}</p>
      <p className="mt-0.5 text-2xs text-ink-faint">{hint}</p>
      {total === 0 ? (
        <p className="mt-panel text-2xs text-ink-faint">{empty ?? '（空）'}</p>
      ) : (
        <div className="mt-panel flex flex-wrap gap-1">
          {[...cidrs, ...hosts].map((item) => (
            <code
              key={item}
              className="rounded-xs border border-line bg-surface px-1.5 py-0.5 text-2xs text-ink-muted"
            >
              {item}
            </code>
          ))}
        </div>
      )}
    </div>
  )
}

export default NetworkPanel
