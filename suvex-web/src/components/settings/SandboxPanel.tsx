import { cn } from '@/lib/cn'
import { Field, Toggle } from './Field'
import { SaveBar, SettingsPanel, type SaveState } from './SaveBar'
import {
  SCAN_MODE_HINT,
  SCAN_MODE_LABEL,
  type ExecutionSource,
  type SandboxConfig,
  type SandboxStatus,
  type ScanMode,
} from '@/data/settings'
import { saveSandbox } from '@/data/settings-store'
import { Boxes, Cpu, Gauge, HardDrive, ShieldOff, Timer, TriangleAlert } from 'lucide-react'
import { useMemo, useState } from 'react'

/**
 * 沙箱与执行环境面板。
 *
 * ★ 这一页要回答的核心问题不是"配置项有哪些"，而是
 *   **"扫描跑起来的时候，它到底能动什么"**。
 *   用户配 API key 时考虑的是成本，配沙箱时考虑的是风险 ——
 *   所以每一项都要写清"打开/关掉会发生什么"，而不是只给一个输入框。
 *
 * ## 任务 13 之后这一页不再有"未交付"的占位
 *
 * 状态（运行中/已停止/异常）现在来自 `GET /settings/sandbox`，
 * 即数据库里那一行 —— 而它由 `health.py` 按 Docker 的真实情况维护。
 * 保存走 `PUT /settings/sandbox`，**不需要重启服务**。
 *
 * ⚠️ 沙箱容器配额里**只有三项能真下发**（任务 14 已接上，走 strix 的环境变量）：
 *   CPU、内存、禁止出网。另外两项 —— **单次超时**与**允许写入挂载卷** ——
 *   执行后端没有对应开关（strix 全包没有扫描级墙钟超时；工作区挂载恒为读写），
 *   它们会被保存但不会改变行为。界面必须把这五个字段**分开标注**，
 *   否则用户会以为"我把内存调到 512MB 就限制了扫描"而实际什么也没发生。
 */

const STATUS_META: Record<
  SandboxStatus,
  { label: string; tone: 'ok' | 'idle' | 'risk'; note: string }
> = {
  running: { label: '运行中', tone: 'ok', note: '沙箱容器已就绪，可以接受扫描任务。' },
  stopped: { label: '已停止', tone: 'idle', note: '沙箱未运行，发起的扫描会排队等待。' },
  error: { label: '异常', tone: 'risk', note: '沙箱启动失败，请检查 Docker 服务与镜像。' },
}

export interface SandboxPanelProps {
  config: SandboxConfig
}

