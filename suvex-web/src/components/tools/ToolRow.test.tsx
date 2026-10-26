import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ToolRow, type ToolRowProps } from '@/components/tools/ToolRow'

/**
 * ## 这一组钉的是「写了但看不见」
 *
 * 后端的 `ToolSpec.default_limitation` 从 v0.86 就在写，10 个工具里 6 个填了
 * （`call_mcp` 那条直接讲「命中 0 可能是没查这一层」），`GET /kernel/tools`
 * 一直在返回它 —— 而这一行**从来没渲染过**。前端类型里甚至没有这个字段，
 * JSON 到了浏览器就被丢掉。
 *
 * 这类失真的后果不是"少一行字"：用户看到 `call_mcp` 回「命中 0」，
 * 只能自己判断是目标没有还是工具没查这一层 —— 而那正是这个字段
 * 当初被写下来的原因。
 *
 * ## 与 `reason` 的区别是这一条的重点
 *
 * | 字段 | 含义 | 用户该做什么 |
 * | --- | --- | --- |
 * | `reason` | 这个工具现在**用不了** | 去修（打开开关 / 配连接 / 改白名单） |
 * | `defaultLimitation` | 它能用，但**默认会漏东西** | 不用担心它坏，但要加参数 |
 *
 * 两者合成一个红色，6 个本来正常的工具会看起来都坏了，
 * 用户会去修一个根本不需要修的东西。所以下面专门有一条测它们的视觉区分。
 */

function props(over: Partial<ToolRowProps> = {}): ToolRowProps {
  return {
    name: 'run_shell',
    label: '执行命令',
    summary: '在沙箱里跑一条命令',
    params: 'command',
    health: 'ready',
    core: true,
    requiresConnection: '',
    reason: '',
    enabled: true,
    onToggle: () => {},
    ...over,
  }
}

describe('ToolRow：默认会漏什么', () => {
  it('★ 有局限时必须显示出来', () => {
    render(
      <ToolRow
        {...props({
          name: 'call_mcp',
          label: '调用 MCP 工具',
          defaultLimitation: '返回「命中 0」时先分清是目标确实没有，还是它没查这一层。',
        })}
      />,
    )
    expect(screen.getByText(/默认会漏/)).toBeTruthy()
    expect(screen.getByText(/没查这一层/)).toBeTruthy()
  })

  it('没有局限时不渲染那一行（空串不是"没有局限"的反面口子）', () => {
    const { container } = render(<ToolRow {...props({ defaultLimitation: '' })} />)
    expect(container.textContent).not.toContain('默认会漏')
  })

  it('缺省（不传这个 prop）时不崩、也不渲染', () => {
    // 兼容旧调用点：这个 prop 是 v0.99 新增的，可选
    const { container } = render(<ToolRow {...props()} />)
    expect(container.textContent).not.toContain('默认会漏')
  })

  it('★ 局限与"未就绪原因"必须用不同颜色（一个是要去修，一个只是使用须知）', () => {
    const { container } = render(
      <ToolRow
        {...props({
          health: 'unconfigured',
          reason: '依赖的连接「vulndb」还没配',
          requiresConnection: 'vulndb',
          defaultLimitation: '只报工具名与 schema，不验证能不能跑通。',
        })}
      />,
    )
    const limitation = Array.from(container.querySelectorAll('p')).find((el) =>
      el.textContent?.includes('默认会漏'),
    )
    const reason = Array.from(container.querySelectorAll('p')).find((el) =>
      el.textContent?.includes('还没配'),
    )
    expect(limitation).toBeTruthy()
    expect(reason).toBeTruthy()
    // 局限用 warn 色；reason 用该健康状态的色（unconfigured = warn 也是 warn，
    // 所以这里换 misconfigured 才能看出区分）
    expect(limitation!.className).toContain('signal-warn')
  })

  it('★ misconfigured 时，局限（warn）与原因（risk）不同色', () => {
    const { container } = render(
      <ToolRow
        {...props({
          health: 'misconfigured',
          reason: '白名单挡住了这条连接',
          defaultLimitation: '只扫约一千个常用端口。',
        })}
      />,
    )
    const limitation = Array.from(container.querySelectorAll('p')).find((el) =>
      el.textContent?.includes('默认会漏'),
    )
    const reason = Array.from(container.querySelectorAll('p')).find((el) =>
      el.textContent?.includes('白名单'),
    )
    expect(limitation!.className).toContain('signal-warn')
    expect(reason!.className).toContain('signal-risk')
  })
})

describe('ToolRow：既有契约不能被这次改动弄坏', () => {
  it('仍显示工具名、中文名、用途、参数', () => {
    render(<ToolRow {...props({ defaultLimitation: '默认只扫一部分端口。' })} />)
    expect(screen.getByText('run_shell')).toBeTruthy()
    expect(screen.getByText('执行命令')).toBeTruthy()
    expect(screen.getByText('在沙箱里跑一条命令')).toBeTruthy()
    expect(screen.getByText(/command/)).toBeTruthy()
  })

  it('主力工具仍有「主力」标记', () => {
    render(<ToolRow {...props()} />)
    expect(screen.getByText('主力')).toBeTruthy()
  })

  it('开关反映 enabled', () => {
    render(<ToolRow {...props({ enabled: false })} />)
    const box = screen.getByRole('checkbox') as HTMLInputElement
    expect(box.checked).toBe(false)
  })
})
