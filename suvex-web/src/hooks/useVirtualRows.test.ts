import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { useVirtualRows } from './useVirtualRows'

/*
  ══════════════════════════════════════════════════════════════
  useVirtualRows 的 DOM 侧行为测试
  ══════════════════════════════════════════════════════════════

  纯数学在 `data/virtual.test.ts`（26 条）。这里只测三件 DOM 侧的事，
  它们都是"类型检查与 lint 都过得去、但运行时是错的"：

  1. **`measureRef` 的身份必须恒定** —— 否则 `EventRow` 的 `memo` 静默失效。
  2. **observer 不能被高度更新重建** —— 重建后一个节点都没 observe，
     已有行再也不被测量（展开一行，位置全错）。
  3. **`measureRef` 靠 `data-virtual-key` 认行** —— 认错了就是把高度
     记到别人身上。用 key 而不是下标：列表头部丢事件时下标会整体左移。

  jsdom 没有 ResizeObserver，所以下面用替身。替身只实现本项目用到的
  `observe` / `disconnect` / 回调，并允许测试手动"报告一次尺寸变化"。
*/

type ROCallback = (entries: Array<{ target: Element; contentRect: { height: number } }>) => void

/** 替身 ResizeObserver：记录观察了哪些节点，可手动触发回调 */
class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []

  observed = new Set<Element>()
  disconnected = false
  private readonly cb: ROCallback

  constructor(cb: ROCallback) {
    this.cb = cb
    FakeResizeObserver.instances.push(this)
  }

  observe(node: Element) {
    this.observed.add(node)
  }

  unobserve(node: Element) {
    this.observed.delete(node)
  }

  disconnect() {
    this.observed.clear()
    this.disconnected = true
  }

  /** 让这个 observer 报告一组节点的新高度 */
  report(pairs: Array<[Element, number]>) {
    this.cb(pairs.map(([target, height]) => ({ target, contentRect: { height } })))
  }
}

/** 造一个带 data-virtual-key 的行节点 */
function makeRow(key: string): HTMLElement {
  const el = document.createElement('li')
  el.dataset.virtualKey = key
  return el
}

const KEYS = ['a', 'b', 'c', 'd', 'e']

function setup(count = KEYS.length) {
  return renderHook(() => useVirtualRows({ count, keys: KEYS.slice(0, count) }))
}

