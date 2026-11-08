/**
 * 画布**氛围层**的偏好（纯逻辑层）。
 *
 * ## 它和主题（亮/暗）是两件事
 *
 * 主题回答"深色还是浅色"，氛围层回答"背景上有没有东西、是什么东西"。
 * 两者正交：任何氛围都能配亮色或暗色，所以它是**独立的偏好**，
 * 不塞进 `theme` 的枚举里 —— 否则组合会炸成 8 个取值，而菜单只能列 3 项。
 *
 * ## 为什么默认仍是「点阵场」
 *
 * 它是今天线上正在跑的那一套（`AgentField`）。加这个开关的目的是**给选择**，
 * 不是偷偷换掉现状 —— 默认值一改，所有人的第一印象都变了，而他们并没有要求。
 *
 * ⚠️ 与 `lib/theme.ts` 同一条兜底规矩：localStorage / document 访问全部包
 *    try/catch。隐私模式下读不到就回到默认，绝不让"背景没切成功"变成崩溃。
 */

/** 存储 key。与主题的 `suvex-theme` 平行命名 */
export const BACKDROP_STORAGE_KEY = ['suvex', 'backdrop'].join('-')

/**
 * 四种氛围。
 *
 * - `field`  点阵场：全站现行的动态点阵（`AgentField`）。表达"系统一直在跑"。
 * - `aurora` 光晕：两团静止的极淡径向光。有纵深、没有噪点。
 * - `grain`  纸纹：细颗粒 + 极淡的垂直渐变。安静、有质感。
 * - `plain`  纯色：什么都不画。做截图、看数据、专注阅读时用。
 */
export type BackdropId = 'field' | 'aurora' | 'grain' | 'plain'

export const BACKDROPS: readonly {
  id: BackdropId
  label: string
  /** 一句话说明它适合什么时候用（菜单里显示） */
  hint: string
}[] = [
  { id: 'field', label: '点阵场', hint: '现行的动态点阵，表达"系统在跑"' },
  { id: 'aurora', label: '光晕', hint: '静止的柔光，安静但有纵深' },
  { id: 'grain', label: '纸纹', hint: '细颗粒质感，适合长时间阅读' },
  { id: 'plain', label: '纯色', hint: '什么都不画，最干净' },
]

export const DEFAULT_BACKDROP: BackdropId = 'field'

export function isBackdropId(value: unknown): value is BackdropId {
  return BACKDROPS.some((item) => item.id === value)
}

/** 读存储里的偏好。非法值 / 读不到 → 默认（见文件头） */
export function readStoredBackdrop(): BackdropId {
  try {
    const raw = window.localStorage.getItem(BACKDROP_STORAGE_KEY)
    return isBackdropId(raw) ? raw : DEFAULT_BACKDROP
  } catch {
    return DEFAULT_BACKDROP
  }
}

/**
 * 把偏好写到 `<html data-backdrop>`。
 *
 * ★ 用 data 属性而不是给 body 换 class：CSS 侧只需要一个选择器
 *   （`[data-backdrop='aurora'] .backdrop-aurora`），而且与主题的
 *   `data-theme` 同一套写法 —— 两套机制长得一样，读代码的人不用切换心智。
 */
export function applyBackdrop(id: BackdropId): void {
  try {
    document.documentElement.dataset.backdrop = id
  } catch {
    // document 不存在（SSR / 测试环境）时什么都不做
  }
}
