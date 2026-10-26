import { Link } from 'react-router-dom'
import { BookOpen, Cable, Wrench } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { cn } from '@/lib/cn'
import type { ResourceCount, ResourceTone } from '@/data/overview'

/**
 * 总览页「可用能力」（v0.96，学 CyberStrikeAI dashboard 的六宫格）。
 *
 * ## 为什么这块必须存在
 *
 * 总览页原本只回答"发现了什么"，而那需要**先跑过扫描**。新装部署打开
 * 总览看到的是一片空状态，于是"这东西能干什么"这个问题没有任何地方回答 ——
 * 答案分散在三个设置页深处（工具开关、MCP 连接、技能库）。
 *
 * 这三格把"家底"摆到首屏：能调多少工具、接了几条外部连接、有多少篇速查卡。
 * 它们**不随扫描变化**，所以新装系统也能看到内容。
 *
 * ## ★ 只有三格，不是 CyberStrikeAI 的六格
 *
 * 它还有「知识 / 角色 / Agents」，这个代码库里**没有对应物**：
 *
 * | 它那格 | 为什么这里没有 |
 * |---|---|
 * | 知识 | `app/` 下没有 knowledge 模块 —— 没有对象就没有数 |
 * | 角色 | `CHAT_PERSONAS` 是写死的 7 个常量，永远显示 7，是装饰不是信息 |
 * | Agents | `agents.scan_id` 是级联外键，agent **属于某一次扫描**。照抄会得到一个随扫描数单调增长的数，读作"我们有 300 个智能体"是错的 |
 *
 * 宁可少三格，也不摆三个需要加注解才能看懂的数字 —— 那种格子会让
 * 用户开始怀疑旁边那三个真数字。
 *
 * ## 数据源与口径全在后端
 *
 * `count` 与 `detail` 都由 `overview.service.build_resources` 生成。
 * 前端**不重算**：一个数字两处算，漂移时无法判断哪个对，
 * 而"总览说 31 篇、技能库页说 30 篇"这种矛盾会直接摧毁对两边的信任。
 *
 * ## 认不出的 `kind` 直接跳过
 *
 * 与 `AttentionList` 同一条纪律：后端将来加一格时，正在运行的旧前端
 * 少画一格是可接受的降级，白屏不是。
 */

/** 语义标识 → 图标与跳转。认不出的 kind 跳过（见文件头注释）。 */
const KIND_META: Record<string, { icon: LucideIcon; to: string }> = {
  mcp_server: { icon: Cable, to: '/tools' },
  mcp_tool: { icon: Wrench, to: '/tools' },
  skill: { icon: BookOpen, to: '/skills' },
}

/**
 * 语气 → 视觉。
 *
 * ⚠️ `muted` 不是"不重要"，它是**读不到**。所以它不能长得像 0 ——
 *    0 是"确实没有"（比如真没配 MCP），`muted` 是"不知道有多少"。
 *    两者都画成灰数字的话，用户会把故障当成事实。
 */
const TONE_META: Record<ResourceTone, { value: string; label: string }> = {
  ok: { value: 'text-ink', label: '' },
  attention: { value: 'text-signal-warn', label: '待配置' },
  muted: { value: 'text-ink-faint', label: '读不到' },
}

export interface ResourceStripProps {
  resources: ResourceCount[]
  className?: string
}

export function ResourceStrip({ resources, className }: ResourceStripProps) {
  // 过滤掉认不出的 kind —— 见文件头注释。
  const known = resources.filter((item) => KIND_META[item.kind] !== undefined)

  // 一条都认不出（后端改了 kind 命名）时整块不渲染：
  // 画一个空壳标题比不画更让人以为坏了。
  if (known.length === 0) return null

  return (
    <section
      aria-labelledby="resource-strip-title"
      className={cn('rounded-md border border-line bg-surface p-panel', className)}
    >
      <div className="flex items-baseline justify-between gap-tight">
        <h2 id="resource-strip-title" className="text-sm font-medium text-ink">
          可用能力
        </h2>
        <span className="text-2xs text-ink-faint">部署时定，与扫描无关</span>
      </div>

      {/*
        ★ 用 `grid-cols-3` 固定三等分而不是 `auto-fit`：
          格数是后端的约定（三格），等分才能让三个数字横向对齐可比较。
          `auto-fit` 在两格时会把它们拉宽，看起来像是"少了一格"。
          窄屏退回单列：三格横排在手机上每格不到 100px，数字会被折行。
      */}
      <div className="mt-panel grid grid-cols-1 gap-panel sm:grid-cols-3">
        {known.map((item) => {
          const meta = KIND_META[item.kind]
          if (!meta) return null // 类型收敛用，理论上到不了
          const tone = TONE_META[item.tone] ?? TONE_META.muted
          const Icon = meta.icon
          return (
            <Link
              key={item.kind}
              to={meta.to}
              className={cn(
                'group flex flex-col gap-1.5 rounded-sm border border-line p-tight',
                'transition-ui duration-fast hover:border-line-strong hover:bg-canvas',
              )}
            >
              <span className="flex items-center gap-1.5 text-2xs text-ink-muted">
                <Icon size={12} strokeWidth={1.75} aria-hidden="true" />
                {item.label}
                {tone.label && (
                  <span className={cn('shrink-0 font-medium', tone.value)}>{tone.label}</span>
                )}
              </span>
              <span className={cn('mono-ui tabular text-xl font-medium', tone.value)} data-numeric>
                {item.count}
              </span>
              {item.detail && (
                <span className="text-2xs leading-relaxed text-ink-faint">{item.detail}</span>
              )}
            </Link>
          )
        })}
      </div>
    </section>
  )
}

export default ResourceStrip
