import { PageHeader } from '@/components/layout/AppShell'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Drawer } from '@/components/ui/Drawer'
import { EmptyState } from '@/components/ui/EmptyState'
import { ErrorState } from '@/components/ui/ErrorState'
import { Skeleton } from '@/components/ui/Skeleton'
import { useAuth } from '@/data/auth-store'
import {
  createSkill,
  deleteSkill,
  ensureSkillsLoaded,
  fetchSkill,
  filterSkillGroups,
  loadSkills,
  loadsOf,
  updateSkill,
  useSkills,
  usageSummary,
  validateDraft,
  type SkillSummary,
} from '@/data/skills'
import { cn } from '@/lib/cn'
import {
  AlertCircle,
  BookOpen,
  Check,
  Eye,
  Loader2,
  Lock,
  MousePointerClick,
  Pencil,
  Plus,
  Search,
  Trash2,
  TrendingUp,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'

/**
 * 技能库页（v0.88）。
 *
 * ## 这一页回答的两个问题
 *
 * 1.「模型手边有哪些速查卡？」—— 清单
 * 2.「哪几篇真的被拉下来用过？」—— 调用统计
 *
 * 第 2 问是这一页独立存在的核心理由。v0.87 把它塞在工具管理页底部时，
 * 它只是一份**只读清单** —— 看得见有哪些技能，但看不出任何一篇有没有
 * 被用过，也改不了。而技能正文直接进模型上下文，是**能改变智能体行为**
 * 的东西，部署者必须能编辑它、也必须能看出哪一篇是死文本。
 *
 * ## 为什么单独一页而不是工具页的一个区块
 *
 * 判据与「工具管理 vs 设置」那条一致：**它是不是另一件事**。
 *
 * 工具页回答「能力边界」（13 个固定工具 + 外部 MCP 连接，都是**开关**）。
 * 技能页回答「这些文本写了什么、模型用过没有」（31 篇正文，是**编辑**）。
 * 前者的操作是拨开关，后者是写文章；前者风险是"关错了变弱"，后者是
 * "改错了模型照着错的做法走"。两件事的操作节奏与风险面都不同。
 *
 * ## 版面：左边清单、右边详情（主从）
 *
 * 桌面端两栏：清单可筛选可滚动，详情常驻右侧。窄屏退化成清单 + 抽屉。
 * 这个形状与「漏洞发现」页一致 —— 分类浏览 + 逐条细看的场景，
 * 主从不该在两种页面里长得不一样。
 *
 * ## 三种"不可改"必须说清是哪一种
 *
 * - 内置技能：随代码分发，**界面上不给编辑/删除按钮**（不是点了才报错）
 * - 只读角色：能看，按钮禁用并说明原因
 * - 统计没拿到：清单照常显示，只说"统计没拿到"，不把整页标成错误
 */

const CATEGORY_LABEL: Record<string, string> = {
  analysis: '分析方法',
  protocols: '协议',
  reconnaissance: '信息收集',
  tooling: '工具用法',
  vulnerabilities: '漏洞类型',
}

function categoryLabel(category: string): string {
  return CATEGORY_LABEL[category] ?? category
}

export function SkillsPage() {
  const { list, usage, status, error, usageTruncated } = useSkills()
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'

  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<string | null>(null)

  /** 编辑器状态：`null` = 关闭；否则是正在编辑/新建的内容。 */
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [saving, setSaving] = useState(false)
  const [editorError, setEditorError] = useState<string | null>(null)

  /** 待删除的技能（非空时弹确认框）。 */
  const [pendingDelete, setPendingDelete] = useState<SkillSummary | null>(null)
  const [deleting, setDeleting] = useState(false)

  useEffect(() => {
    ensureSkillsLoaded()
  }, [])

  const filtered = useMemo(() => filterSkillGroups(list.categories, query), [list.categories, query])
  const summary = useMemo(() => usageSummary(list, usage), [list, usage])

  /*
    ★ 选中项跟着清单走：删掉当前选中的技能之后，`selected` 会指向一个
      已经不存在的名字。这里回落到第一篇（而不是留着旧的空详情），
      否则界面会停在一篇"已删除"的技能上继续显示它的内容。
  */
  const visibleSkills = useMemo(
    () => filtered.flatMap((group) => group.skills),
    [filtered],
  )
  const activeName =
    selected && visibleSkills.some((skill) => skill.name === selected)
      ? selected
      : (visibleSkills[0]?.name ?? null)
  const activeSkill = visibleSkills.find((skill) => skill.name === activeName) ?? null

  function openCreate() {
    setEditor({ mode: 'create', name: '', category: '', description: '', body: '' })
    setEditorError(null)
  }

  async function openEdit(skill: SkillSummary) {
    setEditorError(null)
    setEditor({
      mode: 'edit',
      name: skill.name,
      category: skill.category,
      description: skill.description,
      // 正文要现拉 —— 清单里没有（可能几百 KB，不该随清单下发）。
      body: '',
      loading: true,
    })
    try {
      const detail = await fetchSkill(skill.name)
      setEditor((prev) =>
        prev && prev.name === skill.name
          ? { ...prev, body: detail.body, loading: false }
          : prev,
      )
    } catch (err) {
      setEditor((prev) =>
        prev && prev.name === skill.name
          ? { ...prev, loading: false, body: '' }
          : prev,
      )
      setEditorError(err instanceof Error ? err.message : '拿不到正文。')
    }
  }

  async function handleSave() {
    if (!editor) return
    const draft = {
      name: editor.name,
      category: editor.category,
      description: editor.description,
      body: editor.body,
    }
    /* ★ 前端先校验一遍：不是为了安全（边界在后端与文件层），
       是为了不让人白填一遍表单才被告知名字里不能有大写。 */
    const invalid = validateDraft(draft)
    if (invalid) {
      setEditorError(invalid)
      return
    }

    setSaving(true)
    setEditorError(null)
    try {
      if (editor.mode === 'create') {
        await createSkill(draft)
        setSelected(draft.name)
      } else {
        await updateSkill(draft.name, {
          description: draft.description,
          body: draft.body,
        })
        setSelected(draft.name)
      }
      setEditor(null)
    } catch (err) {
      setEditorError(err instanceof Error ? err.message : '保存失败。')
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete() {
    if (!pendingDelete) return
    setDeleting(true)
    try {
      await deleteSkill(pendingDelete.name)
      if (selected === pendingDelete.name) setSelected(null)
      setPendingDelete(null)
    } catch (err) {
      setEditorError(err instanceof Error ? err.message : '删除失败。')
      setPendingDelete(null)
    } finally {
      setDeleting(false)
    }
  }

  return (
    <>
      <PageHeader navId="skills" />

      <div className="px-gutter py-bay">
        {status === 'loading' ? (
          <div className="flex flex-col gap-bay">
            <Skeleton className="h-20" />
            <Skeleton className="h-64" />
          </div>
        ) : status === 'error' ? (
          <ErrorState
            title="拿不到技能库"
            error={error ?? new Error('后端没有响应。')}
            onRetry={() => void loadSkills()}
          />
        ) : (
          <div className="flex flex-col gap-panel">
            {/* ── 统计摘要 ─────────────────────────────── */}
            <StatsBar
              total={list.total}
              usedCount={summary.usedCount}
              totalLoads={summary.totalLoads}
              topName={summary.topName}
              topLoads={summary.topLoads}
              truncated={usageTruncated}
              sampledEvents={usage.sampledEvents}
              usageMissing={error !== null && usage.items.length === 0}
              usageMessage={error?.message}
            />

            {!isAdmin && (
              <div className="flex items-start gap-tight rounded-sm border border-line px-3 py-2 text-[13px] text-ink-muted">
                <Eye className="mt-0.5 size-3.5 shrink-0" strokeWidth={1.75} />
                <span>
                  你现在的角色是<b>只读</b>：能看全部技能与调用统计，但改不了。
                  需要新增或编辑请找管理员。
                </span>
              </div>
            )}

            {/* ── 主体：主从两栏 ────────────────────────
                ★ 两栏必须**底边对齐**，否则画面会坏：
                  左列清单限了高、内部滚动，右列详情如果让内容自己撑，
                  右栏就会比左栏长，落到下面那片空白里 —— 看起来像"右
                  边的框压住了左边的框"，实测在 1440×1000 下重叠 780px。

                  所以这里把"可用高度"算一次、给两栏一起用：
                  小屏用 dvh 估算（页面还能整页滚），lg 以上用
                  calc(100dvh - 320px)（320px = 顶栏 + 标题 + 统计条 + 边距）。
                ⚠️ 320 这个数不是随手写的：改顶部区域的高度时**必须**同步
                  改这里，否则两栏会在底部对不齐。 */}
            <div className="grid items-start gap-panel lg:grid-cols-[minmax(280px,380px)_1fr]">
              {/* 左：清单 */}
              <section className="flex min-w-0 flex-col rounded-md border border-line bg-surface">
                <header className="flex items-center justify-between gap-tight border-b border-line px-3 py-2">
                  <h2 className="text-[13px] font-medium text-ink">
                    全部技能
                    <span className="ml-1.5 font-normal text-ink-faint">{list.total}</span>
                  </h2>
                  <Button
                    size="sm"
                    onClick={openCreate}
                    disabled={!isAdmin}
                    title={isAdmin ? '新建一篇自定义技能' : '只读角色不能新建'}
                  >
                    <Plus className="size-3.5" strokeWidth={2} />
                    新建
                  </Button>
                </header>

                <div className="border-b border-line px-3 py-2">
                  <div className="relative">
                    <Search
                      className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-ink-faint"
                      strokeWidth={1.75}
                    />
                    <input
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder="搜索名字 / 分类 / 描述"
                      className="h-8 w-full rounded-sm border border-line bg-surface pr-2 pl-7 text-[13px] text-ink outline-none focus:border-line-accent"
                    />
                  </div>
                </div>

                {list.total === 0 ? (
                  <EmptyState
                    icon={BookOpen}
                    title="没有技能"
                    description="技能包随代码分发在 prompts/skills/ 下。这里是空的，说明打包时漏了 —— 不影响工具可用。"
                  />
                ) : visibleSkills.length === 0 ? (
                  <p className="px-3 py-6 text-[13px] text-ink-muted">
                    没有匹配「{query}」的技能。
                  </p>
                ) : (
                  <ul className="max-h-[60dvh] overflow-y-auto lg:max-h-[calc(100dvh-320px)]">
                    {filtered.map((group) => (
                      <li key={group.category}>
                        <h3 className="border-b border-line/60 bg-surface-raised/30 px-3 py-1.5 text-[11px] tracking-wide text-ink-faint uppercase">
                          {categoryLabel(group.category)}
                          <span className="ml-1 normal-case">（{group.skills.length}）</span>
                        </h3>
                        <ul>
                          {group.skills.map((skill) => (
                            <SkillRow
                              key={skill.name}
                              skill={skill}
                              loads={loadsOf(usage, skill.name)?.loads ?? 0}
                              active={skill.name === activeName}
                              onSelect={() => setSelected(skill.name)}
                            />
                          ))}
                        </ul>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              {/* 右：详情
                  ★ overflow-y-auto 是必须的：内置技能的正文可能有几百 KB
                    （虽然是按需取的），自定义技能也可以很长。没有它，
                    一篇长技能会把整行撑到几千像素高。 */}
              <section className="min-w-0 overflow-y-auto rounded-md border border-line bg-surface lg:max-h-[calc(100dvh-320px)]">
                {activeSkill ? (
                  <SkillDetailPane
                    skill={activeSkill}
                    loads={loadsOf(usage, activeSkill.name)}
                    isAdmin={isAdmin}
                    onEdit={() => void openEdit(activeSkill)}
                    onDelete={() => setPendingDelete(activeSkill)}
                    error={editorError}
                  />
                ) : (
                  <EmptyState
                    icon={MousePointerClick}
                    title="选一篇技能看详情"
                    description="左边是全部技能。内置技能随代码分发、只读；自定义的可以改、可以删。"
                  />
                )}
              </section>
            </div>
          </div>
        )}
      </div>

      <SkillEditor
        state={editor}
        saving={saving}
        error={editorError}
        onChange={(next) => setEditor(next)}
        onSave={() => void handleSave()}
        onClose={() => {
          setEditor(null)
          setEditorError(null)
        }}
      />

      <ConfirmDialog
        open={pendingDelete !== null}
        title="删除这篇技能？"
        description={
          pendingDelete
            ? `将删除「${pendingDelete.name}」的正文文件。删掉之后模型再也加载不到它 —— 如果它正在被某个提示词引用，那个引用会变成"没有这个技能"。`
            : ''
        }
        confirmLabel="删除"
        busy={deleting}
        onConfirm={() => void handleDelete()}
        onCancel={() => setPendingDelete(null)}
      />
    </>
  )
}

// ─────────────────────────────────────────────
// 统计摘要
// ─────────────────────────────────────────────

function StatsBar({
  total,
  usedCount,
  totalLoads,
  topName,
  topLoads,
  truncated,
  sampledEvents,
  usageMissing,
  usageMessage,
}: {
  total: number
  usedCount: number
  totalLoads: number
  topName: string | null
  topLoads: number
  truncated: boolean
  sampledEvents: number
  usageMissing: boolean
  usageMessage?: string
}) {
  return (
    <section className="rounded-md border border-line bg-surface px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <Metric label="技能总数" value={total} />
        <Metric
          label="被加载过"
          value={usedCount}
          hint={total > 0 ? `${Math.round((usedCount / total) * 100)}%` : undefined}
        />
        <Metric label="累计加载" value={totalLoads} />
        {topName && (
          <div className="flex items-center gap-1.5 text-[13px]">
            <TrendingUp className="size-3.5 text-ink-faint" strokeWidth={1.75} />
            <span className="text-ink-muted">最常用</span>
            <code className="font-mono text-ink">{topName}</code>
            <span className="text-ink-faint">{topLoads} 次</span>
          </div>
        )}
      </div>

      {/*
        ★ 措辞必须说清这个数证明什么。它是「被 load_skill 拉过」，
          不是「有用」「命中」「采纳」—— 后者从事件流里推不出来。
          把它写大一点很好看，但它就是编的。
      */}
      <p className="mt-2 max-w-[80ch] text-[12px] leading-relaxed text-ink-faint">
        统计口径：模型调用 <code className="font-mono">load_skill</code> 的次数。
        它证明「请过这一篇」，不证明「读进去了」或「照做了」。
        {truncated && (
          <>
            {' '}
            ⚠️ 只统计了最近 {sampledEvents.toLocaleString('zh-CN')} 条事件，
            <b>更早的可能没算进去</b>。
          </>
        )}
        {usageMissing && (
          <span className="text-signal-risk">
            {' '}
            统计没拿到{usageMessage ? `（${usageMessage}）` : ''} —— 清单是准的，只有这里的数缺失。
          </span>
        )}
      </p>
    </section>
  )
}

function Metric({ label, value, hint }: { label: string; value: number; hint?: string }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-[12px] text-ink-muted">{label}</span>
      <span className="font-mono text-lg text-ink">{value}</span>
      {hint && <span className="text-[11px] text-ink-faint">{hint}</span>}
    </div>
  )
}

// ─────────────────────────────────────────────
// 清单行
// ─────────────────────────────────────────────

function SkillRow({
  skill,
  loads,
  active,
  onSelect,
}: {
  skill: SkillSummary
  loads: number
  active: boolean
  onSelect: () => void
}) {
  return (
    <li className="border-b border-line/60 last:border-b-0">
      <button
        type="button"
        onClick={onSelect}
        className={cn(
          'flex w-full items-start gap-2 px-3 py-2 text-left transition-ui duration-fast',
          active ? 'bg-surface-raised' : 'hover:bg-surface-raised/60',
        )}
      >
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <code className="truncate font-mono text-[13px] text-ink">{skill.name}</code>
            {skill.source === 'builtin' ? (
              /* ★ 图标必须有可读的名字：读屏软件面对一个裸 <svg> 只会
                 报"图形"，而这里表达的是"内置/只读"这个**状态**。
                 用 aria-label 而不是 title：title 要悬停才出，键盘与
                 读屏都拿不到。 */
              <Lock
                className="size-3 shrink-0 text-ink-faint"
                strokeWidth={2}
                role="img"
                aria-label="内置技能，只读"
              />
            ) : (
              <span className="shrink-0 rounded-xs border border-line-accent px-1 text-[10px] text-ink-muted">
                自定义
              </span>
            )}
          </div>
          <p className="mt-0.5 line-clamp-2 text-[12px] leading-relaxed text-ink-muted">
            {skill.description || '（没有写描述）'}
          </p>
        </div>

        {/*
          ★ 0 次也要显示，但用 `text-ink-faint` 而不是藏起来。
            「这一篇从没被用过」是这一页最有用的信息之一 ——
            它是一条死文本，占着上下文预算但不产生价值。
        */}
        <span
          className={cn(
            'shrink-0 font-mono text-[11px]',
            loads > 0 ? 'text-ink-muted' : 'text-ink-faint',
          )}
          title={loads > 0 ? `被加载 ${loads} 次` : '从没被加载过'}
        >
          {loads}
        </span>
      </button>
    </li>
  )
}

// ─────────────────────────────────────────────
// 详情
// ─────────────────────────────────────────────

function SkillDetailPane({
  skill,
  loads,
  isAdmin,
  onEdit,
  onDelete,
  error,
}: {
  skill: SkillSummary
  loads: { loads: number; lastLoadedAt: string | null } | null
  isAdmin: boolean
  onEdit: () => void
  onDelete: () => void
  error: string | null
}) {
  const builtin = skill.source === 'builtin'

  return (
    <div className="flex h-full flex-col">
      <header className="flex flex-wrap items-start justify-between gap-tight border-b border-line px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <code className="font-mono text-base text-ink">{skill.name}</code>
            <span className="rounded-xs border border-line px-1.5 py-0.5 text-[11px] text-ink-muted">
              {categoryLabel(skill.category)}
            </span>
          </div>
          <p className="mt-1 max-w-[68ch] text-[13px] leading-relaxed text-ink-muted">
            {skill.description || '（没有写描述）'}
          </p>
        </div>

        <div className="flex items-center gap-tight">
          {/*
            ★ 内置技能**不画**编辑/删除按钮。
              画一个禁用按钮再告诉用户"不许"，不如一开始就不画 ——
              界面上每个灰掉的按钮都在说"这里有东西你碰不到"，
              而内置技能根本没有可编辑的副本。
          */}
          {builtin ? (
            <span className="flex items-center gap-1 text-[12px] text-ink-faint">
              <Lock className="size-3" strokeWidth={2} />
              内置 · 只读
            </span>
          ) : (
            <>
              <Button size="sm" variant="ghost" onClick={onEdit} disabled={!isAdmin}>
                <Pencil className="size-3.5" strokeWidth={1.75} />
                编辑
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={onDelete}
                disabled={!isAdmin}
                className="text-signal-risk"
              >
                <Trash2 className="size-3.5" strokeWidth={1.75} />
                删除
              </Button>
            </>
          )}
        </div>
      </header>

      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-b border-line px-4 py-2 text-[12px] text-ink-muted">
        <span>
          被加载 <b className="font-mono text-ink">{loads?.loads ?? 0}</b> 次
        </span>
        {loads?.lastLoadedAt ? (
          <span>
            最近一次 {new Date(loads.lastLoadedAt).toLocaleString('zh-CN')}
          </span>
        ) : (
          <span className="text-ink-faint">从没被加载过 —— 这一篇目前是死文本</span>
        )}
      </div>

      {error && (
        <div className="flex items-start gap-tight border-b border-line px-4 py-2 text-[13px] text-signal-risk">
          <AlertCircle className="mt-0.5 size-3.5 shrink-0" strokeWidth={1.75} />
          <span>{error}</span>
        </div>
      )}

      <div className="flex-1 overflow-y-auto px-4 py-3">
        {builtin ? (
          /*
            ★ 内置技能不显示正文 —— 不是没做，是**后端就不回**（可能几百 KB）。
              这里说清为什么，而不是留一片空白让人以为加载失败。
          */
          <p className="max-w-[72ch] text-[13px] leading-relaxed text-ink-muted">
            内置技能的正文随代码分发，页面上不回显全文（它可能有几百 KB，
            而回显一个改不了的文本没有用）。模型用{' '}
            <code className="font-mono">load_skill({'{'}"name": "{skill.name}"{'}'})</code>{' '}
            按需拉取。它是随代码分发的，改不了，也没法用同名的自定义技能顶掉
            （内置优先）—— 想要自己的版本，换个名字新建一篇。
          </p>
        ) : (
          <p className="max-w-[72ch] text-[13px] leading-relaxed text-ink-muted">
            这是一篇<b>自定义</b>技能，正文存在部署目录下。点「编辑」看全文并修改 ——
            正文会直接进模型上下文，改错了模型就会照着错的做法走。
          </p>
        )}
      </div>

      <footer className="border-t border-line px-4 py-2.5 text-[12px] leading-relaxed text-ink-faint">
        改动在<b>下一次任务启动时</b>生效 —— 已经跑起来的任务用的是它启动那一刻的技能库。
      </footer>
    </div>
  )
}

// ─────────────────────────────────────────────
// 编辑器
// ─────────────────────────────────────────────

interface EditorState {
  mode: 'create' | 'edit'
  name: string
  category: string
  description: string
  body: string
  loading?: boolean
}

function SkillEditor({
  state,
  saving,
  error,
  onChange,
  onSave,
  onClose,
}: {
  state: EditorState | null
  saving: boolean
  error: string | null
  onChange: (next: EditorState) => void
  onSave: () => void
  onClose: () => void
}) {
  const open = state !== null
  const isCreate = state?.mode === 'create'

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={isCreate ? '新建技能' : `编辑 ${state?.name ?? ''}`}
      footer={
        <div className="flex items-center justify-end gap-tight">
          <Button variant="ghost" onClick={onClose} disabled={saving}>
            取消
          </Button>
          <Button onClick={onSave} disabled={saving || state?.loading}>
            {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />}
            保存
          </Button>
        </div>
      }
    >
      {state && (
        <div className="flex flex-col gap-panel">
          {error && (
            <div className="flex items-start gap-tight rounded-sm border border-line px-3 py-2 text-[13px] text-signal-risk">
              <AlertCircle className="mt-0.5 size-3.5 shrink-0" strokeWidth={1.75} />
              <span className="leading-relaxed">{error}</span>
            </div>
          )}

          <label className="flex flex-col gap-1">
            <span className="text-[12px] text-ink-muted">
              技能名 <span className="text-ink-faint">（模型 load_skill 时写的就是它）</span>
            </span>
            <input
              value={state.name}
              onChange={(event) => onChange({ ...state, name: event.target.value })}
              /* ★ 编辑时名字不可改：它同时是文件名和模型调用时的标识符，
                 改名等于"删一个建一个"。要改就删了重建。 */
              disabled={!isCreate}
              placeholder="例如 kerberos-preauth"
              className="h-9 rounded-sm border border-line bg-surface px-2 font-mono text-[13px] text-ink outline-none focus:border-line-accent disabled:opacity-60"
            />
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-[12px] text-ink-muted">
              分类 <span className="text-ink-faint">（会变成目录名，编辑时不可改）</span>
            </span>
            <input
              value={state.category}
              onChange={(event) => onChange({ ...state, category: event.target.value })}
              disabled={!isCreate}
              placeholder="例如 vulnerabilities"
              className="h-9 rounded-sm border border-line bg-surface px-2 font-mono text-[13px] text-ink outline-none focus:border-line-accent disabled:opacity-60"
            />
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-[12px] text-ink-muted">
              一句话描述 <span className="text-ink-faint">（进「可用技能清单」，模型据此判断该不该加载）</span>
            </span>
            <input
              value={state.description}
              onChange={(event) => onChange({ ...state, description: event.target.value })}
              placeholder="什么时候该拉这一篇"
              className="h-9 rounded-sm border border-line bg-surface px-2 text-[13px] text-ink outline-none focus:border-line-accent"
            />
          </label>

          <label className="flex flex-1 flex-col gap-1">
            <span className="text-[12px] text-ink-muted">
              正文（Markdown）
              {state.loading && (
                <span className="ml-1 text-ink-faint">
                  <Loader2 className="inline size-3 animate-spin" /> 正在读…
                </span>
              )}
            </span>
            <textarea
              value={state.body}
              onChange={(event) => onChange({ ...state, body: event.target.value })}
              rows={18}
              spellCheck={false}
              disabled={state.loading}
              placeholder="# 标题&#10;&#10;这里写清步骤、判据、常见坑。模型会照它做。"
              className="min-h-[280px] flex-1 resize-y rounded-sm border border-line bg-surface px-2 py-1.5 font-mono text-[12px] leading-relaxed text-ink outline-none focus:border-line-accent disabled:opacity-60"
            />
            <span className="text-[11px] text-ink-faint">
              {state.body.length.toLocaleString('zh-CN')} 字符
            </span>
          </label>
        </div>
      )}
    </Drawer>
  )
}

export default SkillsPage
