import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import TimelineView from './TimelineView'
import type { ActivityEvent } from '@/data/activity'

/*
  ══════════════════════════════════════════════════════════════
  时间线视图（虚拟滚动那一批的最后一块）
  ══════════════════════════════════════════════════════════════

  ## 这个文件守的几件事

    `data/timeline.test.ts` 已经守住了纯逻辑（静默段怎么算、轴怎么刻度）。
    这里守的是**组件这一层**特有的东西 —— 那些纯逻辑测试看不见的：

      1. 空态的**三种**措辞不能混（没指定扫描 / 刚开始 / 跑完了但没事件）。
         混掉的话用户分不清"还没开始"和"已经结束且空"，而这两者
         需要的下一步完全相反。

      2. 点静默段时交出去的**边界事件**必须来自同一个 `model`。
         这是本组件最容易写错的一处：父级手里有原始 `events`，若由它
         用 `gap.afterIndex` 去取，SSE 重连补发导致顺序不等于到达顺序时
         就会错位 —— 而且只在重连那几次错。所以边界必须由这里算好。

      3. 尾部静默（`after === null`）要能点，且交出去的 `after` 是 null。
         父级靠这个 null 决定"没落点可跳"，编不出事件。

      4. 没给 `onSelectGap` 时，静默条目**不可点**（disabled）。
         否则按钮看着能按、按了没反应，比不能按更糟。

  ## 坑

  vitest 关了 globals → 显式 `afterEach(cleanup)`。
  这个组件没有子组件、不发请求，所以不 mock 任何模块。
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

afterEach(() => {
  cleanup()
})

describe('TimelineView 空态', () => {
  it('没指定扫描时说"先选一次"，而不是说"还没有事件"', () => {
    render(
      <TimelineView
        events={[]}
        scanStatus={null}
        scanMarked={false}
        // 没指定扫描时不该有轴 —— 也就不该出现"静默"这类结论
        onSelectGap={() => {}}
      />,
    )
    expect(screen.getByText(/先在上面选一次/)).toBeTruthy()
  })

  it('扫描刚开始时说"会随时间长出来"，与"跑完却空"分开', () => {
    render(<TimelineView events={[]} scanStatus="running" scanMarked onSelectGap={() => {}} />)
    expect(screen.getByText(/扫描刚开始/)).toBeTruthy()
  })

  it('已结束且无事件时明说"已经结束"，不让人继续等', () => {
    render(<TimelineView events={[]} scanStatus="completed" scanMarked onSelectGap={() => {}} />)
    const text = screen.getByText(/已经结束了/)
    expect(text).toBeTruthy()
    // 关键是否定"还在等"这个误读
    expect(text.textContent).toMatch(/不是"还在等"/)
  })
})

describe('TimelineView 静默段', () => {
  /*
    造一段 10 分钟静默：a 在 0s、b 在 600s。
    门槛是 2 分钟（`SILENCE_THRESHOLD_MS`），所以这段算可疑静默。
  */
  const withGap = [ev('a', 0), ev('b', 600_000)]

  it('点静默段时交出的边界来自重排后的 model，不是原始数组下标', () => {
    /*
      ★ 乱序输入：'late' 的 at 最晚、但排在数组最前面。

      这正是 SSE 重连补发造成的形态。若组件用原始数组下标取边界，
      交出去的会是数组里的邻居（错的）；用 model 才对。
    */
    const late = ev('late', 900_000)
    const events = [late, ev('a', 0), ev('b', 600_000)]

    const onSelect = vi.fn()
    render(<TimelineView events={events} scanStatus="running" scanMarked onSelectGap={onSelect} />)

    fireEvent.click(screen.getAllByRole('button')[0] ?? screen.getByText(/静默/))

    expect(onSelect).toHaveBeenCalled()
    const [, boundary] = onSelect.mock.calls[0]
    // 第一段静默：0s → 600s，两侧就是 a 与 b（不是数组邻居 late/a）
    expect(boundary.before?.id).toBe('a')
    expect(boundary.after?.id).toBe('b')
  })

  it('尾部静默交出的 after 是 null（父级靠它判断没落点可跳）', () => {
    /*
      a 在 0s，轴终点由 now 给到 20 分钟 → 后面那 20 分钟是尾部静默。
      不传 now 的话轴到 a 就结束，宽度为 0，静默直接不存在。
    */
    const onSelect = vi.fn()
    render(
      <TimelineView
        events={[ev('a', 0)]}
        scanStatus="running"
        scanMarked
        now={T0 + 1_200_000}
        onSelectGap={onSelect}
      />,
    )

    const buttons = screen.getAllByRole('button')
    fireEvent.click(buttons[buttons.length - 1])

    expect(onSelect).toHaveBeenCalled()
    const [, boundary] = onSelect.mock.calls[0]
    expect(boundary.after).toBeNull()
  })

  it('没给 onSelectGap 时静默条目不可点', () => {
    render(<TimelineView events={withGap} scanStatus="running" scanMarked />)
    const buttons = screen.getAllByRole('button')
    expect(buttons.length).toBeGreaterThan(0)
    for (const b of buttons) {
      expect((b as HTMLButtonElement).disabled).toBe(true)
    }
  })
})

