import { describe, expect, it } from 'vitest'

import { MOBILE_MORE_ITEMS, MOBILE_TAB_ITEMS, NAV_ITEMS, findNavItem } from './navigation'

/*
  ══════════════════════════════════════════════════════════════
  导航信息架构的自检（v0.88，补于技能库页加入时）
  ══════════════════════════════════════════════════════════════

  ## 这个文件为什么必须存在

  `navigation.ts` 的注释里写着它自己是"单一事实来源"，而路由表是
  `NAV_ITEMS.map()` 生成的 —— 这件事有一个**极其隐蔽**的失效形状：

     侧栏点得动、URL 变了、页面全白，不报错也不警告。

  原因是 `App.tsx` 里：

      const Page = PAGE_COMPONENTS[item.id]
      if (!Page) return null        ← 静默返回空

  漏一个键就少一个页面，而 TypeScript 查不出来（`Record<string, ...>`
  允许缺键），ESLint 也查不出来，构建照常成功。

  ★ 这类"配了但静默失效"在本项目里已经发生过三次
    （`ir`、`tools`、以及资产/定时那一批），所以现在把它钉成测试：
    **每个 NAV_ITEMS 的 id 必须各自唯一、path 必须唯一、且必须在
    移动端两套列表里恰好出现一次。**

  ⚠️ 这个文件**不导入 App.tsx**：那会把整棵页面树拖进 jsdom
    （十几个页面 chunk + 一个 WebGL 背景），让一个纯数据自检
    变成全站冒烟。`PAGE_COMPONENTS` 的键由 `App.tsx` 自己守
    （见该文件里针对 `ir`/`tools`/`skills` 的注释），这里守的是
    "有没有两个条目撞了 id" —— 撞了的话 `PAGE_COMPONENTS` 里
    必然有一个键被覆盖，那才是真正会导致白屏的原因。
*/

describe('NAV_ITEMS 自检', () => {
  it('id 唯一（撞了会让路由表里有一个键被静默覆盖）', () => {
    const ids = NAV_ITEMS.map((item) => item.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('path 唯一且都以 / 开头', () => {
    const paths = NAV_ITEMS.map((item) => item.path)
    expect(new Set(paths).size).toBe(paths.length)
    for (const path of paths) {
      expect(path.startsWith('/'), `path 应以 / 开头: ${path}`).toBe(true)
    }
  })

  it('每个条目都有 label / blurb / icon', () => {
    for (const item of NAV_ITEMS) {
      expect(item.label, `${item.id} 缺 label`).toBeTruthy()
      expect(item.blurb, `${item.id} 缺 blurb`).toBeTruthy()
      expect(item.icon, `${item.id} 缺 icon`).toBeTruthy()
    }
  })

  it('★ 技能库是一级入口（它答的问题与工具页不同）', () => {
    const skills = findNavItem('/skills')
    expect(skills?.id).toBe('skills')
    expect(skills?.label).toBe('技能库')
    // 与工具管理必须是**两个**条目 —— 合成一个会让"拨开关"与
    // "写正文"两种操作挤在同一屏，而它们的风险面完全不同。
    const tools = findNavItem('/tools')
    expect(tools?.id).toBe('tools')
    expect(tools?.id).not.toBe(skills?.id)
  })

  it('移动端两套列表恰好覆盖全部条目，不重不漏', () => {
    const tabIds = MOBILE_TAB_ITEMS.map((item) => item.id)
    const moreIds = MOBILE_MORE_ITEMS.map((item) => item.id)
    const all = [...tabIds, ...moreIds]
    // 不重
    expect(new Set(all).size).toBe(all.length)
    // 不漏
    expect(new Set(all)).toEqual(new Set(NAV_ITEMS.map((item) => item.id)))
  })

  it('移动端底栏不超过 5 个（再多就挤成图标墙）', () => {
    expect(MOBILE_TAB_ITEMS.length).toBeLessThanOrEqual(5)
  })

  it('findNavItem 命中子路由（/skills/xxx 也要高亮父级）', () => {
    expect(findNavItem('/skills/abc')?.id).toBe('skills')
  })

  it('未知路径不命中任何条目', () => {
    expect(findNavItem('/nope')).toBeUndefined()
  })

  /*
    ★ v0.93：把"漏一个键 = 白屏"从注释变成断言。

    ## 为什么这条值得单独写

    上面第 30-35 行解释了为什么本文件**不 import App.tsx**（那会把整棵
    页面树拖进 jsdom，一个纯数据自检变成全站冒烟）。但那个决定留下一个
    真实的缺口：`PAGE_COMPONENTS` 的完整性**一直只靠注释提醒**，
    而"注释提醒"正是这个项目反复失效的那一类机制。

    实测代价巨大：漏键时 TypeScript 不报（`Record<string, ComponentType>`
    允许缺键）、ESLint 不报、构建成功、测试全绿 —— 症状只有
    「侧栏点得动、URL 变了、页面全白，控制台无任何输出」。

    ## 用读源码文本的办法补上，而不是 import

    这是哑办法，但**没有副作用**：
      · 不执行 App.tsx 的模块图（不拉十几个 page chunk、不拉 WebGL）；
      · 不做"解析 TypeScript"（没有编译器依赖）；
      · 只回答一个问题 —— 每个 nav id 在 `PAGE_COMPONENTS` 里有没有出现。

    ⚠️ 它断言的是**单向包含**（每个 nav id 都要有页面），不是相等：
      `PAGE_COMPONENTS` 里可以有多余的键（比如某个页面暂时从导航里摘下来
      但保留路由），那不是缺陷。
  */
  it('★ 每个导航项都在 App.tsx 的 PAGE_COMPONENTS 里有对应页面', async () => {
    const { readFileSync } = await import('node:fs')
    const { fileURLToPath } = await import('node:url')
    const { dirname, join } = await import('node:path')

    // 本测试文件与 App.tsx 同目录（src/）
    const here = dirname(fileURLToPath(import.meta.url))
    const source = readFileSync(join(here, 'App.tsx'), 'utf8')

    // 取 `const PAGE_COMPONENTS: Record<string, ComponentType> = { ... }` 那一整块
    const start = source.indexOf('const PAGE_COMPONENTS')
    expect(start, 'App.tsx 里找不到 PAGE_COMPONENTS —— 它可能被改名了').toBeGreaterThanOrEqual(0)
    const braceOpen = source.indexOf('{', start)
    const braceClose = source.indexOf('\n}', braceOpen)
    expect(braceClose, 'PAGE_COMPONENTS 的对象字面量没闭合？').toBeGreaterThan(braceOpen)
    const block = source.slice(braceOpen, braceClose)

    const missing = NAV_ITEMS.filter((item) => {
      // 匹配 `  chat: ChatPage,` 这种行（允许前导空白）
      const re = new RegExp(`^\\s*${item.id}\\s*:`, 'm')
      return !re.test(block)
    }).map((item) => item.id)

    // 失败信息要直接告诉人怎么修，而不是只说"缺了"
    expect(
      missing,
      `这些导航项在 App.tsx 的 PAGE_COMPONENTS 里没有页面，点进去会是白屏：${missing.join(', ')}`,
    ).toEqual([])
  })
})
