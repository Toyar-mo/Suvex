import { describe, expect, it } from 'vitest'
import type { ActivityEvent } from './activity'
import {
  SILENCE_THRESHOLD_MS,
  buildTicks,
  buildTimeline,
  formatGap,
  gapIsSuspicious,
  timelineEmptyKind,
} from './timeline'

/**
 * 时间线的纯逻辑。
 *
 * 这些断言守的不是"点画得对不对"，而是**静默段会不会被抹掉**：
 * 32 分钟零产出那次事故里，事件流看起来一直在滚，用户直到 32 分钟后才起疑。
 * 时间线视图存在的全部理由就是把那段静默画出来。所以最重要的几条用例
 * 都在尾部静默、乱序到达、以及"别把结束后的安静当故障"上。
 */

const T0 = Date.UTC(2026, 9, 24, 12, 0, 0)

function ev(id: string, atOffsetMs: number, type: ActivityEvent['type'] = 'tool_call'): ActivityEvent {
  return {
    id,
    scanId: 'scan_1',
    agentId: 'agent-01',
    type,
    summary: `事件 ${id}`,
    detail: null,
    at: new Date(T0 + atOffsetMs).toISOString(),
  }
}

describe('buildTimeline', () => {
  it('按 offset 与百分比摆放事件点', () => {
    const model = buildTimeline([ev('a', 0), ev('b', 30_000), ev('c', 60_000)], { now: T0 + 60_000 })
    expect(model.points.map((p) => p.offsetMs)).toEqual([0, 30_000, 60_000])
    expect(model.points.map((p) => Math.round(p.pct))).toEqual([0, 50, 100])
    expect(model.durationMs).toBe(60_000)
  })

  it('按 at 排序，不按到达顺序', () => {
    /*
      SSE 重连补发会乱序。如果按到达顺序摆，时间轴上会出现"后面的点跑到前面"，
      而那条线看起来仍然像模像样 —— 只是时间是错的。
    */
    const model = buildTimeline([ev('late', 60_000), ev('early', 0), ev('mid', 30_000)], {
      now: T0 + 60_000,
    })
    expect(model.points.map((p) => p.event.id)).toEqual(['early', 'mid', 'late'])
  })

  it('同一毫秒的多条事件保持原有相对顺序', () => {
    const model = buildTimeline([ev('x', 1000), ev('y', 1000), ev('z', 1000)], { now: T0 + 1000 })
    expect(model.points.map((p) => p.event.id)).toEqual(['x', 'y', 'z'])
  })

  it('相邻事件间隔超过门槛时记为一段静默', () => {
    const model = buildTimeline([ev('a', 0), ev('b', SILENCE_THRESHOLD_MS)], {
      now: T0 + SILENCE_THRESHOLD_MS,
    })
    expect(model.gaps).toHaveLength(1)
    expect(model.gaps[0].durationMs).toBe(SILENCE_THRESHOLD_MS)
    expect(model.gaps[0].afterIndex).toBe(1)
  })

  it('间隔不到门槛时不记静默（正常深扫不该满屏飘红）', () => {
    const model = buildTimeline([ev('a', 0), ev('b', SILENCE_THRESHOLD_MS - 1)], {
      now: T0 + SILENCE_THRESHOLD_MS,
    })
    expect(model.gaps).toHaveLength(0)
  })

  it('★ 最后一条到 now 的静默也要算（尾部静默，最要紧的那一段）', () => {
    /*
      32 分钟零产出那次，事故形态不是"两条事件之间的长间隔"，而是
      "事件流停在最后一条上不动了"。只算相邻间隔，这段时间在图上宽度为 0。
      这条用例就是防止那个疏漏回来。
    */
    const model = buildTimeline([ev('a', 0), ev('b', 1000)], { now: T0 + 33 * 60_000 })
    const tail = model.gaps.find((g) => g.afterIndex === 2)
    expect(tail).toBeDefined()
    expect(tail!.durationMs).toBe(33 * 60_000 - 1000)
    expect(tail!.endMs).toBe(model.durationMs)
    expect(Math.round(tail!.widthPct)).toBeGreaterThan(90)
  })

  it('尾部静默不到门槛时不记', () => {
    const model = buildTimeline([ev('a', 0)], { now: T0 + 5_000 })
    expect(model.gaps).toHaveLength(0)
  })

  it('不传 now 时轴终点取最后一条事件（不会凭空造出尾部静默）', () => {
    const model = buildTimeline([ev('a', 0), ev('b', 10_000)])
    expect(model.durationMs).toBe(10_000)
    expect(model.gaps).toHaveLength(0)
  })

  it('now 早于最后一条事件时轴终点仍不早于事件（时钟漂移不把点挤到轴外）', () => {
    const model = buildTimeline([ev('a', 0), ev('b', 10_000)], { now: T0 + 1_000 })
    expect(model.endMs).toBe(T0 + 10_000)
    expect(model.points.every((p) => p.pct <= 100)).toBe(true)
  })

  it('时间戳非法的事件被丢弃，而不是当成 0 画在轴最左端', () => {
    // 当成 0 会造出一条"扫描刚开始时发生的"假事实，看起来完全合理
    const broken: ActivityEvent = { ...ev('bad', 0), at: '不是时间' }
    const model = buildTimeline([broken, ev('a', 5000)], { now: T0 + 5000 })
    expect(model.points.map((p) => p.event.id)).toEqual(['a'])
  })

  it('全部时间戳非法时返回空模型，不抛错', () => {
    const broken: ActivityEvent = { ...ev('bad', 0), at: 'nope' }
    expect(buildTimeline([broken], { now: T0 })).toEqual({
      points: [],
      gaps: [],
      startMs: 0,
      endMs: 0,
      durationMs: 0,
      ticks: [],
    })
  })

  it('空事件流返回空模型', () => {
    expect(buildTimeline([], { now: T0 }).points).toEqual([])
    expect(buildTimeline([], { now: T0 }).gaps).toEqual([])
  })

  it('单条事件：轴长度为 0 时百分比为 0 且不除零', () => {
    const model = buildTimeline([ev('a', 0)], { now: T0 })
    expect(model.durationMs).toBe(0)
    expect(model.points[0].pct).toBe(0)
    expect(Number.isFinite(model.points[0].pct)).toBe(true)
  })

  it('多段静默全部被找出，且按时间顺序', () => {
    const model = buildTimeline(
      [
        ev('a', 0),
        ev('b', 3 * 60_000),
        ev('c', 4 * 60_000),
        ev('d', 20 * 60_000),
      ],
      { now: T0 + 20 * 60_000 },
    )
    expect(model.gaps.map((g) => Math.round(g.durationMs / 60_000))).toEqual([3, 16])
    expect(model.gaps[0].startMs).toBeLessThan(model.gaps[1].startMs)
  })

  it('提高门槛会把短静默过滤掉', () => {
    const events = [ev('a', 0), ev('b', 5 * 60_000)]
    expect(buildTimeline(events, { now: T0 + 5 * 60_000 }).gaps).toHaveLength(1)
    expect(
      buildTimeline(events, { now: T0 + 5 * 60_000, silenceThresholdMs: 10 * 60_000 }).gaps,
    ).toHaveLength(0)
  })

  it('非法门槛退回默认值（不会变成"每段都算静默"）', () => {
    for (const bad of [0, -1, Number.NaN]) {
      const model = buildTimeline([ev('a', 0), ev('b', 1)], {
        now: T0 + 1,
        silenceThresholdMs: bad,
      })
      expect(model.gaps).toHaveLength(0)
    }
  })

  it('静默段的宽度百分比与时长成正比', () => {
    /*
      两段静默 120s 与 240s，在同一根 360s 的轴上应占 1/3 与 2/3。
      宽度算错会让用户误判"哪一段更久" —— 而这正是他要用这张图回答的问题。
    */
    const model = buildTimeline([ev('a', 0), ev('b', 120_000), ev('c', 360_000)], {
      now: T0 + 360_000,
    })
    expect(model.gaps).toHaveLength(2)
    const [first, second] = model.gaps
    expect(first.durationMs).toBe(120_000)
    expect(second.durationMs).toBe(240_000)
    expect(first.widthPct).toBeCloseTo(33.33, 1)
    expect(second.widthPct).toBeCloseTo(66.67, 1)
    expect(second.widthPct / first.widthPct).toBeCloseTo(2, 1)
  })
})

