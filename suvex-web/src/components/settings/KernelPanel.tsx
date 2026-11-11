import { cn } from '@/lib/cn'
import { useI18n } from '@/i18n'
import { T } from '@/i18n/T'
import { Field, Toggle } from './Field'
import { SaveBar, SettingsPanel, type SaveState } from './SaveBar'
import SandboxProfileSelect from '@/components/kernel/SandboxProfileSelect'
import {
  SUBMIT_MODE_LABEL,
  loadKernel,
  saveKernelSettings,
  useKernel,
  type KernelSettings,
  type KernelSubmitMode,
} from '@/data/kernel'
import { ShieldAlert, ShieldCheck } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useProviders } from '@/data/providers'
import { Select } from '@/components/ui/Select'

/**
 * CTF2 解题策略面板（设置页的一块）。
 *
 * ## ★ 沿用沙箱配额面板的 UI 模式
 *
 * 与 `SandboxPanel` 同一套骨架：`SettingsPanel` 外壳 + `Field` +
 * `SaveBar`（脏检测 / 保存中 / 已保存 / 失败四态）+ **逐项校验**。
 *
 * 复用这套不是为了整齐：`SaveBar` 的状态机处理了几件容易写错的事
 * （脏检测、保存中的禁用、失败后保留草稿）。另起一套必然漏掉其中一件 ——
 * 而漏掉的那件通常表现为"点保存没反应"。
 *
 * ## ★ 它自己读 store，不通过 props 拿配置
 *
 * 与 `ScanRetentionPanel` 同一个做法：CTF2 有**自己的端点**、
 * 自己的加载状态、自己的失败原因。把它塞进 `settings-store` 那个
 * 900 行的聚合里，会让"打开任何页面都要多打一个与它无关的请求"，
 * 而它的失败还要在那个大 store 的错误状态里与别的东西混在一起。
 *
 * ## 这一块最要紧的一句话
 *
 * 「自动提交」不是一个便利开关，它是**安全取向**的选择。
 * 所以它的说明里写的是**代价**（消耗不可再生的答题机会），
 * 而不是"更省事"。用户在改它的时候必须明白自己在换什么。
 */

/** ⚠️ 与 `app/kernel/service.py` 的 `MAX_WRONG_SUBMISSIONS_CEILING` 逐字一致 */
const MAX_WRONG_SUBMISSIONS_CEILING = 10
const MIN_WRONG_SUBMISSIONS = 0

/**
 * 解题并发的合法区间。
 *
 * ⚠️ 与 `app/core/vocab.py` 的 `MIN/MAX_CTF2_MAX_CONCURRENT_SOLVES`
 * 和 `schemas.KernelSettingsIn` 的 `Field(ge=1, le=16)` 是**同一对数**。
 *
 * 上限 16 而**不是**沙箱页那个 8：比赛规则写明「题量设计明显超过人工
 * 手工完成能力」，解题的并发诉求天然更高。两边的数不同是刻意的 ——
 * 它们本来就是两个独立的配额。
 */
const MIN_CONCURRENT_SOLVES = 1
const MAX_CONCURRENT_SOLVES = 16

/**
 * 认领轮询间隔的合法区间（秒，v0.24）。
 *
 * ⚠️ 与 `app/core/vocab.py` 的 `MIN/MAX_CTF2_SOLVE_POLL_SECONDS` 和
 * `schemas.KernelSettingsIn` 的 `Field(ge=0.5, le=30)` 是同一对数。
 *
 * 下界 0.5 **不是建议值而是护栏**：填 0 会让调度线程变成忙等，
 * 症状是 CPU 打满而解题**反而变慢** —— 那种"越调越慢"最难归因到
 * 一个设置项上。
 */
const MIN_SOLVE_POLL_SECONDS = 0.5
const MAX_SOLVE_POLL_SECONDS = 30

/**
 * 自动同步间隔的合法区间（秒，v0.24）。
 *
 * ⚠️ **0 是合法值**（= 关闭）。不要把它并进"非法值"里 ——
 * "我不想让它自动同步"是一个正当诉求（PAT 有配额、比赛结束了）。
 */
