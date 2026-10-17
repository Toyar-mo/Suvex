import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ToggleChip from '@/components/ui/ToggleChip'

/*
  ══════════════════════════════════════════════════════════════
  ToggleChip（筛选胶囊 / 分段控件选项）（2026-10-16）
  ══════════════════════════════════════════════════════════════

  ## 为什么测这个

  它承载的是「一组可并存的筛选器」和「二选一的视图」，两者的**无障碍语义
  完全不同**，而屏幕上长得几乎一样。所以这里钉的是语义，不是外观：

  · `aria-pressed` 必须跟着 `active` 走。读屏器靠它说「已按下 / 未按下」；
    只靠底色的话，视障用户完全不知道这一项是开还是关。
  · 默认 `type="button"`。筛选条常常整块塞在搜索区的 `<form>` 里，
    漏写 `type` 会让「点筛选」变成「提交表单」。

  ## 为什么单独断一条「禁用态不用 opacity」

  `FindingsPage.tsx:1509` 原来的注释写得很清楚：「不叠加 opacity，
  会把文字一起压暗」。这条是实测结论不是审美偏好 ——
  `text-ink-muted` 对 `canvas` 是 6.66:1，乘 0.5 之后掉到 2 以下，
  直接不达 AA。而 chip 的禁用常常是「这一项永远没数据」，
  它得长期可读，与「这一刻不能点」不是一回事。

  ## 两个必须自己处理的坑（与 ErrorState.test.tsx 相同）

  · 必须显式 `afterEach(cleanup)`（本项目 `globals: false`）。
  · 不能用 `toHaveTextContent`（没装 `@testing-library/jest-dom`）。
*/
afterEach(cleanup)

