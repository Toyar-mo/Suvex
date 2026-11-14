import { cn } from '@/lib/cn'
import { Button } from '@/components/ui/Button'
import {
  parseAllowedTools,
  parseArgs,
  parseEnv,
  parseHeaders,
  parseTimeout,
  validateDraft,
  type McpServer,
  type McpServerDraft,
  type McpTestResult,
} from '@/data/tools'
import { AlertCircle, Check, Loader2, Plus, X } from 'lucide-react'
import { useState } from 'react'
import { Select } from '@/components/ui/Select'
import { useI18n } from '@/i18n'

/**
 * MCP 连接的管理区块。
 *
 * ## 这个区块要回答的问题
 *
 * 不是"我配了几条连接"，而是 **「智能体除了沙箱里的工具，还能调到什么」**。
 * 所以每条连接都要显示 command 与白名单 —— `command` 决定服务端会
 * `exec` 什么，白名单决定它能被调用的面。
 *
 * ## 为什么"试连"是独立于"保存"的动作
 *
 * 用户在填 `command` 时唯一的疑问是"这个填对了没"。在此之前他只能
 * 保存 → 跑一次真实任务 → 从模型的工具报错里倒推，而那条路上的失败
 * 既说不清原因，又会污染一次已受理的任务（这个理由后端
 * `service.test_mcp_server` 的 docstring 里也写了同一件事）。
 *
 * 所以：**保存**回答"存住了吗"，**试连**回答"打通了吗"。
 * 两个动作、两个结果、两种措辞，不混用（design-system §12 同一条纪律）。
 *
 * ## ⚠️ `env` 只进不出 → 编辑时必须用 `preserveEnv`
 *
 * 已保存的 `env` 不回显（后端只回 `hasEnv`），所以编辑时输入框必然是空的。
 * 而保存是**全量替换** —— 如果直接提交空 `env`，凭据就被静默清掉了。
 *
 * 处理办法不是"提示用户重填"（那是把系统缺陷变成用户负担），而是带上
 * `preserveEnv: true` 明确告诉后端"这次别动 env"。
 * 只有用户**真的**在 env 框里输入了新内容时才提交它并去掉这个标记。
 */

export interface McpPanelProps {
  servers: McpServer[]
  saving: boolean
  error: string | null
  onSave: (servers: McpServerDraft[]) => void
  onTest: (draft: McpServerDraft) => Promise<McpTestResult>
}

/** 空草稿（新增一条时用）。`preserveEnv` 为 false —— 新增的没有旧值可保。 */
function emptyDraft(): McpServerDraft {
  return {
    name: '',
    type: 'stdio',
    command: '',
    url: '',
    headers: {},
    args: [],
    env: {},
    notes: '',
    allowedTools: null,
    preserveEnv: false,
    // ★ v1.62：新增时没有旧请求头可保。
    preserveHeaders: false,
    // ★ v0.83：新增时不给超时（`null` = 用内核默认 60 秒）。
    callTimeout: null,
  }
}

/**
 * 常用 MCP 模板只负责**预填**，不自动保存。
 *
 * 端口/启动方式因插件版本而异，所以 `notes` 必须写清“以实际监听为准”；
 * 直接保存一条没验证过的连接，会制造“配了但不生效”的假象。
 */
interface McpPreset {
  id: string
  label: string
  name: string
  type: McpServer['type']
  command: string
  args: string[]
  url: string
  notes: string
}

const MCP_PRESETS: readonly McpPreset[] = [
  {
    id: 'burp',
    label: 'Burp Suite',
    name: 'burp',
    type: 'http',
    command: '',
    args: [],
    url: 'http://127.0.0.1:9876/sse',
    notes: '需在 Burp 安装 MCP 插件并启用 HTTP/SSE；端口以插件实际监听为准。',
  },
  {
    id: 'ida',
    label: 'IDA Pro',
    name: 'ida',
    type: 'http',
    command: '',
    args: [],
    url: 'http://127.0.0.1:13337/mcp',
    notes: '需在 IDA 中启动 ida-pro-mcp / IDA MCP 服务；端口以其实际监听为准。',
  },
  {
    id: 'ghidra',
    label: 'Ghidra',
    name: 'ghidra',
    type: 'http',
    command: '',
    args: [],
    url: 'http://127.0.0.1:8080/mcp',
    notes: '需启动 GhidraMCP 插件；不同插件默认端口不同，以实际监听为准。',
  },
  {
    id: 'chrome-devtools',
    label: 'Chrome DevTools',
    name: 'chrome-devtools',
    type: 'stdio',
    command: 'npx',
    args: ['-y', 'chrome-devtools-mcp@latest'],
    url: '',
    notes: 'stdio 模板；需要宿主有 Node/npx 和 Chromium。首次 npx 会下载包。',
  },
]

