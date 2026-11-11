import { PageHeader } from '@/components/layout/AppShell'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { EmptyState } from '@/components/ui/EmptyState'
import { ErrorState } from '@/components/ui/ErrorState'
import { SegmentedControl } from '@/components/ui/SegmentedControl'
import { Skeleton } from '@/components/ui/Skeleton'
import { useAuth } from '@/data/auth-store'
import { SkillEditor, type SkillEditorState } from '@/components/kernel/SkillEditor'
import {
  createSkill,
  deleteSkill,
  ensureSkillsLoaded,
  fetchSkill,
  fetchSkillFiles,
  filterSkillGroups,
  loadSkills,
  loadsOf,
  updateSkill,
  importSkillZip,
  useSkills,
  usageSummary,
  validateDraft,
  type SkillSummary,
} from '@/data/skills'
import { cn } from '@/lib/cn'
import { showToast } from '@/data/toast-store'
import {
  AlertCircle,
  BookOpen,
  Copy,
  Eye,
  FileCode2,
  Lock,
  MousePointerClick,
  Pencil,
  Plus,
  Search,
  Trash2,
  TrendingUp,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { WriteupMarkdown } from '@/components/writeup/WriteupMarkdown'
import { useI18n } from '@/i18n'

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
  analysis: 'skills.category.analysis',
  protocols: 'skills.category.protocols',
  reconnaissance: 'skills.category.reconnaissance',
  tooling: 'skills.category.tooling',
  vulnerabilities: 'skills.category.vulnerabilities',
}

function categoryLabel(category: string): string {
  return CATEGORY_LABEL[category] ?? category
}

