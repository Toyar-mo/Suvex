/**
 * 复制文本到剪贴板 —— 带**降级链**。
 *
 * ## 为什么不能只用 `navigator.clipboard`
 *
 * 实测（2026-10-01，用户通过 `http://192.168.44.128:5173` 访问，
 * 用 `browser_evaluate` 在那个页面的真实上下文里读出来的）：
 *
 *     window.isSecureContext        === false
 *     typeof navigator.clipboard    === 'undefined'
 *     navigator.clipboard.writeText → TypeError: Cannot read properties
 *                                      of undefined (reading 'writeText')
 *
 * 异步剪贴板 API **只在安全上下文里存在**（`https` 或 `localhost`）。
 * 而本系统是按「局域网 IP + http」部署的 —— 在**这个部署形态下它永远
 * 不存在**，不是概率问题。
 *
 * 而原来七处调用全都写成同一个形状：
 *
 *     try { await navigator.clipboard.writeText(x) } catch { /* 静默 *\/ }
 *
 * 于是每一次复制都抛那个 `TypeError`，被自己的 `catch` 吞掉 ——
 * **点了没有任何反应**。用户的原话是「这个按了没复制」。
 *
 * ⚠️ 那三处 `catch` 的注释写着「用户仍然可以手动选中复制，功能没丢」。
 *    这个理由**不成立**：`FlagChip` 是 `truncate` 显示的（界面上是
 *    `CTF2{070b3903-1bf5-4d41-9566-e56bc…`），用户即使手动选中，也
 *    **无法确认自己拿全了**。给人一个看起来能按、按了没反应的按钮，
 *    比明确说"复制不了"更糟。
 *
 * ## 降级链
 *
 * | 顺序 | 手段 | 前提 | 本部署下 |
 * | --- | --- | --- | --- |
 * | ① | `navigator.clipboard.writeText` | **安全上下文** + 权限 | 不存在，跳过 |
 * | ② | `document.execCommand('copy')` | **用户手势**（不需要安全上下文） | 调用方都在 click 里 |
 *
 * ② 是这里的关键：`execCommand` **不受安全上下文限制** —— 这正是它与
 * 异步 API 的分水岭，也是老站点在 http 下一直能复制的原因。
 * （它已被标记为 deprecated，但所有主流浏览器都仍然实现它，且目前
 * 没有"在非安全上下文下可用"的替代品。）
 *
 * ⛔ **本轮验证的边界，如实记下**：本机的自动化浏览器产生不了任何输入
 *    事件（实测：`document.body` 捕获阶段的 click 与 keydown 监听器
 *    收到 0 次事件），所以 ② 这条路径**没能**在浏览器里跑通验证。
 *    被验证的是「①→② 的降级顺序」与「①不存在时不再抛异常」。
 *    ②在有真实用户手势时可用，依据是它的规范定义，不是本轮的实测。
 *
 * ## 返回 `boolean` 而不是抛异常
 *
 * 调用方需要**区分**成功与失败才能给出诚实的反馈 —— 而"失败"在这里
 * 不是异常情况，是这个部署形态下的常态。让调用方拿到一个布尔值，
 * 它才有机会说"复制不了，但值在这里，你按 Ctrl+C"。
 */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 权限被拒、iframe 缺 clipboard-write、文档失焦……
      // 这些都是"这条路走不通"，不是"复制失败" —— 继续降级。
    }
  }
  return legacyCopy(text)
}

/**
 * `document.execCommand('copy')` 退路。
 *
 * ## 为什么要造一个 textarea 再选中它
 *
 * `execCommand('copy')` 复制的是**当前选区**，所以在调它之前必须先把
 * 要复制的文本变成选区。用一个临时 textarea 而不是"选中页面上的某个
 * 元素"，是因为后者会**改变用户看到的界面**（文字被高亮），而复制是个
 * 应该无声完成的操作。
 *
 * ## 几个不能省的细节
 *
 * * `readonly` —— 否则移动端会弹键盘，把一次复制变成一次输入；
 * * `position: fixed; top: -1000px` —— 移出视口但仍**在文档里**：
 *   不在文档里的元素无法被 `select()`；
 * * `setSelectionRange(0, len)` —— iOS Safari 上 `select()` 对
 *   `readonly` 元素不生效，必须补这一句；
 * * 用完立刻 `remove()` —— 留在页面上的 textarea 会被 Tab 键聚焦。
 *
 * ⛔ 返回 `false` 表示连退路也失败了（最可能是没有用户手势）。
 *    调用方**必须**给用户别的出路，见 `FlagChip` 的"自动选中"分支。
 */
function legacyCopy(text: string): boolean {
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.setAttribute('aria-hidden', 'true')
  area.style.cssText = 'position:fixed;top:-1000px;left:-1000px;opacity:0'

  document.body.appendChild(area)
  try {
    area.select()
    area.setSelectionRange(0, area.value.length)
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    document.body.removeChild(area)
  }
}

/**
 * 兜底：把某个元素的文本**选中**，让用户按 Ctrl+C。
 *
 * ## 为什么需要它
 *
 * 降级链两级都失败时，唯一诚实的做法是**告诉用户怎么办**，而不是
 * 什么都不发生。选中文本之后，用户只需按一下 Ctrl+C —— 这比"再自己
 * 用鼠标去划一遍"少一步，也明确传递了"自动复制没成功"这个事实。
 *
 * ⚠️ 选中**元素内的完整文本**（`textContent`），而不是"用户看得见的
 *    那部分"：`FlagChip` 在界面上是截断显示的，而用户要的是完整值。
 *    浏览器的选区本来就基于 DOM 文本，所以这里天然是对的 —— 但值
 *    必须从 `textContent` 取，不能用视觉宽度去判断。
 */
export function selectElementText(element: HTMLElement | null): boolean {
  if (!element) return false
  const selection = window.getSelection()
  if (!selection) return false
  try {
    const range = document.createRange()
    range.selectNodeContents(element)
    selection.removeAllRanges()
    selection.addRange(range)
    return true
  } catch {
    return false
  }
}
