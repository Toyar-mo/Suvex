import { cn } from '@/lib/cn'
import { SCAN_GOAL_HINT, SCAN_GOAL_LABEL, type ScanGoal } from '@/data/scan'
import type { ScanAttachment } from '@/data/scans-store'
import { AlertTriangle, Check, FileUp, Loader2, Paperclip, X } from 'lucide-react'
import { useRef } from 'react'

/**
 * 第 4 步：任务背景（任务 12）。
 *
 * ## ★ 这一步为什么存在
 *
 * 起因是一句很实在的抱怨：**「只给网址让他扫描太没意义了」**。
 *
 * 拿 CTF 题举例，题面里有：
 *   · **考点**（"日志模板引擎的污染数据流"）—— 决定 agent 往哪个方向想；
 *   · **提示**（`%hhn`、varargs、边界条件）—— 省掉大量盲试；
 *   · **诱饵**（题面自称"安全备注"的那段）—— 不读题面就会当成真线索；
 *   · **flag 格式** —— 决定"怎样算成功"。
 *
 * 这些信息我们**猜不出来**，而它们对结果的影响远大于任何扫描参数。
 * 所以它必须有一个正式的输入通道，而不是塞在别的字段里。
 *
 * ## 它同时是通用的
 *
 * 渗透测试同样需要交代背景（"第一次评估，重点认证与越权，
 * 测试账号是 …"）。所以这里是一个**自由文本**，不是一组 CTF 字段 ——
 * 加一组 CTF 专用字段会让其他场景没有地方写。
 *
 * ⚠️ **它会含凭据**（测试账号）。所以这一段文本：
 *   · 不进 console.log；
 *   · 后端把它写进**指令文件**而不是命令行（否则 `ps aux` 里能看到全文）。
 *   界面上也要如实提示用户这一点，让他知道写进去是安全的。
 *
 * ## ★ 附件在这一步是可选还是必填，取决于目标类型（任务 17）
 *
 * 对 `url` / `repo` / `path` / `service` 来说附件是**补充材料**：目标是地址，
 * 附件只是额外线索，不传完全正常（所以标题写着"可选"）。
 *
 * 但选「题目附件」时**反过来** —— 没有地址，附件本身就是被测对象：
 * 后端把它解压成一个目录，按本地代码路径交给执行后端，缺附件直接 400。
 * 所以那一种类型下这里必须把话说明白（`attachmentsRequired`），
 * 而不是让用户在两步之后被一个 400 挡回来。
 */
