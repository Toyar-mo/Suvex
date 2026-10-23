import { cn } from '@/lib/cn'
import { PageHeader } from '@/components/layout/AppShell'
import { McpPanel } from '@/components/tools/McpPanel'
import { ToolListEmpty, ToolRow } from '@/components/tools/ToolRow'
import { Button } from '@/components/ui/Button'
import { ErrorState } from '@/components/ui/ErrorState'
import { Skeleton, SkeletonRows } from '@/components/ui/Skeleton'
import { useAuth } from '@/data/auth-store'
import {
  disabledCoreTools,
  ensureToolsLoaded,
  groupTools,
  healthOf,
  loadTools,
  saveDisabledTools,
  saveMcpServers,
  testMcpServer,
  useTools,
  type KernelTool,
} from '@/data/tools'
import { AlertCircle, Check, Loader2, RotateCcw, ShieldQuestion } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'

/**
 * 工具管理页。
 *
 * ## 这一页回答的问题
 *
 * 「这个智能体**此刻**能调什么，为什么有几个调不到？」
 *
 * 它**不是**设置页的一个区块，理由：设置页的四块都在回答"系统怎么跑"
 * （用哪个模型、沙箱给多少资源、提交通不通过人确认）。而这一页回答的是
 * 「能力边界」—— 它同时包含**外部接入**（MCP 连接，可增删、有凭据、要试连）
 * 与**内置开关**（13 个固定工具）。两件事的操作节奏完全不同，
 * 塞进设置页会让那一页既长又混（设置页已经有 9 节了）。
 *
 * ## 版面：两栏，左边"外部"右边"内置"
 *
 * 判据是**数据来源不同**：左边是用户创建的（可能 0 条，可能很多条），
 * 右边是代码里固定的 13 个（永远是那么多）。左边会长、会空；右边不会。
 * 所以左边可滚动、右边是静态表 —— 结构不同，不该硬拉成一样高。
 *
 * ## 三个必须分开显示的"不可用"理由
 *
 * 页面里任何一处"用不了"都要说清是哪一种（见 `toolHealth`）：
 * 被关掉了 / 依赖没配 / 配置有误。合成一个灰点等于让用户猜。
 */

