import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Bot,
  Check,
  ChevronDown,
  FileText,
  FolderPlus,
  Loader2,
  Paperclip,
  Shield,
  ShieldCheck,
  Sparkles,
  X,
} from 'lucide-react'
import { cn } from '@/lib/cn'
import { Button } from '@/components/ui/Button'
import { Modal } from '@/components/ui/Modal'
import type { ChatAttachment, ChatPersona } from '@/data/chat'

/**
 * 对话页的设置控件（v0.94）。
 *
 * 拆成独立文件而不是塞进 `ChatPage.tsx`：那个文件已经 520 行，
 * 再加四组控件会到 800+ 行，而它们的关注点完全不同（页面管布局与
 * 消息流，这里管"这一轮按什么设置跑"）。
 *
 * ## 共同的取舍：每次改动**立刻提交**，没有"保存"按钮
 *
 * 这些设置都很小（一个名字、一个 id、一个布尔），而用户改它们的动作
 * 本身就是"我希望现在这样"。加一个保存按钮会造出两种状态
 * （已保存 / 未保存），而用户分不清"我选了但没生效"和"选了但模型没用"。
 *
 * ⚠️ 例外是**人格选择**：它走弹窗，点卡片即生效并关闭 —— 那也是
 *    "立刻提交"，只是多一次点击来确认意图。
 */

// ══════════════════════════════════════════════════════════════
// 人格弹窗
// ══════════════════════════════════════════════════════════════

/**
 * 选人格的弹窗（学 CyberStrikeAI 的卡片式选择）。
 *
 * ## 为什么用弹窗而不是下拉
 *
 * 七种人格每种的 `summary` 是一句话，需要横向空间才能读。下拉里
 * 放不下，只能显示 label —— 于是用户只能靠名字猜"信息收集"和
 * "渗透测试"差在哪。卡片能把那句话摊开，让选择发生在理解了之后。
 */
export function PersonaDialog({
  open,
  personas,
  current,
  onPick,
  onClose,
}: {
  open: boolean
  personas: ChatPersona[]
  /** 当前人格 id。`null` = 默认。 */
  current: string | null
  onPick: (personaId: string | null) => void
  onClose: () => void
}) {
  return (
    <Modal open={open} onClose={onClose} title="选一个角色" widthClass="max-w-3xl">
      <p className="mb-3 text-xs text-ink-muted">
        角色只改变它**关注什么、怎么说**，不会改变它能做什么 ——
        能调哪些工具由你的权限决定，与选谁无关。
      </p>
      <ul className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
        {personas.map((persona) => {
          // `null` 与 `'default'` 是同一件事（见后端 `update_settings`
          // 那条注释），所以两者都算"选中"。
          const isActive =
            persona.id === 'default'
              ? current === null || current === 'default'
              : current === persona.id
          return (
            <li key={persona.id}>
              <button
                type="button"
                onClick={() => {
                  // 默认人格提交 `null` 而不是 `'default'`：两者落库等价，
                  // 但 `null` 是"没有特别指定"这个语义的准确表达。
                  onPick(persona.id === 'default' ? null : persona.id)
                }}
                className={cn(
                  'flex w-full flex-col items-start gap-0.5 rounded-sm border px-2.5 py-2 text-left',
                  'transition-ui duration-fast',
                  'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
                  isActive
                    ? 'border-accent/60 bg-accent/5'
                    : 'border-line bg-surface-sunken hover:border-line-strong',
                )}
                aria-pressed={isActive}
              >
                <span className="flex w-full items-center gap-1">
                  <span className="text-xs font-medium text-ink">{persona.label}</span>
                  {isActive && (
                    <Check size={12} strokeWidth={2.5} className="ml-auto text-accent" aria-hidden="true" />
                  )}
                </span>
                <span className="text-2xs leading-relaxed text-ink-faint">{persona.summary}</span>
              </button>
            </li>
          )
        })}
      </ul>
    </Modal>
  )
}

