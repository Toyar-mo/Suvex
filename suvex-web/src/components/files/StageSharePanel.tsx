import { useState } from 'react'
import { Button } from '@/components/ui/Button'
import { TextInput } from '@/components/settings/Field'
import { createStage, deleteStage, type StageResult } from '@/data/stage'
import { showToast } from '@/data/toast-store'

/**
 * 受管临时投递（v1.83）。
 *
 * 与普通上传的区别：它一定会过期。用于"给目标机器临时拉一个脚本"，
 * 用完即毁；不写工作区、不进下载中心。
 */
export function StageSharePanel() {
  const [name, setName] = useState('tool.sh')
  const [content, setContent] = useState('')
  const [ttl, setTtl] = useState(15)
  const [busy, setBusy] = useState(false)
  const [created, setCreated] = useState<StageResult | null>(null)

  async function create() {
    if (!name.trim() || !content.trim()) return
    setBusy(true)
    try {
      const row = await createStage({ name: name.trim(), content, ttlMinutes: ttl })
      setCreated(row)
      setContent('')
      showToast({ tone: 'ok', message: `已投递，${ttl} 分钟后过期` })
    } catch (err) {
      showToast({ tone: 'risk', message: err instanceof Error ? err.message : '投递失败' })
    } finally {
      setBusy(false)
    }
  }

  async function destroy() {
    if (!created) return
    try {
      await deleteStage(created.id)
      setCreated(null)
      showToast({ tone: 'ok', message: '已销毁' })
    } catch {
      showToast({ tone: 'risk', message: '销毁失败' })
    }
  }

  const absoluteUrl = created ? `${window.location.origin}${created.url}` : ''

  return (
    <section className="rounded-md border border-line bg-surface p-panel">
      <h2 className="text-sm font-medium text-ink">临时投递</h2>
      <p className="mt-1 text-2xs text-ink-muted">
        给目标机器临时拉取的文件。随机 token、到期即 404，不进工作区。
      </p>
      <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-[12rem_1fr_6rem]">
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-ink-muted">文件名</span>
          <TextInput id="stage-name" value={name} onChange={setName} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-ink-muted">内容</span>
          <textarea
            value={content}
            onChange={(event) => setContent(event.target.value)}
            rows={3}
            className="w-full rounded-sm border border-line bg-surface px-2 py-1.5 font-mono text-sm text-ink"
            placeholder="粘贴脚本或文本"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-ink-muted">TTL 分钟</span>
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
          {busy ? '投递中…' : '创建临时投递'}
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
                showToast({ tone: 'ok', message: '链接已复制' })
              }}
            >
              复制
            </Button>
            <Button variant="ghost" size="sm" onClick={() => void destroy()}>
              销毁
            </Button>
            <span className="text-2xs text-ink-faint">
              到期 {new Date(created.expiresAt).toLocaleString('zh-CN', { hour12: false })}
            </span>
          </>
        )}
      </div>
    </section>
  )
}
