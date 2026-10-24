import { cn } from '@/lib/cn'
import { Button } from '@/components/ui/Button'
import {
  parseAllowedTools,
  parseArgs,
  parseEnv,
  parseTimeout,
  validateDraft,
  type McpServer,
  type McpServerDraft,
  type McpTestResult,
} from '@/data/tools'
import { AlertCircle, Check, Loader2, Plus, X } from 'lucide-react'
import { useState } from 'react'

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
    command: '',
    args: [],
    env: {},
    notes: '',
    allowedTools: null,
    preserveEnv: false,
    // ★ v0.83：新增时不给超时（`null` = 用内核默认 60 秒）。
    callTimeout: null,
  }
}

/** 已保存的连接 → 编辑用草稿。`env` 恒为空、`preserveEnv` 恒为真。 */
function draftOf(server: McpServer): McpServerDraft {
  return {
    name: server.name,
    command: server.command,
    args: [...server.args],
    env: {},
    notes: server.notes,
    allowedTools: server.allowedTools ? [...server.allowedTools] : null,
    // ★ 编辑已有连接：默认不动 env（见文件头那段）。
    preserveEnv: true,
    // ★ 编辑时把已存的超时带回来（它是可回显字段，不像 env）。
    callTimeout: server.callTimeout,
  }
}

