import { cn } from '@/lib/cn'
import { DangerButton, Field, TextInput, Toggle } from './Field'
import RiskConfirm from './RiskConfirm'
import SecretField from './SecretField'
import { SaveBar, SettingsPanel, type SaveState } from './SaveBar'
import { isInsecureHttp } from '@/data/settings'
import {
  PROVIDER_PRESETS,
  validateApiKey,
  validateBaseUrl,
  validateModel,
  validateProviderLabel,
  type ProviderAccount,
  type ProviderPreset,
} from '@/data/settings'
import {
  fromPreset,
  nextAccountId,
  removeProviderAccount,
  saveProviderAccount,
  setDefaultProvider,
  verifyProvider,
} from '@/data/settings-store'
import { Check, Loader2, Plus, ShieldCheck, Star, TriangleAlert, X } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { useRevealOnExpand } from '@/hooks/useRevealOnExpand'

/**
 * 已有密钥时的输入框占位文案。
 *
 * ★ 用点号而不是一句"保持不变"。
 *
 *   两版视觉核查都在这处产生同一个误解：空输入框里显示"保持不变"时，
 *   读起来像**里面已经有这个内容**，而不是"这里本来存着一个密钥、你可以不管它"。
 *   点号占位直接借用了人们对密码框的既有认知 ——
 *   "这里面有东西，被遮住了"，一眼就懂。
 *
 *   它显示的是占位符而不是真实值（真实值根本不在 DOM 里，见 SecretField），
 *   下方还有一行"已保存（sk-dem…0000）"把实情说全。
 *   这与主流控制台对"已保存密钥"的处理一致。
 */
const STORED_KEY_PLACEHOLDER =
  '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022（已保存，留空则保持不变）'

/**
 * LLM Provider 配置面板。
 *
 * ★ 三个设计决定：
 *
 *   1. **账号列表常驻可见，编辑展开在行内。**
 *      不做"列表 → 点进去 → 编辑页"的跳转：provider 通常只有两三个，
 *      跳转会让"我到底配了几个、哪个是默认"这个全局问题需要来回切换才能回答。
 *
 *   2. **密钥不回填。** 已保存的账号展开后，API key 输入框是空的，
 *      只显示"已保存（sk-…）"这样的提示。理由见 SecretField 文件头 ——
 *      把已存的明文搬进 DOM 是这类页面最常见的一处泄露。
 *
 *   3. **保存与验证分开两个动作。** 它们的失败含义不同（没存进去 vs 存了但打不通），
 *      合起来用户就不知道该重填还是该重试。见 settings-store 的 verifyProvider。
 */

export interface ProviderPanelProps {
  accounts: ProviderAccount[]
}

