import { cn } from '@/lib/cn'
import {
  attachmentDownloadUrl,
  deleteFindingAttachment,
  loadFindingAttachments,
  uploadFindingAttachments,
  useFindingAttachments,
} from '@/data/findings-store'
import { showToast } from '@/data/toast-store'
import { errorMessage } from '@/lib/api'
import { Download, Loader2, Paperclip, Trash2, Upload } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

/**
 * 漏洞附件（任务 10 P6-d）。
 *
 * ## 为什么放在详情抽屉里而不是列表行上
 *
 * 附件是"处置证据"—— 截图、复测报告。看它的人是在**研究这一条漏洞**
 * 的时候，而那一刻他已经打开了详情。放在列表上既挤又没人看。
 *
 * ## 一次可以传多个
 *
 * 处置一条漏洞往往要传"修复前 + 修复后"两张截图。逐张上传意味着
 * 用户要点两次、看两次进度、失败时还要判断是哪张失败。
 *
 * ## ⚠️ 上传走 multipart，是 CSRF 防线的唯一例外
 *
 * 见契约 §3.15.1：后端的写请求默认拒绝一切表单类 Content-Type
 * （跨站表单是最经典的 CSRF 载体），附件上传靠一张显式登记表放行。
 * 所以这里**不要**给请求手写 `Content-Type` —— boundary 必须由浏览器生成。
 *
 * ## 删除是不可逆的（附件没有回收站）
 *
 * 所以走确认而不是"先删再撤销"：与漏洞本身的软删除不同，
 * 附件的删除是物理的（文件 + 元信息一起没），没有可撤销的东西。
 * 这个不对称是刻意的 —— 附件是**证据文件**，而"证据的撤销窗口"
 * 没有意义（撤销的那一刻它已经被别的流程引用过了）。
 */