describe('TimelineView 静默段的判定', () => {
  /*
    ★ 先记清一条容易搞错的语义（我在这里错过一次）：

    `buildTimeline` 的 `gaps` **只收达到门槛的间隔**（默认 2 分钟）。
    不够门槛的空白根本不进数组，所以"间隔"这个词只出现在
    「够门槛但扫描已结束」的情况 —— 不是"所有短空白都叫间隔"。

    于是三种组合各一条用例：不达门槛 / 达门槛且在跑 / 达门槛但已结束。
  */

  it('不达门槛的空白不算静默，也不进明细', () => {
    /*
      30 秒间隔，门槛 2 分钟 → `gaps` 为空。

      这条守的是"别把正常的工具调用间隙报成静默"。一次扫描里 30 秒的
      空档遍地都是（一个 nmap 就几十秒），全报出来等于没有报警。
    */
    render(
      <TimelineView
        events={[ev('a', 0), ev('b', 30_000)]}
        scanStatus="running"
        scanMarked
        onSelectGap={() => {}}
      />,
    )

    // 顶部结论行明说"没有超过 2 分钟"，而不是留空让人自己猜
    expect(screen.getByText(/没有超过/)).toBeTruthy()
    // 轴和明细里都不该出现静默/间隔条目
    expect(screen.queryByText(/^静默/)).toBeNull()
    expect(screen.queryByText(/^间隔/)).toBeNull()
  })

  it('扫描在跑时，达到门槛的空白是「静默」并按 risk 呈现', () => {
    render(
      <TimelineView
        events={[ev('a', 0), ev('b', 600_000)]}
        scanStatus="running"
        scanMarked
        onSelectGap={() => {}}
      />,
    )

    // 明细里那一条的标签是「静默 10 分钟」
    expect(screen.getByText(/^静默/)).toBeTruthy()
    // 顶部给出结论，并解释为什么可疑
    expect(screen.getByText(/没有产出|段静默/)).toBeTruthy()
    expect(screen.getByText(/而扫描当时在跑/)).toBeTruthy()
  })

  it('扫描已结束时，同样长的空白只说「间隔」，并说明安静是正常的', () => {
    /*
      ★ 这条区分的是「故障」与「正常收工」。

      一次扫描跑完 45 分钟后放一夜，事件流到最后一秒就停了 ——
      若把"现在"和轴的差距都算成静默，每次查看历史都会看到一条巨大的红带，
      于是这个视图就变成了"永远在报警"，用户很快学会无视它。

      `gapIsSuspicious` 只在 running 时为真，这里守的是它真的被用上了。
    */
    render(
      <TimelineView
        events={[ev('a', 0), ev('b', 600_000)]}
        scanStatus="completed"
        scanMarked
        now={T0 + 3_600_000}
        onSelectGap={() => {}}
      />,
    )

    // 够门槛，所以有明细条目 —— 但措辞必须换成「间隔」。
    // 用 getAllByText：轴上那条静默带的 aria-label 也叫「间隔 10 分」，
    // 与明细里的文字同词。两处都该是「间隔」，这正是要验的。
    expect(screen.getAllByText(/^间隔/).length).toBeGreaterThan(0)
    expect(screen.queryByText(/^静默/)).toBeNull()
    // 并且明说"已结束，之后的安静是正常的"
    expect(screen.getByText(/扫描已结束/)).toBeTruthy()
  })
})
