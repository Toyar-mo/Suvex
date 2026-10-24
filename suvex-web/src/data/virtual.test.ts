import { describe, expect, it } from 'vitest'
import {
  DEFAULT_ESTIMATE_PX,
  DEFAULT_OVERSCAN,
  anchorDelta,
  buildOffsets,
  clamp,
  computeRange,
} from './virtual'

/**
 * 虚拟滚动的窗口计算。
 *
 * 这里守的是三类**在浏览器里很难看出来**的错：
 *   1. 滚到某处空一块（区间算漏了）；
 *   2. 同一行在两处出现（区间重叠）；
 *   3. 最后几条滚不出来（`endIndex` 没把底部算进去）。
 *
 * 每一条都单独断言，因为它们的症状彼此不同、修法也彼此不同。
 */

const NO_MEASURED: ReadonlyMap<number, number> = new Map()

describe('buildOffsets', () => {
  it('每条都用估算值时，偏移量是等差数列', () => {
    const { offsets, totalHeight } = buildOffsets(3, 100, NO_MEASURED)
    expect(offsets).toEqual([0, 100, 200, 300])
    expect(totalHeight).toBe(300)
  })

  it('offsets 的长度恒为 count + 1，空列表是 [0]', () => {
    expect(buildOffsets(0, 100, NO_MEASURED).offsets).toEqual([0])
    expect(buildOffsets(0, 100, NO_MEASURED).totalHeight).toBe(0)
    const { offsets } = buildOffsets(5, 100, NO_MEASURED)
    expect(offsets).toHaveLength(6)
  })

  it('已测量的行用真实高度，其余退回估算值', () => {
    const measured = new Map([
      [0, 200],
      [2, 50],
    ])
    const { offsets, totalHeight } = buildOffsets(3, 100, measured)
    expect(offsets).toEqual([0, 200, 300, 350])
    expect(totalHeight).toBe(350)
  })

  it('估算值被传成 0 / 负数 / NaN 时退回默认值', () => {
    /*
      这条守的是一个具体的坏结果：估算值为 0 会让 totalHeight 变成 0，
      滚动容器没有可滚高度，滚动事件不再触发 —— 表现为
      "事件流卡住不更新了"，而事件其实一直在进来。
    */
    for (const bad of [0, -10, Number.NaN]) {
      expect(buildOffsets(2, bad, NO_MEASURED).totalHeight).toBe(DEFAULT_ESTIMATE_PX * 2)
    }
  })

  it('测量到 0 高度的行不采信，退回估算值', () => {
    // 刚挂载时 ResizeObserver 可能先报一次 0；采信它会让后续所有行整体上移
    const measured = new Map([[0, 0]])
    const { offsets } = buildOffsets(2, 100, measured)
    expect(offsets).toEqual([0, 100, 200])
  })

  it('非法 count（负数 / NaN）当作空列表', () => {
    expect(buildOffsets(-3, 100, NO_MEASURED).offsets).toEqual([0])
    expect(buildOffsets(Number.NaN, 100, NO_MEASURED).offsets).toEqual([0])
  })
})

