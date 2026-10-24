import { cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import EventStream from './EventStream'
import type { ActivityEvent } from '@/data/activity'

/*
  ══════════════════════════════════════════════════════════════
  事件流的「落点跳转」（时间线 → 事件流）
  ══════════════════════════════════════════════════════════════

  ## 这个文件守的几件事

    时间线上点一段静默，用户被送回事件流里那条边界事件上。这条路径
    有三个必须成立的点，而它们都不会自己报错 —— 失败了只是"没跳"或
    "高亮了错的行"，看起来像用户点错了：

      1. **跳了**（`scrollToIndex` 被调用，且用的是目标行的下标）；
      2. **高亮的是那一条**，不是别的（用 `highlighted` prop 传递）；
      3. **找不到目标时什么都不做**。目标可能被 agent 筛选掉了 ——
         这时跳到第 0 行比不跳更糟，因为用户会以为那就是"静默后的第一条"，
         而它其实是别的时间点的一条事件，完全合理、完全错误。

  ## 为什么 mock `useVirtualRows`

  真实 hook 需要 ResizeObserver + 真实布局，jsdom 里量出来的高度全是 0，
  `scrollToIndex` 会算不出东西。这里要验的是 **EventStream 有没有正确调用它**，
  不是虚拟滚动本身算得对不对（那有 `useVirtualRows.test.ts` 和
  `virtual.test.ts` 各自守着）。所以把 hook 换成可控的桩，只记录调用。

  ⚠️ 桩必须保留 hook 的**接口形状**（`attachContainer` / `measureRef` 身份恒定），
     否则 EventStream 里那个 `setContainer` 的 useCallback 会每次重建，
     组件塌掉 —— 那就不是在被测代码的问题上失败了。
*/

const scrollToIndex = vi.fn()

vi.mock('@/hooks/useVirtualRows', () => ({
  useVirtualRows: ({ count }: { count: number }) => ({
    attachContainer: () => {},
    startIndex: 0,
    endIndex: count,
    padTop: 0,
    padBottom: 0,
    totalHeight: 0,
    measureRef: () => {},
    scrollToIndex,
    firstVisibleIndex: 0,
  }),
}))

vi.mock('@/hooks/useAutoScroll', () => ({
  useAutoScroll: () => ({
    attachContainer: () => {},
    isAtBottom: true,
    scrollToLatest: () => {},
  }),
}))

function ev(id: string): ActivityEvent {
  return {
    id,
    scanId: 'scan_1',
    agentId: 'agent-01',
    type: 'tool_call',
    summary: `事件 ${id}`,
    detail: null,
    at: new Date(Date.UTC(2026, 9, 24, 12, 0, 0)).toISOString(),
  }
}

const EVENTS = [ev('a'), ev('b'), ev('c')]

function renderStream(focusEventId: string | null) {
  return render(
    <EventStream
      events={EVENTS}
      latestBatchIds={new Set()}
      filtered={false}
      isPaused={false}
      newWhilePaused={0}
      onPauseChange={() => {}}
      focusEventId={focusEventId}
    />,
  )
}

beforeEach(() => {
  scrollToIndex.mockClear()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('EventStream 落点跳转', () => {
  it('没有落点时一次都不跳', () => {
    renderStream(null)
    expect(scrollToIndex).not.toHaveBeenCalled()
  })

  it('给了落点就滚到那一行的下标，并居中', () => {
    renderStream('b')
    expect(scrollToIndex).toHaveBeenCalledTimes(1)
    // 'b' 在 EVENTS 里是下标 1 —— 断言具体下标，不是"被调用过"
    expect(scrollToIndex).toHaveBeenCalledWith(1, 'center')
  })

  it('落点是被筛掉的事件时**什么都不做**（不跳到第 0 行）', () => {
    /*
      ★ 这条是重点。跳错位置比不跳难发现得多：用户看到事件流动了、
        有一条被强调，于是相信那就是静默后的第一条。而它其实是别处的。

      所以断言两个方向：没调用，且没有行被高亮。
    */
    renderStream('does-not-exist')
    expect(scrollToIndex).not.toHaveBeenCalled()
    /*
      ⚠️ 断言用 `outline-accent`（EventRow 里高亮真正的类名），不用一个
         自己编的 data-*：编的那个属性页面上根本不存在，
         `querySelectorAll` 永远返回空 —— 这条断言就永远是绿的，等于没写。
    */
    const standout = Array.from(document.querySelectorAll('[data-virtual-key]')).filter((r) =>
      r.className.includes('outline-accent'),
    )
    expect(standout.length).toBe(0)
  })

  it('高亮的正是落点那一行，而不是别的行', () => {
    renderStream('c')
    /*
      行上用 `data-virtual-key` 带自己的身份（虚拟滚动靠它认行）。
      断言"被高亮的元素的 key 是 c"，而不是断言"有某个元素被高亮" ——
      后者在把比较写反（`!==`）时依然成立。
    */
    const rows = Array.from(document.querySelectorAll('[data-virtual-key]'))
    const standout = rows.filter((r) => r.className.includes('outline-accent'))
    expect(standout.length).toBe(1)
    expect(standout[0].getAttribute('data-virtual-key')).toBe('c')
  })
})
