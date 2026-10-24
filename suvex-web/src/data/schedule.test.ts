import { describe, expect, it } from 'vitest'
import {
  MAX_INTERVAL_MINUTES,
  MIN_INTERVAL_MINUTES,
  INTERVAL_PRESETS,
  compareSchedules,
  formatInterval,
  formatRelative,
  scheduleMatchesQuery,
  scheduleState,
  type Schedule,
} from './schedule'

/**
 * 定时计划的纯逻辑。
 *
 * 这些是**判据**，不是格式化工具 —— `scheduleState` 与 `compareSchedules`
 * 决定了用户第一眼看到哪一条，判错会让人以为"计划一直在跑"而其实每次都失败。
 */

const NOW = Date.UTC(2026, 9, 24, 12, 0, 0)

function make(overrides: Partial<Schedule> = {}): Schedule {
  return {
    id: 'sched_1',
    name: '每日复查',
    target: 'api.acme.internal',
    targetKind: 'url',
    goal: 'findVulns',
    intervalMinutes: 1440,
    enabled: true,
    nextRunAt: new Date(NOW + 3_600_000).toISOString(),
    lastScanId: null,
    lastRunAt: null,
    failureCount: 0,
    lastFailureReason: null,
    providerId: 'prov_default',
    providerLabel: 'DeepSeek',
    skills: [],
    brief: null,
    createdAt: new Date(NOW - 86_400_000).toISOString(),
    updatedAt: new Date(NOW - 86_400_000).toISOString(),
    ...overrides,
  }
}

describe('scheduleState', () => {
  it('启用却没有下次时间 → broken，不能当成"停用"糊过去', () => {
    // 数据库 CHECK 本该拦住这个组合。真出现说明数据被手改过，
    // 界面上必须能看出来 —— 当成"停用"会让用户以为是自己关的。
    const state = scheduleState(make({ enabled: true, nextRunAt: null }), NOW)
    expect(state).toBe('broken')
  })

  it('停用优先于"待首次执行" —— 用户主动关掉的计划不该显示成"等第一次跑"', () => {
    const state = scheduleState(
      make({ enabled: false, nextRunAt: null, lastRunAt: null, failureCount: 0 }),
      NOW,
    )
    expect(state).toBe('paused')
  })

  it('连续失败优先于"到点" —— 失败的才是要人管的那条', () => {
    const state = scheduleState(
      make({
        failureCount: 2,
        lastRunAt: new Date(NOW - 60_000).toISOString(),
        nextRunAt: new Date(NOW - 1000).toISOString(),
      }),
      NOW,
    )
    expect(state).toBe('failing')
  })

  it('建好但从没到点 → never-run，不是 ok', () => {
    const state = scheduleState(make({ lastRunAt: null }), NOW)
    expect(state).toBe('never-run')
  })

  it('下次时间已过去 → due（调度器可能没在跑）', () => {
    const state = scheduleState(
      make({ lastRunAt: new Date(NOW - 90_000_000).toISOString(), nextRunAt: new Date(NOW - 1000).toISOString() }),
      NOW,
    )
    expect(state).toBe('due')
  })

  it('正常计划 → ok', () => {
    const state = scheduleState(make({ lastRunAt: new Date(NOW - 86_400_000).toISOString() }), NOW)
    expect(state).toBe('ok')
  })

  it('下次时间是垃圾字符串 → broken，不静默当成 ok', () => {
    const state = scheduleState(
      make({ lastRunAt: new Date(NOW - 1000).toISOString(), nextRunAt: 'not-a-date' }),
      NOW,
    )
    expect(state).toBe('broken')
  })
})