describe('computeRange', () => {
  /** 100 行 × 100px = 10000 总高 */
  const tall = () => buildOffsets(100, 100, NO_MEASURED)

  it('在顶部时从第 0 行开始，上方不留空', () => {
    const range = computeRange(tall(), 0, 500, 0)
    expect(range.startIndex).toBe(0)
    expect(range.padTop).toBe(0)
    // 500px 视口 = 5 行（第 0..4 行的顶边 < 500）
    expect(range.endIndex).toBe(5)
  })

  it('滚动到中间时，区间落在视口内且前后都有占位', () => {
    const range = computeRange(tall(), 2000, 500, 0)
    expect(range.startIndex).toBe(20) // 第 20 行顶边正好 2000
    expect(range.endIndex).toBe(25)
    expect(range.padTop).toBe(2000)
    expect(range.padBottom).toBe(10000 - 2500)
  })

  it('滚到底部时最后一条被包含（不会滚不出来）', () => {
    const total = 10000
    const range = computeRange(tall(), total - 500, 500, 0)
    expect(range.endIndex).toBe(100)
    expect(range.padBottom).toBe(0)
  })

  it('滚过内容底部时被夹住，不产生空区间', () => {
    const range = computeRange(tall(), 999999, 500, 0)
    expect(range.endIndex).toBe(100)
    expect(range.startIndex).toBeLessThan(100)
    expect(Number.isFinite(range.padBottom)).toBe(true)
  })

  it('不重叠：padTop + 渲染出的真实高度 + padBottom == totalHeight', () => {
    /*
      这是区间正确性的**总不变量**。任何一处算错（起点漏一条、终点多一条、
      或者首尾没接上）都会让它失败，比逐条比对更稳。
    */
    const { offsets, totalHeight } = tall()
    for (const scrollTop of [0, 137, 2000, 5555, 9500, 9999]) {
      for (const viewport of [0, 200, 500, 1200]) {
        const r = computeRange(buildOffsets(100, 100, NO_MEASURED), scrollTop, viewport, 3)
        const rendered = offsets[r.endIndex] - offsets[r.startIndex]
        expect(r.padTop + rendered + r.padBottom).toBe(totalHeight)
      }
    }
  })

  it('overscan 向两侧各扩几条', () => {
    const tight = computeRange(tall(), 2000, 500, 0)
    const roomy = computeRange(tall(), 2000, 500, 6)
    expect(roomy.startIndex).toBe(tight.startIndex - 6)
    expect(roomy.endIndex).toBe(tight.endIndex + 6)
  })

  it('overscan 不会越界', () => {
    const top = computeRange(tall(), 0, 500, 50)
    expect(top.startIndex).toBe(0)
    const bottom = computeRange(tall(), 9500, 500, 50)
    expect(bottom.endIndex).toBe(100)
  })

  it('视口高度为 0（挂载首帧 / 容器隐藏）仍渲染至少一行', () => {
    /*
      渲染 0 条会让首屏空白，而"空白"和"没有事件"看起来一模一样。
      用户会以为事件流坏了 —— 这是这一页最不该出现的误导。
    */
    const range = computeRange(tall(), 0, 0, 0)
    expect(range.endIndex - range.startIndex).toBeGreaterThanOrEqual(1)
    expect(range.startIndex).toBe(0)
  })

  it('空列表返回空区间，不抛错', () => {
    const empty = buildOffsets(0, 100, NO_MEASURED)
    expect(computeRange(empty, 0, 500, 6)).toEqual({
      startIndex: 0,
      endIndex: 0,
      padTop: 0,
      padBottom: 0,
    })
  })

  it('只有一行时区间恰好是那一行', () => {
    const one = buildOffsets(1, 100, NO_MEASURED)
    const range = computeRange(one, 0, 500, 6)
    expect(range).toEqual({ startIndex: 0, endIndex: 1, padTop: 0, padBottom: 0 })
  })

  it('NaN / 负数滚动位置与默认 overscan 都不炸', () => {
    const range = computeRange(tall(), Number.NaN, Number.NaN)
    expect(range.startIndex).toBe(0)
    expect(range.endIndex).toBeGreaterThanOrEqual(1)
    expect(range.endIndex).toBeLessThanOrEqual(1 + DEFAULT_OVERSCAN + DEFAULT_OVERSCAN)
  })

  it('高度不一的列表也满足总不变量（测量值生效）', () => {
    const measured = new Map([
      [3, 400], // 一条展开的 PoC
      [40, 500],
    ])
    const offsets = buildOffsets(100, 76, measured)
    const { totalHeight } = offsets

    /*
      滚到那条 400px 的行附近（不是第 40 行 —— 那是另一条）。
      断言两件事：总不变量成立，且区间**确实包含**那条高行
      （算错时它会被跳过，表现为"展开的事件凭空消失"）。
    */
    const r = computeRange(offsets, offsets.offsets[2], 600, 2)
    expect(r.padTop + (offsets.offsets[r.endIndex] - offsets.offsets[r.startIndex]) + r.padBottom).toBe(
      totalHeight,
    )
    expect(r.startIndex).toBeLessThanOrEqual(3)
    expect(r.endIndex).toBeGreaterThan(3)
  })
})

describe('anchorDelta', () => {
  it('上方行变高时给出正增量（scrollTop 要跟着加）', () => {
    const before = buildOffsets(10, 100, NO_MEASURED)
    const after = buildOffsets(10, 100, new Map([[0, 300]]))
    // 第 1 行顶边从 100 变成 300
    expect(anchorDelta(before, after, 1)).toBe(200)
  })

  it('锚点行自身变化时不计入（否则等于没补偿）', () => {
    /*
      这是这条函数存在的理由。锚点选"视口第一条可见行"，那一行自己在动，
      拿它当基准就测不出它下面内容的位移 —— 必须用它的**顶边**，
      而顶边只受它上面的行影响，所以点 0 的 delta 恒为 0。
    */
    const before = buildOffsets(10, 100, NO_MEASURED)
    const after = buildOffsets(10, 100, new Map([[0, 300]]))
    expect(anchorDelta(before, after, 0)).toBe(0)
  })

  it('变化发生在锚点下方时增量为 0（不该动）', () => {
    const before = buildOffsets(10, 100, NO_MEASURED)
    const after = buildOffsets(10, 100, new Map([[8, 300]]))
    expect(anchorDelta(before, after, 3)).toBe(0)
  })

  it('行数变化（追加新事件）时锚点仍稳定', () => {
    const before = buildOffsets(10, 100, NO_MEASURED)
    const after = buildOffsets(14, 100, NO_MEASURED)
    expect(anchorDelta(before, after, 5)).toBe(0)
  })

  it('锚点越界被夹住，不抛错', () => {
    const before = buildOffsets(10, 100, NO_MEASURED)
    const after = buildOffsets(10, 100, new Map([[9, 300]]))
    expect(anchorDelta(before, after, 999)).toBe(0)
    expect(anchorDelta(before, after, -5)).toBe(0)
  })

  it('空列表返回 0', () => {
    expect(anchorDelta(buildOffsets(0, 100, NO_MEASURED), buildOffsets(0, 100, NO_MEASURED), 0)).toBe(
      0,
    )
  })
})

describe('clamp', () => {
  it('夹在区间内', () => {
    expect(clamp(5, 0, 10)).toBe(5)
    expect(clamp(-1, 0, 10)).toBe(0)
    expect(clamp(11, 0, 10)).toBe(10)
  })

  it('NaN 退回下界而不是原样返回', () => {
    // 原样返回 NaN 会让它一路流进 offsets[i] 变成 undefined，报错点离源头很远
    expect(clamp(Number.NaN, 3, 10)).toBe(3)
  })
})
