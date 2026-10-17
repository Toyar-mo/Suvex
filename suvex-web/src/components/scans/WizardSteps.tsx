import { cn } from '@/lib/cn'
import type { Skill, TargetKind } from '@/data/scan'
import {
  SCAN_AUTHORIZATION_STATEMENT,
  SKILLS,
  TARGET_KIND_HINT,
  TARGET_KIND_LABEL,
  isApplicable,
} from '@/data/scan'
import type { ProviderConfig } from '@/data/providers'
import { Check, ExternalLink, Info } from 'lucide-react'
import { Link } from 'react-router-dom'
import type { ReactNode } from 'react'
import {
  defaultNameFrom,
  normalizeTarget,
  validateTarget,
  type TargetValidation,
} from '@/lib/validate-target'

/**
 * 新建扫描向导的四个步骤。
 *
 * 每一步只问一件事 —— 这是"对话式分步"与"一屏长表单"的实质区别：
 *   长表单把 12 个字段同时摆在面前，用户无法判断"填到哪算完"，
 *   而且第 3 项的校验错误会在滚动到看不见的地方提示。
 *
 * 每一步都遵守同一条容错原则：**错误就地显示在该字段下方，并说清怎么改。**
 * 不弹 toast、不把所有错误堆到顶部 —— 那两种做法都在强迫用户做寻址工作。
 */

// ─────────────────────────────────────────────
// 步骤 1 · 目标
// ─────────────────────────────────────────────

export interface TargetStepProps {
  kind: TargetKind
  onKindChange: (kind: TargetKind) => void
  value: string
  onValueChange: (value: string) => void
  /** 是否已经尝试过提交（用于决定要不要显示错误） */
  showError: boolean
  name: string
  onNameChange: (name: string) => void
  /**
   * 已上传的题目附件名（只给 `artifact` 用）。
   *
   * ★ 第 1 步自己上传不了文件（上传在「任务背景」那一步），这里只是把
   *   那一步的状态读出来，好让提示写得**准**："还没有附件"和
   *   "已经传了 tryre.zip"该说的话完全不一样 —— 前者要拦住他，
   *   后者要让他放心往下走。
   *
   * ⚠️ 可选 + 默认空数组：其余四种类型不需要它，调用方不传也必须照旧工作。
   */
  attachmentNames?: string[]
}

