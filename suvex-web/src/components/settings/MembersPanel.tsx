import { cn } from '@/lib/cn'
import { useI18n } from '@/i18n'
import { T } from '@/i18n/T'
import { DangerButton, Field, TextInput } from './Field'
import { SettingsPanel } from './SaveBar'
import { isAdmin, useAuth } from '@/data/auth-store'
import {
  MAX_MEMBERS,
  createMember,
  deleteMember,
  loadMembers,
  updateMember,
  useMembers,
  type Member,
} from '@/data/members-store'
import { Check, Pencil, Plus, Trash2, UserRound, X } from 'lucide-react'
import { useEffect, useState } from 'react'

/**
 * 处理人管理面板（v1.79）。
 *
 * ## 它和「用户」是两个不同的名单
 *
 * 这一页回答的是"漏洞能指派给谁"，不是"谁能登录"。两者刻意分开：
 *
 *   · **处理人**可以是不用登录的人（外包、客户方的对接人）；
 *   · **账号**可以没有指派（只读审计账号）。
 *
 * 混成一个名单会逼着每个被指派的人都有一个登录账号 —— 那既多余
 * 又有安全成本。而漏洞页的「指派」对话框与筛选下拉读的正是这份名单。
 *
 * ## 为什么之前这里是个死胡同
 *
 * `/members` 的 CRUD 后端从任务 10 就在，界面却只有漏洞页的指派下拉
 * 会读它 —— 一个处理人都没有时，`AssignDialog` 说「去设置 → 处理人里
 * 加一个」，而设置页根本没有这一节。用户照着提示找过去，什么也找不到。
 * **提示指向不存在的入口**比没有提示更糟。
 *
 * ## 停用优先于删除
 *
 * 删除会让名下漏洞**静默变成未指派**（`ON DELETE SET NULL`），历史里
 * "这条当时是谁负责的"就此消失。所以行内主操作是「停用」，
 * 删除放在次要位置、两步确认，并把被解除指派的条数回给用户。
 */
export default function MembersPanel() {
  const { t } = useI18n()
  const { user } = useAuth()
  const canManage = isAdmin(user)
  const { members, status, error } = useMembers()
  const [adding, setAdding] = useState(false)

  // 面板自己拉一次：`loadMembers` 幂等，指派对话框那头也会调，不会重复请求。
  // 只在还没有任何数据时拉，避免切回设置页时把已加载的名单又刷一遍。
  useEffect(() => {
    if (status === 'idle') void loadMembers()
  }, [status])

  const activeCount = members.filter((m) => m.active).length

  return (
    <SettingsPanel
      id="members"
      title={t('settings.members.title')}
      description={t('settings.members.description')}
    >
      <div className="flex flex-col gap-panel">
        {status === 'error' ? (
          <p role="alert" className="text-xs text-signal-risk">
            {error?.message ?? t('settings.members.loadFailed')}
          </p>
        ) : status !== 'ready' ? (
          <p className="text-xs text-ink-muted">
            <T k="settings.members.loading" />
          </p>
        ) : members.length === 0 ? (
          <div className="rounded-control border border-dashed border-line-strong px-panel py-panel text-center">
            <UserRound
              size={18}
              strokeWidth={1.5}
              className="mx-auto text-ink-faint"
              aria-hidden="true"
            />
            <p className="mt-1.5 text-sm text-ink-muted">
              <T k="settings.members.empty" />
            </p>
            <p className="mt-0.5 text-xs text-ink-faint">
              <T k="settings.members.emptyHint" />
            </p>
          </div>
        ) : (
          <>
            <ul className="rounded-control flex flex-col divide-y divide-line border border-line">
              {members.map((member) => (
                <MemberRow key={member.id} member={member} canManage={canManage} />
              ))}
            </ul>
            <p className="text-2xs text-ink-faint">
              <T
                k="settings.members.countSummary"
                vars={{
                  total: members.length,
                  active: activeCount,
                  limit:
                    members.length >= MAX_MEMBERS
                      ? t('settings.members.limitSuffix', { max: MAX_MEMBERS })
                      : '',
                }}
              />
              {' '}
              <T k="settings.members.countNote" />
            </p>
          </>
        )}

        {canManage &&
          (adding ? (
            <AddMemberForm onDone={() => setAdding(false)} full={members.length >= MAX_MEMBERS} />
          ) : (
            <button
              type="button"
              onClick={() => setAdding(true)}
              disabled={members.length >= MAX_MEMBERS}
              title={
                members.length >= MAX_MEMBERS
                  ? t('settings.members.limitTitle', { max: MAX_MEMBERS })
                  : t('settings.members.addTitle')
              }
              className={cn(
                'rounded-control inline-flex h-9 items-center gap-1.5 self-start border border-line',
                'px-3 text-xs text-ink-muted transition-ui duration-fast',
                'hover:border-line-strong hover:text-ink',
                'disabled:cursor-not-allowed disabled:border-line/50 disabled:text-ink-faint disabled:hover:border-line/50 disabled:hover:text-ink-faint',
              )}
            >
              <Plus size={14} aria-hidden="true" />
              <T k="settings.members.add" />
            </button>
          ))}
      </div>
    </SettingsPanel>
  )
}