// ══════════════════════════════════════════════════════════════
// 模型接入点下拉
// ══════════════════════════════════════════════════════════════

/**
 * 选模型接入点的下拉。
 *
 * ## 为什么用一个原生 `<select>` 而不是自绘下拉
 *
 * 行为上它就是一个单选：一个 id。原生控件免费带来键盘导航、
 * 输入法、读屏器支持、以及"点外面关掉"。自绘一个要在三处
 * 处理焦点，而收益只是外观。本项目的设计纪律是"能借就不自己写"。
 *
 * ⚠️ 但外观还是统一处理的（`appearance-none` + 自定义箭头）：
 *   各浏览器的原生 select 长得不一样，直接放着会破坏这一页的密度。
 */
export function ProviderSelect({
  providers,
  value,
  onChange,
  disabled,
}: {
  providers: Array<{ id: string; label: string }>
  /** `null` = 用平台默认。 */
  value: string | null
  onChange: (providerId: string | null) => void
  disabled?: boolean
}) {
  return (
    <label className="relative flex items-center gap-1">
      <span className="sr-only">模型接入点</span>
      <Bot size={12} strokeWidth={2} className="shrink-0 text-ink-faint" aria-hidden="true" />
      <select
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value || null)}
        disabled={disabled}
        className={cn(
          'appearance-none rounded-xs border border-line bg-surface-sunken py-1 pl-1.5 pr-5',
          'text-2xs text-ink-muted transition-ui duration-fast',
          'hover:border-line-strong focus:outline-none focus:ring-2 focus:ring-accent/40',
          'disabled:cursor-not-allowed disabled:opacity-50',
        )}
      >
        <option value="">平台默认模型</option>
        {providers.map((provider) => (
          <option key={provider.id} value={provider.id}>
            {provider.label}
          </option>
        ))}
      </select>
      <ChevronDown
        size={10}
        strokeWidth={2}
        className="pointer-events-none absolute right-1.5 text-ink-faint"
        aria-hidden="true"
      />
    </label>
  )
}

// ══════════════════════════════════════════════════════════════
// 技能 / MCP 多选
// ══════════════════════════════════════════════════════════════

/**
 * 多选下拉（技能 / MCP server 共用）。
 *
 * ## 为什么两个共用一个组件
 *
 * 它们的行为完全一样：一组名字，勾几个，存成 `string[]`。差别只在
 * label 与空状态文案。写两个会让"点外面关掉"这类行为各实现一遍，
 * 而它们总有一处会写错。
 *
 * ## 为什么自己实现而不装库
 *
 * 需求是"一个能多选的浮层"。装一个多选库（react-select 之类）会引入
 * 它自己的样式体系、受控状态约定、以及一份比这个文件更长的 polyfill。
 * 这里的实现只有"开/关 + 勾选 + 点外面关"，40 行，可控。
 */
