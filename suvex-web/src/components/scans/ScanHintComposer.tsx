import { useState } from 'react'
import { Button } from '@/components/ui/Button'
import { TextInput } from '@/components/settings/Field'
import { api } from '@/lib/api'
import { showToast } from '@/data/toast-store'
import type { Scan } from '@/data/scan'

/**
 * 给运行中的扫描补线索（v1.83）。
 *
 * 后端早已支持 `POST /scans/{id}/hints` 且引擎每轮取一条；缺的只是入口。
 * 只在存在 running 扫描时出现 —— 对没有运行中任务的页面不加装饰。
 */
export function ScanHintComposer({ scans }: { scans: Scan[] }) {
  const running = scans.filter((scan) => scan.status === 'running')
  const [scanId, setScanId] = useState('')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)

  if (running.length === 0) return null
  const selected = running.some((scan) => scan.id === scanId) ? scanId : running[0].id

  async function submit() {
    if (!text.trim()) return
    setBusy(true)
    try {
      await api.post(`/scans/${encodeURIComponent(selected)}/hints`, { text: text.trim() })
      setText('')
      showToast({ tone: 'ok', message: '线索已送出，下一轮生效' })
    } catch (err) {
      showToast({ tone: 'risk', message: err instanceof Error ? err.message : '发送失败' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="mb-panel flex flex-wrap items-end gap-2 rounded-sm border border-line bg-surface px-panel py-2.5">
      <label className="flex flex-col gap-1 text-sm">
        <span className="text-2xs text-ink-muted">运行中的扫描</span>
        <select
          value={selected}
          onChange={(event) => setScanId(event.target.value)}
          className="h-9 max-w-[18rem] rounded-sm border border-line bg-surface px-2 text-sm"
        >
          {running.map((scan) => (
            <option key={scan.id} value={scan.id}>
              {scan.name || scan.target}
            </option>
          ))}
        </select>
      </label>
      <label className="flex min-w-[16rem] flex-1 flex-col gap-1 text-sm">
        <span className="text-2xs text-ink-muted">补一条线索（下一轮生效）</span>
        <TextInput
          id="scan-hint"
          value={text}
          onChange={setText}
          placeholder="例如：登录接口在 /api/v2，不要在 /index.php 上继续"
        />
      </label>
      <Button variant="secondary" size="sm" onClick={() => void submit()} disabled={busy || !text.trim()}>
        {busy ? '发送中…' : '发送线索'}
      </Button>
    </section>
  )
}
