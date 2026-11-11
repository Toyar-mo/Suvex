import { useState } from 'react'

import { Modal } from '@/components/ui/Modal'
import { Button } from '@/components/ui/Button'
import { useI18n } from '@/i18n'
import type { CustomRecipeDraft, KernelRecipe, KernelRecipeParam, RecipeScenario } from '@/data/tools'

/**
 * 新增 / 编辑一条自定义工具配方。
 *
 * ## 参数为什么用 JSON 文本框
 *
 * 配方参数是「模型怎么拼命令」的说明，不是给人日常填的表单：一条工具通常
 * 0–4 个参数，而每个参数有 `format/flag/default/required` 四个字段。做成
 * 四列表格会让弹窗宽到必须横向滚动，且在窄屏上完全不可用。JSON 文本框的
 * 代价是"要懂结构"，但它与 `GET /kernel/recipes` 的形状**逐字一致** ——
 * 用户可以直接把内置配方的参数段复制过来改。
 */
export function CustomRecipeDialog({
  open,
  initial,
  busy,
  error,
  onClose,
  onSubmit,
}: {
  open: boolean
  initial: KernelRecipe | null
  busy: boolean
  error: string | null
  onClose: () => void
  onSubmit: (draft: CustomRecipeDraft) => void
}) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [summary, setSummary] = useState('')
  const [group, setGroup] = useState('自定义')
  const [scenario, setScenario] = useState<RecipeScenario>('both')
  const [command, setCommand] = useState('')
  const [args, setArgs] = useState('')
  const [params, setParams] = useState('[]')
  const [limitation, setLimitation] = useState('')
  const [localError, setLocalError] = useState<string | null>(null)

  const submit = () => {
    let parsedParams: KernelRecipeParam[]
    try {
      const raw = JSON.parse(params || '[]')
      if (!Array.isArray(raw)) throw new Error('params must be an array')
      parsedParams = raw as KernelRecipeParam[]
    } catch {
      setLocalError(t('tools.custom.paramsInvalid', undefined, '参数必须是合法的 JSON 数组。'))
      return
    }
    setLocalError(null)
    onSubmit({
      name: name.trim(),
      summary: summary.trim(),
      group: group.trim() || '自定义',
      scenario,
      command: command.trim(),
      args: args
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean),
      params: parsedParams,
      defaultLimitation: limitation.trim(),
    })
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={
        initial
          ? t('tools.custom.editTitle', undefined, '编辑自定义工具')
          : t('tools.custom.createTitle', undefined, '新增自定义工具')
      }
      widthClass="max-w-2xl"
      footer={
        <div className="flex items-center justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {t('common.cancel', undefined, '取消')}
          </Button>
          <Button onClick={submit} disabled={busy || !name.trim()}>
            {busy ? t('common.saving', undefined, '保存中…') : t('common.save', undefined, '保存')}
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-3 text-sm">
        {(localError || error) && (
          <p className="rounded-sm border border-signal-risk/40 bg-signal-risk/5 px-3 py-2 text-xs text-signal-risk">
            {localError || error}
          </p>
        )}

        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-xs text-ink-muted">{t('tools.custom.name', undefined, '工具名（二进制名）')}</span>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              disabled={Boolean(initial)}
              placeholder="mytool"
              className="h-8 rounded-sm border border-line bg-surface px-2 font-mono text-sm outline-none focus:border-line-accent disabled:opacity-60"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-ink-muted">{t('tools.custom.group', undefined, '分组')}</span>
            <input
              value={group}
              onChange={(event) => setGroup(event.target.value)}
              className="h-8 rounded-sm border border-line bg-surface px-2 text-sm outline-none focus:border-line-accent"
            />
          </label>
        </div>

        <label className="flex flex-col gap-1">
          <span className="text-xs text-ink-muted">{t('tools.custom.summary', undefined, '一句话说明')}</span>
          <input
            value={summary}
            onChange={(event) => setSummary(event.target.value)}
            placeholder={t('tools.custom.summaryPlaceholder', undefined, '它做什么、什么时候该用')}
            className="h-8 rounded-sm border border-line bg-surface px-2 text-sm outline-none focus:border-line-accent"
          />
        </label>

        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-xs text-ink-muted">{t('tools.custom.command', undefined, '执行命令')}</span>
            <input
              value={command}
              onChange={(event) => setCommand(event.target.value)}
              placeholder={name || 'mytool'}
              className="h-8 rounded-sm border border-line bg-surface px-2 font-mono text-sm outline-none focus:border-line-accent"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-ink-muted">{t('tools.custom.scenario', undefined, '主场景')}</span>
            <select
              value={scenario}
              onChange={(event) => setScenario(event.target.value as RecipeScenario)}
              className="h-8 rounded-sm border border-line bg-surface px-2 text-sm outline-none focus:border-line-accent"
            >
              <option value="both">both</option>
              <option value="ctf">ctf</option>
              <option value="pentest">pentest</option>
            </select>
          </label>
        </div>

        <label className="flex flex-col gap-1">
          <span className="text-xs text-ink-muted">{t('tools.custom.args', undefined, '固定参数（每行一个）')}</span>
          <textarea
            value={args}
            onChange={(event) => setArgs(event.target.value)}
            rows={2}
            className="rounded-sm border border-line bg-surface px-2 py-1.5 font-mono text-xs outline-none focus:border-line-accent"
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-xs text-ink-muted">{t('tools.custom.limitation', undefined, '默认参数下做不到什么')}</span>
          <textarea
            value={limitation}
            onChange={(event) => setLimitation(event.target.value)}
            rows={2}
            className="rounded-sm border border-line bg-surface px-2 py-1.5 text-xs outline-none focus:border-line-accent"
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-xs text-ink-muted">
            {t('tools.custom.params', undefined, '参数定义（JSON 数组）')}
          </span>
          <textarea
            value={params}
            onChange={(event) => setParams(event.target.value)}
            rows={6}
            spellCheck={false}
            className="rounded-sm border border-line bg-surface px-2 py-1.5 font-mono text-xs outline-none focus:border-line-accent"
          />
        </label>
      </div>
    </Modal>
  )
}