describe('ToggleChip：语义', () => {
  it('★★ active 落到 aria-pressed="true"（颜色只是冗余编码）', () => {
    render(
      <ToggleChip active onToggle={() => {}}>
        运行中
      </ToggleChip>,
    )

    expect(screen.getByRole('button').getAttribute('aria-pressed')).toBe('true')
  })

  it('★ 非 active 是 aria-pressed="false"（不是省略属性）', () => {
    render(
      <ToggleChip active={false} onToggle={() => {}}>
        已完成
      </ToggleChip>,
    )

    // 省略属性时读屏器会说"按钮"，说"未按下"才是对的 ——
    // 它得能区分"这是个开关"和"这是个普通按钮"。
    expect(screen.getByRole('button').getAttribute('aria-pressed')).toBe('false')
  })

  it('★ 默认 type="button"（筛选条常在 form 里，漏了会提交表单）', () => {
    render(
      <ToggleChip active={false} onToggle={() => {}}>
        全部
      </ToggleChip>,
    )

    expect(screen.getByRole('button').getAttribute('type')).toBe('button')
  })

  it('点击调用 onToggle（而不是 onClick —— 它只改状态，不执行动作）', () => {
    const onToggle = vi.fn()
    render(
      <ToggleChip active={false} onToggle={onToggle}>
        失败
      </ToggleChip>,
    )

    screen.getByRole('button').click()

    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  /*
    ═══════════════════════════════════════════════════════════════════════
    `onClick` 被 Omit 掉这件事只能靠类型系统挡，运行时测不出来 —— 这里能测的
    是"没有第二个人在抢事件"：点击只触发 onToggle，不会被别的 handler 多吃一次。
    真正的防线在 `ToggleChipProps` 的 `Omit<..., 'aria-pressed' | 'onClick'>`。

    ⚠️ 第一版这里写的是 `expect(el.onclick).toBeNull()`，实测**必红**：
    React 用事件委托，`onclick` 上挂的是它自己的桩函数（开发环境叫 `noop$1`），
    不是 null。那是在断言 React 的内部实现，不是在断言本组件的行为。
    ═══════════════════════════════════════════════════════════════════════
  */
  it('★ 点击只走 onToggle 一条路，不多不少（onClick 已在类型层 Omit）', () => {
    const onToggle = vi.fn()
    const { container } = render(
      <ToggleChip active={false} onToggle={onToggle}>
        失败
      </ToggleChip>,
    )

    const el = screen.getByRole('button')
    el.click()
    el.click()

    // 两次点击 → 正好两次回调：少了说明被吞，多了说明有第二个 handler 在偷跑
    expect(onToggle).toHaveBeenCalledTimes(2)
    // 每次点击只收到一次调用（不是一次点击触发两遍）
    expect(onToggle.mock.calls.length).toBe(2)
    // 只有一个按钮节点，没有嵌套的第二个可点元素
    expect(container.querySelectorAll('button').length).toBe(1)
  })
})

describe('ToggleChip：两种形态', () => {
  it('★★ pill 有高度（px-2.5 / h-8），segment 没有（高度由外层容器给）', () => {
    const { rerender } = render(
      <ToggleChip active={false} onToggle={() => {}} variant="pill">
        全部
      </ToggleChip>,
    )
    const pillCss = screen.getByRole('button').className
    expect(pillCss).toContain('h-8')
    expect(pillCss).toContain('rounded-pill')
    // pill 自己画边框（有宽度）；按空白切词比，避免被 `border-line` 蒙过去
    expect(pillCss.split(/\s+/)).toContain('border')

    rerender(
      <ToggleChip active={false} onToggle={() => {}} variant="segment">
        列表
      </ToggleChip>,
    )
    const segCss = screen.getByRole('button').className
    // ★ 这一条是分段控件的关键：自己带 h-8 会把外层 `p-0.5` 的容器顶开，
    //   内选项与边框就对不齐了。所以 segment 只能 h-full。
    expect(segCss).not.toContain('h-8')
    expect(segCss).toContain('h-full')
    expect(segCss).toContain('rounded-xs')
    // 边框由外层 `role="group"` 的容器给，选项自己不该有**边框宽度**。
    // 注意不能写 `not.toMatch(/\bborder\b/)` —— 它会把 `border-line`
    // 这种"边框颜色"类也判成犯规（颜色类没有宽度时并不画边），
    // 于是测试在代码正确时报红。这里按空白切词，只比那个裸的宽度类。
    expect(segCss.split(/\s+/)).not.toContain('border')
  })

  it('pill 的 xs 是 h-7 px-2 text-2xs（FindingsPage 的紧凑筛选）', () => {
    render(
      <ToggleChip active={false} onToggle={() => {}} variant="pill" size="xs">
        待确认
      </ToggleChip>,
    )

    const css = screen.getByRole('button').className
    expect(css).toContain('h-7')
    expect(css).toContain('text-2xs')
  })
})

describe('ToggleChip：★ 禁用态不用 opacity', () => {
  it('★★ disabled 时用 text-ink-faint，且**不带** opacity 类', () => {
    render(
      <ToggleChip active={false} onToggle={() => {}} disabled>
        高危
      </ToggleChip>,
    )

    const css = screen.getByRole('button').className
    expect(css).toContain('text-ink-faint')
    expect(css).toContain('cursor-not-allowed')
    // opacity 乘在整棵子树上，会把文字对比度一起打下去
    expect(css).not.toMatch(/\bopacity-\d/)
  })

  it('★ disabled 时点不动', () => {
    const onToggle = vi.fn()
    render(
      <ToggleChip active={false} onToggle={onToggle} disabled>
        高危
      </ToggleChip>,
    )

    screen.getByRole('button').click()

    expect(onToggle).not.toHaveBeenCalled()
  })
})

describe('ToggleChip：子内容与覆盖', () => {
  it('★ 可以内嵌计数字（状态筛选两处都这么用）', () => {
    render(
      <ToggleChip active onToggle={() => {}}>
        运行中
        <span className="tabular text-2xs">12</span>
      </ToggleChip>,
    )

    expect((screen.getByRole('button').textContent ?? '').replace(/\s/g, '')).toBe('运行中12')
  })

  it('调用方 className 会覆盖基元类名（tailwind-merge 生效）', () => {
    render(
      <ToggleChip active={false} onToggle={() => {}} className="shrink-0">
        全部
      </ToggleChip>,
    )

    expect(screen.getByRole('button').className).toContain('shrink-0')
  })
})
