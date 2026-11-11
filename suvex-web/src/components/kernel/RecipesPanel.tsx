import { useMemo, useState } from 'react'
import { AlertTriangle, Check, ChevronDown, ChevronRight, Search } from 'lucide-react'

import { cn } from '@/lib/cn'
import { filterRecipes, recipeCoverage, type KernelRecipe, type ScenarioFilter } from '@/data/tools'
import { useI18n } from '@/i18n'

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
  const { t } = useI18n()
  const [query, setQuery] = useState('')
  const [onlyLimited, setOnlyLimited] = useState(false)
  const [scenario, setScenario] = useState<ScenarioFilter>('all')
  const [expanded, setExpanded] = useState<string | null>(null)

  // 筛选逻辑在 `data/tools.ts`（纯函数、可单测），与「场景是包含关系」
  // 那条语义放在一起 —— 放到组件里会让它只在这个组件里成立。
  const visible = useMemo(
    () => filterRecipes(recipes, { query, scenario, onlyLimited }),
    [recipes, query, scenario, onlyLimited],
  )
  const coverage = useMemo(() => recipeCoverage(recipes), [recipes])
  const withLimits = coverage.limited

  return (
    <section className="rounded-md border border-line bg-surface">
      <header className="flex flex-wrap items-start justify-between gap-tight border-b border-line px-4 py-3">
        <div>
          <h2 className="text-sm font-medium text-ink">{t('tools.recipes.heading')}</h2>
          <p className="mt-1 max-w-[64ch] text-sm leading-relaxed text-ink-muted">
            {t('tools.recipes.body.a')}
            <b>{t('tools.recipes.body.bold1')}</b>
            {t('tools.recipes.body.b')}
            <b>{t('tools.recipes.body.bold2')}</b>
            {t('tools.recipes.body.c')}
            <b>{t('tools.recipes.body.bold3')}</b>
            {t('tools.recipes.body.d')}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-faint">
          <span>{t('tools.recipes.count', { n: recipes.length })}</span>
          <span className="text-ink-muted">{t('tools.recipes.ctf', { n: coverage.ctf })}</span>
          <span className="text-ink-muted">
            {t('tools.recipes.pentest', { n: coverage.pentest })}
          </span>
          <span>{t('tools.recipes.limited', { n: withLimits })}</span>
        </div>
      </header>

      <div className="flex flex-wrap items-center gap-tight border-b border-line px-4 py-2">
        <div className="relative min-w-[220px] flex-1">
          <Search
            className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-ink-faint"
            strokeWidth={1.75}
          />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t('tools.recipes.searchPlaceholder')}
            className="h-8 w-full rounded-sm border border-line bg-surface pl-7 pr-2 text-sm text-ink outline-none focus:border-line-accent"
          />
        </div>
        <div className="flex items-center gap-1" role="group" aria-label={t('tools.recipes.filterAria')}>
          {(
            [
              ['all', t('tools.recipes.all')],
              ['ctf', 'CTF'],
              ['pentest', t('tools.recipes.pentestChip')],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              onClick={() => setScenario(value)}
              aria-pressed={scenario === value}
              className={cn(
                'rounded-sm border px-2 py-1 text-xs transition-ui duration-fast',
                scenario === value
                  ? 'border-line-accent bg-accent-wash text-ink'
                  : 'border-line text-ink-muted hover:border-line-strong',
              )}
            >
              {label}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => setOnlyLimited((prev) => !prev)}
          className={cn(
            'rounded-sm border px-2 py-1 text-xs transition-ui duration-fast',
            onlyLimited
              ? 'border-line-accent text-ink'
              : 'border-line text-ink-muted hover:border-line-strong',
          )}
        >
          {t('tools.recipes.onlyLimited')}
        </button>
        {(query || onlyLimited || scenario !== 'all') && (
          <button
            type="button"
            onClick={() => {
              setQuery('')
              setOnlyLimited(false)
              setScenario('all')
            }}
            className="text-xs text-ink-faint transition-ui duration-fast hover:text-ink"
          >
            {t('tools.recipes.clear')}
          </button>
        )}
        <span className="text-xs text-ink-faint">
          {visible.length} / {recipes.length}
        </span>
      </div>

      {visible.length === 0 ? (
        <p className="px-4 py-6 text-center text-sm text-ink-muted">
          {t('tools.recipes.empty')}
          {onlyLimited && recipes.length > 0 && withLimits === 0
            ? t('tools.recipes.emptyLimited')
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

      <footer className="border-t border-line px-4 py-2.5 text-xs leading-relaxed text-ink-faint">
        {t('tools.recipes.footer.before')}
        <b>{t('tools.recipes.footer.bold')}</b>
        {t('tools.recipes.footer.after')}
        <br />
        {t('tools.recipes.footer2.before')}
        <b>{t('tools.recipes.footer2.bold')}</b>
        {t('tools.recipes.footer2.after')}
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
  const { t } = useI18n()
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
            <code className="font-mono text-sm text-ink">{recipe.name}</code>
            {recipe.command !== recipe.name && (
              <span className="text-2xs text-ink-faint">→ {recipe.command}</span>
            )}
            {recipe.defaultLimitation && (
              <span
                className="inline-flex items-center gap-1 rounded-sm border border-signal-risk/40 px-1.5 py-px text-2xs text-signal-risk"
                title={t('tools.recipes.limitationTitle')}
              >
                <AlertTriangle className="size-3" strokeWidth={2} />
                {t('tools.recipes.limitationBadge')}
              </span>
            )}
          </div>
          <p className="mt-0.5 text-xs leading-relaxed text-ink-muted">{recipe.summary}</p>
        </div>
      </button>

      {open && hasDetail && (
        <div className="border-t border-line/40 bg-surface-raised/20 px-4 py-2.5 pl-[3.25rem]">
          {recipe.args.length > 0 && (
            <p className="text-xs text-ink-muted">
              {t('tools.recipes.defaultArgs')}
              <code className="font-mono text-ink">{recipe.args.join(' ')}</code>
            </p>
          )}

          {recipe.params.length > 0 && (
            <ul className="mt-1.5 flex flex-col gap-1">
              {recipe.params.map((param) => (
                <li key={param.name} className="flex flex-wrap items-baseline gap-tight text-xs">
                  <code className="font-mono text-ink">{param.name}</code>
                  {param.flag && <code className="font-mono text-ink-faint">{param.flag}</code>}
                  {param.required && <span className="text-2xs text-signal-risk">{t('tools.recipes.required')}</span>}
                  {param.default && (
                    <span className="text-2xs text-ink-faint">
                      {t('tools.recipes.defaultValue', { value: param.default })}
                    </span>
                  )}
                  {param.description && <span className="text-ink-muted">{param.description}</span>}
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
                <p className="text-xs font-medium text-signal-risk">
                  {t('tools.recipes.limitationHeading')}
                </p>
                {/*
                  ★ 保留换行：局限说明里有的是分点写的（`·` 开头）。
                    用普通 <p> 会把它们挤成一段，看不出是几条独立的边界。
                */}
                <p className="mt-0.5 whitespace-pre-line text-xs leading-relaxed text-ink-muted">
                  {recipe.defaultLimitation}
                </p>
              </div>
            </div>
          )}

          {recipe.allowedExitCodes.length > 1 && (
            <p className="mt-1.5 flex items-center gap-1 text-xs text-ink-muted">
              <Check className="size-3 shrink-0 text-ink-faint" strokeWidth={2} />
              {t('tools.recipes.exitCodes.before')}{' '}
              <code className="font-mono">{recipe.allowedExitCodes.join(' / ')}</code>
              {t('tools.recipes.exitCodes.after')}
            </p>
          )}
        </div>
      )}
    </li>
  )
}
