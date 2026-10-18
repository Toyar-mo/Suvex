import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'

/*
  ─────────────────────────────────────────────────────────────
  前端测试配置（2026-10-10 新增）
  ─────────────────────────────────────────────────────────────

  ## 为什么单独一个文件，而不是往 `vite.config.ts` 里加 `test` 段

  `vite.config.ts` 里有大量**只对开发服务器成立**的开关（allowedHosts、
  HMR、代理、watcher 忽略规则），每一个都带着长注释解释它的历史原因。
  把测试配置混进去，会让那份文件同时承担两件事，而改任何一边都要
  重新读一遍另一边的约束。

  这里只需要两样东西：`@/` 别名（与源码一致）和 jsdom 环境。
  重复声明一次别名是可接受的代价 —— 换来的是"改开发服务器配置不会
  影响测试"。

  ⚠️ 如果哪天 `@/` 的目标变了，**两处都要改**。这条注释就是提醒。

  ## 为什么环境是 jsdom 而不是 node

  `useActivityStream` 用到 `setTimeout`、store 用 `window.setTimeout`
  （见 `lib/api.ts` 的超时实现）。node 环境下 `window` 不存在，
  测试会因为"window is not defined"而失败 —— 那个报错看起来像
  被测代码的 bug，其实只是环境选错了。

  纯逻辑模块（如 `scans-store` 的并发去重）其实不需要 DOM，
  但用同一个环境可以少一层心智负担。
*/
export default defineConfig({
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    // 测试文件与源码放在一起（`*.test.ts`），而不是单独的 tests/ 目录。
    // 理由：改动某个模块时，它的测试就在旁边，不容易忘记同步。
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    /*
      全局前置：补 jsdom 没实现的浏览器 API（目前只有 `matchMedia`）。

      少了它，任何真的渲染到 `StatCard` / 侧栏 / 主题逻辑的测试都会炸在
      `window.matchMedia is not a function` —— 那个报错看起来像被测组件的
      bug，其实是环境缺件。详见 `src/test/setup.ts` 的文件头。
    */
    setupFiles: ['./src/test/setup.ts'],
    /*
      ★ 不开 `globals: true`。

      那会把 `describe` / `it` / `expect` 注入全局，于是测试文件里
      看不到它们从哪来 —— 而 TypeScript 那边还要额外配 `types` 才能
      通过类型检查。显式 `import { describe, it, expect } from 'vitest'`
      只多一行，但让依赖关系可见，也省掉一处 tsconfig 改动。
    */
    globals: false,
    /*
      还原 mock：每个用例之后自动 `vi.restoreAllMocks()`。
      少了它，一个用例里替换的 `fetch` 会泄漏到下一个用例，
      症状是"单独跑能过、一起跑就挂"—— 这类失败最难查。
    */
    restoreMocks: true,
  },
})
