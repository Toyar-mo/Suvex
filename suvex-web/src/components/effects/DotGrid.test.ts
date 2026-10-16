import { describe, expect, it } from 'vitest'

import {
  advanceDot,
  pushDot,
  resolvePhysicsParams,
  type Dot,
  type DotPhysicsParams,
} from './DotGrid'

/*
  ══════════════════════════════════════════════════════════════
  DotGrid 的自研物理（2026-10-16 恢复该组件时新增）
  ══════════════════════════════════════════════════════════════

  ## 为什么测这个

  上游用 gsap + InertiaPlugin 做「惯性滑出 + 弹性回位」。本项目不引入
  gsap（GreenSock 自有许可，非 OSI 开源），改成自研物理 ——
  **换掉一个被实测过的库，风险全在新代码这边**，所以要把它的三条承诺
  钉死：

    1. 点真的会被推出去（不是"差不多能动"）；
    2. 一定会回到 0 并收敛（不许发散、不许停在半路）；
    3. 任何 dt（包括标签页切回来那种几秒的）都不会产生 NaN/Infinity。

  这三条在浏览器里只能靠"看着回来了"判断 —— 而本项目明确不接受那种证据。
  物理积分抽成纯函数之后，这里就能给出可证伪的数字。

  ## 口径

  · 步进用 1/60s（真实 rAF 的常见间隔），不是随便一个 dt；
  · "落定"由被测代码自己判定（`advanceDot` 返回 false），
    测试只记录**第几帧**落的定，不自己定义阈值 ——
    否则测的是"我的阈值"而不是"它的行为"。
*/

/** 造一个与组件里 buildGrid 同形状的点 */
function makeDot(cx = 100, cy = 100): Dot {
  return {
    cx,
    cy,
    xOffset: 0,
    yOffset: 0,
    _inertiaApplied: false,
    vx: 0,
    vy: 0,
    targetX: 0,
    targetY: 0,
    phase: 'idle',
    phaseT: 0,
    returnAmp: 1,
  }
}

/** 按 1/60s 步进直到落定，返回帧数与最终状态。帧数上限用于把"不收敛"变成失败而不是挂死。 */
function run(dot: Dot, p: DotPhysicsParams, maxFrames = 3000) {
  let frames = 0
  let peak = 0
  const dt = 1 / 60
  while (frames < maxFrames) {
    const flying = advanceDot(dot, dt, p)
    peak = Math.max(peak, Math.hypot(dot.xOffset, dot.yOffset))
    frames++
    if (!flying) break
  }
  return { frames, peak, dot }
}

/** AgentField / HeroBackdrop 实际传的那组值 */
const PARAMS = resolvePhysicsParams(900, 1.6)

