import { describe, expect, it } from 'vitest'
import type { Finding } from '@/data/finding'
import {
  UNASSIGNED,
  emptyFilter,
  facetCounts,
  filterFindings,
  filterToExportQuery,
  isFilterActive,
  sortFindings,
} from './finding-query'

/*
  ══════════════════════════════════════════════════════════════
  漏洞的筛选、排序与分面计数
  ══════════════════════════════════════════════════════════════

  ## 为什么测这个

  这一层有两个**只在边界上才出错**的地方，而它们的症状都是
  "数字对不上，但没人能解释为什么"：

    1. **`facets` 只忽略"自己那一维"**。忽略多了（把所有维度都忽略）
       角标会指着一批被搜索排除的条目；忽略少了（完全不忽略）
       筛选器会把自己锁死 —— 用户筛了「高危」想改看「中危」，
       而中危角标是 0、按钮禁用，他切不过去。**这不是数字准不准，
       而是筛选器还能不能用。**

    2. **`filterToExportQuery` 表达不了"多选负责人"**。
       后端 `assigneeId` 与 `unassigned` 是两个参数，逗号分隔表达不了
       "张三的 **或** 没人管的"。所以选了多个时**不传这一维** ——
       而导出的范围就比用户看到的大。**绝不能静默丢弃**：
       用户会把那份表发出去。

  ## 与后端的分工

  这里的是**前端本地**筛选（列表已经拿在手上）。后端的 `/findings`
  有自己的筛选实现，两者必须同口径 —— 契约 §3.12 记了那条
  "列表长度 ≠ stats.total 在结构上不可能出现"的约束。
*/

/** 造一条漏洞。只填测试关心的字段。 */
function makeFinding(overrides: Partial<Finding> = {}): Finding {
  return {
    id: 'fnd_1',
    scanId: 'scan_a',
    title: '示例漏洞',
    severity: 'high',
    status: 'new',
    category: 'injection',
    target: 'api.example.com',
    location: '/login',
    verification: 'verified',
    discoveredAt: '2026-10-01T10:00:00.000Z',
    /*
      ⚠️ `detail` 必须给（我第一版没给，于是搜索那条用例抛
      `Cannot read properties of undefined (reading 'affected')`）。

      原因：`filterFindings` 的搜索会拼
      `[title, target, ...detail.affected]` —— 它假定 `detail` 存在。
      真实的 `Finding` 确实带它（`GET /findings` 返回摘要+detail），
      所以这是**测试数据不完整**，不是实现的问题。
    */
    detail: { affected: [] },
    ...overrides,
  } as Finding
}

// ══════════════════════════════════════════════════════════════
// 一、空筛选器与"是否激活"
// ══════════════════════════════════════════════════════════════

describe('emptyFilter / isFilterActive', () => {
  it('空筛选器不激活', () => {
    expect(isFilterActive(emptyFilter())).toBe(false)
  })

  it('任一维度有值就激活', () => {
    const filter = emptyFilter()
    filter.severities = new Set(['high'])

    expect(isFilterActive(filter)).toBe(true)
  })

  it('★ 只有空白字符的搜索词**不算**激活', () => {
    // 否则用户按一下空格，界面上就冒出"清除筛选"按钮
    const filter = emptyFilter()
    filter.query = '   '

    expect(isFilterActive(filter)).toBe(false)
  })

  it('每次调用返回**独立**的对象（不是共享引用）', () => {
    // 共享的话，一处修改会污染所有调用方的"空筛选器"
    const a = emptyFilter()
    a.severities.add('high')

    expect(emptyFilter().severities.size).toBe(0)
  })
})

// ══════════════════════════════════════════════════════════════
// 二、筛选
// ══════════════════════════════════════════════════════════════

describe('filterFindings', () => {
  const rows = [
    makeFinding({ id: 'f1', severity: 'critical', status: 'new', category: 'injection' }),
    makeFinding({ id: 'f2', severity: 'high', status: 'confirmed', category: 'xss' }),
    makeFinding({ id: 'f3', severity: 'low', status: 'fixed', category: 'xss' }),
  ]

  it('空筛选器返回全部', () => {
    expect(filterFindings(rows, emptyFilter())).toHaveLength(3)
  })

  it('同维度多选是"或"', () => {
    const filter = emptyFilter()
    filter.severities = new Set(['critical', 'low'])

    const got = filterFindings(rows, filter).map((f) => f.id)

    expect(got).toEqual(['f1', 'f3'])
  })

  it('★ 不同维度之间是"与"', () => {
    // 这是最容易搞反的一处：筛「高危 + 已确认」应当只有一条，
    // 而不是"高危的加上已确认的"
    const filter = emptyFilter()
    filter.severities = new Set(['high'])
    filter.statuses = new Set(['confirmed'])

    expect(filterFindings(rows, filter).map((f) => f.id)).toEqual(['f2'])
  })

  it('搜索词匹配标题（大小写不敏感）', () => {
    const filter = emptyFilter()
    filter.query = '示例'

    expect(filterFindings(rows, filter)).toHaveLength(3)

    filter.query = '不存在的词'
    expect(filterFindings(rows, filter)).toHaveLength(0)
  })
})