export function MultiSelect({
  label,
  icon,
  options,
  selected,
  onChange,
  emptyHint,
  disabled,
}: {
  label: string
  icon: React.ReactNode
  options: string[]
  selected: string[]
  onChange: (names: string[]) => void
  emptyHint: string
  disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const boxRef = useRef<HTMLDivElement | null>(null)

  // 点外面 / Esc 关掉。用 `pointerdown` 而不是 `click`：click 在
  // 触摸设备上要等 300ms，而浮层已经画出来了，用户会觉得它卡了一下。
  useEffect(() => {
    if (!open) return
    function onDown(event: PointerEvent) {
      if (!boxRef.current?.contains(event.target as Node)) setOpen(false)
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const count = selected.length
  return (
    <div ref={boxRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={disabled}
        className={cn(
          'flex items-center gap-1 rounded-xs border px-1.5 py-1 text-2xs',
          'transition-ui duration-fast',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
          'disabled:cursor-not-allowed disabled:opacity-50',
          count > 0
            ? 'border-accent/50 bg-accent/5 text-ink'
            : 'border-line bg-surface-sunken text-ink-muted hover:border-line-strong',
        )}
        aria-expanded={open}
        aria-haspopup="listbox"
      >
        <span className="text-ink-faint" aria-hidden="true">
          {icon}
        </span>
        {label}
        {count > 0 && <span className="text-ink-faint">· {count}</span>}
        <ChevronDown size={10} strokeWidth={2} aria-hidden="true" />
      </button>

      {open && (
        <div
          role="listbox"
          aria-multiselectable="true"
          className={cn(
            'absolute left-0 top-full z-20 mt-1 max-h-64 w-56 overflow-y-auto',
            'rounded-sm border border-line bg-surface-raised p-1 shadow-lg',
          )}
        >
          {options.length === 0 ? (
            <p className="px-2 py-3 text-2xs text-ink-faint">{emptyHint}</p>
          ) : (
            options.map((name) => {
              const checked = selected.includes(name)
              return (
                <label
                  key={name}
                  className={cn(
                    'flex cursor-pointer items-center gap-1.5 rounded-xs px-1.5 py-1',
                    'text-2xs text-ink-muted transition-ui duration-fast hover:bg-surface-sunken',
                  )}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => {
                      // ⚠️ 顺序要保持稳定：后端把 `skillNames` 当成有序列表
                      //   （挂载预算按挂载顺序截断），所以新勾的**追加到尾部**、
                      //   取消的从中间摘掉，不排序。
                      onChange(
                        checked ? selected.filter((n) => n !== name) : [...selected, name],
                      )
                    }}
                    className="size-3 accent-[var(--accent)]"
                  />
                  <span className="truncate">{name}</span>
                </label>
              )
            })
          )}
        </div>
      )}
    </div>
  )
}

// ══════════════════════════════════════════════════════════════
// 项目输入
// ══════════════════════════════════════════════════════════════

/**
 * 归属项目（一个自由文本 + 已有项目建议）。
 *
 * ## 为什么是自由文本而不是受管理的项目实体
 *
 * 平台里没有 "project" 这张表 —— 项目只是会话上的一个字符串
 * （`ChatSession.project`）。做成受管理实体要加表、加 CRUD、
 * 加"删项目时那些会话去哪"的规则，而用户现在要的只是
 * **侧栏能分组**。等真的需要共享/权限/归档时再升级。
 *
 * ★ 建议列表从**已有会话**里取（去重），所以它自动反映用户
 *   实际用过的名字，不需要额外维护。
 */
export function ProjectInput({
  value,
  suggestions,
  onCommit,
  disabled,
}: {
  value: string | null
  suggestions: string[]
  onCommit: (project: string | null) => void
  disabled?: boolean
}) {
  // ⚠️ 本地草稿 + 失焦提交，而不是每次按键提交：
  //   每次按键都发一个 PATCH，会把用户打字过程逐字符写进库，
  //   而且网络慢时响应乱序会让最终值不可预测。
  const [draft, setDraft] = useState(value ?? '')
  const [editing, setEditing] = useState(false)

  // 外部值变了（切会话）时同步草稿。只在**没在编辑**时同步 ——
  // 正在打字时被覆盖会让人以为自己打的字消失了。
  useEffect(() => {
    if (!editing) setDraft(value ?? '')
  }, [value, editing])

  const listId = useMemo(() => `projects-${Math.random().toString(36).slice(2)}`, [])

  if (!editing) {
    return (
      <button
        type="button"
        onClick={() => setEditing(true)}
        disabled={disabled}
        className={cn(
          'flex items-center gap-1 rounded-xs border border-line bg-surface-sunken px-1.5 py-1',
          'text-2xs text-ink-muted transition-ui duration-fast hover:border-line-strong',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
          'disabled:cursor-not-allowed disabled:opacity-50',
          value ? 'text-ink' : '',
        )}
        title={value ? `项目：${value}` : '归到一个项目里（侧栏会分组）'}
      >
        <FolderPlus size={12} strokeWidth={2} className="text-ink-faint" aria-hidden="true" />
        <span className="max-w-24 truncate">{value || '无项目'}</span>
      </button>
    )
  }

  return (
    <>
      <input
        autoFocus
        value={draft}
        list={listId}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          setEditing(false)
          const cleaned = draft.trim()
          onCommit(cleaned || null)
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            e.currentTarget.blur()
          }
          if (e.key === 'Escape') {
            setDraft(value ?? '')
            // ⚠️ 先改草稿再退出编辑：直接 `setEditing(false)` 会让
            //   `useEffect` 里的同步逻辑读到**旧的** `value`，于是
            //   取消之后框里显示的仍是刚敲的字。
            setEditing(false)
          }
        }}
        placeholder="项目名"
        aria-label="归属项目"
        className={cn(
          'w-28 rounded-xs border border-accent/60 bg-surface-sunken px-1.5 py-1',
          'text-2xs text-ink placeholder:text-ink-faint',
          'focus:outline-none focus:ring-2 focus:ring-accent/40',
        )}
      />
      <datalist id={listId}>
        {suggestions.map((name) => (
          <option key={name} value={name} />
        ))}
      </datalist>
    </>
  )
}

// ══════════════════════════════════════════════════════════════
// 附件条
// ══════════════════════════════════════════════════════════════

/**
 * 输入框上方的附件条：已上传的 + 上传按钮。
 *
 * ## 为什么附件条在输入框**上方**而不是下方
 *
 * 下方会与发送按钮抢位置，而且消息流是往上滚的 —— 附件条在下方
 * 会让它看起来像是"上一条消息的一部分"。放在输入框正上方，
 * 视觉上属于"我要发的东西"。
 */
export function AttachmentBar({
  attachments,
  uploading,
  onPick,
  onRemove,
  disabled,
}: {
  attachments: ChatAttachment[]
  uploading: boolean
  onPick: (file: File) => void
  onRemove: (id: string) => void
  disabled?: boolean
}) {
  const inputRef = useRef<HTMLInputElement | null>(null)

  return (
    <div className="mx-auto flex w-full max-w-[70ch] flex-wrap items-center gap-1 pb-1.5">
      <input
        ref={inputRef}
        type="file"
        className="sr-only"
        onChange={(e) => {
          const file = e.target.files?.[0]
          if (file) onPick(file)
          // ★ 清空 value：不清的话选同一个文件第二次不会触发 onChange
          //   （值没变）。用户会以为"点了没反应"。
          e.target.value = ''
        }}
        aria-label="选择附件"
      />
      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        disabled={disabled || uploading}
        className={cn(
          'flex items-center gap-1 rounded-xs border border-line bg-surface-sunken px-1.5 py-1',
          'text-2xs text-ink-muted transition-ui duration-fast hover:border-line-strong',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
          'disabled:cursor-not-allowed disabled:opacity-50',
        )}
        title="传一个文件给它看（文本类，单个上限 5 MB）"
      >
        {uploading ? (
          <Loader2 size={12} strokeWidth={2} className="animate-spin" aria-hidden="true" />
        ) : (
          <Paperclip size={12} strokeWidth={2} aria-hidden="true" />
        )}
        {uploading ? '上传中…' : '附件'}
      </button>

      {attachments.map((attachment) => (
        <span
          key={attachment.id}
          className={cn(
            'flex items-center gap-1 rounded-xs border border-line bg-surface-sunken',
            'py-1 pl-1.5 pr-0.5 text-2xs text-ink-muted',
          )}
        >
          <FileText size={11} strokeWidth={2} className="text-ink-faint" aria-hidden="true" />
          <span className="max-w-40 truncate" title={attachment.name}>
            {attachment.name}
          </span>
          <span className="text-ink-faint">{Math.max(1, Math.round(attachment.sizeBytes / 1024))}K</span>
          <button
            type="button"
            onClick={() => onRemove(attachment.id)}
            className={cn(
              'rounded-xs p-0.5 text-ink-faint transition-ui duration-fast',
              'hover:text-signal-risk focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
            )}
            aria-label={`移除附件 ${attachment.name}`}
          >
            <X size={10} strokeWidth={2.5} aria-hidden="true" />
          </button>
        </span>
      ))}
    </div>
  )
}

// ══════════════════════════════════════════════════════════════
// 审批开关
// ══════════════════════════════════════════════════════════════

/**
 * 「有副作用的操作要我确认」开关。
 *
 * ★ 打开时图标换成实心盾：这两个状态在视觉上必须**一眼可分**，
 *   用同一个图标只改颜色会让用户以为状态没变（颜色在小尺寸下不显著）。
 */
export function ApprovalToggle({
  enabled,
  onChange,
  disabled,
}: {
  enabled: boolean
  onChange: (enabled: boolean) => void
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!enabled)}
      disabled={disabled}
      role="switch"
      aria-checked={enabled}
      className={cn(
        'flex items-center gap-1 rounded-xs border px-1.5 py-1 text-2xs',
        'transition-ui duration-fast',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
        'disabled:cursor-not-allowed disabled:opacity-50',
        enabled
          ? 'border-accent/50 bg-accent/5 text-ink'
          : 'border-line bg-surface-sunken text-ink-muted hover:border-line-strong',
      )}
      title={
        enabled
          ? '有副作用的操作（建扫描、改技能库、加 MCP）会先停下来等你确认'
          : '它可以直接执行有副作用的操作，不再问你'
      }
    >
      {enabled ? (
        <ShieldCheck size={12} strokeWidth={2} aria-hidden="true" />
      ) : (
        <Shield size={12} strokeWidth={2} aria-hidden="true" />
      )}
      {enabled ? '执行前确认' : '直接执行'}
    </button>
  )
}

