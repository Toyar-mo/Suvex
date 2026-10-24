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

import { createAudit } from './_audit.mjs'

const ROOT = fileURLToPath(new URL('..', import.meta.url))

/*
  `minChecks` = 6（枚举对账 6 条）+ 事件类型比较 + 状态流转表的每一行。

  ⚠️ 这个下限刻意取得**保守**（当前实际执行 20+ 条），只用来挡"清单被误删
  大半"这种事故。取精确值会让每次正常增删枚举都变红，红几次就没人看了。
*/
const audit = createAudit({ name: 'contract-parity', minChecks: 8 })

/**
 * 读一个源码文件。**读不到时给一句能照做的话**，而不是抛 ENOENT。
 *
 * ## 为什么这不只是"错误信息更友好"

 * v0.82 实测踩到：改名阶段把 `src/data/ctf2.ts` 改成了 `kernel.ts`，
 * 但本脚本的清单没跟上。于是整个校验**根本没跑起来**，退出码是 1，
 * 输出是一段 `ENOENT: no such file or directory` 的栈。
 *
 * 在 CI 里这表现为"校验红了"，而红的原因看起来像环境问题（文件缺失），
 * 于是最可能的处理是**跳过它**——那这一整个校验就白写了。
 *
 * 所以：文件不在时立刻说清"哪一行清单指向了一个不存在的文件、
 * 它可能被改名了"，并**指出这就是清单本身要修**。
 */
const source = (rel) => {
  try {
    return readFileSync(join(ROOT, rel), 'utf8')
  } catch (error) {
    if (error && error.code === 'ENOENT') {
      fail(
        `❌ 读取不到源文件：${rel}\n` +
          '   这不是环境问题 —— 多半是**文件被改名/移动了，而本脚本的清单没跟上**。\n' +
          '   请把 `ENUM_CHECKS` 里指向它的那一行改成新路径（并同步类型名）。',
      )
    }
    throw error
  }
}

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
    /*
      ★ 字符类里必须有 `-` 与 `0-9`（v0.21 修）。

      原来写的是 `'([a-z_]+)'` —— 它**匹配不到带连字符或数字的值**，
      而 `Ctf2SandboxProfile` 的取值正是 `ctf2-sandbox` /
      `ctf2-sandbox-re`。后果不是"少抓到一个值"，而是
      **整个枚举解析出 0 个值**，于是校验脚本报

          ❌ Ctf2SandboxProfile 无法从 src/data/ctf2.ts 解析

      那句提示把责任推给"解析器需要更新"，看起来像脚本的锅 ——
      而真相是这个正则的能力边界。两者混在一起时，
      真正的问题（契约缺值）会被这句误导性的提示盖住。

      加 `-` 与 `0-9` 之后 `ctf2-sandbox` 能正确解析。
    */
    for (const m of lines[i].matchAll(/'([a-z0-9_-]+)'/g)) values.push(m[1])
  }
  return values
}

let failures = 0
/*
  ★ 空检查防线（见 `_audit.mjs` 的文件头）：
    这两个包装函数**同时**计数，所以"判通过/失败"与"记一条检查"
    不可能脱节 —— 不存在"忘了计数但结论照出"的路径。
    收尾由 `audit.finish()` 断言 checks > 0。
*/
const fail = (msg) => {
  audit.check()
  console.log(msg)
  failures += 1
}
const pass = (msg) => {
  audit.check()
  console.log(msg)
}

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
  /*
    ★ v0.21 补的七个（2026-10-10）。

    加它们的原因是一个被量出来的漏洞：**这份清单只覆盖它被喂过的枚举**，
    没列进来的即使与契约漂移也**不会报红**。而契约已经漂了整整一个
    CTF2 模块（25 个端点、5 个枚举）都没人发现 —— 因为清单里没有它们。

    · `ScanMode` —— 任务 13 新增的 `quick`/`standard`/`deep`，
      此前不在清单里（调研时明确核出"不会报红"）；
    · 五个 `Kernel*` —— 整个 CTF 模块的枚举，此前零覆盖。

    ⚠️ **v0.82 修过一次静默失效**：改名阶段（`ctf2.ts` → `kernel.ts`、
    `Ctf2*` → `Kernel*`）没有同步这张清单，于是它一直在读一个**不存在的
    文件**。而失败方式是 `ENOENT` 抛异常退出 —— 不是"报红说契约错了"，
    而是脚本根本没跑起来。**一个坏掉的检查比没有检查更糟**：
    CI 里它会红，但红的原因看起来像环境问题，于是容易被人跳过。

    所以这里同时做了两件事：修正路径与类型名；以及把 `source()` 的
    读取失败变成一条**说明清楚**的失败（见下面 `source()` 的实现）。

    ⚠️ 这份清单的维护规则：**新增一个枚举就要加一行**。
       漏加的症状是"校验全绿而契约已经错了"—— 比没有校验更危险，
       因为它会让人以为查过了。
  */
  ['src/data/settings.ts', 'ScanMode'],
  ['src/data/kernel.ts', 'KernelChallengeStatus'],
  ['src/data/kernel.ts', 'KernelSubmissionStatus'],
  ['src/data/kernel.ts', 'KernelEnvironmentStatus'],
  ['src/data/kernel.ts', 'KernelSubmitMode'],
  ['src/data/kernel.ts', 'KernelSandboxProfile'],
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

audit.finish({
  failures,
  okMessage: '══ 契约与源码一致 ══',
  failMessage: `══ 发现 ${failures} 处不一致：请先改 docs/api-contract.md ══`,
})
