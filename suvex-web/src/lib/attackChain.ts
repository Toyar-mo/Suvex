/**
 * 把一条对话的消息序列摊成「攻击链」。
 *
 * 为什么需要它：CyberStrikeAI 的右键菜单里有一项「查看攻击链」。它的价值不在
 * 于画得好看，而在于**把一轮已经跑完的对话还原成可复核的步骤序列** ——
 * 用户问过什么、模型据此决定了什么、每一步调了什么工具、哪一步失败了。
 * 聊天记录本身是线性文本，滚回去一条条看很难回答"它到底做了什么"。
 *
 * ## 为什么在前端算，不加后端端点
 *
 * 链条的全部输入就是 `messages`，而它已经在页面里了（打开会话时就拉过）。
 * 再加一个 `GET /chat/sessions/{id}/chain` 会引入第二份事实来源：同一串消息
 * 由后端重新拼一遍，两边口径迟早会漂 —— 而漂移的表现是"界面上看到的步骤
 * 和聊天记录对不上"，这类 bug 极难归因。**算一次、用同一份数据。**
 *
 * ## 这里刻意不做的事
 *
 * 不做图布局（不引入 cytoscape 之类的图库），也不推断"攻击阶段"（侦察/利用/
 * 提权…）。前者为一个抽屉拉进一个几百 KB 的依赖不划算；后者需要语义判断，
 * 而我们只有文本 —— 猜错的阶段标注比没有标注更误导人。所以这里产出的是
 * **有确定依据的线性步骤**，界面按时间轴渲染。
 */

import type { ChatMessage } from '@/data/chat'

/** 扫描 id 的字面形状：`scan_` + ULID（26 位 Crockford Base32）。 */
const SCAN_ID_RE = /\bscan_[0-9A-Z]{26}\b/g

export type StepKind = 'ask' | 'plan' | 'tool' | 'answer'

export interface AttackStep {
  /** 原消息的 `seq`。用它做 React key —— 比数组下标稳（消息会增量追加）。 */
  seq: number
  role: ChatMessage['role']
  kind: StepKind
  /** 时间轴上的短标题，如「用户提问」「调用 run_shell」「回答」。 */
  label: string
  /** 正文/参数/结果的可读摘要。已截断。 */
  detail: string
  toolName?: string | null
  failed: boolean
}

/** 步骤摘要里每段最多保留的字符数 —— 抽屉是"看个大概"，详情在聊天记录里。 */
const DETAIL_CHARS = 220

function clip(text: string, limit = DETAIL_CHARS): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat
}

/** 工具参数压成一行 `k=v, k=v`，比裸 JSON 好读，也不会撑爆时间轴。 */
export function formatToolArgs(args: Record<string, unknown> | null | undefined): string {
  if (!args) return ''
  const parts: string[] = []
  for (const [key, value] of Object.entries(args)) {
    if (value === null || value === undefined) continue
    const text = typeof value === 'string' ? value : JSON.stringify(value)
    if (!text) continue
    parts.push(`${key}=${text}`)
  }
  return clip(parts.join(', '))
}

/**
 * 从消息里收集所有出现过的扫描 id（按首次出现顺序去重）。
 *
 * 依据：`list_scans` 与 `create_scan` 的工具结果里都带反引号包起来的
 * `scan_xxx`（`_tool_list_scans` 每行 `- \`{id}\` {target}`，
 * `_tool_create_scan` 写 `已创建扫描任务 \`{id}\``）。所以从**消息文本**
 * 里取 id 是有据可查的，不是猜 —— 这也意味着模型就算转述 id 我们也能认出来。
 *
 * 同时扫 `toolArgs`：模型可能把上一个扫描 id 当参数传给后续工具。
 *
 * ⚠️ 只认完整形状（26 位）。用 `scan_` 前缀模糊匹配会把
 * `scan_task`、`scan_01M`（截断的）一起吃进来，然后拿它去请求
 * `/findings?scanId=` 得到 400 或者空结果 —— 空结果又会被读成
 * "这个对话没有漏洞"，而真相是 id 取错了。
 */
export function collectScanIds(messages: readonly ChatMessage[]): string[] {
  const seen = new Set<string>()
  for (const message of messages) {
    const haystacks = [message.content ?? '']
    if (message.toolArgs) haystacks.push(JSON.stringify(message.toolArgs))
    for (const haystack of haystacks) {
      // 每次新建正则（或重置 lastIndex）：`g` 正则是有状态的，
      // 跨消息复用会让 `test` 从上次的位置继续，从而**漏掉匹配**。
      const re = new RegExp(SCAN_ID_RE.source, 'g')
      for (const match of haystack.matchAll(re)) seen.add(match[0])
    }
  }
  return [...seen]
}

/**
 * 消息 → 步骤序列。
 *
 * 分类规则刻意保持"可解释"，不做语义猜测：
 * - `user` → `ask`
 * - `tool` → `tool`（标题用工具名，`toolFailed` 决定红色标记）
 * - `assistant` → 后面紧跟一条 tool 消息时为 `plan`（它在为调用做铺垫），
 *   否则为 `answer`（这一轮到这里就结束了）
 *
 * 空内容的 assistant 消息（历史上有过"模型没有产出内容"的占位消息）
 * 会被跳过 —— 它在时间轴上不构成一步。
 */
export function buildAttackChain(messages: readonly ChatMessage[]): AttackStep[] {
  const steps: AttackStep[] = []
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index]
    const content = (message.content ?? '').trim()
    if (!content && message.role !== 'tool') continue

    if (message.role === 'tool') {
      steps.push({
        seq: message.seq,
        role: message.role,
        kind: 'tool',
        label: `调用 ${message.toolName ?? '工具'}`,
        detail: clip(message.content ?? ''),
        toolName: message.toolName ?? null,
        failed: Boolean(message.toolFailed),
      })
      continue
    }

    if (message.role === 'user') {
      steps.push({
        seq: message.seq,
        role: message.role,
        kind: 'ask',
        label: '用户提问',
        detail: clip(content),
        failed: false,
      })
      continue
    }

    const next = messages[index + 1]
    const headsToTool = next !== undefined && next.role === 'tool'
    steps.push({
      seq: message.seq,
      role: message.role,
      kind: headsToTool ? 'plan' : 'answer',
      label: headsToTool ? '决策' : '回答',
      detail: clip(content),
      failed: false,
    })
  }
  return steps
}

export interface ChainSummary {
  asks: number
  tools: number
  failed: number
  /** 用到的不同工具名，按首次出现顺序 */
  toolNames: string[]
}

export function summarizeChain(steps: readonly AttackStep[]): ChainSummary {
  const toolNames: string[] = []
  let asks = 0
  let tools = 0
  let failed = 0
  for (const step of steps) {
    if (step.kind === 'ask') asks += 1
    if (step.kind === 'tool') {
      tools += 1
      if (step.failed) failed += 1
      const name = step.toolName ?? '工具'
      if (!toolNames.includes(name)) toolNames.push(name)
    }
  }
  return { asks, tools, failed, toolNames }
}