// ══════════════════════════════════════════════════════════════
// 消息上的审批卡
// ══════════════════════════════════════════════════════════════

/**
 * 一条等待确认的工具调用。
 *
 * ## 为什么要显示参数全文（而不是只显示工具名）
 *
 * 用户要判断的是"这条命令该不该跑"。只说"它想调 `run_shell`" 等于
 * 什么都没说 —— `run_shell` 可以跑 `ls` 也可以跑 `rm -rf`。
 * 参数是判断的唯一依据，所以 `toolArgs` 要原样铺开。
 */
export function ApprovalCard({
  message,
  onDecide,
  disabled,
}: {
  message: { toolName?: string | null; toolArgs?: Record<string, unknown> | null; content: string }
  onDecide: (approve: boolean) => void
  disabled?: boolean
}) {
  const args = message.toolArgs
  return (
    <div className="flex flex-col gap-1.5 rounded-sm border border-accent/40 bg-accent/5 px-2.5 py-2">
      <div className="flex items-center gap-1.5">
        <Sparkles size={12} strokeWidth={2} className="text-accent" aria-hidden="true" />
        <span className="text-xs font-medium text-ink">要执行一个操作，等你确认</span>
      </div>
      <div className="text-2xs text-ink-muted">
        <span className="text-ink-faint">工具：</span>
        <code className="font-mono text-ink">{message.toolName ?? '（未知）'}</code>
      </div>
      {args && Object.keys(args).length > 0 && (
        <pre
          className={cn(
            'max-h-40 overflow-auto rounded-xs border border-line bg-surface-sunken',
            'px-2 py-1.5 text-2xs leading-relaxed text-ink-muted',
          )}
        >
          {JSON.stringify(args, null, 2)}
        </pre>
      )}
      <div className="flex items-center gap-1.5">
        <Button size="sm" variant="primary" onClick={() => onDecide(true)} disabled={disabled}>
          执行
        </Button>
        <Button size="sm" variant="secondary" onClick={() => onDecide(false)} disabled={disabled}>
          拒绝
        </Button>
        {disabled && <span className="text-2xs text-ink-faint">处理中…</span>}
      </div>
    </div>
  )
}
