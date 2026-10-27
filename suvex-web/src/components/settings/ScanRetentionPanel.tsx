import { cn } from '@/lib/cn'
import {
  loadScanRetention,
  saveScanRetention,
  useScanRetention,
} from '@/data/scan-retention'
import { errorMessage } from '@/lib/api'
import { Field, TextInput } from './Field'
import { SaveBar, SettingsPanel, type SaveState } from './SaveBar'
import { useEffect, useState } from 'react'

/**
 * 扫描保留规则（任务 9）。
 *
 * 一条规则：「失败 / 已终止的任务，N 天后自动归档」，外加回收站保留期，
 * 以及**归档产物的清理期**（README 缺口清单「扫描产物目录只增不减」）。
 *
 * ## 三个数字是一条生命周期，所以放在同一个面板里
 *
 *     0 天            30 天                60 天            90 天
 *     │ 扫描结束      │ 失败/终止的自动归档 │ 回收站到期删除  │ 产物目录清理
 *
 * 分开两个面板的话，用户调完"归档"不会想到磁盘上还有一份产物在占地方。
 * 放一起才能看出它们是同一件事的三段。
 *
 * ## 为什么 `lastSweepAt` 要显示出来
 *
 * 这是这个面板里最容易被忽略、却最影响信任的一行。没有它，用户设了
 * "30 天"，回列表一看昨天那条失败的还在，只能猜：
 * **规则没生效**，还是**时间没到**？两种猜测导致的下一步动作完全不同。
 * 把上次清理时刻写出来，那个问题就变成了"上次清理是 3 分钟前，还没到"。
 *
 * ## 为什么每个输入框的 help 都要说清"0 是什么意思"
 *
 * "0 = 关闭"是一个**约定**，不是常识 —— 有人会以为 0 表示"立刻归档"。
 * 猜错的后果是记录被意外归档或规则形同虚设，两种都值得用一行字防住。
 */
