import { describe, expect, it } from 'vitest'
import {
  countAdmins,
  deleteBlockedReason,
  demoteBlockedReason,
  type ManagedUser,
} from './users-store'

/*
  ══════════════════════════════════════════════════════════════
  用户管理里的"防自锁"判断
  ══════════════════════════════════════════════════════════════

  ## 为什么单独测这两个纯函数

  它们对应两个**通过界面走进去的死胡同**：

    · 删掉最后一个管理员 → 没人能再改设置，恢复只能直接改库；
    · 把最后一个管理员降级 → 同上；
    · 删掉自己 → 立刻掉线，若同时是最后一个管理员就再也进不来。

  后端**也**拦这三件事（那才是真正的防线）。那为什么前端还要判一次？

  因为**"点了必然 403 的按钮"是坏交互**：用户点了、等了一下、
  弹出一句"这是最后一个管理员" —— 他会觉得系统在耍他。
  正确做法是按钮从一开始就不可点，**并且说明为什么**。

  所以这两个函数的返回值是**原因字符串**而不是布尔值 ——
  界面要显示那句话。灰按钮不给理由同样让人困惑。

  ## 与后端的分工（重要）

  前端判错了的后果是**交互不好**，不是**安全漏洞**：
  后端仍然会拒绝。所以这里的测试是"交互正确性"，
  而安全性的测试在 `test_users_and_roles.py`。
*/

function user(id: string, role: 'admin' | 'viewer', username = id): ManagedUser {
  return { id, username, role, createdAt: '2026-10-01T00:00:00.000Z' }
}

const SOLO_ADMIN = [user('u1', 'admin')]
const TWO_ADMINS = [user('u1', 'admin'), user('u2', 'admin')]
const MIXED = [user('u1', 'admin'), user('u2', 'viewer'), user('u3', 'viewer')]

// ══════════════════════════════════════════════════════════
// 一、删除的两种拒绝
// ══════════════════════════════════════════════════════════

describe('deleteBlockedReason', () => {
  it('★ 不能删自己', () => {
    // 删完立刻掉线 —— 而如果同时是最后一个管理员就再也进不来了
    const reason = deleteBlockedReason(MIXED[1], MIXED, 'u2')

    expect(reason).toContain('当前登录')
  })

  it('★ 不能删最后一个管理员', () => {
    const reason = deleteBlockedReason(SOLO_ADMIN[0], SOLO_ADMIN, 'someone-else')

    expect(reason).toContain('最后一个管理员')
  })

  it('★ 还有别的管理员时**可以**删', () => {
    // 这条是上一条的对照。少了它，"不能删管理员"可能被实现成
    // "永远不能删管理员"，而那是一个功能缺失。
    expect(deleteBlockedReason(TWO_ADMINS[0], TWO_ADMINS, 'someone-else')).toBeNull()
  })

  it('viewer 可以删（它不是管理员，不影响管理能力）', () => {
    expect(deleteBlockedReason(MIXED[1], MIXED, 'u1')).toBeNull()
  })

  it('★ "删自己"优先于"最后一个管理员"报告', () => {
    /*
      两种拒绝同时成立时（自己是唯一的管理员），该说哪一句？

      说"不能删自己" —— 因为那是**用户此刻能理解的原因**
      （"我在删我自己"），而"最后一个管理员"听起来像在讲系统状态。
      而且用户的下一个动作不同：前者他会去删别人，
      后者他会去加一个管理员。
    */
    const reason = deleteBlockedReason(SOLO_ADMIN[0], SOLO_ADMIN, 'u1')

    expect(reason).toContain('当前登录')
  })

  it('未登录（currentUserId 为 null）时不误判为"删自己"', () => {
    // 令牌通道下 `user?.id` 是 undefined —— 不能因此把所有删除都拦掉
    expect(deleteBlockedReason(TWO_ADMINS[0], TWO_ADMINS, null)).toBeNull()
  })
})

// ══════════════════════════════════════════════════════════
// 二、降级的拒绝
// ══════════════════════════════════════════════════════════

describe('demoteBlockedReason', () => {
  it('★ 不能降级最后一个管理员', () => {
    expect(demoteBlockedReason(SOLO_ADMIN[0], SOLO_ADMIN)).toContain('最后一个管理员')
  })

  it('★ 还有别的管理员时可以降级', () => {
    expect(demoteBlockedReason(TWO_ADMINS[0], TWO_ADMINS)).toBeNull()
  })

  it('★ **降级自己**是允许的（只要有别的管理员）', () => {
    /*
      与"删除自己"不同：把自己降成只读之后**还登录着**，
      而且还有别的管理员能把他升回来 —— 这不是死胡同。

      拦住它反而是错的：管理员想交出权限时，那是正当操作。
    */
    expect(demoteBlockedReason(TWO_ADMINS[0], TWO_ADMINS)).toBeNull()
  })
})

// ══════════════════════════════════════════════════════════
// 三、管理员计数
// ══════════════════════════════════════════════════════════

describe('countAdmins', () => {
  it('只数管理员', () => {
    expect(countAdmins(MIXED)).toBe(1)
    expect(countAdmins(TWO_ADMINS)).toBe(2)
  })

  it('空列表是 0（不抛异常）', () => {
    expect(countAdmins([])).toBe(0)
  })

  it('★ 判据是 `role === "admin"`，不是"角色不是 viewer"', () => {
    /*
      这条防的是一个真实的实现陷阱：写成
      `users.filter((u) => u.role !== 'viewer')` 也能通过上面两条断言，
      但库里出现**未知角色**（手工改库、或将来加了新角色）时，
      那个账号会被算成管理员 —— 于是"最后一个管理员不能删"这条
      保护会失效。

      与后端 `scopes_for_role` 的"未知角色 → 空集"是同一条原则：
      **判据要正面写（是 admin），不要反面写（不是 viewer）**。
    */
    const withGhost = [
      user('u1', 'admin'),
      { ...user('u2', 'viewer'), role: 'superuser' as 'viewer' },
    ]

    expect(countAdmins(withGhost)).toBe(1)
  })
})
