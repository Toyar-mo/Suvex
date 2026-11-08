import { afterEach, describe, expect, it, vi } from 'vitest'

import { saveProviderAccount } from './settings-store'
import type { ProviderAccount } from './settings'

/*
  ══════════════════════════════════════════════════════════════
  模型服务保存的两个真实缺陷（v1.78 修复）
  ══════════════════════════════════════════════════════════════

  ① **「设为默认模型」从来没发出去**：勾了保存，界面显示"已保存"，
     而后端默认模型没变。字段一直存在于 `ProviderCreate/Update`，
     只是前端 `common` 里漏了它 —— 不报错、不提示，纯静默。

  ② **新建时验证凭据用的是临时 id**：`POST /providers` 返回的是服务端 id，
     前端却拿临时 id 去 `/providers/{id}/verify` → 404；更糟的是下一次
     保存查不到已有条目会**再 POST 一条**，列表里出现两份同样的配置。
     修复的前提就是这里：保存要把服务端那一条带回来。
*/

const post = vi.fn()
const patch = vi.fn()

vi.mock('@/lib/api', () => ({
  api: {
    post: (...args: unknown[]) => post(...args),
    patch: (...args: unknown[]) => patch(...args),
    get: vi.fn(),
    delete: vi.fn(),
  },
  errorMessage: (err: unknown) => String(err),
}))

vi.mock('@/lib/theme', () => ({}))

function account(over: Partial<ProviderAccount> = {}): ProviderAccount {
  return {
    id: 'draft_1',
    label: 'DeepSeek',
    model: 'deepseek-chat',
    kind: 'cloud',
    apiKey: 'sk-test',
    hasKey: false,
    apiKeyMask: '',
    baseUrl: 'https://api.deepseek.com',
    isDefault: false,
    verified: false,
    verifyError: null,
    note: '',
    ...over,
  } as ProviderAccount
}

afterEach(() => {
  post.mockReset()
  patch.mockReset()
})

describe('saveProviderAccount', () => {
  it('★★ 新建时把「设为默认模型」发出去', async () => {
    post.mockResolvedValue({
      status: 'ok',
      at: 't',
      provider: { ...account({ id: 'prov_real', isDefault: true }) },
    })

    await saveProviderAccount(account({ isDefault: true }))

    expect(post).toHaveBeenCalledTimes(1)
    expect(post.mock.calls[0][1]).toMatchObject({ isDefault: true })
  })

  it('★ 修改时同样带上 isDefault（否则"只改这一项"永远存不进去）', async () => {
    // 先建一条，让 store 里真的有它 —— 第二次才会走 PATCH
    post.mockResolvedValue({
      status: 'ok',
      at: 't',
      provider: { ...account({ id: 'prov_real' }) },
    })
    patch.mockResolvedValue({
      status: 'ok',
      at: 't',
      provider: { ...account({ id: 'prov_real', isDefault: true }) },
    })

    await saveProviderAccount(account({ isDefault: false }))
    await saveProviderAccount(account({ id: 'prov_real', isDefault: true }))

    expect(patch).toHaveBeenCalledTimes(1)
    expect(patch.mock.calls[0][1]).toMatchObject({ isDefault: true })
  })

  it('★★ 返回服务端那一条（新建时 id 会变），验证凭据才能用真 id', async () => {
    post.mockResolvedValue({
      status: 'ok',
      at: 't',
      provider: { ...account({ id: 'prov_real' }) },
    })

    const result = await saveProviderAccount(account())

    expect(result.status).toBe('ok')
    if (result.status === 'ok') {
      expect(result.account?.id).toBe('prov_real')
    }
  })
})
