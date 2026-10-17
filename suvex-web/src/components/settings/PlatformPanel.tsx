import { Drawer } from '@/components/ui/Drawer'
import { cn } from '@/lib/cn'
import {
  PLATFORM_STATUS_LABEL,
  PLATFORM_STATUS_TONE,
  activateCtf2Platform,
  deleteCtf2Platform,
  draftCtf2Platform,
  listCtf2Platforms,
  readCtf2Platform,
  switchToBuiltinPlatform,
  type Ctf2Platform,
} from '@/data/ctf2'
import { showToast } from '@/data/toast-store'
import { Check, Loader2, Plus, RotateCw, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { SettingsPanel } from './SaveBar'

/**
 * 平台接入面板。
 *
 * ## 它解决什么
 *
 * 之前"平台"是写死的 CTF2。比赛要换成赛事方给的 API（地址、认证头、
 * 端点路径、字段名全不同），所以这里有了一条"填个表 → agent 自己识别 →
 * 启用"的路。
 *
 * ## ★ 为什么"识别"是一次后台任务，而不是一次请求
 *
 * 一次识别最坏两分钟：探测几十个候选端点（还得限速）+ 最多三次大模型
 * 调用 + 每次都要真的取一次题来验证。挂在一次 HTTP 请求上**必然超时**，
 * 而超时之后后端还在跑 —— 用户看到"失败"、再点一次却"成功"，
 * 那种不一致看起来像个偶发 bug。
 *
 * 所以：POST 只受理（返回 202），进度从这里**轮询**。
 */

/** 轮询间隔。识别最坏两分钟，2.5 秒一次足够，也不会把后端打热。 */
const POLL_MS = 2500

const TONE_CLASS: Record<string, string> = {
  ok: 'border-signal-ok/40 text-signal-ok',
  warn: 'border-signal-warn/40 text-signal-warn',
  risk: 'border-signal-risk/40 text-signal-risk',
  idle: 'border-line text-ink-muted',
}

export function PlatformPanel() {
  const [platforms, setPlatforms] = useState<Ctf2Platform[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // 抽屉的两种用法：识别一个新平台 / 给已有平台补一把 key 来启用。
  const [dialog, setDialog] = useState<
    { mode: 'draft' } | { mode: 'activate'; platform: Ctf2Platform } | null
  >(null)

  const refresh = useCallback(async () => {
    try {
      setPlatforms(await listCtf2Platforms())
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '读取平台列表失败')
    }
  }, [])

  /*
    ★ 首次加载**内联**在 effect 里，而不是调用 `refresh`。

    两个原因：
    ① eslint 的 `set-state-in-effect` 会把"在 effect 里调用一个内部
       直接 setState 的函数"判成级联渲染（`refresh` 就是这么个函数）；
    ② 内联之后能顺手处理"组件已经卸载"这件事 —— 一次请求还没回来就
       切页面时，`setPlatforms` 会打到已卸载的组件上。
  */
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const rows = await listCtf2Platforms()
        if (!cancelled) {
          setPlatforms(rows)
          setError(null)
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : '读取平台列表失败')
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const handleUseBuiltin = async () => {
    setBusy(true)
    try {
      await switchToBuiltinPlatform()
      await refresh()
      showToast({ message: '已切回内置的 CTF2 驱动', tone: 'ok' })
    } catch (err) {
      showToast({
        message: err instanceof Error ? err.message : '切换失败',
        tone: 'risk',
      })
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async (item: Ctf2Platform) => {
    if (!item.id) return
    setBusy(true)
    try {
      await deleteCtf2Platform(item.id)
      await refresh()
      showToast({ message: '已删除这份平台配置', tone: 'ok' })
    } catch (err) {
      showToast({
        message: err instanceof Error ? err.message : '删除失败',
        tone: 'risk',
      })
    } finally {
      setBusy(false)
    }
  }

  return (
    <SettingsPanel
      id="platform"
      title="平台接入"
      description="CTF2 是内置的。比赛用别的 API 时，在这里填地址、凭据和接口文档，agent 会自己识别端点与字段并试跑验证。"
    >
      {error && (
        <p className="text-xs text-signal-risk">{error}</p>
      )}

      {platforms === null && !error && (
        <p className="flex items-center gap-1.5 text-xs text-ink-muted">
          <Loader2 size={12} className="animate-spin" aria-hidden="true" />
          正在读取…
        </p>
      )}

      {platforms && (
        <ul className="space-y-2">
          {platforms.map((item) => (
            <li
              key={item.id ?? item.slug}
              className={cn(
                'flex flex-wrap items-center gap-2 rounded-sm border p-2.5 transition-ui duration-fast',
                item.active ? 'border-accent/50 bg-accent/5' : 'border-line',
              )}
            >
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="truncate text-xs text-ink">{item.displayName}</span>
                  <span
                    className={cn(
                      'rounded-xs border px-1 text-2xs',
                      TONE_CLASS[PLATFORM_STATUS_TONE[item.status]],
                    )}
                  >
                    {PLATFORM_STATUS_LABEL[item.status]}
                  </span>
                  {item.active && (
                    <span className="inline-flex items-center gap-0.5 rounded-xs border border-accent/40 px-1 text-2xs text-accent-strong">
                      <Check size={10} strokeWidth={3} aria-hidden="true" />
                      使用中
                    </span>
                  )}
                  {item.attempts > 1 && (
                    <span className="text-2xs text-ink-faint">试了 {item.attempts} 次</span>
                  )}
                </div>
                {item.baseUrl && (
                  <p className="mt-0.5 truncate text-2xs text-ink-faint">{item.baseUrl}</p>
                )}
                {item.lastError && (
                  <p className="mt-0.5 text-2xs text-signal-risk">{item.lastError}</p>
                )}
              </div>

              <div className="flex shrink-0 items-center gap-1.5">
                {!item.id && (
                  <button
                    type="button"
                    disabled={busy || item.active}
                    onClick={() => {
                      void handleUseBuiltin()
                    }}
                    className={cn(
                      'h-7 rounded-sm border border-line px-2 text-2xs text-ink-muted',
                      'transition-ui duration-fast hover:border-line-strong hover:text-ink',
                      // 禁用降文字色 + 减淡边框，不压 opacity（见 Button.tsx 的 VARIANT_DISABLED）
                      'disabled:cursor-not-allowed disabled:border-line/50 disabled:text-ink-faint disabled:hover:border-line/50 disabled:hover:text-ink-faint',
                    )}
                  >
                    {item.active ? '当前使用' : '切回这个'}
                  </button>
                )}

                {item.id && !item.active && item.status === 'verified' && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => setDialog({ mode: 'activate', platform: item })}
                    className={cn(
                      'h-7 rounded-sm border border-accent/40 px-2 text-2xs text-accent-strong',
                      'transition-ui duration-fast hover:border-accent',
                      'disabled:cursor-not-allowed disabled:border-accent/20 disabled:text-ink-faint disabled:hover:border-accent/20 disabled:hover:text-ink-faint',
                    )}
                  >
                    启用
                  </button>
                )}

                {item.id && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => {
                      void handleDelete(item)
                    }}
                    title="删除这份配置（不影响账号里的凭据）"
                    className={cn(
                      'inline-flex h-7 items-center gap-1 rounded-sm border border-line px-2 text-2xs text-ink-faint',
                      'transition-ui duration-fast hover:border-signal-risk hover:text-signal-risk',
                      // 本来就在 faint 档，禁用只需不吃 hover
                      'disabled:cursor-not-allowed disabled:hover:border-line disabled:hover:text-ink-faint',
                    )}
                  >
                    <Trash2 size={11} aria-hidden="true" />
                    删除
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      <button
        type="button"
        onClick={() => setDialog({ mode: 'draft' })}
        className={cn(
          'inline-flex h-8 items-center gap-1.5 rounded-sm border border-line px-2.5 text-xs text-ink-muted',
          'transition-ui duration-fast hover:border-line-strong hover:text-ink',
        )}
      >
        <Plus size={12} strokeWidth={2.4} aria-hidden="true" />
        识别一个新平台
      </button>

      {dialog !== null && (
        <PlatformDialog
          mode={dialog.mode}
          platform={dialog.mode === 'activate' ? dialog.platform : undefined}
          onClose={() => setDialog(null)}
          onDone={() => {
            setDialog(null)
            void refresh()
          }}
        />
      )}
    </SettingsPanel>
  )
}

/**
 * 识别 / 启用 的抽屉。
 *
 * ## 一轮识别在后端发生什么
 *
 *   ① probe：真的去打几十个候选端点，找出认证方式与题目列表
 *   ② 大模型：读「接口文档 + 实测报告」生成一份声明式配置
 *   ③ 校验：配置缺项/类型不对 → 把错误回喂，重来
 *   ④ 试跑：用那份配置**真的取一次题**，取不到就带着错误回到 ②
 *
 * 所以界面上要能看出"进行到哪一步了" —— 只说"识别中"的话，用户在两分钟
 * 里唯一能做的就是反复刷新。
 */
function PlatformDialog({
  mode,
  platform,
  onClose,
  onDone,
}: {
  mode: 'draft' | 'activate'
  platform?: Ctf2Platform
  onClose: () => void
  onDone: () => void
}) {
  const [baseUrl, setBaseUrl] = useState('')
  const [accessKey, setAccessKey] = useState('')
  const [document, setDocument] = useState('')
  const [draft, setDraft] = useState<Ctf2Platform | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  /*
    ★ 轮询：只在 `isBuilding` 为真时开着。

    ⚠️ 依赖写成 `draft?.id` 与 `draft?.isBuilding` 两个标量，而不是整个
    `draft` 对象 —— 后者会让每次拿到新结果都重建一次定时器，而重建本身
    又会触发一次请求，形成"越轮询越快"的循环。
  */
  const draftId = draft?.id ?? null
  const building = draft?.isBuilding ?? false
  useEffect(() => {
    if (!draftId || !building) return
    const timer = window.setInterval(() => {
      void readCtf2Platform(draftId)
        .then(setDraft)
        .catch(() => {
          // 轮询失败不打断：下一次还会试。真正的失败由 status=failed 表达。
        })
    }, POLL_MS)
    return () => window.clearInterval(timer)
  }, [draftId, building])

  const handleStart = async () => {
    setBusy(true)
    setError(null)
    try {
      const created = await draftCtf2Platform({ baseUrl, accessKey, document })
      setDraft(created)
    } catch (err) {
      setError(err instanceof Error ? err.message : '发起识别失败')
    } finally {
      setBusy(false)
    }
  }

  const handleActivate = async () => {
    const id = platform?.id ?? draft?.id
    if (!id) return
    setBusy(true)
    setError(null)
    try {
      await activateCtf2Platform(id, { accessKey })
      showToast({ message: '已启用这个平台', tone: 'ok' })
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : '启用失败')
    } finally {
      setBusy(false)
    }
  }

  const target = mode === 'activate' ? platform : (draft ?? undefined)
  const canActivate = target?.status === 'verified' && target.id !== null

  return (
    <Drawer
      open
      onClose={onClose}
      labelledBy="platform-dialog-title"
      title={mode === 'draft' ? '识别一个新平台' : `启用「${platform?.displayName ?? ''}」`}
      className="max-w-xl"
      footer={
        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="h-8 rounded-sm border border-line px-3 text-xs text-ink-muted transition-ui duration-fast hover:border-line-strong hover:text-ink"
          >
            关闭
          </button>
          {mode === 'draft' && !draft && (
            <button
              type="button"
              disabled={busy || !baseUrl.trim() || !accessKey.trim()}
              onClick={() => {
                void handleStart()
              }}
              className={cn(
                'inline-flex h-8 items-center gap-1.5 rounded-sm bg-accent px-3 text-xs text-canvas',
                'transition-ui duration-fast hover:bg-accent-strong',
                // 禁用换 accent-dim 底 + ink-muted 字（暗 4.94:1 / 亮 5.70:1），与 Button primary 一致
                'disabled:cursor-not-allowed disabled:bg-accent-dim disabled:text-ink-muted disabled:hover:bg-accent-dim',
              )}
            >
              {busy ? (
                <Loader2 size={12} className="animate-spin" aria-hidden="true" />
              ) : (
                <RotateCw size={12} aria-hidden="true" />
              )}
              开始识别
            </button>
          )}
          {(mode === 'activate' || draft) && (
            <button
              type="button"
              disabled={busy || !canActivate || !accessKey.trim()}
              onClick={() => {
                void handleActivate()
              }}
              className={cn(
                'inline-flex h-8 items-center gap-1.5 rounded-sm bg-accent px-3 text-xs text-canvas',
                'transition-ui duration-fast hover:bg-accent-strong',
                'disabled:cursor-not-allowed disabled:bg-accent-dim disabled:text-ink-muted disabled:hover:bg-accent-dim',
              )}
            >
              {busy && <Loader2 size={12} className="animate-spin" aria-hidden="true" />}
              启用并保存凭据
            </button>
          )}
        </div>
      }
    >
      <div className="space-y-3">
        {mode === 'draft' && (
          <>
            <label className="block" htmlFor="platform-base-url">
              <span className="mb-1 block text-xs text-ink">平台地址</span>
              <input
                id="platform-base-url"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://比赛平台（只填域名也行，路径前缀 agent 会自己补）"
                className="h-9 w-full rounded-sm border border-line bg-surface-sunken px-2.5 text-sm text-ink transition-ui duration-fast focus:outline-none focus:ring-2 focus:ring-accent/40"
              />
            </label>

            <label className="block" htmlFor="platform-doc">
              <span className="mb-1 block text-xs text-ink">
                接口文档<span className="ml-1 text-2xs text-ink-faint">（可选，但强烈建议）</span>
              </span>
              <textarea
                id="platform-doc"
                value={document}
                onChange={(e) => setDocument(e.target.value)}
                rows={8}
                placeholder="把赛事方给的 API 文档整段粘进来（Markdown / OpenAPI / curl 示例都行）。没有文档时 agent 只能靠实测推断，成功率会低一些。"
                className="w-full rounded-sm border border-line bg-surface-sunken p-2.5 font-mono text-2xs text-ink transition-ui duration-fast focus:outline-none focus:ring-2 focus:ring-accent/40"
              />
            </label>
          </>
        )}

        <label className="block" htmlFor="platform-access-key">
          <span className="mb-1 block text-xs text-ink">
            AccessKey
            <span className="ml-1 text-2xs text-ink-faint">
              （只在内存里过一遍，识别阶段不落库）
            </span>
          </span>
          <input
            id="platform-access-key"
            type="password"
            value={accessKey}
            onChange={(e) => setAccessKey(e.target.value)}
            className="h-9 w-full rounded-sm border border-line bg-surface-sunken px-2.5 text-sm text-ink transition-ui duration-fast focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
        </label>

        {error && <p className="text-xs text-signal-risk">{error}</p>}

        {draft && (
          <div className="space-y-1.5 rounded-sm border border-line p-2.5">
            <div className="flex items-center gap-1.5">
              {draft.isBuilding && (
                <Loader2 size={12} className="animate-spin text-ink-muted" aria-hidden="true" />
              )}
              <span className="text-xs text-ink">
                {draft.isBuilding
                  ? '正在识别…（探测端点 → 生成配置 → 试跑取题，最坏两分钟）'
                  : draft.displayName}
              </span>
              <span
                className={cn(
                  'rounded-xs border px-1 text-2xs',
                  TONE_CLASS[PLATFORM_STATUS_TONE[draft.status]],
                )}
              >
                {PLATFORM_STATUS_LABEL[draft.status]}
              </span>
            </div>

            {draft.status === 'verified' && (
              <p className="text-2xs text-ink-muted">
                已经试跑通过：这份配置**真的取到过题目**。点下面的按钮启用它。
              </p>
            )}

            {draft.lastError && (
              <div className="space-y-1">
                <p className="text-2xs text-signal-risk">{draft.lastError}</p>
                <p className="text-2xs text-ink-faint">
                  配置生成过程中的每次失败都会带着原因重试；上面是最后一次的原因。
                </p>
              </div>
            )}

            {draft.baseUrl && (
              <p className="truncate text-2xs text-ink-faint">{draft.baseUrl}</p>
            )}
          </div>
        )}
      </div>
    </Drawer>
  )
}

export default PlatformPanel