describe('DotGrid 物理：推出去 → 滑停 → 弹回', () => {
  it('★ 被推出去的点真的离开了原位（不是"差不多能动"）', () => {
    const dot = makeDot()
    pushDot(dot, 60, 40)
    expect(dot._inertiaApplied).toBe(true)
    expect(dot.phase).toBe('inertia')

    /*
      一帧（1/60s）的位移是**算得出来**的，所以这个阈值不是拍的：
        目标 72.1px、λ=6 → 一帧后 = 72.1 × (1 - e^{-6/60}) = 6.86px
      而点本身的半径只有 1.5px、格距 30px —— 6.86px 早已是肉眼可见的位移。
      阈值取 3px：远大于"噪声级"，又不去要求一个物理上不存在的初速度
      （惯性是指数逼近，t=0 时速度最大但位移仍从 0 开始）。
    */
    advanceDot(dot, 1 / 60, PARAMS)
    expect(Math.hypot(dot.xOffset, dot.yOffset)).toBeGreaterThan(3)
  })

  it('★ 偏移会回到 0，且闸门重新打开', () => {
    const dot = makeDot()
    pushDot(dot, 60, 40)
    const { dot: end, frames } = run(dot, PARAMS)

    expect(end.xOffset).toBe(0)
    expect(end.yOffset).toBe(0)
    expect(end.phase).toBe('idle')
    expect(end._inertiaApplied).toBe(false)
    // 必须真的收敛，不是撞到 maxFrames 退出的
    expect(frames).toBeLessThan(3000)
  })

  /*
    ★ 这条是**反向验证补出来的**（2026-10-16）。

    "落定判据用能量还是瞬时位置"在调用点实际参数下（returnDuration 1.6s）
    **不可区分** —— 实测：把判据换回瞬时位置，12 条测试全绿。
    原因是每帧位移约 9.9px，而落定阈值只有 0.72px（1% 振幅），
    采样点永远跨过那个过零窗口，采不到"|x| 很小而速度很大"的瞬间。

    两种判据的临界点在 returnDuration ≈ 22s 附近（每帧位移 ≈ 阈值）。
    所以这里跑一档**远离真实配置但仍在函数定义域内**的 25s：
      · 能量判据 → 包络衰减到 1% 需要 25s（4.6/(ζω)）
      · 位置判据 → 第一次过零（约 π/ω ≈ 6s）就被判成"落定"，提前掐断
    于是"落定时间是否 ≥ 半个 returnDuration"就能把两者分开。
  */
  it('★ 落定判据对慢速回弹也成立（区分能量判据 vs 瞬时位置判据）', () => {
    const slow = resolvePhysicsParams(900, 25)
    const dot = makeDot()
    pushDot(dot, 60, 40)
    const { frames, dot: end } = run(dot, slow, 3000)

    expect(frames).toBeLessThan(3000)
    expect(end.phase).toBe('idle')

    const seconds = frames / 60
    // 位置判据会在第一次过零（约 6s）就停；能量判据要 ~25s
    expect(seconds).toBeGreaterThan(12)
    expect(seconds).toBeLessThan(50)
  })

  it('★ 回弹过零到另一侧（弹簧不是单调滑回）', () => {
    const dot = makeDot()
    pushDot(dot, 60, 40)

    let sawNegative = false
    for (let i = 0; i < 3000; i++) {
      if (!advanceDot(dot, 1 / 60, PARAMS)) break
      if (dot.xOffset < -0.5) {
        sawNegative = true
        break
      }
    }
    // 推出去是 +x；没有过冲的话 xOffset 单调降到 0，永远不会变负
    expect(sawNegative).toBe(true)
  })

  it('★ 落定时间在 returnDuration 的量级（1.6s 档）', () => {
    const dot = makeDot()
    pushDot(dot, 60, 40)
    const { frames } = run(dot, PARAMS)
    const seconds = frames / 60
    /*
      实测 1.48s（≈89 帧）。预期区间是"1s 量级"，不是"1.6s 整"：
        · 惯性滑停 ≈ 0.98s（λ=6，从 72px 滑到 0.5px 需要 ln(144)/6）
        · 回弹衰减到 1% ≈ 1.6s 的**包络**，但落定判据用能量，
          而能量里还含速度项，所以比纯包络略早一点点收敛。
      两条加起来约 1.5s —— 与 returnDuration=1.6 同一量级，
      这正是这个 prop 要保住的语义（"大概 1.6 秒回位"）。
    */
    expect(seconds).toBeGreaterThan(1)
    expect(seconds).toBeLessThan(4)
  })

  it('★ 大推与小推的落定时间同量级（returnDuration 语义保住了）', () => {
    const small = makeDot()
    pushDot(small, 5, 3)
    const big = makeDot()
    pushDot(big, 400, 300)

    const a = run(small, PARAMS).frames
    const b = run(big, PARAMS).frames
    // 同一个 prop 就该给同一个时间量级，不该差出数倍
    expect(Math.max(a, b) / Math.min(a, b)).toBeLessThan(2)
  })
})

