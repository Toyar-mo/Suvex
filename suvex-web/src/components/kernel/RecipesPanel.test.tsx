import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { RecipesPanel } from '@/components/kernel/RecipesPanel'
import type { KernelRecipe } from '@/data/tools'

/**
 * ## 这一页的风险形状
 *
 * 它不是"表格画错一行"那么显眼 —— 配方错位的后果是**模型照着错的参数去跑**，
 * 而那不会报错，只会少命中，然后被当成"目标没这个漏洞"。
 *
 * 所以这里钉三件具体的事：
 *
 * ① **局限说明必须可见，且不能被折叠吃掉。**
 *    它答的是"用默认参数跑会漏什么"，而漏掉**不报错**（b181 有实测：
 *    同一目标同一工具只补一个参数就从 0 命中变 2 命中）。
 *    如果它只在展开后出现，就等于承认它"可以不看" —— 那么搜索里
 *    也应该能搜到它，否则有人记得"有个工具默认只扫一千个端口"
 *    却找不到是哪一条。
 *
 * ② **没有开关。** 这一页画的是镜像里的二进制，不是引擎动作。
 *    画一个开关出来会造出假的权力：用户以为关掉 nmap 就调不动了，
 *    而 `run_shell nmap ...` 照样能跑。
 *
 * ③ **退出码说明只给"多码"的工具。** `ffuf` 没命中时退出码是 1，
 *    若 UI 暗示非 0 即失败，用户会去修一个本来正常工作的工具。
 */

function recipe(over: Partial<KernelRecipe> = {}): KernelRecipe {
  return {
    name: 'nmap',
    summary: '端口与服务识别',
    group: '侦察',
    command: 'nmap',
    args: ['-sT', '-sV'],
    params: [],
    defaultLimitation: '',
    allowedExitCodes: [0],
    ...over,
  }
}

describe('RecipesPanel 渲染', () => {
  it('名字与一句话说明始终可见，不用展开', () => {
    render(<RecipesPanel recipes={[recipe()]} />)

    expect(screen.getByText('nmap')).toBeTruthy()
    expect(screen.getByText('端口与服务识别')).toBeTruthy()
  })

  it('统计"几条写了局限"——这是这个面板存在的理由', () => {
    render(
      <RecipesPanel
        recipes={[
          recipe({ name: 'nmap', defaultLimitation: '默认只扫 1000 端口' }),
          recipe({ name: 'sqlmap' }),
        ]}
      />,
    )

    expect(screen.getByText('1 / 2 条写了局限')).toBeTruthy()
  })

  it('有局限的工具在**折叠状态**就带标记，不必展开才发现', () => {
    render(<RecipesPanel recipes={[recipe({ defaultLimitation: '默认只扫 1000 端口' })]} />)

    // 「默认会漏」徽标在折叠态就该在 —— 否则用户要点开 34 行才知道哪些有坑
    expect(screen.getByTitle('用默认参数跑会漏掉东西')).toBeTruthy()
  })

  it('命令名与配方名不同时把真正执行的命令显示出来（exec → sh）', () => {
    render(<RecipesPanel recipes={[recipe({ name: 'exec', command: 'sh' })]} />)

    expect(screen.getByText('→ sh')).toBeTruthy()
  })
})

describe('RecipesPanel 展开', () => {
  it('展开后才显示参数、局限正文与默认参数', () => {
    render(
      <RecipesPanel
        recipes={[
          recipe({
            name: 'nmap',
            args: ['-sT'],
            params: [
              {
                name: 'ports',
                format: 'flag',
                description: '端口范围',
                required: false,
                flag: '-p',
                default: '',
              },
            ],
            defaultLimitation: '默认只扫 1000 端口',
          }),
        ]}
      />,
    )

    // 折叠时：局限正文不该在（正文是一段解释，不是徽标）
    expect(screen.queryByText('默认参数下做不到什么')).toBeNull()

    fireEvent.click(screen.getByText('nmap'))

    expect(screen.getByText('默认参数下做不到什么')).toBeTruthy()
    expect(screen.getByText('默认只扫 1000 端口')).toBeTruthy()
    expect(screen.getByText('ports')).toBeTruthy()
    expect(screen.getByText('-p')).toBeTruthy()
    expect(screen.getByText('默认就带：')).toBeTruthy()
  })

  it('再点一次折叠回去', () => {
    render(<RecipesPanel recipes={[recipe({ defaultLimitation: '默认只扫 1000 端口' })]} />)

    fireEvent.click(screen.getByText('nmap'))
    expect(screen.getByText('默认参数下做不到什么')).toBeTruthy()

    fireEvent.click(screen.getByText('nmap'))
    expect(screen.queryByText('默认参数下做不到什么')).toBeNull()
  })

  it('退出码说明只在"多码都不算失败"时出现', () => {
    const { rerender } = render(
      <RecipesPanel
        recipes={[recipe({ name: 'nmap', args: [], defaultLimitation: 'x', allowedExitCodes: [0] })]}
      />,
    )
    fireEvent.click(screen.getByText('nmap'))
    expect(screen.queryByText(/都不算失败/)).toBeNull()

    rerender(
      <RecipesPanel
        recipes={[
          recipe({ name: 'ffuf', args: [], defaultLimitation: 'x', allowedExitCodes: [0, 1] }),
        ]}
      />,
    )
    fireEvent.click(screen.getByText('ffuf'))
    // ★ ffuf 没命中时退出码 1；这里必须说清"1 也不算失败"，
    //   否则用户会去修一个本来正常工作的工具。
    expect(screen.getByText(/都不算失败/)).toBeTruthy()
  })
})

