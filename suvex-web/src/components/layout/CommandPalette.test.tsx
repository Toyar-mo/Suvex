import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { useEffect, useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { CommandPalette } from '@/components/layout/CommandPalette'

/*
  命令面板的交互测试。

  ★ `vitest.config.ts` 是 `globals: false`，RTL 的自动 cleanup 只在
    globals 模式下注册 —— 于是必须手动 `cleanup()`，否则上一个用例的
    面板会留在 DOM 里，`getByRole('option')` 会因为"找到多个"而炸。
    （这个坑在 AssetsPage.test.tsx 里已经踩过一次。）
*/

// ── store 桩 ──
//
// 面板从四个 store 读快照。这里全桩掉：真实 store 会发 fetch，
// 而本文件要验的是"拿到数据之后面板怎么动"，不是数据怎么来。

const emptySnapshot = {
  items: null,
  total: 0,
  status: 'idle' as const,
  error: null,
}

/*
  ★ 扫描列表做成**可变**的，为的是「输入没变、结果集却变小」这个场景
    （见后面「高亮下标按当前结果数裁切」那条用例）。

    真实世界里它随时会变：面板打开时四个 store 正在拉数据，数据到了
    结果就多；某个扫描被别的页面归档了，结果就少。而**输入框里的字没变**，
    所以 `onChange` 里那次 `setRawActive(0)` 不会发生 —— 高亮会停在一个
    已经不存在的下标上。这份可变引用就是用来复现那一刻的。
*/
const scansStub = vi.hoisted(() => ({
  rows: [
    {
      id: 'scan_1',
      name: 'acme 首页扫描',
      target: 'https://acme.test',
      status: 'completed',
      startedAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'scan_2',
      name: '扫描记录甲',
      target: 'https://a.test',
      status: 'completed',
      startedAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'scan_3',
      name: '扫描记录乙',
      target: 'https://b.test',
      status: 'completed',
      startedAt: '2026-01-01T00:00:00Z',
    },
  ] as { id: string; name: string; target: string; status: string; startedAt: string }[],
  /**
   * ★ 必须让 `useScans()` 每次返回**新数组引用**，不能只改 `rows` 的内容。
   *
   *   面板里 `allItems` 是 `useMemo(..., [scans, ...])` —— 原地 `splice`
   *   不改变引用，memo 不会重算，结果集看起来"没变"。
   *   真实 store 每次快照变化都给新引用（`useSyncExternalStore` 的约定），
   *   这里用 `slice()` 复刻那个语义。
   */
  snapshot(): { id: string; name: string; target: string; status: string; startedAt: string }[] {
    return this.rows.slice()
  },
  /** 订阅者（组件）——`notify()` 让它们重渲染，模拟 store 快照变化 */
  listeners: new Set<() => void>(),
  subscribe(fn: () => void): () => void {
    this.listeners.add(fn)
    return () => {
      this.listeners.delete(fn)
    }
  },
  notify(): void {
    for (const fn of this.listeners) fn()
  },
}))

vi.mock('@/data/scans-store', () => ({
  useScans: () => {
    /*
      ★ 这里必须**真的订阅**，否则"数据变了"不会引起重渲染。

        真实 store 走 `useSyncExternalStore`：快照一变，subscribe 回调
        把所有订阅组件标记为需要重渲染。桩如果只写 `() => rows`，
        改 `scansStub.rows` 之后没有任何东西通知 React —— 列表看起来
        "没变"，测试就抓不到裁切逻辑。

        所以用一个本地 `useState` 计数器：每次 `notify()` 加一，
        重渲染时重新读 `snapshot()`（返回新数组引用）。
    */
    const [, force] = useState(0)
    useEffect(() => scansStub.subscribe(() => force((n) => n + 1)), [])
    return scansStub.snapshot()
  },
}))

vi.mock('@/data/findings-store', () => ({
  useFindings: () => [
    { id: 'f_1', title: 'CORS 预检配置错误', target: 'https://acme.test', severity: 'high' },
  ],
  loadFindings: () => Promise.resolve(),
}))

vi.mock('@/data/assets-store', () => ({
  useAssetsState: () => emptySnapshot,
  loadAssets: () => Promise.resolve(),
}))

vi.mock('@/data/schedules-store', () => ({
  useSchedulesState: () => emptySnapshot,
  loadSchedules: () => Promise.resolve(),
}))

/** 把当前路径记下来，供"点了之后跳到哪"的断言 */
let visited: string[] = []

function LocationSpy() {
  const location = useLocation()
  visited.push(location.pathname + location.search)
  return null
}

function renderPalette(onClose: () => void) {
  return render(
    <MemoryRouter initialEntries={['/overview']}>
      <Routes>
        <Route
          path="*"
          element={
            <>
              <CommandPalette open onClose={onClose} />
              <LocationSpy />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  )
}

const SCANS_DEFAULT = scansStub.rows.slice()

beforeEach(() => {
  visited = []
  // 每个用例从同一份数据开始：上一个用例删掉的行不能漏到这里
  scansStub.rows.length = 0
  scansStub.rows.push(...SCANS_DEFAULT)
})

afterEach(() => {
  cleanup()
})

describe('CommandPalette —— 打开时的基本形态', () => {
  it('渲染出输入框、对话框与操作提示', () => {
    renderPalette(() => {})

    expect(screen.getByRole('dialog', { name: '命令面板' })).toBeTruthy()
    expect(screen.getByRole('combobox')).toBeTruthy()
    // 三条键盘提示都要在 —— 用户第一次打开全靠它知道怎么用
    expect(screen.getByText('↑↓ 选择')).toBeTruthy()
    expect(screen.getByText('↵ 执行')).toBeTruthy()
    expect(screen.getByText('Esc 关闭')).toBeTruthy()
  })

  it('空输入时不列任何条目，只提示开始搜索', () => {
    /*
      ★ 这条守的是 `searchCommands` 的空查询返回 []。
        若它退化成"返回全部"，`listbox` 会出现、这里立刻红 ——
        而且用户一打开面板就要在 200 条里翻。
    */
    renderPalette(() => {})
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(screen.getByText('输入关键字开始搜索')).toBeTruthy()
  })

  it('输入后列出结果，且每条都带类型标签', () => {
    renderPalette(() => {})
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '扫描' } })

    const listbox = screen.getByRole('listbox')
    const options = within(listbox).getAllByRole('option')
    expect(options.length).toBeGreaterThan(0)
    // 第一条应该是动作档（发起新扫描）
    expect(options[0].textContent).toContain('操作')
    expect(options[0].textContent).toContain('发起新扫描')
  })
})