// ─────────────────────────────────────────────
// 一行
// ─────────────────────────────────────────────

function MemberRow({ member, canManage }: { member: Member; canManage: boolean }) {
  const { t } = useI18n()
  const [editing, setEditing] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const run = async (action: () => Promise<{ ok: boolean; error?: string }>, done?: () => void) => {
    setBusy(true)
    setProblem(null)
    setNotice(null)
    const result = await action()
    setBusy(false)
    if (result.ok) done?.()
    else setProblem(result.error ?? t('settings.members.actionFailed'))
  }

  if (editing) {
    return (
      <li className="px-3 py-2.5">
        <EditMemberForm
          member={member}
          onCancel={() => setEditing(false)}
          onSaved={() => {
            setEditing(false)
            setNotice(t('settings.members.saved'))
          }}
        />
      </li>
    )
  }

  return (
    <li className="flex flex-col gap-1.5 px-3 py-2.5">
      <div className="flex items-center gap-3">
        <span
          aria-hidden="true"
          className={cn(
            'flex h-7 w-7 shrink-0 items-center justify-center rounded-pill',
            member.active ? 'bg-accent-wash text-accent' : 'bg-surface-sunken text-ink-faint',
          )}
        >
          <UserRound size={14} />
        </span>

        <span className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2">
          <span className={cn('truncate text-xs', member.active ? 'text-ink' : 'text-ink-muted')}>
            {member.name}
          </span>
          {member.email && <span className="truncate text-2xs text-ink-faint">{member.email}</span>}
          {!member.active && (
            <span className="rounded-pill bg-surface-sunken px-2 py-0.5 text-2xs text-ink-faint">
              <T k="settings.members.inactive" />
            </span>
          )}
        </span>

        {canManage && (
          <span className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              disabled={busy}
              onClick={() => setEditing(true)}
              title={t('settings.members.editTitle')}
              className={rowAction}
            >
              <Pencil size={12} aria-hidden="true" />
              <T k="settings.members.edit" />
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                void run(
                  () => updateMember(member.id, { active: !member.active }),
                  () =>
                    setNotice(
                      member.active
                        ? t('settings.members.deactivatedNotice')
                        : t('settings.members.activatedNotice'),
                    ),
                )
              }
              className={rowAction}
            >
              {member.active ? (
                <X size={12} aria-hidden="true" />
              ) : (
                <Check size={12} aria-hidden="true" />
              )}
              {member.active ? (
                <T k="settings.members.deactivate" />
              ) : (
                <T k="settings.members.activate" />
              )}
            </button>
            {!confirming && (
              <button
                type="button"
                disabled={busy}
                onClick={() => setConfirming(true)}
                aria-label={t('settings.members.deleteAria', { name: member.name })}
                title={t('settings.members.deleteTitle')}
                className={cn(rowAction, 'hover:border-signal-risk/50 hover:text-signal-risk')}
              >
                <Trash2 size={12} aria-hidden="true" />
              </button>
            )}
          </span>
        )}
      </div>

      {/*
        删除要两步，并且说清楚代价。
        后端会返回被解除指派的条数 —— 那个数字必须让用户看见，
        否则那些指派是在无人知晓的情况下消失的。
      */}
      {confirming && (
        <div className="flex flex-wrap items-center gap-2 pl-10 text-2xs text-ink-muted">
          <span>
            <T k="settings.members.deleteWarn" />
          </span>
          <button
            type="button"
            disabled={busy}
            onClick={async () => {
              setBusy(true)
              setProblem(null)
              const result = await deleteMember(member.id)
              setBusy(false)
              setConfirming(false)
              if (!result.ok) setProblem(result.error)
              else if (result.unassigned > 0)
                setNotice(
                  t('settings.members.deletedUnassigned', { n: result.unassigned }),
                )
              else setNotice(t('settings.members.deleted'))
            }}
            className="rounded-control inline-flex h-7 items-center bg-signal-risk/15 px-2 text-signal-risk transition-ui duration-fast hover:bg-signal-risk/25 disabled:cursor-not-allowed disabled:text-ink-faint"
          >
            <T k="settings.members.confirmDelete" />
          </button>
          <DangerButton onClick={() => setConfirming(false)}>
            <T k="settings.members.cancel" />
          </DangerButton>
        </div>
      )}

      {problem && <p className="pl-10 text-2xs text-signal-risk">{problem}</p>}
      {notice && <p className="pl-10 text-2xs text-signal-ok">{notice}</p>}
    </li>
  )
}