export function TargetStep({
  kind,
  onKindChange,
  value,
  onValueChange,
  showError,
  name,
  onNameChange,
  attachmentNames = [],
}: TargetStepProps) {
  const validation = validateTarget(kind, value)
  const hint = TARGET_KIND_HINT[kind]

  const isArtifact = kind === 'artifact'
  /*
    artifact 的名字来源是**附件名**（后端留空时的规则：附件名去掉扩展名）。
    这里先按同一口径算出来，只用于两个地方：名称输入框的占位符，
    以及"留空会叫什么"这个问题的答案。附件还没传时它是空串，
    那时就如实说"上传附件后自动命名"，不编一个提交后不存在的名字。
  */
  const derivedName = isArtifact ? defaultNameFrom('artifact', '', attachmentNames[0]) : ''

  return (
    <div className="flex flex-col gap-bay">
      <Field label="要测什么" help="Suvex 可以直接测已部署的应用，也可以读代码库做白盒分析。">
        {/* 用 radio 而不是 select：每个选项各有说明，下拉里塞不下 */}
        <div className="flex flex-col gap-2">
          {(Object.keys(TARGET_KIND_LABEL) as TargetKind[]).map((k) => (
            <label
              key={k}
              className={cn(
                'flex cursor-pointer items-start gap-tight rounded-sm border p-3 transition-ui duration-fast',
                kind === k
                  ? 'border-accent/50 bg-accent-wash'
                  : 'border-line hover:border-line-strong',
              )}
            >
              <input
                type="radio"
                name="target-kind"
                value={k}
                checked={kind === k}
                onChange={() => onKindChange(k)}
                className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-accent"
              />
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="text-sm text-ink">{TARGET_KIND_LABEL[k]}</span>
                <span className="text-xs text-ink-faint">{KIND_DESCRIPTION[k]}</span>
              </span>
            </label>
          ))}
        </div>
      </Field>

      {isArtifact ? (
        /*
          ★ 整块**替换**掉目标地址输入框，而不是把 `required` 去掉、留一个空框。

          空输入框的表达是"这里该填、你还没填完" —— 用户会去猜要不要随便写点什么，
          而这一类的正确答案恰恰是**什么都不填**。所以他该看到的是一句明确的
          说明加上"下一步做什么"，而不是一个等着被填的输入框。
          （这正是这次改动的起点：原来这类题建不出来，因为空着过不去。）

          外框沿用 Field + surface-sunken + border-line，读起来要和
          一个字段一样 —— 换成一条突兀的提示条会让这个位置显得"缺了点什么"。
        */
        <Field label="目标地址">
          <div className="flex flex-col gap-1.5 rounded-sm border border-line bg-surface-sunken p-3">
            <p className="text-sm text-ink">这类题目不需要目标地址。</p>
            <p className="max-w-[62ch] text-xs leading-relaxed text-ink-faint">
              {/*
                ⚠️ 这段必须是**一个字符串字面量**，不能写成跨行的裸 JSX 文本。
                JSX 会把"换行 + 缩进"折叠成**一个空格**，于是中文句子中间
                会莫名其妙多出一个空格（"解压成一个 工作目录"）——
                在界面上就是一处显眼的排版错误，而源码里看不出来。
                字符串字面量 prettier 不会折行，所以它是稳定的。
              */}
              {
                '请到下一步「任务背景」上传题目文件（zip / ELF / 流量包都行）。附件会被解压成一个工作目录，直接当作代码目录来分析 —— 全程没有网络请求要发。'
              }
            </p>
            {attachmentNames.length > 0 ? (
              // 有附件就说清楚是哪些：用户没法在这一步看到文件列表，
              // 而他可能刚从「任务背景」退回来改类型，正想确认那些文件还在。
              <p className="text-xs text-signal-ok">
                已上传 {attachmentNames.length} 个附件：{attachmentNames.join('、')}
              </p>
            ) : (
              // 只写"还没有附件"不够 —— 要说清后果（没有附件就提交不了），
              // 否则用户以为它和别的可选字段一样
              <p className="text-xs text-signal-warn">
                还没有附件。这类题目必须至少上传一个，否则不能提交。
              </p>
            )}
          </div>
        </Field>
      ) : (
        <Field label="目标地址" required help={hint.example}>
          <input
            type="text"
            value={value}
            onChange={(e) => onValueChange(e.target.value)}
            placeholder={hint.placeholder}
            aria-invalid={showError && !validation.ok}
            aria-describedby="target-feedback"
            // 等宽：地址是要精确输入的东西，不是散文
            className={cn(
              'h-10 w-full rounded-sm border bg-surface-sunken px-3 font-mono text-sm text-ink',
              'transition-ui duration-fast placeholder:text-ink-faint',
              'focus:outline-none focus:ring-2 focus:ring-accent/40',
              showError && !validation.ok
                ? 'border-signal-risk'
                : 'border-line hover:border-line-strong',
            )}
          />
          <ValidationFeedback id="target-feedback" show={showError} validation={validation} />
        </Field>
      )}

      <Field
        label="扫描名称"
        help={isArtifact ? '留空则用题目附件名作为名称。' : '留空则用目标地址作为名称。'}
      >
        <input
          type="text"
          value={name}
          onChange={(e) => onNameChange(e.target.value)}
          /*
            占位符直接给出**会实际生效的那个名字**（附件名去掉扩展名，
            与后端同口径），而不是含糊的「可选」—— 后者回答不了
            "留空会叫什么"，而这个名字会出现在扫描列表里。
          */
          placeholder={isArtifact ? derivedName || '上传附件后自动命名' : value.trim() || '可选'}
          className={cn(
            'h-10 w-full rounded-sm border border-line bg-surface-sunken px-3 text-sm text-ink',
            'transition-ui duration-fast placeholder:text-ink-faint hover:border-line-strong',
            'focus:outline-none focus:ring-2 focus:ring-accent/40',
          )}
        />
      </Field>
    </div>
  )
}

const KIND_DESCRIPTION: Record<TargetKind, string> = {
  url: '对运行中的服务发请求，按实际响应判断。',
  repo: '克隆仓库后先读代码，再针对可疑位置做动态验证。',
  path: '直接分析本机目录里的代码，不经过网络。',
  // ★ 不写成"对网站发请求"的变体：这一类根本没有 HTTP 层，
  //   说清楚"没有 HTTP"比说"是一种服务"有用得多 —— 后者会让人
  //   以为流程一样、只是端口不同。
  service: '直连裸 TCP 端口收发原始字节（Pwn、网络 Misc、Crypto 预言机）。没有 HTTP 层。',
  // ★ 一句话点明"没有可连的地址"，这正是用户选它时的判断依据；
  //   顺带把 CTF 里那三个大类名词写出来 —— 用户是按题目分类找过来的。
  artifact: '逆向、纯文件 Misc、密码学 —— 考点全在附件里，没有可连的地址。',
}

