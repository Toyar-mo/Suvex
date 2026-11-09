import { useLocalStorage } from '@/hooks/useLocalStorage'

const STORAGE_KEY = 'suvex.chat.sessionListCollapsed'

/**
 * 对话页左侧会话清单的折叠状态，记忆到 localStorage。
 *
 * 默认展开：新用户第一眼要看到"有哪些对话"，而不是一个空白的内容区。
 * 窄屏（<md）本来就走覆盖式抽屉，这个状态只影响宽屏的固定列。
 */
export function useChatListCollapsed() {
  const [collapsed, setCollapsed] = useLocalStorage(STORAGE_KEY, false)

  return {
    collapsed,
    setCollapsed,
    toggle: () => setCollapsed((prev) => !prev),
  }
}
