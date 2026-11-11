import { Button } from '@/components/ui/Button'
import { Modal } from '@/components/ui/Modal'
import { buildTree } from '@/components/kernel/skill-tree'
import { validateAttachmentName } from '@/data/skills'
import { cn } from '@/lib/cn'
import {
  AlertCircle,
  Check,
  FileCode2,
  FileText,
  FolderOpen,
  Loader2,
  Plus,
  Trash2,
} from 'lucide-react'
import { useMemo, useRef, useState } from 'react'

import { useI18n } from '@/i18n'

/**
 * 技能编辑器（v0.90）：一个技能 = 一个目录包。
 *
 * ## 从「一个 .md」变成「一个目录」是这次改造的全部意义
 *
 * 旧形态一篇技能只有一个 `.md`。想让模型「按检查表逐条走」「复制这段
 * 脚本去跑」「照着这份表单填」，只能把脚本和检查表**塞进 Markdown
 * 正文**里 —— 模型得把它从正文里抠出来再写一遍，而写出来的那份
 * 和原文是不是一回事，没人保证。
 *
 * CyberStrikeAI 的形态是 `SKILL.md` + 同目录任意附件，模型按相对路径
 * 直接读。抄的是这个形态，不是它的样式。
 *
 * ## ★ 为什么正文用 state.body、附件用 state.files，而不是合成一个 map
 *
 * 因为 `SKILL.md` 的正文有**独立的字段语义**：后端的 `files` 里
 * 永远不含 `SKILL.md`（它由 body 生成），而且清空附件（`files: {}`）
 * 绝不能连正文一起清掉。合成一个 map 之后，"用户删光了附件"和
 * "用户删掉了正文" 就分不开了 —— 那正好是 `files: undefined` 与
 * `files: {}` 那个坑的翻版（见 `skills.ts` 的 `SkillDraft.files`）。
 *
 * 所以这里开着两个抽屉，界面上的文件树只是**视图层合并**，不改变存储。
 */

/**
 * 「正文」在选中状态里的键。
 *
 * ★ 用 `<body>` 而不是 `__body__` 这种看起来更"正常"的哨兵：后者是
 *   **合法的附件名**（白名单允许下划线开头），用户真建一个叫
 *   `__body__` 的附件就会和正文撞键 —— 症状是点正文却打开附件。
 *   `<` 不在附件名白名单里，所以 `<body>` 永远不会是一个真实路径。
 */
const BODY_KEY = '<body>'

/** 把 `TreeRow.path`（`null` = 正文）映射成选中键。 */
const keyOf = (path: string | null): string => path ?? BODY_KEY

export interface SkillEditorState {
  mode: 'create' | 'edit'
  name: string
  category: string
  description: string
  /** `SKILL.md` 的正文 */
  body: string
  /** 附件：`{相对路径: 内容}`，不含 `SKILL.md` */
  files: Record<string, string>
  loading?: boolean
}