// ══════════════════════════════════════════════════════════════
// 三、★ 分面计数（角标）
// ══════════════════════════════════════════════════════════════

describe('facetCounts', () => {
  const rows = [
    makeFinding({ id: 'f1', severity: 'critical', status: 'new' }),
    makeFinding({ id: 'f2', severity: 'high', status: 'new' }),
    makeFinding({ id: 'f3', severity: 'high', status: 'confirmed' }),
    makeFinding({ id: 'f4', severity: 'low', status: 'confirmed' }),
  ]

  it('★ 严重度角标忽略严重度自己的条件', () => {
    /*
      这是 facets 存在的**全部理由**。

      场景：用户已经筛了「critical」（只有 1 条），现在想改看「high」。
      若 high 的角标也是"在当前筛选下"算的，那它是 **0** ——
      而一个 0 的角标通常会让按钮变成禁用，用户**切不过去**。

      所以算严重度角标时要把"严重度"这一维去掉：它回答的是
      "如果改选这一档，会有几条"。
    */
    const filter = emptyFilter()
    filter.severities = new Set(['critical'])

    const counts = facetCounts(rows, filter)

    // 去掉严重度条件后，各档的真实条数
    expect(counts.bySeverity.critical).toBe(1)
    expect(counts.bySeverity.high).toBe(2)
    expect(counts.bySeverity.low).toBe(1)
  })

  it('★ 状态角标仍然受严重度约束（只忽略"自己那一维"）', () => {
    /*
      上一条的反面：算状态角标时**不能**把严重度也忽略掉。

      忽略多了的后果是角标指着一批被筛选排除的条目 ——
      用户看到"已确认 2"，点进去只有 1 条。
    */
    const filter = emptyFilter()
    filter.severities = new Set(['high'])

    const counts = facetCounts(rows, filter)

    // high 的两条里，状态分布是 new:1 + confirmed:1
    expect(counts.byStatus.new).toBe(1)
    expect(counts.byStatus.confirmed).toBe(1)
    // critical / low 的条目被严重度条件排除了，不该出现在状态角标里
    expect(Object.values(counts.byStatus).reduce((a, b) => a + b, 0)).toBe(2)
  })

  it('★ 搜索词约束**所有**角标（不能连搜索一起忽略）', () => {
    /*
      ⚠️ 这一条是**突变验证补出来的**。

      我原本那组用例里，`byStatus` / `byCategory` 的断言恰好与
      "忽略所有维度"的实现在数值上一致 —— 于是把 `without(...)`
      改成 `emptyFilter()`（忽略一切）时，**没有任何测试变红**。

      要抓它，需要一个"搜索词确实排除了部分数据"的场景：
      搜索命中 2 条，而总数是 4 条。此时若角标忽略搜索，
      会算出 4 —— 那正是指着一批被搜索排除的条目。

      这是"测试数据恰好不触发缺陷"的典型：断言本身没错，
      但它没有把实现逼到会出错的那条路径上。
    */
    const rowsWithDistinctTitles = [
      makeFinding({ id: 's1', title: '登录处 SQL 注入', severity: 'high', status: 'new' }),
      makeFinding({ id: 's2', title: '登录处 XSS', severity: 'low', status: 'new' }),
      makeFinding({ id: 's3', title: '上传点任意文件读取', severity: 'high', status: 'confirmed' }),
      makeFinding({ id: 's4', title: '越权访问他人订单', severity: 'medium', status: 'confirmed' }),
    ]
    const filter = emptyFilter()
    filter.query = '登录'

    const counts = facetCounts(rowsWithDistinctTitles, filter)

    // 搜索命中 2 条（s1、s2）—— 所有角标都该基于这 2 条
    expect(counts.total).toBe(2)
    // 若角标忽略搜索，high 会是 2（s1+s3）；受搜索约束则只有 1（s1）
    expect(counts.bySeverity.high).toBe(1)
    expect(counts.bySeverity.low).toBe(1)
    // medium 的 s4 没被搜索命中，不该出现
    expect(counts.bySeverity.medium).toBeUndefined()
    // 状态角标同理：confirmed 的 s3 被搜索排除了
    expect(counts.byStatus.confirmed).toBeUndefined()
  })

  it('★ total 用**全部**条件（它就是"筛出来几条"）', () => {
    const filter = emptyFilter()
    filter.severities = new Set(['critical'])

    const counts = facetCounts(rows, filter)

    // 只有 1 条 critical —— total 必须是 1，而不是 4
    expect(counts.total).toBe(1)
  })

  it('★ total 与"逐条筛选的长度"永远相等', () => {
    /*
      契约 §3.12 记的那条等式：`列表长度 ≠ stats.total` 在结构上
      不可能出现。这里断言前端本地这一份也成立 ——
      两者走的是同一个 `filterFindings`，所以它必然相等。
    */
    const filter = emptyFilter()
    filter.severities = new Set(['high', 'low'])
    filter.statuses = new Set(['confirmed'])

    expect(facetCounts(rows, filter).total).toBe(filterFindings(rows, filter).length)
  })

  it('没有任何数据时不抛异常', () => {
    expect(() => facetCounts([], emptyFilter())).not.toThrow()
    expect(facetCounts([], emptyFilter()).total).toBe(0)
  })
})

