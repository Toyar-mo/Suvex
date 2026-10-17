import { cn } from '@/lib/cn'
import { DangerButton, Field, TextInput } from './Field'
import { SettingsPanel } from './SaveBar'
import {
  createUser,
  deleteBlockedReason,
  deleteUser,
  demoteBlockedReason,
  getUsersServerSnapshot,
  getUsersSnapshot,
  subscribeUsers,
  updateUserRole,
  type ManagedUser,
} from '@/data/users-store'
import { isAdmin, useAuth, type UserRole } from '@/data/auth-store'
import { checkPassword } from '@/data/settings'
import { formatRelativeTime } from '@/lib/datetime'
import { Plus, ShieldCheck, Trash2, UserRound } from 'lucide-react'
import { useState, useSyncExternalStore } from 'react'

/**
 * 用户管理面板（v0.24）。
 *
 * ## 这一节与设置页其它面板的本质区别
 *
 * 别的面板改的是"系统怎么跑"（模型、沙箱、通知）；这一节改的是
 * **谁能进、谁是管理员**。所以它多了两件别处没有的事：
 *
 *   1. **防自锁的前置判断**（`deleteBlockedReason` / `demoteBlockedReason`）——
 *      "删掉最后一个管理员"是一个通过界面走进去的死胡同，
 *      而恢复只能直接改库。所以那两个按钮在那种情况下**不可点**，
 *      并且**要说明为什么**（灰按钮不给理由是坏交互）。
 *   2. **只有管理员能看到它**（`isAdmin` 判断）—— viewer 看不到这个入口。
 *
 *      ⚠️ 这只是**展示性**的：真正的拦截在后端
 *      （`require_admin_session`，而且机器令牌一律拒绝）。
 *      前端藏起来是为了"看到却点不动"的糟糕体验，不是安全边界。
 *
 * ## 为什么没有"改密码"与"改用户名"
 *
 * * **改密码**由本人走「账号与安全」那一节 —— 管理员替别人改口令
 *   意味着管理员知道别人的口令，那不是一个好模型；
 * * **改用户名**会让审计日志里的历史记录与当前名字对不上（有损）。
 *   真要支持得先想清楚"历史记录里那个名字要不要跟着变"。
 */
export default function UsersPanel() {
  const { user } = useAuth()
  const [adding, setAdding] = useState(false)

  return (
    <SettingsPanel
      id="users"
      title="用户"
      description="能登录这个系统的人。管理员能改设置与发起扫描，只读用户只能查看。"
    >
      <UserList currentUserId={user?.id ?? null} currentUserIsAdmin={isAdmin(user)} />

      {adding ? (
        <AddUserForm onDone={() => setAdding(false)} />
      ) : (
        <button
          type="button"
          onClick={() => setAdding(true)}
          className={cn(
            'inline-flex h-9 items-center gap-1.5 self-start rounded-control border border-line',
            'px-3 text-xs text-ink-muted transition-ui duration-fast',
            'hover:border-line-strong hover:text-ink',
          )}
        >
          <Plus size={14} aria-hidden="true" />
          新增用户
        </button>
      )}
    </SettingsPanel>
  )
}

// ─────────────────────────────────────────────
// 列表
// ─────────────────────────────────────────────

function UserList({
  currentUserId,
  currentUserIsAdmin,
}: {
  currentUserId: string | null
  currentUserIsAdmin: boolean
}) {
  /*
    直接订阅 store，而不是从 props 拿。

    理由：用户列表是**这一节自己的数据**（别的面板不消费它），
    而从设置页顶层传下来要穿过三层。`useSyncExternalStore` 的
    订阅本身很轻，让需要它的组件自己订阅更直接。

    ⚠️ 加载由 `SettingsPage` 统一发起（它已经有一套"进页面拉一次"的
       逻辑），这里只负责渲染三种状态。
  */
  const { users, status, error } = useSyncExternalStore(
    subscribeUsers,
    getUsersSnapshot,
    getUsersServerSnapshot,
  )

  if (status === 'loading' || status === 'idle') {
    return <p className="text-xs text-ink-muted">正在加载用户…</p>
  }
  if (status === 'error') {
    return (
      <p className="text-xs text-signal-risk">{error?.message ?? '用户列表加载失败。'}</p>
    )
  }

  return (
    <ul className="flex flex-col divide-y divide-line rounded-control border border-line">
      {users.map((u) => (
        <UserRow
          key={u.id}
          user={u}
          all={users}
          currentUserId={currentUserId}
          currentUserIsAdmin={currentUserIsAdmin}
        />
      ))}
    </ul>
  )
}