export function FindingAttachments({ findingId }: { findingId: string }) {
  const state = useFindingAttachments(findingId)
  const [busy, setBusy] = useState(false)
  /** 正在确认删除的那个附件 id。非空 = 显示二次确认 */
  const [confirmingId, setConfirmingId] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  /*
    打开详情时拉一次。store 自己是幂等的（已 ready 就直接返回），
    所以抽屉反复开关不会重复请求。
  */
  useEffect(() => {
    void loadFindingAttachments(findingId)
  }, [findingId])

  const handleFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return
    setBusy(true)
    try {
      const created = await uploadFindingAttachments(findingId, [...files])
      showToast({ message: `已上传 ${created.length} 个附件` })
    } catch (err) {
      // 后端的错误消息是可直接展示的中文（含大小上限的具体数字）
      showToast({ message: `上传失败：${errorMessage(err)}`, tone: 'risk' })
    } finally {
      setBusy(false)
      // 清空 input：不清的话选同一个文件第二次不会触发 change
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  const handleDelete = async (attachmentId: string) => {
    setConfirmingId(null)
    setBusy(true)
    try {
      await deleteFindingAttachment(findingId, attachmentId)
      showToast({ message: '附件已删除' })
    } catch (err) {
      showToast({ message: `删除失败：${errorMessage(err)}`, tone: 'risk' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-tight">
      {state.status === 'loading' && state.items.length === 0 ? (
        <p className="text-xs text-ink-faint">加载附件…</p>
      ) : state.status === 'error' ? (
        <p className="text-xs text-signal-risk">
          附件加载失败：{state.error?.message ?? '未知错误'}
        </p>
      ) : state.items.length === 0 ? (
        /*
          空态说清"这里该放什么"。只写"暂无附件"的话，用户不知道
          这个框是给他用的还是系统自动生成的 —— 而它的用途
          （处置证据）恰恰不是自明的。
        */
        <p className="rounded-sm border border-dashed border-line-strong px-3 py-2.5 text-xs text-ink-faint">
          还没有附件。可以上传修复前后的截图、复测报告或抓包文件，作为处置证据。
        </p>
      ) : (
        <ul className="flex flex-col gap-1">
          {state.items.map((item) => (
            <li
              key={item.id}
              className="flex items-center gap-2 rounded-sm border border-line px-2.5 py-2"
            >
              <Paperclip size={13} strokeWidth={1.75} className="shrink-0 text-ink-ghost" aria-hidden="true" />
              <span className="min-w-0 flex-1 truncate text-xs text-ink" title={item.filename}>
                {item.filename}
              </span>
              <span className="tabular shrink-0 text-2xs text-ink-faint" data-numeric>
                {formatBytes(item.sizeBytes)}
              </span>

              {confirmingId === item.id ? (
                <>
                  <span className="shrink-0 text-2xs text-signal-risk">删除？</span>
                  <button
                    type="button"
                    onClick={() => void handleDelete(item.id)}
                    disabled={busy}
                    /*
                      禁用不用 `opacity-50`（那会把文字压到 2.2:1），而是撤掉风险色
                      底 + 把文字降到 `ink-faint`：暗 5.94:1 / 亮 5.68:1。
                      取消按钮一直在旁边，用户不需要靠这个按钮的形状来判断该做什么。
                    */
                    className="shrink-0 rounded-xs px-1.5 py-0.5 text-2xs text-signal-risk transition-ui duration-fast hover:bg-signal-risk/10 disabled:cursor-not-allowed disabled:bg-transparent disabled:text-ink-faint disabled:hover:bg-transparent"
                  >
                    确认
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmingId(null)}
                    className="shrink-0 rounded-xs px-1.5 py-0.5 text-2xs text-ink-faint hover:text-ink"
                  >
                    取消
                  </button>
                </>
              ) : (
                <>
                  {/*
                    下载用普通 `<a href>` 而**不是** fetch：
                    服务端已经给了 `Content-Disposition: attachment`，
                    交给浏览器的下载机制才是对的（fetch 拿到的是
                    ArrayBuffer，还得自己再造一个 Blob 和链接）。
                    同源请求会自动带上会话 Cookie。
                  */}
                  <a
                    href={attachmentDownloadUrl(findingId, item.id)}
                    download={item.filename}
                    title={`下载 ${item.filename}`}
                    className="shrink-0 rounded-xs p-1 text-ink-faint transition-ui duration-fast hover:text-accent-strong"
                  >
                    <Download size={12} strokeWidth={2} aria-hidden="true" />
                  </a>
                  <button
                    type="button"
                    onClick={() => setConfirmingId(item.id)}
                    disabled={busy}
                    title={`删除 ${item.filename}`}
                    /*
                      图标按钮：禁用降到 `ink-ghost`。token 注释写明它「仅装饰 / 禁用，
                      禁止承载信息」—— 这里是唯一符合该约定的用法（纯图标，无文字）。
                      压 opacity 会把 12px 的图标擦成一块噪点。
                    */
                    className="shrink-0 rounded-xs p-1 text-ink-faint transition-ui duration-fast hover:text-signal-risk disabled:cursor-not-allowed disabled:text-ink-ghost disabled:hover:text-ink-ghost"
                  >
                    <Trash2 size={12} strokeWidth={2} aria-hidden="true" />
                  </button>
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      {/*
        文件选择藏在 label 后面而不是用一个受控的隐藏 input：
        原生 `<input type="file">` 的样式几乎不可控，而 `label` 包住它
        能让整个按钮区域都可点（包括键盘：label 关联后 input 可聚焦）。
      */}
      <div>
        <input
          ref={inputRef}
          id={`finding-attachments-${findingId}`}
          type="file"
          multiple
          className="sr-only"
          onChange={(e) => void handleFiles(e.target.files)}
        />
        <label
          htmlFor={`finding-attachments-${findingId}`}
          className={cn(
            'inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-sm border px-2.5 text-xs',
            'transition-ui duration-fast',
            /*
              不压 opacity（「处理中…」要读得清，那是用户确认"它真的在动"的唯一证据）。
              `<label>` 没有 disabled 属性，用不了 `disabled:` 伪类；而条件拼
              `hover:border-line/50` 去覆盖正常态 `hover:border-line-strong` 是静默失效
              （同特异性，胜负看生成顺序）。所以用互斥三元。
            */
            busy
              ? 'pointer-events-none border-line/50 text-ink-muted'
              : 'border-line text-ink-muted hover:border-line-strong hover:text-ink',
          )}
        >
          {busy ? (
            <Loader2 size={12} strokeWidth={2} className="animate-spin" aria-hidden="true" />
          ) : (
            <Upload size={12} strokeWidth={2} aria-hidden="true" />
          )}
          {busy ? '处理中…' : '上传附件'}
        </label>
      </div>
    </div>
  )
}

/** 字节 → 人话。附件常是几十 KB 到几 MB，所以只用到 MB 这一档。 */
function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  const kb = bytes / 1024
  if (kb < 1024) return `${Math.round(kb)} KB`
  return `${(kb / 1024).toFixed(1)} MB`
}