/** 已保存的连接 → 编辑用草稿。`env`/`headers` 恒为空、`preserve*` 恒为真。 */
function draftOf(server: McpServer): McpServerDraft {
  return {
    name: server.name,
    type: server.type,
    command: server.command,
    url: server.url,
    // headers 不回显（可能含 token），留空 + preserveHeaders=true 表示"别动它"。
    headers: {},
    args: [...server.args],
    env: {},
    notes: server.notes,
    allowedTools: server.allowedTools ? [...server.allowedTools] : null,
    // ★ 编辑已有连接：默认不动 env（见文件头那段）。
    preserveEnv: true,
    // ★ v1.62：编辑已有连接：默认不动 headers（与 env 同一纪律）。
    preserveHeaders: true,
    // ★ 编辑时把已存的超时带回来（它是可回显字段，不像 env）。
    callTimeout: server.callTimeout,
  }
}

export function McpPanel({ servers, saving, error, onSave, onTest }: McpPanelProps) {
  const { t } = useI18n()
  /** 正在编辑的草稿；`null` = 没在编辑。 */
  const [draft, setDraft] = useState<McpServerDraft | null>(null)
  /** 编辑的是哪一条（`null` = 新增）。用名字标识，因为名字是唯一键。 */
  const [editingName, setEditingName] = useState<string | null>(null)
  const [argsText, setArgsText] = useState('')
  const [envText, setEnvText] = useState('')
  /** ★ v1.62：http 请求头输入（每行 `KEY: VALUE` 或 `KEY=VALUE`）。不回显，留空 = 保留原值。 */
  const [headersText, setHeadersText] = useState('')
  const [toolsText, setToolsText] = useState('')
  /**
   * ★ v0.83：超时用**文本**承载，因为"空"与"0"语义不同。
   *
   * 空串 = 不配置（用内核默认 60 秒）；`0` 是一个非法值，后端会 422。
   * 用 number 状态的话，清空输入框会变成 NaN 或被当成 0，
   * 而 0 会被 `Field(gt=0)` 拒掉 —— 用户会看到一个莫名其妙的"必须大于 0"，
   * 而他只是想把这项改回默认。
   */
  const [timeoutText, setTimeoutText] = useState('')
  const [formError, setFormError] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [preset, setPreset] = useState('')
  const [testResult, setTestResult] = useState<McpTestResult | null>(null)
  /**
   * 已保存连接的**逐条试连结果**（v1.79）。
   *
   * 以前「试连」只在编辑表单里：想确认一条旧连接还通不通，得先点编辑、
   * 再点试连 —— 而编辑表单会把凭据输入框清空，用户会担心"我只是想测一下，
   * 别把配置改了"。现在列表行上直接可以测。
   */
  const [rowTests, setRowTests] = useState<
    Record<string, { status: 'testing' | 'ok' | 'fail'; text: string }>
  >({})

  async function testSaved(server: McpServer) {
    setRowTests((prev) => ({
      ...prev,
      [server.name]: { status: 'testing', text: t('tools.mcp.testConnecting') },
    }))
    try {
      const result = await onTest(draftOf(server))
      setRowTests((prev) => ({
        ...prev,
        [server.name]: result.ok
          ? {
              status: 'ok',
              text:
                t('tools.mcp.testOkTools', { n: result.tools.length }) +
                (result.elapsedMs ? t('tools.mcp.testOkMs', { ms: result.elapsedMs }) : ''),
            }
          : { status: 'fail', text: result.reason || t('tools.mcp.testFail') },
      }))
    } catch (error) {
      setRowTests((prev) => ({
        ...prev,
        [server.name]: {
          status: 'fail',
          text: error instanceof Error ? error.message : t('tools.mcp.testRequestFailed'),
        },
      }))
    }
  }

  function startAdd() {
    const next = emptyDraft()
    setDraft(next)
    setEditingName(null)
    setArgsText('')
    setEnvText('')
    setHeadersText('')
    setToolsText('')
    setTimeoutText('')
    setFormError(null)
    setTestResult(null)
  }

  function startPreset(presetId: string) {
    const item = MCP_PRESETS.find((entry) => entry.id === presetId)
    if (!item) return
    const next = emptyDraft()
    next.name = item.name
    next.type = item.type
    next.command = item.command
    next.args = [...item.args]
    next.url = item.url
    next.notes = item.notes
    setDraft(next)
    setEditingName(null)
    setArgsText(item.args.join(' '))
    setEnvText('')
    setHeadersText('')
    setToolsText('')
    setTimeoutText('')
    setFormError(null)
    setTestResult(null)
    setPreset('')
  }

  function startEdit(server: McpServer) {
    setDraft(draftOf(server))
    setEditingName(server.name)
    setArgsText(server.args.join(' '))
    // env 不回显 —— 留空表示"不改动"（见文件头）
    setEnvText('')
    // headers 同样不回显 —— 留空表示"不改动"
    setHeadersText('')
    setToolsText(server.allowedTools ? server.allowedTools.join(', ') : '')
    // 超时可回显（它不像 env 那样是凭据）
    setTimeoutText(server.callTimeout == null ? '' : String(server.callTimeout))
    setFormError(null)
    setTestResult(null)
  }

  function cancel() {
    setDraft(null)
    setEditingName(null)
    setFormError(null)
    setTestResult(null)
  }

  /** 把界面上的几段文本拼回草稿。 */
  function currentDraft(): McpServerDraft {
    if (!draft) return emptyDraft()
    const env = parseEnv(envText)
    const headers = parseHeaders(headersText)
    return {
      ...draft,
      args: parseArgs(argsText),
      env,
      headers,
      allowedTools: parseAllowedTools(toolsText),
      // ★ 用户在 env 框里**真的写了东西** → 提交它并取消"保留"；
      //   编辑时留空 → 保留原值（不传空 env 把凭据清掉）。
      preserveEnv: editingName !== null && Object.keys(env).length === 0,
      // ★ v1.62：headers 同一纪律 —— 编辑时留空 = 保留原值。
      preserveHeaders: editingName !== null && Object.keys(headers).length === 0,
      // ★ v0.83：空串 = 不配置（用内核默认）；否则按数字提交。
      callTimeout: parseTimeout(timeoutText),
    }
  }

  async function handleTest() {
    const next = currentDraft()
    const invalid = validateDraft(next)
    if (invalid) {
      setFormError(invalid)
      return
    }
    setFormError(null)
    setTesting(true)
    setTestResult(null)
    try {
      setTestResult(await onTest(next))
    } catch (error) {
      setTestResult({
        ok: false,
        reason: error instanceof Error ? error.message : t('tools.mcp.testRequestFailed'),
        tools: [],
        serverName: '',
        serverVersion: '',
        elapsedMs: 0,
      })
    } finally {
      setTesting(false)
    }
  }

  function handleCommit() {
    const next = currentDraft()
    const invalid = validateDraft(next)
    if (invalid) {
      setFormError(invalid)
      return
    }
    const others = servers
      .filter((server) => server.name !== editingName)
      .map((server) => draftOf(server))

    // ⚠️ 已有的 env 拿不回来（后端只回 hasEnv），所以这里有一个必须说清的
    //    语义：编辑时 env 框留空 = 保留原值，填了才覆盖。
    //
    //    实现上做不到"留空就保留"——我们提交的是整份草稿，env 为空对象
    //    会让后端把那条连接的 env 清掉。所以这里**显式提示**用户：
    //    编辑已有连接时必须重填 env，否则会丢。见下面渲染里的警告。
    onSave([
      ...others,
      {
        ...next,
        name: next.name.trim(),
        command: next.type === 'http' ? '' : next.command.trim(),
        url: next.type === 'http' ? next.url.trim() : '',
      },
    ])
    setDraft(null)
    setEditingName(null)
  }

  function handleRemove(name: string) {
    const rest = servers.filter((server) => server.name !== name).map((server) => draftOf(server))
    onSave(rest)
  }

  return (
    <section className="rounded-md border border-line bg-surface">
      <header className="flex items-start justify-between gap-tight border-b border-line px-4 py-3">
        <div>
          <h2 className="text-sm font-medium text-ink">{t('tools.mcp.heading')}</h2>
          <p className="mt-1 max-w-[62ch] text-sm leading-relaxed text-ink-muted">
            {t('tools.mcp.body.before')}{' '}
            <code className="font-mono">list_mcps</code> /{' '}
            <code className="font-mono">call_mcp</code>
            {t('tools.mcp.body.after')}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {/* 一眼看清规模与凭据面：http/stdio 各几条、有没有带凭据的 */}
          <span className="text-xs text-ink-faint">
            {t('tools.mcp.count', { n: servers.length })}
            {servers.length > 0 && (
              <>
                {' · '}
                {servers.filter((server) => server.type === 'http').length} HTTP
                {' · '}
                {t('tools.mcp.withCred', {
                  n: servers.filter((server) => server.hasEnv || server.hasHeaders).length,
                })}
              </>
            )}
          </span>
          <Select
            value={preset}
            options={[
              { value: '', label: t('tools.mcp.preset', undefined, '从模板添加') },
              ...MCP_PRESETS.map((item) => ({ value: item.id, label: item.label })),
            ]}
            onChange={startPreset}
            disabled={saving || draft !== null}
            size="sm"
            ariaLabel={t('tools.mcp.preset', undefined, '从模板添加')}
            className="w-36"
          />
          <Button
            size="sm"
            variant="secondary"
            onClick={startAdd}
            disabled={saving || draft !== null}
          >
            <Plus className="size-3.5" strokeWidth={1.75} />
            {t('tools.mcp.add')}
          </Button>
        </div>
      </header>

      {/* 已保存的连接 */}
      {servers.length === 0 ? (
        <div className="px-4 py-6 text-sm text-ink-muted">
          <p>{t('tools.mcp.emptyTitle')}</p>
          <p className="mt-1 text-xs text-ink-faint">{t('tools.mcp.emptyHint')}</p>
        </div>
      ) : (
        <ul className="divide-y divide-line/60">
          {servers.map((server) => (
            <li key={server.name} className="px-4 py-3">
              <div className="flex flex-wrap items-baseline gap-x-tight gap-y-1">
                <code className="font-mono text-sm text-ink">{server.name}</code>
                {/* ★ v1.62：传输类型徽标，一眼分清 stdio / http。 */}
                <span className="rounded-sm border border-line px-1 py-px text-2xs text-ink-faint">
                  {server.type === 'http' ? 'HTTP' : 'stdio'}
                </span>
                {server.hasEnv && (
                  <span className="rounded-sm border border-line px-1 py-px text-2xs text-ink-faint">
                    {t('tools.mcp.hasEnv')}
                  </span>
                )}
                {server.hasHeaders && (
                  <span className="rounded-sm border border-line px-1 py-px text-2xs text-ink-faint">
                    {t('tools.mcp.hasHeaders')}
                  </span>
                )}
                {server.allowedTools && (
                  <span className="text-2xs text-ink-faint">
                    {t('tools.mcp.allowlist', { n: server.allowedTools.length })}
                  </span>
                )}
                {/*
                  ★ v0.83：把生效的超时显示在列表里。
                    不显示的话，"这条连接到底是不是 60 秒"只能点进编辑才知道，
                    而超时配错的症状是"调用偶尔失败"，没人会想到去核对它。
                    没配时显示内核默认值，让人知道它确实是 60 而不是"没限制"。
                */}
                <span className="text-2xs text-ink-faint">
                  {t('tools.mcp.timeout', {
                    value:
                      server.callTimeout == null
                        ? t('tools.mcp.timeoutDefault')
                        : server.callTimeout,
                  })}
                </span>
              </div>
              <p className="mt-1 break-all font-mono text-xs text-ink-muted">
                {server.type === 'http'
                  ? `http ${server.url}`
                  : `${server.command} ${server.args.join(' ')}`}
              </p>
              {server.notes && <p className="mt-1 text-xs text-ink-faint">{server.notes}</p>}
              <div className="mt-2 flex flex-wrap items-center gap-tight">
                <button
                  type="button"
                  onClick={() => void testSaved(server)}
                  disabled={saving || rowTests[server.name]?.status === 'testing'}
                  className="text-xs text-ink-muted transition-ui duration-fast hover:text-ink disabled:opacity-50"
                >
                  {rowTests[server.name]?.status === 'testing'
                    ? t('tools.mcp.testing')
                    : t('tools.mcp.test')}
                </button>
                <button
                  type="button"
                  onClick={() => startEdit(server)}
                  disabled={saving}
                  className="text-xs text-ink-muted transition-ui duration-fast hover:text-ink disabled:opacity-50"
                >
                  {t('tools.mcp.edit')}
                </button>
                <button
                  type="button"
                  onClick={() => handleRemove(server.name)}
                  disabled={saving}
                  className="text-xs text-ink-muted transition-ui duration-fast hover:text-signal-risk disabled:opacity-50"
                >
                  {t('tools.mcp.delete')}
                </button>
                {rowTests[server.name] && rowTests[server.name].status !== 'testing' && (
                  <span
                    role="status"
                    className={cn(
                      'text-2xs',
                      rowTests[server.name].status === 'ok' ? 'text-signal-ok' : 'text-signal-risk',
                    )}
                  >
                    {rowTests[server.name].text}
                  </span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      {/* 保存错误 */}
      {error && (
        <div className="flex items-start gap-tight border-t border-line px-4 py-2.5 text-sm text-signal-risk">
          <AlertCircle className="mt-0.5 size-3.5 shrink-0" strokeWidth={1.75} />
          <span className="leading-relaxed">{error}</span>
        </div>
      )}

      {/* 编辑表单 */}
      {draft && (
        <div className="border-t border-line bg-surface-raised/40 px-4 py-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium text-ink">
              {editingName ? t('tools.mcp.editTitle', { name: editingName }) : t('tools.mcp.add')}
            </h3>
            <button
              type="button"
              onClick={cancel}
              className="text-ink-faint transition-ui duration-fast hover:text-ink"
              aria-label={t('tools.mcp.cancelAria')}
            >
              <X className="size-4" strokeWidth={1.75} />
            </button>
          </div>

          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-ink-muted">{t('tools.mcp.nameLabel')}</span>
              <input
                value={draft.name}
                onChange={(event) => setDraft({ ...draft, name: event.target.value })}
                placeholder="demo"
                className="h-8 rounded-sm border border-line bg-surface px-2 font-mono text-sm text-ink outline-none focus:border-line-accent"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-ink-muted">{t('tools.mcp.typeLabel')}</span>
              <Select
                size="sm"
                value={draft.type}
                options={[
                  { value: 'stdio', label: t('tools.mcp.typeStdio') },
                  { value: 'http', label: t('tools.mcp.typeHttp') },
                ]}
                onChange={(type) => setDraft({ ...draft, type })}
                ariaLabel={t('tools.mcp.typeLabel')}
              />
            </label>
          </div>

          {/* ★ v1.62：按类型切换 url / command 输入框。 */}
          {draft.type === 'http' ? (
            <label className="mt-2 flex flex-col gap-1">
              <span className="text-xs text-ink-muted">{t('tools.mcp.urlLabel')}</span>
              <input
                value={draft.url}
                onChange={(event) => setDraft({ ...draft, url: event.target.value })}
                placeholder="https://example.com/api/mcp"
                className="h-8 rounded-sm border border-line bg-surface px-2 font-mono text-sm text-ink outline-none focus:border-line-accent"
              />
            </label>
          ) : (
            <>
              <label className="mt-2 flex flex-col gap-1">
                <span className="text-xs text-ink-muted">{t('tools.mcp.commandLabel')}</span>
                <input
                  value={draft.command}
                  onChange={(event) => setDraft({ ...draft, command: event.target.value })}
                  placeholder="/opt/suvex/demo_mcp.py"
                  className="h-8 rounded-sm border border-line bg-surface px-2 font-mono text-sm text-ink outline-none focus:border-line-accent"
                />
              </label>

              <label className="mt-2 flex flex-col gap-1">
                <span className="text-xs text-ink-muted">{t('tools.mcp.argsLabel')}</span>
                <input
                  value={argsText}
                  onChange={(event) => setArgsText(event.target.value)}
                  placeholder="--port 9000"
                  className="h-8 rounded-sm border border-line bg-surface px-2 font-mono text-sm text-ink outline-none focus:border-line-accent"
                />
              </label>
            </>
          )}

          {/* ★ v1.62：http 连接的请求头（如鉴权 token）。不回显，留空 = 保留原值。 */}
          {draft.type === 'http' && (
            <label className="mt-2 flex flex-col gap-1">
              <span className="text-xs text-ink-muted">
                {t('tools.mcp.headersLabel')}
                {editingName && !headersText && (
                  <span className="ml-1 text-ink-faint">{t('tools.mcp.keepNote')}</span>
                )}
              </span>
              <textarea
                value={headersText}
                onChange={(event) => setHeadersText(event.target.value)}
                rows={2}
                placeholder="Authorization: Bearer xxx"
                className="rounded-sm border border-line bg-surface px-2 py-1.5 font-mono text-sm text-ink outline-none focus:border-line-accent"
              />
            </label>
          )}

          <label className="mt-2 flex flex-col gap-1">
            <span className="text-xs text-ink-muted">
              {t('tools.mcp.envLabel')}
              {editingName && !envText && (
                <span className="ml-1 text-ink-faint">{t('tools.mcp.keepNote')}</span>
              )}
            </span>
            <textarea
              value={envText}
              onChange={(event) => setEnvText(event.target.value)}
              rows={2}
              placeholder="API_BASE=https://example.com"
              className="rounded-sm border border-line bg-surface px-2 py-1.5 font-mono text-sm text-ink outline-none focus:border-line-accent"
            />
          </label>

          <label className="mt-2 flex flex-col gap-1">
            <span className="text-xs text-ink-muted">
              {t('tools.mcp.allowLabel')}
            </span>
            <input
              value={toolsText}
              onChange={(event) => setToolsText(event.target.value)}
              placeholder="recon_scan, run_poc"
              className="h-8 rounded-sm border border-line bg-surface px-2 font-mono text-sm text-ink outline-none focus:border-line-accent"
            />
          </label>

          {/*
            ★ v0.83：单次调用超时。
              它必须有输入框，因为慢工具（网段扫描 ~85 秒、批量模板处理
              100 秒+）在默认 60 秒下**必然失败**，而报错长得像"连接坏了"。
          */}
          <label className="mt-2 flex flex-col gap-1">
            <span className="text-xs text-ink-muted">
              {t('tools.mcp.timeoutLabel')}
              <span className="ml-1 text-ink-faint">{t('tools.mcp.timeoutHint')}</span>
            </span>
            <input
              value={timeoutText}
              onChange={(event) => setTimeoutText(event.target.value)}
              inputMode="numeric"
              placeholder="300"
              className="h-8 w-40 rounded-sm border border-line bg-surface px-2 font-mono text-sm text-ink outline-none focus:border-line-accent"
            />
          </label>

          {formError && (
            <p className="mt-2 flex items-start gap-1 text-xs text-signal-risk">
              <AlertCircle className="mt-0.5 size-3 shrink-0" strokeWidth={1.75} />
              {formError}
            </p>
          )}

          {/* 试连结果 —— 成功/失败都常驻，不自动消失 */}
          {testResult && (
            <div
              className={cn(
                'mt-2 rounded-sm border px-2 py-1.5 text-xs leading-relaxed',
                testResult.ok
                  ? 'border-line text-ink-muted'
                  : 'border-signal-risk/40 text-signal-risk',
              )}
            >
              {testResult.ok ? (
                <>
                  <p className="flex items-center gap-1 text-ink">
                    <Check className="size-3" strokeWidth={2} />
                    {t('tools.mcp.connected', { ms: testResult.elapsedMs })}
                    {testResult.serverName && ` · ${testResult.serverName}`}
                    {testResult.serverVersion && ` ${testResult.serverVersion}`}
                  </p>
                  <p className="mt-0.5 font-mono text-ink-faint">
                    {testResult.tools.length > 0
                      ? testResult.tools.join(', ')
                      : t('tools.mcp.noTools')}
                  </p>
                </>
              ) : (
                <p>{testResult.reason}</p>
              )}
            </div>
          )}

          <div className="mt-2.5 flex items-center gap-2">
            <Button size="sm" onClick={handleCommit} disabled={saving}>
              {saving ? <Loader2 className="size-3.5 animate-spin" /> : null}
              {editingName ? t('tools.mcp.saveEdit') : t('tools.mcp.addShort')}
            </Button>
            <Button size="sm" variant="secondary" onClick={handleTest} disabled={testing || saving}>
              {testing ? <Loader2 className="size-3.5 animate-spin" /> : null}
              {t('tools.mcp.test')}
            </Button>
            <span className="text-2xs text-ink-faint">{t('tools.mcp.testNoSave')}</span>
          </div>
        </div>
      )}
    </section>
  )
}

export default McpPanel
