/*
  ══════════════════════════════════════════════════════════════
  反向端点对账：**从后端代码抽端点，反查契约文档**
  ══════════════════════════════════════════════════════════════

  ## 为什么需要它（与 contract-coverage.mjs 的分工）

  `contract-coverage.mjs` 里的 `REQUIRED_ENDPOINTS` 是**手写清单** ——
  它只能防"忘了写"，防不住"写的时候没想到"。而这个项目刚吃过一次：

    整个 CTF2 模块（25 个端点）从来没进过那份清单，
    所以它即使从契约里**整个消失**，校验也不报红 ——
    而它已经真的消失过一次，直到 2026-10-10 做交付评估时才被发现。

  手写清单的覆盖度等于"写清单的人当时的记忆"，而记忆会漏。
  这个脚本换一个方向：**代码是事实，契约必须跟上它**。
  它遍历各模块的 router.py，抽全部端点，逐个反查契约里有没有。

  ⚠️ 写这个文件时我踩了**同一个坑的第三次**：上面那句原本写成
     "app 斜杠星号斜杠 router.py"，而 `*` 加 `/` 会**提前闭合块注释** ——
     报错位置指向几十行之后（`SyntaxError: Unexpected token '{'`），
     看起来像语法写错了。前两次分别在 `.gitignore` 与
     `eslint.config.js` 的注释里。**块注释里不要写通配路径。**

  ## 判据

  端点路径在契约里出现即可（不要求特定格式）——
  因为契约里同一个端点可能出现在端点表、变更记录、示例等多个位置，
  苛求格式会让这个检查变成"格式警察"而不是"漏写探测器"。

  路径里的 `{id}` / `{scanId}` 这类**参数名在各处不统一**
  （后端用 `scanId`、契约表里有的写 `{id}`），所以比对时把
  花括号参数归一化成空花括号。不这么做的话会有几十个假阳性 ——
  而假阳性多了，这个检查就会被人忽略（那比没有检查更糟）。
*/

import { readFileSync, readdirSync, existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const webRoot = join(here, '..')
/*
  ⚠️ 必须指到 `suvex-api/app`，不是 `suvex-api`。

  第一版写成 `join(webRoot, '..', 'suvex-api')` —— 少了一层 `app/`，
  于是 `readdirSync` 遍历的是仓库根（`alembic/` `app/` `docs/` …），
  那里当然没有 `router.py`，脚本扫到 **0 个端点**。

  而它当时**报的是绿的**：

      ✓ 共扫描 0 个端点，缺失 0 个。
      ══ 契约覆盖了后端全部端点 ══

  这是"永远为真的空检查"最典型的形态 —— 它比没有检查更危险，
  因为它会让人以为查过了。所以这个脚本对"扫到 0 个"必须**报错**，
  见文件末尾的断言。
*/
const appRoot = join(webRoot, '..', 'suvex-api', 'app')

const contract = readFileSync(join(webRoot, 'docs', 'api-contract.md'), 'utf8')
// 与 contract-coverage.mjs 同样的归一化：连续空白压成单空格
const doc = contract.replace(/[ \t]+/g, ' ')

/** 把 `{scanId}` / `{id}` 统一成 `{}`，消除参数命名差异带来的假阳性 */
const normalise = (path) => path.replace(/\{[^}]*\}/g, '{}')

/** 把契约全文里的路径也归一化一遍，然后做包含判断 */
const docNormalised = normalise(doc)

