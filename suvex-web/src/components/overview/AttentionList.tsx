import { Link } from 'react-router-dom'
import { CalendarClock, Info, ShieldAlert, SquareCheckBig } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { cn } from '@/lib/cn'
import type { AttentionItem, AttentionLevel } from '@/data/overview'

/**
 * 总览页「现在最该做什么」（v0.95，学 CyberStrikeAI 的 recommended actions）。
 *
 * ## 为什么这块与活动流是两件事
 *
 * 活动流回答"正在发生什么"，这块回答"我该动哪件"。
 * 一个只在有扫描在跑时才有内容，另一个在任何时候都有意义
 * （"3 个计划连续失败"与有没有扫描在跑无关）。
 * 合并成一块的话，无事可做时用户看到的是一片空白 —— 而"没事"与
 * "没人告诉我该看哪里"是两种完全不同的状态，前者是好消息。
 *
 * ## 排序在后端，这里只渲染
 *
 * 两边都排的话，"为什么第 2 条在第 1 条上面"会变成一件说不清的事。
 * 规则只存在一处（`suvex-api/app/overview/service.py: build_attention`）。
 *
 * ## ★ `kind` 认不出时**忽略**那一条，不报错
 *
 * 后端将来加一种新建议（比如"有 3 个模型密钥过期"）时，正在运行的旧前端
 * 不该因此整块崩掉 —— 少显示一条建议是可接受的降级，
 * 白屏不是。这与 `SeverityChart` 对未知 severity 退化的处理是同一条纪律。
 */

/** 语义标识 → 图标与跳转。认不出的 kind 直接跳过（见文件头注释）。 */
const KIND_META: Record<string, { icon: LucideIcon; to: string; cta: string }> = {
  unresolved_critical: {
    icon: ShieldAlert,
    // ⚠️ 带 `?severity=critical` 而不是只跳列表：用户从"3 个严重漏洞"
    //    点进去，看到的应该是那 3 个，而不是全部 40 条重新找一遍。
    to: '/findings?severity=critical',
    cta: '去处置',
  },
  unresolved_high: {
    icon: ShieldAlert,
    to: '/findings?severity=high',
    cta: '去处置',
  },
  pending_approval: {
    icon: SquareCheckBig,
    // 审批在对话页里逐条呈现，跳对话页而不是"审批中心"（没有这个页面）
    to: '/chat',
    cta: '去审批',
  },
  failing_schedule: {
    icon: CalendarClock,
    to: '/schedules',
    cta: '去检查',
  },
}

/**
 * 等级 → 视觉。三档刻意不同构：
 * urgent 用红色左边条 + 实心图标底，warning 用琥珀色，info 用中性灰。
 *
 * ⚠️ 不用「红色 label 但同样的左边条」这种只靠颜色区分的做法 ——
 *    色盲用户（约 8% 的男性）会完全看不出差别。所以除了颜色，
 *    左边条的**宽度**与图标的**填充**也不同。
 */
const LEVEL_META: Record<AttentionLevel, { bar: string; iconWrap: string; text: string }> = {
  urgent: {
    bar: 'bg-signal-risk',
    iconWrap: 'bg-signal-risk/10 text-signal-risk',
    text: 'text-signal-risk',
  },
  warning: {
    bar: 'bg-signal-warn',
    iconWrap: 'bg-signal-warn/10 text-signal-warn',
    text: 'text-signal-warn',
  },
  info: {
    bar: 'bg-line-strong',
    iconWrap: 'bg-canvas text-ink-muted',
    text: 'text-ink-muted',
  },
}

/** 等级 → 给读屏器的一句话。颜色和图标读不出来，文字要补上。 */
const LEVEL_LABEL: Record<AttentionLevel, string> = {
  urgent: '紧急',
  warning: '需留意',
  info: '提示',
}

export interface AttentionListProps {
  items: AttentionItem[]
  className?: string
}

