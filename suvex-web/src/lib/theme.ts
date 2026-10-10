/**
 * 主题的**纯逻辑层**：读偏好 → 解析偏好 → 落到 DOM。
 *
 * ## 为什么「跟随系统」的解析放在 JS 里，而不是交给 CSS 的媒体查询
 *
 * 这是这次加亮色主题最重要的一处收口。CSS 那边只写两套**确定**的状态：
 * `:root`（暗）与 `[data-theme="light"]`（亮），不需要
 * `@media (prefers-color-scheme)` 分支。于是：
 *
 *   · 「跟随系统」只剩一件事 —— 每次解析时现读一次 `matchMedia`，
 *     它不构成第三种样式状态，CSS 侧永远只有"当前是什么"；
 *   · 用户显式选过亮/暗之后，系统偏好再也影响不到界面。
 *     这一点如果交给 CSS 做会很脏：那意味着"媒体查询"和"显式选择"
 *     是两套真源，得靠 `:root:not([data-theme])` 这类选择器去调和，
 *     而它一遇到"跟随系统"就退化（跟随系统时 `data-theme` 到底写不写？）。
 *     现在判断只发生在一个地方，就是这里。
 *
 * ★ 兜底方向是刻意的：CSS 里 `:root` 是暗色，所以本模块在**拿不到答案**时
 *   （window/document 不存在、localStorage 或 matchMedia 被拦）一律按暗色解析。
 *   两边相信同一件事，于是 JS 万一没跑起来，界面退化成的就是今天的样子 ——
 *   而不是先亮一下再闪回暗色。
 *
 * ⚠️ 这个文件里**所有** localStorage / matchMedia / document 访问都包在 try/catch 里。
 *   隐私模式、禁用存储的沙箱、若干企业内部浏览器会把它们直接抛出来，
 *   而「主题没切成功」是这个产品里最不该造成崩溃的一类失败：
 *   界面照样能用，只是颜色不对。
 */

/** 存储 key。刻意不加 `suvex.` 前缀 —— 与 `useSidebarCollapsed` 的 key 不同，这里是契约里写死的名字 */
export const THEME_STORAGE_KEY = 'suvex-theme'

/** 用户在菜单里选的那个东西。`system` 是缺省，也是"我不打算管这件事" */
export type ThemePreference = 'system' | 'light' | 'dark'

/** 真正生效的主题。它才是写到 `<html data-theme>` 上的值 */
export type ResolvedTheme = 'light' | 'dark'

/**
 * 深色偏好查询串。
 *
 * 抽成常量是因为它被解析与订阅两处用到，而写错一个字符的失败方式是**静默**的：
 * 永远解析成亮色，没有任何报错。`index.html` 的内联脚本里有一份字面量副本
 * （那段脚本必须在 bundle 之前执行，没法 import）—— 改这里时别忘了它。
 */
export const DARK_MODE_QUERY = '(prefers-color-scheme: dark)'

/** 菜单顺序：从"最不管事"到"最明确"。跟随系统在前，因为它是缺省 */
export const THEME_PREFERENCES: readonly ThemePreference[] = ['system', 'light', 'dark']

export function isThemePreference(value: unknown): value is ThemePreference {
  return value === 'system' || value === 'light' || value === 'dark'
}

/**
 * 读存储里的偏好。
 *
 * 非法值（手改过、上一版留下的、被别的脚本覆盖的）一律当缺省 ——
 * 与其猜用户想要哪个，不如回到「跟随系统」这个不会错得离谱的答案。
 * 读取本身可能抛（隐私模式），抛了也走同一条路。
 */
export function readStoredPreference(): ThemePreference {
  try {
    const raw = window.localStorage.getItem(THEME_STORAGE_KEY)
    return isThemePreference(raw) ? raw : 'system'
  } catch {
    return 'system'
  }
}

/**
 * 系统当前是否偏好深色。
 *
 * ⚠️ 拿不到答案时返回 `true`（= 暗色），与 `:root` 的现状一致 —— 见文件头。
 *    `window` 不存在时这里会抛 ReferenceError，正好被同一个 catch 接住。
 */
export function prefersDark(): boolean {
  try {
    return window.matchMedia(DARK_MODE_QUERY).matches
  } catch {
    return true
  }
}

