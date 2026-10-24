import { describe, expect, it } from 'vitest'

import { NAV_ITEMS } from '@/navigation'
import {
  COMMAND_KIND_META,
  actionCommands,
  assetCommands,
  fallbackCommand,
  findingCommands,
  pageCommands,
  scanCommands,
  scheduleCommands,
  searchCommands,
  type CommandItem,
} from '@/data/command'

/*
  命令面板的检索逻辑测试 —— 纯函数，不挂组件。

  ★ 这里的用例基本都按「改坏它，这条测试会不会红」来设计：
    只断言"有结果"的测试对排序逻辑是零约束（详见下面每条注释）。
*/

const item = (over: Partial<CommandItem> & Pick<CommandItem, 'id' | 'title'>): CommandItem => ({
  kind: 'page',
  subtitle: '',
  to: '/x',
  keywords: '',
  ...over,
})

describe('searchCommands —— 匹配与排序', () => {
  it('标题前缀命中排在标题包含之前', () => {
    /*
      这条要防的退化：把排序里的 strength 整个去掉（只按类型+长度），
      下面两个用例标题长度故意写成「短的恰好是"包含"那条」，
      于是「长度优先」的退化写法会把顺序颠倒 —— 这条就红。
    */
    const contains = item({ id: 'page:a', title: '查看扫描' })
    const prefix = item({ id: 'page:b', title: '扫描记录' })
    const result = searchCommands([contains, prefix], '扫描')
    expect(result.map((r) => r.id)).toEqual(['page:b', 'page:a'])
  })

  it('标题命中排在仅关键词命中之前', () => {
    /*
      这条防的退化：把 MATCH_KEYWORD 与 MATCH_TITLE 的档位写反。
      光看「都返回了 2 条」是查不出来的 —— 必须断言顺序。
    */
    const keywordOnly = item({ id: 'page:a', title: '设置', keywords: '扫描' })
    const titleHit = item({ id: 'page:b', title: '扫描记录' })
    const result = searchCommands([keywordOnly, titleHit], '扫描')
    expect(result.map((r) => r.id)).toEqual(['page:b', 'page:a'])
  })

  it('动作排在任何数据条目之前 —— 即使数据条目的标题前缀命中更短', () => {
    /*
      ★ 这条是面板存在意义的守卫。

        若排序把 kind 优先级去掉（比如按"命中强度 → 长度"排），
        打开面板会因为库里正好有一条叫「扫描」的记录而把「发起新扫描」
        挤到下面 —— 而按 Cmd+K 的人十次有八次是想**做点什么**。
    */
    const action = item({ id: 'action:new-scan', kind: 'action', title: '发起新扫描' })
    const page = item({ id: 'page:scan', kind: 'page', title: '扫描' })
    const result = searchCommands([page, action], '扫')
    expect(result.map((r) => r.id)).toEqual(['action:new-scan', 'page:scan'])
  })

  it('多个词是 AND 不是 OR', () => {
    /*
      ★ 防的退化：把 `hitAll = false` 改成"任一命中即可"。
        改成 OR 之后下面 `shouldNotMatch` 会被搜出来 —— 那是噪音。
        用 AND 的理由见 command.ts：打得越多应该越少，而不是越多。
    */
    const both = item({ id: 'scan:1', kind: 'scan', title: 'acme 扫描' })
    const onlyOne = item({ id: 'scan:2', kind: 'scan', title: 'acme 复测' })
    const result = searchCommands([both, onlyOne], 'acme 扫描')
    expect(result.map((r) => r.id)).toEqual(['scan:1'])
  })

  it('多词时整体强度取最弱的那一次命中', () => {
    /*
      `acme` 命中标题、`漏洞` 只命中关键词 → 整体算关键词档。

      不放宽断言的话（例如只断言"返回了"），把 worst 改成取最大值
      也照样通过 —— 而取最大值会让"沾了一个强词"的结果整体被抬高，
      排序变成由最幸运的那个词决定。
    */
    const weak = item({ id: 'finding:1', kind: 'finding', title: 'acme 越权', keywords: '漏洞' })
    const strong = item({ id: 'finding:2', kind: 'finding', title: 'acme 漏洞' })
    const result = searchCommands([weak, strong], 'acme 漏洞')
    // strong 两词都在标题里（前缀档），weak 只有一个是 → strong 在前
    expect(result.map((r) => r.id)).toEqual(['finding:2', 'finding:1'])
  })

  it('同样输入两次，顺序完全一致', () => {
    /*
      ★ 防的不只是随机性：加一条 `sort` 里的不稳定 tie-break 就会出现
        "列表每次刷新换位置"，用户不敢点。这里要求两次结果 id 序列相等。

        构造：三条同类型、同标题长度、同命中档 —— 只有 id 不同。
    */
    const items = [
      item({ id: 'page:zzz', title: '扫 a' }),
      item({ id: 'page:aaa', title: '扫 b' }),
      item({ id: 'page:mmm', title: '扫 c' }),
    ]
    const first = searchCommands(items, '扫').map((r) => r.id)
    const second = searchCommands([...items].reverse(), '扫').map((r) => r.id)
    expect(first).toEqual(second)
    // 且确实是按 id 升序兜底（不是恰好两次都空）
    expect(first).toEqual(['page:aaa', 'page:mmm', 'page:zzz'])
  })

  it('标题短的排在前面 —— 同一个命中档内', () => {
    const long = item({ id: 'page:a', title: '扫描记录与对比视图' })
    const short = item({ id: 'page:b', title: '扫描记录' })
    const result = searchCommands([long, short], '扫描')
    expect(result.map((r) => r.id)).toEqual(['page:b', 'page:a'])
  })

  it('空查询返回空数组（不是"全部"）', () => {
    /*
      ★ 空查询返回全部是个诱人但错误的做法：面板一打开就列出 200 条，
        用户要往下翻。这里明确返回空，由组件显示"输入关键字开始搜索"。
    */
    expect(searchCommands([item({ id: 'page:a', title: '扫描' })], '')).toEqual([])
    expect(searchCommands([item({ id: 'page:a', title: '扫描' })], '   ')).toEqual([])
  })

  it('limit 生效', () => {
    const items = Array.from({ length: 30 }, (_, index) =>
      item({ id: `page:${String(index).padStart(2, '0')}`, title: `扫描${index}` }),
    )
    expect(searchCommands(items, '扫描').length).toBe(24)
    expect(searchCommands(items, '扫描', 5).length).toBe(5)
  })

  it('大小写不敏感', () => {
    const aws = item({ id: 'page:a', title: 'AWS 资产' })
    expect(searchCommands([aws], 'aws').length).toBe(1)
    expect(searchCommands([aws], 'AWS').length).toBe(1)
  })
})

