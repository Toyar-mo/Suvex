import { useMemo, useState } from 'react'
import { AlertTriangle, Check, ChevronDown, ChevronRight, Search } from 'lucide-react'

import { cn } from '@/lib/cn'
import type { KernelRecipe } from '@/data/tools'

/**
 * 「命令行工具」面板 —— 沙箱镜像里装着的二进制**怎么调**。
 *
 * ## 为什么它与上面的「内置工具」是两个面板
 *
 * | | 内置工具 | 本面板 |
 * | --- | --- | --- |
 * | 是什么 | 引擎能调的动作（`run_shell` / `http_request`） | 镜像里的二进制（nmap / sqlmap） |
 * | 模型怎么用 | `ACTION: {"tool": "run_shell"}` | 把命令写进 `run_shell` 的参数里 |
 * | 有开关吗 | 有（关掉就真的调不动） | **没有**（装没装由镜像决定） |
 *
 * 合在一个列表里会造出假的权力：用户以为关掉 `nmap` 就调不动了，
 * 而 `run_shell nmap ...` 照样能跑。所以分面板、并且在本面板里**不画开关**。
 *
 * ## 为什么折叠而不是平铺
 *
 * 34 条配方全展开是一屏放不下的长文。而这一页最常见的动作是
 * 「确认某个工具能不能用 / 它默认会漏什么」—— 目标明确但不集中。
 * 所以：**名字 + 一句话永远可见**（扫一眼就够了），
 * 参数与局限按需展开（那是要细看时才看的）。
 *
 * ## ★ 局限说明用红色，而不是灰色小字
 *
 * 它答的是「用默认参数跑会漏掉什么」，而漏掉的后果**不报错**：
 * 实测过同一目标、同一工具、只补一个参数就从 0 命中变 2 命中，
 * 而模型看到那个 0 写下的是「目标没有这个漏洞」。
 * 用灰色小字写，等于承认它「可以不看」。
 */
export function RecipesPanel({ recipes }: { recipes: KernelRecipe[] }) {
  const [query, setQuery] = useState('')
  const [onlyLimited, setOnlyLimited] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase()
    return recipes.filter((recipe) => {
      if (onlyLimited && !recipe.defaultLimitation) return false
      if (!q) return true
      // 搜名字、中文说明、**以及局限文本本身** —— 有人会记得
      // 「有个工具默认只扫一千个端口」而不记得它叫 nmap。
      return (
        recipe.name.toLowerCase().includes(q) ||
        recipe.summary.toLowerCase().includes(q) ||
        recipe.group.toLowerCase().includes(q) ||
        recipe.defaultLimitation.toLowerCase().includes(q)
      )
    })
  }, [recipes, query, onlyLimited])

  const withLimits = recipes.filter((recipe) => recipe.defaultLimitation).length

  return (
    <section className="rounded-md border border-line bg-surface">
      <header className="flex flex-wrap items-start justify-between gap-tight border-b border-line px-4 py-3">
        <div>
          <h2 className="text-sm font-medium text-ink">命令行工具</h2>
          <p className="mt-1 max-w-[64ch] text-[13px] leading-relaxed text-ink-muted">
            沙箱镜像里装着的二进制<b>怎么调</b>，以及<b>用默认参数跑会漏掉什么</b>。
            这些<b>没有开关</b> —— 装没装由镜像决定，不是由配置决定。
          </p>
        </div>
        <span className="shrink-0 text-[12px] text-ink-faint">
          {withLimits} / {recipes.length} 条写了局限
        </span>
      </header>

      <div className="flex flex-wrap items-center gap-tight border-b border-line px-4 py-2">
        <div className="relative min-w-[220px] flex-1">
          <Search
            className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-ink-faint"
            strokeWidth={1.75}
          />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索工具（名字 / 用途 / 局限）"
            className="h-8 w-full rounded-sm border border-line bg-surface pr-2 pl-7 text-[13px] text-ink outline-none focus:border-line-accent"
          />
        </div>
        <button
          type="button"
          onClick={() => setOnlyLimited((prev) => !prev)}
          className={cn(
            'rounded-sm border px-2 py-1 text-[12px] transition-ui duration-fast',
            onlyLimited
              ? 'border-line-accent text-ink'
              : 'border-line text-ink-muted hover:border-line-strong',
          )}
        >
          只看有局限的
        </button>
        {(query || onlyLimited) && (
          <button
            type="button"
            onClick={() => {
              setQuery('')
              setOnlyLimited(false)
            }}
            className="text-[12px] text-ink-faint transition-ui duration-fast hover:text-ink"
          >
            清除筛选
          </button>
        )}
        <span className="text-[12px] text-ink-faint">
          {visible.length} / {recipes.length}
        </span>
      </div>

      {visible.length === 0 ? (
        <p className="px-4 py-6 text-center text-[13px] text-ink-muted">
          没有匹配的工具。
          {onlyLimited && recipes.length > 0 && withLimits === 0
            ? '（这一版还没有任何一条写了局限说明）'
            : ''}
        </p>
      ) : (
        <ul>
          {visible.map((recipe) => (
            <RecipeRow
              key={recipe.name}
              recipe={recipe}
              open={expanded === recipe.name}
              onToggle={() => setExpanded(expanded === recipe.name ? null : recipe.name)}
            />
          ))}
        </ul>
      )}

      <footer className="border-t border-line px-4 py-2.5 text-[12px] leading-relaxed text-ink-faint">
        配方是<b>只读</b>的：它不是开关，只是把「该怎么调」与「默认会漏什么」写下来，
        让模型与你都看得到。要改调用方式，直接在任务里让它用不同的参数。
      </footer>
    </section>
  )
}

