/**
 * 全局测试前置：给 jsdom 补上它没实现的浏览器 API。
 *
 * 为什么要有这个文件：`jsdom` 不做布局、不做渲染观察，于是这几个 API 一律
 * 缺失，而源码里真的在用它们：
 *
 *   · `window.matchMedia` —— `hooks/useMediaQuery.ts`、`hooks/useSidebarCollapsed.ts`、
 *     `lib/theme.ts`（「跟随系统」要读 `prefers-color-scheme`）
 *   · `IntersectionObserver` —— `components/layout/AppShell.tsx`（`PageHeader` 就在里面）、
 *     `components/effects/DotGrid.tsx`、`components/effects/Threads.tsx`、
 *     `components/settings/SaveBar.tsx`、`pages/SettingsPage.tsx`
 *   · `ResizeObserver` —— 同样在布局/画布类组件里
 *
 * 少了它们，任何**真的渲染到页面级组件**的测试都会炸在
 * `TypeError: window.matchMedia is not a function` 或
 * `ReferenceError: IntersectionObserver is not defined` 上。
 *
 * 这类报错极具误导性：看起来像被测组件的 bug，其实是环境缺件。
 * 之前 `src/components/` 下的 5 个组件测试能过，只是因为它们渲染的
 * 恰好是不碰这些 API 的叶子组件 —— 一旦开始测页面就会撞上。
 * 与其让每个页面测试各写一份桩，不如在这里补一次。
 *
 * 为什么用 `Object.defineProperty` / `class` 而不是 `vi.stubGlobal`：
 * `vitest.config.ts` 开了 `restoreMocks: true`，用 `vi.*` 创建的桩会被
 * 逐个用例还原；而这是**环境补丁**，不该随用例生命周期反复失效。
 *
 * 为什么观察器一律「什么都不做」：它们真正的职责是驱动懒加载与进场动画，
 * 测试断言的是**渲染出的文本与结构**，不依赖"是否进入视口"。做成空实现
 * 反而让断言稳定（不会因为动画时序不同而抖动）。需要真实回调的用例
 * 可以自己再覆盖一次（后来者生效）。
 */

import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: (query: string): MediaQueryList => {
      const mediaQueryList = {
        // 一律 false =「精细指针 / 浅色偏好 / 不要求减少动效」这套默认，
        // 是最不容易让断言受动画与时序影响的一档。
        matches: false,
        media: query,
        onchange: null,
        // 源码只认 addEventListener/removeEventListener（见 theme.ts 的注释：
        // 不兜 addListener，因为那条兼容分支写错就是静默失效）。
        // 这里仍把两者都补上，免得将来某处用到时又炸一次。
        addEventListener: () => {},
        removeEventListener: () => {},
        addListener: () => {},
        removeListener: () => {},
        dispatchEvent: () => false,
      }
      return mediaQueryList as unknown as MediaQueryList
    },
  })
}

/** 回调永远不触发的观察器 —— 只要不抛异常，被测组件就能正常挂载。 */
class NoopObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return []
  }
}

if (typeof globalThis.IntersectionObserver === 'undefined') {
  Object.defineProperty(globalThis, 'IntersectionObserver', {
    writable: true,
    configurable: true,
    value: class extends NoopObserver {
      root = null
      rootMargin = ''
      thresholds: number[] = []
    },
  })
}

if (typeof globalThis.ResizeObserver === 'undefined') {
  Object.defineProperty(globalThis, 'ResizeObserver', {
    writable: true,
    configurable: true,
    // 不声明 constructor：父类的无参构造已经够用，回调收到就被丢弃 ——
    // 这正是我们要的（测试不依赖"是否进入视口"）。
    value: class extends NoopObserver {},
  })
}

/**
 * `Element.prototype.scrollIntoView` —— jsdom 同样没有实现它
 * （它属于"布局/滚动"一类，jsdom 一律不碰）。
 *
 * 用到它的地方：`components/layout/CommandPalette.tsx` 把高亮项滚进视口
 * （按 ↑↓ 移动时要让人看得见当前选中哪一条）。
 *
 * ★ 补成**空实现**，不去记录"谁被滚动过"：
 *   断言"滚动了"没有意义 —— 真正该断言的是 `aria-activedescendant`
 *   指向了正确的那一条（读屏器读的是它，回车执行的也是它）。
 *   滚动只是视觉跟随，滚没滚不影响正确性，所以这里只要不抛异常。
 */
if (
  typeof window !== 'undefined' &&
  typeof window.Element.prototype.scrollIntoView !== 'function'
) {
  Object.defineProperty(window.Element.prototype, 'scrollIntoView', {
    writable: true,
    configurable: true,
    value: () => {},
  })
}

/**
 * ★ 每个用例后清空 DOM —— RTL 的自动清理在本项目**从来没生效**。
 *
 * `@testing-library/react` 的自动清理靠模块顶层那句
 * `if (typeof afterEach === 'function') afterEach(cleanup)`；
 * 而 `vitest.config.ts` 明确写了 `globals: false`，那些全局函数根本不存在，
 * 于是它静默跳过了注册（`process.env.RTL_SKIP_AUTO_CLEANUP` 也没人设）。
 *
 * 症状极具误导性：同一文件里第二次 `render()` 之后，DOM 里同时躺着两次
 * 渲染的节点，`getByText('x')` 报
 * `Found multiple elements with the text: x` ——
 * 看起来像夹具写重了或组件重复渲染，其实是上一个用例没清理。
 * `ToolMention.test.tsx` 就因此误报过 3 次（标题/说明文字互相撞车），
 * 组件本身一行没错。
 *
 * 为什么是 `afterEach` 而不是 `beforeEach`：某个用例失败时，DOM 能保留到
 * 下一个用例开始前才被清掉，调试时还看得到现场。
 * 为什么在这里而不是让每个测试文件自己写：漏写一个文件就退回到上面那种
 * 假 bug，而排查一次要花掉半小时 —— 默认就对，比记得写更可靠。
 */
afterEach(() => {
  cleanup()
})