export function SandboxPanel({ config }: SandboxPanelProps) {
  const [draft, setDraft] = useState<SandboxConfig>(config)
  const [saveState, setSaveState] = useState<SaveState>({ kind: 'idle' })
  const [touched, setTouched] = useState(false)

  const dirty = useMemo(
    () =>
      draft.cpuLimit !== config.cpuLimit ||
      draft.memoryLimitMb !== config.memoryLimitMb ||
      draft.timeoutMinutes !== config.timeoutMinutes ||
      draft.networkIsolated !== config.networkIsolated ||
      draft.allowVolumeWrite !== config.allowVolumeWrite ||
      draft.maxConcurrentScans !== config.maxConcurrentScans ||
      draft.scanMaxTurns !== config.scanMaxTurns ||
      draft.scanMode !== config.scanMode,
    [draft, config],
  )

  const errors = useMemo(() => {
    return {
      cpu: draft.cpuLimit < 1 ? 'CPU 至少 1 核' : null,
      memory: draft.memoryLimitMb < 512 ? '内存至少 512 MB' : null,
      timeout:
        draft.timeoutMinutes < 1
          ? '超时至少 1 分钟'
          : draft.timeoutMinutes > 24 * 60
            ? '超时不应超过 24 小时'
            : null,
      /*
        ⚠️ 下面两条区间与后端 `settings/service.py` 的常量**必须一致**。

        两处都写是刻意的：客户端这份让用户不必往返就能看到错误，
        服务端那份才是真正生效的校验（客户端校验不是安全边界）。
        但区间一旦不一致，用户就会看到"前端说行、后端说不行" ——
        所以改任何一边都必须同步另一边。
      */
      concurrent:
        draft.maxConcurrentScans < 1 || draft.maxConcurrentScans > 8
          ? '并发应在 1-8 之间'
          : null,
      turns:
        draft.scanMaxTurns < 10 || draft.scanMaxTurns > 2000
          ? '回合上限应在 10-2000 之间'
          : null,
    }
  }, [draft])

  const hasError = Object.values(errors).some((e) => e !== null)

  const handleSave = async () => {
    setTouched(true)
    if (hasError) {
      setSaveState({ kind: 'failed', error: '有配置项超出允许范围，请先修正' })
      return
    }
    if (!dirty) {
      setSaveState({ kind: 'unchanged' })
      return
    }

    setSaveState({ kind: 'saving' })
    // ★ 现在这是**真的网络请求**，必须 await。
    //   旧版这里是同步的内存赋值，`saving` 状态一闪而过；真实请求有可见的
    //   等待，而"点完没反应"正是 SaveBar 要消灭的那种体验。
    const result = await saveSandbox(draft)
    if (result.status === 'ok') {
      setSaveState({ kind: 'saved', at: result.at })
      setTouched(false)
    } else if (result.status === 'failed') {
      setSaveState({ kind: 'failed', error: result.error })
    } else {
      setSaveState({ kind: 'unchanged' })
    }
  }

  const handleReset = () => {
    setDraft(config)
    setTouched(false)
    setSaveState({ kind: 'idle' })
  }

  /*
    ★ 状态现在来自后端（`GET /settings/sandbox`），可以照实渲染了 ——
    不再是内存里那份写着 `running` 的假初值。

    ⚠️ 但仍要克制：`health.py` 当前的实现是"如实返回配置里**记录**的状态"，
    并不去问 Docker 守护进程（本后端还不管理容器）。所以这里能说的是
    "配置认为它在跑"，而不是"我确认过容器在跑"。后者是 `health.py` 的待办。
  */
  const statusMeta = STATUS_META[config.status]

  return (
    <SettingsPanel
      id="sandbox"
      title="沙箱与执行环境"
      description="扫描时的代码执行、PoC 验证都在隔离容器内完成，不会直接作用于你的机器。"
    >
      {/* ── 运行状态（只读） ── */}
      <div className="flex flex-wrap items-center gap-x-tight gap-y-2 rounded-sm border border-line bg-surface-sunken p-3">
        <span
          className={cn(
            'flex h-7 w-7 shrink-0 items-center justify-center rounded-pill',
            statusMeta.tone === 'ok' && 'bg-signal-ok/15 text-signal-ok',
            statusMeta.tone === 'idle' && 'bg-ink-ghost/20 text-ink-faint',
            statusMeta.tone === 'risk' && 'bg-signal-risk/15 text-signal-risk',
          )}
        >
          <Boxes size={14} strokeWidth={1.75} aria-hidden="true" />
        </span>

        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="flex flex-wrap items-center gap-1.5">
            <span className="text-sm text-ink">Docker 沙箱 · {statusMeta.label}</span>
            <span className="mono-ui text-ink-faint">{config.image}</span>
          </span>
          <span className="text-2xs text-ink-faint">{statusMeta.note}</span>
        </div>
      </div>

      {/* ── 资源限制 ── */}
      <div className="grid gap-panel sm:grid-cols-3">
        <Field
          label="CPU 上限"
          htmlFor="sandbox-cpu"
          help="核数。超出的部分不会分配，扫描会变慢而不是失败。"
          error={touched ? errors.cpu : null}
        >
          <div className="flex items-center gap-1.5">
            <Cpu
              size={13}
              strokeWidth={1.75}
              className="shrink-0 text-ink-ghost"
              aria-hidden="true"
            />
            <NumberInput
              id="sandbox-cpu"
              value={draft.cpuLimit}
              min={1}
              max={64}
              onChange={(v) => setDraft({ ...draft, cpuLimit: v })}
              suffix="核"
              error={touched ? errors.cpu : null}
            />
          </div>
        </Field>

        <Field
          label="内存上限"
          htmlFor="sandbox-memory"
          help="超出会被容器杀掉，扫描标记为失败。"
          error={touched ? errors.memory : null}
        >
          <div className="flex items-center gap-1.5">
            <HardDrive
              size={13}
              strokeWidth={1.75}
              className="shrink-0 text-ink-ghost"
              aria-hidden="true"
            />
            <NumberInput
              id="sandbox-memory"
              value={draft.memoryLimitMb}
              min={512}
              max={65536}
              step={512}
              onChange={(v) => setDraft({ ...draft, memoryLimitMb: v })}
              suffix="MB"
              error={touched ? errors.memory : null}
            />
          </div>
        </Field>

        <Field
          label="单次超时"
          htmlFor="sandbox-timeout"
          help="防止一次扫描无限跑下去。"
          error={touched ? errors.timeout : null}
        >
          <div className="flex items-center gap-1.5">
            <Timer
              size={13}
              strokeWidth={1.75}
              className="shrink-0 text-ink-ghost"
              aria-hidden="true"
            />
            <NumberInput
              id="sandbox-timeout"
              value={draft.timeoutMinutes}
              min={1}
              max={1440}
              onChange={(v) => setDraft({ ...draft, timeoutMinutes: v })}
              suffix="分钟"
              error={touched ? errors.timeout : null}
            />
          </div>
        </Field>
      </div>

      {/* ── 隔离策略 ── */}
      <div className="flex flex-col gap-1.5">
        <span className="text-xs font-medium text-ink">隔离策略</span>

        <Toggle
          id="sandbox-network"
          label="禁止沙箱访问外网"
          hint="限制影响范围，但针对外部目标的扫描会直接失败 —— 按需开启。"
          checked={draft.networkIsolated}
          onChange={(v) => setDraft({ ...draft, networkIsolated: v })}
        />

        {/*
          这一条解释必须出现：用户很容易把"禁止外网"理解成"更安全所以应该开"，
          然后困惑于扫描为什么跑不动。取舍写在开关下面，比放在文档里有用。
        */}
        {draft.networkIsolated && (
          <div className="flex items-start gap-1.5 rounded-sm border border-signal-warn/40 bg-signal-warn/[0.06] p-2">
            <TriangleAlert
              size={12}
              strokeWidth={2}
              className="mt-0.5 shrink-0 text-signal-warn"
              aria-hidden="true"
            />
            <span className="text-2xs text-ink-muted">
              已禁止出网。针对 <code className="mono-ui">https://</code> 目标的动态扫描将无法执行，
              只能分析本地代码。需要打外部目标时请关闭它。
            </span>
          </div>
        )}

        <Toggle
          id="sandbox-write"
          label="允许写入挂载卷"
          hint="关闭时所有写入都在容器内，容器销毁即消失。开启会让 PoC 能改动宿主目录。"
          checked={draft.allowVolumeWrite}
          onChange={(v) => setDraft({ ...draft, allowVolumeWrite: v })}
        />

        {draft.allowVolumeWrite && (
          <div className="flex items-start gap-1.5 rounded-sm border border-signal-risk/40 bg-signal-risk/[0.06] p-2">
            <ShieldOff
              size={12}
              strokeWidth={2}
              className="mt-0.5 shrink-0 text-signal-risk"
              aria-hidden="true"
            />
            <span className="text-2xs text-ink-muted">
              写入已放行。验证脚本可能修改宿主上的文件，仅在你确信目标代码可丢弃时开启。
            </span>
          </div>
        )}
      </div>

      {/* ── 执行（任务 13）────────────────────────────── */}
      {/*
        ★ 这一块与上面的「容器配额」要分开读，因为**生效程度不同**：
        配额那几项目前会被保存但不会被执行后端读取；这三项是真生效的。
        混在一起写会让用户以为"我把内存调到 512MB 就限制了扫描"。
      */}
      <div className="flex flex-col gap-panel">
        <div className="flex flex-col gap-0.5">
          <span className="text-xs font-medium text-ink">执行</span>
          <span className="text-2xs text-ink-faint">
            与容器配额不同，这三项是<strong className="font-medium text-ink-muted">真的生效</strong>的：
            并发几秒内生效，回合上限与扫描深度对之后新发起的扫描生效。都不需要重启服务。
          </span>
        </div>

        <div className="grid gap-panel sm:grid-cols-2">
          <Field
            label="同时运行的扫描数"
            htmlFor="sandbox-concurrent"
            help="超出的扫描会排队等待，不会被拒绝。上限受本机资源与模型服务并发额度限制。"
            error={touched ? errors.concurrent : null}
          >
            <div className="flex items-center gap-1.5">
              <Gauge
                size={13}
                strokeWidth={1.75}
                className="shrink-0 text-ink-ghost"
                aria-hidden="true"
              />
              <NumberInput
                id="sandbox-concurrent"
                value={draft.maxConcurrentScans}
                min={1}
                max={8}
                onChange={(v) => setDraft({ ...draft, maxConcurrentScans: v })}
                suffix="个"
                error={touched ? errors.concurrent : null}
              />
            </div>
          </Field>

          <Field
            label="单个 agent 回合上限"
            htmlFor="sandbox-turns"
            help="strix 的 --max-turns。调小能明显缩短单次扫描，代价是复杂题目可能没跑完就被截断。"
            error={touched ? errors.turns : null}
          >
            <NumberInput
              id="sandbox-turns"
              value={draft.scanMaxTurns}
              min={10}
              max={2000}
              step={10}
              onChange={(v) => setDraft({ ...draft, scanMaxTurns: v })}
              suffix="轮"
              error={touched ? errors.turns : null}
            />
          </Field>
        </div>

        <Field label="扫描深度" htmlFor="sandbox-mode" help={SCAN_MODE_HINT[draft.scanMode]}>
          <div id="sandbox-mode" className="flex flex-wrap gap-1.5">
            {(Object.keys(SCAN_MODE_LABEL) as ScanMode[]).map((mode) => (
              <button
                key={mode}
                type="button"
                onClick={() => setDraft({ ...draft, scanMode: mode })}
                aria-pressed={draft.scanMode === mode}
                className={cn(
                  'h-8 rounded-sm border px-3 text-xs transition-ui duration-fast',
                  draft.scanMode === mode
                    ? 'border-accent/60 bg-accent/10 text-accent-strong'
                    : 'border-line text-ink-muted hover:border-line-strong',
                )}
              >
                {SCAN_MODE_LABEL[mode]}
              </button>
            ))}
          </div>
        </Field>

        <ExecutionSourceNote source={config.executionSource} />
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
            CPU、内存、禁止出网这三项会真正下发给沙箱容器；而
            <span className="text-ink-muted">单次超时</span>与
            <span className="text-ink-muted">允许写入挂载卷</span>
            目前无法下发 —— 执行后端没有对应开关，它们会被保存但不会改变行为。
          </span>
        }
      />
    </SettingsPanel>
  )
}

