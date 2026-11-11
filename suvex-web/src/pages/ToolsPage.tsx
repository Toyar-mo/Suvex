import { cn } from '@/lib/cn'
import { useI18n } from '@/i18n'
import { PageHeader } from '@/components/layout/AppShell'
import { RecipesPanel } from '@/components/kernel/RecipesPanel'
import { McpPanel } from '@/components/tools/McpPanel'
import { ToolListEmpty, ToolRow } from '@/components/tools/ToolRow'
import { Button } from '@/components/ui/Button'
import { ErrorState } from '@/components/ui/ErrorState'
import { Skeleton, SkeletonRows } from '@/components/ui/Skeleton'
import { useAuth } from '@/data/auth-store'
import {
  disabledCoreTools,
  ensureToolsLoaded,
  filterTools,
  groupTools,
  healthOf,
  loadTools,
  recipeCoverage,
  saveDisabledTools,
  saveMcpServers,
  testMcpServer,
  toolHealthCounts,
  useTools,
  type KernelTool,
} from '@/data/tools'
import {
  AlertCircle,
  Boxes,
  Check,
  Loader2,
  Plug,
  RotateCcw,
  ShieldQuestion,
  Terminal,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'

/**
 * 工具管理页。
 *
 * ## 这一页回答的问题
 *
 * 「这个智能体**此刻**能调什么，为什么有几个调不到，我的场景还缺什么？」
 *
 * 它把能力分成**三层**，三层的数据来源与操作节奏都不同：
 *
 * | 层 | 是什么 | 能做什么 |
 * | --- | --- | --- |
 * | 内置工具 | 引擎能调的动作（`run_shell` / `http_request` …） | 开关，下一次任务生效 |
 * | MCP 连接 | 外部工具服务 | 增删改、试连、白名单、超时 |
 * | 命令行配方 | 沙箱镜像里的二进制（nmap / sqlmap …） | **只读** —— 装没装由镜像决定 |
 *
 * 合成一个"工具"列表会造出假的权力：用户以为关掉 `nmap` 就调不动了，
 * 而 `run_shell nmap ...` 照样能跑。所以三层分开、各自成页签。
 *
 * ## 为什么是页签而不是三个常驻区块
 *
 * 三块加起来接近 50 行条目，叠在一起要滚很久；而用户一次只干一件事
 * （接一个服务 / 关一个能力 / 查一个工具怎么调）。摘要条与「能力缺口」
 * 常驻在页签上方，跨页签的信息不会被藏起来。
 */

type TabId = 'builtin' | 'mcp' | 'recipes'

const TABS: { id: TabId; label: string; hint: string }[] = [
  { id: 'builtin', label: 'tools.tab.builtin', hint: 'tools.tab.builtinHint' },
  { id: 'mcp', label: 'tools.tab.mcp', hint: 'tools.tab.mcpHint' },
  { id: 'recipes', label: 'tools.tab.recipes', hint: 'tools.tab.recipesHint' },
]

function tabFromHash(hash: string): TabId {
  const id = hash.replace('#', '')
  return id === 'mcp' || id === 'recipes' || id === 'builtin' ? id : 'builtin'
}

/**
 * 镜像能力缺口 —— **实测快照**，不是推导出来的。
 *
 * ★ 为什么写死在页面上：这些"缺"来自镜像里没有那个二进制，接口没有
 *   任何字段能表达它（配方只描述"有配方的工具怎么调"）。把它藏起来，
 *   用户只能在模型跑失败后倒推"是不是根本没有这个工具"。
 *
 * ⚠️ 换镜像时**必须**同步这张表。顶部写了实测日期，就是为了让下一个
 *   改镜像的人知道这里会过期，而不是把它当成永远正确的清单。
 */
function SummaryCard({
  label,
  value,
  detail,
  tone = 'default',
  icon,
}: {
  label: string
  value: string | number
  detail: string
  tone?: 'default' | 'warn' | 'risk'
  icon: React.ReactNode
}) {
  return (
    <div className="rounded-md border border-line bg-surface px-panel py-3">
      <div className="flex items-center gap-1.5 text-xs text-ink-muted">
        <span className="text-ink-faint" aria-hidden="true">
          {icon}
        </span>
        {label}
      </div>
      <p
        className={cn(
          'mt-1 text-xl font-medium tabular-nums',
          tone === 'default' && 'text-ink',
          tone === 'warn' && 'text-signal-warn',
          tone === 'risk' && 'text-signal-risk',
        )}
      >
        {value}
      </p>
      <p className="mt-0.5 text-2xs leading-snug text-ink-faint">{detail}</p>
    </div>
  )
}

export function ToolsPage() {
  const { t } = useI18n()
  const { mcp, catalog, recipes, status, error } = useTools()
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'

  /** 未保存的开关草稿（`null` = 无改动）。 */
  const [draftDisabled, setDraftDisabled] = useState<string[] | null>(null)
  const [savingTools, setSavingTools] = useState(false)
  const [toolsError, setToolsError] = useState<string | null>(null)
  const [savedAt, setSavedAt] = useState<string | null>(null)

  const [savingMcp, setSavingMcp] = useState(false)
  const [mcpError, setMcpError] = useState<string | null>(null)

  const [toolQuery, setToolQuery] = useState('')
  const [onlyProblems, setOnlyProblems] = useState(false)
  const [onlyLimited, setOnlyLimited] = useState(false)

  const [tab, setTab] = useState<TabId>(() => tabFromHash(window.location.hash))

  useEffect(() => {
    ensureToolsLoaded()
  }, [])

  // 页签写回 hash：刷新、分享链接都能落回同一层；用 replaceState 避免
  // 每次切页签都往历史里压一条（那会让"后退"变成切页签）。
  useEffect(() => {
    const next = `#${tab}`
    if (window.location.hash !== next) {
      window.history.replaceState(null, '', next)
    }
  }, [tab])

  const visibleTools = useMemo(
    () => filterTools(catalog.tools, { query: toolQuery, onlyProblems, onlyLimited }),
    [catalog.tools, toolQuery, onlyProblems, onlyLimited],
  )
  const grouped = useMemo(
    () => groupTools(visibleTools, catalog.groups),
    [visibleTools, catalog.groups],
  )
  const health = useMemo(() => healthOf(catalog.tools), [catalog.tools])
  const counts = useMemo(() => toolHealthCounts(catalog.tools), [catalog.tools])
  const coverage = useMemo(() => recipeCoverage(recipes.recipes), [recipes.recipes])

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

  /** 整组全开 / 全关 —— 逐条点 13 次是这个页面最烦的动作之一。 */
  function setGroupEnabled(tools: KernelTool[], enabled: boolean) {
    const current = new Set(effectiveDisabled)
    for (const tool of tools) {
      if (enabled) current.delete(tool.name)
      else current.add(tool.name)
    }
    setDraftDisabled([...current])
    setSavedAt(null)
    setToolsError(null)
  }

  async function handleSaveTools() {
    const next = effectiveDisabled
    if (!dirty) return

    const core = disabledCoreTools(catalog.tools, next)
    if (core.length > 0) {
      const names = core.map((tool) => tool.label).join('、')
      const ok = window.confirm(
        `${t('tools.confirm.core.title', { names })}\n\n` +
          t('tools.confirm.core.body') +
          `\n\n${t('tools.confirm.core.question')}`,
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
      setToolsError(err instanceof Error ? err.message : t('tools.saveFailed'))
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
      setMcpError(err instanceof Error ? err.message : t('tools.saveFailed'))
    } finally {
      setSavingMcp(false)
    }
  }

  const problemCount = counts.unconfigured + counts.misconfigured

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
            title={t('tools.error.title')}
            error={error ?? new Error(t('tools.error.noResponse'))}
            onRetry={() => void loadTools()}
          />
        ) : (
          <div className="flex flex-col gap-bay">
            {!isAdmin && (
              <div className="flex items-start gap-tight rounded-sm border border-line px-3 py-2 text-sm text-ink-muted">
                <ShieldQuestion className="mt-0.5 size-3.5 shrink-0" strokeWidth={1.75} />
                <span>
                  {t('tools.readonly.before')}
                  <b>{t('tools.readonly.bold')}</b>
                  {t('tools.readonly.after')}
                </span>
              </div>
            )}

            {/* ── 摘要：三层能力一眼看清 ─────────────────────── */}
            <section aria-label={t('tools.summary.aria')} className="grid grid-cols-2 gap-panel xl:grid-cols-3">
              <SummaryCard
                label={t('tools.summary.builtin')}
                value={counts.total}
                detail={`${t('tools.summary.builtinDetail', {
                  ready: counts.ready,
                  disabled: counts.disabled,
                })}${
                  problemCount > 0
                    ? ` · ${t('tools.summary.builtinProblem', { n: problemCount })}`
                    : ''
                }`}
                tone={problemCount > 0 ? 'warn' : 'default'}
                icon={<Boxes className="size-3.5" />}
              />
              <SummaryCard
                label={t('tools.summary.mcp')}
                value={mcp.servers.length}
                detail={
                  mcp.servers.length === 0
                    ? t('tools.summary.mcpNone')
                    : t('tools.summary.mcpDetail', {
                        http: mcp.servers.filter((s) => s.type === 'http').length,
                        cred: mcp.servers.filter((s) => s.hasEnv || s.hasHeaders).length,
                      })
                }
                icon={<Plug className="size-3.5" />}
              />
              <SummaryCard
                label={t('tools.summary.recipes')}
                value={recipes.recipes.length}
                detail={t('tools.summary.recipesDetail', {
                  ctf: coverage.ctf,
                  pentest: coverage.pentest,
                  limited: coverage.limited,
                })}
                icon={<Terminal className="size-3.5" />}
              />
            </section>

            {/* ── 页签 ────────────────────────────────────── */}
            <div role="tablist" aria-label={t('tools.tablistAria')} className="flex flex-wrap gap-1">
              {TABS.map((item) => {
                const selected = tab === item.id
                return (
                  <button
                    key={item.id}
                    role="tab"
                    aria-selected={selected}
                    type="button"
                    onClick={() => setTab(item.id)}
                    className={cn(
                      'flex flex-col items-start rounded-md border px-3 py-2 text-left transition-ui duration-fast',
                      selected
                        ? 'border-accent/50 bg-accent-wash text-ink'
                        : 'border-line text-ink-muted hover:border-line-strong hover:text-ink',
                    )}
                  >
                    <span className="flex items-center gap-1.5 text-sm">
                      {t(item.label)}
                      {item.id === 'builtin' && dirty && (
                        <span
                          className="size-1.5 rounded-pill bg-signal-warn"
                          title={t('tools.dirtyTitle')}
                          aria-label={t('tools.dirtyAria')}
                        />
                      )}
                    </span>
                    <span className="text-2xs text-ink-faint">{t(item.hint)}</span>
                  </button>
                )
              })}
            </div>

            {tab === 'mcp' && (
              <McpPanel
                servers={mcp.servers}
                saving={savingMcp || !isAdmin}
                error={mcpError}
                onSave={(servers) => void handleSaveMcp(servers)}
                onTest={testMcpServer}
              />
            )}

            {tab === 'builtin' && (
              <section className="rounded-md border border-line bg-surface">
                <header className="flex flex-wrap items-start justify-between gap-tight border-b border-line px-4 py-3">
                  <div>
                    <h2 className="text-sm font-medium text-ink">{t('tools.builtin.heading')}</h2>
                    <p className="mt-1 max-w-[64ch] text-sm leading-relaxed text-ink-muted">
                      {t('tools.builtin.body')}
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
                        {t('tools.enableAll')}
                      </Button>
                    )}
                    {savedAt && !dirty && (
                      <span className="flex items-center gap-1 text-xs text-ink-muted">
                        <Check className="size-3" strokeWidth={2} />
                        {t('tools.savedAt', {
                          time: new Date(savedAt).toLocaleTimeString('zh-CN'),
                        })}
                      </span>
                    )}
                    <Button
                      size="sm"
                      onClick={() => void handleSaveTools()}
                      disabled={!dirty || savingTools || !isAdmin}
                    >
                      {savingTools ? <Loader2 className="size-3.5 animate-spin" /> : null}
                      {dirty
                        ? t('tools.saveCount', { n: effectiveDisabled.length })
                        : t('tools.save')}
                    </Button>
                  </div>
                </header>

                <div className="flex flex-wrap items-center gap-tight border-b border-line px-4 py-2">
                  <input
                    value={toolQuery}
                    onChange={(event) => setToolQuery(event.target.value)}
                    placeholder={t('tools.searchPlaceholder')}
                    className="h-8 min-w-[220px] flex-1 rounded-sm border border-line bg-surface px-2 text-sm text-ink outline-none focus:border-line-accent"
                  />
                  <button
                    type="button"
                    onClick={() => setOnlyProblems((prev) => !prev)}
                    aria-pressed={onlyProblems}
                    className={cn(
                      'rounded-sm border px-2 py-1 text-xs transition-ui duration-fast',
                      onlyProblems
                        ? 'border-line-accent bg-accent-wash text-ink'
                        : 'border-line text-ink-muted hover:border-line-strong',
                    )}
                  >
                    {t('tools.filterProblems')}
                    {problemCount > 0 && (
                      <span className="ml-1 text-ink-faint">{problemCount}</span>
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => setOnlyLimited((prev) => !prev)}
                    aria-pressed={onlyLimited}
                    className={cn(
                      'rounded-sm border px-2 py-1 text-xs transition-ui duration-fast',
                      onlyLimited
                        ? 'border-line-accent bg-accent-wash text-ink'
                        : 'border-line text-ink-muted hover:border-line-strong',
                    )}
                  >
                    {t('tools.filterLimited')}
                  </button>
                  {(toolQuery || onlyProblems || onlyLimited) && (
                    <button
                      type="button"
                      onClick={() => {
                        setToolQuery('')
                        setOnlyProblems(false)
                        setOnlyLimited(false)
                      }}
                      className="text-xs text-ink-faint transition-ui duration-fast hover:text-ink"
                    >
                      {t('tools.clearFilters')}
                    </button>
                  )}
                  <span className="text-xs text-ink-faint">
                    {visibleTools.length} / {catalog.tools.length}
                  </span>
                </div>

                {toolsError && (
                  <div className="flex items-start gap-tight border-b border-line px-4 py-2.5 text-sm text-signal-risk">
                    <AlertCircle className="mt-0.5 size-3.5 shrink-0" strokeWidth={1.75} />
                    <span className="leading-relaxed">{toolsError}</span>
                  </div>
                )}

                {error && (
                  <div className="border-b border-line px-4 py-2 text-xs text-ink-faint">
                    {t('tools.partialError', { message: error.message })}
                  </div>
                )}

                {catalog.tools.length === 0 ? (
                  <ToolListEmpty reason={error?.message} />
                ) : (
                  grouped.map(({ group, tools }) => (
                    <div key={group}>
                      <div className="flex items-center justify-between border-b border-line/60 bg-surface-raised/30 px-4 py-1.5">
                        <h3 className="text-2xs uppercase tracking-wide text-ink-faint">{group}</h3>
                        {tools.length > 0 && (
                          <div className="flex items-center gap-2 text-2xs">
                            <button
                              type="button"
                              onClick={() => setGroupEnabled(tools, true)}
                              disabled={!isAdmin || savingTools}
                              className="text-ink-faint transition-ui duration-fast hover:text-ink disabled:cursor-not-allowed disabled:opacity-40"
                            >
                              {t('tools.groupAllOn')}
                            </button>
                            <button
                              type="button"
                              onClick={() => setGroupEnabled(tools, false)}
                              disabled={!isAdmin || savingTools}
                              className="text-ink-faint transition-ui duration-fast hover:text-ink disabled:cursor-not-allowed disabled:opacity-40"
                            >
                              {t('tools.groupAllOff')}
                            </button>
                          </div>
                        )}
                      </div>
                      {tools.length === 0 ? (
                        <p className="px-4 py-2 text-xs text-ink-faint">
                          {t('tools.groupEmpty')}
                        </p>
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
                              defaultLimitation={tool.defaultLimitation}
                              calls={tool.calls ?? 0}
                              lastCallAt={tool.lastCallAt}
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

                <footer className="border-t border-line px-4 py-2.5 text-xs leading-relaxed text-ink-faint">
                  {t('tools.footer.before')}
                  <b>{t('tools.footer.bold')}</b>
                  {t('tools.footer.after')}
                </footer>
              </section>
            )}

            {tab === 'recipes' && <RecipesPanel recipes={recipes.recipes} />}
          </div>
        )}
      </div>
    </>
  )
}

export default ToolsPage
