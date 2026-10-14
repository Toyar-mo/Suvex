import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ErrorState from '@/components/ui/ErrorState'
import { ApiError } from '@/lib/api'

/*
  ══════════════════════════════════════════════════════════════
  ErrorState 的"连不上"与"后端说不行"两条分支（2026-10-14）
  ══════════════════════════════════════════════════════════════

  ## 为什么测这个

  组件自己的 docstring 写得很清楚：这两种失败的**下一步动作完全不同**——

    · `status === 0`（后端没起 / 断网 / 超时）→ 去启动服务，**重试没用**
    · `status !== 0`（后端答了 4xx/5xx）→ 看提示改输入，或稍后重试

  而开发态第一条极常见（忘了起后端）。如果两条走同一句"加载失败，
  请重试"，用户会一直点重试 —— 这是**文案导致的行为误导**，
  不会让任何测试变红，也不会让构建失败。

  `ApiError.isNetworkError` 这个 getter **存在的唯一理由**就是这条区分
  （见 `lib/api.ts` 里的注释）。所以这里钉住：
  它被真的用上了，而且用对了地方。

  ## 为什么测的是"渲染出什么文案"而不是内部状态

  用户看到的就是文案与图标。断言内部变量（比如 `isNetwork`）会让
  "变量算对了但渲染时用错了"漏过去 —— 而那正是要防的。

  ## ★★ 两个必须自己处理的坑（都实测踩过）

  ### ① 必须手动 `cleanup`

  `@testing-library/react` 的**自动** cleanup 挂在全局 `afterEach` 上，
  而它只有在 `globals: true` 时才注册（见 `vitest.config.ts` 里
  "不开 globals" 那段）。本项目刻意关了 globals —— 于是 DOM 会
  在用例之间**累积**，第二个用例开始 `getByRole('alert')` 就报
  `Found multiple elements`。

  实测症状（本文件第一版）：14 个用例里 13 个失败，报错全是
  "found multiple elements with the role alert" —— 看起来像组件
  渲染了多个 alert，其实是上一个用例的 DOM 还留在 document 里。

  ⚠️ 修法选了**不改全局配置**的那个（本文件 `afterEach` 里显式
     `cleanup()`）：另一个是往 vitest.config.ts 加 `setupFiles` 并开
     globals，那会影响**所有**现有测试文件的假设。一个测试文件的事，
     不该动全局。

  ### ② 不能用 `toHaveTextContent`

  那个匹配器来自 `@testing-library/jest-dom`，而本项目**没装它**
  （`node_modules/@testing-library/jest-dom` 不存在）。本次任务的
  红线是"不引入新测试依赖"，所以这里一律用原生 `textContent` +
  vitest 自带的 `toContain` / `not.toContain`。

  ⚠️ 这也意味着**本文件是第一个用 `@testing-library/react` 做渲染测试的**
     （此前 5 个测试文件都是纯逻辑/hook）。上面两条坑是它带来的新约束，
     后续再写组件测试时会同样遇到 —— 所以写在这里而不是只写在心里。
*/
afterEach(cleanup)

/** 取 alert 元素的文本。`getByRole` 在多个匹配时会抛错（正是我们要的） */
function alertText(): string {
  return screen.getByRole('alert').textContent ?? ''
}

describe('ErrorState：网络错误分支（status === 0）', () => {
  it('★ 标题是"连接不上后端服务"，不是笼统的"加载失败"', () => {
    render(
      <ErrorState
        error={new ApiError('NETWORK_ERROR', '连接不上后端服务（/api/v1）。请确认服务已启动。', 0)}
      />,
    )

    expect(alertText()).toContain('连接不上后端服务')
    expect(alertText()).not.toContain('加载失败')
  })

  it('★★ 后端地址出现在文案里（docstring 承诺"直接把地址写出来"）', () => {
    // 这是最快的排查路径：用户看到地址就知道该去起哪个服务。
    // 文案由 `lib/api.ts` 构造（它知道 API_BASE_URL），这里验它被渲染出来。
    const message = '连接不上后端服务（/api/v1）。请确认服务已启动。'
    render(<ErrorState error={new ApiError('NETWORK_ERROR', message, 0)} />)

    expect(alertText()).toContain('/api/v1')
  })

  it('★★ 网络错误**不显示**错误码（那一行只对"后端答了"有意义）', () => {
    render(<ErrorState error={new ApiError('NETWORK_ERROR', '连不上', 0)} />)

    expect(alertText()).not.toContain('NETWORK_ERROR')
  })

  it('★ 超时（REQUEST_ABORTED）也走网络分支 —— 它同样是 status 0', () => {
    render(<ErrorState error={new ApiError('REQUEST_ABORTED', '请求超时或被取消，请重试。', 0)} />)

    // 标题仍是"连接不上"：超时在后端没应答这个意义上与断网同类。
    expect(alertText()).toContain('连接不上后端服务')
  })
})

