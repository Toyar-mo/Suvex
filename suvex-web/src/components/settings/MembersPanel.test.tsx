/**
 * 处理人面板的渲染契约（v1.79）。
 *
 * ## 为什么测这个面板
 *
 * 它是补上的一个**曾经不存在的入口**：漏洞页的指派对话框提示
 * 「去设置 → 处理人里加一个」，而设置页原本没有这一节 —— 用户照着提示
 * 找过去什么也没有。所以要钉住的是：
 *
 *   1. 名单真的渲染出来（名字、邮箱、已停用标记）；
 *   2. **只有管理员**看得到新增/停用/删除 —— viewer 点了必然 403；
 *   3. 新增真的调用 `createMember`，不是只把表单画出来。
 *
 * ⚠️ `vitest.config.ts` 关了 `globals`，手动 `cleanup()`；不用 jest-dom 匹配器。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import MembersPanel from './MembersPanel'
import type { Member, MemberWriteResult } from '@/data/members-store'

const memberList: Member[] = [
  {
    id: 'member_1',
    name: '张三',
    email: 'zhangsan@example.com',
    active: true,
    createdAt: '2026-10-01T10:00:00.000Z',
  },
  {
    id: 'member_2',
    name: '李四',
    email: null,
    active: false,
    createdAt: '2026-10-02T10:00:00.000Z',
  },
]

const createMember = vi.fn<(name: string, email: string | null) => Promise<MemberWriteResult>>(
  async () => ({ ok: true }),
)

let admin = true
vi.mock('@/data/auth-store', () => ({
  isAdmin: () => admin,
  useAuth: () => ({ user: { id: 'u1', username: 'admin', role: admin ? 'admin' : 'viewer' } }),
}))

vi.mock('@/data/members-store', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/data/members-store')>()
  return {
    ...actual,
    useMembers: () => ({
      members: memberList,
      status: 'ready',
      saving: false,
      error: null,
    }),
    loadMembers: vi.fn(async () => {}),
    createMember: (...args: [string, string | null]) => createMember(...args),
    updateMember: vi.fn(async () => ({ ok: true as const })),
    deleteMember: vi.fn(async () => ({ ok: true as const, unassigned: 0 })),
  }
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  admin = true
})

describe('MembersPanel', () => {
  it('列出名字、邮箱与停用状态', () => {
    render(<MembersPanel />)

    expect(screen.getByText('张三')).not.toBeNull()
    expect(screen.getByText('zhangsan@example.com')).not.toBeNull()
    expect(screen.getByText('李四')).not.toBeNull()
    expect(screen.getByText('已停用')).not.toBeNull()
    // 可指派人数说的是 active 的那一个
    expect(screen.getByText(/其中 1 人可被指派/)).not.toBeNull()
  })

  it('★ 只读用户看得到名单，但没有任何管理按钮', () => {
    admin = false
    render(<MembersPanel />)

    expect(screen.getByText('张三')).not.toBeNull()
    expect(screen.queryByRole('button', { name: /新增处理人/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /编辑/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /停用/ })).toBeNull()
  })

  it('★ 新增真的提交（姓名去掉首尾空格，空邮箱变成 null）', async () => {
    render(<MembersPanel />)

    fireEvent.click(screen.getByRole('button', { name: /新增处理人/ }))
    fireEvent.change(screen.getByLabelText('姓名'), { target: { value: '  王五  ' } })
    fireEvent.click(screen.getByRole('button', { name: '创建' }))

    await waitFor(() => expect(createMember).toHaveBeenCalledWith('王五', null))
  })
})
