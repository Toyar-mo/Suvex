/**
 * 契约覆盖度检查 —— 对着**开发任务合集的需求清单**查契约是否覆盖到位。
 *
 *   npm run verify:coverage
 *
 * ★ 与 contract-parity.mjs 的区别（两个检查查的是不同东西）：
 *
 *   · contract-parity 查「**我说的和代码一致吗**」—— 防抄写错误；
 *   · 本脚本查「**要求的东西我写全了吗**」—— 防漏项。
 *
 *   第二个更容易出问题：漏一个端点不会报错，只会让后端实现到一半
 *   发现"这个功能契约里没定义"，然后**临时发明一个接口** ——
 *   那正是契约存在的意义被破坏的方式。
 *
 * 需求清单来自任务合集任务 0 的原文："至少覆盖：认证登录、LLM provider 的
 * 增删改查、扫描的创建/列表/详情/终止/重跑、漏洞的列表/详情/状态流转/批量操作、
 * 报告的生成/列表/下载、全局统计数据"。
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'
import { join } from 'node:path'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const rawDoc = readFileSync(join(ROOT, 'docs/api-contract.md'), 'utf8')

/*
  ★ 归一化后再匹配 —— 这一条是被实测打出来的。

  第一版用 `| GET | \`/providers\`` 这种**带格式的精确串**去匹配，
  结果 Prettier 一跑（它会对齐 Markdown 表格的列宽）就变成：

      | GET    | `/providers`  | ...

  于是一整排检查全部误报"缺端点"。**检查匹配了排版，而不是内容。**

  这个教训和本项目其它几处同源：凡是"用字面串匹配结构化文本"的检查，
  都要先归一化空白 —— 否则它会因为一次无害的格式化而变红，
  而红了之后人的第一反应是去改被检查的东西（把格式改回去），
  那是在为检查的脆弱性买单。

  做法：把所有连续空白压成单空格，再去匹配。
*/
const doc = rawDoc.replace(/[ \t]+/g, ' ')

/** [合集要求, 契约里应当出现的片段[]] —— 片段按**空白已归一化**的文本匹配 */
const REQUIRED_ENDPOINTS = [
  ['认证登录', ['/auth/login', '/auth/logout', '/auth/me']],
  ['provider 列表', ['| GET | `/providers`']],
  ['provider 新建', ['| POST | `/providers`']],
  ['provider 更新', ['| PATCH | `/providers/{id}`']],
  ['provider 删除', ['| DELETE | `/providers/{id}`']],
  ['扫描创建', ['| POST | `/scans`']],
  ['扫描列表', ['| GET | `/scans`']],
  ['扫描详情', ['| GET | `/scans/{id}`']],
  ['扫描终止', ['| POST | `/scans/{id}/abort`']],
  ['扫描重跑', ['| POST | `/scans/{id}/rerun`']],
  ['漏洞列表', ['| GET | `/findings`']],
  ['漏洞详情', ['| GET | `/findings/{id}`']],
  ['漏洞状态流转', ['| POST | `/findings/status`']],
  ['漏洞批量操作', ['| POST | `/findings/status`']],
  ['报告生成', ['| POST | `/reports`']],
  ['报告列表', ['| GET | `/reports`']],
  ['报告下载', ['| GET | `/reports/{id}/download`']],
  ['全局统计', ['| GET | `/overview`']],
]

/** 合集点名的三件"结构"要求 */
const REQUIRED_STRUCTURE = [
  ['实时事件连接路径（按 scan_id 订阅）', ['GET /api/v1/scans/{scanId}/events']],
  ['每种事件类型的消息结构', ['"kind": "tool_call"']],
  ['统一错误响应格式', ['"code": "SCAN_TARGET_NOT_AUTHORIZED"']],
  ['每个端点标注鉴权要求', ['| 鉴权 |']],
  ['高风险操作的授权确认字段', ['"statement": "我有权对此目标进行安全测试"']],
  ['授权确认要落库留痕（不是前端摆设）', ['authorization_received_at']],
  ['高风险操作的鉴权级别', ['TARGET_NOT_AUTHORIZED']],
]

let missing = 0

console.log('══ REST 端点覆盖度（对照任务合集需求清单）══\n')
for (const [label, needles] of REQUIRED_ENDPOINTS) {
  const absent = needles.filter((n) => !doc.includes(n))
  if (absent.length === 0) {
    console.log(`✓  ${label.padEnd(18)} ${needles.length} 个端点`)
  } else {
    console.log(`❌ ${label.padEnd(18)} 缺: ${absent.join(', ')}`)
    missing += 1
  }
}

console.log('\n══ 结构性要求 ══\n')
for (const [label, needles] of REQUIRED_STRUCTURE) {
  const absent = needles.filter((n) => !doc.includes(n))
  if (absent.length === 0) {
    console.log(`✓  ${label}`)
  } else {
    console.log(`❌ ${label}  （缺片段: ${absent.join(', ')}）`)
    missing += 1
  }
}

/*
  ══ 决策一致性 ══

  ★ 这一类错误的性质与"漏写"完全不同，但同样危险：

    §0 定了一条决策，而文档别处还留着与它矛盾的旧表述。
    文档内部的矛盾比缺失更坏 —— 缺失会被发现（写代码时找不到定义），
    矛盾不会：两个地方都写得言之凿凿，读者只会照着先看到的那个做。

    实测踩过：PDF 这条决策从"服务端渲染"改成"保留浏览器打印"之后，
    §3.5 的下载端点说明、§7 的前端改动表里**都还写着服务端渲染** ——
    三处互相打架，而 grep 单个关键词时每处看起来都正常。

    所以这里做**反向断言**：某条决策定了之后，与它矛盾的旧措辞必须消失。
*/
const REJECTED_PHRASES = [
  [
    'PDF 决策（§3.5.1 定为"本轮保留浏览器打印"）',
    ['PDF 从 `browser-print` 换成服务端 `file`', '接入后应改为服务端渲染真 PDF'],
  ],
  [
    '登录决策（§0.3 已定为"要"）',
    ['本地/单机 → 不要', '**这个必须你选**', '建议 | 见 §1.2 的两种方案'],
  ],
  ['实时通道决策（§0.1 已定为 SSE）', ['这一条我不替你决定', '若选 WebSocket，§4 整节需要替换']],
]

console.log('\n══ 决策一致性（不该再出现的旧表述）══\n')
for (const [label, deadPhrases] of REJECTED_PHRASES) {
  const alive = deadPhrases.filter((p) => doc.includes(p))
  if (alive.length === 0) {
    console.log(`✓  ${label}：无矛盾残留`)
  } else {
    for (const p of alive) console.log(`❌ ${label}：仍存在旧表述 "${p}"`)
    missing += 1
  }
}

console.log(
  `\n${missing === 0 ? '══ 需求清单全部覆盖，且决策无矛盾 ══' : `══ ${missing} 项待处理，请修 docs/api-contract.md ══`}`,
)
if (missing > 0) process.exitCode = 1