/** 把偏好解析成具体值。这是全站唯一一处"跟不跟系统"的判断 */
export function resolveTheme(preference: ThemePreference): ResolvedTheme {
  if (preference === 'system') return prefersDark() ? 'dark' : 'light'
  return preference
}

/**
 * 应用主题并返回解析结果。
 *
 * 写到 `documentElement`（即 `<html>`）而不是 body 或某个 React 根：
 *   · CSS 的覆盖选择器就是 `[data-theme="light"]`，与 `:root` 同一层；
 *   · html 在 React 挂载**之前**就存在，所以首帧（甚至 React 崩掉时）样式也生效。
 *
 * 返回解析值而不是让调用方再算一遍 —— 那会变成"同一时刻解析两次"，
 * 而两次之间系统偏好理论上可以变（实测几乎不会，但没必要留这个口子）。
 */
export function applyTheme(preference: ThemePreference): ResolvedTheme {
  const resolved = resolveTheme(preference)
  try {
    document.documentElement.dataset.theme = resolved
  } catch {
    // 极端沙箱里连 document 都可能拿不到。设不上就设不上，界面还是暗色
  }
  syncBrowserChrome()
  return resolved
}

/**
 * 让浏览器自己的界面跟着主题走。
 *
 * ## 它管的是什么
 *
 * `index.html` 里那个 `<meta name="theme-color">`（移动端地址栏 / 状态栏的底色）
 * 和 `<meta name="color-scheme">`（首个 CSS 到达之前，浏览器对原生控件
 * 与默认背景的猜测）。
 *
 * ## ★ 为什么不在这里写死两个色值
 *
 * 因为这个文件里**不许出现裸色值** —— token 的唯一事实来源是
 * `src/index.css` 的 `:root` / `[data-theme='light']`。写死一份 hex
 * 就等于又开了一个副本，而副本的下场一定是漂移（这个项目刚因为
 * `index.css` 与 `tailwind.config.js` 各存一份值而漂移过一次）。
 *
 * 所以这里**现读计算后的变量**：`--suvex-canvas` 是 RGB 通道三元组
 * （`8 9 12`），用 `rgb(...)` 包一下就是合法颜色值。
 *
 * ⚠️ 整个函数包在 try/catch 里：`theme-color` 是锦上添花，
 *    设不上最多是移动端地址栏颜色不匹配，绝不该影响主题本身。
 */
function syncBrowserChrome(): void {
  try {
    const canvas = getComputedStyle(document.documentElement)
      .getPropertyValue('--suvex-canvas')
      .trim()
    if (!canvas) return
    const meta = document.querySelector('meta[name="theme-color"]')
    // 变量值形如 `8 9 12`；已经是合法颜色（手动改过 hex）时原样用
    meta?.setAttribute('content', canvas.includes(' ') ? `rgb(${canvas})` : canvas)
  } catch {
    // 拿不到就维持 index.html 里的静态值（暗色）—— 与 :root 的兜底一致
  }
}

/**
 * 订阅系统配色变化，返回解绑函数。
 *
 * 把"访问 matchMedia"这件事关在这个文件里（它要 try/catch），
 * 调用方（`data/theme-store`）只负责决定**什么时候挂、什么时候摘**。
 *
 * ⚠️ 这里只认 `addEventListener`，不兜 `addListener`（很老的 Safari 只有后者）。
 *    理由：那条兼容分支一旦写错就是"监听静默失效"，而失效的代价仅仅是
 *    「跟随系统」在系统换色后要等下次刷新才生效 —— 退化得很轻，
 *    不值得为它增加一条几乎永远跑不到的路径。
 */
export function subscribeSystemTheme(onChange: () => void): () => void {
  try {
    const query = window.matchMedia(DARK_MODE_QUERY)
    query.addEventListener('change', onChange)
    return () => {
      try {
        query.removeEventListener('change', onChange)
      } catch {
        // 解绑失败没有补救手段，也没有后果：页面生命周期结束时它会自然消失
      }
    }
  } catch {
    // matchMedia 不可用：返回一个空解绑函数，让调用方不必区分这两种情形
    return () => {}
  }
}
