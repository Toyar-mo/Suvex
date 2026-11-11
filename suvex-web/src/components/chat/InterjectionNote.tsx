import type { ChatMessage } from '@/data/chat'
import { T } from '@/i18n/T'

/**
 * 跑的过程中用户插的一句话。
 *
 * 它已经落库（是真正的 `ChatMessage`），但模型**还没读到** —— 下一轮才会
 * 看到。所以它既不是"待发气泡"（那个还没落库），也不是普通用户消息
 * （那个会开新回合）。单独一种形状，并且把"下一轮会看到"写在脸上：
 * 这是插话功能唯一需要界面证明的事。
 */
export function InterjectionNote({ message }: { message: ChatMessage }) {
  return (
    <div className="flex justify-end" data-interjection="true">
      <div className="max-w-[520px] rounded-lg rounded-br-xs bg-accent/8 px-3.5 py-2">
        <p className="mb-0.5 text-2xs text-ink-faint"><T k="chatmsg.interjection"/></p>
        <p className="whitespace-pre-wrap break-words text-sm leading-[1.5] text-ink">
          {message.content}
        </p>
      </div>
    </div>
  )
}