describe('CommandPalette —— 键盘操作', () => {
  it('↓ 移动高亮，aria-activedescendant 跟着变', () => {
    /*
      ★ 断言 aria-activedescendant 而不是样式类名：
        类名是实现的偶然，而 aria-activedescendant 是**读屏器读到的就是它** ——
        它错了，用读屏器的用户就不知道回车会执行哪一条。
    */
    renderPalette(() => {})
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: '扫描' } })

    const first = input.getAttribute('aria-activedescendant')
    expect(first).not.toBeNull()

    fireEvent.keyDown(input, { key: 'ArrowDown' })
    const second = input.getAttribute('aria-activedescendant')
    expect(second).not.toBeNull()
    expect(second).not.toBe(first)

    const active = document.querySelector('[data-active="true"]')
    expect(active?.id).toBe(second)
  })

  it('↑ 从第一条绕回最后一条', () => {
    renderPalette(() => {})
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: '扫描' } })
    const count = screen.getAllByRole('option').length

    fireEvent.keyDown(input, { key: 'ArrowUp' })
    const active = document.querySelector('[data-active="true"]')
    // 绕回最后一条：它的下标是 count-1
    expect(active?.id.endsWith(`-${count - 1}`)).toBe(true)
  })

  it('↓ 按到最底一条再按，绕回第一条（不能越界）', () => {
    /*
      ★ 这条补的是「一直往下按」的边界。

        没有取模的话，下标会一路加到底再越界 —— 那一刻
        `results[activeIndex]` 是 `undefined`，`aria-activedescendant` 变成
        `undefined`，读屏器什么都不念；回车也执行不了任何东西。
        面板看起来完全正常（还有条目在列），只是键盘失灵了。

      ★ 之前只有"↓ 按一次"那条，它永远碰不到边界。
    */
    renderPalette(() => {})
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: '扫描' } })
    const count = screen.getAllByRole('option').length
    expect(count).toBeGreaterThan(1)

    // 从第一条开始，按 count 次 ↓ 应该正好绕回第一条
    for (let i = 0; i < count; i += 1) fireEvent.keyDown(input, { key: 'ArrowDown' })

    const active = document.querySelector('[data-active="true"]')
    expect(active).not.toBeNull()
    expect(active?.id.endsWith('-0')).toBe(true)
    // 而且 aria-activedescendant 必须跟着是第一条，不能是 undefined
    expect(input.getAttribute('aria-activedescendant')).toBe(active?.id)
  })

  it('高亮项变化时把它滚进视口', () => {
    /*
      ★ 为什么这条要断言 `scrollIntoView` 被调用过：
        jsdom 不实现布局，"看不见"这件事在这里无法观测 —— 而它的真实后果
        是"按 ↓ 十几次之后，高亮在第 12 条，用户眼前还停在第 1 条"。
        能观测的最小信号就是"我们确实要求浏览器把它滚进来"。

      ★ 断言的是**对当前高亮项**调用，不是"随便调了"：
        拿 `[data-active="true"]` 元素对比，而不是看调用次数 ——
        次数对但滚错元素，症状一模一样（滚了个不相干的）。
    */
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
    try {
      renderPalette(() => {})
      const input = screen.getByRole('combobox')
      fireEvent.change(input, { target: { value: '扫描' } })
      fireEvent.keyDown(input, { key: 'ArrowDown' })
      fireEvent.keyDown(input, { key: 'ArrowDown' })

      const active = document.querySelector('[data-active="true"]')
      expect(active).not.toBeNull()
      expect(scrollSpy).toHaveBeenCalled()
      expect(scrollSpy.mock.instances).toContain(active)
    } finally {
      scrollSpy.mockRestore()
    }
  })

  it('Enter 执行高亮项：关闭面板并跳转', () => {
    const onClose = vi.fn()
    renderPalette(onClose)
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: '发起新扫描' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    expect(onClose).toHaveBeenCalledTimes(1)
    expect(visited).toContain('/scans?new=1')
  })

  it('Esc 关闭面板', () => {
    const onClose = vi.fn()
    renderPalette(onClose)
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('高亮下标按当前结果数裁切 —— 数据变少但输入没变时也不越界', () => {
    /*
      ★ 这条是 `activeIndex = Math.min(rawActive, results.length - 1)` 的
        **唯一真能守住它的用例**。

        为什么换个走法：原来那条「先输宽词 ↓ 到底、再输窄词」其实测不到裁切 ——
        `onChange` 里已经 `setRawActive(0)`，所以只要动过输入框，下标就归零了，
        裁切那行删掉测试照样绿（这是反向验证抓出来的假绿）。

        真实会踩到的场景是：**输入框里的字没变，结果集自己变小了** ——
        面板打开时四个 store 正在拉数据、或者某个扫描被别的页面归档了。
        那一刻 `setRawActive(0)` 不会发生，高亮停在一个已经不存在的下标上：
        `aria-activedescendant` 是 `undefined`（读屏器什么都不念），
        回车执行 `results[rawActive]` 得到 `undefined`（按了没反应）。
        而面板看起来完全正常 —— 还有条目在列，只是键盘失灵了。

        复现方式：把扫描列表桩里的行**在面板打开期间**减少。
    */
    const onClose = vi.fn()
    renderPalette(onClose)
    const input = screen.getByRole('combobox')

    // 两条「扫描记录」（第三条是 `acme 首页扫描`，不含"扫描记录"四个字，不会命中）
    fireEvent.change(input, { target: { value: '扫描记录' } })
    expect(screen.getAllByRole('option')).toHaveLength(2)
    // 从第 1 条按到第 2 条（下标 1）
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(input.getAttribute('aria-activedescendant')).toMatch(/-1$/)

    // 数据自己少了：`扫描记录甲` 被别处归档/删除，只剩乙。输入框一个字没动。
    // （`splice(1, 1)` 只删 scan_2 —— scan_1 是 acme，本来就不命中这条查询）
    scansStub.rows.splice(1, 1)
    // ★ 必须 notify()：真实 store 是快照变化 → 订阅回调 → 重渲染。
    //   光改数组不通知，React 什么都不会做（这也正是真实世界里
    //   "数据到了但 UI 没更新"这类 bug 的成因）。
    act(() => scansStub.notify())

    const options = screen.getAllByRole('option')
    expect(options).toHaveLength(1)
    const active = document.querySelector('[data-active="true"]')
    expect(active).not.toBeNull()
    // 关键：高亮必须还在**存在的那一条**上，而不是悬空的 undefined
    expect(input.getAttribute('aria-activedescendant')).toBe(active?.id)
    expect(active?.id).toBe(options[0].id)

    // 而且此刻回车要真的能执行它
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('结果被筛到只剩一条时，高亮不会越界', () => {
    /*
      ★ 这条对应 `activeIndex` 的派生裁切。

        走法：先输一个宽词拿到多条、↓ 到底，再输入一个只命中一条的词。
        如果高亮下标没有裁切，它会停在一个不存在的下标上，
        那时回车执行的是 `undefined` —— 用户按了回车，什么都没发生，
        而面板看起来还在正常工作。
    */
    const onClose = vi.fn()
    renderPalette(onClose)
    const input = screen.getByRole('combobox')

    fireEvent.change(input, { target: { value: '扫描' } })
    for (let i = 0; i < 5; i += 1) fireEvent.keyDown(input, { key: 'ArrowDown' })

    fireEvent.change(input, { target: { value: 'CORS' } })
    const options = screen.getAllByRole('option')
    expect(options.length).toBe(1)

    const active = document.querySelector('[data-active="true"]')
    expect(active).not.toBeNull()
    expect(active?.textContent).toContain('CORS')

    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('输入变化时高亮回到第一条', () => {
    renderPalette(() => {})
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: '扫描' } })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'ArrowDown' })

    fireEvent.change(input, { target: { value: '扫描记录' } })
    const active = document.querySelector('[data-active="true"]')
    expect(active?.id.endsWith('-0')).toBe(true)
  })
})