export function ToolsPage() {
  const { mcp, catalog, status, error } = useTools()
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'

  /** 未保存的开关草稿（`null` = 无改动）。 */
  const [draftDisabled, setDraftDisabled] = useState<string[] | null>(null)
  const [savingTools, setSavingTools] = useState(false)
  const [toolsError, setToolsError] = useState<string | null>(null)
  const [savedAt, setSavedAt] = useState<string | null>(null)

  const [savingMcp, setSavingMcp] = useState(false)
  const [mcpError, setMcpError] = useState<string | null>(null)

  useEffect(() => {
    ensureToolsLoaded()
  }, [])

  const grouped = useMemo(
    () => groupTools(catalog.tools, catalog.groups),
    [catalog.tools, catalog.groups],
  )
  const health = useMemo(() => healthOf(catalog.tools), [catalog.tools])

  const effectiveDisabled = draftDisabled ?? catalog.disabled
  const dirty =
    draftDisabled !== null &&
    [...draftDisabled].sort().join(',') !== [...catalog.disabled].sort().join(',')

  function toggleTool(tool: KernelTool, next: boolean) {
    const current = new Set(effectiveDisabled)
    if (next) current.delete(tool.name)
    else current.add(tool.name)
    setDraftDisabled([...current])
    setSavedAt(null)
    setToolsError(null)
  }

  async function handleSaveTools() {
    const next = effectiveDisabled
    if (!dirty) return

    // ★ 关掉主力工具要二次确认。
    //
    //   判据不是"这个开关危险"（它不是安全边界），而是"它的后果不明显"：
    //   关掉 `run_shell` 之后每一次任务都会明显变差，而现场看起来像
    //   模型变笨 —— 用户很难把两件事联系起来。所以这里拦一下。
    const core = disabledCoreTools(catalog.tools, next)
    if (core.length > 0) {
      const names = core.map((tool) => tool.label).join('、')
      const ok = window.confirm(
        `要关掉「${names}」？\n\n` +
          '这些是主力工具：关掉之后，之后每一次扫描/解题的能力都会明显下降，' +
          '而失败现场看起来会像"模型变笨了"，不容易联想到是这里关掉的。\n\n' +
          '确定要关吗？',
      )
      if (!ok) return
    }

    setSavingTools(true)
    setToolsError(null)
    try {
      await saveDisabledTools(next)
      setDraftDisabled(null)
      setSavedAt(new Date().toISOString())
    } catch (err) {
      setToolsError(err instanceof Error ? err.message : '保存失败。')
    } finally {
      setSavingTools(false)
    }
  }

  async function handleSaveMcp(servers: Parameters<typeof saveMcpServers>[0]) {
    setSavingMcp(true)
    setMcpError(null)
    try {
      await saveMcpServers(servers)
    } catch (err) {
      setMcpError(err instanceof Error ? err.message : '保存失败。')
    } finally {
      setSavingMcp(false)
    }
  }

  return (
    <>
      <PageHeader navId="tools" />

      <div className="px-gutter py-bay">
        {status === 'loading' ? (
          <div className="flex flex-col gap-bay">
            <Skeleton className="h-40" />
            <SkeletonRows rows={4} rowClassName="h-12" />
          </div>
        ) : status === 'error' ? (
          <ErrorState
            title="拿不到工具清单"
            error={error ?? new Error('后端没有响应。')}
            onRetry={() => void loadTools()}
          />
        ) : (
          <div className="flex flex-col gap-bay">
            {/*
              ★ 顶部一句话说清"这一页在看什么"，并且**只在非管理员时**
                提示只读 —— 让只读用户先知道为什么开关点不动，
                而不是点一下才发现。
            */}
            {!isAdmin && (
              <div className="flex items-start gap-tight rounded-sm border border-line px-3 py-2 text-[13px] text-ink-muted">
                <ShieldQuestion className="mt-0.5 size-3.5 shrink-0" strokeWidth={1.75} />
                <span>
                  你现在的角色是<b>只读</b>：能看清单，但改不了开关。
                  需要调整请找管理员。
                </span>
              </div>
            )}

            {/* ── 左：MCP 连接（外部接入）──────────────────── */}
            <McpPanel
              servers={mcp.servers}
              gogoReady={mcp.gogoReady}
              gogoReason={mcp.gogoReason}
              saving={savingMcp || !isAdmin}
              error={mcpError}
              onSave={(servers) => void handleSaveMcp(servers)}
              onTest={testMcpServer}
            />

            {/* ── 右：内置工具（开关）──────────────────────── */}
            <section className="rounded-md border border-line bg-surface">
              <header className="flex flex-wrap items-start justify-between gap-tight border-b border-line px-4 py-3">
                <div>
                  <h2 className="text-sm font-medium text-ink">内置工具</h2>
                  <p className="mt-1 max-w-[64ch] text-[13px] leading-relaxed text-ink-muted">
                    智能体在沙箱里能调的全部动作。这里的开关<b>不改变进程边界</b> ——
                    隔离在沙箱、URL 白名单与 hostguard 里；关掉某个工具只是让它这一侧
                    少一个能力。
                  </p>
                </div>

                <div className="flex items-center gap-tight">
                  {effectiveDisabled.length > 0 && (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        setDraftDisabled([])
                        setSavedAt(null)
                      }}
                      disabled={!isAdmin || savingTools}
                    >
                      <RotateCcw className="size-3.5" strokeWidth={1.75} />
                      全部开启
                    </Button>
                  )}
                  {/*
                    ★ 保存状态**常驻**，不用 toast —— 与设置页同一条纪律
                      （design-system §12：toast 把"看过"当成了"知道"）。
                  */}
                  {savedAt && !dirty && (
                    <span className="flex items-center gap-1 text-[12px] text-ink-muted">
                      <Check className="size-3" strokeWidth={2} />
                      已保存 · {new Date(savedAt).toLocaleTimeString('zh-CN')}
                    </span>
                  )}
                  <Button
                    size="sm"
                    onClick={() => void handleSaveTools()}
                    disabled={!dirty || savingTools || !isAdmin}
                  >
                    {savingTools ? <Loader2 className="size-3.5 animate-spin" /> : null}
                    {dirty ? `保存 ${effectiveDisabled.length} 项改动` : '保存'}
                  </Button>
                </div>
              </header>

              {toolsError && (
                <div className="flex items-start gap-tight border-b border-line px-4 py-2.5 text-[13px] text-signal-risk">
                  <AlertCircle className="mt-0.5 size-3.5 shrink-0" strokeWidth={1.75} />
                  <span className="leading-relaxed">{toolsError}</span>
                </div>
              )}

              {/*
                ★ 部分加载失败时（store 里 `status==='ready'` 但 `error` 非空），
                  这里提示一句"另一半没拿到"，而不是把已拿到的也藏起来。
              */}
              {error && (
                <div className="border-b border-line px-4 py-2 text-[12px] text-ink-faint">
                  一部分数据没加载出来：{error.message}
                </div>
              )}

              {catalog.tools.length === 0 ? (
                <ToolListEmpty reason={error?.message} />
              ) : (
                grouped.map(({ group, tools }) => (
                  <div key={group}>
                    <h3 className="border-b border-line/60 bg-surface-raised/30 px-4 py-1.5 text-[11px] tracking-wide text-ink-faint uppercase">
                      {group}
                    </h3>
                    {tools.length === 0 ? (
                      <p className="px-4 py-2 text-[12px] text-ink-faint">这一栏是空的。</p>
                    ) : (
                      <ul>
                        {tools.map((tool) => (
                          <ToolRow
                            key={tool.name}
                            name={tool.name}
                            label={tool.label}
                            summary={tool.summary}
                            params={tool.params}
                            health={health[tool.name] ?? 'ready'}
                            core={tool.core}
                            requiresConnection={tool.requiresConnection}
                            reason={tool.reason}
                            enabled={
                              draftDisabled === null
                                ? tool.enabled
                                : !draftDisabled.includes(tool.name)
                            }
                            onToggle={(next) => toggleTool(tool, next)}
                          />
                        ))}
                      </ul>
                    )}
                  </div>
                ))
              )}

              <footer
                className={cn(
                  'border-t border-line px-4 py-2.5 text-[12px] leading-relaxed text-ink-faint',
                )}
              >
                改动在<b>下一次任务启动时</b>生效 —— 已经跑起来的任务用的是它启动那一刻的配置。
              </footer>
            </section>
          </div>
        )}
      </div>
    </>
  )
}

export default ToolsPage