export function McpPanel({
  servers,
  saving,
  error,
  onSave,
  onTest,
}: McpPanelProps) {
  /** 正在编辑的草稿；`null` = 没在编辑。 */
  const [draft, setDraft] = useState<McpServerDraft | null>(null)
  /** 编辑的是哪一条（`null` = 新增）。用名字标识，因为名字是唯一键。 */
  const [editingName, setEditingName] = useState<string | null>(null)
  const [argsText, setArgsText] = useState('')
  const [envText, setEnvText] = useState('')
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
  const [testResult, setTestResult] = useState<McpTestResult | null>(null)

  function startAdd() {
    const next = emptyDraft()
    setDraft(next)
    setEditingName(null)
    setArgsText('')
    setEnvText('')
    setToolsText('')
    setTimeoutText('')
    setFormError(null)
    setTestResult(null)
  }

  function startEdit(server: McpServer) {
    setDraft(draftOf(server))
    setEditingName(server.name)
    setArgsText(server.args.join(' '))
    // env 不回显 —— 留空表示"不改动"（见文件头）
    setEnvText('')
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
    return {
      ...draft,
      args: parseArgs(argsText),
      env,
      allowedTools: parseAllowedTools(toolsText),
      // ★ 用户在 env 框里**真的写了东西** → 提交它并取消"保留"；
      //   编辑时留空 → 保留原值（不传空 env 把凭据清掉）。
      preserveEnv: editingName !== null && Object.keys(env).length === 0,
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
        reason: error instanceof Error ? error.message : '试连请求失败。',
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
    onSave([...others, { ...next, name: next.name.trim(), command: next.command.trim() }])
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
          <h2 className="text-sm font-medium text-ink">MCP 连接</h2>
          <p className="mt-1 max-w-[60ch] text-[13px] leading-relaxed text-ink-muted">
            外部工具服务。配一条之后，智能体就能通过{' '}
            <code className="font-mono">list_mcps</code> /{' '}
            <code className="font-mono">call_mcp</code> 调到它提供的工具。
          </p>
        </div>
        <Button
          size="sm"
          variant="secondary"
          onClick={startAdd}
          disabled={saving || draft !== null}
        >
          <Plus className="size-3.5" strokeWidth={1.75} />
          添加连接
        </Button>
      </header>

      {/* 已保存的连接 */}
      {servers.length === 0 ? (
        <div className="px-4 py-6 text-sm text-ink-muted">
          <p>还没有配置任何 MCP 连接。</p>
          <p className="mt-1 text-[12px] text-ink-faint">
            不配也能正常跑 —— 沙箱里的 nmap / ffuf / curl 一直都在。
            配了才会多出「外部工具」那一类能力。
          </p>
        </div>
      ) : (
        <ul className="divide-y divide-line/60">
          {servers.map((server) => (
            <li key={server.name} className="px-4 py-3">
              <div className="flex flex-wrap items-baseline gap-x-tight gap-y-1">
                <code className="font-mono text-[13px] text-ink">{server.name}</code>
                {server.hasEnv && (
                  <span className="rounded-sm border border-line px-1 py-px text-[11px] text-ink-faint">
                    带环境变量
                  </span>
                )}
                {server.allowedTools && (
                  <span className="text-[11px] text-ink-faint">
                    白名单 {server.allowedTools.length} 项
                  </span>
                )}
                {/*
                  ★ v0.83：把生效的超时显示在列表里。
                    不显示的话，"这条连接到底是不是 60 秒"只能点进编辑才知道，
                    而超时配错的症状是"调用偶尔失败"，没人会想到去核对它。
                    没配时显示内核默认值，让人知道它确实是 60 而不是"没限制"。
                */}
                <span className="text-[11px] text-ink-faint">
                  超时 {server.callTimeout == null ? '默认 60' : server.callTimeout} 秒
                </span>
              </div>
              <p className="mt-1 break-all font-mono text-[12px] text-ink-muted">
                {server.command} {server.args.join(' ')}
              </p>
              {server.notes && <p className="mt-1 text-[12px] text-ink-faint">{server.notes}</p>}
              <div className="mt-2 flex gap-tight">
                <button
                  type="button"
                  onClick={() => startEdit(server)}
                  disabled={saving}
                  className="text-[12px] text-ink-muted transition-ui duration-fast hover:text-ink disabled:opacity-50"
                >
                  编辑
                </button>
                <button
                  type="button"
                  onClick={() => handleRemove(server.name)}
                  disabled={saving}
                  className="text-[12px] text-ink-muted transition-ui duration-fast hover:text-signal-risk disabled:opacity-50"
                >
                  删除
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {/* 保存错误 */}
      {error && (
        <div className="flex items-start gap-tight border-t border-line px-4 py-2.5 text-[13px] text-signal-risk">
          <AlertCircle className="mt-0.5 size-3.5 shrink-0" strokeWidth={1.75} />
          <span className="leading-relaxed">{error}</span>
        </div>
      )}

      {/* 编辑表单 */}
      {draft && (
        <div className="border-t border-line bg-surface-raised/40 px-4 py-3">
          <div className="flex items-center justify-between">
            <h3 className="text-[13px] font-medium text-ink">
              {editingName ? `编辑 ${editingName}` : '添加连接'}
            </h3>
            <button
              type="button"
              onClick={cancel}
              className="text-ink-faint transition-ui duration-fast hover:text-ink"
              aria-label="取消"
            >
              <X className="size-4" strokeWidth={1.75} />
            </button>
          </div>

          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            <label className="flex flex-col gap-1">
              <span className="text-[12px] text-ink-muted">连接名（模型用它指认）</span>
              <input
                value={draft.name}
                onChange={(event) => setDraft({ ...draft, name: event.target.value })}
                placeholder="demo"
                className="h-8 rounded-sm border border-line bg-surface px-2 font-mono text-[13px] text-ink outline-none focus:border-line-accent"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[12px] text-ink-muted">命令（可执行文件）</span>
              <input
                value={draft.command}
                onChange={(event) => setDraft({ ...draft, command: event.target.value })}
                placeholder="/opt/suvex/demo_mcp.py"
                className="h-8 rounded-sm border border-line bg-surface px-2 font-mono text-[13px] text-ink outline-none focus:border-line-accent"
              />
            </label>
          </div>

          <label className="mt-2 flex flex-col gap-1">
            <span className="text-[12px] text-ink-muted">参数（空格分隔）</span>
            <input
              value={argsText}
              onChange={(event) => setArgsText(event.target.value)}
              placeholder="--port 9000"
              className="h-8 rounded-sm border border-line bg-surface px-2 font-mono text-[13px] text-ink outline-none focus:border-line-accent"
            />
          </label>

          <label className="mt-2 flex flex-col gap-1">
            <span className="text-[12px] text-ink-muted">
              环境变量（每行 KEY=VALUE）
              {editingName && !envText && (
                <span className="ml-1 text-ink-faint">
                  · 留空 = 保留原值（已保存的不回显，所以这里看不到它）
                </span>
              )}
            </span>
            <textarea
              value={envText}
              onChange={(event) => setEnvText(event.target.value)}
              rows={2}
              placeholder="API_BASE=https://example.com"
              className="rounded-sm border border-line bg-surface px-2 py-1.5 font-mono text-[13px] text-ink outline-none focus:border-line-accent"
            />
          </label>

          <label className="mt-2 flex flex-col gap-1">
            <span className="text-[12px] text-ink-muted">
              白名单（逗号分隔，留空 = 允许这条连接的全部工具）
            </span>
            <input
              value={toolsText}
              onChange={(event) => setToolsText(event.target.value)}
              placeholder="recon_scan, run_poc"
              className="h-8 rounded-sm border border-line bg-surface px-2 font-mono text-[13px] text-ink outline-none focus:border-line-accent"
            />
          </label>

          {/*
            ★ v0.83：单次调用超时。
              它必须有输入框，因为慢工具（网段扫描 ~85 秒、批量模板处理
              100 秒+）在默认 60 秒下**必然失败**，而报错长得像"连接坏了"。
          */}
          <label className="mt-2 flex flex-col gap-1">
            <span className="text-[12px] text-ink-muted">
              单次调用超时（秒）
              <span className="ml-1 text-ink-faint">
                · 留空 = 用内核默认 60 秒；慢工具（资产测绘 / 跑 POC）建议 300
              </span>
            </span>
            <input
              value={timeoutText}
              onChange={(event) => setTimeoutText(event.target.value)}
              inputMode="numeric"
              placeholder="300"
              className="h-8 w-40 rounded-sm border border-line bg-surface px-2 font-mono text-[13px] text-ink outline-none focus:border-line-accent"
            />
          </label>

          {formError && (
            <p className="mt-2 flex items-start gap-1 text-[12px] text-signal-risk">
              <AlertCircle className="mt-0.5 size-3 shrink-0" strokeWidth={1.75} />
              {formError}
            </p>
          )}

          {/* 试连结果 —— 成功/失败都常驻，不自动消失 */}
          {testResult && (
            <div
              className={cn(
                'mt-2 rounded-sm border px-2 py-1.5 text-[12px] leading-relaxed',
                testResult.ok
                  ? 'border-line text-ink-muted'
                  : 'border-signal-risk/40 text-signal-risk',
              )}
            >
              {testResult.ok ? (
                <>
                  <p className="flex items-center gap-1 text-ink">
                    <Check className="size-3" strokeWidth={2} />
                    连上了 · {testResult.elapsedMs} ms
                    {testResult.serverName && ` · ${testResult.serverName}`}
                    {testResult.serverVersion && ` ${testResult.serverVersion}`}
                  </p>
                  <p className="mt-0.5 font-mono text-ink-faint">
                    {testResult.tools.length > 0
                      ? testResult.tools.join(', ')
                      : '（这个 server 没有报任何工具）'}
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
              {editingName ? '保存修改' : '添加'}
            </Button>
            <Button size="sm" variant="secondary" onClick={handleTest} disabled={testing || saving}>
              {testing ? <Loader2 className="size-3.5 animate-spin" /> : null}
              试连
            </Button>
            <span className="text-[11px] text-ink-faint">试连不会保存 —— 先确认能通，再保存。</span>
          </div>
        </div>
      )}
    </section>
  )
}

export default McpPanel
