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
  createNotifyChannel,
  removeNotifyChannel,
  saveNoticeRule,
  sendTestNotification,
  updateNotifyChannel,
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
 *   每个渠道上那个"发送测试"按钮存在的全部理由，就是把这个问题
 *   从**事故时**提前到**配置时**。
 *
 *   ⚠️ 这个目标目前只走到一半：v0.22 交付了测试端点，但它只做地址形状的
 *      本地校验，**不真的往外发**（真实发送要 SMTP / Webhook / Slack 三套
 *      协议实现）。所以按钮现在能回答的是"这条地址填对了吗"，
 *      而不是"出事时我收得到吗"。界面上必须如实说明这一点
 *      （见 `handleTest` 与按钮旁那句提示）—— 把"形状对"读成"通道通"，
 *      正是这个面板最该防住的那种误解。
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

  const handleSave = async () => {
    if (!dirty) {
      setSaveState({ kind: 'unchanged' })
      return
    }

    /*
      ★ `saving` 必须在 `await` **之前**落下，其余三态都在之后。

      顺序反了的话，请求往返期间界面上没有任何"进行中"的痕迹：
      按钮不禁用、文案还是"保存"，用户会以为点了没反应而反复点 ——
      而这恰好与 `SaveBar` 把 saving 拿去禁用按钮的设计意图相反。
    */
    setSaveState({ kind: 'saving' })

    const result = await saveNoticeRule(draft)
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
        /*
          ⚠️ 这里原来挂着 `BACKEND_PENDING_NOTE`（"后端接口尚未交付，刷新回到初始值"）。
          通知已在 v0.22 交付，规则真的写进 `PUT /settings/notices/rule` ——
          再留着那句话就是**否认一个已经可靠的功能**，与吹嘘没有的能力
          是同一种失真，只是方向相反（`BACKEND_PENDING` 那段注释里写过这条原则）。
          所以整条 note 去掉，而不是换成一句别的什么。
        */
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
            /*
              ⚠️ 这个 `id` 只是个**占位**，不会被发给服务端。

              新建走 `POST /settings/notices/channels`，主键由**服务端**分配
              （`ntc_` 前缀）—— 让客户端指定主键等于让它决定"覆盖哪一条"。
              所以这里不再有 `nextChannelId()` 那种前端编号。

              但占位值不能是空串：`id` 被拿去拼 DOM 元素的 id
              （`${channel.id}-label` 等，供 `htmlFor` 与输入框关联），
              空串会得到 `-label` 这种合法但读不出所以然的 id。
            */
            channel={{
              id: 'new-channel',
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
    ★ 测试状态复用 `SaveState` 的 `saving` 那一态来表示"正在发送"，
    不另外加一个 `testing` 布尔 —— 那会变成两个 state 描述同一件事，
    而它们迟早会有一次对不上（`handleTest` 里推着它走的本来就是
    `setTestState`）。

    ⚠️ 这一态必须真的有：按钮禁用期间它无关紧要，但按钮现在可点了，
    请求有往返时间，缺了它用户会以为点了没反应而反复点。
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

  const testing = testState.kind === 'saving'

  /**
   * 把草稿落库。
   *
   * ★ 新建与改已有是**两个端点**，这里合并成一个函数，因为调用方
   *   （保存、发送测试）要的是同一个问题："草稿现在存住了没有"。
   *
   * ⚠️ 两者的返回值都是 `{channel} | {error}`，**不是 `SaveResult`** ——
   *    所以调用点判的是 `'error' in result`，不是 `result.status === 'failed'`。
   *    这个形状差别的来源：创建/更新是**资源写入**，服务端要把归一后的
   *    整条资源回传（改 `target` 会把 `verified` 打回 `false`，
   *    所以响应里的 `verified` 可能与提交的不同 —— 一律以响应为准）。
   *
   * ⚠️ 新建时**不传 id**（类型上就 `Omit` 掉了），服务端分配 `ntc_` 前缀。
   *    也**不传 `verified` / `verifyError`**：那是服务端的事实，不是用户的声明。
   */
  const persist = async (): Promise<{ channel: NotifyChannel } | { error: string }> => {
    if (isNew) {
      return createNotifyChannel({
        kind: draft.kind,
        label: draft.label,
        target: draft.target,
        enabled: draft.enabled,
      })
    }

    /*
      只提交**改动的那几项** —— 后端这个端点是 PATCH（唯一允许部分更新的
      配置端点）。全量回传会把没动过的字段也写一遍，而 `target` 那一项
      在后端有副作用：写它就等于声明"地址变了"，`verified` 会被打回 `false`。
      也就是说，全量回传会让"只改了个名字"顺手把验证状态清掉。
    */
    const patch: Partial<Pick<NotifyChannel, 'label' | 'target' | 'enabled'>> = {}
    if (draft.label !== channel.label) patch.label = draft.label
    if (draft.target !== channel.target) patch.target = draft.target
    if (draft.enabled !== channel.enabled) patch.enabled = draft.enabled

    return updateNotifyChannel(channel.id, patch)
  }

  const handleSave = async () => {
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
    const result = await persist()
    if ('error' in result) {
      setSaveState({ kind: 'failed', error: result.error })
      return
    }

    // 时刻在本地取：创建/更新的响应里没有 `at`（它回传的是整条渠道）
    setSaveState({ kind: 'saved', at: new Date().toISOString() })
    setTouched(false)

    if (isNew) {
      onDone()
      return
    }

    /*
      以服务端返回的为准，而不是"提交成功就当草稿等于基准"。
      改 `target` 会把 `verified` 打回 `false`，服务端也可能对值做归一 ——
      不同步的话草稿与基准就分叉了，界面上会一直亮着"有未保存的改动"，
      而用户找不到自己还能改什么。
    */
    setDraft(result.channel)
  }

  /**
   * 发送测试消息。
   *
   * ★ 先保存再测试 —— 否则测的是没保存的临时值，
   *   用户会以为"测通了就是配好了"，实际 store 里还是旧的。
   *
   * ⚠️ **不要把结果包装成成功。** 后端 v0.22 交付了这个端点，但它
   *    **只做地址形状的本地校验、不真的往外发**，所以它回的永远是
   *    `verified: false` + 一句 `verifyError`（例如"地址形状正确，
   *    但真实发送尚未接入"）。store 原样透传那句话，这里就如实渲染成
   *    一条**说明**而不是绿勾 —— 用户得到的是"地址没问题，但真实发送
   *    还没接"这个准确认知，而不是一个会撒谎的"已送达"。
   */
  const handleTest = async () => {
    setTouched(true)
    if (hasError) {
      setTestState({ kind: 'failed', error: '请先修正标红的字段' })
      return
    }

    setTestState({ kind: 'saving' })

    /*
      已有渠道且没改动时**不发那次 PATCH** —— 它是部分更新，空 patch
      只会白跑一个往返。新建则必须发，否则没有 id 可测。
    */
    const saved = !isNew && !dirty ? { channel } : await persist()
    if ('error' in saved) {
      setTestState({ kind: 'failed', error: saved.error })
      return
    }
    if (!isNew) setDraft(saved.channel)

    try {
      const result = await sendTestNotification(saved.channel)
      setTestState(
        result.status === 'ok'
          ? { kind: 'saved', at: result.at }
          : { kind: 'failed', error: result.error },
      )
    } catch {
      setTestState({ kind: 'failed', error: '测试请求失败，请检查网络后重试' })
    }

    /*
      新建时渠道在 `persist` 那一步就**真的建好了**（服务端已分配 id，
      列表里也多了一行）。此时还留着这个挂着"新增"身份的编辑器，
      用户会以为没建成，再点一次"添加渠道"就多出一条重复渠道。
      所以收起来 —— 这次测试的结论由列表行上的 `verified` / `verifyError` 展示。

      ⚠️ 这一句**不能**放进 `finally`：`finally` 在 return 时也会跑，
         于是"校验没过"和"创建失败"两条路径都会把用户填了一半的表单收掉。
         收起只该发生在**真的建成之后**。
    */
    if (isNew) onDone()
  }

  const handleDelete = async () => {
    const result = await removeNotifyChannel(channel.id)
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
            ★ 按钮从"禁用"改成**可点**，但结论仍然如实呈现。

            这里原来把结果渲染成绿色的「测试消息已送达 · 刚刚」，
            而实际上一个字节都没发出去。通知是**告警通道** ——
            给用户一个假的「已送达」，等于让他带着一条从未验证过的通道上线，
            而且他会因为见过那句绿字而**更加确信它没问题**。
            这比"没有这个功能"严重得多，所以当时的选择是禁用按钮。

            现在端点交付了，按钮可以点 —— 但它仍然**不会**给出绿字：
            后端只做本地校验，把结论写进 `verifyError`，store 原样透传。
            用户至少能确认"我这条地址形状是对的"，而不是对着一句
            "暂时无法验证"猜自己是哪里填错了。

            ⚠️ `testState.kind === 'saved'` 那个分支要等后端接入真实发送
               才会第一次走到 —— 留着它就是为了那一天。
          */}
          <button
            type="button"
            onClick={handleTest}
            disabled={testing}
            title="当前只校验地址形状，不会真的发送"
            className={cn(
              'inline-flex h-9 items-center gap-1.5 rounded-sm border px-3 text-xs transition-ui duration-fast',
              testing
                ? 'cursor-wait border-line text-ink-faint'
                : 'border-line text-ink-muted hover:border-line-strong hover:text-ink',
            )}
          >
            <Send size={12} strokeWidth={2} aria-hidden="true" />
            {testing ? '正在发送…' : '发送测试消息'}
          </button>

          <span className="text-2xs text-ink-faint">接口只做本地校验 —— 点它不会真的发出通知</span>

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
            /*
              ⚠️ 这里原来第一行是 `BACKEND_PENDING_NOTE`（"后端未交付，
              刷新回到初始值"）。通知已交付，那句话不再成立 ——
              去掉，而不是"留着更保险"（见 `BACKEND_PENDING` 的说明）。
            */
            !isNew ? (
              <span className="flex flex-wrap items-center gap-tight">
                <span>删除后不会再收到这个渠道的通知。</span>
                <DangerButton onClick={handleDelete}>删除渠道</DangerButton>
              </span>
            ) : null
          }
        />
      </div>
    </div>
  )
}

export default NotifyPanel
