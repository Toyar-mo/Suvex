import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

/*
  ─────────────────────────────────────────────────────────────
  裸色的防回归判据：源码里不许再出现 `text-white` / `text-black`
  ─────────────────────────────────────────────────────────────

  ## 为什么要有这条判据

  2026-10-17 收口时发现 7 个手写的实心主色按钮用 `text-white` 当字色
  （`AccountPanel` ×3、`PlatformPanel` ×2、`SaveBar`、`UsersPanel`）。
  暗色主题下白字压在 `accent`（#8B7CF6）上只有 **3.33:1**，hover 到
  `accent-strong` 更差（**2.38:1**）——都不到 AA 的 4.5:1。亮色主题下
  这行代码"看起来没事"（白字压 #5B4BC4 是 6.46:1），所以静态看代码
  永远看不出来，只有按主题逐个算对比度才会暴露。

  正确写法是 `text-canvas`：它是"画布底的反色"，暗色取近黑（对 `accent`
  5.98:1、对 `accent-strong` 8.36:1），亮色取近白（对 `accent` 6.46:1）。
  同一个类名在两种主题下都成立，这才是 token 的意义。

  ## 为什么不写在 eslint 规则里

  `no-restricted-syntax` 要匹配 JSX 属性里的字符串拼接，写起来比这条
  扫描更脆；而且这类"配色纪律"跟项目里已有的 `Button.test.tsx` 断言
  （禁止把 `text-canvas` 改回 `text-white`）是同一族，放在测试里更集中。

  ## 判据自己也要被验证（见下面的 `扫描器` 分组）

  只写"expect(结果为空)"有个经典陷阱：文件没读到、正则写错、注释剥多了，
  都会让结果**恰好**为空而永远绿。所以这里先用一段**合成样本**验证扫描器
  本身：代码里的裸色要被抓到，注释里提到的不许误报。
*/

/*
  ## 为什么用 `process.cwd()` 而不是 `import.meta.url`

  第一版用 `fileURLToPath(new URL('../', import.meta.url))`，在 vitest 里直接炸：
  `TypeError: The URL must be of scheme file` —— 经过 vite 转换后 `import.meta.url`
  不是 `file:` 协议。`vitest run` 的工作目录就是包根，用 cwd 拼最稳，
  拼错了下面 `existsSync` 会立刻报出来，不会退化成"零文件 → 零违规"的假绿。
*/
const PKG_ROOT = process.cwd()
const SRC_DIR = join(PKG_ROOT, 'src')
const SCANNED = /\.(ts|tsx)$/
const BARE_COLOR = /\btext-(?:white|black)\b/g

/** 去掉块注释与行注释 —— 注释里写"别用 `text-white`"是正当的，不该被抓。 */
function stripComments(code: string): string {
  return code
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

/** 递归列出 src 下的 .ts/.tsx，**跳过测试文件**（理由见下面 it 里的注释）。 */
function listSources(dir: string, acc: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules') continue
      listSources(full, acc)
    } else if (SCANNED.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      acc.push(full)
    }
  }
  return acc
}

/** 返回 [文件:类名] 的违规清单；纯函数，便于用合成样本验证它真的会报。 */
function findBareColors(files: { path: string; code: string }[]): string[] {
  const hits: string[] = []
  for (const f of files) {
    for (const m of stripComments(f.code).matchAll(BARE_COLOR)) {
      hits.push(`${f.path}: ${m[0]}`)
    }
  }
  return hits
}

describe('裸色判据（text-white / text-black）', () => {
  it('★ 扫描器自身有效：代码里的裸色会被抓到，注释里的不会误报', () => {
    const sample = [
      'const a = "bg-accent text-xs text-white"', // 代码：必须抓到
      '/* 历史记录：这里原来写的是 `text-white` */', // 注释：不该报
      '// 也不要写 text-black', // 行注释：不该报
      'const b = "text-canvas"', // 正确写法：不该报
      'const url = "https://example.com/x"', // 行注释剥除不能把 URL 之后吃掉
      'const c = "text-black"', // 代码：必须抓到
    ].join('\n')
    expect(findBareColors([{ path: 'sample.tsx', code: sample }])).toEqual([
      'sample.tsx: text-white',
      'sample.tsx: text-black',
    ])
  })

  it('src 下所有 .ts/.tsx 都没有裸 text-white / text-black', () => {
    /*
      为什么跳过 `*.test.ts(x)`：`Button.test.tsx` 里写着
      `expect(cls).not.toContain('text-white')` —— 测试文件**必须**能提到这个名字
      才写得出反向断言，按字符串扫描会把正当用法一起判红。被管的是产出代码。
    */
    expect(existsSync(SRC_DIR)).toBe(true)
    const files = listSources(SRC_DIR).map((path) => ({
      path: relative(SRC_DIR, path).replace(/\\/g, '/'),
      code: readFileSync(path, 'utf8'),
    }))
    // 先证明确实读到了源码，避免"零文件 → 零违规"的假绿
    expect(files.length).toBeGreaterThan(100)
    expect(files.some((f) => f.code.includes('text-canvas'))).toBe(true)
    expect(findBareColors(files)).toEqual([])
  })
})