export function SkillsPage() {
  const { t } = useI18n()
  const { list, usage, status, error, usageTruncated } = useSkills()
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'

  const [query, setQuery] = useState('')
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<string | null>(null)

  /** 编辑器状态：`null` = 关闭；否则是正在编辑/新建的内容。 */
  const [editor, setEditor] = useState<SkillEditorState | null>(null)
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
    setEditor({ mode: 'create', name: '', category: '', description: '', body: '', files: {} })
    setEditorError(null)
  }

  /*
    ★ 「复制为自定义」（2026-10-26 加）。

    内置技能只读是正确的（部署会整目录覆盖，改了下次就没了），
    但过去这一步是**死路**：想基于一篇内置技能改出自己的版本，
    只能打开它、手动全选复制、再新建、再粘回去。76 篇里 72 篇是内置的，
    等于"想改任何一篇"都要走这套手抄流程。

    这里做的是把正文取出来填进新建表单：
      · 名字留空 —— 由用户起。同名的内置优先（`all_skills` 的规则），
        所以叫原名会静默不生效，那是最难自查的失败形状；
      · 分类沿用原篇 —— 分类是界面分组的依据，让用户重选一遍没有收益；
      · 附件不复制 —— 内置技能的附件接口回 404（不可编辑），
        拿不到就别假装拿到了。

    填进去的是**副本**，不是引用：保存后在自定义目录里生成真正的第二篇。
  */
  async function openDuplicateAsCustom(skill: SkillSummary) {
    setEditorError(null)
    setEditor({
      mode: 'create',
      /* 起一个明显不同的默认名，但仍是合法标识符（小写 + 连字符）。
         用 `-copy` 后缀而不是空着：空名字会让"保存"按钮一直是灰的，
         用户不知道要先想个名字；而直接叫原名会撞上"内置优先"静默失效。 */
      name: `${skill.name}-copy`,
      category: skill.category,
      description: skill.description,
      body: '',
      files: {},
      loading: true,
    })
    try {
      const detail = await fetchSkill(skill.name)
      setEditor((prev) =>
        prev && prev.name === `${skill.name}-copy`
          ? { ...prev, body: detail.body, loading: false }
          : prev,
      )
    } catch (err) {
      setEditor((prev) =>
        prev && prev.name === `${skill.name}-copy`
          ? { ...prev, loading: false, body: '' }
          : prev,
      )
      setEditorError(err instanceof Error ? err.message : t('skills.duplicate.error'))
    }
  }

  async function openEdit(skill: SkillSummary) {
    setEditorError(null)
    setEditor({
      mode: 'edit',
      name: skill.name,
      category: skill.category,
      description: skill.description,
      body: '',
      files: {},
      // 正文与附件都要现拉 —— 清单里没有（可能几百 KB，不该随清单下发）。
      loading: true,
    })
    try {
      /*
        ★ 正文与附件**并行**拉，但两个请求的错误要分开管：
          附件接口对内置技能会 400/404（内置没有附件可编辑），
          而正文仍然应该正常显示。若用 Promise.all，附件一失败
          正文也就一起没了 —— 用户会看到一篇空白的已存在技能。
      */
      const [detail, files] = await Promise.all([
        fetchSkill(skill.name),
        fetchSkillFiles(skill.name).catch(() => ({}) as Record<string, string>),
      ])
      setEditor((prev) =>
        prev && prev.name === skill.name
          ? { ...prev, body: detail.body, files, loading: false }
          : prev,
      )
    } catch (err) {
      setEditor((prev) =>
        prev && prev.name === skill.name
          ? { ...prev, loading: false, body: '', files: {} }
          : prev,
      )
      setEditorError(err instanceof Error ? err.message : t('skills.bodyError'))
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
      /*
        ★ `files` 必须原样传下去，不能被折叠成 `?? {}`。
          这里的 `editor.files` 永远是一个对象（打开时已填好），
          所以**编辑**一律是全量替换语义 —— 这正是我们要的：
          用户删掉的附件就该被删掉。而"只改正文不动附件"在这个
          界面里不存在（附件面板始终可见且全部加载着）。
      */
      if (editor.mode === 'create') {
        await createSkill({ ...draft, files: editor.files })
        setSelected(draft.name)
      } else {
        await updateSkill(draft.name, {
          description: draft.description,
          body: draft.body,
          files: editor.files,
        })
        setSelected(draft.name)
      }
      setEditor(null)
    } catch (err) {
      setEditorError(err instanceof Error ? err.message : t('skills.saveFailed'))
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
      setEditorError(err instanceof Error ? err.message : t('skills.deleteFailed'))
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
            title={t('skills.error.title')}
            error={error ?? new Error(t('skills.error.noResponse'))}
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
              <div className="flex items-start gap-tight rounded-sm border border-line px-3 py-2 text-sm text-ink-muted">
                <Eye className="mt-0.5 size-3.5 shrink-0" strokeWidth={1.75} />
                <span>
                  {t('skills.readonly.before')}
                  <b>{t('skills.readonly.bold')}</b>
                  {t('skills.readonly.after')}
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
                  <h2 className="text-sm font-medium text-ink">
                    {t('skills.list.heading')}
                    <span className="ml-1.5 font-normal text-ink-faint">{list.total}</span>
                  </h2>
                  <label className="inline-flex h-8 cursor-pointer items-center gap-1 rounded-sm border border-line px-2 text-2xs text-ink-muted hover:border-line-strong hover:text-ink">
                    <input
                      type="file"
                      accept=".zip,application/zip"
                      className="hidden"
                      onChange={(event) => {
                        const file = event.target.files?.[0]
                        if (!file) return
                        void importSkillZip(file)
                          .then(async () => {
                            await loadSkills()
                            showToast({ tone: 'ok', message: t('skills.importOk') })
                          })
                          .catch((err: unknown) => {
                            showToast({
                              tone: 'risk',
                              message: err instanceof Error ? err.message : t('skills.importFailed'),
                            })
                          })
                        event.target.value = ''
                      }}
                    />
                    {t('skills.import')}
                  </label>
                  <Button
                    size="sm"
                    onClick={openCreate}
                    disabled={!isAdmin}
                    title={isAdmin ? t('skills.new.adminTitle') : t('skills.new.readonlyTitle')}
                  >
                    <Plus className="size-3.5" strokeWidth={2} />
                    {t('skills.new.button')}
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
                      placeholder={t('skills.searchPlaceholder')}
                      className="h-8 w-full rounded-sm border border-line bg-surface pr-2 pl-7 text-sm text-ink outline-none focus:border-line-accent"
                    />
                  </div>
                </div>

                {list.total === 0 ? (
                  <EmptyState
                    icon={BookOpen}
                    title={t('skills.empty.title')}
                    description={t('skills.empty.desc')}
                  />
                ) : visibleSkills.length === 0 ? (
                  <p className="px-3 py-6 text-sm text-ink-muted">
                    {t('skills.noMatch', { query })}
                  </p>
                ) : (
                  <ul className="max-h-[60dvh] overflow-y-auto lg:max-h-[calc(100dvh-320px)]">
                    {filtered.map((group) => (
                      <li key={group.category}>
                        <h3 className="border-b border-line/60 bg-surface-raised/30 text-2xs tracking-wide text-ink-faint uppercase">
                          <button
                            type="button"
                            onClick={() =>
                              setCollapsed((prev) => {
                                const next = new Set(prev)
                                if (next.has(group.category)) next.delete(group.category)
                                else next.add(group.category)
                                return next
                              })
                            }
                            className="flex w-full items-center justify-between px-3 py-1.5 hover:text-ink"
                            aria-expanded={!collapsed.has(group.category)}
                          >
                            <span>
                              {collapsed.has(group.category) ? '▸' : '▾'} {t(categoryLabel(group.category))}
                              <span className="ml-1 normal-case">（{group.skills.length}）</span>
                            </span>
                          </button>
                        </h3>
                        {!collapsed.has(group.category) && (
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
                        )}
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
                    onDuplicate={() => void openDuplicateAsCustom(activeSkill)}
                    onDelete={() => setPendingDelete(activeSkill)}
                    error={editorError}
                  />
                ) : (
                  <EmptyState
                    icon={MousePointerClick}
                    title={t('skills.detail.emptyTitle')}
                    description={t('skills.detail.emptyDesc')}
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
        title={t('skills.delete.title')}
        description={
          pendingDelete
            ? t('skills.delete.desc', { name: pendingDelete.name })
            : ''
        }
        confirmLabel={t('skills.delete.confirm')}
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
  const { t } = useI18n()
  return (
    <section className="rounded-md border border-line bg-surface px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <Metric label={t('skills.metric.total')} value={total} />
        <Metric
          label={t('skills.metric.used')}
          value={usedCount}
          hint={total > 0 ? `${Math.round((usedCount / total) * 100)}%` : undefined}
        />
        <Metric label={t('skills.metric.loads')} value={totalLoads} />
        {topName && (
          <div className="flex items-center gap-1.5 text-sm">
            <TrendingUp className="size-3.5 text-ink-faint" strokeWidth={1.75} />
            <span className="text-ink-muted">{t('skills.stats.topUsed')}</span>
            <code className="font-mono text-ink">{topName}</code>
            <span className="text-ink-faint">{t('skills.stats.times', { n: topLoads })}</span>
          </div>
        )}
      </div>

      {/*
        ★ 措辞必须说清这个数证明什么。它是「被 load_skill 拉过」，
          不是「有用」「命中」「采纳」—— 后者从事件流里推不出来。
          把它写大一点很好看，但它就是编的。
      */}
      <p className="mt-2 max-w-[80ch] text-xs leading-relaxed text-ink-faint">
        {t('skills.stats.scopeBefore')} <code className="font-mono">load_skill</code>
        {t('skills.stats.scopeAfter')}
        {truncated && (
          <>
            {' '}
            {t('skills.stats.truncatedBefore', {
              n: sampledEvents.toLocaleString('zh-CN'),
            })}
            <b>{t('skills.stats.truncatedBold')}</b>
            {t('skills.stats.truncatedAfter')}
          </>
        )}
        {usageMissing && (
          <span className="text-signal-risk">
            {' '}
            {t('skills.stats.missing')}
            {usageMessage ? t('skills.stats.missingDetail', { msg: usageMessage }) : ''}
            {t('skills.stats.missingAfter')}
          </span>
        )}
      </p>
    </section>
  )
}

function Metric({ label, value, hint }: { label: string; value: number; hint?: string }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-xs text-ink-muted">{label}</span>
      <span className="font-mono text-lg text-ink">{value}</span>
      {hint && <span className="text-2xs text-ink-faint">{hint}</span>}
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
  const { t } = useI18n()
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
            <code className="truncate font-mono text-sm text-ink">{skill.name}</code>
            {skill.source === 'builtin' ? (
              /* ★ 图标必须有可读的名字：读屏软件面对一个裸 <svg> 只会
                 报"图形"，而这里表达的是"内置/只读"这个**状态**。
                 用 aria-label 而不是 title：title 要悬停才出，键盘与
                 读屏都拿不到。 */
              <Lock
                className="size-3 shrink-0 text-ink-faint"
                strokeWidth={2}
                role="img"
                aria-label={t('skills.builtinAria')}
              />
            ) : (
              <span className="shrink-0 rounded-xs border border-line-accent px-1 text-[10px] text-ink-muted">
                {t('skills.custom')}
              </span>
            )}
            {/*
              ★ 附件数只在 >0 时显示：0 是绝大多数（31 篇里多数是单文件），
                每行都挂一个「0」只会把真正的信号（这一篇带配套文件）淹掉。
            */}
            {(skill.fileCount ?? 0) > 0 && (
              <span
                className="shrink-0 rounded-xs border border-line px-1 text-[10px] text-ink-faint"
                title={t('skills.attachmentsTitle', { n: skill.fileCount ?? 0 })}
              >
                +{skill.fileCount}
              </span>
            )}
          </div>
          <p className="mt-0.5 line-clamp-2 text-xs leading-relaxed text-ink-muted">
            {skill.description || t('skills.noDesc')}
          </p>
        </div>

        {/*
          ★ 0 次也要显示，但用 `text-ink-faint` 而不是藏起来。
            「这一篇从没被用过」是这一页最有用的信息之一 ——
            它是一条死文本，占着上下文预算但不产生价值。
        */}
        <span
          className={cn(
            'shrink-0 font-mono text-2xs',
            loads > 0 ? 'text-ink-muted' : 'text-ink-faint',
          )}
          title={loads > 0 ? t('skills.loadedTitle', { n: loads }) : t('skills.neverLoaded')}
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
  onDuplicate,
  onDelete,
  error,
}: {
  skill: SkillSummary
  loads: { loads: number; lastLoadedAt: string | null } | null
  isAdmin: boolean
  onEdit: () => void
  onDuplicate: () => void
  onDelete: () => void
  error: string | null
}) {
  const { t } = useI18n()
  const builtin = skill.source === 'builtin'

  /*
    ★ 正文按需拉（2026-10-26 改）。

    在这之前，详情面板对内置技能**一片空白** —— 76 篇里 72 篇是内置的，
    等于这一页只能看 4 篇。理由原本写的是「内置的改不了，回显没用」，
    但「改不了」不等于「看不见」：真正决定模型行为的正是那 72 篇。

    ## 为什么要按需拉而不是随清单下发

    清单要渲染 76 篇，正文单篇就有 9000～24000 字符
    （`llm-applications` 实测 23358），全下发是 800KB+ 的响应，
    而用户通常只看一两篇。所以清单只给描述，点开哪篇拉哪篇。

    ## 为什么切走之后不清空

    用 `skill.name` 作 key 记在同一个 state 里，切换时先显示上一份
    会被误读成"这篇的正文长这样"。所以切换时立刻置 loading，
    并在请求回来时校验 `name` 仍是当前这篇（下面的 cancelled 标志 +
    name 比对），避免慢请求回来覆盖了新选中的技能。
  */
  const [body, setBody] = useState('')
  const [lineCount, setLineCount] = useState(0)
  const [loading, setLoading] = useState(false)
  const [bodyError, setBodyError] = useState<string | null>(null)
  /*
    视图状态**不随选中项重置** —— 用户切到「原文」是在表达一种偏好
    （我要看保真原文），换一篇还按这个偏好来才是对的。
    每次切换都弹回默认会让那个按钮看起来"不管用"。
  */
  const [viewMode, setViewMode] = useState<'rendered' | 'raw'>('rendered')

  useEffect(() => {
    let cancelled = false
    // 重置与请求一起放进宏任务：effect 体内同步 setState 会触发级联渲染告警。
    const timer = window.setTimeout(() => {
      setLoading(true)
      setBodyError(null)
      setBody('')
      setLineCount(0)

      fetchSkill(skill.name)
        .then((detail) => {
          if (cancelled) return
          setBody(detail.body)
          setLineCount(detail.lineCount)
        })
        .catch((err: unknown) => {
          if (cancelled) return
          setBodyError(err instanceof Error ? err.message : t('skills.bodyError'))
        })
        .finally(() => {
          if (cancelled) return
          setLoading(false)
        })
    }, 0)

    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [skill.name])

  return (
    <div className="flex h-full flex-col">
      <div className="grid grid-cols-3 gap-2 border-b border-line px-4 py-2.5 text-sm">
        <div>
          <div className="text-2xs text-ink-faint">{t('skills.totalCalls')}</div>
          <div className="tabular text-ink">{loads?.loads ?? 0}</div>
        </div>
        <div>
          <div className="text-2xs text-ink-faint">{t('skills.lastCall')}</div>
          <div className="text-ink">
            {loads?.lastLoadedAt
              ? new Date(loads.lastLoadedAt).toLocaleString('zh-CN', { hour12: false })
              : t('skills.neverCalled')}
          </div>
        </div>
        <div>
          <div className="text-2xs text-ink-faint">{t('skills.source')}</div>
          <div className="text-ink">
            {builtin ? t('skills.sourceBuiltin') : t('skills.custom')}
          </div>
        </div>
      </div>

      <header className="flex flex-wrap items-start justify-between gap-tight border-b border-line px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <code className="font-mono text-base text-ink">{skill.name}</code>
            <span className="rounded-xs border border-line px-1.5 py-0.5 text-2xs text-ink-muted">
              {t(categoryLabel(skill.category))}
            </span>
          </div>
          <p className="mt-1 max-w-[68ch] text-sm leading-relaxed text-ink-muted">
            {skill.description || t('skills.noDesc')}
          </p>
        </div>

        <div className="flex items-center gap-tight">
          {/*
            ★ 内置技能**不画**编辑/删除按钮。
              画一个禁用按钮再告诉用户"不许"，不如一开始就不画 ——
              界面上每个灰掉的按钮都在说"这里有东西你碰不到"，
              而内置技能根本没有可编辑的副本。

            ★ 但「复制为自定义」要画（2026-10-26 加）。
              它是一个**真能做成的动作**，不是灰按钮：把正文取出来
              填进新建表单，保存后得到一篇属于你的技能。
              没有它的话，"想改内置技能"就是死路 —— 只能手抄。
          */}
          {builtin ? (
            <>
              <span className="flex items-center gap-1 text-xs text-ink-faint">
                <Lock className="size-3" strokeWidth={2} />
                {t('skills.builtinReadonly')}
              </span>
              <Button size="sm" variant="ghost" onClick={onDuplicate} disabled={!isAdmin}>
                <Copy className="size-3.5" strokeWidth={1.75} />
                {t('skills.duplicate')}
              </Button>
            </>
          ) : (
            <>
              <Button size="sm" variant="ghost" onClick={onEdit} disabled={!isAdmin}>
                <Pencil className="size-3.5" strokeWidth={1.75} />
                {t('skills.edit')}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={onDelete}
                disabled={!isAdmin}
                className="text-signal-risk"
              >
                <Trash2 className="size-3.5" strokeWidth={1.75} />
                {t('skills.deleteAction')}
              </Button>
            </>
          )}
        </div>
      </header>

      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-b border-line px-4 py-2 text-xs text-ink-muted">
        <span>
          {t('skills.loadedPrefix')}{' '}
          <b className="font-mono text-ink">{loads?.loads ?? 0}</b>
          {t('skills.loadedSuffix')}
        </span>
        {loads?.lastLoadedAt ? (
          <span>
            {t('skills.lastLoadedAt', {
              time: new Date(loads.lastLoadedAt).toLocaleString('zh-CN'),
            })}
          </span>
        ) : (
          <span className="text-ink-faint">{t('skills.deadText')}</span>
        )}
      </div>

      {error && (
        <div className="flex items-start gap-tight border-b border-line px-4 py-2 text-sm text-signal-risk">
          <AlertCircle className="mt-0.5 size-3.5 shrink-0" strokeWidth={1.75} />
          <span>{error}</span>
        </div>
      )}

      <div className="flex-1 overflow-y-auto px-4 py-3">
        {loading ? (
          <div className="space-y-2">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-5/6" />
          </div>
        ) : bodyError ? (
          <div className="flex items-start gap-tight text-sm text-signal-risk">
            <AlertCircle className="mt-0.5 size-3.5 shrink-0" strokeWidth={1.75} />
            <span>{bodyError}</span>
          </div>
        ) : body ? (
          /*
            ★ 正文有两种看法，默认「排版」，可切到「原文」。

            2026-10-26 用户反馈：「文字太紧凑了吧，右边还空着一大块」。

            原因不是字号小，是**源文件本身就硬换行到 ~72 字符**：
            技能 .md 是写给模型读的，作者按 72 列折了行，`whitespace-pre-wrap`
            会把这些换行原样保留 —— 于是 12px 等宽字体下每行只占 ~500px，
            而面板有 ~1400px，右边那一大块永远是空的，同时行距又挤。

            所以：
              · 「排版」走项目的 Markdown 渲染器，段落**自然重排**，
                宽度用起来、行高 1.8、字号提到 14px；
              · 「原文」保留逐字符保真的 `<pre>` —— 它仍是审计入口：
                技能里的 HTML 版权注释、围栏、缩进只有在这里才原样可见，
                渲染后那部分会消失。两个视图**各有各的用途**，不是二选一。

            ⚠️ 默认给「排版」而不是「原文」：第一眼要能读懂，
               保真需求是第二步才会有的（要核对"它到底怎么写的"）。
          */
          <div>
            <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-ink-faint">
              <SegmentedControl
                size="sm"
                ariaLabel={t('skills.view.aria')}
                value={viewMode}
                onChange={(next) => setViewMode(next)}
                options={[
                  {
                    id: 'rendered',
                    label: t('skills.view.rendered'),
                    icon: <BookOpen className="size-3" strokeWidth={2} />,
                  },
                  {
                    id: 'raw',
                    label: t('skills.view.raw'),
                    icon: <FileCode2 className="size-3" strokeWidth={2} />,
                  },
                ]}
              />

              <span className="flex items-center gap-1">
                <Eye className="size-3" strokeWidth={2} />
                {viewMode === 'raw'
                  ? t('skills.view.fidelity', { lines: lineCount, chars: body.length })
                  : t('skills.view.renderedInfo', { lines: lineCount, chars: body.length })}
              </span>
              {viewMode === 'raw' && (
                <span className="text-ink-faint/80">{t('skills.view.rawHint')}</span>
              )}
            </div>

            {viewMode === 'rendered' ? (
              /*
                `max-w-[110ch]`：不用满整块面板，但也不像原文那样只用三分之一。
                约 60% 宽度 —— 段落长到 150 字符以上会开始难读，110 是个折中。
                列表、表格、代码块本来就没有这个上限（它们该占满）。
              */
              <div className="max-w-[110ch] text-base leading-[1.8] text-ink-muted">
                <WriteupMarkdown markdown={body} />
              </div>
            ) : (
              <pre className="max-w-full overflow-x-auto rounded-sm border border-line bg-surface-raised/40 p-3 font-mono text-xs leading-relaxed break-words whitespace-pre-wrap text-ink-muted">
                {body}
              </pre>
            )}
          </div>
        ) : (
          <p className="max-w-[72ch] text-sm leading-relaxed text-ink-muted">
            {t('skills.bodyMissing.before')}{' '}
            <code className="font-mono">prompts/skills/</code>
            {t('skills.bodyMissing.after')}
          </p>
        )}
      </div>

      <footer className="border-t border-line px-4 py-2.5 text-xs leading-relaxed text-ink-faint">
        {t('skills.footer.before')}
        <b>{t('skills.footer.bold')}</b>
        {t('skills.footer.after')}
      </footer>
    </div>
  )
}

export default SkillsPage