describe('useVirtualRows', () => {
  beforeEach(() => {
    FakeResizeObserver.instances = []
    window.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
  })

  afterEach(() => {
    // 还原，避免污染同进程里的其它测试文件
    // @ts-expect-error jsdom 上这个属性本来就不存在
    delete window.ResizeObserver
  })

  it('measureRef 的身份在重渲染之间保持不变', () => {
    const { result, rerender } = setup()
    const first = result.current.measureRef

    rerender()

    // 这条断言守的是 EventRow 的 memo 是否还有效。
    // 一旦有人把它改回 `measureRef(index)` 返回闭包，这里立刻红。
    expect(result.current.measureRef).toBe(first)
  })

  it('measureRef 靠 data-virtual-key 找到对应的行', () => {
    /*
      ★ 这条测试的断言改过两次，前两版都是**假绿**，值得记下来。

      第一版断言 `totalHeight` 变大 —— 错了：总量是各行高度之**和**，
      不管把 200px 记到哪一行，和都一样。所以「恒取第一行」这个改法
      能让它照样通过。

      第二版改用下标（`data-virtual-index`），断言视口落在第几行。它对
      「高度记在哪个**位置**」是敏感的 —— 但仍然错了：列表头部丢事件时
      所有下标会左移，元素上刻的下标是**渲染那一刻**的快照，之后再读到
      就已经指错行。

      现在按 key 认行（`data-virtual-key` = 事件 id，天生不受位移影响），
      断言也改为看位置：把 key `b`（第 1 行）撑很高，滚到 300px：
        正确（记在 b）：偏移 0/100/600/700/800 → 300 落在第 1 行
        错误（记在 a）：偏移 0/600/700/800/900 → 300 落在第 0 行
    */
    const { result } = renderHook(() =>
      useVirtualRows({ count: 5, keys: KEYS, estimatePx: 100 }),
    )
    const observer = FakeResizeObserver.instances[0]

    // 挂一个真实滚动容器：clientHeight 与 scrollTop 在 jsdom 里默认不可用，
    // 用 defineProperty 造出"150px 高的视口"
    const box = document.createElement('div')
    Object.defineProperty(box, 'clientHeight', { value: 150, configurable: true })
    box.scrollTop = 0
    // ★ 必须走 attachContainer（写 state），不是直接改 containerRef.current：
    //   滚动监听挂在"节点进 state"这个信号上，直接改 ref 不会触发 effect，
    //   表现就是滚不动 —— 这条测试本身就在守这个行为。
    act(() => result.current.attachContainer(box))

    const rowB = makeRow('b')
    act(() => result.current.measureRef(rowB))
    act(() => observer.report([[rowB, 500]]))

    // 滚到 300px 并让 hook 收到 scroll 事件
    act(() => {
      box.scrollTop = 300
      box.dispatchEvent(new Event('scroll'))
    })

    // 300px 落在第 1 行（起点 100，终点 600）里
    expect(result.current.firstVisibleIndex).toBe(1)
  })

  it('没有 data-virtual-key 的节点被忽略，不抛错', () => {
    const { result } = setup()
    const observer = FakeResizeObserver.instances[0]
    const bare = document.createElement('li')

    // 这两个分支以前会静默地把高度记到第一行上
    act(() => result.current.measureRef(bare))
    act(() => result.current.measureRef(null))

    expect(observer.observed.size).toBe(0)
  })

  it('高度更新之后 observer 没有被重建（重建会让已有行不再被测量）', () => {
    const { result } = setup()
    const observer = FakeResizeObserver.instances[0]

    const rowA = makeRow('a')
    act(() => result.current.measureRef(rowA))

    // 触发一次高度更新 —— 这正是曾经把 effect 依赖写成 [measuredByKey] 时
    // 会 disconnect 掉旧 observer 的时刻
    act(() => observer.report([[rowA, 120]]))

    expect(observer.disconnected).toBe(false)
    expect(FakeResizeObserver.instances).toHaveLength(1)

    // 更新之后，新挂载的行仍然能被观察（旧 bug 下这里会是 0）
    const rowC = makeRow('c')
    act(() => result.current.measureRef(rowC))
    expect(observer.observed.has(rowC)).toBe(true)
  })

  it('scrollToIndex 在没有容器时不抛错', () => {
    const { result } = setup()
    // 容器 ref 还没挂上（比如服务端渲染的首帧）
    expect(() => result.current.scrollToIndex(3)).not.toThrow()
  })

  it('容器在挂载之后才出现时，滚动监听照样挂得上', () => {
    /*
      ★ 这条守的是"effect 只在挂载时读一次 ref"这个缺陷。

      真实场景：页面先渲染骨架屏，数据到了才换成真正的滚动容器；或者
      条件渲染把容器换掉。如果滚动/尺寸两个 effect 依赖为空、在挂载时
      `containerRef.current` 读一次，那时容器还不存在 → 直接 return，
      之后**再也不会重试**。表现是"能渲染但滚不动、可视区间永远停在
      第一屏"，没有任何报错。

      修法是把容器节点镜像进 state（`attachContainer`），effect 依赖它。

      这条测试同时是**用例 D 的专属守卫**：把 effect 依赖从 `[node]` 改回
      `[]`，下面那次 dispatchEvent 之后 scrollTop 就不会同步，断言立刻红。
    */
    const { result } = setup()

    // 挂载时没有容器 —— 首帧骨架屏的情形
    expect(result.current.attachContainer).toBeTypeOf('function')

    const box = document.createElement('div')
    Object.defineProperty(box, 'clientHeight', { value: 200, configurable: true })
    box.scrollTop = 0

    // 容器现在才出现（晚于首次 effect）
    act(() => result.current.attachContainer(box))

    // 挂上之后监听必须已经生效：滚动一次，hook 要收到并重算
    act(() => {
      box.scrollTop = 250
      box.dispatchEvent(new Event('scroll'))
    })

    // 250px / 估算 76px ≈ 第 3 行（估算高度默认值见 data/virtual.ts）。
    // 不写死具体数字，只要"不再停在第一屏"就说明监听挂上了。
    expect(result.current.firstVisibleIndex).toBeGreaterThan(0)
  })

  it('行数为 0 时区间是空的', () => {
    const { result } = renderHook(() => useVirtualRows({ count: 0, keys: [] }))
    expect(result.current.startIndex).toBe(0)
    expect(result.current.endIndex).toBe(0)
    expect(result.current.totalHeight).toBe(0)
  })
})