export function ProviderPanel({ accounts }: ProviderPanelProps) {
  /** 正在编辑的账号 id。null = 没有在编辑 */
  const [editingId, setEditingId] = useState<string | null>(null)
  /** 正在新增时，用户选了哪个预设。非空表示"新增流程"处于打开状态 */
  const [addingPreset, setAddingPreset] = useState<ProviderPreset | null>(null)

  const [saveState, setSaveState] = useState<SaveState>({ kind: 'idle' })

  const closeAll = useCallback(() => {
    setEditingId(null)
    setAddingPreset(null)
    setSaveState({ kind: 'idle' })
  }, [])

  return (
    <SettingsPanel
      id="providers"
      title="模型服务"
      description="扫描时的推理由这里配置的模型完成。凭据只用于服务端调用，界面不会完整回显已保存的密钥。"
    >
      <div className="flex flex-col gap-2">
        {accounts.map((account) => (
          <AccountRow
            key={account.id}
            account={account}
            editing={editingId === account.id}
            onToggleEdit={() => {
              setEditingId(editingId === account.id ? null : account.id)
              setSaveState({ kind: 'idle' })
            }}
            // 同一时刻只允许一行展开：两行都开着会让"哪个保存条属于谁"变得含糊
            disabled={addingPreset !== null || (editingId !== null && editingId !== account.id)}
            saveState={saveState}
            setSaveState={setSaveState}
            onClose={closeAll}
          />
        ))}

        {/* ── 新增 ── */}
        {addingPreset ? (
          <div className="rounded-sm border border-accent/40 bg-accent-wash/40 p-3">
            <div className="mb-2 flex items-center justify-between gap-tight">
              <span className="text-xs text-ink">新增 · {addingPreset.label}</span>
              <button
                type="button"
                onClick={closeAll}
                aria-label="取消新增"
                className="rounded-sm p-1 text-ink-faint transition-ui duration-fast hover:text-ink"
              >
                <X size={13} strokeWidth={2} aria-hidden="true" />
              </button>
            </div>
            <AccountEditor
              account={{
                ...fromPreset(addingPreset, nextAccountId(addingPreset.id)),
                apiKey: '',
                // ★ 新建时服务端还没有这条记录，必然是"没有凭据、没有掩码"。
                //   这两个字段是**服务端 owned** 的，这里只是把草稿的形状补齐；
                //   保存成功后会用服务端返回的那一份整体覆盖。
                //   注意本地生成的 id 只是草稿的 key —— 真正的 id 由服务端分配，
                //   随 `POST /providers` 的响应回来（契约 §1.1 的字符串主键）。
                hasKey: false,
                apiKeyMask: '',
                isDefault: accounts.length === 0,
                verified: false,
                verifyError: null,
              }}
              isNew
              saveState={saveState}
              setSaveState={setSaveState}
              onDone={closeAll}
            />
          </div>
        ) : (
          <AddProviderPicker
            onPick={(preset) => {
              setAddingPreset(preset)
              setEditingId(null)
              setSaveState({ kind: 'idle' })
            }}
          />
        )}
      </div>
    </SettingsPanel>
  )
}

// ─────────────────────────────────────────────
// 新增时的服务商选择
// ─────────────────────────────────────────────

function AddProviderPicker({ onPick }: { onPick: (preset: ProviderPreset) => void }) {
  const [open, setOpen] = useState(false)

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex h-9 w-fit items-center gap-1.5 rounded-sm border border-line px-3 text-xs text-ink-muted transition-ui duration-fast hover:border-line-strong hover:text-ink"
      >
        <Plus size={13} strokeWidth={2} aria-hidden="true" />
        添加模型服务
      </button>
    )
  }

  return (
    <div className="flex flex-col gap-2 rounded-sm border border-line p-3">
      <p className="text-2xs text-ink-faint">
        选择服务商后，模型名与接入点会自动填好，通常只需要补一个 API key。
      </p>
      <div className="flex flex-wrap gap-1.5">
        {PROVIDER_PRESETS.map((preset) => (
          <button
            key={preset.id}
            type="button"
            onClick={() => onPick(preset)}
            className="inline-flex h-8 items-center gap-1.5 rounded-sm border border-line px-2.5 text-xs text-ink-muted transition-ui duration-fast hover:border-accent/50 hover:bg-accent-wash hover:text-ink"
          >
            {preset.label}
            {preset.kind === 'local' && (
              <span className="rounded-xs border border-line px-1 text-2xs text-ink-faint">
                本地
              </span>
            )}
          </button>
        ))}
      </div>
      <button
        type="button"
        onClick={() => setOpen(false)}
        className="w-fit text-2xs text-ink-faint transition-ui duration-fast hover:text-ink"
      >
        取消
      </button>
    </div>
  )
}

// ─────────────────────────────────────────────
// 列表行
// ─────────────────────────────────────────────

