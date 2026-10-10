import { PageHeader } from '@/components/layout/AppShell'

/**
 * 占位页面的统一实现。
 *
 * 这是任务 1 的**诚实占位**，不是假内容。刻意不做这几件事：
 *   - 不铺假表格、假图表、假指标卡。那些会让人以为功能已经存在，
 *     而且等真数据接进来时全部要删（taste：不编造具体值）。
 *   - 不做"coming soon"营销话术。
 *   - 不写任务编号。第一版我在这里写了 "Delivered in task 2/4/5/6"，
 *     但 brief 里只明确说过任务 3 是 Agent Activity —— 其余编号是我推的。
 *     推断出来的具体值比留白更糟：看的人无法分辨哪个是排期、哪个是猜测。
 *
 * 它只回答两个问题：这是哪个页面 / 它将来干什么。
 * 中间那块虚线框是**唯一**允许的装饰性容器 —— 它表达"这里还没有东西"，
 * 是语义而不是装饰（design-system §7 禁止的是等大卡片阵列，不是这一处）。
 */

export interface PagePlaceholderProps {
  /** 对应 navigation.ts 的 NavItem.id */
  navId: string
  /** 这个页面将来会有哪些区域 —— 取自 brief 里的功能范围，不额外发明 */
  planned: string[]
}

export function PagePlaceholder({ navId, planned }: PagePlaceholderProps) {
  return (
    <>
      <PageHeader navId={navId} />

      <div className="px-gutter py-bay">
        <div className="max-w-[68ch] rounded-md border border-dashed border-line-strong p-panel">
          <p className="mono-ui text-ink-faint">占位 · 尚未接入数据</p>

          <p className="mt-panel text-sm text-ink-muted">这个页面将来包含：</p>
          <ul className="mt-tight flex flex-col gap-1.5">
            {planned.map((entry) => (
              <li key={entry} className="flex gap-tight text-sm text-ink-muted">
                <span
                  aria-hidden="true"
                  className="mt-2 h-1 w-1 shrink-0 rounded-pill bg-ink-ghost"
                />
                <span>{entry}</span>
              </li>
            ))}
          </ul>

          <p className="mt-panel text-xs text-ink-faint">
            这里还没有接数据，本次构建只是把外壳搭起来。
          </p>
        </div>
      </div>
    </>
  )
}

export default PagePlaceholder
