import { cn } from '@/lib/cn'
import { DangerButton, Field, TextInput, Toggle } from './Field'
import { SaveBar, SettingsPanel, type SaveState } from './SaveBar'
import {
  validateEmail,
  validateWebhookUrl,
  type NoticeRule,
  type NotifyChannel,
  type NotifyChannelKind,
} from '@/data/settings'
import {
  BACKEND_PENDING_NOTE,
  nextChannelId,
  removeNotifyChannel,
  saveNoticeRule,
  saveNotifyChannel,
  sendTestNotification,
} from '@/data/settings-store'
import { SEVERITY_META, SEVERITY_ORDER, type Severity } from '@/data/findings'
import { formatRelativeTime } from '@/lib/datetime'
import { useRevealOnExpand } from '@/hooks/useRevealOnExpand'
import { Check, Mail, Send, TriangleAlert, Webhook, X, MessageSquare } from 'lucide-react'
import { useMemo, useState } from 'react'

/**
 * 通知设置面板。
 *
 * ★ 这一页的分组是刻意的：**「发给谁」与「什么时候发」分成两块。**
 *   把 Slack 地址和"只通知严重漏洞"塞进同一个对象，就表达不了
 *   "邮件收全部、Slack 只收严重"这种真实需求。
 *   见 data/settings.ts 里 NotifyChannel 与 NoticeRule 的说明。
 *
 * ★ 通知类配置最要命的问题不是"配得难"，而是**"出事时才发现没通"**。
 *   所以每个渠道都有"发送测试"按钮，把"通知是否真的能送达"
 *   从事故时才发现提前到配置时就知道。
 */

const CHANNEL_META: Record<
  NotifyChannelKind,
  { label: string; icon: typeof Mail; placeholder: string; help: string }
> = {
  email: {
    label: '邮件',
    icon: Mail,
    placeholder: 'security@example.com',
    help: '可以填邮件列表地址，通知会发给列表里的所有人。',
  },
  webhook: {
    label: 'Webhook',
    icon: Webhook,
    placeholder: 'https://hooks.example.com/security',
    help: '以 POST 发送 JSON，含漏洞标题、严重度与目标。接收端需要支持 https。',
  },
  slack: {
    label: 'Slack',
    icon: MessageSquare,
    placeholder: 'https://hooks.slack.com/services/…',
    help: '在 Slack 里创建 Incoming Webhook 后把地址粘贴到这里。',
  },
}

export interface NotifyPanelProps {
  rule: NoticeRule
  channels: NotifyChannel[]
}

export function NotifyPanel({ rule, channels }: NotifyPanelProps) {
  return (
    <SettingsPanel
      id="notify"
      title="通知"
      description="发现问题时推送到团队正在看的地方。通知内容包含漏洞标题与目标地址，请确认渠道是可信的。"
    >
      <RuleSection rule={rule} />
      <ChannelSection channels={channels} />
    </SettingsPanel>
  )
}

// ─────────────────────────────────────────────
// 触发规则
// ─────────────────────────────────────────────

