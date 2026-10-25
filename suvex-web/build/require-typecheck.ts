/*
  ─────────────────────────────────────────────────────────────
  产物必须来自类型检查过的代码
  ─────────────────────────────────────────────────────────────

  ## 为什么要有这个插件

  2026-10-25（v0.90）发生过一次线上事故：`SkillEditor.tsx` 里
  `BODY_KEY` 和 `keyOf` 一共五个引用从未定义，技能页整页被错误边界接住。

  它能上线，是因为部署时手敲的是：

      npx vite build        ← esbuild 逐文件转译，不做跨作用域检查

  而 `package.json` 里的 `build` 是：

      tsc -b && vite build  ← 类型检查先跑，五个 TS2304 会当场拦下

  同一个仓库、同一个产物目录，两条构建路径的**检查强度不同**。
  esbuild 只负责把 TS 擦成 JS，未定义标识符原样进产物，直到浏览器
  执行到那一行才炸 —— 而那时它已经是「已部署的线上缺陷」了。

  ## 为什么不在 package.json 里做

  `build` 脚本本身没错，错在「绕过它照样能产出 dist」。所以防线必须
  落在**`vite build` 自己身上**：无论谁、从哪条路径调起构建，都先过一遍
  类型检查。写成 `prebuild` 只对 `npm run build` 生效，`npx vite build`
  依旧绕得过去 —— 那正是事故发生的那条路径。

  ## 关键词守卫

  本文件里刻意出现 `tsc -b` 字样，是为了让
  `src/data/build-discipline.test.ts` 能断言「守卫仍以类型检查为前提」，
  而不是被谁悄悄换成一句 `console.warn`。
*/

import { execFileSync } from 'node:child_process'
import { fileURLToPath, URL } from 'node:url'

import type { Plugin } from 'vite'

/** 仓库根（`vite.config.ts` 所在目录）。tsc 必须在它下面跑，否则找不到 tsconfig。 */
export const WEB_ROOT = fileURLToPath(new URL('..', import.meta.url))

/**
 * 类型检查命令。
 *
 * 用 `-b`（build 模式）而不是 `--noEmit`：本项目是 project references
 * （`tsconfig.json` 里 `references` 指向 `tsconfig.app.json` 与
 * `tsconfig.node.json`），只有 `-b` 会真正沿着 references 递归检查，
 * 且带增量信息（`.tsbuildinfo`），第二次起很快。
 */
export const TYPECHECK_COMMAND = 'tsc'
export const TYPECHECK_ARGS = ['-b'] as const

/**
 * 同步跑一次类型检查。失败就抛 —— 让 `vite build` 以非零码退出。
 *
 * ★ 必须同步且 `stdio: 'inherit'`：
 *   异步的话 Vite 会继续往下打包，dist 里仍是坏产物；
 *   不继承 stdio 的话，用户只看到一句「构建失败」，看不到是哪一行哪个
 *   标识符 —— 而那正是修复需要的全部信息。
 */
export function runTypecheck(): void {
  execFileSync(TYPECHECK_COMMAND, [...TYPECHECK_ARGS], {
    cwd: WEB_ROOT,
    stdio: 'inherit',
    // Windows 上 tsc 是 .cmd，必须走 shell 才能被解析到
    shell: process.platform === 'win32',
  })
}

/** 构建前强制类型检查的插件。只在 `vite build` 生效，不影响 `vite`（dev）。 */
export function requireTypecheck(): Plugin {
  return {
    name: 'suvex:require-typecheck',
    // 只要 build —— dev 下不做全量类型检查，那是编辑器和 CI 的事
    apply: 'build',
    // 用 buildStart 而不是 configResolved：前者在真正开始写 dist 之前
    buildStart() {
      try {
        runTypecheck()
      } catch {
        // 不打印 tsc 的输出（已经经 inherit 打到终端了），只补一句
        // 说明后果 —— 否则用户会以为「构建脚本自己坏了」。
        throw new Error(
          '类型检查未通过，已中止构建。\n' +
            '  dist 没有被写入 —— 这是故意的：未定义标识符能通过 esbuild 转译，\n' +
            '  会变成线上才炸的缺陷（v0.90 的 SkillEditor.tsx 就是这样上线的）。',
        )
      }
    },
  }
}
