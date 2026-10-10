/**
 * 契约一致性检查 —— 把 `docs/api-contract.md` 里声明的字段/枚举取值
 * 与前端真实源码逐条对账。
 *
 *   npm run verify:contract
 *
 * ★ 为什么需要它（这是本项目的既有教训，不是过度设计）：
 *
 *   契约文档是**手写**的，而它声明"字段从源码逐字提取"。
 *   手抄就会有错 —— 本项目已经栽过好几次"文档与实现漂移"：
 *      · 品牌改名后忘了同步文档里的 token 前缀示例（`strix_` → `svx_`）；
 *      · 动效 token 名与代码里的类名不一致，动画静默失效。
 *   这类问题的共同点是**不报错**，只是文档和代码说的不是一回事。
 *
 *   所以"字段与源码一致"这条声明必须被机器验证，而不是靠人保证。
 *   而且这份契约接下来要被 8 个后端任务依赖 —— 它错一个字段名，
 *   后面全部返工。
 *
 * 检查内容：
 *   1. 各枚举类型的取值集合，是否全部出现在契约里；
 *   2. 两种 ActivityEventType（完整 8 种 / Overview 投影 6 种）的差异描述是否准确；
 *   3. 状态流转表是否与 FINDING_STATUS_TRANSITIONS 一致。
 *
 * 实现要点：**直接读源码文本解析**，不 import。因为这些是 `.ts` 类型
 * （编译后不存在），而且本项目用的是 `@/` 别名，node 不认识。
 * 纯文本解析虽然朴素，但对"枚举取值"这种东西足够可靠 —— 而且没有依赖。
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'
import { join } from 'node:path'

const ROOT = fileURLToPath(new URL('..', import.meta.url))

const source = (rel) => readFileSync(join(ROOT, rel), 'utf8')

/*
  ★ 契约文本**先归一化空白再匹配**。

  被实测打出来的：第一版直接匹配原文，而 Prettier 会对齐 Markdown 表格列宽，
  于是 `| GET | \`/providers\`` 变成 `| GET    | \`/providers\`` ——
  一整排检查全部误报。**检查匹配了排版而不是内容。**

  归一化之后，一次无害的格式化不会再让它变红。凡是"用字面串匹配结构化文本"
  的检查都要先压空白，否则它每次都会因为格式变化而失去可信度。
*/
const contract = source('docs/api-contract.md').replace(/[ \t]+/g, ' ')

/**
 * 从 `export type X = 'a' | 'b' | ...` 里抽出取值集合。
 *
 * ⚠️ 第一版用了一个跨行的贪婪正则，遇到 `ActivityEventType` 时**越过它自己的
 * 结尾**，一路吃进下面 `type WithSeverity = { severity?: 'critical' | ... }`
 * 的取值，报出 13 个值 —— 而源码里明明是 8 个。
 * 一个会**误报**的检查比没有检查更坏：它会让人去"修"本来正确的东西。
 *
 * 正确做法：按行读，遇到"非空且不以 `|` 开头"的行就停 ——
 * 那是下一个声明的开始。
 */
function unionValues(code, typeName) {
  const lines = code.split('\n')
  const start = lines.findIndex((l) => new RegExp(`^export type ${typeName}\\s*=`).test(l))
  if (start === -1) return null

  const values = []
  for (let i = start; i < lines.length; i++) {
    const trimmed = lines[i].trim()
    if (i > start) {
      // 空行与注释行不算结束
      const isComment =
        trimmed.startsWith('*') || trimmed.startsWith('/*') || trimmed.startsWith('//')
      if (trimmed !== '' && !trimmed.startsWith('|') && !isComment) break
    }
    for (const m of lines[i].matchAll(/'([a-z_]+)'/g)) values.push(m[1])
  }
  return values
}

let failures = 0
const fail = (msg) => {
  console.log(msg)
  failures += 1
}
const pass = (msg) => console.log(msg)

// ─────────────────────────────────────────────
// 1 · 枚举取值对账
// ─────────────────────────────────────────────

/** [源文件, 类型名] */
const ENUM_CHECKS = [
  ['src/data/scan.ts', 'ScanStatus'],
  ['src/data/scan.ts', 'TargetKind'],
  ['src/data/scan.ts', 'SkillId'],
  ['src/data/finding.ts', 'Verification'],
  ['src/data/finding.ts', 'FindingStatus'],
  ['src/data/finding.ts', 'FindingCategory'],
  ['src/data/activity.ts', 'AgentRole'],
  ['src/data/activity.ts', 'AgentStatus'],
  ['src/data/overview.ts', 'AgentPhase'],
  ['src/data/settings.ts', 'TokenScope'],
]