const MIN_AUTO_SYNC_SECONDS = 0
const MAX_AUTO_SYNC_SECONDS = 3600

/**
 * 解题回合预算的区间（v0.26）。
 *
 * ⚠️ 与 `app/core/vocab.py` 的 `MIN/MAX_CTF2_MAX_TURNS` 和
 * `schemas.KernelSettingsIn` 的 `Field(ge=5, le=500)` 是同一对数。
 *
 * 上界 500 只是"别手滑填 100000"的护栏，**不是**成本护栏 ——
 * 真正的成本是每轮一次 LLM 调用，而那该由人在现场按题权衡。
 */
const MIN_MAX_TURNS = 5
const MAX_MAX_TURNS = 500

/*
  每题墙钟预算（秒，v1.61）。与后端 `vocab.MIN/MAX_CTF2_WALL_CLOCK_SECONDS` 同值。

  ★ 留空 = 自动（按 `maxTurns × 45` 推导），所以这一项**允许为空**，
    校验只在"填了数"时生效（见 `wallClockError`）。
*/
const MIN_WALL_CLOCK_SECONDS = 120
const MAX_WALL_CLOCK_SECONDS = 24 * 3600

export function KernelPanel() {
  const { t } = useI18n()
  const { settings, status, settingsError } = useKernel()

  /*
    进页面拉一次。
    ⚠️ 只在**还没加载过**时拉（`idle`）：`loadKernel` 会打三个端点，
    而设置页与 CTF2 页共用同一个 store —— 每次切到设置页都重拉一遍
    是白烧请求，而且是肉眼可见的闪烁。
  */
  useEffect(() => {
    if (status === 'idle') void loadKernel()
  }, [status])

  const [draft, setDraft] = useState<KernelSettings | null>(settings)
  const [saveState, setSaveState] = useState<SaveState>({ kind: 'idle' })
  const [touched, setTouched] = useState(false)
  // 备用模型的下拉选项来自已配置的 provider（v0.35）。
  const providers = useProviders()

  /*
    配置是异步来的，所以第一份草稿要从"后来才到的 settings"里补。

    ★ 但**只在草稿还是空的时候补一次**。
    无条件地 `setDraft(settings)` 会在每次 store 刷新时把用户正在改的
    内容覆盖掉 —— 而那个症状是"我填了 5，回头一看又变回 3"，
    且没人会怀疑是"同步逻辑"，只会以为保存失败了。
  */
  if (draft === null && settings !== null) {
    setDraft(settings)
  }

  const dirty = useMemo(() => {
    if (!draft || !settings) return false
    return (
      draft.submitMode !== settings.submitMode ||
      draft.maxWrongSubmissions !== settings.maxWrongSubmissions ||
      draft.defaultSandboxProfile !== settings.defaultSandboxProfile ||
      draft.maxConcurrentSolves !== settings.maxConcurrentSolves ||
      draft.solvePollSeconds !== settings.solvePollSeconds ||
      draft.autoSyncSeconds !== settings.autoSyncSeconds ||
      draft.autoPriorityNew !== settings.autoPriorityNew ||
      draft.autoStartNew !== settings.autoStartNew ||
      draft.maxTurns !== settings.maxTurns ||
      draft.wallClockSeconds !== settings.wallClockSeconds ||
      draft.fallbackProviderId !== settings.fallbackProviderId ||
      draft.enableAgentsecScenarios !== settings.enableAgentsecScenarios ||
      draft.enableIrScenarios !== settings.enableIrScenarios ||
      draft.enableMultiAgent !== settings.enableMultiAgent
    )
  }, [draft, settings])

  /*
    ★ 两个字段的校验**分开**返回。
    原来只有一个 `error`，而它是"每题错误提交上限"的报错 ——
    再加一项时若沿用同一个变量，用户在并发输入框里填错值会看到
    一条挂在"错误提交上限"下面的提示，指向完全无关的地方。
  */
  const error = useMemo(() => {
    if (!draft) return null
    if (
      draft.maxWrongSubmissions < MIN_WRONG_SUBMISSIONS ||
      draft.maxWrongSubmissions > MAX_WRONG_SUBMISSIONS_CEILING
    ) {
      return t('settings.kernel.errorRange', {
        min: MIN_WRONG_SUBMISSIONS,
        max: MAX_WRONG_SUBMISSIONS_CEILING,
      })
    }
    return null
  }, [draft, t])

  const concurrencyError = useMemo(() => {
    if (!draft) return null
    if (
      draft.maxConcurrentSolves < MIN_CONCURRENT_SOLVES ||
      draft.maxConcurrentSolves > MAX_CONCURRENT_SOLVES
    ) {
      return t('settings.kernel.errorRange', {
        min: MIN_CONCURRENT_SOLVES,
        max: MAX_CONCURRENT_SOLVES,
      })
    }
    return null
  }, [draft, t])

  /*
    ★ 校验继续**逐项分开**（同 `error` / `concurrencyError` 那条理由）：
    再加两项时若共用一个变量，用户在"自动同步间隔"里填错值会看到一条
    挂在"同时解题的题目数"下面的提示，指向完全无关的地方。
  */
  const pollError = useMemo(() => {
    if (!draft) return null
    if (
      draft.solvePollSeconds < MIN_SOLVE_POLL_SECONDS ||
      draft.solvePollSeconds > MAX_SOLVE_POLL_SECONDS
    ) {
      return t('settings.kernel.errorRangeSeconds', {
        min: MIN_SOLVE_POLL_SECONDS,
        max: MAX_SOLVE_POLL_SECONDS,
      })
    }
    return null
  }, [draft, t])

  const autoSyncError = useMemo(() => {
    if (!draft) return null
    if (
      draft.autoSyncSeconds < MIN_AUTO_SYNC_SECONDS ||
      draft.autoSyncSeconds > MAX_AUTO_SYNC_SECONDS
    ) {
      return t('settings.kernel.errorRangeSecondsZero', {
        min: MIN_AUTO_SYNC_SECONDS,
        max: MAX_AUTO_SYNC_SECONDS,
      })
    }
    return null
  }, [draft, t])

  const maxTurnsError = useMemo(() => {
    if (!draft) return null
    if (draft.maxTurns < MIN_MAX_TURNS || draft.maxTurns > MAX_MAX_TURNS) {
      return t('settings.kernel.errorRangeTurns', {
        min: MIN_MAX_TURNS,
        max: MAX_MAX_TURNS,
      })
    }
    return null
  }, [draft, t])

  /*
    墙钟预算校验：**只有填了数才校验**（`null` = 自动，合法）。
    与上面几项"必填区间"不同 —— 这一项留空是**推荐**行为（自动推导），
    所以空值通过。填了数越界才报错（与后端的 `_assert_wall_clock_seconds`
    同一条纪律：越界 400，不静默夹取）。
  */
  const wallClockError = useMemo(() => {
    if (!draft) return null
    if (draft.wallClockSeconds === null) return null
    if (
      draft.wallClockSeconds < MIN_WALL_CLOCK_SECONDS ||
      draft.wallClockSeconds > MAX_WALL_CLOCK_SECONDS
    ) {
      return t('settings.kernel.errorWallClock', {
        min: MIN_WALL_CLOCK_SECONDS,
        max: MAX_WALL_CLOCK_SECONDS,
      })
    }
    return null
  }, [draft, t])

  const handleSave = async () => {
    setTouched(true)
    if (
      error ||
      concurrencyError ||
      pollError ||
      autoSyncError ||
      maxTurnsError ||
      wallClockError ||
      !draft
    ) {
      setSaveState({ kind: 'failed', error: t('settings.kernel.invalidSave') })
      return
    }
    if (!dirty) {
      setSaveState({ kind: 'unchanged' })
      return
    }
    setSaveState({ kind: 'saving' })
    try {
      await saveKernelSettings({
        submitMode: draft.submitMode,
        maxWrongSubmissions: draft.maxWrongSubmissions,
        defaultSandboxProfile: draft.defaultSandboxProfile,
        maxConcurrentSolves: draft.maxConcurrentSolves,
        solvePollSeconds: draft.solvePollSeconds,
        autoSyncSeconds: draft.autoSyncSeconds,
        autoPriorityNew: draft.autoPriorityNew,
        autoStartNew: draft.autoStartNew,
        maxTurns: draft.maxTurns,
        wallClockSeconds: draft.wallClockSeconds,
        fallbackProviderId: draft.fallbackProviderId,
        enableAgentsecScenarios: draft.enableAgentsecScenarios,
        enableIrScenarios: draft.enableIrScenarios,
        enableMultiAgent: draft.enableMultiAgent,
      })
      setSaveState({ kind: 'saved', at: new Date().toISOString() })
      setTouched(false)
    } catch (err) {
      setSaveState({
        kind: 'failed',
        error: err instanceof Error ? err.message : t('settings.kernel.saveFailed'),
      })
    }
  }

  const handleReset = () => {
    setDraft(settings)
    setTouched(false)
    setSaveState({ kind: 'idle' })
  }

  /*
    读不到配置：显示说明而不是一个空表单。

    ★ 空表单比"读不到"更坏 —— 用户会以为所有项都是关闭的，
      然后保存一次把它们真的写成关闭。
      （`/kernel/settings` 是 admin 级，只有 read 的会话会拿到 403。）
  */
  if (!draft) {
    return (
      <SettingsPanel
        id="ctf"
        title={t('settings.kernel.panelTitleFallback')}
        description={t('settings.kernel.descFallback')}
      >
        {/*
          ★ 加载/失败态也必须能被「内核通用能力」这个锚点找到：
          导航 id 是 `ctf` 与 `kernel-core`，而这里原来只有一个 `kernel` ——
          配置读不到时点这两个导航项会**落到空处**。
        */}
        <span id="kernel-core" aria-hidden="true" />
        <p className="text-xs text-ink-muted">
          {settingsError ? (
            <T
              k="settings.kernel.loadError"
              vars={{ message: settingsError.message }}
            />
          ) : (
            <T k="settings.kernel.loading" />
          )}
        </p>
      </SettingsPanel>
    )
  }

  return (
    <>
      <SettingsPanel
        id="ctf"
        title={t('settings.kernel.title')}
        description={t('settings.kernel.description')}
      >
        {/* ── 提交策略 ── */}
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-ink">
            <T k="settings.kernel.submitStrategy" />
          </span>

          <div
            role="radiogroup"
            aria-label={t('settings.kernel.submitStrategy')}
            className="flex flex-col gap-1.5"
          >
            {(Object.keys(SUBMIT_MODE_LABEL) as KernelSubmitMode[]).map((mode) => {
              const active = draft.submitMode === mode
              const Icon = mode === 'safe' ? ShieldCheck : ShieldAlert
              return (
                <button
                  key={mode}
                  type="button"
                  role="radio"
                  aria-checked={active}
                  onClick={() => setDraft({ ...draft, submitMode: mode })}
                  className={cn(
                    'flex items-start gap-2 rounded-sm border px-2.5 py-2 text-left',
                    'transition-ui duration-fast',
                    active
                      ? 'border-accent/60 bg-accent/10'
                      : 'border-line hover:border-line-strong',
                  )}
                >
                  <Icon
                    size={13}
                    strokeWidth={1.75}
                    className={cn(
                      'mt-0.5 shrink-0',
                      active
                        ? 'text-accent-strong'
                        : mode === 'auto'
                          ? 'text-signal-warn'
                          : 'text-ink-ghost',
                    )}
                    aria-hidden="true"
                  />
                  <span className="flex min-w-0 flex-col gap-0.5">
                    <span className={cn('text-xs', active ? 'text-ink' : 'text-ink-muted')}>
                      {t(`settings.kernel.submit.${mode}.label`)}
                    </span>
                    <span className="text-2xs text-ink-faint">
                      {t(`settings.kernel.submit.${mode}.hint`)}
                    </span>
                  </span>
                </button>
              )
            })}
          </div>

          {/*
          ★ 选到 auto 时**必须**出现这段警告。
          它不是"再确认一次"，而是把代价写成具体的话：
          自动提交消耗的是**不可再生**的答题机会。
        */}
          {draft.submitMode === 'auto' && (
            <div className="flex items-start gap-1.5 rounded-sm border border-signal-warn/40 bg-signal-warn/[0.06] p-2">
              <ShieldAlert
                size={12}
                strokeWidth={2}
                className="mt-0.5 shrink-0 text-signal-warn"
                aria-hidden="true"
              />
              <span className="text-2xs text-ink-muted">
                <T k="settings.kernel.autoWarningBefore" />
                <strong className="font-medium text-ink">
                  <T k="settings.kernel.autoWarningStrong" />
                </strong>
                <T k="settings.kernel.autoWarningAfter" />
              </span>
            </div>
          )}
        </div>

        {/* ── 每题错误提交上限 ── */}
        <Field
          label={t('settings.kernel.maxWrongLabel')}
          htmlFor="ctf2-max-wrong"
          help={t('settings.kernel.maxWrongHelp')}
          error={touched ? error : null}
        >
          <span className="flex min-w-0 max-w-[10rem] items-center gap-1">
            <input
              id="ctf2-max-wrong"
              type="number"
              value={draft.maxWrongSubmissions}
              min={MIN_WRONG_SUBMISSIONS}
              max={MAX_WRONG_SUBMISSIONS_CEILING}
              onChange={(e) => {
                const parsed = Number(e.target.value)
                // 输入框清空时 Number('') 是 0 —— 保留 0 交给校验，
                // 因为用户确实输入了一个（非法的）值。与 SandboxPanel 同一处理。
                setDraft({
                  ...draft,
                  maxWrongSubmissions: Number.isFinite(parsed) ? parsed : 0,
                })
              }}
              aria-invalid={error && touched ? true : undefined}
              className={cn(
                'h-9 w-full min-w-0 rounded-sm border bg-surface-sunken px-2.5 text-sm tabular-nums text-ink',
                'transition-ui duration-fast focus:outline-none focus:ring-2 focus:ring-accent/40',
                error && touched ? 'border-signal-risk' : 'border-line hover:border-line-strong',
              )}
            />
            <span className="shrink-0 text-2xs text-ink-faint">
              <T k="settings.kernel.unitTimes" />
            </span>
          </span>
        </Field>

        {/* ── 解题并发配额（v0.22）── */}
        <Field
          label={t('settings.kernel.concurrentLabel')}
          htmlFor="ctf2-concurrent-solves"
          /*
          ★ 这段说明必须写清"它和沙箱那页的并发是两个数"。
          在改之前两者**共用同一个值**，而那个共用本身就是一处 bug
          （两个调度器各自数自己的名额，实际跑出两倍）。
          不说明的话，用户会以为这里和那里应该填一样的数。
        */
          help={t('settings.kernel.concurrentHelp')}
          error={touched ? concurrencyError : null}
        >
          <span className="flex min-w-0 max-w-[10rem] items-center gap-1">
            <input
              id="ctf2-concurrent-solves"
              type="number"
              value={draft.maxConcurrentSolves}
              min={MIN_CONCURRENT_SOLVES}
              max={MAX_CONCURRENT_SOLVES}
              onChange={(e) => {
                const parsed = Number(e.target.value)
                setDraft({
                  ...draft,
                  maxConcurrentSolves: Number.isFinite(parsed) ? parsed : 0,
                })
              }}
              aria-invalid={concurrencyError && touched ? true : undefined}
              className={cn(
                'h-9 w-full min-w-0 rounded-sm border bg-surface-sunken px-2.5 text-sm tabular-nums text-ink',
                'transition-ui duration-fast focus:outline-none focus:ring-2 focus:ring-accent/40',
                concurrencyError && touched
                  ? 'border-signal-risk'
                  : 'border-line hover:border-line-strong',
              )}
            />
            <span className="shrink-0 text-2xs text-ink-faint">
              <T k="settings.kernel.unitChallenges" />
            </span>
          </span>
        </Field>

        {/* ── 认领轮询间隔（v0.24）── */}
        <Field
          label={t('settings.kernel.pollLabel')}
          htmlFor="ctf2-solve-poll"
          /*
          ★ 这段说明必须点破"它和沙箱那页的轮询不是同一个数"。
          在改之前两者**共用** `scan_poll_seconds`，于是"把扫描轮询调大"
          会顺带把 CTF2 的认领延迟一起调大 —— 而界面上没有任何地方提示
          这两件事连着（与 v0.22 的配额超卖是同一类病）。
        */
          help={t('settings.kernel.pollHelp')}
          error={touched ? pollError : null}
        >
          <span className="flex min-w-0 max-w-[10rem] items-center gap-1">
            <input
              id="ctf2-solve-poll"
              type="number"
              step={0.5}
              value={draft.solvePollSeconds}
              min={MIN_SOLVE_POLL_SECONDS}
              max={MAX_SOLVE_POLL_SECONDS}
              onChange={(e) => {
                const parsed = Number(e.target.value)
                setDraft({
                  ...draft,
                  solvePollSeconds: Number.isFinite(parsed) ? parsed : 0,
                })
              }}
              aria-invalid={pollError && touched ? true : undefined}
              className={cn(
                'h-9 w-full min-w-0 rounded-sm border bg-surface-sunken px-2.5 text-sm tabular-nums text-ink',
                'transition-ui duration-fast focus:outline-none focus:ring-2 focus:ring-accent/40',
                pollError && touched
                  ? 'border-signal-risk'
                  : 'border-line hover:border-line-strong',
              )}
            />
            <span className="shrink-0 text-2xs text-ink-faint">
              <T k="settings.kernel.unitSeconds" />
            </span>
          </span>
        </Field>

        {/* ── 自动同步（v0.24：抢首杀）── */}
        <Field
          label={t('settings.kernel.autoSyncLabel')}
          htmlFor="ctf2-auto-sync"
          /*
          ★ 这个值默认 30 秒而不是 1 秒，理由必须写在这里：
          请求预算和解题**共享同一个 PAT**，而"平台不会更快地出新题"。
          写清代价，用户才好自己决定要不要更激进。
        */
          help={t('settings.kernel.autoSyncHelp')}
          error={touched ? autoSyncError : null}
        >
          <span className="flex min-w-0 max-w-[10rem] items-center gap-1">
            <input
              id="ctf2-auto-sync"
              type="number"
              step={5}
              value={draft.autoSyncSeconds}
              min={MIN_AUTO_SYNC_SECONDS}
              max={MAX_AUTO_SYNC_SECONDS}
              onChange={(e) => {
                const parsed = Number(e.target.value)
                setDraft({
                  ...draft,
                  autoSyncSeconds: Number.isFinite(parsed) ? parsed : 0,
                })
              }}
              aria-invalid={autoSyncError && touched ? true : undefined}
              className={cn(
                'h-9 w-full min-w-0 rounded-sm border bg-surface-sunken px-2.5 text-sm tabular-nums text-ink',
                'transition-ui duration-fast focus:outline-none focus:ring-2 focus:ring-accent/40',
                autoSyncError && touched
                  ? 'border-signal-risk'
                  : 'border-line hover:border-line-strong',
              )}
            />
            <span className="shrink-0 text-2xs text-ink-faint">
              <T k="settings.kernel.unitSeconds" />
            </span>
          </span>
        </Field>

        {/* ── 自动作业的两个开关（v0.24）── */}
        <div className="space-y-2">
          <Toggle
            id="ctf2-auto-priority"
            label={t('settings.kernel.autoPriorityLabel')}
            hint={t('settings.kernel.autoPriorityHint')}
            checked={draft.autoPriorityNew}
            onChange={(value) => setDraft({ ...draft, autoPriorityNew: value })}
          />
          <Toggle
            id="ctf2-auto-start"
            label={t('settings.kernel.autoStartLabel')}
            hint={t('settings.kernel.autoStartHint')}
            checked={draft.autoStartNew}
            onChange={(value) => setDraft({ ...draft, autoStartNew: value })}
          />
        </div>

        {/* ── 两个场景包开关（v0.40 / v0.41）── */}
        {/*
        ★ 单独一组而不是并进上面那两个自动作业开关。
        那两个管的是"什么时候开跑"，这两个管的是"跑的时候按哪套纪律判定" ——
        混在一起会让人以为它们也是"省事"类的开关，而它们恰好相反：
        打开之后那几类题的产出是**结论**，不是 flag。

        ⚠️ 都是**全局**开关（存在 CTF2 配置里），不按题设置。理由与
        `defaultSandboxProfile` 之外的各项一致：这道题的分类决定它属于
        哪一族，所以逐题再给一个开关等于把同一件事说两遍，
        而两处取值不一致时的表现是"打开了但没生效"。

        ⚠️ 两个开关**互相独立**，不要合并：共用一个的话，一族的分类会在
        另一族开着时通过校验，而对应的提示词块却不注入 —— 分类合法、
        纪律没注入，是静默失效。
      */}
        <div className="space-y-2">
          <Toggle
            id="ctf2-agentsec"
            label={t('settings.kernel.agentsecLabel')}
            hint={t('settings.kernel.agentsecHint')}
            checked={draft.enableAgentsecScenarios}
            onChange={(value) => setDraft({ ...draft, enableAgentsecScenarios: value })}
          />
        </div>

        <SaveBar
          state={saveState}
          dirty={dirty}
          onSave={() => {
            void handleSave()
          }}
          onReset={handleReset}
          note={
            <span className="text-2xs text-ink-muted">
              <T k="settings.kernel.saveNote" />
            </span>
          }
        />
      </SettingsPanel>

      <SettingsPanel
        id="kernel-core"
        title={t('settings.kernel.coreTitle')}
        description={t('settings.kernel.coreDescription')}
      >
        {/* ── 默认沙箱镜像 ── */}
        <SandboxProfileSelect
          id="ctf2-default-profile"
          value={draft.defaultSandboxProfile}
          onChange={(next) => setDraft({ ...draft, defaultSandboxProfile: next })}
          /*
            ★ 这里的效力与「CTF」页那个选择器**不同**，必须分开说。

            每道题真正的镜像在同步时按分类写死在题目行上（`sandbox_profile`），
            解题读的是那一列；本设置只决定「CTF」页那个选择器的默认值。
            照搬"只影响这次解题"会把这句说反。
          */
          hint={t('settings.kernel.defaultProfileHint')}
        />

        {/* ── 回合预算（v0.26）── */}
        <Field
          label={t('settings.kernel.maxTurnsLabel')}
          htmlFor="ctf2-max-turns"
          /*
            ★ 这段说明给的是**怎么填**的判据，不是一句"越大越好"。
            判据来自实测数据 —— 用户看到"6 轮 vs 37 条命令"这两组数，
            自己就知道该往哪调。
          */
          help={t('settings.kernel.maxTurnsHelp')}
          error={touched ? maxTurnsError : null}
        >
          <span className="flex min-w-0 max-w-[10rem] items-center gap-1">
            <input
              id="ctf2-max-turns"
              type="number"
              step={10}
              value={draft.maxTurns}
              min={MIN_MAX_TURNS}
              max={MAX_MAX_TURNS}
              onChange={(e) => {
                const parsed = Number(e.target.value)
                setDraft({ ...draft, maxTurns: Number.isFinite(parsed) ? parsed : 0 })
              }}
              aria-invalid={maxTurnsError && touched ? true : undefined}
              className={cn(
                'h-9 w-full min-w-0 rounded-sm border bg-surface-sunken px-2.5 text-sm tabular-nums text-ink',
                'transition-ui duration-fast focus:outline-none focus:ring-2 focus:ring-accent/40',
                maxTurnsError && touched
                  ? 'border-signal-risk'
                  : 'border-line hover:border-line-strong',
              )}
            />
            <span className="shrink-0 text-2xs text-ink-faint">
              <T k="settings.kernel.unitTurns" />
            </span>
          </span>
        </Field>

        {/* ── 墙钟预算（v1.61）── */}
        <Field
          label={t('settings.kernel.wallClockLabel')}
          htmlFor="ctf2-wall-clock"
          /*
            ★ 说明必须点破"留空 = 自动"，否则用户会以为空着是"没设=不限时"。
            它恰恰相反：空着是"按回合自动给一个合理值"，而**这**才是推荐用法。
          */
          help={t('settings.kernel.wallClockHelp', {
            min: MIN_WALL_CLOCK_SECONDS,
            max: MAX_WALL_CLOCK_SECONDS,
          })}
          error={touched ? wallClockError : null}
        >
          <span className="flex min-w-0 max-w-[10rem] items-center gap-1">
            <input
              id="ctf2-wall-clock"
              type="number"
              step={60}
              // 空串 ⇄ null：留空表示"自动推导"，不是 0。
              value={draft.wallClockSeconds === null ? '' : draft.wallClockSeconds}
              min={MIN_WALL_CLOCK_SECONDS}
              max={MAX_WALL_CLOCK_SECONDS}
              placeholder={t('settings.kernel.wallClockPlaceholder', {
                seconds: draft.maxTurns * 45,
              })}
              onChange={(e) => {
                const raw = e.target.value
                if (raw === '') {
                  setDraft({ ...draft, wallClockSeconds: null })
                  return
                }
                const parsed = Number(raw)
                setDraft({
                  ...draft,
                  wallClockSeconds: Number.isFinite(parsed) ? parsed : null,
                })
              }}
              aria-invalid={wallClockError && touched ? true : undefined}
              className={cn(
                'h-9 w-full min-w-0 rounded-sm border bg-surface-sunken px-2.5 text-sm tabular-nums text-ink',
                'transition-ui duration-fast placeholder:text-ink-faint focus:outline-none focus:ring-2 focus:ring-accent/40',
                wallClockError && touched
                  ? 'border-signal-risk'
                  : 'border-line hover:border-line-strong',
              )}
            />
            <span className="shrink-0 text-2xs text-ink-faint">
              <T k="settings.kernel.unitSeconds" />
            </span>
          </span>
        </Field>

        <Field
          label={t('settings.kernel.fallbackLabel')}
          htmlFor="ctf2-fallback-provider"
          /*
            ★ 这一段必须把"什么时候会换"讲清楚，否则用户会以为它是负载均衡。
            它不是 —— 主接入点**调不动**时才换，正常情况下永远用主模型。
          */
          help={t('settings.kernel.fallbackHelp')}
        >
          <Select
            id="ctf2-fallback-provider"
            value={draft.fallbackProviderId ?? ''}
            options={[
              { value: '', label: t('settings.kernel.fallbackDisabled') },
              ...providers.map((provider) => ({ value: provider.id, label: provider.label })),
            ]}
            onChange={(value) =>
              setDraft({ ...draft, fallbackProviderId: value || null })
            }
            className="max-w-[20rem]"
          />
        </Field>

        {/* ── 跨场景开关（v0.41 / v0.58）── */}
        {/*
          ★ 这两个开关与 CTF 解题无关：应急响应场景包面向 ir-* 分类，
          多智能体编排改变的是**执行方式**，对任何题型都成立。
          所以它们属于「内核通用能力」而不是「CTF 解题」。
        */}
        <div className="space-y-2">
          <Toggle
            id="ctf2-ir"
            label={t('settings.kernel.irLabel')}
            hint={t('settings.kernel.irHint')}
            checked={draft.enableIrScenarios}
            onChange={(value) => setDraft({ ...draft, enableIrScenarios: value })}
          />
          <Toggle
            id="ctf2-multi-agent"
            label={t('settings.kernel.multiAgentLabel')}
            hint={t('settings.kernel.multiAgentHint')}
            checked={draft.enableMultiAgent}
            onChange={(value) => setDraft({ ...draft, enableMultiAgent: value })}
          />
        </div>

        {/*
          ★ 这一块也有自己的保存条 —— 遵守「每个面板自带保存条」的约定。
          两块共用同一个 draft 与同一套 handleSave/handleReset，
          所以在这里点保存与在上面点保存**效果完全相同**
          （保存的是整份内核配置，不分块提交），这样用户在哪一块改完
          都能就地保存，不用滚回上一块。
        */}
        <SaveBar
          state={saveState}
          dirty={dirty}
          onSave={() => {
            void handleSave()
          }}
          onReset={handleReset}
          note={
            <span className="text-2xs text-ink-muted">
              <T k="settings.kernel.coreSaveNote" />
            </span>
          }
        />
      </SettingsPanel>
    </>
  )
}

export default KernelPanel