function RuleSection({ rule }: { rule: NoticeRule }) {
  const [draft, setDraft] = useState<NoticeRule>(rule)
  const [saveState, setSaveState] = useState<SaveState>({ kind: 'idle' })

  const dirty = useMemo(
    () =>
      draft.minSeverity !== rule.minSeverity ||
      draft.notifyOnScanComplete !== rule.notifyOnScanComplete ||
      draft.notifyOnCriticalImmediate !== rule.notifyOnCriticalImmediate,
    [draft, rule],
  )

  const handleSave = () => {
    if (!dirty) {
      setSaveState({ kind: 'unchanged' })
      return
    }
    setSaveState({ kind: 'saving' })
    const result = saveNoticeRule(draft)
    setSaveState(
      result.status === 'ok'
        ? { kind: 'saved', at: result.at }
        : result.status === 'failed'
          ? { kind: 'failed', error: result.error }
          : { kind: 'unchanged' },
    )
  }

  return (
    <div className="flex flex-col gap-panel">
      <div className="flex flex-col gap-2">
        <span className="text-xs font-medium text-ink">触发条件</span>

        <Field
          label="最低通知级别"
          htmlFor="notice-severity"
          help="达到或高于这一级才会推送。级别越低，打扰越多。"
        >
          <div className="flex flex-wrap gap-1">
            {SEVERITY_ORDER.map((severity) => {
              const active = draft.minSeverity === severity
              return (
                <button
                  key={severity}
                  type="button"
                  onClick={() => setDraft({ ...draft, minSeverity: severity })}
                  aria-pressed={active}
                  className={cn(
                    'inline-flex h-7 items-center gap-1.5 rounded-pill border px-2 text-2xs transition-ui duration-fast',
                    active
                      ? 'border-accent/50 bg-accent-wash text-ink'
                      : 'border-line text-ink-muted hover:border-line-strong hover:text-ink',
                  )}
                >
                  <span
                    aria-hidden="true"
                    className={cn('h-1.5 w-1.5 rounded-pill', SEVERITY_META[severity].bg)}
                  />
                  {SEVERITY_META[severity].label}
                </button>
              )
            })}
          </div>
        </Field>

        {/* 当前阈值的人话复述 —— 让用户确认自己选对了 */}
        <p className="text-2xs text-ink-faint">
          当前会在发现
          <span className="text-ink">
            「{SEVERITY_META[draft.minSeverity as Severity].label}」及以上{' '}
          </span>
          的问题时通知。
        </p>

        <Toggle
          id="notice-complete"
          label="扫描结束时通知"
          hint="即使没有发现问题也发一条，用于确认扫描确实跑完了。"
          checked={draft.notifyOnScanComplete}
          onChange={(v) => setDraft({ ...draft, notifyOnScanComplete: v })}
        />

        <Toggle
          id="notice-critical"
          label="发现严重问题时立即通知"
          hint="不等扫描结束，边跑边发 —— 早一分钟知道就少一分钟暴露窗口。"
          checked={draft.notifyOnCriticalImmediate}
          onChange={(v) => setDraft({ ...draft, notifyOnCriticalImmediate: v })}
        />
      </div>

      <SaveBar
        state={saveState}
        dirty={dirty}
        onSave={handleSave}
        onReset={() => {
          setDraft(rule)
          setSaveState({ kind: 'idle' })
        }}
        note={<span className="text-2xs text-ink-muted">{BACKEND_PENDING_NOTE}</span>}
      />
    </div>
  )
}

// ─────────────────────────────────────────────
// 渠道列表
// ─────────────────────────────────────────────

function ChannelSection({ channels }: { channels: NotifyChannel[] }) {
  const [adding, setAdding] = useState<NotifyChannelKind | null>(null)

  return (
    <div className="flex flex-col gap-2 border-t border-line pt-panel">
      <div className="flex flex-wrap items-center justify-between gap-tight">
        <span className="text-xs font-medium text-ink">通知渠道</span>
        {adding === null && (
          <div className="flex gap-1">
            {(Object.keys(CHANNEL_META) as NotifyChannelKind[]).map((kind) => {
              const Icon = CHANNEL_META[kind].icon
              return (
                <button
                  key={kind}
                  type="button"
                  onClick={() => setAdding(kind)}
                  className="inline-flex h-7 items-center gap-1 rounded-sm border border-line px-2 text-2xs text-ink-muted transition-ui duration-fast hover:border-accent/50 hover:bg-accent-wash hover:text-ink"
                >
                  {/* 用渠道自己的图标而不是统一加号：三种渠道一眼可分 */}
                  <Icon size={11} strokeWidth={1.75} aria-hidden="true" />
                  {CHANNEL_META[kind].label}
                </button>
              )
            })}
          </div>
        )}
      </div>

      {channels.length === 0 && adding === null && (
        <p className="rounded-sm border border-dashed border-line-strong p-3 text-center text-2xs text-ink-faint">
          还没有配置任何渠道。不配也能用，只是发现问题时没有人会收到通知。
        </p>
      )}

      <div className="flex flex-col gap-2">
        {channels.map((channel) => (
          <ChannelRow key={channel.id} channel={channel} />
        ))}

        {adding !== null && (
          <ChannelEditor
            channel={{
              id: nextChannelId(adding),
              kind: adding,
              label: '',
              target: '',
              enabled: true,
              verified: false,
              verifyError: null,
            }}
            isNew
            onDone={() => setAdding(null)}
          />
        )}
      </div>
    </div>
  )
}