export function BriefStep({
  brief,
  onBriefChange,
  goal,
  onGoalChange,
  attachments,
  onStage,
  onRemoveAttachment,
  staging,
  attachmentsRequired = false,
  showError = false,
}: {
  brief: string
  onBriefChange: (value: string) => void
  goal: ScanGoal
  onGoalChange: (value: ScanGoal) => void
  attachments: ScanAttachment[]
  onStage: (files: File[]) => void
  onRemoveAttachment: (name: string) => void
  staging: { busy: boolean; error: string | null }
  /**
   * 附件是否必填（只有「题目附件」为 true）。
   * ⚠️ 可选 + 默认 false：其余四种目标类型下附件是可选材料，
   *    不传这个 prop 时必须与改动前完全一样。
   */
  attachmentsRequired?: boolean
  /** 是否已尝试过提交 —— 决定要不要把"缺附件"显示成错误 */
  showError?: boolean
}) {
  const inputRef = useRef<HTMLInputElement>(null)

  return (
    <div className="flex flex-col gap-panel">
      <header className="flex flex-col gap-1">
        <h3 className="text-sm text-ink">告诉它这次任务是什么</h3>
        <p className="max-w-[62ch] text-xs leading-relaxed text-ink-faint">
          这一段会<strong className="font-normal text-ink-muted">原样</strong>交给智能体
          作为任务背景。题目描述、考点提示、测试范围、测试账号都可以写在这里 ——
          它比任何扫描参数都更能决定结果。
          <span className="text-ink-muted"> 不写也行</span>，但只给一个网址时，
          智能体只能盲扫。
        </p>
      </header>

      {/* ── 成功判据 ── */}
      <fieldset className="flex flex-col gap-tight">
        <legend className="mb-1 text-2xs text-ink-faint">怎样算成功</legend>
        <div role="radiogroup" className="flex flex-wrap gap-tight">
          {(Object.keys(SCAN_GOAL_LABEL) as ScanGoal[]).map((value) => {
            const active = goal === value
            return (
              <button
                key={value}
                type="button"
                role="radio"
                aria-checked={active}
                onClick={() => onGoalChange(value)}
                title={SCAN_GOAL_HINT[value]}
                className={cn(
                  'inline-flex h-9 items-center gap-1.5 rounded-sm border px-3 text-xs',
                  'transition-ui duration-fast',
                  active
                    ? 'border-accent/50 bg-accent-wash text-ink'
                    : 'border-line text-ink-muted hover:border-line-strong hover:text-ink',
                )}
              >
                {active && <Check size={12} strokeWidth={2.5} aria-hidden="true" />}
                {SCAN_GOAL_LABEL[value]}
              </button>
            )
          })}
        </div>
        {/*
          把选中的那个判据的说明**摊开写**，而不是只放在 title 里：
          它解释的是"agent 的取舍会怎么变"，而那正是用户做选择时
          唯一需要知道的事 —— 藏在悬停提示里等于没写。
        */}
        <p className="max-w-[62ch] text-2xs leading-relaxed text-ink-faint">
          {SCAN_GOAL_HINT[goal]}
        </p>
      </fieldset>

      {/* ── 任务背景正文 ── */}
      <div className="flex flex-col gap-1">
        <label htmlFor="scan-brief" className="text-2xs text-ink-faint">
          任务背景
        </label>
        <textarea
          id="scan-brief"
          value={brief}
          onChange={(e) => onBriefChange(e.target.value)}
          rows={8}
          maxLength={8000}
          placeholder={
            '例如：\n' +
            '题目：LogVault —— 日志模板引擎的污染数据流（PWN，500 分）\n' +
            '提示：关注 %hhn 与 varargs 的边界条件；题面里那段"安全备注"是诱饵。\n' +
            '目标：拿到 flag，格式 flag{...}\n' +
            '附件见下（二进制 chall）。'
          }
          className={cn(
            'w-full resize-y rounded-sm border border-line bg-surface-sunken px-2.5 py-2',
            'text-xs leading-relaxed text-ink',
            'transition-ui duration-fast placeholder:text-ink-ghost',
            'hover:border-line-strong focus:outline-none focus:ring-2 focus:ring-accent/40',
          )}
        />
        <div className="flex items-center justify-between gap-tight">
          {/*
            ⚠️ 这句提示是**功能的一部分**，不是免责声明。
            用户会犹豫"能不能把测试账号写进去" —— 不告诉他
            "这段不进程列表"，他会自己去删掉最关键的那一行。
          */}
          <p className="text-2xs text-ink-faint">
            含口令也可以写：它不会出现在进程列表里（保存在指令文件中，仅本机可读）。
          </p>
          <span
            className={cn(
              'tabular shrink-0 text-2xs',
              brief.length > 7600 ? 'text-signal-warn' : 'text-ink-ghost',
            )}
            data-numeric
          >
            {brief.length} / 8000
          </span>
        </div>
      </div>

      {/* ── 附件 ── */}
      <div className="flex flex-col gap-tight">
        {/*
          ★ 标题按"可选 / 必填"分两种说法，而不是永远写"（可选）"。
          「题目附件」那一类下附件就是被测对象本身，标题还写着"可选"
          会让用户直接跳过它，然后在确认页被一条错误拦住 —— 而那时
          他已经填完后面所有步骤了。
        */}
        <span className="text-2xs text-ink-faint">
          题目附件{attachmentsRequired ? '（这类题目必须上传）' : '（可选）'}
        </span>

        {attachments.length > 0 && (
          <ul className="flex flex-col gap-1">
            {attachments.map((file) => (
              <li
                key={file.name}
                className="flex items-center gap-2 rounded-sm border border-line px-2.5 py-1.5"
              >
                <Paperclip size={12} strokeWidth={1.75} className="shrink-0 text-ink-ghost" aria-hidden="true" />
                <span className="min-w-0 flex-1 truncate text-xs text-ink" title={file.name}>
                  {file.name}
                </span>
                <span className="tabular shrink-0 text-2xs text-ink-faint" data-numeric>
                  {formatBytes(file.sizeBytes)}
                </span>
                <button
                  type="button"
                  onClick={() => onRemoveAttachment(file.name)}
                  disabled={staging.busy}
                  aria-label={`移除 ${file.name}`}
                  /*
                    纯图标：禁用降到 `ink-ghost`（token 的约定用途就是「仅装饰 / 禁用」）。
                    不用 `opacity-50` —— 12px 的图标压一半会糊成一团，看不出形状。
                  */
                  className="shrink-0 rounded-xs p-0.5 text-ink-faint transition-ui duration-fast hover:text-signal-risk disabled:cursor-not-allowed disabled:text-ink-ghost disabled:hover:text-ink-ghost"
                >
                  <X size={12} strokeWidth={2} aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
        )}

        <div>
          <input
            ref={inputRef}
            id="scan-attachments"
            type="file"
            multiple
            className="sr-only"
            onChange={(e) => {
              const files = e.target.files
              if (files && files.length > 0) onStage([...files])
              // 清空：不清的话选同一个文件第二次不触发 change
              if (inputRef.current) inputRef.current.value = ''
            }}
          />
          <label
            htmlFor="scan-attachments"
            className={cn(
              'inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-sm border px-2.5 text-xs',
              'transition-ui duration-fast',
              /*
                上传在飞时不能压 `opacity-50` —— 那会把「上传中…」这几个字打到 2.2:1，
                而这几个字正是用户此刻唯一能确认"它真的在动"的证据。

                `<label>` 没有 `disabled` 属性，用不了 `disabled:` 伪类，所以不能写
                `busy && 'hover:border-line/50'` 去"覆盖"正常态的 `hover:border-line-strong`
                —— 两者特异性相同，谁赢看 Tailwind 生成顺序，是静默失效。
                改成互斥三元：两种状态不同时存在，就不会有覆盖问题。
              */
              staging.busy
                ? 'pointer-events-none border-line/50 text-ink-muted'
                : 'border-line text-ink-muted hover:border-line-strong hover:text-ink',
            )}
          >
            {staging.busy ? (
              <Loader2 size={12} strokeWidth={2} className="animate-spin" aria-hidden="true" />
            ) : (
              <FileUp size={12} strokeWidth={2} aria-hidden="true" />
            )}
            {staging.busy ? '上传中…' : '选择文件'}
          </label>
          <p className="mt-1 text-2xs text-ink-faint">
            任意类型（二进制、源码、字典、流量包）。它们会被放进沙箱工作区的
            <code className="mono-ui mx-0.5">attachments/</code>
            目录，智能体可以直接读。
            {/*
              ★ 这一句在「题目附件」下不是补充说明，而是**功能说明的一半**：
              那类题目没有目标地址，附件解压后的目录就是被分析的东西本身。
              不写清楚，用户会以为附件和别处一样只是"顺手带上的材料"。
            */}
            {attachmentsRequired && ' 这类题目没有目标地址，附件就是被测对象本身。'}
          </p>
        </div>

        {staging.error && (
          <p className="flex items-center gap-1 text-2xs text-signal-risk">
            <AlertTriangle size={11} strokeWidth={2} aria-hidden="true" />
            {staging.error}
          </p>
        )}

        {/*
          ⚠️ "必须上传附件"这条错误**只在点过下一步之后**才出现 ——
          与其余字段同一条原则（刚进这一步就飘红是最烦人的表单体验之一）。
          与目标地址那条提示的分工：第 1 步先告诉他"这里不用填地址、
          下一步要传附件"，这里则是在他真要往下走时拦住并说清怎么补。
        */}
        {showError && attachmentsRequired && attachments.length === 0 && (
          <p className="flex items-center gap-1 text-2xs text-signal-risk" role="alert">
            <AlertTriangle size={11} strokeWidth={2} aria-hidden="true" />
            这类题目必须至少上传一个附件 —— 没有附件就没有可分析的东西。请点上面的「选择文件」。
          </p>
        )}
      </div>
    </div>
  )
}

/** 字节 → 人话。与漏洞附件面板同一档精度（KB/MB 够用）。 */
function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  const kb = bytes / 1024
  if (kb < 1024) return `${Math.round(kb)} KB`
  return `${(kb / 1024).toFixed(1)} MB`
}
