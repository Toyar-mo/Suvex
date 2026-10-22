import { cn } from '@/lib/cn'
import { Field, Toggle } from './Field'
import { SaveBar, SettingsPanel, type SaveState } from './SaveBar'
import {
  SCAN_MODE_HINT,
  SCAN_MODE_LABEL,
  type ExecutionSource,
  type SandboxConfig,
  type ScanMode,
} from '@/data/settings'
import { saveSandbox } from '@/data/settings-store'
import { Boxes, Cpu, Gauge, HardDrive, ShieldAlert, ShieldOff, Timer, TriangleAlert } from 'lucide-react'
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
 * 保存走 `PUT /settings/sandbox`，**不需要重启服务**。
 *
 * ⚠️ 自研引擎下各字段的真实生效情况（2026-10 实测，`scans/agent_runner.py`）：
 *   · CPU、内存 —— ✅ 真下发（`SolveSpec.cpus/memory_mb` → 临时容器）；
 *   · 禁止出网 —— ⚠️ 只对本地形态（repo/path/artifact）生效；url/service
 *     这类"要去连别人"的目标**强制开网**（否则工具全连不上，渗透必然失败）；
 *   · 扫描深度 —— ✅ 决定回合天花板（快速 40 / 标准 80 / 深度 150）；
 *   · 回合上限 —— ✅ 与深度天花板取较小值（不再是旧版"永远夹 80"的死值）；
 *   · 单次超时 —— ✅ 墙钟超时（v0.64）：到点引擎自己在轮询里取消循环、
 *     按"超时收尾"结束（不是失败）；strix 后端不读（它没有扫描级墙钟超时）；
 *   · 允许写入挂载卷 —— ✅ 持久工作区挂载读写开关（v0.64）：开→`:rw`、
 *     关→`:ro`；关掉后 agent 仍可用容器内 `/work` tmpfs，只是产出出不了
 *     容器、跨重启不保留（不影响发现收集）。
 * 界面必须把这些分开标注，否则用户会以为"我把内存调到 512MB 就限制了扫描"
 * 而实际什么也没发生。
 */

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
      draft.scanMode !== config.scanMode ||
      draft.enableOffensivePrompt !== config.enableOffensivePrompt,
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

  return (
    <SettingsPanel
      id="sandbox"
      title="沙箱与执行环境"
      description="扫描时的代码执行、PoC 验证都在隔离容器内完成，不会直接作用于你的机器。"
    >
      {/* ── 运行方式（说明性，不是实时容器状态） ── */}
      {/*
        ★ 这里原来把后端配置里的一列 `status` 当成「Docker 容器现在在不在跑」
        渲染出「Docker 沙箱 · 已停止 / 沙箱未运行，发起的扫描会排队等待」。
        那是**错的两层**：
          ① 后端根本不管理常驻容器，`status` 是配置里记的一个值（默认
             `stopped`），从没被改成 `running` —— 它不是实时状态；
          ② 更关键：自研引擎的沙箱是**按每次任务临时起容器**（`suvex-sandbox`
             那一族镜像，本地构建），跑完即销毁，压根没有「一个常驻沙箱」。
             所以「已停止 ⇒ 扫描排队等待」是假话 —— 扫描照跑。
        改成如实描述「每次扫描按需起一个隔离容器」，不再假装有常驻状态。
        `config.status` 仍保留在类型里（后端契约未变），只是不再当实时灯用。
      */}
      <div className="flex flex-wrap items-center gap-x-tight gap-y-2 rounded-sm border border-line bg-surface-sunken p-3">
        <span
          className={cn(
            'flex h-7 w-7 shrink-0 items-center justify-center rounded-pill',
            'bg-signal-ok/15 text-signal-ok',
          )}
        >
          <Boxes size={14} strokeWidth={1.75} aria-hidden="true" />
        </span>

        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="flex flex-wrap items-center gap-1.5">
            <span className="text-sm text-ink">沙箱与执行环境 · 每次扫描按需启动</span>
            <span className="mono-ui text-ink-faint">内核镜像 suvex-sandbox 系列</span>
          </span>
          <span className="text-2xs text-ink-faint">
            每条扫描任务开跑时起一个隔离容器（本地镜像），跑完即销毁，不在宿主机上长期驻留。
            下面的 CPU / 内存上限作用于这个临时容器。
          </span>
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
        ★ 这一块与上面的「容器配额」分开读，因为**生效范围不同**：
        「容器配额」里的 CPU/内存/禁止出网按上面注释的形态生效（禁止出网
        对 url/service 目标会被强制放开）；「执行」里的深度/回合上限/超时
        是真生效的。混在一起写会让用户以为"我把内存调到 512MB 就限制了扫描"。
      */}
      <div className="flex flex-col gap-panel">
        <div className="flex flex-col gap-0.5">
          <span className="text-xs font-medium text-ink">执行</span>
          <span className="text-2xs text-ink-faint">
            这三项<strong className="font-medium text-ink-muted">真的生效</strong>：
            并发几秒内生效；「扫描深度」决定回合的天花板（快速 40 / 标准 80 / 深度 150 轮），
            「回合上限」在其下再收细 —— 两者取较小值；对之后新发起的扫描生效。都不需要重启服务。
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
            help="一次扫描最多跑多少轮。实际值取它与你选的「扫描深度」天花板中较小的那个（快速 40 / 标准 80 / 深度 150）。调小能缩短单次扫描，代价是复杂目标可能没跑完就被截断。"
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

      {/* ── 越权与注入手法块（v0.42）──────────────────────── */}
      {/*
        ★ 为什么单独立一块，不并进上面那个「执行」组：

        那个组回答的是「扫描跑起来是什么样」（并发、回合、深度）——
        都是**资源**。这一条回答的是「agent 用什么强度去试探目标」——
        是**行为**。混在一起会让人以为它只是又一个性能参数。

        ★ 为什么它不按题目分类设置：
        它改的是同一件事做得全不全，与题目叫什么无关。做成逐题开关
        等于把同一件事说两遍，而两处取值不一致时的表现是"打开了但没生效"。
      */}
      <div className="flex flex-col gap-panel">
        <div className="flex flex-col gap-0.5">
          <span className="text-xs font-medium text-ink">评测强度</span>
          <span className="text-2xs text-ink-faint">
            下面这一项会改变 agent 的<strong className="font-medium text-ink-muted">行为</strong>，
            不是资源配额。全局生效，不按题目分类设置。
          </span>
        </div>

        <Toggle
          id="sandbox-offensive"
          label="启用越权与注入强化手法"
          hint="打开后，解题与渗透扫描的提示词末尾都会追加一份手法矩阵（注入、越权、文件读取、SSRF、竞态的具体换形写法）。它只扩大试探的覆盖面，不放松任何判定纪律 —— 「不越界访问」「禁止口令爆破与压力测试」照旧成立。"
          checked={draft.enableOffensivePrompt}
          onChange={(v) => setDraft({ ...draft, enableOffensivePrompt: v })}
        />

        {draft.enableOffensivePrompt && (
          <div className="flex items-start gap-1.5 rounded-sm border border-signal-warn/40 bg-signal-warn/[0.06] p-2">
            <ShieldAlert
              size={12}
              strokeWidth={2}
              className="mt-0.5 shrink-0 text-signal-warn"
              aria-hidden="true"
            />
            <span className="text-2xs text-ink-muted">
              已启用。agent 会更系统地尝试注入与越权路径，耗时与回合消耗也会相应上升。
              它不影响判定标准：拿不到证据的结论仍然是「待跟进」，不会因为「打过一遍」就变成「已命中」。
            </span>
          </div>
        )}
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
            CPU、内存会下发给沙箱容器；扫描深度决定回合天花板、回合上限再收细；
            单次超时到点会停掉本次扫描；允许写入挂载卷控制工作区是读写还是只读。
            禁止出网对 url/服务类目标会被强制放开（否则连不上目标）。
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