/** 抽出某个 router 文件里的全部端点 */
function endpointsOf(file) {
  const src = readFileSync(file, 'utf8')
  const found = []

  /*
    模块前缀。`APIRouter(prefix="/ctf2")` 这种写法下，装饰器里的路径
    是**相对**的 —— 不拼上前缀就会拿 `/accounts` 去契约里找
    （而契约里写的是 `/ctf2/accounts`），于是整模块假阳性。
  */
  const prefixMatch = src.match(/APIRouter\(\s*prefix\s*=\s*"([^"]*)"/)
  const prefix = prefixMatch ? prefixMatch[1] : ''

  /*
    装饰器有两种写法，都要认：
      @router.get("/path", ...)
      @router.post(
          "/path",
          ...
      )

    ⚠️ 路径要允许**空串**：`@router.get("")` 是合法写法（模块根路径，
       `audit` 与 `members` 都用了它）。第一版用 `"([^"]+)"` 要求至少
       一个字符，于是这两个模块的根端点被**静默漏掉** ——
       而漏扫的表现是"✓ 0 个端点都在契约里"，看起来一切正常。
  */
  for (const block of src.split(/(?=@router\.)/)) {
    const head = block.match(/^@router\.(get|post|put|patch|delete)\(\s*\n?\s*"([^"]*)"/)
    if (head) {
      /*
        拼上模块前缀。空路径（`@router.get("")`，模块根）拼完就是
        `/audit` 这样的模块名本身 —— 与契约端点表里的写法一致。

        ⚠️ 路径用 `[^"]*`（允许空串）而不是 `[^"]+`：`audit` 与
           `members` 都用了空路径写法，要求至少一个字符会把它们的
           根端点**静默漏掉**，而漏扫的表现是"✓ 0 个端点都在契约里"。
      */
      found.push({ method: head[1].toUpperCase(), path: prefix + head[2] })
    }
  }
  return found
}

/** 找出所有 router 文件 */
function routerFiles() {
  const files = []
  for (const entry of readdirSync(appRoot, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue
    const candidate = join(appRoot, entry.name, 'router.py')
    if (existsSync(candidate)) files.push(candidate)
  }
  return files
}

console.log('══ 反向端点对账（后端代码 → 契约文档）══\n')

let total = 0
const missing = []

for (const file of routerFiles()) {
  const moduleName = file.split(/[\\/]/).at(-2)
  const endpoints = endpointsOf(file)
  total += endpoints.length

  const absent = endpoints.filter((e) => !docNormalised.includes(normalise(e.path)))
  if (absent.length === 0) {
    console.log(`✓  ${moduleName.padEnd(12)} ${String(endpoints.length).padStart(2)} 个端点都在契约里`)
  } else {
    console.log(`❌ ${moduleName.padEnd(12)} ${absent.length}/${endpoints.length} 个端点不在契约里：`)
    for (const e of absent) {
      console.log(`       ${e.method.padEnd(6)} ${e.path}`)
      missing.push(`${moduleName}: ${e.method} ${e.path}`)
    }
  }
}

console.log(`\n共扫描 ${total} 个端点，缺失 ${missing.length} 个。`)

/*
  ★ 扫到 0 个端点必须**报错**，不能报绿。

  这是"永远为真的空检查"的守门断言。第一版脚本因为路径少了一层
  `app/`，扫到 0 个端点却输出「契约覆盖了后端全部端点」——
  绿灯，但什么都没查。这类检查比没有检查更危险：它会让人以为查过了。

  20 是当前端点总数的下界（实际 103 个）。取一个宽松但非零的阈值，
  这样将来模块增减都不会误报，而"路径写错导致一个都没扫到"必被抓到。
*/
if (total < 20) {
  console.log(`\n❌ 只扫到 ${total} 个端点 —— 这不像正常情况。`)
  console.log('   最可能的原因是脚本里的路径写错了（不是契约有问题）。')
  console.log(`   检查 appRoot 是否指向了 suvex-api/app：${appRoot}`)
  process.exitCode = 1
} else if (missing.length > 0) {
  console.log('\n⚠️ 契约是「唯一事实来源」—— 端点存在于代码而不在契约里，')
  console.log('   意味着前端按契约接入时会漏掉它。请补 docs/api-contract.md。')
  process.exitCode = 1
} else {
  console.log('\n══ 契约覆盖了后端全部端点 ══')
}