describe('buildTicks', () => {
  it('刻度数不超过上限', () => {
    for (const duration of [1000, 45_000, 60_000, 600_000, 3_600_000, 86_400_000]) {
      expect(buildTicks(duration, 6).length).toBeLessThanOrEqual(7)
    }
  })

  it('第一个刻度恒在 0，最后一个不超过总时长', () => {
    const ticks = buildTicks(10 * 60_000, 6)
    expect(ticks[0].offsetMs).toBe(0)
    expect(ticks[ticks.length - 1].offsetMs).toBeLessThanOrEqual(10 * 60_000)
  })

  it('末刻度补总时长（用户最想知道一共多久）', () => {
    const ticks = buildTicks(7 * 60_000 + 13_000, 6)
    expect(ticks[ticks.length - 1].pct).toBe(100)
    expect(ticks[ticks.length - 1].offsetMs).toBe(7 * 60_000 + 13_000)
  })

  it('零长度轴给一个 0 刻度', () => {
    expect(buildTicks(0)).toEqual([{ offsetMs: 0, pct: 0, label: '0 秒' }])
    expect(buildTicks(Number.NaN)).toEqual([{ offsetMs: 0, pct: 0, label: '0 秒' }])
  })

  it('刻度按 offset 单调递增', () => {
    const ticks = buildTicks(3_600_000, 6)
    for (let i = 1; i < ticks.length; i += 1) {
      expect(ticks[i].offsetMs).toBeGreaterThan(ticks[i - 1].offsetMs)
    }
  })
})

