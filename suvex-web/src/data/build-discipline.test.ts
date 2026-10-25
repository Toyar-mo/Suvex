import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

/**
 * 构建纪律：产物必须来自类型检查过的代码。
 *
 * ## 这条测试防的是什么
 *
 * 2026-10-25（v0.90）真实事故：`SkillEditor.tsx` 里 `BODY_KEY` 和 `keyOf`
 * 一共五个引用从未定义，技能页整页被错误边界接住。
 *
 * 上线路径是 `npx vite build` —— esbuild 逐文件转译，不做跨作用域检查，
 * 未定义标识符原样进了产物，直到浏览器执行到那一行才炸。而
 * `package.json` 的 `build` 是 `tsc -b && vite build`，那五个 TS2304
 * 本来会被当场拦下。
 *
 * **同一个仓库、同一个产物目录，两条构建路径的检查强度不同。**
 *
 * 所以在 `vite.config.ts` 里挂了 `requireTypecheck()` 插件（见
 * `build/require-typecheck.ts`）：无论谁调起 `vite build`，都先跑类型检查。
 *
 * ## 为什么这些断言是"读文件"而不是"跑一次构建"
 *
 * 跑真构建要 20 秒以上，放进单测会让 `npm test` 从秒级变成分钟级，
 * 而那条纪律的**失效方式**是「有人删掉了插件」或「有人把 tsc 换成
 * 一句 console.warn」—— 两者都表现为**源码里少了一行/改了一个参数**，
 * 读文件就能拦住。
 *
 * 真构建的验证放在反向验证脚本里（改坏 `SkillEditor.tsx` 加一个未定义
 * 标识符，断言 `npm run build` 与 `npx vite build` **都**失败）。
 */

/*
  ## 为什么用 `process.cwd()` 而不是 `import.meta.url`

  与 `src/lib/design-tokens.test.ts` 同一原因：经过 vite 转换后
  `import.meta.url` 不是 `file:` 协议，`fileURLToPath` 会抛
  `TypeError: The URL must be of scheme file`。`vitest run` 的工作目录
  就是包根，用 cwd 拼最稳。

  ⚠️ 拼错了必须**当场炸**，不能退化成「读到空字符串 → 断言全过」的假绿。
  `read()` 里对内容长度做了下界，`vite.config.ts` 尤其不可能只有几十字节。
*/
const WEB_ROOT = process.cwd()

function read(rel: string): string {
  const full = join(WEB_ROOT, rel)
  const text = readFileSync(full, 'utf8')
  // 读错路径会直接抛 ENOENT；但读到一个"几乎空"的文件不会。
  // 这里再拦一道，避免将来把路径前缀改错而静默变绿。
  expect(text.length, `${full} 内容异常短，可能读错了文件`).toBeGreaterThan(200)
  return text
}

/**
 * 去掉注释，只留下真正会被执行的代码。
 *
 * 为什么需要：`// requireTypecheck(),` 里依然含有 `requireTypecheck`，
 * 用 `toContain` 检查源码等于把「被注释掉」当成「已启用」。反向验证
 * 实测过：把插件那行注释掉，旧断言全绿 —— 插件早就没了，测试还说它在。
 *
 * 只处理 `//` 行注释与 `/* *\/` 块注释，不追求完整词法分析：这两个
 * 文件里没有字符串内的 `//`，而将来真有了，误剥会表现为断言变红
 * （需要人来看一眼），不会表现为静默变绿。
 */
function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/.*$/gm, '$1')
}