// ══════════════════════════════════════════════════════════════
// 四、排序
// ══════════════════════════════════════════════════════════════

describe('sortFindings', () => {
  const rows = [
    makeFinding({ id: 'f-low', severity: 'low', discoveredAt: '2026-10-03T00:00:00.000Z' }),
    makeFinding({ id: 'f-crit', severity: 'critical', discoveredAt: '2026-10-01T00:00:00.000Z' }),
    makeFinding({ id: 'f-high', severity: 'high', discoveredAt: '2026-10-02T00:00:00.000Z' }),
  ]

  it('按严重度：严重在前', () => {
    const got = sortFindings(rows, 'severity', 'desc').map((f) => f.id)

    expect(got).toEqual(['f-crit', 'f-high', 'f-low'])
  })

  it('按时间：新的在前', () => {
    const got = sortFindings(rows, 'recent', 'desc').map((f) => f.id)

    expect(got).toEqual(['f-low', 'f-high', 'f-crit'])
  })

  it('★ 排序不修改原数组', () => {
    /*
      原地 `sort()` 会改掉调用方传进来的数组 —— 而那个数组往往
      直接来自 store 的快照。改它等于**在渲染之外修改 store 的数据**，
      症状是"列表顺序自己变了"或者"另一个页面看到的顺序不对"。
    */
    const original = rows.map((f) => f.id)
    sortFindings(rows, 'severity', 'desc')

    expect(rows.map((f) => f.id)).toEqual(original)
  })
})

// ══════════════════════════════════════════════════════════════
// 五、★ 导出映射（绝不静默丢弃）
// ══════════════════════════════════════════════════════════════

describe('filterToExportQuery', () => {
  it('单一维度正常映射', () => {
    const filter = emptyFilter()
    filter.severities = new Set(['high'])

    const { query } = filterToExportQuery(filter)

    expect(query.severity).toBe('high')
  })

  it('★ 多选负责人**不传**这一维，并如实报告', () => {
    /*
      后端 `assigneeId` 与 `unassigned` 是**两个参数**，逗号分隔
      表达不了"张三的 **或** 没人管的"。

      所以选了多个时不传 —— 但那会让导出的范围比用户看到的大，
      而**用户会把那份表发出去**。所以必须由
      `unexpressableDimensions` 告诉调用方，让它拦住并提示。

      ⚠️ 这个数组里装的是**中文标签**（`'负责人'`），不是字段名
      （`'assignees'`）—— 我第一版断言的是字段名，于是失败。
      而中文标签是**更好的**设计：它会被直接拼进给用户看的提示语
      （"以下维度未能表达：负责人"），用字段名反而要多一层映射。
    */
    const filter = emptyFilter()
    filter.assignees = new Set(['m1', 'm2'])

    const { query, unexpressableDimensions } = filterToExportQuery(filter)

    expect(query.assigneeId).toBeUndefined()
    expect(query.unassigned).toBeUndefined()
    expect(unexpressableDimensions).toContain('负责人')
  })

  it('★ 恰好选中一个负责人时正常传', () => {
    const filter = emptyFilter()
    filter.assignees = new Set(['m1'])

    const { query, unexpressableDimensions } = filterToExportQuery(filter)

    expect(query.assigneeId).toBe('m1')
    expect(unexpressableDimensions).toHaveLength(0)
  })

  it('★ 选中"未指派"映射成 unassigned 标志', () => {
    const filter = emptyFilter()
    filter.assignees = new Set([UNASSIGNED])

    const { query } = filterToExportQuery(filter)

    expect(query.unassigned).toBe(true)
    expect(query.assigneeId).toBeUndefined()
  })

  it('空筛选器产生空查询（导出全部）', () => {
    const { query, unexpressableDimensions } = filterToExportQuery(emptyFilter())

    expect(unexpressableDimensions).toHaveLength(0)
    // 除空的维度外不该有别的东西
    expect(Object.values(query).filter(Boolean)).toHaveLength(0)
  })
})