describe('formatGap', () => {
  it('分秒分层', () => {
    expect(formatGap(5_000)).toBe('5 秒')
    expect(formatGap(60_000)).toBe('1 分')
    expect(formatGap(120_000)).toBe('2 分')
    expect(formatGap(380_000)).toBe('6 分 20 秒')
    expect(formatGap(3_600_000)).toBe('1 小时')
    expect(formatGap(3_780_000)).toBe('1 小时 3 分')
  })

  it('零 / 负数 / NaN 给破折号而不是 "0 秒"', () => {
    // 0 秒静默不存在，显示 "0 秒" 会让人以为真有一段很短的静默
    expect(formatGap(0)).toBe('—')
    expect(formatGap(-1000)).toBe('—')
    expect(formatGap(Number.NaN)).toBe('—')
  })
})

describe('gapIsSuspicious', () => {
  it('扫描在跑 + 超过门槛 → 可疑', () => {
    expect(gapIsSuspicious(SILENCE_THRESHOLD_MS, 'running')).toBe(true)
  })

  it('扫描已结束 → 安静是正常的，不可疑', () => {
    /*
      这条是"别把报警做成噪音"的守门人。扫描结束后本来就没有事件，
      把它标红会让轴永远在飘红，用户学会忽略，真正出事时也没人看。
    */
    expect(gapIsSuspicious(60 * 60_000, 'completed')).toBe(false)
    expect(gapIsSuspicious(60 * 60_000, 'failed')).toBe(false)
    expect(gapIsSuspicious(60 * 60_000, 'queued')).toBe(false)
    expect(gapIsSuspicious(60 * 60_000, null)).toBe(false)
  })

  it('扫描在跑但没超门槛 → 不可疑（正常深扫期间本来就没有事件）', () => {
    expect(gapIsSuspicious(SILENCE_THRESHOLD_MS - 1, 'running')).toBe(false)
  })

  it('非法时长不可疑', () => {
    expect(gapIsSuspicious(Number.NaN, 'running')).toBe(false)
    expect(gapIsSuspicious(-1, 'running')).toBe(false)
  })
})

describe('timelineEmptyKind', () => {
  it('有事件时不是空态', () => {
    expect(timelineEmptyKind(1, true, 'running')).toBe('none')
  })

  it('没有指定扫描 → no-scan', () => {
    expect(timelineEmptyKind(0, false, null)).toBe('no-scan')
  })

  it('指定了扫描、在跑 / 排队 → starting', () => {
    expect(timelineEmptyKind(0, true, 'running')).toBe('starting')
    expect(timelineEmptyKind(0, true, 'queued')).toBe('starting')
  })

  it('指定了扫描、已结束 → produced-nothing（是结论，不是等待）', () => {
    expect(timelineEmptyKind(0, true, 'completed')).toBe('produced-nothing')
    expect(timelineEmptyKind(0, true, 'failed')).toBe('produced-nothing')
  })
})