function UserRow({
  user,
  all,
  currentUserId,
  currentUserIsAdmin,
}: {
  user: ManagedUser
  all: ManagedUser[]
  currentUserId: string | null
  currentUserIsAdmin: boolean
}) {
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)

  const isSelf = user.id === currentUserId
  const deleteBlocked = deleteBlockedReason(user, all, currentUserId)
  const demoteBlocked = user.role === 'admin' ? demoteBlockedReason(user, all) : null

  const run = async (
    action: () => Promise<{ error: string } | { user: ManagedUser } | { ok: true }>,
  ) => {
    setBusy(true)
    setProblem(null)
    const result = await action()
    setBusy(false)
    if ('error' in result) setProblem(result.error)
  }

  return (
    <li className="flex flex-col gap-2 px-3 py-2.5">
      <div className="flex items-center gap-3">
        <span
          aria-hidden="true"
          className={cn(
            'flex h-7 w-7 shrink-0 items-center justify-center rounded-pill',
            user.role === 'admin' ? 'bg-accent-wash text-accent' : 'bg-surface-sunken text-ink-muted',
          )}
        >
          {user.role === 'admin' ? <ShieldCheck size={14} /> : <UserRound size={14} />}
        </span>

        <span className="flex min-w-0 flex-col">
          <span className="truncate text-xs text-ink">
            {user.username}
            {isSelf && <span className="ml-1.5 text-2xs text-ink-faint">（你）</span>}
          </span>
          <span className="text-2xs text-ink-faint">
            创建于 {formatRelativeTime(user.createdAt)}
          </span>
        </span>

        <span className="ml-auto flex items-center gap-2">
          <RoleBadge role={user.role} />

          {/*
            ⚠️ 只读用户看不到任何操作按钮 —— 后端会 403，而
               "点得到的按钮永远失败"是坏交互。见组件头部的说明：
               这是展示性的，不是安全边界。
          */}
          {currentUserIsAdmin && (
            <>
              {user.role === 'admin' ? (
                <button
                  type="button"
                  disabled={busy || demoteBlocked !== null}
                  title={demoteBlocked ?? '降级为只读'}
                  onClick={() => void run(() => updateUserRole(user.id, 'viewer'))}
                  className={actionClass}
                >
                  降为只读
                </button>
              ) : (
                <button
                  type="button"
                  disabled={busy}
                  title="提升为管理员"
                  onClick={() => void run(() => updateUserRole(user.id, 'admin'))}
                  className={actionClass}
                >
                  升为管理员
                </button>
              )}

              <button
                type="button"
                disabled={busy || deleteBlocked !== null}
                title={deleteBlocked ?? '删除这个用户'}
                onClick={() => void run(() => deleteUser(user.id))}
                className={cn(actionClass, 'hover:border-signal-risk/50 hover:text-signal-risk')}
              >
                <Trash2 size={13} aria-hidden="true" />
              </button>
            </>
          )}
        </span>
      </div>

      {/*
        ★ 灰按钮必须给理由。

        用户看到"降为只读"点不动，第一反应是"界面坏了" ——
        而真相是"他是最后一个管理员"。所以把原因**显示出来**，
        而不是只挂在 title 上（title 要悬停才看得到，而移动端没有悬停）。
      */}
      {currentUserIsAdmin && (demoteBlocked || deleteBlocked) && (
        <p className="pl-10 text-2xs text-signal-warn">
          {deleteBlocked ?? demoteBlocked}
        </p>
      )}

      {problem && <p className="pl-10 text-2xs text-signal-risk">{problem}</p>}
    </li>
  )
}

const actionClass = cn(
  'inline-flex h-7 items-center gap-1 rounded-control border border-line px-2 text-2xs',
  'text-ink-muted transition-ui duration-fast',
  'hover:border-line-strong hover:text-ink',
  /*
    禁用：降文字色 + 减淡边框，不压 opacity（后者会把文字压到 2.2:1）。
    `disabled:hover:` 是必需品 —— 禁用的 <button> 仍然匹配 `:hover`，
    不带 `disabled:` 前缀的话会被上面两条 `hover:` 抢走。
  */
  'disabled:cursor-not-allowed disabled:border-line/50 disabled:text-ink-faint disabled:hover:border-line/50 disabled:hover:text-ink-faint',
)

