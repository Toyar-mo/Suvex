/**
 * 虚拟滚动的窗口计算 —— **全部是纯函数**，hook 与组件只负责"什么时候算"。
 *
 * ## 为什么把数学单独抽出来
 *
 * 虚拟滚动真正会错的地方不在 DOM，而在三个数字：从第几条开始、到第几条结束、
 * 上下各留多高的占位。这三样算错了，症状是**看起来完全正常**的：
 * 滚到某处突然空一块、或者同一行在两处出现、或者到底部时最后两条滚不出来。
 * 这种 bug 在浏览器里靠肉眼一个个滚很费时间，而它们其实都可以用纯函数断言。
 *
 * ## 高度为什么不能是常数
 *
 * 事件行有折叠态与展开态：折叠态约 68px，展开态可能是 400px（PoC 源码）。
 * 按常数估算会让"展开很多行"的用户滚到后面时位置全错 —— 而且偏差会累积，
 * 因为每个误差都往上加。
 *
 * 所以真实高度靠 ResizeObserver 量回来，存在 `measured` 里；
 * **没量到的那一行**才退回估算值。这与"先按估算渲染、量到再校正"是同一件事，
 * 但这里把它拆成纯函数，校正的时机由调用方决定。
 *
 * ⚠️ 高度变化会**移动**后面的所有行。如果变化发生在视口上方，
 *    用户正在看的内容会整体跳一下 —— 这是虚拟滚动最恼人的一个 bug，
 *    也是 `anchorDelta` 存在的原因（见它的注释）。
 */

/** 未测量行的高度估算：折叠态两条 3 行中文 + 上下内距，取 76px */
export const DEFAULT_ESTIMATE_PX = 76

/**
 * 视口上下各多渲染几条。
 *
 * 为什么必须有：滚动事件是按帧采样的，而一帧内可能滚过几十像素。
 * 只渲染"严格可见"的那几条，快速滚动时就会在边缘看到空白闪烁。
 * 多渲染 6 条（约 450px）足够覆盖一帧的惯性滚动。
 */
export const DEFAULT_OVERSCAN = 6

/** 前缀和形式的行偏移量：`offsets[i]` 是第 i 行的顶边，`offsets[count]` 是总高度 */
export interface HeightOffsets {
  /** 长度恒为 `count + 1`。count 为 0 时是 `[0]` */
  offsets: number[]
  totalHeight: number
}

/**
 * 算出每行的顶边位置。
 *
 * 复杂度 O(n)。列表上限是 `useActivityStream` 的 `MAX_EVENTS = 5000`，
 * 一次扫描 5000 次整数加法在滚动帧里是可以忽略的量级 —— 所以这里不去做
 * "增量维护前缀和"那种优化。它带来的状态同步问题（测量、截断、重排三处都要
 * 跟着改）比省下的几微秒贵得多。
 */
export function buildOffsets(
  count: number,
  estimate: number,
  measured: ReadonlyMap<number, number>,
): HeightOffsets {
  const safeCount = Number.isFinite(count) && count > 0 ? Math.floor(count) : 0
  // 估算值被传成 0 / NaN / 负数时退回默认值：否则整个列表高度为 0，
  // 滚动容器不滚，看起来像"事件流不再更新了"
  const fallback =
    Number.isFinite(estimate) && estimate > 0 ? estimate : DEFAULT_ESTIMATE_PX

  const offsets = new Array<number>(safeCount + 1)
  let acc = 0
  for (let i = 0; i < safeCount; i += 1) {
    offsets[i] = acc
    const h = measured.get(i)
    // 0 高度同样退回估算：刚挂载时 ResizeObserver 可能先报一次 0，
    // 若采信它，那几行会被算成"零高度"，后续所有位置一起上移。
    acc += Number.isFinite(h) && (h as number) > 0 ? (h as number) : fallback
  }
  offsets[safeCount] = acc
  return { offsets, totalHeight: acc }
}

/** 渲染窗口。语义：`[startIndex, endIndex)`，上下用 padding 撑出未渲染的空间 */
export interface VirtualRange {
  startIndex: number
  endIndex: number
  /** 上方未渲染行占出的高度 */
  padTop: number
  /** 下方未渲染行占出的高度 */
  padBottom: number
}