/**
 * 执行类三项当前值的来源。
 *
 * ★ 只在**至少一项来自环境默认**时显示。全都由用户设过就没必要啰嗦 ——
 *   那种提示会变成页面上每看一次都要忽略的噪声。
 *
 * 为什么必须说：部署时若设了 `SUPEX_MAX_CONCURRENT_SCANS=3`，用户在这里
 * 看到 3 会以为是自己以前设的，于是既不知道改回什么，也不知道它其实是
 * 环境默认值。标出来这件事才有确定答案。
 */
function ExecutionSourceNote({ source }: { source: ExecutionSource }) {
  const keys = Object.keys(source) as (keyof ExecutionSource)[]
  const fromDefault = keys.filter((key) => source[key] === 'default')
  if (fromDefault.length === 0) return null

  const labels: Record<keyof ExecutionSource, string> = {
    maxConcurrentScans: '并发',
    scanMaxTurns: '回合上限',
    scanMode: '扫描深度',
  }

  return (
    <span className="text-2xs text-ink-faint">
      其中 {fromDefault.map((key) => labels[key]).join('、')} 取自服务端环境变量（部署基线），
      不是你在此处设过的值。保存一次后即以这里为准。
    </span>
  )
}

/** 数字输入。统一的窄宽度 + 后缀单位，避免三个框宽窄不一 */
function NumberInput({
  id,
  value,
  onChange,
  min,
  max,
  step = 1,
  suffix,
  error,
}: {
  id: string
  value: number
  onChange: (value: number) => void
  min: number
  max: number
  step?: number
  suffix: string
  error?: string | null
}) {
  return (
    <span className="flex min-w-0 flex-1 items-center gap-1">
      <input
        id={id}
        type="number"
        value={value}
        min={min}
        max={max}
        step={step}
        onChange={(e) => {
          const parsed = Number(e.target.value)
          // 输入框被清空时 Number('') 是 0，会让校验立刻飘红。
          // 保留 0 并交给校验处理是更诚实的行为（用户确实输入了非法值）。
          onChange(Number.isFinite(parsed) ? parsed : 0)
        }}
        aria-invalid={error ? true : undefined}
        className={cn(
          'h-9 w-full min-w-0 rounded-sm border bg-surface-sunken px-2.5 text-sm tabular-nums text-ink',
          'transition-ui duration-fast focus:outline-none focus:ring-2 focus:ring-accent/40',
          error ? 'border-signal-risk' : 'border-line hover:border-line-strong',
        )}
      />
      <span className="shrink-0 text-2xs text-ink-faint">{suffix}</span>
    </span>
  )
}

export default SandboxPanel