describe('DotGrid 物理：数值稳（不许 NaN / 发散 / 卡住）', () => {
  it('★ 极端 dt 不产生 NaN（标签页切回来）', () => {
    const dot = makeDot()
    pushDot(dot, 60, 40)

    for (const dt of [0, 1e-6, 1 / 60, 0.5, 3, 60, 3600]) {
      advanceDot(dot, dt, PARAMS)
      expect(Number.isFinite(dot.xOffset)).toBe(true)
      expect(Number.isFinite(dot.yOffset)).toBe(true)
      expect(Number.isFinite(dot.vx)).toBe(true)
      expect(Number.isFinite(dot.vy)).toBe(true)
    }
  })

  it('★ 反复推动（鼠标快速划过）不会发散', () => {
    const dot = makeDot()
    for (let round = 0; round < 40; round++) {
      // 模拟"闸门开着就再推一次"
      if (!dot._inertiaApplied) pushDot(dot, 60, 40)
      for (let f = 0; f < 5; f++) advanceDot(dot, 1 / 60, PARAMS)
      expect(Math.hypot(dot.xOffset, dot.yOffset)).toBeLessThan(1000)
      expect(Number.isFinite(dot.xOffset)).toBe(true)
    }
  })

  it('★ 即使参数被改成发散的组合，兜底超时也会把点拉回静止', () => {
    // resistance 极小 → λ 很小（滑得极慢）；returnDuration 极小 → 兜底预算极小。
    // 这正是"有人改常量改坏"的情形。
    const nasty = resolvePhysicsParams(0, 0.05)
    const dot = makeDot()
    pushDot(dot, 60, 40)
    const { dot: end, frames } = run(dot, nasty, 3000)

    expect(frames).toBeLessThan(3000)
    expect(end.xOffset).toBe(0)
    expect(end.yOffset).toBe(0)
    expect(end._inertiaApplied).toBe(false)
  })

  /*
    ★ 这条是**反向验证补出来的**（2026-10-16）。

    第一版测试在"兜底超时只在回弹分支生效"这个 mutation 下**依然是绿的** ——
    因为 λ 的下限修好之后，惯性阶段总能在几秒内滑完，
    那条 `frames < 3000` 的断言就再也碰不到"惯性阶段没有兜底"这条路。
    也就是说：那条断言看着像在测兜底，其实测的是 λ 收敛。

    所以这里直接把两个量对上：**兜底预算只有 hardCap 秒，
    落定帧数就不该超过 hardCap/dt 太多** —— 与 λ 快慢无关。
    params(150, 0.05) → λ=1（慢，滑完要 5s）、hardCap=0.2s。
    没有兜底的话它会滑满 5s（约 298 帧）；有兜底则 12 帧左右就归零。
  */
  it('★ 兜底超时在**惯性阶段**也生效（不只是回弹）', () => {
    const p = resolvePhysicsParams(150, 0.05)
    expect(p.hardCap).toBeCloseTo(0.2, 5)
    expect(p.lambda).toBeCloseTo(1, 5)

    const dot = makeDot()
    pushDot(dot, 60, 40)
    const { frames, dot: end } = run(dot, p, 3000)

    const budgetFrames = Math.ceil(p.hardCap * 60)
    expect(frames).toBeLessThanOrEqual(budgetFrames + 5)
    expect(end.phase).toBe('idle')
    expect(end._inertiaApplied).toBe(false)
  })

  /*
    ★ 这条同样是反向验证补出来的。

    "有过冲"原先只断言"过程中出现过 x < -0.5"。而落定判据如果退回
    **瞬时位置**（`|x| <= 阈值` 就归零），点会在某次过零时被提前掐断 ——
    但那一刻的 |x| 恰好很小，所以"出现过 -0.5"仍可能成立，断言照绿。
    换句话说：那条断言抓不住"过冲被吃掉"。

    改成断言**过冲的幅度**：第一次变号之后弹到另一侧的峰值，
    理论上是 returnAmp 的 31%（ζ=0.35 → e^{-πζ/√(1-ζ²)}）。
    被提前掐断的话这个峰值会接近 0。取 15% 作阈值 ——
    既远低于理论值、又远高于"被掐断"的水平。
  */
  it('★ 过冲幅度达到理论量级（不是"碰巧出现过负值"）', () => {
    const dot = makeDot()
    pushDot(dot, 60, 40)

    let returnAmp = 0
    let sign = 0
    let peakAfterFlip = 0
    let sawFlip = false
    for (let i = 0; i < 3000; i++) {
      const flying = advanceDot(dot, 1 / 60, PARAMS)
      if (dot.phase === 'return' && returnAmp === 0) returnAmp = dot.returnAmp
      if (dot.xOffset !== 0) {
        const s = Math.sign(dot.xOffset)
        if (sign === 0) sign = s
        else if (s !== sign) {
          sawFlip = true
          peakAfterFlip = Math.max(peakAfterFlip, Math.abs(dot.xOffset))
        }
      }
      if (!flying) break
    }

    expect(sawFlip).toBe(true)
    expect(returnAmp).toBeGreaterThan(0)
    // 理论 31%，被提前掐断会接近 0
    expect(peakAfterFlip / returnAmp).toBeGreaterThan(0.15)
  })

  it('★ 非法 resistance（0 / 负数）不会让惯性阶段永远滑不到头', () => {
    const p = resolvePhysicsParams(-5, 1.6)
    expect(p.lambda).toBeGreaterThan(0)
    const dot = makeDot()
    pushDot(dot, 60, 40)
    const { frames } = run(dot, p, 3000)
    expect(frames).toBeLessThan(3000)
  })

  it('★ 闸门语义：飞行中不会被再次推动（否则点会黏在光标上）', () => {
    const dot = makeDot()
    pushDot(dot, 60, 40)
    expect(dot._inertiaApplied).toBe(true)

    // 飞行中再推：组件里的条件是 `!dot._inertiaApplied`，所以推不进来。
    // 这里直接验证"闸门为真"这个前提成立 —— 上面的 run() 能落定就是它的后果。
    advanceDot(dot, 1 / 60, PARAMS)
    expect(dot._inertiaApplied).toBe(true)

    const { dot: end } = run(dot, PARAMS)
    expect(end._inertiaApplied).toBe(false)
  })
})