function RoleBadge({ role }: { role: UserRole }) {
  return (
    <span
      className={cn(
        'rounded-pill px-2 py-0.5 text-2xs',
        role === 'admin' ? 'bg-accent-wash text-accent' : 'bg-surface-sunken text-ink-muted',
      )}
    >
      {role === 'admin' ? '管理员' : '只读'}
    </span>
  )
}

// ─────────────────────────────────────────────
// 新增
// ─────────────────────────────────────────────

function AddUserForm({ onDone }: { onDone: () => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<UserRole>('viewer')
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)

  const submit = async () => {
    setProblem(null)

    // 前端先挡一道明显的（空用户名、弱口令），但**真正的校验在后端** ——
    // 这里只是为了少一次往返，不是为了替代它。
    if (!username.trim()) {
      setProblem('请填写用户名。')
      return
    }
    /*
      ⚠️ `checkPassword` 返回的是**对象**（`{strength, requirements, acceptable}`），
      不是问题列表 —— 我第一版按数组用它（`problems.length` / `.join()`），
      typecheck 直接报错。

      它的形状是刻意的：`requirements` 逐条带 `met` 标记，
      界面直接渲染那个列表（打勾/打叉），而不是自己重写一遍口令规则。
      所以这里用 `acceptable` 判断能不能提交，用 `requirements`
      里未达成的项拼出提示。
    */
    const check = checkPassword(password)
    if (!check.acceptable) {
      const unmet = check.requirements.filter((r) => !r.met).map((r) => r.label)
      setProblem(`口令强度不足：${unmet.join('、')}`)
      return
    }

    setBusy(true)
    const result = await createUser({ username: username.trim(), password, role })
    setBusy(false)

    if ('error' in result) {
      setProblem(result.error)
      return
    }
    onDone()
  }

  return (
    <div className="flex flex-col gap-3 rounded-control border border-line bg-surface-raised p-3">
      <Field label="用户名" htmlFor="new-user-name" help="登录时用。大小写不敏感。">
        <TextInput
          id="new-user-name"
          value={username}
          // ⚠️ `TextInput` 的 `onChange` 收的是**值**，不是事件 ——
          //    与原生 input 不同。写成 `(e) => setUsername(e.target.value)`
          //    会在运行时抛 `e.target is undefined`（typecheck 会先拦住）。
          onChange={setUsername}
          autoComplete="off"
          placeholder="例如 zhangsan"
        />
      </Field>

      <Field
        label="初始口令"
        htmlFor="new-user-pass"
        help="至少 12 位，含大小写字母与数字。告诉本人后请让他自己改。"
      >
        <TextInput
          id="new-user-pass"
          type="password"
          value={password}
          onChange={setPassword}
          autoComplete="new-password"
        />
      </Field>

      <Field label="角色" htmlFor="new-user-role">
        <div className="flex gap-1">
          {(['viewer', 'admin'] as const).map((r) => (
            <button
              key={r}
              type="button"
              aria-pressed={role === r}
              onClick={() => setRole(r)}
              className={cn(
                'inline-flex h-7 items-center rounded-pill border px-2.5 text-2xs transition-ui duration-fast',
                role === r
                  ? 'border-accent/50 bg-accent-wash text-ink'
                  : 'border-line text-ink-muted hover:border-line-strong hover:text-ink',
              )}
            >
              {r === 'admin' ? '管理员' : '只读'}
            </button>
          ))}
        </div>
      </Field>

      <p className="text-2xs text-ink-faint">
        {role === 'admin'
          ? '管理员能改设置、管模型服务、发起扫描 —— 包括对真实目标发包。'
          : '只读用户能看扫描记录、漏洞与报告，但不能发起扫描或改任何配置。'}
      </p>

      {problem && <p className="text-2xs text-signal-risk">{problem}</p>}

      <div className="flex gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => void submit()}
          className={cn(
            'inline-flex h-8 items-center rounded-control bg-accent px-3 text-xs text-white',
            /*
              禁用配色与 `Button` 的 primary 一致（`bg-accent-dim` + `ink-muted`），
              不压 opacity。这里没换基元是因为它自带 `h-9`，而这个面板是 h-8 的
              紧凑排布 —— 换就会把这一排撑高。
            */
            'transition-ui duration-fast hover:bg-accent-strong disabled:cursor-not-allowed disabled:bg-accent-dim disabled:text-ink-muted disabled:hover:bg-accent-dim',
          )}
        >
          {busy ? '正在创建…' : '创建'}
        </button>
        <DangerButton onClick={onDone}>取消</DangerButton>
      </div>
    </div>
  )
}
