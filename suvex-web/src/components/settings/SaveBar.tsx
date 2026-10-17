import { cn } from '@/lib/cn'
import { AlertCircle, Check, Clock, Info } from 'lucide-react'
import type { ReactNode } from 'react'

/**
 * 保存反馈条 —— **常驻，不消失**。
 *
 * ★ 这是验收要求「不要用一闪而过的 toast 糊弄」的直接落点。
 *
 *   为什么常驻是对的：
 *     1. toast 的问题是**它把"看过"当成了"知道"**。用户抬头晚了一秒就错过，
 *        而设置页要回答的问题恰恰是"我这次的改动到底存住了没有" ——
 *        这个问题在任何时刻都该有答案，不该依赖"你有没有及时看到那 3 秒"。
 *     2. 保存是有后果的动作（密钥、通知渠道）。给一个能停留的确认，
 *        用户才有可能回头核对。这也是"信任感"的一部分：
 *        一个连保存结果都不肯留下来的界面，不值得托付凭据。
 *     3. 失败时更是如此 —— 失败了却让提示飘走，用户会以为保存成功了。
 *
 *   的状态机见 SaveState。四态各有明确的措辞，不混用：
 *     idle      什么都没发生（不显示）
 *     saving    正在保存（显示进行中，禁用按钮）
 *     saved     已保存 + **保存时刻**（时刻是关键：用户能据此确认是这一次的保存）
 *     failed    失败 + 原因 + 草稿仍在（输入不丢）
 *     unchanged 没有改动（点保存但没改过，不能说"已保存"——那是虚假的动作感）
 */

export type SaveState =
  | { kind: 'idle' }
  | { kind: 'saving' }
  | { kind: 'saved'; at: string }
  | { kind: 'failed'; error: string }
  | { kind: 'unchanged' }

export interface SaveBarProps {
  state: SaveState
  /** 有未保存改动时为 true —— 用于给按钮加"未保存"标记 */
  dirty: boolean
  onSave: () => void
  onReset: () => void
  /** 按钮文案，默认"保存" */
  saveLabel?: string
  disabled?: boolean
  /** 补充说明（如"改完记得回来保存"） */
  note?: ReactNode
}

