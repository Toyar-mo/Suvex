import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createRef } from 'react'

import { Button, IconButton } from '@/components/ui/Button'

/*
  ══════════════════════════════════════════════════════════════
  Button / IconButton 基元（2026-10-16）
  ══════════════════════════════════════════════════════════════

  ## 为什么测这个

  这个文件存在的唯一理由，是钉住三件**不会让任何检查变红**的事：

  1. `type` 默认必须是 `button`。HTML 的默认值是 `submit` ——
     任何包在 `<form>` 里的按钮，漏写 `type` 就会**提交表单**。
     而"提交了两次"在界面上看不出来，只会在网络面板里多一条请求。
     收编 33 处裸 button 时，这正是最容易整体引入的回归。

  2. `loading` 时必须做**同步**防重复提交。`disabled` 要等 re-render
     才生效，挡不住同一帧里的第二次点击。第 ④ 组用例就是那个对照实验：
     传 `loading` 的双击只触发 1 次，不传的双击触发 2 次 ——
     两条一起断言，才能证明"1 次"不是测试环境碰巧造成的。

  3. `ref` 必须能接到真实 DOM 节点。`ConfirmDialog` 靠
     `cancelRef.current?.focus()` 做初始焦点，接不到就等于弹窗
     打开后焦点留在后面的页面上 —— 键盘用户 Tab 着就跑了，
     而**屏幕上完全看不出来**。

  ## 为什么断言 class 而不是视觉

  项目没有截图回归。变体类名是设计系统的**唯一载体**（`bg-accent text-canvas`
  这个组合是 §7 实测出来的，换成别的写法对比度就掉到 AA 以下），
  所以这里直接钉字符串片段。它测不出"好不好看"，但能测出
  "有人把 `text-canvas` 改成了 `text-white`"。

  ## 两个必须自己处理的坑（与 ErrorState.test.tsx 相同）

  · 必须显式 `afterEach(cleanup)` —— 本项目 `globals: false`，
    testing-library 的自动 cleanup 不注册，DOM 会跨用例累积。
  · 不能用 `toHaveTextContent` —— 没装 `@testing-library/jest-dom`。
*/
afterEach(cleanup)

/** 直接读 DOM 的 className，用于断言变体类名确实落地 */
function classes(el: HTMLElement): string {
  return el.className
}