export function ScanRetentionPanel() {
  const { config, status, error } = useScanRetention()
  /*
    ★ 只存**用户改过的**值，显示值由它和服务端的 config 派生。

    第一版写的是"effect 里把 config 同步进两个 useState"，而项目有一条
    `react-hooks/set-state-in-effect` 规则禁止 effect 里同步 setState ——
    它挡的不是风格问题：那种写法会让"服务端数据"和"输入框内容"变成
    **两份需要手动保持一致的真相**，而同步的时机永远差一帧。

    派生写法没有这个问题：config 一变，没被用户碰过的输入框自然跟着变；
    用户一改，`draft` 就接管。清空 `draft` 即"回到服务端的值"。
  */
  const [draft, setDraft] = useState<{
    archive?: string
    trash?: string
    artifact?: string
  }>({})
  const [saveState, setSaveState] = useState<SaveState>({ kind: 'idle' })

  useEffect(() => {
    void loadScanRetention()
  }, [])

  const archiveDays = draft.archive ?? (config ? String(config.archiveAfterDays) : '')
  const trashDays = draft.trash ?? (config ? String(config.trashRetentionDays) : '')
  const artifactDays =
    draft.artifact ?? (config ? String(config.artifactRetentionDays) : '')
  const setArchiveDays = (v: string) => setDraft((d) => ({ ...d, archive: v }))
  const setTrashDays = (v: string) => setDraft((d) => ({ ...d, trash: v }))
  const setArtifactDays = (v: string) => setDraft((d) => ({ ...d, artifact: v }))

  const parse = (raw: string): number | null => {
    const trimmed = raw.trim()
    if (trimmed === '') return null
    const value = Number(trimmed)
    if (!Number.isInteger(value) || value < 0) return null
    return value
  }

  const archiveValue = parse(archiveDays)
  const trashValue = parse(trashDays)
  const artifactValue = parse(artifactDays)
  const invalid =
    archiveValue === null || trashValue === null || artifactValue === null
      ? '请填写 0 或正整数（0 表示关闭）'
      : null

  const dirty =
    config !== null &&
    (archiveValue !== config.archiveAfterDays ||
      trashValue !== config.trashRetentionDays ||
      artifactValue !== config.artifactRetentionDays)

  const handleSave = async () => {
    if (
      invalid ||
      archiveValue === null ||
      trashValue === null ||
      artifactValue === null
    ) {
      setSaveState({ kind: 'failed', error: invalid ?? '取值不合法' })
      return
    }
    if (!dirty) {
      setSaveState({ kind: 'unchanged' })
      return
    }
    setSaveState({ kind: 'saving' })
    try {
      await saveScanRetention({
        archiveAfterDays: archiveValue,
        trashRetentionDays: trashValue,
        artifactRetentionDays: artifactValue,
      })
      setDraft({})  // 清空草稿 → 显示值回到服务端刚返回的那份
      setSaveState({ kind: 'saved', at: new Date().toISOString() })
    } catch (err) {
      setSaveState({ kind: 'failed', error: errorMessage(err) })
    }
  }

  return (
    <SettingsPanel
      id="scan-retention"
      title="扫描保留"
      description="让扫描列表自己保持清爽：到期的记录自动归档，回收站里的按保留期清除，归档的产物到点清理磁盘。"
    >
      {status === 'loading' || status === 'idle' ? (
        <p className="text-2xs text-ink-faint">正在读取…</p>
      ) : status === 'error' ? (
        <div className="flex flex-col gap-2">
          <p role="alert" className="text-2xs text-signal-risk">
            读取保留规则失败：{errorMessage(error)}
          </p>
          <button
            type="button"
            onClick={() => void loadScanRetention()}
            className="w-fit text-xs text-accent-strong underline hover:text-accent"
          >
            重试
          </button>
        </div>
      ) : (
        <>
          <div className="grid gap-panel sm:grid-cols-2">
            <Field
              label="自动归档"
              htmlFor="retention-archive-days"
              help="失败 / 已终止的扫描，过了这么多天自动归档。填 0 关闭这条规则。"
              error={invalid}
            >
              <TextInput
                id="retention-archive-days"
                value={archiveDays}
                onChange={setArchiveDays}
                mono
                placeholder="30"
              />
            </Field>

            <Field
              label="回收站保留期"
              htmlFor="retention-trash-days"
              help="回收站里的扫描过这么多天会被永久删除。填 0 表示不自动清除。"
            >
              <TextInput
                id="retention-trash-days"
                value={trashDays}
                onChange={setTrashDays}
                mono
                placeholder="30"
              />
            </Field>

            {/*
              ★ 第三个数字（README 缺口清单「扫描产物目录只增不减」）：
              产物清理是这条生命周期里**唯一不可逆**的一步，所以它的
              help 要把"删什么、留什么"说清楚 —— 否则用户会以为它连
              附件一起删，从而不敢开启。

              ⚠️ 2026-10-26 审计修正：原文写「附件原件与 challenge 目录
              不受影响」，**其中后半句是错的**。`challenge/` 住在
              `<scan_id>/` 里面（`runner.challenge_dir()` = `scan_root /
              scan_id / CHALLENGE_DIRNAME`），而 `sweep_retired_artifacts`
              走 `remove_scan_artifacts` 的 `shutil.rmtree(scan_root/<scan_id>)`
              会把整个目录端掉 —— `challenge/` 跟着走。
              真正不受影响的是 `_uploads/<scan_id>/` 里的**原件**（按 id
              拼路径，构造上碰不到兄弟目录）。`runner.py:715-718` 的注释
              其实早就说对了这点（"`challenge/` 跟着产物一起删是对的"），
              是界面文案写反了。
            */}
            <Field
              label="归档产物保留期"
              htmlFor="retention-artifact-days"
              help="已归档的扫描，产物目录（扫描日志、报告素材、challenge/ 里解开的题目素材）再留这么多天就清理掉，释放磁盘。附件原件不受影响——它在 _uploads/ 下，重跑同一道题还要用。填 0 表示不自动清理。"
            >
              <TextInput
                id="retention-artifact-days"
                value={artifactDays}
                onChange={setArtifactDays}
                mono
                placeholder="90"
              />
            </Field>
          </div>

          <p className={cn('text-2xs', config?.lastSweepAt ? 'text-ink-faint' : 'text-ink-ghost')}>
            {config?.lastSweepAt
              ? `上次清理：${new Date(config.lastSweepAt).toLocaleString()}（保存后也会立刻清理一次）`
              : '还没执行过清理。保存一次即可立刻生效。'}
          </p>

          <SaveBar
            state={saveState}
            dirty={dirty}
            disabled={Boolean(invalid)}
            onSave={() => void handleSave()}
            onReset={() => {
              // 清空草稿即"回到服务端的值" —— 不需要知道那个值是多少
              setDraft({})
              setSaveState({ kind: 'idle' })
            }}
          />
        </>
      )}
    </SettingsPanel>
  )
}

export default ScanRetentionPanel
