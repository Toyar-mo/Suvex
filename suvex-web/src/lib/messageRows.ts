import type { ChatMessage } from '@/data/chat'

/**
 * 一条消息是否要收进「过程详情」折叠块。
 *
 * ## 只收**旁白**，不收工具行
 *
 * 工具行（`role === 'tool'`）本来就已经是一行灰字（见 `ToolCard`，v0.103
 * 无条件折叠）。再套一层折叠 = 两级隐藏，用户在跑的时候**完全看不到它在干嘛**，
 * 那比"气泡太多"更糟 —— 不信任感来自"黑箱"，不是来自"话多"。
 *
 * 真正吵的是**模型边干边说的旁白**（"我先看看附件里是什么。"「解出来的文件
 * 叫 pwn…」「这是 CTF 的 pwn 二进制，我拆开看逻辑。」）。它们才是用户截图里
 * 那一串气泡，而且任何一句被截断都长得像"它停在半路"。
 *
 * 所以：旁白收走，工具行留在主线（一行、可展开、失败红点可见）。
 *
 * ★ 审批中的消息**永远**不收：那是等用户点确认的卡片。
 */
export function isProcessMessage(message: ChatMessage): boolean {
  if (message.approvalStatus === 'pending') return false
  return message.surface === 'process'
}

/** 折叠后的一行：要么是一条对话气泡，要么是一整段过程。 */
export type MessageRowItem =
  | { kind: 'single'; message: ChatMessage }
  | { kind: 'process'; items: ChatMessage[] }

/**
 * 把消息列表折成「对话行 / 过程块」（v1.25）。
 *
 * ## 为什么要把连续的过程消息合并
 *
 * 用户原话：「又中断了，就不能一次性把 flag 给我解出来吗」，截图里是
 * 一串小气泡夹在工具行之间（「我先看看附件里是什么。」「解出来的文件叫
 * pwn…」「这是 CTF 的 pwn 二进制，我拆开看逻辑。」）。
 *
 * 一次任务里"旁白 → 工具 → 旁白 → 工具"会来回很多次，逐条渲染就是那一串。
 * 合并成**一个**折叠块之后，主对话流只剩三样东西：
 * 「用户 →（过程详情 N 步）→ 结论」。
 */
export function groupMessageRows(messages: ChatMessage[]): MessageRowItem[] {
  const rows: MessageRowItem[] = []
  let bucket: ChatMessage[] = []

  const flush = () => {
    if (bucket.length > 0) {
      rows.push({ kind: 'process', items: bucket })
      bucket = []
    }
  }

  for (const message of messages) {
    if (isProcessMessage(message)) {
      bucket.push(message)
      continue
    }
    flush()
    rows.push({ kind: 'single', message })
  }
  flush()
  return rows
}