function AccountRow({
  account,
  editing,
  onToggleEdit,
  disabled,
  saveState,
  setSaveState,
  onClose,
}: {
  account: ProviderAccount
  editing: boolean
  onToggleEdit: () => void
  disabled: boolean
  saveState: SaveState
  setSaveState: (state: SaveState) => void
  onClose: () => void
}) {
  const [defaultState, setDefaultState] = useState<SaveState>({ kind: 'idle' })
  /**
   * 展开后把整行带进视野。
   *
   * 实测：视口 1018px 时，展开后的保存按钮在 1085px —— 在折叠线以下。
   * 用户点「编辑」却看不到保存按钮，对一个"保存状态必须可信"的页面
   * 是很糟的开场。见 useRevealOnExpand 的说明。
   */
  const rowRef = useRevealOnExpand<HTMLDivElement>(editing)

  const handleSetDefault = async () => {
    setDefaultState({ kind: 'saving' })
    const result = await setDefaultProvider(account.id)
    setDefaultState(
      result.status === 'ok'
        ? { kind: 'saved', at: result.at }
        : result.status === 'failed'
          ? { kind: 'failed', error: result.error }
          : { kind: 'unchanged' },
    )
  }

  return (
    <div
      ref={rowRef}
      className={cn(
        'rounded-sm border transition-ui duration-fast',
        editing ? 'border-accent/40 bg-accent-wash/30' : 'border-line',
      )}
    >
      {/* ── 行头：状态 + 名称 + 快捷操作 ── */}
      <div className="flex flex-wrap items-center gap-x-tight gap-y-2 p-3">
        {/* 可用状态点。它是这一行最重要的信息：这个 provider 能不能用 */}
        <span
          className={cn(
            'flex h-5 w-5 shrink-0 items-center justify-center rounded-pill',
            account.verified
              ? 'bg-signal-ok/15 text-signal-ok'
              : 'bg-signal-warn/15 text-signal-warn',
          )}
          title={account.verified ? '已验证可用' : (account.verifyError ?? '尚未验证')}
        >
          {account.verified ? (
            <Check size={11} strokeWidth={2.5} aria-hidden="true" />
          ) : (
            <TriangleAlert size={11} strokeWidth={2.5} aria-hidden="true" />
          )}
        </span>

        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="flex flex-wrap items-center gap-1.5">
            <span className="text-sm text-ink">{account.label}</span>
            <span className="mono-ui text-ink-faint">{account.model}</span>
            {account.kind === 'local' && (
              <span className="rounded-xs border border-line px-1 text-2xs text-ink-faint">
                本地
              </span>
            )}
            {/* 默认标记 —— 总览状态条显示的就是这个模型 */}
            {account.isDefault && (
              <span className="inline-flex items-center gap-1 rounded-xs border border-accent/40 px-1 text-2xs text-accent-strong">
                <Star size={9} strokeWidth={2.5} aria-hidden="true" />
                默认
              </span>
            )}
          </span>

          <span className="flex flex-wrap items-center gap-x-2 text-2xs text-ink-faint">
            {account.verified ? (
              <span>已验证可用</span>
            ) : (
              // 未验证的原因要具体，不能只说"未验证"
              <span className="text-signal-warn">{account.verifyError ?? '尚未验证'}</span>
            )}
            {/* 凭据掩码回显 —— 直接用**服务端给的成品掩码**（如 `sk-****abcd`）。
                它是"这里有一份凭据"的证据，而不是凭据本身。

                ⚠️ 判据必须是 `hasKey`，**不能**是 `apiKey.length > 0`：
                接入后端后 `apiKey` 恒为空串（后端 `from_model` 硬编码），
                用旧判据这段永远不会渲染 —— 用户会以为自己的密钥丢了，
                于是重新粘贴一遍，而"最后修改时间"之类的元信息就此失真。

                ⚠️ 也**不要**对 `apiKeyMask` 再跑一遍 `secretPreview`：
                掩码本身已经是成品，再过一遍会变成 `••••••••`，
                反而看不出是哪一把 key。 */}
            {account.hasKey && account.apiKeyMask && (
              <>
                <span aria-hidden="true">·</span>
                <span className="mono-ui">{account.apiKeyMask}</span>
              </>
            )}
          </span>
        </div>

        <div className="flex shrink-0 items-center gap-1">
          {!account.isDefault && (
            <button
              type="button"
              onClick={handleSetDefault}
              className="inline-flex h-7 items-center gap-1 rounded-sm px-2 text-2xs text-ink-faint transition-ui duration-fast hover:bg-surface-raised hover:text-ink"
            >
              <Star size={11} strokeWidth={1.75} aria-hidden="true" />
              设为默认
            </button>
          )}

          <button
            type="button"
            onClick={onToggleEdit}
            disabled={disabled}
            className={cn(
              'inline-flex h-7 items-center rounded-sm px-2 text-2xs transition-ui duration-fast',
              disabled
                ? 'cursor-not-allowed text-ink-ghost'
                : 'text-ink-muted hover:bg-surface-raised hover:text-ink',
            )}
          >
            {editing ? '收起' : '编辑'}
          </button>
        </div>
      </div>

      {/* 设为默认的结果就地显示在这一行 —— 不打扰别的行，也不会飘走 */}
      {defaultState.kind !== 'idle' && !editing && (
        <div className="px-3 pb-2">
          <SaveStateInline state={defaultState} />
        </div>
      )}

      {/* ── 展开的编辑器 ── */}
      {editing && (
        <div className="border-t border-line/60 p-3">
          <AccountEditor
            account={account}
            saveState={saveState}
            setSaveState={setSaveState}
            onDone={onClose}
          />
        </div>
      )}
    </div>
  )
}