console.log('══ 枚举取值对账（源码 → 契约）══\n')

for (const [file, typeName] of ENUM_CHECKS) {
  const values = unionValues(source(file), typeName)
  if (!values || values.length === 0) {
    fail(`❌ ${typeName.padEnd(16)} 无法从 ${file} 解析（解析器需要更新？）`)
    continue
  }
  const missing = values.filter((v) => !contract.includes(v))
  if (missing.length === 0) {
    pass(`✓  ${typeName.padEnd(16)} ${values.length} 个取值都在契约里`)
  } else {
    fail(`❌ ${typeName.padEnd(16)} 契约缺: ${missing.join(', ')}`)
  }
}

// ─────────────────────────────────────────────
// 2 · 两种事件类型的差异
// ─────────────────────────────────────────────

console.log('\n══ 事件类型：完整 vs Overview 投影 ══\n')

const full = unionValues(source('src/data/activity.ts'), 'ActivityEventType')
const projection = unionValues(source('src/data/overview.ts'), 'ActivityEventType')

if (!full || !projection) {
  fail('❌ 无法解析两种 ActivityEventType')
} else {
  console.log(`完整事件（data/activity.ts）  : ${full.length} 种`)
  console.log(`Overview 投影（data/overview.ts）: ${projection.length} 种`)

  const onlyFull = full.filter((v) => !projection.includes(v))
  const onlyProjection = projection.filter((v) => !full.includes(v))

  console.log(`\n只在完整事件里: ${onlyFull.join(', ') || '(无)'}`)
  console.log(`只在投影里    : ${onlyProjection.join(', ') || '(无)'}\n`)

  /*
    契约里明确断言："完整 8 种 / 投影 6 种 / 少的正是 reasoning 与 failed"。
    这条断言是给后端看的（它决定后端只发完整事件、前端自行投影），
    所以它必须准确 —— 断言错了会让后端少发两类事件。
  */
  const claimOk =
    full.length === 8 &&
    projection.length === 6 &&
    onlyProjection.length === 0 &&
    onlyFull.length === 2 &&
    onlyFull.includes('reasoning') &&
    onlyFull.includes('failed')

  if (claimOk) {
    pass('✓  契约的断言成立：完整 8 种、投影 6 种，少的正是 reasoning 与 failed')
  } else {
    fail('❌ 契约对两种事件类型的差异描述已过期，需要更新 §2.5')
  }

  const missingTypes = full.filter((v) => !contract.includes(v))
  if (missingTypes.length === 0) pass('✓  8 种事件类型全部出现在契约中')
  else fail(`❌ 契约缺事件类型: ${missingTypes.join(', ')}`)
}

// ─────────────────────────────────────────────
// 3 · 状态流转表
// ─────────────────────────────────────────────

console.log('\n══ 状态流转表 ══\n')

const findingSrc = source('src/data/finding.ts')
const block = findingSrc.match(/FINDING_STATUS_TRANSITIONS[^=]*=\s*\{([\s\S]*?)\n\}/)

if (!block) {
  fail('❌ 无法解析 FINDING_STATUS_TRANSITIONS')
} else {
  const rows = [...block[1].matchAll(/(\w+):\s*\[([^\]]*)\]/g)]
  for (const [, from, raw] of rows) {
    const targets = [...raw.matchAll(/'([a-z_]+)'/g)].map((x) => x[1])
    // 契约里这一行以 `from:` 开头
    const line = contract.split('\n').find((l) => l.trim().startsWith(`${from}:`))
    const ok = line && targets.every((t) => line.includes(t))
    if (ok) {
      pass(`✓  ${from.padEnd(16)} → ${targets.join(', ')}`)
    } else {
      fail(`❌ ${from.padEnd(16)} → ${targets.join(', ')}   （契约里缺失或不符）`)
    }
  }
}

// ─────────────────────────────────────────────

console.log(
  `\n${failures === 0 ? '══ 契约与源码一致 ══' : `══ 发现 ${failures} 处不一致：请先改 docs/api-contract.md ══`}`,
)
if (failures > 0) process.exitCode = 1
