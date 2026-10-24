import { beforeEach, describe, expect, it } from 'vitest'

import {
  filterSkillGroups,
  loadsOf,
  usageSummary,
  validateDraft,
  type SkillListSnapshot,
  type SkillSummary,
  type SkillUsageSnapshot,
} from './skills'

/*
  ══════════════════════════════════════════════════════════════
  skills.ts 的纯函数（v0.88）
  ══════════════════════════════════════════════════════════════

  ## 这个文件守什么

  技能页里最容易出错的地方不是布局，而是**说错话** —— 而且这里有两处
  特别容易说错的数：

    1. 「31 篇里有几篇被用过」
    2. 「最常用的是哪一篇」

  这两个数都来自**两个不同的数据源**（清单来自文件、次数来自事件表）。
  它们的口径稍不一致，界面上就会给出一句听着很确定、实际是错的结论。
  具体三个坑（下面都有对应用例）：

    · 统计表里留着**已删除技能**的名字 → 分子分母对不上
    · 0 次的技能**不在**统计响应里（后端只回被加载过的）→ 误判成"没有统计"
    · 截断时把"最近 2 万条里 0 次"说成"从来没被加载过" → 后果最严重

  ## 为什么 validateDraft 也要测

  它的规则必须与后端 `SkillCreateIn` 的 `pattern` **一致**。不一致的
  后果不是安全问题（真正的边界在文件层），而是**白填一遍表单** ——
  用户写完 200 行正文，点保存，才被告知名字里不能有大写。
  这份重复是刻意的（前端挡明显错误、后端挡真实边界），所以两边都要钉住。
*/

// ─────────────────────────────────────────────
// 造数据
// ─────────────────────────────────────────────

function skill(
  name: string,
  source: 'builtin' | 'custom' = 'builtin',
  category = 'analysis',
): SkillSummary {
  return { name, category, description: `${name} 的描述`, source }
}

/**
 * ★ 注意：分组里的每条 skill 的 `category` 必须与**分组名**一致 ——
 *   这是后端的契约（它先按分类分组再下发）。造数据时写错会让"搜分类"
 *   的用例测到一件假的事（曾经真的这么错过一次：工厂把 category 写死
 *   成 `analysis`，于是 `vulnerabilities` 组里的技能带着 `analysis`，
 *   而函数那时读的是 skill 自己的字段 → 用例红，看起来像函数有 bug）。
 */
function list(...groups: [string, SkillSummary[]][]): SkillListSnapshot {
  return {
    total: groups.reduce((sum, [, skills]) => sum + skills.length, 0),
    categories: groups.map(([category, skills]) => ({
      category,
      skills: skills.map((item) => ({ ...item, category })),
    })),
  }
}

function usage(...items: [string, number][]): SkillUsageSnapshot {
  return {
    sampledEvents: 100,
    since: null,
    items: items.map(([name, loads]) => ({
      name,
      loads,
      lastLoadedAt: loads > 0 ? '2026-10-24T10:00:00Z' : null,
    })),
  }
}

let EMPTY_LIST: SkillListSnapshot
let EMPTY_USAGE: SkillUsageSnapshot

beforeEach(() => {
  EMPTY_LIST = { total: 0, categories: [] }
  EMPTY_USAGE = { sampledEvents: 0, since: null, items: [] }
})

// ─────────────────────────────────────────────
// filterSkillGroups
// ─────────────────────────────────────────────

