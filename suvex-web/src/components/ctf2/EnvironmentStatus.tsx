import { cn } from '@/lib/cn'
import {
  ENVIRONMENT_STATUS_LABEL,
  type Ctf2Environment,
  type Ctf2EnvironmentStatus,
} from '@/data/ctf2'
import { showToast } from '@/data/toast-store'
import { copyText } from '@/lib/clipboard'
import { Check, Copy, ExternalLink, Loader2, TriangleAlert } from 'lucide-react'
import { useState } from 'react'

/**
 * 题目环境的状态徽标 + 访问入口。
 *
 * ## ★ 为什么它单独一个组件
 *
 * 环境状态在**三个**地方出现：题目卡片、确认提交弹窗、题目详情。
 * 而它有一套不平凡的展示规则（见下），三处各写一遍必然漂移 ——
 * 而漂移的表现是"同一个环境在卡片上说就绪、在弹窗上说启动中"，
 * 那会让人完全不知道该信哪个。
 *
 * ## 展示规则里两条要紧的
 *
 * **① `accessUrl` 只在 `ready` 时渲染。**
 * 数据库层有一条约束守着这件事（`access_url_only_when_ready`），
 * 所以前端这里再判一次不是"防御性编程"，而是**同一句话的第二处表达**：
 * 一个非 ready 状态下出现的可点链接，用户点进去是连接被拒 ——
 * 而那会被报告成"你们的链接坏了"。
 *
 * **② `error` 与 `expired` 的补救提示不同。**
 * 到期是正常老化（重新启动即可），错误要去看日志。
 * 混成一句"环境异常"，用户不知道该点重启还是该找人。
 */
export function EnvironmentStatus({
  environment,
  onRelease,
  compact = false,
}: {
  environment: Ctf2Environment | null
  onRelease?: () => void
  /** 卡片上的紧凑形态：只显示徽标 + 入口，不套外框 */
  compact?: boolean
}) {
  if (!environment) {
    return (
      <span className="inline-flex items-center gap-1.5 text-2xs text-ink-faint">
        <span className="h-1.5 w-1.5 shrink-0 rounded-pill bg-ink-ghost" aria-hidden="true" />
        环境未启动
      </span>
    )
  }

  const status = environment.status
  const urls = environment.accessUrls.length
    ? environment.accessUrls
    : environment.accessUrl
      ? [environment.accessUrl]
      : []

  return (
    <div
      className={cn(
        'flex flex-col gap-1.5',
        !compact && 'rounded-sm border border-line bg-surface-sunken p-2.5',
      )}
    >
      <div className="flex flex-wrap items-center gap-x-tight gap-y-1">
        <StatusBadge status={status} />
        {environment.pollCount > 0 && (
          <span className="tabular text-2xs text-ink-faint">轮询 {environment.pollCount} 次</span>
        )}
        {urls.length > 1 && (
          <span className="tabular text-2xs text-ink-faint">{urls.length} 个入口</span>
        )}
        {onRelease && status === 'ready' && (
          <button
            type="button"
            onClick={onRelease}
            className="ml-auto text-2xs text-ink-muted underline transition-ui duration-fast hover:text-ink"
          >
            释放环境
          </button>
        )}
      </div>

      {/* ① 地址**只在 ready 时**渲染。见文件头那段。 */}
      {status === 'ready' && urls.length > 0 && (
        <div className="flex flex-col gap-1">
          {urls.map((url) => (
            <AccessUrl key={url} url={url} />
          ))}
        </div>
      )}

      {/*
        ② 失败原因原样显示（后端给的是具体的中文）。
        截断到两行是布局考虑，但**不改成"环境异常"这类笼统说法** ——
        具体原因（"轮询 200 次仍未就绪"）才是用户能采取行动的依据。
      */}
      {environment.lastError && status !== 'ready' && (
        <p className="flex items-start gap-1.5 text-2xs text-ink-muted">
          <TriangleAlert
            size={11}
            strokeWidth={2}
            className="mt-0.5 shrink-0 text-signal-warn"
            aria-hidden="true"
          />
          <span className="line-clamp-2">{environment.lastError}</span>
        </p>
      )}
    </div>
  )
}

function StatusBadge({ status }: { status: Ctf2EnvironmentStatus }) {
  const spinning = status === 'starting'
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-pill px-1.5 py-0.5 text-2xs',
        status === 'ready' && 'bg-signal-ok/15 text-signal-ok',
        status === 'starting' && 'bg-accent-wash text-accent-strong',
        status === 'error' && 'bg-signal-risk/15 text-signal-risk',
        status === 'expired' && 'bg-signal-warn/15 text-signal-warn',
        status === 'stopped' && 'bg-ink-ghost/20 text-ink-faint',
      )}
    >
      {spinning ? (
        <Loader2 size={10} strokeWidth={2} className="animate-spin" aria-hidden="true" />
      ) : null}
      {ENVIRONMENT_STATUS_LABEL[status]}
    </span>
  )
}

/**
 * 一个可复制、可打开的访问地址。
 *
 * ⚠️ `rel="noreferrer noopener"` 是必须的：题目环境是**外部**地址，
 * 而 `target="_blank"` 不带 `noopener` 时新页面能通过 `window.opener`
 * 改我们这一页的地址（反向 tabnabbing）。`noreferrer` 顺手把
 * Referer 也去掉 —— 我们这一页的 URL 里可能有 query（比如题目 id）。
 */
function AccessUrl({ url }: { url: string }) {
  const [copied, setCopied] = useState(false)

  const copy = () => {
    // `navigator.clipboard` 在非安全上下文里**不存在**（本系统按 http +
    // 局域网 IP 部署，实测 `isSecureContext === false`），走降级链。
    // 见 `@/lib/clipboard` 的说明。
    void copyText(url).then((ok) => {
      if (!ok) {
        showToast({ message: '复制失败，请手动选中后按 Ctrl+C', tone: 'risk' })
        return
      }
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    })
  }

  return (
    <span className="flex items-center gap-1.5 rounded-xs bg-surface px-1.5 py-1">
      <code className="mono-ui min-w-0 flex-1 truncate text-2xs text-ink-muted" title={url}>
        {url}
      </code>
      <button
        type="button"
        onClick={copy}
        aria-label={`复制 ${url}`}
        className="shrink-0 rounded-xs p-0.5 text-ink-faint transition-ui duration-fast hover:text-ink"
      >
        {copied ? (
          <Check size={11} strokeWidth={2} className="text-signal-ok" aria-hidden="true" />
        ) : (
          <Copy size={11} strokeWidth={2} aria-hidden="true" />
        )}
      </button>
      <a
        href={url}
        target="_blank"
        rel="noreferrer noopener"
        aria-label={`在新标签打开 ${url}`}
        className="shrink-0 rounded-xs p-0.5 text-ink-faint transition-ui duration-fast hover:text-accent-strong"
      >
        <ExternalLink size={11} strokeWidth={2} aria-hidden="true" />
      </a>
    </span>
  )
}

export default EnvironmentStatus
