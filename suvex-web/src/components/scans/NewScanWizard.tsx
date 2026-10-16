import { cn } from '@/lib/cn'
import {
  SCAN_AUTHORIZATION_STATEMENT,
  SKILLS,
  isApplicable,
  type ScanGoal,
  type SkillId,
  type TargetKind,
} from '@/data/scan'
import { useProviders } from '@/data/providers'
import {
  createScan,
  stageScanAttachments,
  type ScanAttachment,
} from '@/data/scans-store'
import { showToast } from '@/data/toast-store'
import { errorMessage } from '@/lib/api'
import { ArrowLeft, ArrowRight, Check, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { LaunchButton, StepIndicator } from './WizardChrome'
import { BriefStep } from './BriefStep'
import { ConfirmStep, ProviderStep, SkillsStep, TargetStep } from './WizardSteps'
import { defaultNameFrom, normalizeTarget, validateTarget } from '@/lib/validate-target'

/**
 * 新建扫描向导。
 *
 * 为什么是分步而不是一屏长表单（brief 的要求，也是这里的设计依据）：
 *   一屏长表单把 12 个字段同时铺开，用户看不出"填到哪算完"；
 *   而且第 3 项校验失败时，错误提示可能在滚动位置之外。
 *   分步之后每一步只有一个焦点，校验就地反馈，进度是可见的。
 *
 * 步骤：
 *   0 目标 → 1 检测范围 → 2 模型 → 3 确认
 *
 * 两个刻意的取舍：
 *   1. **下一步按钮不预先禁用**。填了空就点"下一步"时，需求是"告诉我哪里不对"，
 *      而不是"什么都不发生"。所以按钮始终可点，点完就地报错并聚焦。
 *      预先禁用会让用户对着一个灰按钮猜哪里没填。
 *   2. **最后一步才出现"启动扫描"**，且它是高风险按钮的完整四态
 *      （见 WizardChrome 的 LaunchButton）。
 */

const STEPS = [
  { id: 'target', label: '目标' },
  { id: 'skills', label: '检测范围' },
  { id: 'provider', label: '模型' },
  // ★ 任务背景（任务 12）。它排在「确认」之前：用户填完配置，
  //   最后交代"这次到底要干什么"，然后是确认。
  { id: 'brief', label: '任务背景' },
  { id: 'confirm', label: '确认' },
]

export interface NewScanWizardProps {
  /** 完成后回调（关闭向导、跳转等） */
  onDone: (createdScanId: string | null) => void
  onCancel: () => void
}

export function NewScanWizard({ onDone, onCancel }: NewScanWizardProps) {
  const providers = useProviders()

  const [stepIndex, setStepIndex] = useState(0)
  const [completed, setCompleted] = useState<Set<number>>(new Set())

  // 表单状态
  const [kind, setKind] = useState<TargetKind>('url')
  const [target, setTarget] = useState('')
  const [name, setName] = useState('')
  const [skills, setSkills] = useState<Set<string>>(new Set(['api']))
  const [providerId, setProviderId] = useState<string | null>(
    () => providers.find((p) => p.configured)?.id ?? null,
  )

  // ── 任务背景与判据（任务 12）──
  const [brief, setBrief] = useState('')
  const [goal, setGoal] = useState<ScanGoal>('findVulns')

  /**
   * 已暂存的题目附件 + 它的批次 id。
   *
   * ★ 附件走**暂存**而不是"建完再传"：扫描创建后毫秒级被调度器认领，
   *   而按 id 上传要求 `queued` 状态 —— 那个窗口根本不存在。
   *   所以选文件时立刻上传拿 stagingId，点启动时带上它。
   */
  const [staged, setStaged] = useState<{ stagingId: string | null; files: ScanAttachment[] }>({
    stagingId: null,
    files: [],
  })
  const [staging, setStaging] = useState<{ busy: boolean; error: string | null }>({
    busy: false,
    error: null,
  })

  const [showErrors, setShowErrors] = useState(false)
  const [launching, setLaunching] = useState(false)
  /**
   * 授权确认（契约 §5.2）。
   *
   * 默认 **false** —— 绝不能默认勾上。这个勾的全部价值就在于
   * "用户主动同意过那句**具体的话**"，预勾等于把它降级成一个
   * 走过场的弹窗，而服务端会把它当作法律留痕落库。
   */
  const [authorized, setAuthorized] = useState(false)
  /**
   * 启动失败的原因。
   *
   * 必须分辨：`403 TARGET_NOT_AUTHORIZED`（去勾授权）、
   * `409`（并发满，稍后再试）、网络不可达（去起后端）——
   * 三种的下一步动作完全不同，用同一句"启动失败"会把人引向错误方向。
   */
  const [launchError, setLaunchError] = useState<string | null>(null)

  const selectedSkills = useMemo(() => SKILLS.filter((s) => skills.has(s.id)), [skills])
  const selectedProvider = providers.find((p) => p.id === providerId) ?? null

  /**
   * 已上传附件的文件名（任务 17）。
   *
   * ★ 单独抽出来，是因为 `artifact` 下**名字与目标都从它来**：
   *   「扫描名称」的预览取第一个附件名去扩展名，确认页的「目标」行显示
   *   第一个附件名 —— 两处都必须与后端同口径（后端把第一个附件的
   *   文件名写进 `scans.target`，留空的名字也用附件名推导）。
   *   在这里算一次，就不会出现两个地方各推一套、显示不一致的情况。
   */
  const attachmentNames = useMemo(() => staged.files.map((f) => f.name), [staged.files])

  /**
   * 换目标类型。
   *
   * ⚠️ 必须顺手**剔掉新类型不适用的技能**，不能只 `setKind`。
   * 不剔的话会出现一种没人能解释的状态：先选「已部署的应用」（默认勾着
   * 「API 接口」），再切成「题目附件」—— 界面上「API 接口」明明躺在
   * "不适用"那一区、复选框是空的，提交上去的 `skills` 里却还带着 `api`。
   * 而这一类错误在界面上**看不到**，只能等扫描跑完发现方向不对。
   *
   * 判定直接复用 `SkillsStep` 的 `isApplicable`：两处各写一遍必然漂移，
   * 而漂移的表现正是上面那种"显示与提交不一致"。
   */
  const handleKindChange = (next: TargetKind) => {
    setKind(next)
    setSkills((prev) => new Set([...prev].filter((id) => {
      const skill = SKILLS.find((s) => s.id === id)
      return skill !== undefined && isApplicable(skill, next)
    })))
  }

  /**
   * 当前步骤能否推进。
   *
   * 注意：这个值**不用于禁用"下一步"按钮**（见文件头注释），
   * 只用于决定"点下去之后要不要报错"。
   */
  const stepValid = useMemo(() => {
    switch (stepIndex) {
      case 0:
        // artifact 下目标**必须为空**，而 validateTarget 对它的空值返回通过 ✓
        return validateTarget(kind, target).ok
      case 1:
        return skills.size > 0
      case 2:
        return providerId !== null && selectedProvider?.configured === true
      /*
        任务背景：除 artifact 外**没有必填项** —— 不写背景是正常情况。
        ⚠️ artifact 是例外：它的必填项从"目标地址"挪到了这里（附件就是被测对象），
        没有附件时后端直接 400。所以要在这一跳就拦住，
        并且由 BriefStep 就地说明原因（见 BriefStepProps.showError）。
      */
      case 3:
        return kind !== 'artifact' || staged.files.length > 0
      case 4:
        return true
      default:
        return false
    }
  }, [stepIndex, kind, target, skills, providerId, selectedProvider, staged.files])

  const goNext = () => {
    if (!stepValid) {
      setShowErrors(true)
      return
    }
    setShowErrors(false)
    setCompleted((prev) => new Set(prev).add(stepIndex))
    setStepIndex((i) => Math.min(i + 1, STEPS.length - 1))
  }

  const goBack = () => {
    setShowErrors(false)
    setStepIndex((i) => Math.max(i - 1, 0))
  }

  const jumpTo = (index: number) => {
    setShowErrors(false)
    setStepIndex(index)
  }

  /**
   * 暂存题目附件（任务 12）。
   *
   * ★ 选文件时**立刻**上传，而不是等到点启动 —— 见 `staged` 的说明。
   *
   * 失败时**保留已有的**附件并显示原因：网络抖动让用户重选一遍
   * 所有文件是很糟的体验，而他刚才选的可能是一个大字典。
   */
  const handleStageFiles = async (files: File[]) => {
    setStaging({ busy: true, error: null })
    try {
      const result = await stageScanAttachments(files)
      // 再传一批是**追加**：用户可以分几次选（比如先选二进制、再选字典）
      setStaged((prev) => ({
        stagingId: result.stagingId,
        files: [...prev.files, ...result.files],
      }))
      setStaging({ busy: false, error: null })
    } catch (err) {
      setStaging({ busy: false, error: errorMessage(err) })
    }
  }

  /**
   * 从列表里移除一个附件。
   *
   * ⚠️ 这里**只从界面上移除**，不动暂存区的文件 —— 因为它没有一个
   * 对应的"删除单个暂存文件"端点（暂存批次是原子的：要么整批认领，
   * 要么过期清掉）。如实反映这一点：文案说"不随本次扫描提交"，
   * 而不是"已删除"。
   */
  const handleRemoveAttachment = (name: string) => {
    setStaged((prev) => ({ ...prev, files: prev.files.filter((f) => f.name !== name) }))
  }

  const handleLaunch = async () => {
    if (!authorized) return
    setLaunching(true)
    setLaunchError(null)

    try {
      const created = await createScan({
        // artifact 的名字来自附件名（第三个参数），后端留空时也是这个规则
        name: name.trim() || defaultNameFrom(kind, target, attachmentNames[0]),
        target: normalizeTarget(kind, target),
        targetKind: kind,
        skills: selectedSkills.map((s) => s.id as SkillId),
        providerId: providerId ?? '',
        // ★ 授权确认（契约 §5.2）。措辞用常量 —— 服务端逐字比对
        // ★ 任务背景、判据与附件（任务 12）。
        // 判据默认 findVulns，空背景后端会归一成 null。
        brief,
        goal,
        attachmentStagingId: staged.stagingId,
        authorization: {
          confirmed: true,
          statement: SCAN_AUTHORIZATION_STATEMENT,
        },
      })
      setLaunching(false)
      /*
        ★ 目标可达性预检的提示（任务 11）——**只在这一次弹**。

        `notice` 是"刚刚那次创建观察到的事实"，后端只在创建响应里带它，
        `GET /scans` 里没有（也就不会被存进列表）。

        为什么值得单独提示：实测有过一次扫描跑 32 分钟零产出，原因是
        目标只应答 http 而 agent 选了 https。用户填 `example.com` 时
        心里想的可能是 https，真相可能相反 —— 不告诉他，他会看着
        一次毫无产出的扫描猜半天。所以这里**不是**可选的成功提示，
        而是这次创建最该被看见的那句话。
      */
      if (created.notice) {
        showToast({ message: created.notice, tone: 'info' })
      }
      // 用**后端分配的 id** 跳转 —— 本地生成 id 的话，跳过去那条记录并不存在
      onDone(created.id)
    } catch (error) {
      setLaunching(false)
      setLaunchError(errorMessage(error))
    }
  }

  /** 只有最后一步的启动按钮可能被禁用，且必须给出原因 */
  const launchDisabledReason = (() => {
    if (stepIndex !== STEPS.length - 1) return null
    /*
      ⚠️ artifact 这一条必须排在目标校验**之前**，而且不能省。

      `validateTarget('artifact', '')` 是**通过**的（空地址正是它的合法值），
      所以拿"目标无效"当理由在这里永远是 null —— 真正的缺失项是附件。
      不给这条的话，用户会看到一个能按的「启动扫描」，按下去拿回一个
      400，而那条报错里不会说"去哪补附件"。
    */
    if (kind === 'artifact' && staged.files.length === 0) {
      return '这类题目必须上传题目附件，请回到「任务背景」一步上传'
    }
    if (!validateTarget(kind, target).ok) return '目标地址无效，请返回第一步修正'
    if (skills.size === 0) return '未选择任何检测能力'
    if (!selectedProvider?.configured) return '未选择可用的模型'
    // 契约 §5.5 指定的措辞，逐字使用
    if (!authorized) return '请先确认你有权测试该目标'
    return null
  })()

  return (
    <div
      className="flex flex-col rounded-md border border-line bg-surface shadow-overlay"
      role="dialog"
      aria-labelledby="wizard-title"
    >
      {/* ── 头部：标题 + 进度 + 关闭 ── */}
      <div className="flex flex-wrap items-center gap-panel border-b border-line px-panel py-3">
        <h2 id="wizard-title" className="text-sm font-medium text-ink">
          新建扫描
        </h2>

        <div className="min-w-0 flex-1">
          <StepIndicator
            steps={STEPS}
            currentIndex={stepIndex}
            completed={completed}
            onJump={jumpTo}
          />
        </div>

        <button
          type="button"
          onClick={onCancel}
          aria-label="关闭"
          className="shrink-0 rounded-sm p-1.5 text-ink-faint transition-ui duration-fast hover:bg-surface-raised hover:text-ink"
        >
          <X size={16} strokeWidth={1.75} aria-hidden="true" />
        </button>
      </div>

      {/* ── 步骤内容 ──
          ★ 任务 8 改：`AnimatedContent`（gsap + ScrollTrigger）→ 纯 CSS `animate-page-in`。

          两个理由，第二个是硬缺陷：

          1. **它把 gsap 钉在主 chunk 里。** AnimatedContent 静态 import gsap，
             而向导属于 ScansPage 的依赖链 —— 构建报告确认过，只要这个 import
             存在，gsap（70 kB 未压缩 / 27 kB gzip）就永远进不了独立 chunk，
             每个只想看首页的人都要先下载它。
             改完之后 gsap 只被懒加载的 AgentField 依赖，落进独立 chunk。

          2. **它是"先隐藏、等 JS 放行"。** AnimatedContent 渲染时带 `invisible`，
             靠 gsap 在 effect 里 `set visibility:'visible'` 兜回来。
             在一个**表单**里这个失败模式尤其不能接受：gsap 若因为任何原因
             没跑完，用户看到的是一片空白 —— 而这不是装饰区域，
             是他要填的目标、要选的模型、要按的启动按钮。
             纯 CSS 动画第一帧就可见，且不依赖任何 JS。

          过渡仍然只用 fade + 8px 上移的**语义**（现在是纯透明度，见下方说明），
          每次切换重播 —— 靠 `key={stepIndex}` 重挂载。
          不用左右滑动：分步表单的前进/后退方向虽然明确，但横向位移
          会让用户在窄屏上产生"页面在晃"的感觉。

          减少动效偏好下不带 animation 类：index.css 的全局兜底还会把时长
          压到 0.001ms，两条一起保证"要么快速淡入、要么完全不动"。 */}
      <div className="min-h-[22rem] px-panel py-bay">
        <div key={stepIndex} className="animate-page-in">
          <StepBody
            index={stepIndex}
            kind={kind}
            onKindChange={handleKindChange}
            target={target}
            onTargetChange={setTarget}
            name={name}
            onNameChange={setName}
            showErrors={showErrors}
            skills={skills}
            onToggleSkill={(id) =>
              setSkills((prev) => {
                const next = new Set(prev)
                if (next.has(id)) next.delete(id)
                else next.add(id)
                return next
              })
            }
            providers={providers}
            providerId={providerId}
            onSelectProvider={setProviderId}
            selectedSkills={selectedSkills}
            authorized={authorized}
            onAuthorizedChange={setAuthorized}
            onJump={jumpTo}
            brief={brief}
            onBriefChange={setBrief}
            goal={goal}
            onGoalChange={setGoal}
            attachments={staged.files}
            attachmentNames={attachmentNames}
            onStage={(files) => void handleStageFiles(files)}
            onRemoveAttachment={handleRemoveAttachment}
            staging={staging}
          />
        </div>
      </div>

      {/* ── 底部：导航 + 高风险启动 ── */}
      <div className="flex flex-wrap items-center justify-between gap-panel border-t border-line px-panel py-3">
        <button
          type="button"
          onClick={stepIndex === 0 ? onCancel : goBack}
          className={cn(
            'inline-flex h-10 items-center gap-1.5 rounded-sm px-3 text-sm',
            'text-ink-muted transition-ui duration-fast hover:bg-surface-raised hover:text-ink',
          )}
        >
          <ArrowLeft size={14} strokeWidth={1.75} aria-hidden="true" />
          {stepIndex === 0 ? '取消' : '上一步'}
        </button>

        {stepIndex < STEPS.length - 1 ? (
          /*
            ★ 改成实心主按钮，并加宽内距。

            1. **底色用全站统一的实心主按钮范式** `bg-accent text-canvas
               hover:bg-accent-strong`（出处：WizardChrome.tsx:71 的 LaunchButton、
               NoScansYet.tsx:84、ConfirmDialog.tsx:103 等）。不是自己挑的紫色 ——
               design-system §7 有实测记录：这套 token 里只有
               「accent 底 + canvas 字」能同时满足「主色醒目」与「文字可读」
               （5.98:1，hover 提亮到 accent-strong 是 8.36:1）；
               另一条被试过的写法「accent-deep 底 + ink 字」只有 4.06:1，
               hover 掉到 2.74:1，不达 AA。
            2. **去掉描边**：实心按钮再描边是两套视觉语言叠加，而且
               `border-line-strong` 在紫色底上会显出一条脏边。
            3. **"调宽"用加大内距 `px-3` → `px-5`，不用 `min-w`** ——
               这个向导里按钮宽度由内容决定是既有做法，
               `WizardChrome.tsx:56-62` 专门解释过为什么不让宽度自己动。
            4. `press` + `font-medium` 跟随既有主按钮写法：这是"推进流程"的
               关键操作，按下要有即时反馈（design-system §5.1）。

            ⚠️ 主次仍然分明：它是 `h-10` + `px-5`，而最后一步的 `LaunchButton`
            是 `h-11` + `press-lg` + 更重的分量 —— 「下一步」不该抢过「启动扫描」。
          */
          <button
            type="button"
            onClick={goNext}
            className={cn(
              'press inline-flex h-10 items-center gap-1.5 rounded-sm px-5 text-sm font-medium',
              'bg-accent text-canvas transition-ui duration-fast hover:bg-accent-strong',
            )}
          >
            下一步
            <ArrowRight size={14} strokeWidth={1.75} aria-hidden="true" />
          </button>
        ) : (
          <div className="flex flex-col items-end gap-tight">
            <LaunchButton
              disabledReason={launchDisabledReason}
              loading={launching}
              onClick={() => void handleLaunch()}
            />

            {/*
              启动失败要**留在原地**说清楚，而不是弹个 toast 飘走。
              这一跳是真实发包：403 要去勾授权、409 表示并发已满、
              网络不可达要去起后端 —— 三种的下一步动作完全不同。
            */}
            {launchError && (
              <p role="alert" className="max-w-[44ch] text-right text-2xs text-signal-risk">
                {launchError}
              </p>
            )}
          </div>
        )}
      </div>

      {/* 每一步都提示当前进度，读屏器也能感知 */}
      <p className="sr-only" aria-live="polite">
        第 {stepIndex + 1} 步，共 {STEPS.length} 步：{STEPS[stepIndex].label}
      </p>
    </div>
  )
}

/** 步骤分发。抽出来是为了让上面两个分支（有/无动效）共用同一份参数 */
interface StepBodyProps {
  index: number
  kind: TargetKind
  onKindChange: (k: TargetKind) => void
  target: string
  onTargetChange: (v: string) => void
  name: string
  onNameChange: (v: string) => void
  showErrors: boolean
  skills: Set<string>
  onToggleSkill: (id: string) => void
  providers: ReturnType<typeof useProviders>
  providerId: string | null
  onSelectProvider: (id: string) => void
  selectedSkills: typeof SKILLS
  /** 授权确认状态（契约 §5.5：第 4 步要有这个必勾项） */
  authorized: boolean
  onAuthorizedChange: (value: boolean) => void
  onJump: (i: number) => void
  // ── 任务背景与附件（任务 12）──
  brief: string
  onBriefChange: (value: string) => void
  goal: ScanGoal
  onGoalChange: (value: ScanGoal) => void
  attachments: ScanAttachment[]
  /**
   * 附件的文件名（`attachments` 的投影）。
   *
   * ★ 单独传一份而不是让子组件自己 `.map(f => f.name)`：第 1 步
   *   （目标）与第 4 步（确认）都只需要名字，而它们拿不到
   *   `ScanAttachment` 的完整形状 —— 传名字是这两步唯一真正的依赖，
   *   也避免"为了读个名字而把整个 store 类型铺进展示组件"。
   */
  attachmentNames: string[]
  onStage: (files: File[]) => void
  onRemoveAttachment: (name: string) => void
  staging: { busy: boolean; error: string | null }
}

function StepBody({
  index,
  kind,
  onKindChange,
  target,
  onTargetChange,
  name,
  onNameChange,
  showErrors,
  skills,
  onToggleSkill,
  providers,
  providerId,
  onSelectProvider,
  selectedSkills,
  authorized,
  onAuthorizedChange,
  onJump,
  brief,
  onBriefChange,
  goal,
  onGoalChange,
  attachments,
  attachmentNames,
  onStage,
  onRemoveAttachment,
  staging,
}: StepBodyProps) {
  switch (index) {
    case 0:
      return (
        <TargetStep
          kind={kind}
          onKindChange={onKindChange}
          value={target}
          onValueChange={onTargetChange}
          showError={showErrors}
          name={name}
          onNameChange={onNameChange}
          attachmentNames={attachmentNames}
        />
      )
    case 1:
      return (
        <SkillsStep selected={skills} onToggle={onToggleSkill} kind={kind} showError={showErrors} />
      )
    case 2:
      return (
        <ProviderStep providers={providers} selectedId={providerId} onSelect={onSelectProvider} />
      )
    case 3:
      return (
        <BriefStep
          brief={brief}
          onBriefChange={onBriefChange}
          goal={goal}
          onGoalChange={onGoalChange}
          attachments={attachments}
          onStage={onStage}
          onRemoveAttachment={onRemoveAttachment}
          staging={staging}
          // artifact 下附件是必填项，这一步必须能说清"为什么过不去"
          attachmentsRequired={kind === 'artifact'}
          showError={showErrors}
        />
      )
    case 4:
      return (
        <ConfirmStep
          name={name}
          target={target}
          kind={kind}
          skills={selectedSkills}
          provider={providers.find((p) => p.id === providerId) ?? null}
          authorized={authorized}
          onAuthorizedChange={onAuthorizedChange}
          onJump={onJump}
          attachmentNames={attachmentNames}
        />
      )
    default:
      return null
  }
}

export { Check }
export default NewScanWizard