describe('compareSchedules', () => {
  it('数据异常排最前', () => {
    const broken = make({ id: 'a', name: 'a', enabled: true, nextRunAt: null })
    const ok = make({ id: 'b', name: 'b', lastRunAt: new Date(NOW - 1000).toISOString() })
    expect([ok, broken].sort((x, y) => compareSchedules(x, y, NOW))[0].id).toBe('a')
  })

  it('连续失败排在正常之前', () => {
    const failing = make({ id: 'a', name: 'a', failureCount: 1, lastRunAt: new Date(NOW - 1000).toISOString() })
    const ok = make({ id: 'b', name: 'b', lastRunAt: new Date(NOW - 1000).toISOString() })
    expect([ok, failing].sort((x, y) => compareSchedules(x, y, NOW))[0].id).toBe('a')
  })

  /*
    ★ 上面那几条两两比较**单独拿出来是不够的**（反向验证实测证明）：
      `[ok, failing]` 里 ok 的档位是 4、failing 是 1，只要比较函数返回
      "小的在前"，哪怕它把 a 的档位写死成 0（完全不算状态），结果也恰好是
      failing 在前 —— 测试通过，但排序逻辑其实没被验证。

    所以这里再补一条**整表排序**：五个档位各一条，名字取成与期望顺序
    恰好相反的字母序（z 打头的排最前），任何一处档位写错或比较函数
    退化成"只算一边"，全序列断言都会立刻红。
  */
  it('整表排序：五档齐全时顺序必须是 异常→失败→到点→待首次→正常→停用', () => {
    const broken = make({ id: 'broken', name: 'z-broken', enabled: true, nextRunAt: null })
    const failing = make({
      id: 'failing',
      name: 'y-failing',
      failureCount: 2,
      lastRunAt: new Date(NOW - 5000).toISOString(),
      nextRunAt: new Date(NOW + 60_000).toISOString(),
    })
    const due = make({
      id: 'due',
      name: 'x-due',
      lastRunAt: new Date(NOW - 5000).toISOString(),
      nextRunAt: new Date(NOW - 60_000).toISOString(),
    })
    const neverRun = make({ id: 'never-run', name: 'w-never', enabled: true, nextRunAt: new Date(NOW + 600_000).toISOString() })
    const ok = make({
      id: 'ok',
      name: 'v-ok',
      lastRunAt: new Date(NOW - 5000).toISOString(),
      nextRunAt: new Date(NOW + 3_600_000).toISOString(),
    })
    const paused = make({ id: 'paused', name: 'a-paused', enabled: false, nextRunAt: null })

    const sorted = [ok, paused, neverRun, due, failing, broken]
      .sort((x, y) => compareSchedules(x, y, NOW))
      .map((s) => s.id)

    expect(sorted).toEqual(['broken', 'failing', 'due', 'never-run', 'ok', 'paused'])
  })

  it('停用排最后 —— 但不能被隐藏，也不能排到前面占视线', () => {
    const paused = make({ id: 'a', name: 'a', enabled: false, nextRunAt: null })
    const ok = make({ id: 'b', name: 'b', lastRunAt: new Date(NOW - 1000).toISOString() })
    expect([paused, ok].sort((x, y) => compareSchedules(x, y, NOW))[0].id).toBe('b')
  })

  it('同档内按下次执行时间升序 —— 最近要跑的排前面', () => {
    const soon = make({
      id: 'a',
      name: 'z-soon',
      lastRunAt: new Date(NOW - 1000).toISOString(),
      nextRunAt: new Date(NOW + 60_000).toISOString(),
    })
    const later = make({
      id: 'b',
      name: 'a-later',
      lastRunAt: new Date(NOW - 1000).toISOString(),
      nextRunAt: new Date(NOW + 7_200_000).toISOString(),
    })
    // 名字故意倒过来取：字母序恰好相反，排序必须按时间而不是按名字
    expect([later, soon].sort((x, y) => compareSchedules(x, y, NOW))[0].id).toBe('a')
  })

  it('完全同档同时刻时用名字做确定性 tie-break', () => {
    const a = make({ id: 'a', name: 'a', lastRunAt: new Date(NOW - 1000).toISOString() })
    const b = make({ id: 'b', name: 'b', lastRunAt: new Date(NOW - 1000).toISOString() })
    expect(compareSchedules(a, b, NOW)).toBeLessThan(0)
    expect(compareSchedules(b, a, NOW)).toBeGreaterThan(0)
    expect(compareSchedules(a, a, NOW)).toBe(0)
  })
})

describe('formatInterval', () => {
  it('能整除天的用天', () => {
    expect(formatInterval(1440)).toBe('每 1 天')
    expect(formatInterval(2880)).toBe('每 2 天')
  })

  it('能整除小时的用小时', () => {
    expect(formatInterval(60)).toBe('每 1 小时')
    expect(formatInterval(360)).toBe('每 6 小时')
  })

  it('90 分钟不写成 1.5 小时 —— 带小数的间隔会让人怀疑数值', () => {
    expect(formatInterval(90)).toBe('每 90 分钟')
  })

  it('非法输入给破折号而不是 NaN', () => {
    expect(formatInterval(0)).toBe('—')
    expect(formatInterval(-5)).toBe('—')
    expect(formatInterval(NaN)).toBe('—')
  })
})

describe('formatRelative', () => {
  it('未来说"后"，过去说"前"', () => {
    expect(formatRelative(new Date(NOW + 3 * 3_600_000).toISOString(), NOW)).toBe('3 小时后')
    expect(formatRelative(new Date(NOW - 2 * 86_400_000).toISOString(), NOW)).toBe('2 天前')
  })

  it('一分钟以内说"刚刚" —— "0 分钟后"是假精确', () => {
    expect(formatRelative(new Date(NOW + 1000).toISOString(), NOW)).toBe('刚刚')
    expect(formatRelative(new Date(NOW - 1000).toISOString(), NOW)).toBe('刚刚')
  })

  it('null 与垃圾值都给出破折号', () => {
    expect(formatRelative(null, NOW)).toBe('—')
    expect(formatRelative('nope', NOW)).toBe('—')
  })
})

describe('scheduleMatchesQuery', () => {
  it('名字和目标都参与匹配', () => {
    const schedule = make({ name: '每日复查', target: 'api.acme.internal' })
    expect(scheduleMatchesQuery(schedule, '每日')).toBe(true)
    expect(scheduleMatchesQuery(schedule, 'ACME')).toBe(true)
    expect(scheduleMatchesQuery(schedule, 'nothing')).toBe(false)
  })

  it('空查询匹配一切', () => {
    expect(scheduleMatchesQuery(make(), '   ')).toBe(true)
  })
})

describe('常量', () => {
  it('预设间隔全部落在后端允许的范围内', () => {
    for (const preset of INTERVAL_PRESETS) {
      expect(preset.minutes).toBeGreaterThanOrEqual(MIN_INTERVAL_MINUTES)
      expect(preset.minutes).toBeLessThanOrEqual(MAX_INTERVAL_MINUTES)
    }
  })

  it('后端范围与 app/schedules/models.py 一致', () => {
    // 跨仓库对账点：改一边必须同时改另一边，否则前端能选出一个
    // 后端会 400 的间隔 —— 而报错在提交那一刻才出现。
    expect(MIN_INTERVAL_MINUTES).toBe(5)
    expect(MAX_INTERVAL_MINUTES).toBe(365 * 24 * 60)
  })
})