function ChannelRow({ channel }: { channel: NotifyChannel }) {
  const [editing, setEditing] = useState(false)
  const meta = CHANNEL_META[channel.kind]
  const Icon = meta.icon

  if (editing) {
    return <ChannelEditor channel={channel} onDone={() => setEditing(false)} />
  }

  return (
    <div className="flex flex-wrap items-center gap-x-tight gap-y-2 rounded-sm border border-line p-3">
      <span
        className={cn(
          'flex h-7 w-7 shrink-0 items-center justify-center rounded-pill',
          channel.verified
            ? 'bg-signal-ok/15 text-signal-ok'
            : 'bg-signal-warn/15 text-signal-warn',
        )}
        title={channel.verified ? '已通过测试' : (channel.verifyError ?? '尚未测试')}
      >
        <Icon size={13} strokeWidth={1.75} aria-hidden="true" />
      </span>

      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="flex flex-wrap items-center gap-1.5">
          <span className="text-sm text-ink">{channel.label || meta.label}</span>
          <span className="rounded-xs border border-line px-1 text-2xs text-ink-faint">
            {meta.label}
          </span>
          {!channel.enabled && <span className="text-2xs text-ink-faint">已停用</span>}
        </span>
        <span className="flex flex-wrap items-center gap-x-2 text-2xs">
          <span className="mono-ui truncate text-ink-faint">{channel.target}</span>
          {!channel.verified && (
            <span className="text-signal-warn">{channel.verifyError ?? '尚未测试'}</span>
          )}
        </span>
      </div>

      <button
        type="button"
        onClick={() => setEditing(true)}
        className="inline-flex h-7 shrink-0 items-center rounded-sm px-2 text-2xs text-ink-muted transition-ui duration-fast hover:bg-surface-raised hover:text-ink"
      >
        编辑
      </button>
    </div>
  )
}