describe('filterSkillGroups', () => {
  const categories = list(
    ['analysis', [skill('web-pentest'), skill('code-review')]],
    ['vulnerabilities', [skill('sqli'), skill('xss')]],
  ).categories

  it('空查询原样返回（引用都不变）', () => {
    expect(filterSkillGroups(categories, '')).toBe(categories)
    expect(filterSkillGroups(categories, '   ')).toBe(categories)
  })

  it('匹配名字', () => {
    const result = filterSkillGroups(categories, 'sqli')
    expect(result).toHaveLength(1)
    expect(result[0].skills.map((item) => item.name)).toEqual(['sqli'])
  })

  it('匹配描述', () => {
    // 描述是 `${name} 的描述`，所以搜 "的描述" 会命中全部
    const result = filterSkillGroups(categories, '的描述')
    expect(result.flatMap((group) => group.skills)).toHaveLength(4)
  })

  it('匹配分类名', () => {
    const result = filterSkillGroups(categories, 'vulnerabilities')
    expect(result).toHaveLength(1)
    expect(result[0].category).toBe('vulnerabilities')
  })

  it('多个词是「都要命中」而不是「任意命中」', () => {
    // 两个词都只在 web-pentest 上成立
    expect(filterSkillGroups(categories, 'web analysis')).toHaveLength(1)
    // `web sqli` 没有任何一篇同时满足
    expect(filterSkillGroups(categories, 'web sqli')).toHaveLength(0)
  })

  it('★ 筛选后空分类要丢掉（否则界面空一片）', () => {
    const result = filterSkillGroups(categories, 'sqli')
    // 只剩 vulnerabilities 一个分类 —— analysis 被筛选筛空了，必须消失
    expect(result.map((group) => group.category)).toEqual(['vulnerabilities'])
  })

  it('大小写不敏感', () => {
    expect(filterSkillGroups(categories, 'SQLI')).toHaveLength(1)
  })

  it('★ 分类不一致时只认分组名（不让两个来源打架）', () => {
    // 手工造一份"坏数据"：分组叫 `vulnerabilities`，但里面的 skill
    // 自带 category 是 `analysis`。契约上不该出现，但一旦出现，
    // 搜索必须与**界面上显示的分类**（分组名）保持一致 ——
    // 否则用户搜到的和看到的不是同一件事。
    const inconsistent = [
      { category: 'vulnerabilities', skills: [skill('sqli', 'builtin', 'analysis')] },
    ]
    expect(filterSkillGroups(inconsistent, 'vulnerabilities')).toHaveLength(1)
    expect(filterSkillGroups(inconsistent, 'analysis')).toHaveLength(0)
  })
})

// ─────────────────────────────────────────────
// loadsOf
// ─────────────────────────────────────────────

describe('loadsOf', () => {
  it('有记录时返回那一条', () => {
    const result = loadsOf(usage(['sqli', 7]), 'sqli')
    expect(result?.loads).toBe(7)
  })

  it('★ 没有记录时返回 null（不是 0 次的记录）', () => {
    // ★ 这个区分很重要：后端**只回被加载过的**技能，所以"查不到"
    //   才是常态（0 次的那 25 篇都不在响应里）。页面必须用 `?? 0`
    //   兜底，而这里返回 null 让调用点无法忘记兜底。
    expect(loadsOf(usage(['sqli', 7]), 'xss')).toBeNull()
  })
})

// ─────────────────────────────────────────────
// usageSummary
// ─────────────────────────────────────────────

describe('usageSummary', () => {
  it('空清单 + 空统计 → 全零，不是 NaN', () => {
    expect(usageSummary(EMPTY_LIST, EMPTY_USAGE)).toEqual({
      usedCount: 0,
      totalLoads: 0,
      topName: null,
      topLoads: 0,
    })
  })

  it('正常情况：算出用过几篇、累计次数、最常用的一篇', () => {
    const l = list(['analysis', [skill('a'), skill('b'), skill('c')]])
    const u = usage(['a', 3], ['b', 9])
    expect(usageSummary(l, u)).toEqual({
      usedCount: 2,
      totalLoads: 12,
      topName: 'b',
      topLoads: 9,
    })
  })

  it('★ 统计表里留着已删除技能的名字 → 不计入分子', () => {
    // 事件是历史，删技能不会删事件。清单里只有 a，
    // 但统计里还有已删的 `gone`（100 次）。
    const l = list(['analysis', [skill('a')]])
    const u = usage(['gone', 100], ['a', 2])
    const result = usageSummary(l, u)
    // ★ 如果不按清单过滤：usedCount 会是 2（> 总篇数 1）、
    //   topName 会是 `gone` —— 界面上就会出现一篇不存在的"最常用技能"。
    expect(result.usedCount).toBe(1)
    expect(result.totalLoads).toBe(2)
    expect(result.topName).toBe('a')
  })

  it('★ 0 次的记录不计入 usedCount（后端不会回它，但要防）', () => {
    const l = list(['analysis', [skill('a'), skill('b')]])
    const u = usage(['a', 5], ['b', 0])
    expect(usageSummary(l, u).usedCount).toBe(1)
  })

  it('并列最多时取先出现的那一篇（稳定，不随对象顺序抖动）', () => {
    const l = list(['analysis', [skill('a'), skill('b')]])
    const u = usage(['a', 4], ['b', 4])
    expect(usageSummary(l, u).topName).toBe('a')
  })

  it('一篇都没用过 → topName 为 null（页面据此不显示「最常用」）', () => {
    const l = list(['analysis', [skill('a')]])
    expect(usageSummary(l, EMPTY_USAGE).topName).toBeNull()
  })
})

