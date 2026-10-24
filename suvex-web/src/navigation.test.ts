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
})
