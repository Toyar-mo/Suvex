/**
 * `TopStatusBar` 的 provider 分支：**三种"没有 provider"的处境，三句不同的话**。
 *
 * ## 为什么必须单独测这一行
 *
 * 这条状态条是全局常驻的，每个页面都在它下面渲染。它说错一句话，
 * 用户在每个页面都会被误导一次 —— 而它错的恰恰是最容易错的那种：
 * **把"我没权限/还没加载"说成"你没配置"**。
 *
 * 2026-10-26 实机撞到过：`zzviewer88`（只读）登录后状态条写
 * 「未配置 · 去「设置」添加模型服务」，而库里配着默认 provider
 * （`gzd/deepseek-v4.1-flash`）。根因是 `settings-store` 把
 * `GET /providers`（`read`）和四路 `GET /settings/*`（`admin`）绑进
 * 同一个 `Promise.all` —— 403 一来，`accounts` 就一直是空的。
 *
 * 修法是把 `accountsStatus` 独立出来，于是 `provider === null` 有了三种
 * 完全不同的原因。**这三种必须说三句不同的话**，否则等于因为他没权限
 * 而谎报系统状态 —— 说反比不说更糟，它会让人去重配一遍已有的东西。
 *
 * ## 判定口径
 *
 * `loading` → 说「读取中」，**且绝不能出现「未配置」三个字**；
 * `error`   → 说「读不到」，同样不能出现「未配置」；
 * `ready`   → 这才轮到「未配置 · 去「设置」添加模型服务」。
 *
 * ⚠️ `vitest.config.ts` 关掉了 `globals`，自动 cleanup 不注册，手动 `cleanup()`。
 */
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useFleetStatus, type FleetStatus } from '@/data/fleet'

import { TopStatusBar } from './TopStatusBar'

vi.mock('@/data/fleet', () => ({ useFleetStatus: vi.fn() }))
vi.mock('./ThemeToggle', () => ({ ThemeToggle: () => <button type="button">theme</button> }))

const mockedUseFleetStatus = vi.mocked(useFleetStatus)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

/**
 * 造一份 `FleetStatus`。
 *
 * 默认值刻意选"有 provider、有用量"的**正常态**：这样每个用例只需覆盖
 * 它真正关心的那一项，而不是在 5 个用例里重复 6 个不相关字段。
 */
function makeStatus(overrides: Partial<FleetStatus> = {}): FleetStatus {
  return {
    agents: { running: 0, queued: 0 },
    provider: null,
    providerStatus: 'ready',
    tokens: null,
    ...overrides,
  }
}

function renderBar(status: FleetStatus) {
  mockedUseFleetStatus.mockReturnValue(status)
  return render(<TopStatusBar />)
}

/**
 * 整条状态条的可读文本。
 *
 * ★ 用它而不是 `getByText(/未配置/)`：本组用例的**关键断言是"这句话不该
 *   出现"，而且不许以任何形态出现**。`getAllByText` 找不到就抛、找到了
 *   还得判断是哪个断点的那份 —— 直接看整条文本更直白，也顺带覆盖了
 *   `hidden sm:inline` / `sm:hidden` 两套文案（jsdom 里 `hidden` 只是
 *   class，两段文本都在 DOM 里，所以一条断言能同时钉住宽屏与窄屏的措辞）。
 */
function barText(): string {
  const el = document.querySelector('[data-status-bar]')
  if (!el) throw new Error('状态条没渲染出来')
  return el.textContent ?? ''
}

describe('TopStatusBar · provider 三态', () => {
  it('loading：说"读取中"，绝不说"未配置"', () => {
    renderBar(makeStatus({ provider: null, providerStatus: 'loading' }))

    expect(barText()).toContain('读取中')
    // ★ 核心断言：还没拉完就说"未配置"是抢答，会把正在加载的用户推去重配。
    expect(barText()).not.toContain('未配置')
    expect(barText()).not.toContain('去「设置」')
  })

  it('error：说"读不到"，绝不说"未配置"', () => {
    renderBar(makeStatus({ provider: null, providerStatus: 'error' }))

    // 这是 viewer 撞到的那条分支：403 不是"没配"，是"读不到"。
    expect(barText()).toContain('读不到')
    expect(barText()).not.toContain('未配置')
    expect(barText()).not.toContain('去「设置」')
  })

  it('ready 且真的没有 provider：才说"未配置 · 去设置添加"', () => {
    renderBar(makeStatus({ provider: null, providerStatus: 'ready' }))

    // 新装的系统就是这样，这时候提醒他去配置是对的，也是唯一该说这句话的处境。
    expect(barText()).toContain('未配置')
    expect(barText()).toContain('去「设置」添加模型服务')
  })

  it('有 provider 时显示短名与 model，且不出现任何兜底文案', () => {
    renderBar(
      makeStatus({
        provider: { id: 'p1', label: 'gzd', model: 'deepseek-v4.1-flash' },
        providerStatus: 'ready',
      }),
    )

    expect(barText()).toContain('gzd')
    expect(barText()).toContain('deepseek-v4.1-flash')
    // provider 存在时，三句兜底话都不该出现（否则就是分支写反了）。
    for (const wrong of ['读取中', '读不到', '未配置']) {
      expect(barText()).not.toContain(wrong)
    }
  })

  it('providerStatus 与 provider 同时存在时以 provider 为准（不透出错误态）', () => {
    /*
      这个组合在真实数据里不该出现（有 provider 说明读成功了），但它是
      **最容易被写错的边界**：若渲染层先判 `providerStatus === 'error'`
      再判 `provider`，就会在明明有模型的情况下报"读不到"。
      钉住它，免得后来有人为了"稳妥"把错误判断提到前面。
    */
    renderBar(
      makeStatus({
        provider: { id: 'p1', label: 'gzd', model: 'flash' },
        providerStatus: 'error',
      }),
    )

    expect(barText()).toContain('gzd')
    expect(barText()).not.toContain('读不到')
  })
})

describe('TopStatusBar · 用量与任务数', () => {
  it('用量为 null 时说"用量暂无数据"，不说"未接入"', () => {
    renderBar(makeStatus({ tokens: null }))

    // `null` 的意思是"还没数"，不是"平台没这个能力"——后者会让看过代码的人
    // 以为端点还没做，而 `GET /overview` 从 10-15 就带着用量字段了。
    expect(barText()).toContain('用量暂无数据')
    expect(barText()).not.toContain('未接入')
  })

  it('有用量时只报"花了多少"，不摆不存在的分母与占比', () => {
    renderBar(
      makeStatus({
        tokens: { used: 184_300, budget: null, windowLabel: '累计' },
      }),
    )

    expect(barText()).toContain('184.3k')
    // `1M` 与 `18%` 是 v0.36 删掉的编造值：平台不替用户管配额，
    // 没有分母就不该摆除法。这条断言防止它们被"顺手加回来"。
    expect(barText()).not.toContain('18%')
    expect(barText()).not.toMatch(/\/\s*1M/)
  })

  it('有任务在跑时高亮，且文案带"扫描"二字以区别于智能体数', () => {
    renderBar(makeStatus({ agents: { running: 3, queued: 1 } }))

    expect(barText()).toContain('3')
    // ★ 主语不能省：省掉后这个数字会和活动页的「智能体 X/Y」读成同一个量，
    //   一个 0 一个 6，看起来像状态没同步（任务 11 记录过）。
    expect(barText()).toContain('扫描运行中')
    expect(barText()).toContain('1 排队')
  })
})