function ChannelEditor({
  channel,
  isNew = false,
  onDone,
}: {
  channel: NotifyChannel
  isNew?: boolean
  onDone: () => void
}) {
  const [draft, setDraft] = useState<NotifyChannel>(channel)
  const [touched, setTouched] = useState(false)
  const [saveState, setSaveState] = useState<SaveState>({ kind: 'idle' })
  /*
    ⚠️ 这里**没有** `testing` 状态 —— 与"发送测试消息"按钮被禁用的是同一个原因
    （后端未交付，见 settings-store 的 `sendTestNotification`）。
    后端交付后要把它加回来：真实请求有往返时间，按钮需要"正在发送…"这一态，
    否则用户会以为点了没反应而反复点。
  */
  const [testState, setTestState] = useState<SaveState>({ kind: 'idle' })
  /*
    编辑器只要挂载就是"展开"状态 —— 它由父组件按需渲染。
    所以这里传常量 true：挂载即带进视野。
    同样的问题在 provider 编辑器上实测过（保存按钮落在折叠线以下 67px）。
  */
  const editorRef = useRevealOnExpand<HTMLDivElement>(true)

  const meta = CHANNEL_META[draft.kind]

  const errors = useMemo(() => {
    const target =
      draft.kind === 'email' ? validateEmail(draft.target) : validateWebhookUrl(draft.target)
    const label = draft.label.trim().length === 0 ? '请填写名称（用于区分多个渠道）' : null
    return {
      target: target.ok ? null : target.error,
      label,
    }
  }, [draft])

  const hasError = Object.values(errors).some((e) => e !== null)

  const dirty =
    isNew ||
    draft.label !== channel.label ||
    draft.target !== channel.target ||
    draft.enabled !== channel.enabled

  const handleSave = () => {
    setTouched(true)
    if (hasError) {
      setSaveState({ kind: 'failed', error: '有字段未通过校验，请先修正' })
      return
    }
    if (!dirty) {
      setSaveState({ kind: 'unchanged' })
      return
    }

    setSaveState({ kind: 'saving' })
    const result = saveNotifyChannel(draft)
    if (result.status === 'ok') {
      setSaveState({ kind: 'saved', at: result.at })
      setTouched(false)
      if (isNew) onDone()
    } else if (result.status === 'failed') {
      setSaveState({ kind: 'failed', error: result.error })
    } else {
      setSaveState({ kind: 'unchanged' })
    }
  }

  /**
   * 发送测试消息。
   *
   * ★ 按钮现在是**禁用**的（后端未交付，见 `sendTestNotification`），
   *   所以这个函数暂时不会被调到。它保留下来是因为后端交付后
   *   只需要去掉按钮上那个 `disabled` —— 先保存再测试、
   *   失败态渲染这些逻辑都是现成的。
   *
   * 先保存再测试 —— 否则测的是没保存的临时值，
   * 用户会以为"测通了就是配好了"，实际 store 里还是旧的。
   */
  const handleTest = async () => {
    setTouched(true)
    if (hasError) {
      setTestState({ kind: 'failed', error: '请先修正标红的字段' })
      return
    }

    const saved = saveNotifyChannel(draft)
    if (saved.status === 'failed') {
      setTestState({ kind: 'failed', error: saved.error })
      return
    }

    setTestState({ kind: 'saving' })
    try {
      const result = await sendTestNotification(draft)
      setTestState(
        result.status === 'ok'
          ? { kind: 'saved', at: result.at }
          : { kind: 'failed', error: result.error },
      )
    } catch {
      setTestState({ kind: 'failed', error: '测试请求失败，请检查网络后重试' })
    }
  }

  const handleDelete = () => {
    const result = removeNotifyChannel(channel.id)
    if (result.status === 'failed') {
      setSaveState({ kind: 'failed', error: result.error })
      return
    }
    onDone()
  }

  return (
    <div
      ref={editorRef}
      className={cn(
        'flex flex-col gap-panel rounded-sm border p-3',
        isNew ? 'border-accent/40 bg-accent-wash/40' : 'border-line',
      )}
    >
      <div className="flex items-center justify-between gap-tight">
        <span className="text-xs text-ink">
          {isNew ? `新增 · ${meta.label}` : `编辑 · ${meta.label}`}
        </span>
        <button
          type="button"
          onClick={onDone}
          aria-label="收起"
          className="rounded-sm p-1 text-ink-faint transition-ui duration-fast hover:text-ink"
        >
          <X size={13} strokeWidth={2} aria-hidden="true" />
        </button>
      </div>

      <div className="grid gap-panel sm:grid-cols-2">
        <Field
          label="名称"
          htmlFor={`${channel.id}-label`}
          required
          error={touched ? errors.label : null}
        >
          <TextInput
            id={`${channel.id}-label`}
            value={draft.label}
            onChange={(v) => setDraft({ ...draft, label: v })}
            placeholder="如：安全组邮件列表"
            error={touched ? errors.label : null}
          />
        </Field>

        <Field
          label={draft.kind === 'email' ? '收件地址' : 'Webhook 地址'}
          htmlFor={`${channel.id}-target`}
          required
          help={meta.help}
          error={touched ? errors.target : null}
        >
          <TextInput
            id={`${channel.id}-target`}
            value={draft.target}
            onChange={(v) => setDraft({ ...draft, target: v })}
            placeholder={meta.placeholder}
            mono={draft.kind !== 'email'}
            error={touched ? errors.target : null}
          />
        </Field>
      </div>

      <Toggle
        id={`${channel.id}-enabled`}
        label="启用这个渠道"
        hint="停用后保留配置但不再发送，方便临时静音。"
        checked={draft.enabled}
        onChange={(v) => setDraft({ ...draft, enabled: v })}
      />

      <div className="flex flex-col gap-tight border-t border-line pt-panel">
        <div className="flex flex-wrap items-center gap-tight">
          {/*
            ★ **按钮禁用，而不是让它假装发了一次。**

            这里原来把结果渲染成绿色的「测试消息已送达 · 刚刚」，
            而实际上一个字节都没发出去（见 settings-store 的
            `sendTestNotification`）。通知是**告警通道** ——
            给用户一个假的「已送达」，等于让他带着一条从未验证过的通道上线，
            而且他会因为见过那句绿字而**更加确信它没问题**。
            这比"没有这个功能"严重得多。

            后端交付 `POST /settings/notify/test` 之后：去掉 `disabled` 即可，
            `handleTest` 与下面两段反馈都是现成的（保留着就是为了那一天）。
          */}
          <button
            type="button"
            onClick={handleTest}
            disabled
            title="后端接口尚未交付，当前无法真实发送测试消息"
            className="inline-flex h-9 cursor-not-allowed items-center gap-1.5 rounded-sm border border-line/50 px-3 text-xs text-ink-faint"
          >
            <Send size={12} strokeWidth={2} aria-hidden="true" />
            发送测试消息
          </button>

          <span className="text-2xs text-ink-faint">后端未交付，暂时无法验证它是否可达</span>

          {testState.kind === 'saved' && (
            <span role="status" className="inline-flex items-center gap-1 text-2xs text-signal-ok">
              <Check size={11} strokeWidth={2.5} aria-hidden="true" />
              测试消息已送达 · {formatRelativeTime(testState.at)}
            </span>
          )}
          {testState.kind === 'failed' && (
            <span role="alert" className="inline-flex items-center gap-1 text-2xs text-signal-risk">
              <TriangleAlert size={11} strokeWidth={2} aria-hidden="true" />
              {testState.error}
            </span>
          )}
        </div>

        <SaveBar
          state={saveState}
          dirty={dirty}
          onSave={handleSave}
          onReset={() => {
            setDraft(channel)
            setTouched(false)
            setSaveState({ kind: 'idle' })
          }}
          saveLabel={isNew ? '添加渠道' : '保存'}
          note={
            <span className="flex flex-col gap-tight">
              <span className="text-2xs text-ink-muted">{BACKEND_PENDING_NOTE}</span>
              {!isNew && (
                <span className="flex flex-wrap items-center gap-tight">
                  <span>删除后不会再收到这个渠道的通知。</span>
                  <DangerButton onClick={handleDelete}>删除渠道</DangerButton>
                </span>
              )}
            </span>
          }
        />
      </div>
    </div>
  )
}

export default NotifyPanel