export function AttentionList({ items, className }: AttentionListProps) {
  /*
   * 过滤掉认不出的 kind —— 见文件头注释。
   *
   * ★ 判据就是 `KIND_META` 本身，**不是**另抄一份"认得的 kind"清单。
   *   一开始我把这个判据抽到了 `data/attention.ts` 供测试断言用，结果
   *   那份清单与本文件各改一半时会越过过滤、落到下面 `KIND_META[...]`
   *   上崩掉（"看得懂列表"与"画得出条目"变成了两件事）——
   *   等于亲手在文件头承诺的"不白屏"上开了个洞。
   *   现在只认这一个来源：**能画出来的才允许通过过滤**。
   */
  const known = items.filter((item) => KIND_META[item.kind] !== undefined)

  return (
    <section
      aria-labelledby="attention-list-title"
      className={cn('rounded-md border border-line bg-surface p-panel', className)}
    >
      <div className="flex items-baseline justify-between gap-tight">
        <h2 id="attention-list-title" className="text-sm font-medium text-ink">
          该做什么
        </h2>
        {known.length > 0 && (
          <span className="mono-ui tabular text-ink-faint" data-numeric>
            {known.length} 项
          </span>
        )}
      </div>

      {known.length === 0 ? (
        /*
         * ★ 空状态要**说明为什么空**，而不是只说"没有"。
         *
         * "没有待办"与"我看不出来有没有"在空列表上长得一模一样。
         * 前者是好消息，后者是故障 —— 用户无从分辨就会一直怀疑这块坏了。
         * 所以这里明确列出被检查过的四件事。
         */
        <div className="mt-panel flex items-start gap-tight rounded-sm bg-canvas p-tight">
          <Info
            size={14}
            strokeWidth={1.75}
            className="mt-0.5 shrink-0 text-signal-ok"
            aria-hidden="true"
          />
          <p className="text-xs leading-relaxed text-ink-muted">
            没有需要你处理的事。这里会检查四项：待处置的严重/高危漏洞、
            等你确认的智能体动作、连续失败的定时复查。
          </p>
        </div>
      ) : (
        <ul className="mt-panel flex flex-col gap-tight">
          {known.map((item) => {
            const kind = KIND_META[item.kind]
            // 上面已经过滤过，理论上到不了这里。留这个判断是**为了让
            // 类型收敛**（而不是 `!` 断言）：`!` 会在两者不一致时变成
            // 运行时崩溃，而这里选择少画一条。
            if (!kind) return null
            const level = LEVEL_META[item.level] ?? LEVEL_META.info
            const Icon = kind.icon
            return (
              <li key={item.kind}>
                <Link
                  to={kind.to}
                  className={cn(
                    'group relative flex items-start gap-tight rounded-sm border border-line',
                    'py-2.5 pl-3 pr-2.5 transition-ui duration-fast',
                    'hover:border-line-strong hover:bg-canvas',
                  )}
                >
                  {/* 左边条：等级的第二条信息通道（不只靠颜色） */}
                  <span
                    className={cn('absolute inset-y-0 left-0 w-0.5 rounded-l-sm', level.bar)}
                    aria-hidden="true"
                  />
                  <span
                    className={cn(
                      'mt-px flex size-6 shrink-0 items-center justify-center rounded-sm',
                      level.iconWrap,
                    )}
                    aria-hidden="true"
                  >
                    <Icon size={13} strokeWidth={2} />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-baseline gap-1.5">
                      {/* 等级文字：第三条信息通道，也是读屏器唯一能读到的 */}
                      <span className={cn('shrink-0 text-2xs font-medium', level.text)}>
                        {LEVEL_LABEL[item.level] ?? item.level}
                      </span>
                      <span className="truncate text-xs font-medium text-ink">{item.title}</span>
                    </span>
                    {item.detail && (
                      <span className="mt-0.5 block text-2xs leading-relaxed text-ink-muted">
                        {item.detail}
                      </span>
                    )}
                  </span>
                  <span className="mt-0.5 shrink-0 text-2xs text-ink-faint transition-ui duration-fast group-hover:text-ink-muted">
                    {kind.cta} →
                  </span>
                </Link>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

export default AttentionList