describe('RecipesPanel 筛选', () => {
  const recipes = [
    recipe({ name: 'nmap', summary: '端口与服务识别', defaultLimitation: '默认只扫 1000 端口' }),
    recipe({ name: 'sqlmap', summary: 'SQL 注入验证' }),
    recipe({ name: 'ffuf', summary: '目录与参数爆破', defaultLimitation: '默认不带递归' }),
  ]

  it('搜名字', () => {
    render(<RecipesPanel recipes={recipes} />)
    fireEvent.change(screen.getByPlaceholderText('搜索工具（名字 / 用途 / 局限）'), {
      target: { value: 'sql' },
    })

    expect(screen.getByText('sqlmap')).toBeTruthy()
    expect(screen.queryByText('nmap')).toBeNull()
    expect(screen.getByText('1 / 3')).toBeTruthy()
  })

  it('★ 搜局限正文 —— 有人记得"默认只扫一千个端口"却不记得它叫 nmap', () => {
    render(<RecipesPanel recipes={recipes} />)
    fireEvent.change(screen.getByPlaceholderText('搜索工具（名字 / 用途 / 局限）'), {
      target: { value: '1000' },
    })

    expect(screen.getByText('nmap')).toBeTruthy()
    expect(screen.queryByText('sqlmap')).toBeNull()
  })

  it('只看有局限的', () => {
    render(<RecipesPanel recipes={recipes} />)
    fireEvent.click(screen.getByText('只看有局限的'))

    expect(screen.getByText('nmap')).toBeTruthy()
    expect(screen.getByText('ffuf')).toBeTruthy()
    expect(screen.queryByText('sqlmap')).toBeNull()
  })

  it('筛不到东西时说清楚，而不是留一片白', () => {
    render(<RecipesPanel recipes={recipes} />)
    fireEvent.change(screen.getByPlaceholderText('搜索工具（名字 / 用途 / 局限）'), {
      target: { value: 'zzz-not-a-tool' },
    })

    expect(screen.getByText(/没有匹配的工具/)).toBeTruthy()
  })

  it('★ 全库都没有局限说明时，"只看有局限的"的空结果要解释原因', () => {
    // 空结果的两种原因完全不同：搜错了（用户该改词） vs 这一版确实没写局限
    // （用户该知道这功能还没数据）。给同一句话会让人以为是自己搜错了。
    render(<RecipesPanel recipes={[recipe({ name: 'nmap' }), recipe({ name: 'sqlmap' })]} />)
    fireEvent.click(screen.getByText('只看有局限的'))

    expect(screen.getByText(/这一版还没有任何一条写了局限说明/)).toBeTruthy()
  })

  it('清除筛选把两个条件一起清掉', () => {
    render(<RecipesPanel recipes={recipes} />)
    fireEvent.change(screen.getByPlaceholderText('搜索工具（名字 / 用途 / 局限）'), {
      target: { value: 'sql' },
    })
    fireEvent.click(screen.getByText('清除筛选'))

    expect(screen.getByText('nmap')).toBeTruthy()
    expect(screen.getByText('ffuf')).toBeTruthy()
    expect(screen.getByText('3 / 3')).toBeTruthy()
  })
})

describe('RecipesPanel 不造假的权力', () => {
  it('★ 这一页没有任何开关 / checkbox —— 装没装由镜像决定，不由配置决定', () => {
    const { container } = render(
      <RecipesPanel recipes={[recipe({ defaultLimitation: '默认只扫 1000 端口' })]} />,
    )
    fireEvent.click(screen.getByText('nmap'))

    expect(container.querySelectorAll('input[type="checkbox"]')).toHaveLength(0)
    expect(container.querySelectorAll('[role="switch"]')).toHaveLength(0)
    // 按钮只有「展开」和「只看有局限的」两种，没有"启用/停用"
    const labels = within(container)
      .getAllByRole('button')
      .map((el) => el.textContent ?? '')
    expect(labels.some((text) => /启用|停用|开启|关闭/.test(text))).toBe(false)
  })

  it('说明里写明这些没有开关', () => {
    render(<RecipesPanel recipes={[recipe()]} />)

    expect(screen.getByText(/没有开关/)).toBeTruthy()
  })
})