// ─────────────────────────────────────────────
// 步骤 2 · 技能范围
// ─────────────────────────────────────────────

export interface SkillsStepProps {
  selected: Set<string>
  onToggle: (id: string) => void
  kind: TargetKind
  showError: boolean
}

export function SkillsStep({ selected, onToggle, kind, showError }: SkillsStepProps) {
  const applicable = SKILLS.filter((s) => isApplicable(s, kind))
  const inapplicable = SKILLS.filter((s) => !isApplicable(s, kind))

  return (
    <div className="flex flex-col gap-bay">
      <Field
        label="检测范围"
        required
        help="勾选得越多，扫描越慢、消耗的 token 越多。建议先跑一轮核心项。"
      >
        <div className="flex flex-col gap-2">
          {applicable.map((skill) => (
            <SkillRow
              key={skill.id}
              skill={skill}
              checked={selected.has(skill.id)}
              onToggle={() => onToggle(skill.id)}
            />
          ))}
        </div>

        {/*
          不适用的技能**显示出来但禁用**，而不是直接隐藏。
          隐藏会让用户以为"这个产品没有云配置检查"；
          显示并说明"这类目标不适用"才回答了真正的问题。
        */}
        {inapplicable.length > 0 && (
          <div className="mt-panel flex flex-col gap-2">
            <p className="text-2xs text-ink-faint">以下技能不适用于{TARGET_KIND_LABEL[kind]}：</p>
            {inapplicable.map((skill) => (
              <SkillRow key={skill.id} skill={skill} checked={false} onToggle={() => {}} disabled />
            ))}
          </div>
        )}

        {showError && selected.size === 0 && (
          <p className="mt-2 flex items-center gap-1 text-xs text-signal-risk" role="alert">
            <Info size={12} strokeWidth={2} aria-hidden="true" />
            至少勾选一项，否则扫描没有可用的检测能力
          </p>
        )}
      </Field>
    </div>
  )
}

/**
 * 技能适用性判定（`isApplicable`）放在数据层 `data/scan.ts`。
 *
 * ★ 为什么不在这个文件里定义：它有两个调用点 —— 这里把技能分成
 *   适用 / 不适用两区，`NewScanWizard` 换目标类型时剔掉不再适用的勾选。
 *   组件文件里导出普通函数会让 fast refresh 失效
 *   （`react-refresh/only-export-components`），而两处各写一份判断
 *   必然漂移，症状正是"界面上没勾、提交时却带着它"。
 */

function SkillRow({
  skill,
  checked,
  onToggle,
  disabled = false,
}: {
  skill: Skill
  checked: boolean
  onToggle: () => void
  disabled?: boolean
}) {
  return (
    <label
      className={cn(
        'flex items-start gap-tight rounded-sm border p-3 transition-ui duration-fast',
        disabled
          ? // ★ 不用 opacity 表达"禁用"。
            //   opacity 是乘在**整行**上的，会把主标签从 15.7:1 拽到 3.93:1、
            //   描述拽到 2.13:1 —— 连"这个技能是干什么的"都读不出来，
            //   而这段文字的全部意义就是解释为什么它不可选。
            //   改用「降一级文字色 + 减淡边框 + 禁止光标」，视觉上照样是灰的，
            //   但每一层都还在 AA 之上。
            'cursor-not-allowed border-line/50'
          : checked
            ? 'cursor-pointer border-accent/50 bg-accent-wash'
            : 'cursor-pointer border-line hover:border-line-strong',
      )}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        disabled={disabled}
        /*
          原本这里压 `opacity-35`，理由是"控件不是文字、不受对比度规则约束"。
          但 design-system.md 的条款是不分控件与文字的：`opacity` 乘在整棵子树上、
          不认识层级，一旦将来这个 input 有了自定义外观（现在就有 `accent-accent`
          接管选中色），压暗会连着把选中态一起糊掉。

          改成换色：选中色从 `accent` 降到 `accent-dim`。"不可选"与"我没选"的区分
          由整行负责 —— 边框已经是 `border-line/50`、文字是 `ink-muted`、
          光标是 `cursor-not-allowed`，比让一个 14px 的方框变淡更说得清。
        */
        className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-accent disabled:cursor-not-allowed disabled:accent-accent-dim"
      />
      <span className="flex min-w-0 flex-col gap-0.5">
        <span className="flex flex-wrap items-center gap-1.5">
          <span className={cn('text-sm', disabled ? 'text-ink-muted' : 'text-ink')}>
            {skill.label}
          </span>
          {skill.needsCredentials && (
            // 需要凭据的技能要提前说 —— 否则用户跑到一半才发现缺配置
            <span
              className={cn(
                'rounded-xs border border-signal-warn/40 px-1 text-2xs text-signal-warn',
                disabled && 'border-signal-warn/25 text-signal-warn/80',
              )}
            >
              需要凭据
            </span>
          )}
        </span>
        <span className="text-xs text-ink-faint">{skill.description}</span>
      </span>
    </label>
  )
}