export function SkillEditor({
  state,
  saving,
  error,
  onChange,
  onSave,
  onClose,
}: {
  state: SkillEditorState | null
  saving: boolean
  error: string | null
  onChange: (next: SkillEditorState) => void
  onSave: () => void
  onClose: () => void
}) {
  const { t } = useI18n()
  const open = state !== null
  const isCreate = state?.mode === 'create'

  /** 当前正在编辑哪一个文件（`null` = 正文） */
  const [activeKey, setActiveKey] = useState<string>(BODY_KEY)
  /** 「新建文件」输入行是否展开 */
  const [addingFile, setAddingFile] = useState(false)
  const [newFileName, setNewFileName] = useState('')
  const newFileRef = useRef<HTMLInputElement>(null)

  const tree = useMemo(() => (state ? buildTree(state.files) : []), [state])

  /*
    ★ 选中项可能已经被删掉（用户刚删了正在编辑的那个附件）。
      这时回落到正文，而不是留在一个不存在的文件上 —— 否则编辑区
      是空白、保存时还会把它当新文件写回去。
  */
  const activePath =
    activeKey !== BODY_KEY && state && activeKey in state.files ? activeKey : null
  const activeValue = activePath === null ? (state?.body ?? '') : (state?.files[activePath] ?? '')

  function setActiveValue(value: string) {
    if (!state) return
    if (activePath === null) {
      onChange({ ...state, body: value })
    } else {
      onChange({ ...state, files: { ...state.files, [activePath]: value } })
    }
  }

  function addFile() {
    if (!state) return
    const raw = newFileName.trim()
    const invalid = validateAttachmentName(raw)
    if (invalid) return // 错误由下面的提示行显示，不弹窗打断
    if (raw in state.files) return // 已存在，改成选中它更合理
    onChange({ ...state, files: { ...state.files, [raw]: '' } })
    setActiveKey(raw)
    setNewFileName('')
    setAddingFile(false)
  }

  function removeFile(path: string) {
    if (!state) return
    const next = { ...state.files }
    delete next[path]
    onChange({ ...state, files: next })
    if (activeKey === path) setActiveKey(BODY_KEY)
  }

  const newNameError = newFileName.trim() ? validateAttachmentName(newFileName.trim()) : null
  const fileCount = state ? Object.keys(state.files).length : 0

  return (
    <Modal
      open={open}
      onClose={onClose}
      widthClass="max-w-5xl"
      // ★ 显式给确定高度，不依赖 Modal 撑满。内部是「文件树 + 正文」两栏，
      //   两边各自 overflow-y-auto，没有确定高度就滚不起来（内容一变弹窗就
      //   自己长高变矮）。别处内容短的弹窗撑满只会留下半屏空白，所以这个
      //   撑满是**这一处的需要**，不该写进 Modal 默认值。
      heightClass="max-h-[88vh] h-[88vh]"
      title={
        isCreate ? (
          t('skills.editor.createTitle')
        ) : (
          <span className="font-mono text-sm">{state?.name}</span>
        )
      }
      titleAside={
        state && !state.loading ? (
          <span className="rounded-sm border border-line px-1.5 py-0.5 text-2xs text-ink-faint">
            {fileCount === 0
              ? t('skills.editor.singleFile')
              : t('skills.editor.withFiles', { n: fileCount })}
          </span>
        ) : null
      }
      footer={
        <div className="flex items-center justify-between gap-tight">
          <span className="text-2xs text-ink-faint">
            {t('skills.editor.footerBefore')}{' '}
            <code className="font-mono">load_skill</code>
            {t('skills.editor.footerAfter')}
          </span>
          <div className="flex items-center gap-tight">
            <Button variant="ghost" onClick={onClose} disabled={saving}>
              {t('skills.editor.cancel')}
            </Button>
            <Button onClick={onSave} disabled={saving || state?.loading}>
              {saving ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <Check className="size-3.5" />
              )}
              {t('skills.editor.save')}
            </Button>
          </div>
        </div>
      }
    >
      {state && (
        <div className="flex h-full min-h-0 flex-col gap-panel">
          {error && (
            <div className="flex items-start gap-tight rounded-sm border border-line px-3 py-2 text-sm text-signal-risk">
              <AlertCircle className="mt-0.5 size-3.5 shrink-0" strokeWidth={1.75} />
              <span className="leading-relaxed">{error}</span>
            </div>
          )}

          {/* ── 元信息（两行并排，占满宽度）────────────── */}
          <div className="grid shrink-0 gap-panel sm:grid-cols-2">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-ink-muted">
                {t('skills.editor.nameLabel')}{' '}
                <span className="text-ink-faint">{t('skills.editor.nameHint')}</span>
              </span>
              <input
                value={state.name}
                onChange={(event) => onChange({ ...state, name: event.target.value })}
                /* ★ 编辑时名字不可改：它同时是目录名和模型调用标识，改名等于删一个建一个 */
                disabled={!isCreate}
                placeholder={t('skills.editor.namePlaceholder')}
                className="h-9 rounded-sm border border-line bg-surface px-2 font-mono text-sm text-ink outline-none focus:border-line-accent disabled:opacity-60"
              />
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-xs text-ink-muted">
                {t('skills.editor.categoryLabel')}{' '}
                <span className="text-ink-faint">{t('skills.editor.categoryHint')}</span>
              </span>
              <input
                value={state.category}
                onChange={(event) => onChange({ ...state, category: event.target.value })}
                disabled={!isCreate}
                placeholder={t('skills.editor.categoryPlaceholder')}
                className="h-9 rounded-sm border border-line bg-surface px-2 font-mono text-sm text-ink outline-none focus:border-line-accent disabled:opacity-60"
              />
            </label>
          </div>

          <label className="flex shrink-0 flex-col gap-1">
            <span className="text-xs text-ink-muted">
              {t('skills.editor.descLabel')}{' '}
              <span className="text-ink-faint">{t('skills.editor.descHint')}</span>
            </span>
            <input
              value={state.description}
              onChange={(event) => onChange({ ...state, description: event.target.value })}
              placeholder={t('skills.editor.descPlaceholder')}
              className="h-9 rounded-sm border border-line bg-surface px-2 text-sm text-ink outline-none focus:border-line-accent"
            />
          </label>

          {/* ── 主体：文件树 + 内容 ───────────────────── */}
          <div className="grid min-h-0 flex-1 gap-panel md:grid-cols-[220px_1fr]">
            {/* 左：包内文件 */}
            <div className="flex min-h-0 flex-col rounded-sm border border-line">
              <div className="flex shrink-0 items-center justify-between border-b border-line px-2 py-1.5">
                <span className="text-2xs font-medium uppercase tracking-wide text-ink-faint">
                  {t('skills.editor.files')}
                </span>
                <button
                  type="button"
                  onClick={() => {
                    setAddingFile(true)
                    setNewFileName('')
                    // 下一帧焦点进输入框（此时它才被渲出来）
                    requestAnimationFrame(() => newFileRef.current?.focus())
                  }}
                  className="rounded-sm p-0.5 text-ink-faint transition-ui duration-fast hover:bg-surface-raised hover:text-ink"
                  aria-label={t('skills.editor.newFileAria')}
                  title={t('skills.editor.newFileAria')}
                >
                  <Plus size={13} strokeWidth={2} />
                </button>
              </div>

              <div className="min-h-0 flex-1 overflow-y-auto py-1">
                {state.loading ? (
                  <div className="flex items-center gap-1 px-2 py-1 text-xs text-ink-faint">
                    <Loader2 className="size-3 animate-spin" /> {t('skills.editor.reading')}
                  </div>
                ) : (
                  tree.map((row) => {
                    const key = keyOf(row.path)
                    const selected = key === activeKey || (activeKey === BODY_KEY && row.path === null)
                    if (row.isDir) {
                      return (
                        <div
                          key={'dir:' + row.path}
                          style={{ paddingLeft: row.depth * 10 + 8 }}
                          className="flex items-center gap-1 py-0.5 text-xs text-ink-faint"
                        >
                          <FolderOpen size={12} strokeWidth={1.75} />
                          <span className="truncate">{row.label}</span>
                          <span className="text-ink-faint/70">({row.count})</span>
                        </div>
                      )
                    }
                    const isBody = row.path === null
                    return (
                      <div
                        key={key}
                        style={{ paddingLeft: row.depth * 10 + 8 }}
                        className={cn(
                          'group flex items-center gap-1 py-0.5 pr-1 text-xs',
                          selected ? 'bg-surface-raised text-ink' : 'text-ink-muted',
                        )}
                      >
                        <button
                          type="button"
                          onClick={() => setActiveKey(key)}
                          className="flex min-w-0 flex-1 items-center gap-1 text-left hover:text-ink"
                        >
                          {isBody ? (
                            <FileText size={12} strokeWidth={1.75} className="shrink-0" />
                          ) : (
                            <FileCode2 size={12} strokeWidth={1.75} className="shrink-0" />
                          )}
                          <span className="truncate font-mono">{row.label}</span>
                        </button>
                        {/* 正文不能删（它就是这一篇技能本身），附件可以 */}
                        {!isBody && (
                          <button
                            type="button"
                            onClick={() => removeFile(row.path as string)}
                            className="shrink-0 rounded-sm p-0.5 text-ink-faint opacity-0 transition-ui duration-fast hover:text-signal-risk group-hover:opacity-100 focus-visible:opacity-100"
                            aria-label={t('skills.editor.deleteAria', { path: row.path ?? '' })}
                            title={t('skills.editor.deleteAria', { path: row.path ?? '' })}
                          >
                            <Trash2 size={11} strokeWidth={1.75} />
                          </button>
                        )}
                      </div>
                    )
                  })
                )}

                {addingFile && (
                  <div className="flex flex-col gap-0.5 px-2 py-1">
                    <input
                      ref={newFileRef}
                      value={newFileName}
                      onChange={(event) => setNewFileName(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') {
                          event.preventDefault()
                          addFile()
                        }
                        if (event.key === 'Escape') {
                          event.preventDefault()
                          setAddingFile(false)
                        }
                      }}
                      placeholder="scripts/extra.sh"
                      className="h-7 w-full rounded-sm border border-line bg-surface px-1.5 font-mono text-2xs text-ink outline-none focus:border-line-accent"
                    />
                    {newNameError && (
                      <span className="text-[10px] leading-tight text-signal-risk">
                        {newNameError}
                      </span>
                    )}
                    <div className="flex gap-1">
                      <button
                        type="button"
                        onClick={addFile}
                        disabled={!!newNameError || !newFileName.trim()}
                        className="rounded-sm border border-line px-1.5 py-0.5 text-[10px] text-ink-muted hover:text-ink disabled:opacity-50"
                      >
                        {t('skills.editor.add')}
                      </button>
                      <button
                        type="button"
                        onClick={() => setAddingFile(false)}
                        className="rounded-sm px-1.5 py-0.5 text-[10px] text-ink-faint hover:text-ink"
                      >
                        {t('skills.editor.cancel')}
                      </button>
                    </div>
                  </div>
                )}

                {!addingFile && fileCount === 0 && !state.loading && (
                  <p className="px-2 py-2 text-[10px] leading-relaxed text-ink-faint">
                    {t('skills.editor.emptyHint')}
                  </p>
                )}
              </div>
            </div>

            {/* 右：内容 */}
            <label className="flex min-h-0 flex-col gap-1">
              <span className="shrink-0 text-xs text-ink-muted">
                {activePath === null ? (
                  <>
                    {t('skills.editor.bodyLabel')}
                    <span className="ml-1 text-ink-faint">{t('skills.editor.bodyHint')}</span>
                  </>
                ) : (
                  <span className="font-mono">
                    {activePath}
                    <span className="ml-1 font-sans text-ink-faint">
                      {t('skills.editor.attachmentHint')}
                    </span>
                  </span>
                )}
              </span>
              <textarea
                value={activeValue}
                onChange={(event) => setActiveValue(event.target.value)}
                spellCheck={false}
                disabled={state.loading}
                placeholder={
                  activePath === null
                    ? t('skills.editor.bodyPlaceholder')
                    : t('skills.editor.attachmentPlaceholder')
                }
                className="min-h-[240px] flex-1 resize-none rounded-sm border border-line bg-surface px-2 py-1.5 font-mono text-xs leading-relaxed text-ink outline-none focus:border-line-accent disabled:opacity-60"
              />
              <span className="shrink-0 text-2xs text-ink-faint">
                {t('skills.editor.chars', { n: activeValue.length.toLocaleString('zh-CN') })}
              </span>
            </label>
          </div>
        </div>
      )}
    </Modal>
  )
}
