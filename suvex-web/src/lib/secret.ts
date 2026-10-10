import type { ApiToken } from '@/data/settings'

/**
 * 密钥相关的**纯逻辑**。
 *
 * 为什么单独成文件而不是留在 SecretField.tsx 里：
 *   项目有一条 Fast Refresh 的约束 —— 组件文件只应导出组件。
 *   混出一个纯函数会让该文件的 HMR 退化成整页刷新（开发时每次改动都丢状态）。
 *   这是本项目已经踩过的坑（见 lib/finding-query.ts 的文件头）。
 *   顺带的好处：这些函数可以直接被脚本 import 来验证，不必渲染 React。
 */

/**
 * 密钥的**可辨认预览** —— 前 6 位 + 后 4 位，中间省略。
 *
 * 界面上显示 `sk-dem…0f2a` 这样的片段，作用是让用户确认
 * "这里确实有一个密钥，而且是哪一个"，**不是**让任何人从中还原它。
 *
 * 为什么取头尾而不只取开头：真实世界里同一家的多个 key
 * 前缀往往完全相同（都以 sk- 或 sk-proj- 开头），只给前缀等于没给信息 ——
 * 用户分不出哪条是哪条。尾部几位才是真正能区分的部分（各家 key 尾部随机）。
 *
 * 这条规则与主流控制台一致（GitHub 的 token、Stripe 的 key 都显示尾部）。
 *
 * ⚠️ 短于 12 位的值不做截断处理，直接整体省略掉 ——
 *    否则"前 6 + 后 4"会覆盖整个字符串，等于把它完整印出来。
 *    这个边界不处理的话，一个 10 位的短 key 会被原样显示。
 */
export function secretPreview(value: string): string {
  if (value.length <= 12) return '\u2022'.repeat(8)
  return `${value.slice(0, 6)}\u2026${value.slice(-4)}`
}

// ─────────────────────────────────────────────
// 令牌状态
// ─────────────────────────────────────────────

export type TokenState = 'active' | 'expired' | 'stale'

/** 距上次创建多久算"该轮换了"。90 天是常见合规周期 */
const STALE_DAYS = 90

/**
 * 判断令牌当前处于什么状态。
 *
 * ★ `now` 是**显式参数**，不在函数内部取 Date.now()。
 *   两个理由，第二个是硬约束：
 *
 *   1. 纯函数才好测 —— 传一个固定时刻就能验证"过期/将过期"的边界；
 *   2. React 的渲染必须是纯的。在组件里直接调 Date.now() 会被
 *      eslint 的 react-hooks/purity 规则拦下，而且它**确实是 bug**：
 *      每次重渲染都得到不同的"现在"，同一个令牌的过期状态会在
 *      两次渲染之间跳变，界面上表现为标签忽隐忽现。
 *
 *   调用方用挂载时刻固定一个 now（见 AccountPanel），
 *   这与报告页 referenceNow 的做法一致。
 */
export function tokenState(token: ApiToken, now: number): TokenState {
  if (token.expiresAt !== null) {
    return new Date(token.expiresAt).getTime() < now ? 'expired' : 'active'
  }

  // 永不过期的令牌：太久没换过就提示轮换，但不强制 ——
  // 强制过期会被绕过（用户写死一个永久令牌然后忘掉它）
  const age = now - new Date(token.createdAt).getTime()
  return age > STALE_DAYS * 86_400_000 ? 'stale' : 'active'
}