describe('CommandPalette —— 鼠标操作', () => {
  it('点击条目跳到对应路径并关闭', () => {
    const onClose = vi.fn()
    renderPalette(onClose)
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'acme' } })

    const option = screen.getAllByRole('option')[0]
    fireEvent.click(option)

    expect(onClose).toHaveBeenCalledTimes(1)
    expect(visited.length).toBeGreaterThan(1)
  })

  it('鼠标移动到某条上会同步高亮 —— 回车执行的是鼠标指着的那条', () => {
    /*
      ★ 这条守的是一个**无声的错误执行**：

        鼠标停在第 3 条、高亮还在第 1 条时按回车，用户预期执行第 3 条，
        实际执行第 1 条。两个动作都"成功了"，界面上看不出错。
        修法就是 onMouseMove 同步下标，这条用例盯着它。
    */
    renderPalette(() => {})
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: '扫描' } })

    const options = screen.getAllByRole('option')
    expect(options.length).toBeGreaterThan(1)
    fireEvent.mouseMove(options[1])

    const active = document.querySelector('[data-active="true"]')
    expect(active?.id).toBe(options[1].id)
  })

  it('点击遮罩关闭面板', () => {
    const onClose = vi.fn()
    renderPalette(onClose)
    fireEvent.click(screen.getByRole('button', { name: '关闭命令面板' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe('CommandPalette —— 搜索的边界', () => {
  it('一条都没命中时给出"去漏洞页全文搜"的兜底项', () => {
    /*
      ★ 没有兜底项时，用户打出精准词得到空面板，结论是「系统里没有这个」。
        真相往往只是「不在已加载的那几条里」。这条守的是那句话必须存在。
    */
    renderPalette(() => {})
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '完全不存在的东西xyz' } })

    const options = screen.getAllByRole('option')
    expect(options.length).toBe(1)
    expect(options[0].textContent).toContain('在漏洞页搜索')
  })

  it('有命中时不出现兜底项 —— 别在用户已经找到时再添噪音', () => {
    renderPalette(() => {})
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'CORS' } })

    const options = screen.getAllByRole('option')
    expect(options.length).toBe(1)
    expect(options[0].textContent).toContain('CORS')
    expect(options[0].textContent).not.toContain('在漏洞页搜索')
  })

  it('关闭时不渲染任何东西', () => {
    render(
      <MemoryRouter>
        <CommandPalette open={false} onClose={() => {}} />
      </MemoryRouter>,
    )
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByRole('combobox')).toBeNull()
  })
})