/** ISO → "15:30:42"。保存确认里精确到秒，让用户能确认这是刚才那一次 */
function formatClock(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export function SaveBar({
  state,
  dirty,
  onSave,
  onReset,
  saveLabel = '保存',
  disabled = false,
  note,
}: SaveBarProps) {
  const saving = state.kind === 'saving'

  return (
    <div className="flex flex-col gap-tight">
      <div className="flex flex-wrap items-center gap-tight">
        <button
          type="button"
          onClick={onSave}
          disabled={saving || disabled}
          className={cn(
            'inline-flex h-9 items-center gap-1.5 rounded-sm px-3.5 text-sm transition-ui duration-fast',
            saving || disabled
              ? 'cursor-not-allowed bg-surface-raised text-ink-faint'
              : dirty
                ? 'bg-accent text-canvas hover:bg-accent-strong'
                : 'border border-line text-ink-muted hover:border-line-strong hover:text-ink',
          )}
        >
          {saving ? '正在保存…' : saveLabel}
        </button>

        <button
          type="button"
          onClick={onReset}
          disabled={saving || !dirty}
          className={cn(
            'inline-flex h-9 items-center rounded-sm px-2.5 text-sm transition-ui duration-fast',
            saving || !dirty
              ? 'cursor-not-allowed text-ink-ghost'
              : 'text-ink-muted hover:bg-surface-raised hover:text-ink',
          )}
        >
          放弃改动
        </button>

        {/*
          "未保存"标记。它是**状态**而不是提示 —— 一直挂在按钮旁边，
          直到保存成功或放弃改动为止。
          没有它的话，用户改了一半去别的 tab 逛一圈回来，
          完全看不出这里有没有未落地的改动。
        */}
        {dirty && !saving && (
          <span className="inline-flex items-center gap-1 text-2xs text-signal-warn">
            <span aria-hidden="true" className="h-1.5 w-1.5 rounded-pill bg-signal-warn" />
            有未保存的改动
          </span>
        )}

        {/* 保存结果：常驻显示，不自动消失 */}
        <SaveStateLabel state={state} />
      </div>

      {note && <div className="text-2xs text-ink-faint">{note}</div>}
    </div>
  )
}

/** 保存结果的常驻标签 */
function SaveStateLabel({ state }: { state: SaveState }) {
  switch (state.kind) {
    case 'idle':
      return null

    case 'saving':
      return (
        <span className="inline-flex items-center gap-1 text-2xs text-ink-muted">
          {/*
            ★ 任务 8 改：`animate-pulse` → `animate-breath`。
            两者看起来都是"闪烁"，但它们是两套节奏：pulse 是 Tailwind 的
            通用工具（1→0.5，2s），breath 是本项目的 token（0.35↔0.85，2.6s）。
            全站的状态点已经在用 breath，这里再用 pulse 就有了两种
            "正在进行的闪烁"—— 动效一致性检查要抓的正是这种游离值。
          */}
          <Clock size={11} strokeWidth={2} className="animate-breath" aria-hidden="true" />
          正在写入…
        </span>
      )

    case 'saved':
      return (
        <span
          role="status"
          className="inline-flex items-center gap-1 text-2xs text-signal-ok"
          // 精确到秒 —— 让用户能确认"这就是我刚才那次保存"
          title={`保存于 ${formatClock(state.at)}`}
        >
          <Check size={11} strokeWidth={2.5} aria-hidden="true" />
          已保存 · {formatClock(state.at)}
        </span>
      )

    case 'failed':
      return (
        <span role="alert" className="inline-flex items-center gap-1 text-2xs text-signal-risk">
          <AlertCircle size={11} strokeWidth={2} aria-hidden="true" />
          保存失败：{state.error}
        </span>
      )

    case 'unchanged':
      return (
        <span className="inline-flex items-center gap-1 text-2xs text-ink-faint">
          <Info size={11} strokeWidth={2} aria-hidden="true" />
          没有需要保存的改动
        </span>
      )
  }
}

/**
 * 面板外壳：标题 + 说明 + 内容 + 保存条。
 *
 * 每个面板自带保存条，而不是全页一个"保存全部"：
 *   provider、沙箱、通知、令牌是**四件独立的事**，
 *   一个全局保存会让用户改了一个开关却不敢点（怕连带提交别的）。
 *   分块保存同时让"哪一块没保存成功"变得明确。
 */
export function SettingsPanel({
  id,
  title,
  description,
  children,
  footer,
}: {
  id: string
  title: string
  description: string
  children: ReactNode
  footer?: ReactNode
}) {
  return (
    <section
      /*
        ★★ `id` 必须落在 `<section>` 上 —— 它就是锚点目标。

        `SettingsPage` 的导航用的是原生锚点（`<a href="#providers">` 等，
        见那边的 `SECTIONS`），而**锚点只认元素的 `id` 属性**。
        这里原来只把 `id` 拿去拼了 `${id}-heading`（给标题用），
        `<section>` 自己**没有 id** —— 于是：

        · 点导航标签：浏览器找不到 `#providers`，**什么都不做**
          （用户的原话是"点击这些不会跳转"）；
        · `SectionNav` 里那个 IntersectionObserver 观察的也是
          `document.getElementById('providers')`，拿到 null，
          而它写的是 `if (el) observer.observe(el)` —— **静默跳过**。
          结果是它一个区块都没观察，`active` 永远停在初值，
          **"模型服务"永远高亮**，滚到哪儿都不变。

        两个症状同一个根因。修法就是这一行。

        ⚠️ `aria-labelledby` 仍然指向 `${id}-heading`，两者不要合并：
            `id` 是"这一节"的锚点，`${id}-heading` 是"这一节标题"的锚点，
            语义不同（前者给跳转用，后者给读屏器念标题用）。
      */
      id={id}
      aria-labelledby={`${id}-heading`}
      /*
        ★ `scroll-mt` 补偿 sticky 导航的高度。

        锚点滚动会把目标滚到视口**最顶端**，而顶上有两个 sticky 元素：
        状态条（`top-0`，高 44px）与设置导航（`top-[52px]`，高 42px）——
        它们的下沿在 94px 处。不减掉这 94px，跳过去的区块标题会被
        压在导航底下，用户看到的是"跳了，但不知道跳到哪"。

        104px = 94（遮挡）+ 10（留一点呼吸）。数值来自浏览器里实测的
        两个 sticky 元素的 `top + height`，不是估的。
      */
      className="flex scroll-mt-[104px] flex-col gap-panel rounded-md border border-line bg-surface p-panel"
    >
      <div className="flex flex-col gap-1">
        <h2 id={`${id}-heading`} className="text-sm font-medium text-ink">
          {title}
        </h2>
        <p className="max-w-[68ch] text-xs text-ink-faint">{description}</p>
      </div>

      {children}

      {footer && <div className="border-t border-line pt-panel">{footer}</div>}
    </section>
  )
}

export default SettingsPanel