/** 行内保存结果 —— 复用 SaveBar 的措辞逻辑，但形态更轻 */
function SaveStateInline({ state }: { state: SaveState }) {
  if (state.kind === 'saved') {
    return <span className="text-2xs text-signal-ok">已设为默认</span>
  }
  if (state.kind === 'failed') {
    return (
      <span role="alert" className="text-2xs text-signal-risk">
        操作失败：{state.error}
      </span>
    )
  }
  if (state.kind === 'unchanged') {
    return <span className="text-2xs text-ink-faint">它已经是默认了</span>
  }
  return null
}

// ─────────────────────────────────────────────
// 编辑器
// ─────────────────────────────────────────────

/**
 * 账号编辑表单。
 *
 * ★ 草稿状态（draft）是本组件的核心：它持有未保存的修改，
 *   点保存才写进 store。这保证了：
 *     · 打字打到一半不会产生"半截的已保存值"；
 *     · 保存失败时草稿仍在，用户的输入不丢。
 */
function AccountEditor({
  account,
  isNew = false,
  saveState,
  setSaveState,
  onDone,
}: {
  account: ProviderAccount
  isNew?: boolean
  saveState: SaveState
  setSaveState: (state: SaveState) => void
  onDone: () => void
}) {
  /**
   * 草稿。
   *
   * ★ 已有账号的草稿里 **apiKey 必须清空**。
   *
   *   实测抓到的缺陷：早先这里直接 `useState(account)`，
   *   于是已保存的密钥被原样放进 SecretField 的 value ——
   *   一展开编辑器，完整明文就进了 DOM 和屏幕。
   *   这同时违反了三条：页面上写的"不会回填"、
   *   验收要求的"不要明文常驻"、以及这个组件存在的全部意义。
   *
   *   正确做法是：输入框永远从空开始（空 = 沿用已存的值），
   *   只有用户真的输入了新的 key，才把 keyEdited 置为 true 并在保存时替换。
   *   见 handleSave 里 payload 的拼装。
   */
  const [draft, setDraft] = useState<ProviderAccount>(() =>
    isNew ? account : { ...account, apiKey: '' },
  )
  const [touched, setTouched] = useState(false)
  const [verifying, setVerifying] = useState(false)
  const [verifyResult, setVerifyResult] = useState<SaveState>({ kind: 'idle' })
  /** 用户是否输入了新 key（用于决定显示"已保存"还是格式提示） */
  const [keyEdited, setKeyEdited] = useState(isNew)

  /**
   * 是否已确认"这个 http 地址的风险由我承担"（v0.12）。
   *
   * ⚠️ **刻意是组件内 state，不落库、不进 localStorage。**
   * 两个理由：
   *   · 它是"这一次我知情了"，不是一个应该长期记住的配置 ——
   *     落库之后下次打开表单会直接放行，那道提示就白设了；
   *   · 换一个 provider 卡片时它天然重置（每个卡片有自己的 state），
   *     不会出现"确认过一个、所有卡片都放行"。
   */
  const [httpAcknowledged, setHttpAcknowledged] = useState(false)

  /**
   * 校验。
   *
   * `touched`（提交过）才把错误显示出来 —— 用户刚开始输入就飘红是最烦人的表单体验。
   * 这与向导里的约定一致（ValidationFeedback）。
   */
  const errors = useMemo(() => {
    const label = validateProviderLabel(draft.label)
    const model = validateModel(draft.model)
    // ★ 把"已确认"传进校验：确认前拦下，确认后放行（v0.12）
    const baseUrl = validateBaseUrl(draft.baseUrl, httpAcknowledged)
    const apiKey = validateApiKey(draft.apiKey, draft.kind)

    return {
      label: label.ok ? null : label.error,
      model: model.ok ? null : model.error,
      baseUrl: baseUrl.ok ? null : baseUrl.error,
      // 已保存的账号没动 key 时，不校验 key（空 = 沿用已存的那个）
      apiKey: !isNew && !keyEdited ? null : apiKey.ok ? null : apiKey.error,
    }
  }, [draft, isNew, keyEdited, httpAcknowledged])

  const hasError = Object.values(errors).some((e) => e !== null)

  /** 是否有未保存改动。用它决定"放弃改动"是否可点、以及保存按钮的样式 */
  const dirty = useMemo(() => {
    if (isNew) return true // 新增永远算"有改动"
    return (
      draft.label !== account.label ||
      draft.model !== account.model ||
      draft.baseUrl !== account.baseUrl ||
      draft.isDefault !== account.isDefault ||
      keyEdited
    )
  }, [draft, account, keyEdited, isNew])

  const handleSave = async () => {
    setTouched(true)
    if (hasError) {
      setSaveState({ kind: 'failed', error: '有字段未通过校验，请先修正' })
      return
    }
    if (!dirty) {
      // 点了保存但什么都没改 —— 不能说"已保存"，那是虚假的动作感
      setSaveState({ kind: 'unchanged' })
      return
    }

    setSaveState({ kind: 'saving' })

    /*
      ★ `apiKey` 只在**用户这次真的输入了**的时候才带出去。

      接入后端后 `account.apiKey` 恒为空串（后端 `from_model` 硬编码，
      那条路径上没有任何变量能通向明文）。所以这里传空串 ——
      store 见到空串会**完全不发这个字段**，后端据此保持原凭据不变
      （契约 §2.9：省略 `apiKey` = 不改凭据）。

      ⚠️ 不要写成 `keyEdited ? draft.apiKey : account.apiKey`：
      目前两者等价（都是空串），但一旦 `account.apiKey` 哪天带上了掩码，
      把它回传就会被后端当成一个新 key 存下去，真凭据被**静默**毁掉 ——
      而"静默"正是这个 bug 最坏的地方，用户不会收到任何报错。
      写成空串，让"不改凭据"这个意图在代码里是显式的。
    */
    const payload: ProviderAccount = {
      ...draft,
      apiKey: keyEdited ? draft.apiKey : '',
    }

    const result = await saveProviderAccount(payload)
    if (result.status === 'ok') {
      setSaveState({ kind: 'saved', at: result.at })
      // 保存成功后同步基准，让 dirty 归零
      // 保存成功：把草稿的 key 清回空 —— 不清的话，这次的值会立刻回填进输入框
      setDraft({ ...payload, apiKey: '' })
      setKeyEdited(false)
      setTouched(false)
      if (isNew) onDone()
    } else if (result.status === 'failed') {
      setSaveState({ kind: 'failed', error: result.error })
    } else {
      setSaveState({ kind: 'unchanged' })
    }
  }

  const handleReset = () => {
    // ⚠️ 显式清空 apiKey，而不是依赖 `account` 恰好是空的。
    //    草稿里可能还留着用户刚敲进去的明文；而"放弃改动"的语义
    //    就是**回到什么都没输入的状态**。
    setDraft({ ...account, apiKey: '' })
    setTouched(false)
    setKeyEdited(isNew)
    setSaveState({ kind: 'idle' })
    setVerifyResult({ kind: 'idle' })
  }

  const handleDelete = async () => {
    const result = await removeProviderAccount(account.id)
    if (result.status === 'failed') {
      // 两种 409 都会走到这里，后端给的中文说明可以直接展示：
      //   PROVIDER_IS_DEFAULT → "它当前是默认…先去把另一个设为默认"
      //   PROVIDER_IN_USE     → "还有未结束的扫描在使用它"
      setSaveState({ kind: 'failed', error: result.error })
      return
    }
    onDone()
  }

  /**
   * 验证凭据。
   *
   * 先把未保存的改动落库再验证 —— 否则验证的是旧值，
   * 用户改了 key 点验证，得到的是"上一个 key 的结果"，那会很困惑。
   */
  const handleVerify = async () => {
    setTouched(true)
    if (errors.apiKey || errors.baseUrl || errors.model) {
      setVerifyResult({ kind: 'failed', error: '请先修正标红的字段' })
      return
    }

    const payload: ProviderAccount = {
      ...draft,
      apiKey: keyEdited ? draft.apiKey : '',
    }

    const saved = await saveProviderAccount(payload)
    if (saved.status === 'failed') {
      setVerifyResult({ kind: 'failed', error: saved.error })
      return
    }

    // 同 handleSave：草稿里的 key 清回空，避免明文回填进输入框
    setDraft({ ...payload, apiKey: '' })
    setKeyEdited(false)
    setVerifying(true)
    setVerifyResult({ kind: 'saving' })

    try {
      const result = await verifyProvider(payload)
      setVerifyResult(
        result.status === 'ok'
          ? { kind: 'saved', at: result.at }
          : { kind: 'failed', error: result.error },
      )
    } catch {
      setVerifyResult({ kind: 'failed', error: '验证请求失败，请检查网络后重试' })
    } finally {
      setVerifying(false)
    }
  }

  return (
    <div className="flex flex-col gap-panel">
      <div className="grid gap-panel sm:grid-cols-2">
        <Field
          label="显示名称"
          htmlFor={`${account.id}-label`}
          required
          /*
            这一行 help 不只是补充说明 —— 它让同一行两列的结构**对齐**。
            实测：隔壁「模型名称」有 help 而这里没有时，两列输入框的
            顶部相差 24px（help 行的高度），并排的输入框一个上一个下，
            看起来像没对齐的表格。给两列同样的「标签 + 说明 + 控件」结构，
            它们自然落在同一条基线上。
          */
          help="用于在列表与扫描配置里辨认这一条。"
          error={touched ? errors.label : null}
        >
          <TextInput
            id={`${account.id}-label`}
            value={draft.label}
            onChange={(v) => setDraft({ ...draft, label: v })}
            error={touched ? errors.label : null}
          />
        </Field>

        <Field
          label="模型名称"
          htmlFor={`${account.id}-model`}
          required
          help="与配置文件里的模型标识符一致，不要写中文别名。"
          error={touched ? errors.model : null}
        >
          <TextInput
            id={`${account.id}-model`}
            value={draft.model}
            onChange={(v) => setDraft({ ...draft, model: v })}
            mono
            error={touched ? errors.model : null}
          />
        </Field>
      </div>

      <Field
        label="接入地址"
        htmlFor={`${account.id}-baseurl`}
        help="留空则使用官方默认地址。自建网关或代理时填写。"
        error={touched ? errors.baseUrl : null}
        /*
          ★ 风险提示与"我已知晓风险"按钮放在**错误位置**（v0.12）。
          报错说"这条地址有问题"，解决它的动作就在同一处 ——
          用户不需要去别的地方找开关。

          提示的显示条件与校验用的是**同一个判据**（`isInsecureHttp`）：
          两处各写一遍必然漂移，而漂移的表现是
          "提示说不安全、但校验已经放行了"。
        */
        footer={
          isInsecureHttp(draft.baseUrl) ? (
            <RiskConfirm
              acknowledged={httpAcknowledged}
              onAcknowledge={() => setHttpAcknowledged(true)}
              risk="该地址使用 http —— API key 会以明文经过网络。内网自建网关可确认后继续。"
              acknowledgedText="已确认使用 http，密钥将以明文传输到该地址。"
            />
          ) : null
        }
      >
        <TextInput
          id={`${account.id}-baseurl`}
          value={draft.baseUrl}
          onChange={(v) => setDraft({ ...draft, baseUrl: v })}
          placeholder="留空使用默认地址"
          mono
          error={touched && !isInsecureHttp(draft.baseUrl) ? errors.baseUrl : null}
        />
      </Field>

      {/*
        ★ 密钥字段：已保存的账号不回填，只提示"已保存"。
        见 SecretField 文件头与 AccountEditor 里 payload 的拼装。
      */}
      <SecretField
        id={`${account.id}-apikey`}
        label="API key"
        value={draft.apiKey}
        onChange={(v) => {
          setKeyEdited(true)
          setDraft({ ...draft, apiKey: v })
        }}
        required={draft.kind !== 'local'}
        savedHint={
          // 同上一处：判据是 `hasKey`，掩码用服务端的成品
          !keyEdited && !isNew && account.hasKey
            ? `已保存（${account.apiKeyMask}）· 如需更换请直接输入新值`
            : null
        }
        placeholder={
          isNew || keyEdited
            ? '粘贴 API key'
            : account.hasKey
              ? STORED_KEY_PLACEHOLDER
              : '粘贴 API key'
        }
        hint={
          PROVIDER_PRESETS.find((p) => p.id.startsWith(account.id.split('_')[1] ?? ''))?.keyHint
        }
        error={touched ? errors.apiKey : null}
        help={
          draft.kind === 'local'
            ? '本地服务通常不需要 API key，留空即可。'
            : '密钥只用于服务端调用，保存后不再完整回显。'
        }
      />

      <Toggle
        id={`${account.id}-default`}
        label="设为默认模型"
        hint="总览状态条与未指定模型的扫描会使用它。全部账号中只有一个默认。"
        checked={draft.isDefault}
        onChange={(v) => setDraft({ ...draft, isDefault: v })}
      />

      {/* ── 操作区 ── */}
      <div className="flex flex-col gap-tight border-t border-line pt-panel">
        <div className="flex flex-wrap items-center gap-tight">
          <button
            type="button"
            onClick={handleVerify}
            disabled={verifying}
            className={cn(
              'inline-flex h-9 items-center gap-1.5 rounded-sm border px-3 text-xs transition-ui duration-fast',
              verifying
                ? 'cursor-wait border-line text-ink-faint'
                : 'border-line text-ink-muted hover:border-line-strong hover:text-ink',
            )}
          >
            {verifying ? (
              <Loader2 size={12} strokeWidth={2} className="animate-spin" aria-hidden="true" />
            ) : (
              <ShieldCheck size={12} strokeWidth={2} aria-hidden="true" />
            )}
            {verifying ? '正在验证…' : '验证凭据'}
          </button>

          {/* 验证结果与保存结果分开展示 —— 它们失败的含义不同 */}
          {verifyResult.kind === 'saved' && (
            <span role="status" className="text-2xs text-signal-ok">
              凭据可用，验证通过
            </span>
          )}
          {verifyResult.kind === 'failed' && (
            <span role="alert" className="text-2xs text-signal-risk">
              {verifyResult.error}
            </span>
          )}
        </div>

        <SaveBar
          state={saveState}
          dirty={dirty}
          onSave={handleSave}
          onReset={handleReset}
          saveLabel={isNew ? '添加' : '保存'}
          note={
            !isNew ? (
              <span className="flex flex-wrap items-center gap-tight">
                <span>删除这个 provider 不会影响已完成的扫描记录。</span>
                <DangerButton onClick={handleDelete}>删除此配置</DangerButton>
              </span>
            ) : null
          }
        />
      </div>
    </div>
  )
}

export default ProviderPanel