describe('Button：type 与基础渲染', () => {
  it('★ 默认 type="button"（漏了这个就会提交表单）', () => {
    render(<Button>保存</Button>)

    // 断言的是**渲染出来的属性**，不是组件的默认参数 ——
    // 参数写对了但没传给 DOM 的情况必须能被抓到。
    expect(screen.getByRole('button').getAttribute('type')).toBe('button')
  })

  it('★ 显式传 type="submit" 时尊重调用方（登录/初始化两处需要）', () => {
    render(<Button type="submit">登录</Button>)

    expect(screen.getByRole('button').getAttribute('type')).toBe('submit')
  })

  it('点击会调用 onClick', () => {
    const onClick = vi.fn()
    render(<Button onClick={onClick}>保存</Button>)

    screen.getByRole('button').click()

    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('disabled 时点不动（连 onClick 都不该跑）', () => {
    const onClick = vi.fn()
    render(
      <Button disabled onClick={onClick}>
        保存
      </Button>,
    )

    screen.getByRole('button').click()

    expect(onClick).not.toHaveBeenCalled()
  })
})

describe('Button：loading 契约', () => {
  it('★★ loading 强制 disabled + aria-busy + 转圈图标', () => {
    render(
      <Button loading loadingLabel="正在登录…">
        登录
      </Button>,
    )

    const el = screen.getByRole('button')
    expect(el.hasAttribute('disabled')).toBe(true)
    expect(el.getAttribute('aria-busy')).toBe('true')
    // 转圈图标是 aria-hidden 的装饰：读屏器应该听到"正在登录…"，不是"图片"
    expect(el.querySelector('svg[aria-hidden="true"]')).toBeTruthy()
  })

  it('★★ loading 时文字换成 loadingLabel（用户要知道在等什么）', () => {
    render(
      <Button loading loadingLabel="正在登录…">
        登录
      </Button>,
    )

    // ⚠️ 不能写成 `not.toContain('登录')` —— "正在登录…"本身含"登录"，
    //    那样写必然误报。只能钉**完整文本**，才能验出"两句并存"的情况。
    expect((screen.getByRole('button').textContent ?? '').trim()).toBe('正在登录…')
  })

  it('loading 但不传 loadingLabel 时沿用 children（不渲染空白按钮）', () => {
    render(<Button loading>保存</Button>)

    expect((screen.getByRole('button').textContent ?? '').trim()).toBe('保存')
  })

  it('非 loading 时没有 aria-busy（false 不该写成字符串 "false"）', () => {
    render(<Button>保存</Button>)

    expect(screen.getByRole('button').hasAttribute('aria-busy')).toBe(false)
  })
})

/*
  ═══════════════════════════════════════════════════════════════════════
  busy 档：2026-10-17 补。
  起因是 Review 指出 `ReportsPage` 的「重新下载」被压平了 —— 文案说
  「正在生成…」（等一等），光标却说 `cursor-not-allowed`（不许点）。
  这一组用例把两档的差异钉死在类名上，以后谁想再压平会先红一次。
  ═══════════════════════════════════════════════════════════════════════
*/
describe('Button：★ busy 档（"等一等"不是"不许点"）', () => {
  it('★★ busy 时不可点 + aria-busy（和 loading 一样挡住重复提交）', () => {
    const onClick = vi.fn()
    render(
      <Button busy onClick={onClick}>
        重新下载
      </Button>,
    )

    const el = screen.getByRole('button')
    expect(el.hasAttribute('disabled')).toBe(true)
    expect(el.getAttribute('aria-busy')).toBe('true')

    el.click()
    expect(onClick).not.toHaveBeenCalled()
  })

  it('★★ busy 用 cursor-wait 且**不**压暗（压暗是在说"出问题了"）', () => {
    render(<Button busy>重新下载</Button>)

    // ⚠️ 按整词断言，不用 `toContain` —— `disabled:cursor-not-allowed`
    //    里也含 "cursor" 字样，子串匹配会给出假绿灯。
    const tokens = (screen.getByRole('button').className ?? '').split(/\s+/)
    expect(tokens).toContain('disabled:cursor-wait')
    expect(tokens).not.toContain('disabled:cursor-not-allowed')
    // twMerge 让后写的 disabled:opacity-100 顶掉变体自带的 disabled:opacity-50
    expect(tokens).toContain('disabled:opacity-100')
    expect(tokens).not.toContain('disabled:opacity-50')
  })

  it('★★ 对照：普通 disabled 仍然是 not-allowed + 压暗（两档真的不同）', () => {
    render(<Button disabled>重新下载</Button>)

    const tokens = (screen.getByRole('button').className ?? '').split(/\s+/)
    expect(tokens).toContain('disabled:cursor-not-allowed')
    expect(tokens).toContain('disabled:opacity-50')
    expect(tokens).not.toContain('disabled:cursor-wait')
  })

  it('busy 与 loading 可以同时给（转圈由 loading 管，光标由 busy 管）', () => {
    render(
      <Button loading busy loadingLabel="正在生成…">
        生成并导出
      </Button>,
    )

    const el = screen.getByRole('button')
    expect(el.querySelector('svg[aria-hidden="true"]')).toBeTruthy()
    expect(el.getAttribute('aria-busy')).toBe('true')
    expect((el.className ?? '').split(/\s+/)).toContain('disabled:cursor-wait')
  })
})

describe('Button：★ 同步防重复提交（pendingRef 守卫）', () => {
  it('★★ 传了 loading 的按钮，同一帧内连点两次只触发一次', () => {
    const onClick = vi.fn()
    // loading 传常量 false 复现真实场景：handler 里 `await` 之后才 setPending，
    // 而这中间按钮还没被 disabled —— 守卫要挡的正是这一帧。
    render(
      <Button loading={false} onClick={onClick}>
        提交
      </Button>,
    )

    const el = screen.getByRole('button')
    el.click()
    el.click()

    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('★★ 对照：不传 loading 的按钮，连点两次就是两次（证明上一条的 1 不是环境造成的）', () => {
    const onClick = vi.fn()
    render(<Button onClick={onClick}>提交</Button>)

    const el = screen.getByRole('button')
    el.click()
    el.click()

    expect(onClick).toHaveBeenCalledTimes(2)
  })

  it('★★ loading 由 true 回到 false 后按钮恢复可点（守卫会解锁，不是锁死）', () => {
    const onClick = vi.fn()
    const { rerender } = render(
      <Button loading onClick={onClick}>
        提交
      </Button>,
    )

    // 加载中：disabled，点不动
    screen.getByRole('button').click()
    expect(onClick).not.toHaveBeenCalled()

    // 请求结束
    rerender(
      <Button loading={false} onClick={onClick}>
        提交
      </Button>,
    )

    screen.getByRole('button').click()
    expect(onClick).toHaveBeenCalledTimes(1)
  })
})

describe('Button：ref 与无障碍透传', () => {
  it('★★ ref 接到真实 DOM 节点（ConfirmDialog 的初始焦点靠它）', () => {
    const ref = createRef<HTMLButtonElement>()
    render(<Button ref={ref}>取消</Button>)

    // 断言的是"拿到的确实是那个按钮"，不只是"不为 null"
    expect(ref.current).toBe(screen.getByRole('button'))
  })

  it('★ aria-pressed / role / aria-selected 原样透传（ReportsPage 的 radio/tab 靠它）', () => {
    render(
      <Button role="radio" aria-checked="true">
        导出格式
      </Button>,
    )

    const el = screen.getByRole('radio')
    expect(el.getAttribute('aria-checked')).toBe('true')
  })

  it('调用方传的 className 会覆盖基元类名（tailwind-merge 生效）', () => {
    render(<Button className="ml-0.5 flex-1">下载</Button>)

    const css = classes(screen.getByRole('button'))
    expect(css).toContain('ml-0.5')
    expect(css).toContain('flex-1')
  })
})

describe('Button：变体类名（设计系统的唯一载体）', () => {
  it('★★ primary 是 bg-accent + text-canvas（§7 实测 5.98:1 的唯一组合）', () => {
    render(<Button variant="primary">发起扫描</Button>)

    const css = classes(screen.getByRole('button'))
    expect(css).toContain('bg-accent')
    expect(css).toContain('text-canvas')
    expect(css).toContain('hover:bg-accent-strong')
    // 裸白字是被禁的：亮色下 text-white 落在 accent 上不足以说明问题，
    // 真正的问题是它绕过了 token（换主题时不会跟着变）。
    expect(css).not.toContain('text-white')
  })

  it('★ link 族不带高度与内距（行内链接不该把句子撑开）', () => {
    render(<Button variant="link">清除筛选条件</Button>)

    const css = classes(screen.getByRole('button'))
    expect(css).toContain('underline')
    // 任何 h-7/h-8/h-9/h-10 与 px-2/px-3 都不该出现
    expect(css).not.toMatch(/\bh-\d/)
    expect(css).not.toMatch(/\bpx-\d/)
  })

  it('★ danger 用 signal-risk 实心（确认框里的删除）', () => {
    render(<Button variant="danger">删除</Button>)

    const css = classes(screen.getByRole('button'))
    expect(css).toContain('bg-signal-risk/90')
    expect(css).toContain('hover:bg-signal-risk')
  })

  it('尺寸 sm 是 h-8 px-3 text-xs', () => {
    render(<Button size="sm">拉取</Button>)

    const css = classes(screen.getByRole('button'))
    expect(css).toContain('h-8')
    expect(css).toContain('px-3')
    expect(css).toContain('text-xs')
  })
})

describe('IconButton', () => {
  it('★★ aria-label 落到 DOM（图标对读屏器是一片空白）', () => {
    render(
      <IconButton label="关闭提示">
        <span aria-hidden="true">×</span>
      </IconButton>,
    )

    expect(screen.getByRole('button').getAttribute('aria-label')).toBe('关闭提示')
  })

  it('★ 默认 type="button"', () => {
    render(<IconButton label="关闭">×</IconButton>)

    expect(screen.getByRole('button').getAttribute('type')).toBe('button')
  })

  it('xs 是 24×24（触摸目标下限），sm 是 32×32', () => {
    const { rerender } = render(<IconButton label="关闭">×</IconButton>)
    expect(classes(screen.getByRole('button'))).toContain('h-6')

    rerender(
      <IconButton label="关闭" size="sm">
        ×
      </IconButton>,
    )
    expect(classes(screen.getByRole('button'))).toContain('h-8')
  })

  it('点击会调用 onClick', () => {
    const onClick = vi.fn()
    render(
      <IconButton label="关闭" onClick={onClick}>
        ×
      </IconButton>,
    )

    screen.getByRole('button').click()

    expect(onClick).toHaveBeenCalledTimes(1)
  })
})