function RecipeRow({
  recipe,
  open,
  onToggle,
}: {
  recipe: KernelRecipe
  open: boolean
  onToggle: () => void
}) {
  const hasDetail =
    recipe.params.length > 0 || recipe.args.length > 0 || Boolean(recipe.defaultLimitation)

  return (
    <li className="border-b border-line/60 last:border-b-0">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full items-start gap-tight px-4 py-2.5 text-left transition-ui duration-fast hover:bg-surface-raised/30"
      >
        <span className="mt-0.5 shrink-0 text-ink-faint">
          {hasDetail ? (
            open ? (
              <ChevronDown className="size-3.5" strokeWidth={2} />
            ) : (
              <ChevronRight className="size-3.5" strokeWidth={2} />
            )
          ) : (
            // 没有可展开内容时给一个占位，让左边对齐 —— 否则有细节和没细节的
            // 两行会错开 14px，看起来像缩进层级不同。
            <span className="block size-3.5" />
          )}
        </span>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-tight">
            {/*
              ★ 等宽字体：`name` 同时是配方名和二进制名，模型就是拿这个名字
                去 `run_shell` 里敲的。用正文字体显示会让人以为它可以被翻译。
            */}
            <code className="font-mono text-[13px] text-ink">{recipe.name}</code>
            {recipe.command !== recipe.name && (
              <span className="text-[11px] text-ink-faint">→ {recipe.command}</span>
            )}
            {recipe.defaultLimitation && (
              <span
                className="inline-flex items-center gap-1 rounded-sm border border-signal-risk/40 px-1.5 py-px text-[11px] text-signal-risk"
                title="用默认参数跑会漏掉东西"
              >
                <AlertTriangle className="size-3" strokeWidth={2} />
                默认会漏
              </span>
            )}
          </div>
          <p className="mt-0.5 text-[12px] leading-relaxed text-ink-muted">{recipe.summary}</p>
        </div>
      </button>

      {open && hasDetail && (
        <div className="border-t border-line/40 bg-surface-raised/20 px-4 py-2.5 pl-[3.25rem]">
          {recipe.args.length > 0 && (
            <p className="text-[12px] text-ink-muted">
              默认就带：<code className="font-mono text-ink">{recipe.args.join(' ')}</code>
            </p>
          )}

          {recipe.params.length > 0 && (
            <ul className="mt-1.5 flex flex-col gap-1">
              {recipe.params.map((param) => (
                <li key={param.name} className="flex flex-wrap items-baseline gap-tight text-[12px]">
                  <code className="font-mono text-ink">{param.name}</code>
                  {param.flag && <code className="font-mono text-ink-faint">{param.flag}</code>}
                  {param.required && (
                    <span className="text-[11px] text-signal-risk">必填</span>
                  )}
                  {param.default && (
                    <span className="text-[11px] text-ink-faint">默认 {param.default}</span>
                  )}
                  {param.description && (
                    <span className="text-ink-muted">{param.description}</span>
                  )}
                </li>
              ))}
            </ul>
          )}

          {recipe.defaultLimitation && (
            <div className="mt-2 flex items-start gap-tight rounded-sm border border-signal-risk/30 bg-signal-risk/5 px-2.5 py-2">
              <AlertTriangle
                className="mt-0.5 size-3.5 shrink-0 text-signal-risk"
                strokeWidth={1.75}
              />
              <div className="min-w-0">
                <p className="text-[12px] font-medium text-signal-risk">默认参数下做不到什么</p>
                {/*
                  ★ 保留换行：局限说明里有的是分点写的（`·` 开头）。
                    用普通 <p> 会把它们挤成一段，看不出是几条独立的边界。
                */}
                <p className="mt-0.5 text-[12px] leading-relaxed whitespace-pre-line text-ink-muted">
                  {recipe.defaultLimitation}
                </p>
              </div>
            </div>
          )}

          {recipe.allowedExitCodes.length > 1 && (
            <p className="mt-1.5 flex items-center gap-1 text-[12px] text-ink-muted">
              <Check className="size-3 shrink-0 text-ink-faint" strokeWidth={2} />
              退出码 <code className="font-mono">{recipe.allowedExitCodes.join(' / ')}</code>{' '}
              都不算失败 —— 包括「没命中」。
            </p>
          )}
        </div>
      )}
    </li>
  )
}
