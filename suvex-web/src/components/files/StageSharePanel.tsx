import { useEffect, useState } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { TextInput } from '@/components/settings/Field'
import { createStage, deleteStage, loadStages, type StageResult } from '@/data/stage'
import { showToast } from '@/data/toast-store'
import { useI18n } from '@/i18n'
import { T } from '@/i18n/T'

/**
 * 受管临时投递（v1.83）。
 *
 * 与普通上传的区别：它一定会过期。用于"给目标机器临时拉一个脚本"，
 * 用完即毁；不写工作区、不进下载中心。
 */
export function StageSharePanel() {
  const { t, locale } = useI18n()
  const [name, setName] = useState('tool.sh')
  const [content, setContent] = useState('')
  const [ttl, setTtl] = useState(15)
  const [busy, setBusy] = useState(false)
  const [created, setCreated] = useState<StageResult | null>(null)
  const [items, setItems] = useState<StageResult[]>([])
  /* 低频次要动作：默认收起，给文件和筛选让出首屏。 */
  const [open, setOpen] = useState(false)

  async function reload() {
    try {
      setItems(await loadStages())
    } catch {
      // 列表是增值信息：失败静默
    }
  }

  useEffect(() => {
    // 异步读取后写状态：数据加载，不是从 props 派生的级联渲染。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload()
  }, [])

  async function create() {
    if (!name.trim() || !content.trim()) return
    setBusy(true)
    try {
      const row = await createStage({ name: name.trim(), content, ttlMinutes: ttl })
      setCreated(row)
      setContent('')
      await reload()
      showToast({
        tone: 'ok',
        message: t('files.stage.created', { n: ttl }, `已投递，${ttl} 分钟后过期`),
      })
    } catch (err) {
      showToast({
        tone: 'risk',
        message: err instanceof Error ? err.message : t('files.stage.createFailed', undefined, '投递失败'),
      })
    } finally {
      setBusy(false)
    }
  }

  async function destroy() {
    if (!created) return
    try {
      await deleteStage(created.id)
      setCreated(null)
      await reload()
      showToast({ tone: 'ok', message: t('files.stage.destroyed', undefined, '已销毁') })
    } catch {
      showToast({ tone: 'risk', message: t('files.stage.destroyFailed', undefined, '销毁失败') })
    }
  }

  const absoluteUrl = created ? `${window.location.origin}${created.url}` : ''

  return (
    <section className="rounded-md border border-line bg-surface p-panel">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-sm font-medium text-ink">
            <T k="files.stage.title" fallback="临时投递" />
          </h2>
          <p className="mt-1 text-2xs text-ink-muted">
            <T k="files.stage.desc" fallback="给目标机器临时拉取的文件。随机 token、到期即 404，不进工作区。" />
          </p>
        </div>
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          className="flex shrink-0 items-center gap-1 rounded-sm px-1.5 py-1 text-2xs text-ink-faint transition-ui duration-fast hover:text-ink"
        >
          {open
            ? t('files.stage.collapse', undefined, '收起')
            : t('files.stage.expand', undefined, '展开')}
          {open ? (
            <ChevronUp size={12} strokeWidth={2} aria-hidden="true" />
          ) : (
            <ChevronDown size={12} strokeWidth={2} aria-hidden="true" />
          )}
        </button>
      </div>
      {open && (
        <>
      <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-[12rem_1fr_6rem]">
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-ink-muted"><T k="files.stage.name" fallback="文件名" /></span>
          <TextInput id="stage-name" value={name} onChange={setName} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-ink-muted"><T k="files.stage.content" fallback="内容" /></span>
          <textarea
            value={content}
            onChange={(event) => setContent(event.target.value)}
            rows={3}
            className="w-full rounded-sm border border-line bg-surface px-2 py-1.5 font-mono text-sm text-ink"
            placeholder={t('files.stage.contentPlaceholder', undefined, '粘贴脚本或文本')}
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-ink-muted"><T k="files.stage.ttl" fallback="TTL 分钟" /></span>
          <input
            type="number"
            min={1}
            max={120}
            value={ttl}
            onChange={(event) => setTtl(Number(event.target.value) || 15)}
            className="h-9 rounded-sm border border-line bg-surface px-2 text-sm text-ink"
          />
        </label>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button variant="primary" size="sm" onClick={() => void create()} disabled={busy || !content.trim()}>
          {busy
            ? t('files.stage.busy', undefined, '投递中…')
            : t('files.stage.create', undefined, '创建临时投递')}
        </Button>
        {created && (
          <>
            <code className="max-w-[28rem] truncate rounded-sm border border-line px-2 py-1 font-mono text-2xs text-ink">
              {absoluteUrl}
            </code>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => {
                void navigator.clipboard?.writeText(absoluteUrl)
                showToast({ tone: 'ok', message: t('files.stage.linkCopied', undefined, '链接已复制') })
              }}
            >
              <T k="files.stage.copy" fallback="复制" />
            </Button>
            <Button variant="ghost" size="sm" onClick={() => void destroy()}>
              <T k="files.stage.destroy" fallback="销毁" />
            </Button>
            <span className="text-2xs text-ink-faint">
              {t(
                'files.stage.expiresAt',
                { time: new Date(created.expiresAt).toLocaleString(locale, { hour12: false }) },
                `到期 ${new Date(created.expiresAt).toLocaleString(locale, { hour12: false })}`,
              )}
            </span>
          </>
        )}
      </div>

      {items.length > 0 && (
        <ul className="mt-3 flex flex-col gap-1 border-t border-line pt-2 text-2xs">
          {items.map((item) => (
            <li key={item.id} className="flex flex-wrap items-center gap-2 text-ink-muted">
              <code className="font-mono text-ink">{item.name}</code>
              <span className="truncate text-ink-faint">
                {window.location.origin}
                {item.url}
              </span>
              <span>
                {t(
                  'files.stage.expiresAt',
                  { time: new Date(item.expiresAt).toLocaleString(locale, { hour12: false }) },
                  `到期 ${new Date(item.expiresAt).toLocaleString(locale, { hour12: false })}`,
                )}
              </span>
              <button
                type="button"
                className="ml-auto text-ink-faint underline-offset-2 hover:text-signal-risk hover:underline"
                onClick={() => void deleteStage(item.id).then(reload)}
              >
                <T k="files.stage.destroy" fallback="销毁" />
              </button>
            </li>
          ))}
        </ul>
      )}
        </>
      )}
    </section>
  )
}