describe('构建纪律：vite build 必须先过类型检查', () => {
  it('vite.config.ts 真的挂了 requireTypecheck 插件（不是注释掉的那行）', () => {
    const code = stripComments(read('vite.config.ts'))
    expect(code).toContain('requireTypecheck')
    // 插件实例化（只 import 不调用，等于没有防线）
    expect(code).toMatch(/requireTypecheck\s*\(\s*\)/)
    // 而且必须在 plugins 数组里被调用 —— 单独一行调用、结果丢掉也没用
    expect(code).toMatch(/plugins:\s*\[[\s\S]*requireTypecheck\s*\(\s*\)[\s\S]*\]/)
  })

  it('插件的类型检查命令是 tsc -b（不是 --noEmit，也不是注释）', () => {
    const code = stripComments(read('build/require-typecheck.ts'))
    expect(code).toContain("export const TYPECHECK_COMMAND = 'tsc'")
    expect(code).toContain('export const TYPECHECK_ARGS')
    expect(code).toMatch(/TYPECHECK_ARGS\s*=\s*\['-b'\]/)
  })

  it('插件只在 build 生效（dev 下不该被全量类型检查拖慢）', () => {
    const code = stripComments(read('build/require-typecheck.ts'))
    expect(code).toMatch(/apply:\s*'build'/)
  })

  it('类型检查失败时抛错中止构建，而不是只警告', () => {
    const code = stripComments(read('build/require-typecheck.ts'))
    // 关键点：catch 里必须 throw。
    // 只在 catch 里"某处有 throw"不够 —— 前面加一行 console.warn + return
    // 就绕过去了（反向验证实测过这条假绿）。所以要求 catch 块里
    // **没有任何 return / continue / process.exit(0)**，只有 throw 一条出口。
    expect(code).toMatch(/catch\s*\{[\s\S]*?throw new Error/)
    const catchBlock = code.match(/catch\s*\{([\s\S]*?)\n\s*\}\s*,?\s*\)?\s*\}?\s*$/m)
    const body = catchBlock ? catchBlock[1] : code.slice(code.indexOf('catch'))
    expect(body, 'catch 块里出现了 return —— 会让失败变成"只警告"').not.toMatch(
      /\breturn\b/,
    )
    expect(body, 'catch 块里出现了 exit(0) —— 会伪装成构建成功').not.toMatch(
      /exit\s*\(\s*0\s*\)/,
    )
    expect(body, 'catch 块里出现了 console.warn —— 失败被降级成提示').not.toMatch(
      /console\.warn/,
    )
  })

  it('package.json 的 build 仍然是 tsc -b && vite build（不是裸 vite build）', () => {
    const pkg = JSON.parse(read('package.json')) as { scripts: Record<string, string> }
    expect(pkg.scripts.build).toBe('tsc -b && vite build')
  })

  it('tsc 用同步调用（异步会让 Vite 继续打包出坏 dist）', () => {
    const code = stripComments(read('build/require-typecheck.ts'))
    expect(code).toContain('execFileSync')
    // 不能出现异步的 exec / spawn
    expect(code).not.toMatch(/\bexecFile\(/)
    expect(code).not.toMatch(/\bexec\(/)

    // ★ buildStart 本身也不能是 async：Vite 会 await plugin hook，
    //   但异步 hook 里套同步 execFileSync 不解决问题 —— 反向验证实测过，
    //   把 buildStart 改成 `async buildStart` 再 `setTimeout(runTypecheck, 0)`，
    //   构建会在 tsc 跑完之前就打完包，坏产物照样进 dist。
    expect(code, 'buildStart 被写成了 async —— 构建可能先于类型检查完成').not.toMatch(
      /async\s+buildStart/,
    )
    // 也不能把类型检查推迟到定时器/微任务里
    expect(code, 'runTypecheck 被推迟执行了').not.toMatch(
      /setTimeout\s*\(\s*runTypecheck/,
    )
    expect(code, 'runTypecheck 被推迟执行了').not.toMatch(
      /setImmediate\s*\(\s*runTypecheck/,
    )
  })

  it('tsc 的输出必须直通终端（否则用户不知道是哪个标识符坏了）', () => {
    const code = stripComments(read('build/require-typecheck.ts'))
    // stdio: 'inherit' 是这条纪律唯一的信息出口。
    // 改成 'ignore' 构建照样会失败，但报错只剩"构建失败"一句 ——
    // 而修复需要的正是 tsc 报的那行 TS2304。
    expect(code).toMatch(/stdio:\s*'inherit'/)
  })
})