describe('pageCommands —— 由导航表派生', () => {
  it('页面项数量与导航表一致，且路径逐条相同', () => {
    /*
      ★ 这是"永远不和侧栏漂移"的守卫。

        如果有人手写一份页面列表（而不是 map NAV_ITEMS），
        新增页面时它会静默少一条 —— 面板搜不到那个页面，
        而侧栏有。这条会当场红。
    */
    const pages = pageCommands()
    expect(pages.length).toBe(NAV_ITEMS.length)
    expect(pages.map((p) => p.to)).toEqual(NAV_ITEMS.map((n) => n.path))
  })

  it('每个页面项的 kind 都是 page 且 id 带 page: 前缀', () => {
    for (const page of pageCommands()) {
      expect(page.kind).toBe('page')
      expect(page.id.startsWith('page:')).toBe(true)
    }
  })

  it('搜索页面的中文名能搜到它（不只是靠 id）', () => {
    const pages = pageCommands()
    const assets = pages.find((p) => p.to === '/assets')
    expect(assets).toBeDefined()
    const result = searchCommands(pages, '资产')
    expect(result.some((r) => r.to === '/assets')).toBe(true)
  })
})

describe('actionCommands', () => {
  it('四条动作全部带 kind=action 且 id 唯一', () => {
    const actions = actionCommands()
    expect(actions.length).toBe(4)
    expect(new Set(actions.map((a) => a.id)).size).toBe(4)
    for (const action of actions) expect(action.kind).toBe('action')
  })

  it('新扫描与新定时都带 ?new=1，否则跳过去什么都不发生', () => {
    /*
      ★ 这条守的是跨文件契约：ScansPage / SchedulesPage 读 `new=1` 才开向导。
        有人改了其中一边（比如把 `new` 改成 `wizard`），另一边静默失效 ——
        点了"发起新扫描"，跳到扫描页，没弹窗，用户以为坏了。
    */
    const byId = Object.fromEntries(actionCommands().map((a) => [a.id, a]))
    expect(byId['action:new-scan'].to).toBe('/scans?new=1')
    expect(byId['action:new-schedule'].to).toBe('/schedules?new=1')
  })

  it('处理未关闭漏洞跳的是 ?status=open', () => {
    const byId = Object.fromEntries(actionCommands().map((a) => [a.id, a]))
    expect(byId['action:review-findings'].to).toBe('/findings?status=open')
  })
})