const rowAction = cn(
  'inline-flex h-7 items-center gap-1 rounded-control border border-line px-2 text-2xs',
  'text-ink-muted transition-ui duration-fast',
  'hover:border-line-strong hover:text-ink',
  'disabled:cursor-not-allowed disabled:border-line/50 disabled:text-ink-faint disabled:hover:border-line/50 disabled:hover:text-ink-faint',
)

// ─────────────────────────────────────────────
// 新增 / 编辑
// ─────────────────────────────────────────────

function AddMemberForm({ onDone, full }: { onDone: () => void; full: boolean }) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)

  const submit = async () => {
    setProblem(null)
    if (!name.trim()) {
      setProblem(t('settings.members.nameRequired'))
      return
    }
    setBusy(true)
    const result = await createMember(name.trim(), email.trim() || null)
    setBusy(false)
    if (!result.ok) {
      setProblem(result.error)
      return
    }
    onDone()
  }

  return (
    <div className="rounded-control flex flex-col gap-3 border border-line bg-surface-raised p-3">
      <Field
        label={t('settings.members.nameLabel')}
        htmlFor="new-member-name"
      >
        <TextInput
          id="new-member-name"
          value={name}
          onChange={setName}
          autoComplete="off"
          placeholder={t('settings.members.namePlaceholder')}
        />
      </Field>

      <Field
        label={t('settings.members.emailLabel')}
        htmlFor="new-member-email"
        help={t('settings.members.emailHelp')}
      >
        <TextInput
          id="new-member-email"
          type="email"
          value={email}
          onChange={setEmail}
          autoComplete="off"
          placeholder="zhangsan@example.com"
        />
      </Field>

      {full && (
        <p className="text-2xs text-signal-warn">
          <T k="settings.members.full" />
        </p>
      )}
      {problem && <p className="text-2xs text-signal-risk">{problem}</p>}

      <div className="flex gap-2">
        <button
          type="button"
          disabled={busy || full}
          onClick={() => void submit()}
          className={cn(
            'rounded-control inline-flex h-8 items-center bg-accent px-3 text-xs text-canvas',
            'transition-ui duration-fast hover:bg-accent-strong disabled:cursor-not-allowed disabled:bg-accent-dim disabled:text-ink-muted disabled:hover:bg-accent-dim',
          )}
        >
          {busy ? <T k="settings.members.creating" /> : <T k="settings.members.create" />}
        </button>
        <DangerButton onClick={onDone}>
          <T k="settings.members.cancel" />
        </DangerButton>
      </div>
    </div>
  )
}

function EditMemberForm({
  member,
  onCancel,
  onSaved,
}: {
  member: Member
  onCancel: () => void
  onSaved: () => void
}) {
  const { t } = useI18n()
  const [name, setName] = useState(member.name)
  const [email, setEmail] = useState(member.email ?? '')
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)

  const submit = async () => {
    setProblem(null)
    if (!name.trim()) {
      setProblem(t('settings.members.nameEmpty'))
      return
    }
    setBusy(true)
    const result = await updateMember(member.id, {
      name: name.trim(),
      email: email.trim() || null,
    })
    setBusy(false)
    if (!result.ok) {
      setProblem(result.error)
      return
    }
    onSaved()
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="grid gap-2 sm:grid-cols-2">
        <Field label={t('settings.members.nameLabel')} htmlFor={`member-name-${member.id}`}>
          <TextInput
            id={`member-name-${member.id}`}
            value={name}
            onChange={setName}
            autoComplete="off"
          />
        </Field>
        <Field
          label={t('settings.members.emailLabel')}
          htmlFor={`member-email-${member.id}`}
          help={t('settings.members.emailOptional')}
        >
          <TextInput
            id={`member-email-${member.id}`}
            type="email"
            value={email}
            onChange={setEmail}
            autoComplete="off"
          />
        </Field>
      </div>

      {problem && <p className="text-2xs text-signal-risk">{problem}</p>}

      <div className="flex gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => void submit()}
          className={cn(
            'rounded-control inline-flex h-8 items-center bg-accent px-3 text-xs text-canvas',
            'transition-ui duration-fast hover:bg-accent-strong disabled:cursor-not-allowed disabled:bg-accent-dim disabled:text-ink-muted disabled:hover:bg-accent-dim',
          )}
        >
          {busy ? <T k="settings.members.saving" /> : <T k="settings.members.save" />}
        </button>
        <DangerButton onClick={onCancel}>
          <T k="settings.members.cancel" />
        </DangerButton>
      </div>
    </div>
  )
}