/** 第一个 `offsets[i] >= value` 的 i（即下界） */
function lowerBound(offsets: number[], value: number, count: number): number {
  let lo = 0
  let hi = count
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (offsets[mid] < value) lo = mid + 1
    else hi = mid
  }
  return lo
}

/** 第一个 `offsets[i] > value` 的 i（即上界） */
function upperBound(offsets: number[], value: number, count: number): number {
  let lo = 0
  let hi = count
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (offsets[mid] <= value) lo = mid + 1
    else hi = mid
  }
  return lo
}

/**
 * 从滚动位置算出该渲染哪几行。
 *
 * `scrollTop` 会被夹进 `[0, totalHeight]`：浏览器在内容变矮时（比如删了行、
 * 或有行从展开态收回折叠态）会自己调整 scrollTop，两者之间有一帧的错位，
 * 夹一次可以避免算出一个不存在的位置。
 */
export function computeRange(
  { offsets, totalHeight }: HeightOffsets,
  scrollTop: number,
  viewportHeight: number,
  overscan: number = DEFAULT_OVERSCAN,
): VirtualRange {
  const count = offsets.length - 1
  if (count <= 0) {
    return { startIndex: 0, endIndex: 0, padTop: 0, padBottom: 0 }
  }

  const top = clamp(Number.isFinite(scrollTop) ? scrollTop : 0, 0, Math.max(0, totalHeight))
  const height = Number.isFinite(viewportHeight) && viewportHeight > 0 ? viewportHeight : 0
  const bottom = top + height

  // 可见区第一条：顶边落在 top 上的那条，或它前一条（跨过 top 的那条）
  const first = Math.max(0, upperBound(offsets, top, count) - 1)
  // 可见区末尾（不含）：第一条顶边 >= bottom 的就是它
  let lastExclusive = Math.max(first + 1, lowerBound(offsets, bottom, count))

  /*
    ⚠️ 视口高度为 0 时（挂载第一帧、或容器被 display:none 时）仍要渲染至少一条。
       否则首屏是空白，而"空白"看起来和"没有事件"一模一样 ——
       用户会以为事件流坏了。渲染一条的代价可以忽略。
  */
  if (height === 0) lastExclusive = first + 1

  const extra = Number.isFinite(overscan) && overscan > 0 ? Math.floor(overscan) : 0
  const startIndex = clamp(first - extra, 0, count - 1)
  const endIndex = clamp(lastExclusive + extra, startIndex + 1, count)

  return {
    startIndex,
    endIndex,
    padTop: offsets[startIndex],
    padBottom: totalHeight - offsets[endIndex],
  }
}

/**
 * 高度变化后，滚动位置该补偿多少。
 *
 * ## 它解决的问题
 *
 * 视口上方有一行从折叠变展开（高度 +300px），它下面所有行的顶边都下移 300px。
 * 而 `scrollTop` 没变，于是**用户正在看的内容整体上移了 300px** ——
 * 表现为"我明明没滚，内容自己跳了"。
 *
 * ## 做法
 *
 * 记下变化前后的偏移表，取一个**锚点行**（通常是当前视口的第一条可见行），
 * 算出它的顶边移动了多少，把 `scrollTop` 加上同样的量。锚点行在屏幕上
 * 位置不变，视觉上就完全静止。
 *
 * 锚点必须选**视口内**的行，不能选"第一条"：第一条变化时它自己在动，
 * 拿它当锚点等于没补偿。
 */
export function anchorDelta(
  before: HeightOffsets,
  after: HeightOffsets,
  anchorIndex: number,
): number {
  const count = Math.min(before.offsets.length, after.offsets.length) - 1
  if (count <= 0) return 0
  /*
    ⚠️ 夹到 `count - 1` 而不是 `count`。

    `offsets[count]` 是**总高度**，不是某一行的顶边。如果越界锚点被夹到那里，
    最后一行的展开（它只改变总高度、不改变任何行的顶边）会被算成一次位移，
    于是 `scrollTop` 被无谓地补偿一次 —— 用户会看到内容凭空下移。

    锚点的定义是"某一行"，合法范围就是 `0 .. count-1`。
  */
  const i = clamp(Math.floor(anchorIndex), 0, count - 1)
  const delta = after.offsets[i] - before.offsets[i]
  return Number.isFinite(delta) ? delta : 0
}

export function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min
  if (value < min) return min
  if (value > max) return max
  return value
}