// ─────────────────────────────────────────────
// 步骤 3 · LLM 配置
// ─────────────────────────────────────────────

export interface ProviderStepProps {
  providers: ProviderConfig[]
  selectedId: string | null
  onSelect: (id: string) => void
}

export function ProviderStep({ providers, selectedId, onSelect }: ProviderStepProps) {
  const configured = providers.filter((p) => p.configured)
  const unconfigured = providers.filter((p) => !p.configured)

  // 一个都没配：必须给出去路，而不是让用户卡住
  if (configured.length === 0) {
    return (
      <div className="flex flex-col items-start gap-panel">
        <p className="max-w-[52ch] text-sm text-ink-muted">
          还没有配置任何可用的模型。扫描需要一个能用的 LLM 才能规划步骤和判断结果。
        </p>

        <Link
          to="/settings"
          className="press inline-flex h-10 items-center gap-2 rounded-sm bg-accent px-3 text-sm font-medium text-canvas transition-ui duration-fast hover:bg-accent-strong"
        >
          前往设置配置模型
          <ExternalLink size={14} strokeWidth={2} aria-hidden="true" />
        </Link>

        <p className="text-xs text-ink-faint">配置完成后回到这里，新配置的模型会自动出现。</p>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-bay">
      <Field label="使用哪个模型" required help="决定扫描时的推理质量与成本。">
        <div className="flex flex-col gap-2">
          {configured.map((provider) => (
            <label
              key={provider.id}
              className={cn(
                'flex cursor-pointer items-start gap-tight rounded-sm border p-3 transition-ui duration-fast',
                selectedId === provider.id
                  ? 'border-accent/50 bg-accent-wash'
                  : 'border-line hover:border-line-strong',
              )}
            >
              <input
                type="radio"
                name="provider"
                checked={selectedId === provider.id}
                onChange={() => onSelect(provider.id)}
                className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-accent"
              />
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="flex flex-wrap items-center gap-1.5">
                  <span className="text-sm text-ink">{provider.label}</span>
                  <span className="mono-ui text-ink-faint">{provider.model}</span>
                  {provider.kind === 'local' && (
                    <span className="rounded-xs border border-line px-1 text-2xs text-ink-faint">
                      本地
                    </span>
                  )}
                </span>
                <span className="text-xs text-ink-faint">{provider.note}</span>
              </span>
            </label>
          ))}
        </div>
      </Field>

      {/* 未配置的也列出来，说明缺什么 —— 用户可以判断要不要去配 */}
      {unconfigured.length > 0 && (
        <div className="flex flex-col gap-2">
          <p className="text-2xs text-ink-faint">尚未配置：</p>
          {unconfigured.map((provider) => (
            <div
              key={provider.id}
              // 同 SkillRow：不整卡套 opacity。这里的「去配置」是真的可点链接，
              // 被 opacity-60 压到 3.57:1 就不能接受了。
              // 用减淡边框 + 虚线表达"尚未配置"，文字保持可读。
              className="flex items-center justify-between gap-tight rounded-sm border border-dashed border-line/60 p-3"
            >
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="text-sm text-ink-muted">{provider.label}</span>
                <span className="text-xs text-ink-faint">{provider.missing}</span>
              </span>
              <Link
                to="/settings"
                className="shrink-0 text-xs text-accent-strong underline hover:text-accent"
              >
                去配置
              </Link>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────
// 步骤 4 · 确认
// ─────────────────────────────────────────────

export interface ConfirmStepProps {
  name: string
  target: string
  kind: TargetKind
  skills: Skill[]
  provider: ProviderConfig | null
  /**
   * 是否已勾选授权确认（契约 §5.2）。
   *
   * ⚠️ 状态由**父组件**持有，而不是这里自己 `useState`：
   *    启动按钮在父组件（`LaunchButton` 需要据此决定禁不禁用），
   *    而"禁不禁用"依赖这个值。放在这里就得再把状态提上去一次。
   */
  authorized: boolean
  onAuthorizedChange: (value: boolean) => void
  onJump: (index: number) => void
  /**
   * 已上传的题目附件名（只给 `artifact` 用）。
   *
   * ⚠️ 必需而不是可有可无的小细节：`artifact` 的 `target` 契约上就是空串，
   *    不处理的话确认页会渲染出一行空的「目标：」—— 看起来像页面坏了，
   *    而用户会在这一步决定要不要按那个高风险的启动按钮。
   */
  attachmentNames?: string[]
}

export function ConfirmStep({
  name,
  target,
  kind,
  skills,
  provider,
  authorized,
  onAuthorizedChange,
  onJump,
  attachmentNames = [],
}: ConfirmStepProps) {
  const isArtifact = kind === 'artifact'
  /*
    artifact 下显示什么代替"目标"：
      · 有附件 → 第一个附件的文件名（后端也会把它写进 `scans.target`，
        所以这不是界面上另编的说法，而是提交后真的会存进去的那个值）；
      · 没附件 → 退回类型名「题目附件」。此时提交本来就会被拦住，
        所以这里不需要也不该编一个名字。
  */
  const targetLabel = isArtifact
    ? (attachmentNames[0] ?? TARGET_KIND_LABEL.artifact)
    : normalizeTarget(kind, target)

  return (
    <div className="flex flex-col gap-bay">
      {/*
        把要执行的动作写成一句完整的话。
        确认页的价值不是"再列一遍字段"，而是让用户**读懂将要发生什么** ——
        尤其这是高风险操作。
      */}
      <div className="rounded-md border border-line bg-surface-sunken p-panel">
        <p className="text-sm leading-relaxed text-ink">
          {isArtifact ? (
            <>
              将对上传的题目附件 <Mono>{targetLabel}</Mono> 做一次白盒分析，使用{' '}
              <Mono>{provider?.label ?? '未选择'}</Mono>，启用{' '}
              <Mono>{skills.map((s) => s.label).join(' · ') || '未选择'}</Mono> 等{skills.length}{' '}
              项检测能力。
            </>
          ) : (
            <>
              将对 <Mono>{targetLabel}</Mono> 发起一次主动扫描，使用{' '}
              <Mono>{provider?.label ?? '未选择'}</Mono>，启用{' '}
              <Mono>{skills.map((s) => s.label).join(' · ') || '未选择'}</Mono> 等{skills.length}{' '}
              项检测能力。
            </>
          )}
        </p>
        <p className="mt-tight text-xs text-ink-muted">
          {isArtifact
            ? // ★ 不能照抄下面那句"会向目标发送真实请求" —— artifact 下**没有**目标，
              //   一个字的网络请求都不会发。照抄会让用户以为要额外授权一个外部地址，
              //   而这里真正需要他确认的是"这些文件我有权看"。
              '附件会解压到沙箱工作目录里分析，不会向任何地址发包。请确认这些文件你有权查看。'
            : '扫描会向目标发送真实请求，可能触发对方的告警或日志记录。确认这个目标是你有权测试的。'}
        </p>
      </div>

      <dl className="flex flex-col gap-0">
        <SummaryRow
          label="扫描名称"
          value={name || defaultNameFrom(kind, target, attachmentNames[0])}
          onEdit={() => onJump(0)}
        />
        <SummaryRow label="目标类型" value={TARGET_KIND_LABEL[kind]} onEdit={() => onJump(0)} />
        <SummaryRow label="目标" value={targetLabel} mono />
        <SummaryRow
          label="检测范围"
          value={skills.map((s) => s.label).join('、')}
          onEdit={() => onJump(1)}
        />
        <SummaryRow
          label="模型"
          value={provider ? `${provider.label} · ${provider.model}` : '未选择'}
          onEdit={() => onJump(2)}
        />
      </dl>

      {/*
        ★ 授权确认（契约 §5.5）—— 本产品唯一涉及法律责任的设计。

        三件事必须同时成立，缺一条这个复选框就是摆设：

        1. **必勾**：没勾时启动按钮禁用，并给出原因（`LaunchButton` 已经
           强制要求"禁用必须给原因"，所以这条约束能自然接进去）；
        2. **措辞逐字一致**：服务端会逐字比对那句话（契约 §5.3），
           所以文案直接用 `SCAN_AUTHORIZATION_STATEMENT` 常量 ——
           在组件里另写一句就会前后端不一致，提交时返 400，
           而那个报错看起来像网络问题；
        3. **落库留痕**：勾选后连同服务端时刻、IP、UA 一起写进那条扫描
           的记录里。所以这里要说清"会被记录"，而不是让用户以为
           这只是个走形式的前端弹窗。
      */}
      <label
        className={cn(
          'flex cursor-pointer items-start gap-tight rounded-md border p-panel transition-ui duration-fast',
          authorized
            ? 'border-signal-ok/40 bg-signal-ok/[0.06]'
            : 'border-line-strong bg-surface hover:border-accent/40',
        )}
      >
        <input
          type="checkbox"
          checked={authorized}
          onChange={(event) => onAuthorizedChange(event.target.checked)}
          className="mt-0.5 h-4 w-4 shrink-0 accent-accent"
        />
        <span className="flex flex-col gap-1">
          <span className="text-sm text-ink">{SCAN_AUTHORIZATION_STATEMENT}</span>
          <span className="text-2xs text-ink-muted">
            对未授权目标发起扫描在很多司法管辖区是违法行为。这次确认会连同服务端时间与来源
            一起记录在这条扫描的历史里 —— 它不是一个只在界面上出现的勾。
          </span>
        </span>
      </label>
    </div>
  )
}

function SummaryRow({
  label,
  value,
  mono = false,
  onEdit,
}: {
  label: string
  value: string
  mono?: boolean
  onEdit?: () => void
}) {
  return (
    <div className="flex items-baseline justify-between gap-panel border-b border-line/60 py-2.5 last:border-b-0">
      <dt className="shrink-0 text-xs text-ink-faint">{label}</dt>
      <dd className="flex min-w-0 items-baseline gap-tight">
        <span className={cn('truncate text-sm text-ink', mono && 'mono-ui')}>{value}</span>
        {onEdit && (
          <button
            type="button"
            onClick={onEdit}
            className="shrink-0 text-xs text-accent-strong hover:text-accent hover:underline"
          >
            修改
          </button>
        )}
      </dd>
    </div>
  )
}

// ─────────────────────────────────────────────
// 共用小组件
// ─────────────────────────────────────────────

function Field({
  label,
  help,
  required = false,
  children,
}: {
  label: string
  help?: string
  required?: boolean
  children: ReactNode
}) {
  return (
    <div className="flex flex-col gap-tight">
      <div className="flex items-baseline gap-1.5">
        <span className="text-sm font-medium text-ink">{label}</span>
        {required && (
          <span className="text-2xs text-signal-risk" aria-label="必填">
            必填
          </span>
        )}
      </div>
      {help && <p className="text-xs text-ink-faint">{help}</p>}
      {children}
    </div>
  )
}

/**
 * 字段级校验反馈。
 *
 * 只在"提交过"之后才显示错误 —— 用户刚开始输入就飘红是最烦人的表单体验之一。
 * 但**提示（hint）一直显示**，因为它是在帮忙而不是在指责。
 */
function ValidationFeedback({
  id,
  show,
  validation,
}: {
  id: string
  show: boolean
  validation: TargetValidation
}) {
  if (show && validation.error) {
    return (
      <p id={id} role="alert" className="mt-1.5 flex items-start gap-1 text-xs text-signal-risk">
        <Info size={12} strokeWidth={2} className="mt-0.5 shrink-0" aria-hidden="true" />
        <span>
          {validation.error}
          {validation.hint && <span className="text-ink-faint"> —— {validation.hint}</span>}
        </span>
      </p>
    )
  }

  if (validation.hint) {
    return (
      <p id={id} className="mt-1.5 text-xs text-ink-faint">
        {validation.hint}
      </p>
    )
  }

  return null
}

function Mono({ children }: { children: ReactNode }) {
  return <code className="mono-ui text-accent-strong">{children}</code>
}

export { Check }
