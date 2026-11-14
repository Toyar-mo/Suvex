import type { ChatMessage } from '@/data/chat'

/**
 * 一个对话回合：一条用户消息 + 它之后的所有助手/工具消息。
 *
 * ## 为什么要按回合切
 *
 * v1.25 的 `groupMessageRows` 只把旁白收进折叠块，主线仍是"一条消息一行"。
 * 于是同一轮里的「旁白 → 工具 → 旁白 → 工具 → 结论」会平铺成一长串，
 * 读起来分不清哪几行属于同一次回答。
 *
 * metis 的对话页是按回合组织的：用户气泡之后是**一个**助手回合，
 * 回合内部再分「工作过程」与「最终答复」。这里只做分组，渲染交给
 * `AssistantTurn`。
 *
 * ## 边界
 *
 * - 只有 `role === 'user'` 会开新回合；插话也是 user，所以插话会接在
 *   当前回合之后成为新回合的起点 —— 这与模型实际读到的上下文一致。
 * - 没有用户消息的助手消息（例如刷新后只拉到了半轮）单独成一个回合，
 *   不丢掉。
 */
export interface ConversationTurn {
  /** 这一轮的用户输入；半轮数据里可能没有。 */
  user: ChatMessage | null
  /** 用户消息之后的助手/工具消息，保持原始顺序。 */
  items: ChatMessage[]
}

export function groupConversationTurns(messages: ChatMessage[]): ConversationTurn[] {
  const turns: ConversationTurn[] = []
  let current: ConversationTurn = { user: null, items: [] }

  const flush = () => {
    if (current.user !== null || current.items.length > 0) turns.push(current)
    current = { user: null, items: [] }
  }

  /*
    ★ 同一 id 只渲染一次。
    长轮询/重连可能把同一个 `message` 帧投递两遍（服务端仍是同一条 id），
    不去重时界面会出现两张一模一样的工具卡；这不是模型调用了两次。
  */
  const seen = new Set<string>()
  for (const message of messages) {
    if (seen.has(message.id)) continue
    seen.add(message.id)
    if (message.role === 'user') {
      flush()
      current.user = message
      continue
    }
    current.items.push(message)
  }
  flush()
  return turns
}
