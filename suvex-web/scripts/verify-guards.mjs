/**
 * 验证「对账脚本的空检查防线」本身是有效的。
 *
 *   npm run verify:guards
 *
 * ══════════════════════════════════════════════════════════════
 * 为什么需要一个"检查检查的脚本"
 * ══════════════════════════════════════════════════════════════
 *
 * `_audit.mjs` 的存在是为了挡住一种失效：**脚本一条检查都没跑，
 * 却输出"全部通过"并返回 0**。项目里真发生过（`endpoint-parity.mjs`
 * 的注释记着：路径少写一层 `app/`，扫到 0 个端点却报绿）。
 *
 * 但 `_audit.mjs` 自己也可能被改错 —— 而它的失效方式是**静默**的：
 * 如果 `finish()` 忘了设 `process.exitCode = 1`，那四个脚本会**看起来
 * 仍然正常工作**（它们平时本来就该报绿），只有在真正空了的那天才暴露。
 *
 * 所以这里做**变异验证**：构造三个必然触发保护的小场景，断言它们
 * 确实以非零码退出。这不是重复测试 `_audit.mjs` 的实现细节，
 * 而是断言"这道防线真的会拦住东西"。
 *
 * ★ 这与项目里既有的变异验证是同一条纪律（见 `suvex-web/README.md`：
 *   "这套测试做过突变验证——把关键逻辑逐个破坏，确认对应测试变红"）。
 *   一个从未被证明会变红的检查，和没有检查是同义的。
 *
 * ⚠️ 本脚本自身也走 `createAudit` —— 它同样有"一条都没查就报绿"的风险。
 */

import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath, URL } from 'node:url'

import { createAudit } from './_audit.mjs'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const AUDIT_PATH = join(ROOT, 'scripts', '_audit.mjs')

const audit = createAudit({ name: 'verify-guards', minChecks: 5 })

/**
 * 跑一段用 `_audit.mjs` 的临时脚本，返回它的退出码。
 *
 * ⚠️ 用真子进程而不是 import 后捕获异常：`finish()` 的设计**就是**
 * 通过 `process.exitCode` 表达结果。用 import 只能验返回值，
 * 验不到"CI 会不会红"——而"CI 会不会红"正是这里唯一关心的事。
 */
function runProbe(source) {
  const dir = mkdtempSync(join(tmpdir(), 'suvex-guard-'))
  try {
    const file = join(dir, 'probe.mjs')
    writeFileSync(file, source, 'utf8')
    execFileSync(process.execPath, [file], { stdio: 'pipe' })
    return 0
  } catch (error) {
    return typeof error.status === 'number' ? error.status : 1
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

/** 生成一个导入 `_audit.mjs` 的探针脚本。`auditRel` 必须是绝对 file URL。 */
function probe(body) {
  const importUrl = `file://${AUDIT_PATH.replace(/\\/g, '/')}`
  return `import { createAudit } from ${JSON.stringify(importUrl)}\n${body}\n`
}

let failures = 0
function expect(condition, label, detail = '') {
  audit.check()
  if (condition) {
    console.log(`  ✓ ${label}`)
  } else {
    console.log(`  ✗ ${label}${detail ? `  ← ${detail}` : ''}`)
    failures += 1
  }
}

console.log('══ 空检查防线的变异验证 ══\n')

// ── 1. 正常情况：查了、没失败 → 必须绿 ──
//
// 这一条是**对照组**。没有它，下面三条即使全部通过也无法说明问题
// —— 一个"永远返回 1"的实现同样能过那三条。
{
  const code = runProbe(
    probe(`
const audit = createAudit({ name: 'ok', minChecks: 2 })
audit.check(); audit.check(); audit.check()
audit.finish({ failures: 0, okMessage: 'ok' })
`),
  )
  expect(code === 0, '正常通过时退出码为 0（对照组）', `实际 ${code}`)
}

// ── 2. 一条检查都没跑 → 必须红 ──
{
  const code = runProbe(
    probe(`
const audit = createAudit({ name: 'empty', minChecks: 1 })
for (const item of []) audit.check()
audit.finish({ failures: 0, okMessage: '全部通过' })
`),
  )
  expect(code !== 0, '★ 零检查必须非零退出（核心防线）', `实际 ${code}`)
}

// ── 3. 检查数低于下限 → 必须红 ──
{
  const code = runProbe(
    probe(`
const audit = createAudit({ name: 'shrunk', minChecks: 10 })
for (let i = 0; i < 3; i++) audit.check()
audit.finish({ failures: 0, okMessage: '全部通过' })
`),
  )
  expect(code !== 0, '★ 检查数低于 minChecks 必须非零退出', `实际 ${code}`)
}

// ── 4. 有失败 → 必须红（与"空"是两条独立路径） ──
{
  const code = runProbe(
    probe(`
const audit = createAudit({ name: 'fail', minChecks: 1 })
audit.check()
audit.finish({ failures: 1, failMessage: '发现问题' })
`),
  )
  expect(code !== 0, '有失败时必须非零退出', `实际 ${code}`)
}

// ── 5. ★ minChecks <= 0 必须被拒绝 ──
//
// ## 这条是**变异验证抓出来的**，不是想出来的
//
// 第一版 `finish()` 只有两道判断：`checks === 0` 与 `checks < minChecks`。
// 我把前者改成 `if (false)`（模拟有人删掉防线）再跑本脚本 ——
// **它照样绿**。因为 `checks < minChecks` 把零检查也兜住了（0 < 1 成立）。
//
// 也就是说：那道"零检查"防线是否有效，**取决于 minChecks 恰好 > 0**，
// 而那是一个调用方随手能改的数字。改成 0 就全废了。
//
// 现在 `finish()` 显式拒绝 minChecks <= 0，让"下限"这个参数不能成为
// 绕过零检查的通道。这一条盯的就是那个拒绝。
{
  const code = runProbe(
    probe(`
const audit = createAudit({ name: 'bad-min', minChecks: 0 })
for (let i = 0; i < 5; i++) audit.check()
audit.finish({ failures: 0, okMessage: '全部通过' })
`),
  )
  expect(code !== 0, '★ minChecks=0 必须被拒绝（否则零检查防线可被绕过）', `实际 ${code}`)
}

audit.finish({
  failures,
  okMessage: '══ 防线有效：零检查与规模萎缩都会红 ══',
  failMessage: `══ ${failures} 条防线失效：空检查会被当成通过 ══`,
})