describe('ErrorState：后端应答了错误（status !== 0）', () => {
  it('★ 标题是"加载失败"，且**显示后端那句话**', () => {
    render(<ErrorState error={new ApiError('VALIDATION_FAILED', '目标地址格式不正确。', 422)} />)

    expect(alertText()).toContain('加载失败')
    expect(alertText()).toContain('目标地址格式不正确。')
  })

  it('★★ 显示错误码（报障时能原样念出来，且契约冻结可检索）', () => {
    render(<ErrorState error={new ApiError('VALIDATION_FAILED', '格式不对', 422)} />)

    expect(alertText()).toContain('VALIDATION_FAILED')
  })

  it('★ 500 也走"后端应答"分支（不是网络错误）', () => {
    render(<ErrorState error={new ApiError('INTERNAL_ERROR', '服务器内部错误。', 500)} />)

    expect(alertText()).toContain('加载失败')
    expect(alertText()).toContain('INTERNAL_ERROR')
    expect(alertText()).not.toContain('连接不上后端服务')
  })
})

describe('ErrorState：重试按钮', () => {
  it('★ 传了 onRetry 才渲染按钮，且点击会调用它', () => {
    const onRetry = vi.fn()
    render(<ErrorState error={new Error('x')} onRetry={onRetry} />)

    screen.getByRole('button', { name: /重试/ }).click()

    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('★★ 不传 onRetry 就**没有**按钮（没有重试路径时给按钮是骗人的）', () => {
    render(<ErrorState error={new Error('x')} />)

    expect(screen.queryByRole('button')).toBeNull()
  })

  it('★ 网络错误时按钮照样渲染（是否该点由调用方决定，组件不擅自隐藏）', () => {
    // 组件不替调用方判断"网络错误重试有没有用" —— 那取决于调用方
    // 是否有别的恢复路径。组件只按 onRetry 是否存在决定。
    render(<ErrorState error={new ApiError('NETWORK_ERROR', '连不上', 0)} onRetry={() => {}} />)

    expect(screen.queryByRole('button', { name: /重试/ })).not.toBeNull()
  })
})

describe('ErrorState：兜底与无障碍', () => {
  it('★ 非 Error 对象给兜底文案（不显示 undefined）', () => {
    render(<ErrorState error={{ weird: true }} />)

    expect(alertText()).toContain('发生了未知错误，请稍后重试。')
  })

  it('★ 空 message 的 Error 也走兜底（不渲染一个空白段落）', () => {
    render(<ErrorState error={new Error('')} />)

    expect(alertText()).toContain('发生了未知错误，请稍后重试。')
  })

  it('★★ role="alert"（失败要立刻被告知，不能等读屏器空闲）', () => {
    render(<ErrorState error={new Error('x')} />)

    // `status` 是礼貌的旁白，`alert` 才是"现在就要说"。
    // `getByRole('alert')` 本身在找不到时会抛错，所以这一行就是断言。
    expect(screen.getByRole('alert')).toBeTruthy()
  })

  it('★ title 传了就用它覆盖默认标题', () => {
    render(<ErrorState error={new Error('x')} title="漏洞列表加载失败" />)

    expect(alertText()).toContain('漏洞列表加载失败')
    // ⚠️ 这里**不能**断言 `not.toContain('加载失败')` —— 自定义标题
    //    "漏洞列表加载失败"本身就含这三个字，那样写必然失败。
    //    要验"默认标题没被同时渲染"，只能断言默认那句的完整形态。
    expect(alertText()).not.toContain('连接不上后端服务')
  })
})