// ─────────────────────────────────────────────
// validateDraft
// ─────────────────────────────────────────────

describe('validateDraft', () => {
  const ok = {
    name: 'kerberos-preauth',
    category: 'vulnerabilities',
    description: '何时该看这一篇',
    body: '# 标题\n\n正文',
  }

  it('合法草稿返回 null', () => {
    expect(validateDraft(ok)).toBeNull()
  })

  it('名字不能有大写', () => {
    expect(validateDraft({ ...ok, name: 'Kerberos' })).toContain('小写')
  })

  it('名字不能以连字符或下划线开头', () => {
    // ★ 与后端 pattern `^[a-z0-9][a-z0-9_-]*$` 一致：首字符必须是字母数字。
    //   `-foo` 会被当成命令行参数，`_foo` 在一些工具链里是保留前缀。
    expect(validateDraft({ ...ok, name: '-foo' })).not.toBeNull()
    expect(validateDraft({ ...ok, name: '_foo' })).not.toBeNull()
  })

  it('名字不能有路径分隔符（目录穿越的第一道防线）', () => {
    expect(validateDraft({ ...ok, name: '../evil' })).not.toBeNull()
    expect(validateDraft({ ...ok, name: 'a/b' })).not.toBeNull()
  })

  it('名字长度上限 64', () => {
    expect(validateDraft({ ...ok, name: 'a'.repeat(64) })).toBeNull()
    expect(validateDraft({ ...ok, name: 'a'.repeat(65) })).not.toBeNull()
  })

  it('分类长度上限 32', () => {
    expect(validateDraft({ ...ok, category: 'a'.repeat(32) })).toBeNull()
    expect(validateDraft({ ...ok, category: 'a'.repeat(33) })).not.toBeNull()
  })

  it('★ 空正文要拦（空技能拉下来什么也没有，模型会以为查到了）', () => {
    expect(validateDraft({ ...ok, body: '' })).toContain('正文')
    expect(validateDraft({ ...ok, body: '   \n\t ' })).toContain('正文')
  })

  it('正文上限 200000 字符', () => {
    expect(validateDraft({ ...ok, body: 'x'.repeat(200_000) })).toBeNull()
    expect(validateDraft({ ...ok, body: 'x'.repeat(200_001) })).not.toBeNull()
  })

  it('描述上限 2000 字符（可以为空）', () => {
    expect(validateDraft({ ...ok, description: '' })).toBeNull()
    expect(validateDraft({ ...ok, description: 'x'.repeat(2000) })).toBeNull()
    expect(validateDraft({ ...ok, description: 'x'.repeat(2001) })).not.toBeNull()
  })

  it('★ 名字的合法形状与后端 SkillCreateIn 的 pattern 逐条对齐', () => {
    // 后端：`^[a-z0-9][a-z0-9_-]*$`，min_length=1，max_length=64
    const legal = ['a', 'a1', 'a-b', 'a_b', 'abc-def_9', '9start']
    for (const name of legal) {
      expect(validateDraft({ ...ok, name }), `应合法: ${name}`).toBeNull()
    }
    const illegal = ['', 'A', 'a b', 'a.b', 'a/b', 'a\\b', '-a', '_a', '中文']
    for (const name of illegal) {
      expect(validateDraft({ ...ok, name }), `应非法: ${name}`).not.toBeNull()
    }
  })
})