describe('实体转换', () => {
  it('扫描项的 to 是详情路径，副标题带状态中文', () => {
    const [scan] = scanCommands(
      [{ id: 'scan_1', name: 'acme 首页', target: 'https://acme.test', status: 'running', startedAt: '2026-01-01' }],
      (s) => (s === 'running' ? '运行中' : s),
    )
    expect(scan.to).toBe('/scans/scan_1')
    expect(scan.subtitle).toContain('运行中')
    expect(scan.id).toBe('scan:scan_1')
  })

  it('扫描没有名字时用目标代替，不留空标题', () => {
    /*
      空标题的条目在列表里是一行空白 —— 用户看得见它、点得到它，
      但不知道那是什么。
    */
    const [scan] = scanCommands(
      [{ id: 'scan_1', name: '', target: 'https://acme.test', status: 'failed', startedAt: '2026-01-01' }],
      (s) => s,
    )
    expect(scan.title).toBe('https://acme.test')
  })

  it('漏洞项的 to 带 URL 编码的 finding 参数', () => {
    const [finding] = findingCommands(
      [{ id: 'f_1/2', title: 'CORS 预检', target: 'https://a.test', severity: 'high' }],
      () => '高危',
    )
    expect(finding.to).toBe(`/findings?finding=${encodeURIComponent('f_1/2')}`)
    expect(finding.subtitle).toContain('高危')
  })

  it('资产项跳到扫描页并按目标筛选，目标做了编码', () => {
    const [asset] = assetCommands([{ target: 'https://a.test/x y', openFindingCount: 2, scanCount: 3 }])
    expect(asset.to).toBe(`/scans?target=${encodeURIComponent('https://a.test/x y')}`)
    expect(asset.subtitle).toContain('2 条未处置')
  })

  it('没有未处置漏洞的资产，副标题说清"无"而不是省略', () => {
    /*
      ★ 省略这一句会让「0 条未处置」和「这个字段没返回」看起来一样。
        对于一张安全台账，"看起来都干净"和"不知道"必须是两句话。
    */
    const [asset] = assetCommands([{ target: 'a.test', openFindingCount: 0, scanCount: 1 }])
    expect(asset.subtitle).toContain('无未处置漏洞')
  })

  it('定时计划项区分启用与停用', () => {
    const [on] = scheduleCommands([{ id: 's1', name: '每日', target: 'a.test', enabled: true }])
    const [off] = scheduleCommands([{ id: 's2', name: '每周', target: 'a.test', enabled: false }])
    expect(on.subtitle).toContain('启用中')
    expect(off.subtitle).toContain('已停用')
  })

  it('所有转换出来的 id 都带各自 kind 前缀，不会互相撞', () => {
    const ids = [
      ...scanCommands([{ id: 'x', name: 'n', target: 't', status: 'running', startedAt: '' }], (s) => s),
      ...findingCommands([{ id: 'x', title: 'n', target: 't', severity: 'high' }], (s) => s),
      ...assetCommands([{ target: 'x', openFindingCount: 0, scanCount: 0 }]),
      ...scheduleCommands([{ id: 'x', name: 'n', target: 't', enabled: true }]),
    ].map((i) => i.id)
    expect(new Set(ids).size).toBe(ids.length)
  })
})

describe('fallbackCommand', () => {
  it('空查询给 null —— 不要在没打字时就喊"去别处搜"', () => {
    expect(fallbackCommand('')).toBeNull()
    expect(fallbackCommand('   ')).toBeNull()
  })

  it('非空查询给一条去漏洞页全文搜的出口，且做了编码', () => {
    const fallback = fallbackCommand('cors 预检')
    expect(fallback).not.toBeNull()
    expect(fallback?.to).toBe(`/findings?q=${encodeURIComponent('cors 预检')}`)
    // 副标题必须说清「这里只搜已加载的」，否则用户会以为系统里真没有
    expect(fallback?.subtitle).toContain('只搜已加载')
  })
})

describe('COMMAND_KIND_META', () => {
  it('动作的 order 是最小的 —— 排序第一关键字', () => {
    /*
      ★ 这条替代了"动作排最前"的行为测试的直接对应：
        把 action 的 order 改成 9，这条立刻红，不用去猜排序实现。
    */
    const orders = Object.values(COMMAND_KIND_META).map((m) => m.order)
    expect(COMMAND_KIND_META.action.order).toBe(Math.min(...orders))
    expect(new Set(orders).size).toBe(orders.length)
  })
})
