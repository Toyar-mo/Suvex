import {
  ApprovalToggle,
  AttachmentBar,
  InlineSelect,
  MultiSelect,
  PersonaDialog,
  ProjectInput,
  ProviderSelect,
} from '@/components/chat/ChatControls'
import {
  ToolMention,
  parseMentionQuery,
  resolveMentions,
  type MentionQuery,
} from '@/components/chat/ToolMention'
import { Button } from '@/components/ui/Button'
import { T } from '@/i18n/T'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { EmptyState } from '@/components/ui/EmptyState'
import { ErrorState } from '@/components/ui/ErrorState'
import { Modal } from '@/components/ui/Modal'
import { SessionAttackChainModal } from '@/components/chat/SessionAttackChainModal'
import { SessionBatchModal } from '@/components/chat/SessionBatchModal'
import { GroupNameDialog } from '@/components/chat/GroupNameDialog'
import { AssistantTurn } from '@/components/chat/AssistantTurn'
import { AgentHub } from '@/components/chat/AgentHub'
import { useAutoScroll } from '@/hooks/useAutoScroll'
import { useChatListCollapsed } from '@/hooks/useChatListCollapsed'
import { useLocalStorage } from '@/hooks/useLocalStorage'
import { MessageRow } from '@/components/chat/MessageRow'
import { ChallengeBar, MissingChallengeBar } from '@/components/chat/ChallengeBar'
import { ChallengeProcess } from '@/components/chat/ChallengeProcess'
import { ChatPlanStrip } from '@/components/chat/ChatPlanStrip'
import { SessionContextMenu } from '@/components/chat/SessionContextMenu'
import { SessionFindingsModal } from '@/components/chat/SessionFindingsModal'
import { Skeleton } from '@/components/ui/Skeleton'
import { useAuth } from '@/data/auth-store'
import { loadKernel, useKernel } from '@/data/kernel'
import {
  archiveSession,
  createSession,
  decideApproval,
  dropStagedAttachment,
  clearStaleSending,
  compactSession,
  forkSession,
  interject,
  interruptTurn,
  loadEarlierMessages,
  loadPersonas,
  loadSessions,
  loadTools,
  openSession,
  pinSession,
  purgeArchivedSessions,
  purgeSession,
  renameSession,
  sendMessage,
  unarchiveSession,
  updateSettings,
  uploadAttachment,
  useChat,
  type ChatMessage,
  type ChatSession,
  type ChatSessionDetail,
  listTurns,
  resumeTurn,
  pollTurnProgress,
  refreshActiveMessages,
  getChatSnapshot,
  clearLastInterrupt,
  fetchMcpStatus,
  fetchContextUsage,
  type ChatContextUsage,
  type ChatMcpStatus,
  type ChatTurnRun,
} from '@/data/chat'
import type { PhasePayload } from '@/data/chat-stream'
import { groupConversationTurns } from '@/lib/conversationTurns'
import {
  buildConversationMarkdown,
  buildConversationMarkdownFileName,
  downloadMarkdownFile,
  type MarkdownDetail,
} from '@/lib/conversationMarkdown'
import { fetchSessionFindings, fetchSessionMessages } from '@/data/session-insight'
import type { FindingListItem } from '@/data/finding'
import { useProjectedProviders } from '@/data/settings-store'
import { ensureToolsLoaded, useTools } from '@/data/tools'
import { api, errorMessage } from '@/lib/api'
import { showToast } from '@/data/toast-store'
import { getToolExpansion, setToolExpansion } from '@/components/chat/toolExpansion'
import { cn } from '@/lib/cn'
import { useI18n } from '@/i18n'
import {
  Archive,
  ArrowDown,
  BookOpen,
  ChevronDown,
  ChevronRight,
  Eye,
  EyeOff,
  Folder,
  MessageSquarePlus,
  MessagesSquare,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Plug,
  Send,
  Settings2,
  Sparkles,
  Trash2,
  UserRound,
  X,
  Users,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

/**
 * 对话页（v0.93）。
 *
 * ## 这一页要解决什么
 *
 * 前面每一页都是**表单**：填参数、点按钮、等结果。而安全工作的实际节奏
 * 是"我先问一句、它查一下、我再追问"。表单撑不起这个过程 —— 用户要来回
 * 在四五个页面之间跳，然后自己把结果拼起来。
 *
 * 对话页让模型**直接动手**：它手上有四个工具（建扫描、看扫描、写技能、
 * 试 MCP 连接），用户说一句话，它调工具，结果回到同一个对话框里。
 *
 * ## 版面：左会话清单 + 右消息流（fillsViewport）
 *
 * 与「智能体活动」同一个形态，理由也相同：它是一块**工作台**，不是一篇
 * 文档。页面头（大标题 + 说明）会占掉 80px 且滚动时完全无用；
 * 消息流要的是一个自包含的滚动区，滚到顶/底不该带动整页。
 *
 * ## 三种消息长什么样
 *
 * | 角色 | 显示 |
 * |---|---|
 * | `user` | 右对齐气泡，深底 |
 * | `assistant` | 左对齐，正文 + 可折叠的"思考过程" |
 * | `tool` | 一张卡：工具名 + 参数 + 结果摘要，失败时变红 |
 *
 * 工具卡是这一页与普通聊天最大的差别：用户必须能看见**它到底做了什么**，
 * 否则一次"帮你建了扫描"和一次"我以为我建了其实没建"在界面上长得一样。
 *
 * ## ⚠️ 不显示原始动作协议
 *
 * `content` 是后端剥掉 `ACTION: {...}` 之后的正文字。前端**不要**再去
 * 找 `ACTION:` —— 那会把"内部协议处理"复制一份到浏览器里，而且两份
 * 逻辑必然会漂。见 `app/chat/engine.py::_narration_before_action`。
 */

/** 空态预设：覆盖全部对话工具，按“我现在想干什么”分组。 */
const STARTER_GROUPS: Array<{ label: string; prompts: string[] }> = [
  {
    label: '扫描与子代理',
    prompts: [
      '我上次扫了什么？有没有失败的？',
      '帮我对 example.com 发起一次 web 扫描',
      '现在有哪些扫描在跑？队列里还有吗？',
      '用子代理并行检查 A、B、C 三个地址，分别返回状态码和响应长度',
    ],
  },
  {
    label: 'HTTP 验证与沙箱',
    prompts: [
      '验证这个 HTTP 发现是否稳定复现：GET http://example.com/path 返回 200 和关键词',
      '在沙箱里看看 /work 里有什么、装了哪些工具',
      '打开 http://example.com 并截图给我',
    ],
  },
  {
    label: 'CTF 与比赛',
    prompts: [
      '列出当前 CTF 看板上的题目和状态',
      '解一下 CR7',
      '从比赛平台拉取最新题目',
      '把 flag{...} 提交到当前题目',
      '查看比赛竞速进度',
      '开始比赛竞速',
      '停止比赛竞速',
    ],
  },
  {
    label: '技能与 MCP',
    prompts: [
      '现在手上有哪些技能？哪几篇没被用过？',
      '读一下 pcap-analysis 技能',
      '把 pcap-analysis 技能加载到本轮上下文',
      '加载 nmap 配方，告诉我默认参数会漏什么',
      '把这次方法保存成一个技能',
      '删除技能 xxx',
      '列出已挂载的 MCP 工具',
      '帮我加一个 MCP 工具连接',
    ],
  },
]


/** 日本語プリセット。例文そのものがユーザーの入力になるので、UI の訳とは別に持つ。 */
const STARTER_GROUPS_JA: Array<{ label: string; prompts: string[] }> = [
  {
    label: 'スキャンとサブエージェント',
    prompts: [
      '前回のスキャン結果は？失敗はあった？',
      'example.com に対して Web スキャンを開始して',
      '今実行中のスキャンは？キューに残っている？',
      'サブエージェントで A・B・C を並行チェックし、ステータスコードと応答長を返して',
    ],
  },
  {
    label: 'HTTP 検証とサンドボックス',
    prompts: [
      'この HTTP 所見が再現するか確認して：GET http://example.com/path が 200 とキーワードを返す',
      'サンドボックスの /work の中身と、入っているツールを確認して',
      'http://example.com を開いてスクリーンショットを撮って',
    ],
  },
  {
    label: 'CTF と競技',
    prompts: [
      '現在の CTF ボードの課題と状態を一覧して',
      'CR7 を解いて',
      '競技プラットフォームから最新の課題を取得して',
      'flag{...} を現在の課題に提出して',
      '競技レースの進捗を確認して',
      '競技レースを開始して',
      '競技レースを停止して',
    ],
  },
  {
    label: 'スキルと MCP',
    prompts: [
      '利用できるスキルは？未使用のものは？',
      'pcap-analysis スキルを読んで',
      'pcap-analysis スキルを今回のコンテキストに読み込んで',
      'nmap レシピを読み込み、デフォルト設定の抜けを教えて',
      '今回の手法をスキルとして保存して',
      'スキル xxx を削除して',
      'マウント済みの MCP ツールを一覧して',
      'MCP ツール接続を追加して',
    ],
  },
]

const PRIMARY_STARTERS_JA = [
  '前回のスキャン結果は？失敗はあった？',
  'example.com に対して Web スキャンを開始して',
  '利用できるスキルは？未使用のものは？',
  'MCP ツール接続を追加して',
]

/** 英文预设（其他非中文语言先回退英文；示例消息本身就是给用户点的）。 */
const STARTER_GROUPS_EN: Array<{ label: string; prompts: string[] }> = [
  {
    label: 'Scans & subagents',
    prompts: [
      'What did I scan last time? Any failures?',
      'Start a web scan against example.com',
      'Which scans are running now? Anything queued?',
      'Check A, B and C in parallel with subagents; return status code and response length',
    ],
  },
  {
    label: 'HTTP verification & sandbox',
    prompts: [
      'Verify this HTTP finding reproduces: GET http://example.com/path returns 200 with a keyword',
      'Look inside /work and list the tools installed in the sandbox',
      'Open http://example.com and screenshot it for me',
    ],
  },
  {
    label: 'CTF & competitions',
    prompts: [
      'List the challenges and states on the current CTF board',
      'Solve CR7',
      'Pull the latest challenges from the competition platform',
      'Submit flag{...} to the current challenge',
      'Show race progress',
      'Start the competition race',
      'Stop the competition race',
    ],
  },
  {
    label: 'Skills & MCP',
    prompts: [
      'Which skills do I have? Which ones were never used?',
      'Read the pcap-analysis skill',
      'Load the pcap-analysis skill into this turn',
      'Load the nmap recipe and tell me what the defaults miss',
      'Save this method as a skill',
      'Delete skill xxx',
      'List the mounted MCP tools',
      'Help me add an MCP tool connection',
    ],
  },
]

const PRIMARY_STARTERS_EN = [
  'What did I scan last time? Any failures?',
  'Start a web scan against example.com',
  'Which skills do I have? Which ones were never used?',
  'Help me add an MCP tool connection',
]

const PRIMARY_STARTERS = [
  '我上次扫了什么？有没有失败的？',
  '帮我对 example.com 发起一次 web 扫描',
  '现在手上有哪些技能？哪几篇没被用过？',
  '帮我加一个 MCP 工具连接',
]

function StarterPrompts({
  onPick,
  disabled,
  expanded,
  onToggle,
}: {
  onPick: (prompt: string) => void
  disabled?: boolean
  expanded: boolean
  onToggle: () => void
}) {
  const { t, locale } = useI18n()
  const useJa = locale.startsWith('ja')
  const useEn = !locale.startsWith('zh')
  const groups = useJa ? STARTER_GROUPS_JA : useEn ? STARTER_GROUPS_EN : STARTER_GROUPS
  const primary = useJa
    ? PRIMARY_STARTERS_JA
    : useEn
      ? PRIMARY_STARTERS_EN
      : PRIMARY_STARTERS
  if (!expanded) {
    return (
      <div className="flex w-full flex-col gap-1.5">
        {primary.map((prompt) => (
          <button
            key={prompt}
            type="button"
            disabled={disabled}
            onClick={() => onPick(prompt)}
            className={cn(
              'flex w-full items-center gap-2 rounded-md border border-line bg-surface px-3 py-2 text-left',
              'text-xs text-ink-muted transition-ui duration-fast',
              'hover:border-line-strong hover:text-ink',
              'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
              'disabled:cursor-not-allowed disabled:opacity-60',
            )}
          >
            <Sparkles size={12} strokeWidth={2} className="shrink-0 text-ink-faint" aria-hidden="true" />
            {prompt}
          </button>
        ))}
        <button
          type="button"
          onClick={onToggle}
          disabled={disabled}
          className="self-start px-1 pt-0.5 text-2xs text-ink-faint underline-offset-2 hover:text-ink hover:underline disabled:opacity-50"
        >
          {t('chat.moreStarters', { n: groups.reduce((sum, group) => sum + group.prompts.length, 0) }, `更多预设（${groups.reduce((sum, group) => sum + group.prompts.length, 0)}）`)}
        </button>
      </div>
    )
  }

  return (
    <div className="flex w-full flex-col gap-3">
      {groups.map((group) => (
        <section key={group.label} className="flex flex-col gap-1.5">
          <p className="text-2xs uppercase tracking-wide text-ink-faint">{group.label}</p>
          <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
            {group.prompts.map((prompt) => (
              <button
                key={prompt}
                type="button"
                disabled={disabled}
                onClick={() => onPick(prompt)}
                className={cn(
                  'flex w-full items-start gap-2 rounded-md border border-line bg-surface px-3 py-2 text-left',
                  'text-xs text-ink-muted transition-ui duration-fast',
                  'hover:border-line-strong hover:text-ink',
                  'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
                  'disabled:cursor-not-allowed disabled:opacity-60',
                )}
              >
                <Sparkles
                  size={12}
                  strokeWidth={2}
                  className="mt-0.5 shrink-0 text-ink-faint"
                  aria-hidden="true"
                />
                {prompt}
              </button>
            ))}
          </div>
        </section>
      ))}
      <button
        type="button"
        onClick={onToggle}
        className="self-start px-1 text-2xs text-ink-faint underline-offset-2 hover:text-ink hover:underline"
      >
        {t('chat.collapseStarters', undefined, '收起预设')}
      </button>
    </div>
  )
}

/**
 * 「正在想…」那一行该显示什么（v0.100）。
 *
 * ## 为什么它值得一个独立函数 + 独立注释
 *
 * 改造前这一行永远是「正在想…」。而一轮对话可能有 1 次模型调用 +
 * 最多 6 轮工具往返，实测能到 30 秒以上 —— 用户在那段时间里**无法判断**
 * 「它在推进」还是「它卡住了」。这两件事的应对完全不同（等 / 刷新重发）。
 *
 * 所以这里把后端推来的 `phase` 翻成一句人话：
 *   · `thinking` → 第几轮推理（数字让"在推进"看得见）
 *   · `tool`     → 正在跑哪个工具（名字让"它到底在干嘛"看得见）
 *
 * `phase` 为 `null`（刚点发送、还没收到第一帧）时回落到原文案 ——
 * 那一刻确实没有更多信息可说，硬编一个"准备中"反而像卡住了。
 */
// 纯函数与页面同文件：测试直接导入它；拆文件只会让"进度文案"的定义离开唯一使用者。
// eslint-disable-next-line react-refresh/only-export-components
export /** Token 数按 OMP 习惯显示为 K / M，而不是 24,000 / 1,000,000。 */
function formatTokenCount(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0'
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`
  }
  if (value >= 1_000) {
    return `${(value / 1_000).toFixed(1).replace(/\.0$/, '')}K`
  }
  return String(Math.round(value))
}

function phaseLabel(
  phase:
    | PhasePayload
    | null
    | {
        phase: string
        round: number
        tool?: string
        reason?: string
        attempt?: number
        maxAttempts?: number
      },
  t?: (key: string, vars?: Record<string, string | number>, fallback?: string) => string,
): string {
  const tr = (
    key: string,
    vars: Record<string, string | number> | undefined,
    fallback: string,
  ) => (t ? t(key, vars, fallback) : fallback)
  if (!phase) return tr('chat.a.thinking', undefined, '正在想…')
  const round = typeof phase.round === 'number' ? phase.round + 1 : 0
  if (phase.phase === 'resume') {
    // ★ v1.14：模型这一轮没写动作（或交了白卷），服务端正在自动续跑。
    //
    // 为什么要区分 `reason`：被输出上限切断时，模型是"话说了一半"，
    // 用户已经看到半句话；格式错时它是"想好了没说怎么动手"，界面上什么都没有。
    // 两种等待的体验不同，但**都不要在界面暴露协议内部词**（`ACTION`、
    // `finish_reason`）—— 用户只关心"还要不要等"。
    if (phase.reason === 'final-check') {
      // ★ v1.29：模型写了 FINAL 但那像是「接下来要做什么」，服务端正在
      // 让它确认一次。用户看到的是「已经停下」，所以要明确说在核实。
      return tr('chat.a.phase.finalCheck', { round }, `第 ${round} 轮 · 正在确认它是不是真的做完了…`)
    }
    if (phase.reason === 'truncation') {
      return tr('chat.a.phase.truncation', { round }, `第 ${round} 轮 · 回复太长被截断了，正在接着写…`)
    }
    if (phase.reason === 'compaction') {
      return tr('chat.a.phase.compaction', { round }, `第 ${round} 轮 · 上下文过长，正在压缩后继续…`)
    }
    return tr('chat.a.phase.finish', { round }, `第 ${round} 轮 · 正在让它把话说完…`)
  }
  if (phase.phase === 'tool' && phase.tool) {
    return tr('chat.a.phase.tool', { round, tool: phase.tool }, `第 ${round} 轮 · 正在执行 ${phase.tool}…`)
  }
  return round > 0
    ? tr('chat.a.phase.round', { round }, `第 ${round} 轮 · 模型在推理…`)
    : tr('chat.a.thinking', undefined, '正在想…')
}

/**
 * 这一轮已经跑了几次工具（v1.30）。
 *
 * 只数 `role === 'tool'` 的消息：它们是**执行过**的证据。旁白（`process`）
 * 也代表"动过"，但每轮开头模型都会先写一句旁白 —— 把旁白算进去会让
 * 计数在真正执行第一个工具之前就从 1 开始，反而看不出工具的推进。
 */
// eslint-disable-next-line react-refresh/only-export-components
export function turnToolCount(items: ChatMessage[]): number {
  return items.filter((message) => message.role === 'tool').length
}

/**
 * 「MCP 已挂载 N 台 · 连通 M 台」小字（v1.80）。
 *
 * ★ 为什么不能省：勾选本身只证明"存了一个偏好"。上一版这个下拉被移除，
 *   原因正是"勾了没效果，用户无从判断"。这里把 `/mcp-status` 的**真实**
 *   试连结果放在下拉旁边 —— 连上几台、哪台失败，一眼可见；失败原因在
 *   `title` 里给全（鼠标悬停可读，不占版面）。
 */
function McpMountStatus({
  status,
  loading,
}: {
  status: ChatMcpStatus | null
  loading: boolean
}) {
  const { t } = useI18n()
  if (loading) {
    return <span className="text-2xs text-ink-faint"><T k="chatmsg.mcpTesting"/></span>
  }
  if (!status || status.mountedCount === 0) return null

  const failed = status.servers.filter((server) => !server.connected)
  const detail = failed.length
    ? failed
        .map((server) =>
          t(
            'chat.a.mcp.serverFailed',
            { name: server.name, reason: server.reason ?? t('chat.a.mcp.connectFailed', undefined, '连接失败') },
            `${server.name}：${server.reason ?? '连接失败'}`,
          ),
        )
        .join('\n')
    : status.servers
        .map((server) =>
          t(
            'chat.a.mcp.serverTools',
            { name: server.name, count: server.toolCount },
            `${server.name}：${server.toolCount} 个工具`,
          ),
        )
        .join('\n')

  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 text-2xs',
        failed.length > 0 ? 'text-signal-risk' : 'text-ink-faint',
      )}
      title={detail}
      data-mcp-status={failed.length > 0 ? 'failed' : 'ok'}
    >
      {t(
        'chat.a.mcp.mounted',
        { mounted: status.mountedCount, connected: status.connectedCount },
        `已挂载 ${status.mountedCount} 台 · 连通 ${status.connectedCount} 台`,
      )}
      {failed.length > 0 &&
        t('chat.a.mcp.failedSuffix', { n: failed.length }, ` · ${failed.length} 台试连失败`)}
    </span>
  )
}

function runningProgressText(
  t: (key: string, vars?: Record<string, string | number>, fallback?: string) => string,
  round: number,
  tools: number,
): string {
  // 不用 t 的 vars 插值：线上出现过 `{round}` 原样显示；这里显式替换，
  // 保证任何 locale 下数字都会落到文案里。
  return t('chat.ph.serverRunningProgress', undefined, '这一轮还在跑（服务端）')
    .replace('{round}', String(round))
    .replace('{tools}', String(tools))
}

export function ChatPage() {
  const { t } = useI18n()
  const {
    sessions,
    archivedSessions,
    active,
    status,
    error,
    sending,
    personas,
    stagedAttachments,
    deciding,
    // ★ `tools` 是**对话工具**（`GET /chat/tools`，10 个：list_scans /
    //   create_scan / save_skill 一类），不是下面 `useTools()` 那份**内核
    //   技能库**。两者名字都在讲"工具"，混用会让 @补全列出根本点不动的条目。
    tools,
    pendingUserContent,
    // ★ v0.100：流式过程中的两个状态 —— 现在在做什么、用户插了哪些话。
    phase,
    liveToolOutput,
    liveToolProgress,
    interjections,
    // ★ v0.102：正在流入的正文（还没落库）。
    streamingText,
    // ★ v1.61：服务端轮询到的"这一轮跑到哪了"（本页丢了 SSE 时的兜底）。
    serverTurn,
    // ★ v1.62：用户已请求「彻底停止」，但这一轮还没真正停下（可能卡在工具里）。
    stopRequested,
    // ★ v1.65：本浏览器在跑的会话集合 —— 按会话独立并发的唯一事实来源。
    sendingSessionIds,
    // ★ v1.68：最近一次中断/继续操作的结果 —— 让弹窗有明确回执，不再"点了没反应"。
    lastInterrupt,
  } = useChat()
  const { user } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  /*
    ★ v1.79：题目状态要**跟着服务端走**。

    `active.challenge` 是打开会话那一刻的快照，之后消息刷新、轮询都不更新 ——
    于是题解完了，题目条和过程面板仍然写着「解题中」，而过程事件每 3 秒
    还在轮询（`running` 一直是 true）。这里订阅看板 store（它本来就是
    题目状态的唯一前端真源），非终态时 5 秒刷一次，到终态立刻停。
  */
  const { challenges: kernelChallenges } = useKernel()

  const [draft, setDraft] = useState('')
  const [showList, setShowList] = useState(false)
  const [startersExpanded, setStartersExpanded] = useState(false)
  // 宽屏会话清单折叠（窄屏用 `showList` 的抽屉）；记忆到 localStorage。
  const { collapsed: listCollapsed, toggle: toggleList } = useChatListCollapsed()
  // v1.19：服务端说"这一轮可以继续"时，把那个 run 存下来渲染提示条。
  const [stuckTurn, setStuckTurn] = useState<ChatTurnRun | null>(null)
  const [resuming, setResuming] = useState(false)
  //: 最近一次 `listTurns` 的结果。错误条要从中取「最近一条 failed 的 run」做重试。
  const [lastRuns, setLastRuns] = useState<ChatTurnRun[]>([])
  const [contextUsage, setContextUsage] = useState<ChatContextUsage | null>(null)

  /*
    ★ 从 URL 派生"要打开哪个会话"，而不是在 effect 里 setState。

    与 `SchedulesPage` 的 `creatingDraft ?? searchParams.get('new')` 同一写法：
    先看本地覆盖，没有再看 URL。这样刷新页面/分享链接能回到同一个会话，
    而"刚新建、还没进 URL"那半秒也不会闪一下空屏。
  */
  const wantedId = searchParams.get('session')

  useEffect(() => {
    void loadSessions()
    // ⚠️ 这里有两个同名的 `loadTools`，它们拉的是**不同的东西**：
    //   · `@/data/chat` 的 `loadTools()` → `GET /chat/tools`，是给消息区做“这个模型能干什么”的说明文本；
    //   · `@/data/tools` 的 `ensureToolsLoaded()` → `GET /kernel/{skills,mcp-servers}`，才是下面「技能」「MCP」两个下拉的选项来源。
    //
    // ★ v1.18 修的是后者从来没被调用过：`skillOptions` / `mcpOptions` 读的是 `useTools()` 的快照，
    //   而那个 store 只有工具页会去加载。于是对话页的技能下拉永远是空的（显示“技能库是空的”），
    //   而技能库其实有 77 篇。这类“配了但是没接线”的缺陷在界面上只表现为一句空提示，
    //   很难联想到是加载入口漏了。
    void loadTools()
    void ensureToolsLoaded()
    // ★ 人格列表与工具列表分开拉：`loadPersonas` 失败**不挂 error**
    //   （人格是可选功能，读不到只意味着下拉是空的），而 `loadTools`
    //   失败会让整页进错误态。两者的容错级别不同，所以不能并成一个。
    void loadPersonas()
  }, [])

  /*
    ★ v1.66：只要**本浏览器**还有会话在跑，就每 4 秒刷一次会话列表，
    让侧栏的「运行中」转圈点保持新鲜。

    为什么需要：`running` 来自服务端列表接口（`loadSessions`），而列表只在
    进页面 / 一轮结束时重拉。若用户盯着列表而某个**后台**会话（不是当前
    打开这个）在跑，它的点不会自己亮/灭。这里在有会话跑时低频轮询列表补上。
    没有会话在跑就完全不轮询（依赖数组为空则停），不产生无谓开销。
  */
  /*
    ★ v1.68.2：只要**还有任何「在跑」的迹象**就每 4 秒刷一次会话列表。

    用户原话「结束了前端对话列还会打转，要刷新网页才行」。根因：`anyRunning`
    原来只看本浏览器的 `sendingSessionIds` —— 某个会话这一轮结束、`sending` 一清，
    `anyRunning` 立刻变 false，4 秒轮询就停了；可侧栏那枚转圈点读的是**服务端**
    的 `session.running`，最后一次 fetch 若恰好是 `true`（DB 还没翻成 done，
    或结束不是本页发起的），这个 `true` 就**冻结**在那里，转圈永远不停，必须刷新
    网页重新拉列表才消失。

    修法：把「侧栏还有会话显示 running」也算进轮询条件。这样只要还有一枚转圈点，
    就持续每 4 秒重拉列表，等服务端把 `running` 翻成 false，点自动灭掉 —— 自愈，
    不用刷新。等所有点都灭了才停轮询，不产生无谓开销。
  */
  const anySessionRunning = sendingSessionIds.length > 0 || sessions.some((s) => s.running)
  useEffect(() => {
    if (!anySessionRunning) return
    const timer = setInterval(() => void loadSessions(), 4000)
    return () => clearInterval(timer)
  }, [anySessionRunning])

  // 模型接入点：读设置里那份账号，只用**已配置好**的（没配的选了也跑不起来，
  // 而界面上摆一堆跑不起来的选项等于把失败推给用户）。
  const providers = useProjectedProviders()
  const usableProviders = useMemo(
    () => providers.filter((provider) => provider.configured),
    [providers],
  )

  // 技能库与 MCP 连接清单：挂载下拉的选项来源。
  const toolsStore = useTools()
  const skillOptions = useMemo(
    () => toolsStore.skills.categories.flatMap((group) => group.skills.map((s) => s.name)),
    [toolsStore.skills],
  )
  const mcpOptions = useMemo(
    () => toolsStore.mcp.servers.map((server) => server.name),
    [toolsStore.mcp],
  )

  // 项目建议：从已有会话里取，去重。这样它自动反映用户实际用过的名字，
  // 不需要单独维护一份项目清单（平台里没有 project 表，见 ChatControls）。
  const projectSuggestions = useMemo(() => {
    const names = new Set<string>()
    for (const session of sessions) {
      if (session.project) names.add(session.project)
    }
    return [...names].sort()
  }, [sessions])

  const [personaOpen, setPersonaOpen] = useState(false)
  const [uploading, setUploading] = useState(false)
  /**
   * ★ v0.100：一次插话正在提交。
   *
   * 与 `sending` 分开：`sending` 是"整轮在跑"（由 store 管），这个是
   * "我刚按下插话、HTTP 还没回来"（几百毫秒）。合成一个会让发送按钮
   * 在整轮期间一直转圈 —— 暗示"点不动"，而它此刻正是**能**点的。
   */
  const [interjecting, setInterjecting] = useState(false)
  /** ★ v1.65：正在建会话+发首句（防连点建出两个会话）。与全局 `sending`
   *  解耦 —— 别的会话在跑不该让欢迎屏的推荐问法按钮变灰。 */
  const [pickingStarter, setPickingStarter] = useState(false)
  /**
   * ★ v1.25：「中断当前步骤」弹窗。
   *
   * `interrupting` 是"中断请求已发出、还在等服务端确认"的那几百毫秒；
   * `interruptNote` 是用户填的补充说明（只在「中断并继续」时有意义）。
   */
  const [interruptOpen, setInterruptOpen] = useState(false)
  const [interruptNote, setInterruptNote] = useState('')
  const [interrupting, setInterrupting] = useState(false)

  /*
    ── v1.15 会话管理（右键菜单 / 批量 / 已归档）────────────────────

    ★ `menuAnchor` 存的是**右键那一刻的视口坐标 + 会话 id**，不是"某条会话
      被选中"。菜单自己算边界要用坐标；而 `sessionId` 让菜单在列表刷新后
      还能确认自己指向谁（刷新会重排，靠索引会指错行）。
  */
  const [menuAnchor, setMenuAnchor] = useState<{
    x: number
    y: number
    sessionId: string
    /* 标题**在右键那一刻**就抄一份：列表刷新后那条会话可能暂时不在数组里，
       而菜单不该因此自己消失（用户会以为"鼠标放上去就没了"）。 */
    title: string
    archived: boolean
    pinned: boolean
    project: string | null
  } | null>(null)
  /** 正在重命名的会话（`null` = 对话框关着）。 */
  const [renameTarget, setRenameTarget] = useState<{ id: string; title: string } | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  const [renaming, setRenaming] = useState(false)
  /** 等待二次确认的危险操作。 */
  const [confirmAction, setConfirmAction] = useState<
    | { kind: 'archive'; id: string; title: string }
    | { kind: 'purge'; id: string; title: string }
    | { kind: 'purgeAll'; n: number }
    | null
  >(null)
  const [batchOpen, setBatchOpen] = useState(false)
  /* v1.20：新建分组弹窗。`ids` 让同一套 UI 同时服务「单条右键」与「批量勾选」。 */
  const [newGroupFor, setNewGroupFor] = useState<string[] | null>(null)
  const [batchBusy, setBatchBusy] = useState(false)
  /** 「已归档」区是否展开。默认收起 —— 它是低频的"回收站"。 */
  const [archivedOpen, setArchivedOpen] = useState(false)
  /** 已收起的父会话 id。默认展开；记住到 localStorage，刷新后不反复找。 */
  const [collapsedParentList, setCollapsedParentList] = useLocalStorage<string[]>(
    'suvex.chat.collapsedParents',
    [],
  )
  const collapsedParents = useMemo(
    () => new Set(collapsedParentList),
    [collapsedParentList],
  )

  /*
    ── v1.16 两个只读洞察视图 ────────────────────────────────────

    ★ 取数放在**事件处理器**里，不放进 effect：本项目的 lint 明确禁止
      `set-state-in-effect`（在 effect 体里同步 setState 会触发级联渲染）。
      菜单点了才取，天然是事件；也顺手解决了"右键别人的会话"这个场景 ——
      `fetchSessionMessages` 不会改 `active`（`openSession` 会）。
  */
  const [chainView, setChainView] = useState<{
    open: boolean
    title: string
    loading: boolean
    error: string | null
    messages: ChatMessage[]
  }>({ open: false, title: '', loading: false, error: null, messages: [] })
  const [findingsView, setFindingsView] = useState<{
    open: boolean
    title: string
    loading: boolean
    error: string | null
    sessionId: string
    scanIds: string[]
    items: FindingListItem[]
  }>({ open: false, title: '', loading: false, error: null, sessionId: '', scanIds: [], items: [] })

  /** 右键时被点的那条会话。找不到时退回右键那一刻的快照，**不返回 null**。 */
  const menuSession = useMemo(() => {
    if (!menuAnchor) return null
    return (
      sessions.find((s) => s.id === menuAnchor.sessionId) ??
      archivedSessions.find((s) => s.id === menuAnchor.sessionId) ?? {
        id: menuAnchor.sessionId,
        title: menuAnchor.title,
        archived: menuAnchor.archived,
        pinned: menuAnchor.pinned,
        project: menuAnchor.project,
      }
    )
  }, [menuAnchor, sessions, archivedSessions])

  /*
    ★ 只在**会话 id 变了**时才拉详情。

    依赖 `[wantedId]` 而不是 `[wantedId, active]`：后者会在 active 更新后
    再触发一次，于是无限拉取。而 `active?.id === wantedId` 守卫让同一会话
    重复渲染时不重复请求。
  */
  useEffect(() => {
    if (!wantedId) return
    if (active?.id === wantedId) return
    void openSession(wantedId)
  }, [wantedId, active?.id])

  const composerRef = useRef<HTMLTextAreaElement | null>(null)

  /** 只看回答：隐藏工作过程，只保留结论。 */
  const [answersOnly, setAnswersOnly] = useState(false)
  /** 子代理面板（Web Agent Hub）。 */
  const [agentHubOpen, setAgentHubOpen] = useState(false)
  /** 对话设置（项目 / 人格 / 审批 / 技能 / MCP）收在齿轮后面，默认不占一行。 */
  const [settingsOpen, setSettingsOpen] = useState(false)
  /**
   * 本会话挂载的 MCP server 的**真实**试连结果。
   *
   * ★ 不能从 `active.mcpServerNames` 直接推：那只是偏好。真正决定模型能
   *   不能调那些工具的是"此时此刻连不连得上"。所以面板打开、或改完挂载
   *   之后都重新试连一次（见下面的 effect）。
   */
  const [mcpStatus, setMcpStatus] = useState<ChatMcpStatus | null>(null)
  const [mcpStatusLoading, setMcpStatusLoading] = useState(false)
  const activeId = active?.id
  // 用字符串做依赖：数组每次会话刷新都是新引用，直接依赖会无谓重连。
  const mountedMcpKey = (active?.mcpServerNames ?? []).join('\u0000')

  useEffect(() => {
    if (!settingsOpen || !activeId) {
      setMcpStatus(null)
      return
    }
    let cancelled = false
    setMcpStatusLoading(true)
    void fetchMcpStatus(activeId)
      .then((next) => {
        if (!cancelled) setMcpStatus(next)
      })
      .catch(() => {
        // 试连失败是**正常结果**（server 挂了），不是页面错误。
        // 具体原因由后端逐台返回；拿不到整个响应时才退化成"未知"。
        if (!cancelled) setMcpStatus(null)
      })
      .finally(() => {
        if (!cancelled) setMcpStatusLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [settingsOpen, activeId, mountedMcpKey])
  /**
   * 输入框里正在输入的 `@提及`。`null` = 不显示补全层。
   *
   * ★ 每次 `onChange` / 光标移动都重算，不做防抖：计算量是
   *   `lastIndexOf` + 一次正则，比一次重渲染便宜得多；一旦防抖，
   *   快速打字时会看到补全层"慢半拍"地追着光标跑。
   */
  const [mention, setMention] = useState<MentionQuery | null>(null)

  const messageCount = active?.messages.length ?? 0

  /*
    自动滚动对标成熟客户端（ChatGPT / Claude）：
      · 用户没往回翻 → 跟随到底，**流式 token 增长也跟随**；
      · 用户往上翻 → 暂停，不再把他拽回底部；
      · 用户自己滚回底部 → 自动恢复跟随。
    旧实现只监听「消息条数」：token 在视口下方增长时页面不动，落一条
    消息又无条件跳到底 —— 正是"卡一下、猛跳一下"的来源。
  */
  /*
    Ctrl/Cmd+O：展开/收起**全部**工具输出（omp 的 Ctrl+O）。
    单卡仍可独立开合；按一次快捷键进入全局模式，点任意卡退出该模式。
  */
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 'o') return
      event.preventDefault()
      const next = getToolExpansion() === 'expanded' ? 'collapsed' : 'expanded'
      setToolExpansion(next)
      showToast({
        message:
          next === 'expanded'
            ? t('chat.expandAll', undefined, '已展开全部工具输出')
            : t('chat.collapseAll', undefined, '已收起全部工具输出'),
        tone: 'ok',
      })
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [t])

  const streamingLength = streamingText?.length ?? 0
  const contentKey =
    messageCount * 10_000_000 + streamingLength + (pendingUserContent !== null ? 5_000_000 : 0)
  const [autoFollow, setAutoFollow] = useState(true)
  const { attachContainer, isAtBottom, scrollToLatest } = useAutoScroll({
    follow: autoFollow,
    contentKey,
    onUserAway: () => setAutoFollow(false),
    onUserBack: () => setAutoFollow(true),
  })

  /*
    「加载更早」要自己保住滚动位置。

    往上插一页会让内容高度突然变大：浏览器的锚定在这里不可靠（消息列表
    高度是动态的，Markdown 渲染完才定型），表现是**点一下就跳回几个月前
    的某一句**。所以插入前后各量一次 scrollHeight，把差值补进 scrollTop ——
    用户视线停在原来那条消息上，新的旧消息在它上面出现。
  */
  const scrollerElRef = useRef<HTMLDivElement | null>(null)
  const [loadingEarlier, setLoadingEarlier] = useState(false)

  /*
    ★ 这个回调的**身份必须恒定**（`attachContainer` 的 docstring 写了原因）：
      身份一变，React 会先用 null 调旧的、再用节点调新的，等于把滚动容器
      卸载重挂一次 —— 滚动位置归零，用户正在看的地方被弹回顶部。
  */
  const setScrollerRef = useCallback(
    (el: HTMLDivElement | null) => {
      scrollerElRef.current = el
      attachContainer(el)
    },
    [attachContainer],
  )

  const handleLoadEarlier = async () => {
    if (!active || loadingEarlier) return
    const el = scrollerElRef.current
    const before = el?.scrollHeight ?? 0
    setLoadingEarlier(true)
    const wasFollowing = autoFollow
    setAutoFollow(false)
    const inserted = await loadEarlierMessages(active.id)
    setLoadingEarlier(false)
    if (inserted && el) {
      // 等 Markdown 渲染完再补偿，否则量到的是插入前的高度
      requestAnimationFrame(() => {
        el.scrollTop += el.scrollHeight - before
      })
    }
    if (wasFollowing && !inserted) setAutoFollow(true)
  }

  // 切会话：回到跟随（新会话直接看最新）。
  useEffect(() => {
    setAutoFollow(true)
  }, [active?.id])

  // 刚发出消息：无论此前翻到哪，都拉回底部（否则会以为点了没反应）。
  useEffect(() => {
    if (pendingUserContent === null) return
    setAutoFollow(true)
    scrollToLatest('auto')
  }, [pendingUserContent, scrollToLatest])

  /**
   * ★ v0.100：能不能按发送。
   *
   * 两种合法动作，**判据不同**：
   *   · 空闲 → 发一条消息（会起一轮，所以要 `sending === false`）
   *   · 跑着 → 插一句话（不起新一轮，所以 `sending === true` **也允许**）
   *
   * 改造前这里写的是 `!sending` —— 于是跑的时候输入框被禁用，
   * 用户的原话「正在想的时候不能插话」说的就是这个。
   */
  /*
    ★ v1.19：进会话时问一次"上一轮是不是断在半路"。

    依赖里带 `sending` 是刻意的：一轮**刚结束**时要复查一次。中断的那一轮
    服务端会落成 `interrupted`，但前端手上没有那个信号 —— 只有等这一轮
    结束后再问一次，提示条才会在用户眼前出现，而不是要等刷新页面。

    拿不到（网络错/接口 404）就当没有：`listTurns` 内部已经吞掉了异常。
  */
  /*
    ★ v1.61：**服务端在跑、本页看不见**时的兜底轮询（resumable polling）。

    背景（用户原话）：「服务端在后台跑、前端没有显示，我不知道进行得怎么样」。
    根因：前端只信本页那条 SSE 流。流一断（刷新、切页面、done 帧丢），
    界面就以为"没在跑"，而服务端 v1.54 起**断开不取消**，那一轮还在后台干。

    做法照抄 CyberStrikeAI 的 `get_tool_execution` / resumable polling：
    - 只要服务端还有 `running` 的轮次，就每 1.5s 拉一次 `/turns` 更新进度；
    - 每轮都顺手重取消息（服务端可能已落了新工具结果/正文，本页没收到帧）；
    - 一旦服务端说"跑完了"：
        · 若本页**没有**在跑（`!sending`，说明流早断了）→ 把这批新消息接上、
          清掉本地残留的"在跑"状态，界面自动变成"已结束"，**不必刷新网页**；
        · 若本页在跑（`sending`，SSE 正常）→ 交给 SSE 的 done 帧收尾，这里不动。

    触发条件：进会话就启动。为什么进会话就启动（而不是只在发现异常时）：
    刷新页面那一刻 `sending` 是 false、`lastRuns` 还没拉到 —— 正是"服务端在跑
    我却不知道"的最典型场景；进会话即开轮询，才能立刻把那条 running 轮接回来。
    */
  useEffect(() => {
    const sessionId = active?.id
    if (!sessionId) return
    let alive = true
    let timer: ReturnType<typeof setTimeout> | null = null
    // 见过服务端报"在跑"没有。用来区分"刚发出去、服务端还没建 running 行"
    // 和"流早断了、本地 sending 卡住"两种情形（见下面 done 分支）。
    let sawRunning = false

    const tick = async () => {
      if (!alive) return
      const result = await pollTurnProgress(sessionId)
      if (!alive) return
      /*
        ★ v1.79：轮次列表顺便当"能不能继续"的依据（原来这里有第二个
        `listTurns` effect，同一端点一轮开始/结束各打一次）。请求失败时
        `runs` 是 null —— 什么都不写，下一拍再试。
      */
      if (result.runs) {
        setLastRuns(result.runs)
        setStuckTurn(result.runs.find((run) => run.resumable) ?? null)
      }
      const stillRunning = result.status === 'running'
      // 请求失败：保持现状（**不能**据此认为这一轮结束了），稍后重试。
      if (result.status === 'unknown') {
        timer = setTimeout(() => void tick(), 3000)
        return
      }
      // ★ v1.65：判"本会话这一轮在本浏览器里有没有 SSE 在跑"，要看**这个
      //   会话**是否在 `sendingSessionIds` 里 —— 不能再用全局 `sending`
      //   （它现在表示"任意会话在跑"，会把别的会话的跑算成本会话的跑）。
      const localRunning = () => getChatSnapshot().sendingSessionIds.includes(sessionId)
      if (stillRunning) {
        sawRunning = true
        /*
          ★ v1.79：SSE 正常流着的时候把轮询降到 5 秒。

          流式期间每 1.5 秒还打一次 `/turns`，是同一份事实的第二条通道 ——
          而它换来的只是"done 帧丢了也能收尾"这一层保险。流在的时候，
          保险可以稀一点；流不在（刷新、断线）才需要 1.5 秒的密度。
        */
        if (!localRunning()) {
          void refreshActiveMessages(sessionId)
          timer = setTimeout(() => void tick(), 1500)
        } else {
          timer = setTimeout(() => void tick(), 5000)
        }
        return
      }
      // 服务端说没在跑了。
      if (localRunning()) {
        // 本页还以为在跑。两种可能：
        //   (a) 刚发出去，服务端还没建 running 行 —— 再等一拍看看；
        //   (b) 流早断了、本地 sending 卡死 —— 清掉它（否则界面永远转圈）。
        // 判据：见过 running 之后再消失 = 真结束((b))；从没见过 = 大概率(a)。
        if (sawRunning) {
          clearStaleSending(sessionId)
          void refreshActiveMessages(sessionId)
          void loadSessions()
          void refreshStuckTurn(sessionId)
          // ★ v1.68.2：done 帧/轮询可能比轮次落库（status→done）先到，这一拍
          //   拉到的 `session.running` 可能仍是 true。1 秒后再权威补拉一次列表，
          //   让侧栏转圈点跟着服务端真正收尾 —— 否则要刷新网页它才灭。
          window.setTimeout(() => {
            if (alive) void loadSessions()
          }, 1000)
          return
        }
        timer = setTimeout(() => void tick(), 1500)
        return
      }
      // 本页也没在跑：说明流早断了、服务端这轮已收尾 —— 把尾巴接上。
      void refreshActiveMessages(sessionId)
      void loadSessions()
      void refreshStuckTurn(sessionId)
      // ★ v1.68.2：同上，1 秒后权威补拉一次，收掉可能残留的侧栏转圈点。
      window.setTimeout(() => {
        if (alive) void loadSessions()
      }, 1000)
      // 不再排下一拍（SSE 结束后 sending 变 false，这个 effect 依赖 sending
      // 会重跑一次 listTurns；真没活动了就没有轮询开销）。
    }

    void tick()
    return () => {
      alive = false
      if (timer !== null) clearTimeout(timer)
    }
    // 依赖 `sending`：一轮 SSE 刚结束/刚开始时重新评估要不要接着轮询。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active?.id, sending])

  /**
   * ★ v1.22：手动复查一次"这一轮还能不能继续"。
   *
   * 抽成函数的理由：`handleRetryError` 跑完之后要知道黄条该不该收起来
   * （它可能已经 done 了）。与上面那个 effect 是同一段逻辑，不抽出来就
   * 会漂成两份 —— 而两份口径不一致时的症状是"点了重试，黄条和红条同时
   * 挂在屏幕上"，用户不知道该看哪个。
   */
  async function refreshStuckTurn(sessionId: string) {
    const runs = await listTurns(sessionId)
    // 只在用户还停在同一个会话时才写状态（请求是异步的，期间可能切走）。
    if (active?.id !== sessionId) return
    // null = 请求失败：保持原状，不要把它当成"没有可继续的轮次"。
    if (!runs) return
    setLastRuns(runs)
    setStuckTurn(runs.find((run) => run.resumable) ?? null)
  }

  /**
   * ★ v1.22：错误条上「重试」指向哪一轮。
   *
   * 判据：最近一次 `listTurns` 里**状态为 `failed` 的最近那条**。为什么
   * 不是 `interrupted`：`interrupted` 由后台 resumer 自动接手（黄条那条路），
   * 用户不需要点；而 `failed` 是我们已经放弃自动接手的（重试次数用尽或
   * 确定性失败，例如密钥无效），只有人工介入才可能好 —— 那正是这个按钮。
   *
   * 取最近一条而不是全部：重试是按轮次的，一次只推一轮。
   */
  const retryableRunId = lastRuns.find((run) => run.status === 'failed')?.id ?? null

  /**
   * ★ v1.24：正在跑的那一轮是不是**当前这个会话**的。
   *
   * 只有它才该把输入框变成「插话」。跑在别的会话里时，当前会话根本没有
   * 在读历史的轮次 —— 插进去的话永远不会被读到（实测事故见 `chat.ts` 里
   * `sendingSessionId` 的注释：A 会话长任务在跑，用户在新建的 B 会话发
   * 「你好」被当成插话，B 里就永远停在「模型在推理…」）。
   */
  const sendingHere = sendingSessionIds.includes(active?.id ?? '')
  /**
   * 有**别的**会话在本浏览器里跑着。
   *
   * ★ v1.65：这不再阻止当前会话发送（后端本来就支持多会话并发，CyberStrikeAI
   *   也是请求级独立并发）。它只用来给一句温和的提示，绝不当"不能发"的闸。
   *   老设计拿它当闸，于是"一个长会话跑着 → 新建会话发不出去"（用户实测）。
   */
  const sendingElsewhere = sendingSessionIds.some((id) => id !== active?.id)

  /**
   * ★ v1.49：**服务端**说这个会话还有一轮在跑（`lastRuns` 里最近一条是
   * `running`）。
   *
   * 为什么光靠本地的 `sending` 不够：`sending` 只是**这个浏览器**握着的
   * SSE 流的本地布尔。done 帧一旦丢了（网络抖动、切页面、关了又开），
   * 本地 `sending` 会变成 false —— 可服务端那一轮**还在跑**。此时用户再
   * 发一条，老代码会起一条新轮次，两条并行（老轮还在干、新轮 round=0 空转）
   * = 用户看到的「问一句停一次 / 一直在循环」。
   *
   * 后端 P0 已经用 409 `TURN_ALREADY_RUNNING` 兜住了这条；这里把同一事实
   * 反映到 UI 上：本地 `sending` 已经跟不上服务端时，也要把输入框变成
   * 「插话」并给一句提示，而不是让用户以为可以开第二轮。
   */
  const serverRunning =
    !sendingHere &&
    (lastRuns[0]?.status === 'running' ||
      lastRuns.some((run) => run.status === 'running') ||
      // ★ v1.61：轮询说服务端还在跑（`pollTurnProgress` 写进 `serverTurn`）。
      //   这条比 `lastRuns` 更新（`lastRuns` 只在进入会话/一轮结束时刷新），
      //   刷新页面回到一个正在跑的长任务时，靠它立刻把界面切成"在跑"。
      serverTurn !== null)

  /** 当前会话这一轮正在跑（本地在跑，或服务端说还在跑）—— Enter 是插话。 */
  /*
    题目在服务端的**当前**状态：会话详情里那份是打开时的快照，看板 store
    才是活的（见上面的订阅）。两者都没有时退回快照。
  */
  const liveChallenge = active?.challenge
    ? (kernelChallenges.find((item) => item.id === active.challenge?.id) ?? null)
    : null
  const challengeStatus = liveChallenge?.status ?? active?.challenge?.status ?? null
  const challengeRunning =
    challengeStatus !== null &&
    ['environment_ready', 'solving', 'submitting'].includes(challengeStatus)

  /*
    题目在跑 → 每 5 秒刷一次看板 store（它按需轮询自己的接口）。
    到终态立刻停，不留一个永远转的定时器。
  */
  useEffect(() => {
    if (!active?.challengeId || !challengeRunning) return
    const timer = window.setInterval(() => {
      void loadKernel({ silent: true })
    }, 5000)
    return () => window.clearInterval(timer)
  }, [active?.challengeId, challengeRunning])

  const turnRunningHere = sendingHere || serverRunning

  /*
    ★ v1.79：归档的会话不能发。

    从「已归档」里点开一个会话时，输入框照常可用、发送按钮照常亮 ——
    一按发送后端固定回"这个对话已归档"。用户得到的是一个看起来能用、
    实际必然失败的操作。
  */
  // v1.92：归档会话也可以直接发 —— 后端会在发送时自动恢复。
  const canSend = draft.trim().length > 0 && active !== null

  async function handleNew() {
    const session = await createSession()
    if (session) {
      setSearchParams({ session: session.id }, { replace: true })
      setDraft('')
      composerRef.current?.focus()
    }
  }

  /**
   * ★ v1.25：中断当前这一轮。
   *
   * 两个按钮落到同一个端点、两种 mode：
   * - 「中断并继续」（`continue`）：停下当前输出，把补充并入上下文后自动接着跑。
   * - 「彻底停止」（`stop`）：这一轮就地结束，后台不再自动接手。
   */
  async function submitInterrupt(mode: 'continue' | 'stop') {
    if (!active || interrupting) return
    setInterrupting(true)
    try {
      const ok = await interruptTurn(active.id, mode, interruptNote.trim())
      // ★ v1.68：不再"成功就静默关窗"（那正是用户说的"点了没反馈"）。
      //   两种结果都在弹窗里显示明确回执（读 store 里的 `lastInterrupt`）；
      //   成功时停留 1.4s 让用户看清"已请求…"，再自动关；失败则留在弹窗里
      //   显示服务端原话（尤其 409「当前没有在跑的回复」），由用户手动关。
      if (ok) {
        setInterruptNote('')
        window.setTimeout(() => {
          setInterruptOpen(false)
        }, 1400)
      }
    } finally {
      setInterrupting(false)
    }
  }

  async function handleSend() {
    if (!canSend || !active) return
    const content = draft.trim()

    // OMP 的 /compact：不发起模型轮次，只压当前会话上下文。
    if (content === '/compact') {
      setDraft('')
      const result = await compactSession(active.id)
      if (result) {
        showToast({
          message: t(
            'chat.a.compacted',
            { before: String(result.tokensBefore), after: String(result.tokensAfter) },
            `已压缩上下文：约 ${result.tokensBefore} → ${result.tokensAfter} tokens。`,
          ),
          tone: 'ok',
        })
      }
      return
    }

    // ★★ v0.100：跑着的时候，这一下是**插话**，不是发消息。
    //
    // 两条路径的差别不是措辞：`sendMessage` 会起一整轮（新的模型调用、
    // 新的工具额度），而 `interject` 只往库里写一句话、由正在跑的那一轮
    // 在下一轮开头读到。混用会让用户在模型跑的时候莫名开启第二轮，
    // 而那一轮的上下文与第一轮并行 —— seq 顺序、工具预算都会乱。
    //
    // ★ v1.65：不再挡「另一个会话在跑」。别的会话在跑，不影响当前会话发一条
    //   新消息（后端支持多会话并发）。`turnRunningHere` 只在本会话有轮次时
    //   才为真，所以这里走的是正确的分支：本会话跑着 → 插话；本会话没跑 →
    //   正常发（即便别的会话在跑）。

    if (turnRunningHere) {
      setDraft('')
      setInterjecting(true)
      try {
        const outcome = await interject(active.id, content)
        if (outcome === 'no-turn') {
          // ★ v1.24：服务端说这一轮其实早就结束了（我们这边的 `sending`
          //   是过期的 —— done 帧丢了的典型症状）。把过期状态清掉，退回
          //   "正常发送"，否则这句话会被静默吞掉（用户以为发了，没人回答）。
          clearStaleSending(active?.id)
          const ok = await sendMessage(active.id, content)
          if (!ok) setDraft(content)
        } else if (outcome === 'failed') {
          setDraft(content)
        }
      } finally {
        setInterjecting(false)
      }
      return
    }

    // ★ 从**原始 draft** 里解析提及，而不是从 trim 后的 content：
    //   两者只差首尾空白，但用同一个来源能避免"编辑器看到的和发出去的
    //   不是同一段字"这类难以复现的偏差。
    const hinted = resolveMentions(draft, tools)
    // ★ 先清输入框再发（v0.97）。原来是 `await sendMessage(...)` 之后才清，
    //   而那一等是整整一轮（模型想 + 可能十几次工具调用，实测能到 30 秒）。
    //   期间输入框里字还在、屏幕上也没有新气泡，用户看到的是"点了发送，
    //   什么都没发生"—— 于是他会再点一次。现在点下即清、即出气泡。
    //   失败时下面 catch 会把原文放回框里，不会丢字。
    setDraft('')
    const ok = await sendMessage(active.id, content, undefined, hinted)
    if (!ok) setDraft(content)
    setMention(null)
  }

  /**
   * 手动接着跑一轮中断的对话（v1.19）。
   *
   * ★ 为什么要有这个按钮，而不是只靠后台自动接手：后台要等心跳过期
   *   （90 秒）才敢判定"那一轮没人跑了"—— 那个延迟是必须的，否则会把
   *   正在跑的一轮当成死的，造出两轮并发写同一个会话。但用户**眼看着**
   *   对话停住的时候，他知道那一轮已经死了，不需要等 90 秒。
   *
   * 失败时不把提示条收起来：服务端状态没变，用户还能再点一次；
   * 收起来反而会让人以为"点成功了但没反应"。
   */
  async function handleResume() {
    if (!active || !stuckTurn || resuming) return
    setResuming(true)
    try {
      await resumeTurn(active.id, stuckTurn.id)
      setStuckTurn(null)
    } catch (err) {
      /*
        不能只把异常吞掉。服务端拒绝接手时（轮次已经结束 / 还在跑）
        原来界面毫无反馈，用户看到的就是「点了没反应」。
        这里把原因弹出来，并按服务端最新状态重算黄条 —— 如果确实
        已经不可接手，黄条应当消失，而不是继续挂着让人反复点。
      */
      showToast({
        tone: 'risk',
        message: t('chat.a.resumeFailed', { error: errorMessage(err) }, `继续失败：${errorMessage(err)}`),
      })
      await refreshStuckTurn(active.id)
    } finally {
      setResuming(false)
    }
  }

  /**
   * ★ v1.22：错误条上的「重试」。
   *
   * 与 `handleResume`（黄条）的区别只在**触发时机**：黄条是服务端主动
   * 告诉我们"这一轮还能继续"（心跳判定），这里是用户在红色错误条上
   * 自己决定再试一次。两者落到同一个端点、同一个"从断点接着跑"的语义。
   */
  async function handleRetryError() {
    const runId = retryableRunId
    if (!active || !runId || resuming) return
    setResuming(true)
    try {
      await resumeTurn(active.id, runId)
      setStuckTurn(null)
    } catch {
      /* 新的失败会经 store 的 `error` 重新挂上错误条，保持可再点 */
    } finally {
      setResuming(false)
      // 无论成败都重新对一次账：这一轮可能已经 done，黄条要收起来。
      void refreshStuckTurn(active.id)
    }
  }

  /**
   * 选中补全项：把 `@正在输入` 换成 `@工具名 `。
   *
   * 末尾那个空格是刻意的 —— 换成 `@run_shell` 之后如果还要接别的字，
   * 没有分隔符的话下一次 `parseMentionQuery` 会把新字当成提及的一部分，
   * 补全层会一直挂着不走。
   */
  function handlePickMention(name: string) {
    if (!mention) return
    const caret = composerRef.current?.selectionStart ?? draft.length
    const next = `${draft.slice(0, mention.start)}@${name} ${draft.slice(caret)}`
    setDraft(next)
    setMention(null)
    // 光标放到插入内容之后。`requestAnimationFrame` 是因为受控 textarea
    // 要等 React 把 value 刷进 DOM 之后设 selectionStart 才不会被重置回末尾。
    requestAnimationFrame(() => {
      const el = composerRef.current
      if (!el) return
      const pos = mention.start + name.length + 2
      el.focus()
      el.setSelectionRange(pos, pos)
    })
  }

  async function handleArchive(id: string) {
    await archiveSession(id)
    if (wantedId === id) {
      setSearchParams({}, { replace: true })
    }
  }

  /**
   * 下载会话 Markdown。
   *
   * ★ 一定要**先拉详情**：`sessions` 里只有元信息，没有 `messages`。
   *   拿 `active.messages` 只在"导出的正是当前打开的那个会话"时才对，
   *   而右键菜单对**任意**一条都可用。
   */
  async function handleDownload(id: string, detail: MarkdownDetail) {
    try {
      const full = await api.get<ChatSessionDetail>(`/chat/sessions/${id}`)
      const filename = buildConversationMarkdownFileName(full, detail)
      downloadMarkdownFile(filename, buildConversationMarkdown(full, detail))
      /*
        ★ v1.79：下载成功要说一声。

        浏览器对"下载"几乎没有可见反馈（有的浏览器完全不弹任何东西），
        而这一次点击背后还有一次网络请求 —— 用户没有任何依据判断
        "文件到底有没有下来"。原来成功分支完全静默。
      */
      showToast({
        message: t('chat.a.downloaded', { filename }, `已下载 ${filename}`),
        tone: 'ok',
      })
    } catch (error) {
      /*
        导出失败**不**改全局 error 状态：那会把整页推进错误态，
        而用户只是没下到一个文件。但也不能只在控制台留痕 ——
        用户点了菜单项、什么都没发生，会以为菜单坏了。
      */
      showToast({
        message: t(
          'chat.a.exportFailed',
          { error: errorMessage(error) },
          `导出 Markdown 失败：${errorMessage(error)}`,
        ),
        tone: 'risk',
      })
    }
  }

  function openRename(target: { id: string; title: string }) {
    setRenameTarget(target)
    setRenameDraft(target.title)
  }

  /**
   * 打开「攻击链」：先确保拿到那条会话的**消息**。
   *
   * ★ 当前正在看的会话直接用 `active.messages`，省一次请求；别的会话才去拉。
   *   判断依据是 `active.id`，不是"会话在列表里"——列表里只有元信息。
   *
   * ★ 分页（2026-11）之后还要看 `hasEarlier`：长会话的 `active.messages`
   *   只是最后一页。直接拿它去拼攻击链，会**静悄悄地**只分析后半段 ——
   *   而攻击链恰恰最需要开头那几步。没载全就照常去拉全量。
   */
  async function openAttackChain(session: { id: string; title: string }) {
    const complete = active?.id === session.id && !active.hasEarlier
    const cached = complete && active ? active.messages : null
    setChainView({
      open: true,
      title: session.title,
      loading: cached === null,
      error: null,
      messages: cached ?? [],
    })
    if (cached !== null) return
    try {
      const messages = await fetchSessionMessages(session.id)
      setChainView((view) => (view.open ? { ...view, loading: false, messages } : view))
    } catch (error) {
      setChainView((view) =>
        view.open ? { ...view, loading: false, error: errorMessage(error) } : view,
      )
    }
  }

  /** 取关联漏洞。`sessionId` 单独存一份，重试时不用再问调用方要。 */
  async function loadFindings(sessionId: string, title: string) {
    setFindingsView((view) => ({
      ...view,
      open: true,
      title,
      loading: true,
      error: null,
      sessionId,
    }))
    try {
      const { scanIds, items } = await fetchSessionFindings(sessionId)
      setFindingsView((view) =>
        view.sessionId === sessionId ? { ...view, loading: false, scanIds, items } : view,
      )
    } catch (error) {
      setFindingsView((view) =>
        view.sessionId === sessionId
          ? { ...view, loading: false, error: errorMessage(error) }
          : view,
      )
    }
  }

  function openFindings(session: { id: string; title: string }) {
    void loadFindings(session.id, session.title)
  }

  async function submitRename() {
    if (!renameTarget) return
    const next = renameDraft.trim()
    if (!next || next === renameTarget.title) {
      setRenameTarget(null)
      return
    }
    setRenaming(true)
    try {
      await renameSession(renameTarget.id, next)
      setRenameTarget(null)
    } finally {
      setRenaming(false)
    }
  }

  async function handleMoveTo(id: string, project: string | null) {
    await updateSettings(id, { project })
  }

  async function handleBatchMoveTo(ids: string[], project: string | null) {
    setBatchBusy(true)
    try {
      for (const id of ids) await updateSettings(id, { project })
      await loadSessions()
    } finally {
      setBatchBusy(false)
    }
  }

  /**
   * 批量删除（= 归档）。返回失败条数。
   *
   * ★ 逐个循环而不是 batch 端点：与 CyberStrikeAI 的做法一致
   *   （`chat.js:11121`），详见 `SessionBatchModal` 顶部注释。
   */
  /**
   * 新建分组并把 `ids` 放进去（v1.20）。
   *
   * 与 `handleBatchMoveTo` 共用同一条路径 —— 新建分组的最后一步**就是**
   * 一次普通移动，多的只有"名字校验"这一层，而校验在 `GroupNameDialog` 里。
   * 分成两份实现的话，一处改了另一处必然漂开。
   */
  async function handleCreateGroup(ids: string[], name: string) {
    await handleBatchMoveTo(ids, name)
    setNewGroupFor(null)
  }

  async function handleBatchArchive(ids: string[]): Promise<string[]> {
    setBatchBusy(true)
    const failed: string[] = []
    try {
      for (const id of ids) {
        const ok = await archiveSession(id)
        /*
          ★ 记下**哪几条**失败，而不只是失败条数（v1.79）。

          批量弹窗会把失败的 id 留在选中里，用户可以直接重试；
          只给一个数字的话，他得在一长串里重新找出是哪几条。
        */
        if (!ok) failed.push(id)
        if (wantedId === id) setSearchParams({}, { replace: true })
      }
      await loadSessions()
    } finally {
      setBatchBusy(false)
    }
    return failed
  }

  /**
   * 点空态里的「可以这样问」——**真的把它发出去**（v0.102）。
   *
   * ## 修的是什么
   *
   * 原来这里只有 `setDraft(text)` + 聚焦输入框。而空态下 `active === null`，
   * 输入框在整段渲染里是**禁用**的（占位文案「请先新建或打开一个对话」），
   * 所以用户点了引导项之后：草稿写进去了、但看不见也发不出去 ——
   * 表现就是「点了没反应」。
   *
   * ## 为什么直接建会话 + 发送，而不是"先建会话再把草稿填好"
   *
   * 引导项是**整句话的提问**，不是半个模板。用户点它的意图就是"我要问这个"。
   * 还要再点一次发送等于让用户为自己的点击确认一次，那与"引导"的语义相反。
   *
   * ## 失败时不留残骸
   *
   * 建会话失败 → 什么都不做（`createSession` 已经 patch 了 error）。
   * 发送失败 → 会话留着（它已经建好了，用户可以在里面重新问），
   * 但把草稿填回输入框，让那句话不丢。
   *
   * ⚠️ `sending` 期间按钮要禁用（见渲染处），否则连点两下会建出两个会话。
   */
  async function handlePickStarter(text: string) {
    // ★ v1.65：只防"连点建出两个会话"，不再因**别的**会话在跑而拦下
    //   （后端支持多会话并发；欢迎屏时 `active` 是 null，全局 sending 判断
    //   也会因为「别的会话在跑」而误伤）。
    if (pickingStarter) return
    setPickingStarter(true)
    try {
      const session = await createSession()
      if (!session) return
      setSearchParams({ session: session.id }, { replace: true })
      // 先不填草稿：那句话马上会以乐观气泡的形式出现在消息流里，
      // 再把它留在输入框就是同屏两份。
      setDraft('')
      const ok = await sendMessage(session.id, text)
      if (!ok) setDraft(text)
    } finally {
      setPickingStarter(false)
    }
  }

  /** 打开人格弹窗并提交选择。 */
  async function handlePickPersona(personaId: string | null) {
    setPersonaOpen(false)
    if (!active) return
    await updateSettings(active.id, { persona: personaId })
  }

  async function handleUpload(file: File) {
    if (!active) return
    setUploading(true)
    try {
      await uploadAttachment(active.id, file)
    } finally {
      setUploading(false)
    }
  }

  /**
   * 从某条消息分叉：复制截至它的历史到新会话，然后直接切过去。
   * 失败由 store 写进 error 条，这里只负责导航。
   */
  const handleFork = useCallback(
    async (seq: number) => {
      if (!active) return
      const created = await forkSession(active.id, seq)
      if (!created) return
      setSearchParams({ session: created.id }, { replace: true })
      setShowList(false)
      showToast({
        message: t('chat.a.forkedContinue', undefined, '已创建分支，可以从这里换一条路继续。'),
        tone: 'ok',
      })
    },
    [active, setSearchParams, t],
  )

  async function handleDecide(messageId: string, approve: boolean) {
    if (!active) return
    await decideApproval(active.id, messageId, approve)
  }

  // 上下文占用：会话打开、消息数变化、压缩边界变化时刷新；失败就不显示。
  useEffect(() => {
    if (!active) {
      setContextUsage(null)
      return
    }
    let alive = true
    void fetchContextUsage(active.id).then((usage) => {
      if (alive) setContextUsage(usage)
    })
    return () => {
      alive = false
    }
  }, [active?.id, active?.messageCount, active?.compactions?.length])

  /*
    ★ `messages` 自己也要 memo：`active?.messages ?? []` 在没有会话时每次
      渲染都造一个新数组，下游 `turns` 的 useMemo 就永远命中不了
      （`react-hooks/exhaustive-deps` 也会指着这一行）。
  */
  const messages = useMemo(() => active?.messages ?? [], [active])
  // ★ 按回合切：用户消息开一个回合，它之后的助手/工具消息都归同一回合。
  const turns = useMemo(() => groupConversationTurns(messages), [messages])

  /*
    ★ 正在跑的那一轮必须**在原地**变成 live，而不是在消息流末尾另起一块。

    store 把这一轮的旁白 / 工具 / 答复**边跑边落库**。如果这轮仍按
    "已完成"渲染，用户会看到页脚先冒出来、正文却还挂在回合外面 ——
    同一个回合被画成两半，等 `TURN_DONE` 一到整段又跳位。

    判据：`sendingHere` 且乐观气泡已经消失（= 这句用户消息已落库，
    它后面的回合就是正在进行的那一轮）。乐观气泡还在时用户消息还没进
    `messages`，没有"进行中的回合"可以复用，只能在流末尾另画一块。
  */
  const liveTurnIndex = sendingHere && pendingUserContent === null ? turns.length - 1 : -1

  /*
    ★ v1.30：live 阶段行上的「已 N 次工具」计数。

    `liveTurnIndex >= 0` 时（那句用户消息已落库、回合在原地 live），计数器
    直接从**当前这一轮已经落库的消息**里数，不需要后端再推一个新字段 ——
    `turn.items` 本来就随每轮工具结果刷新。

    `liveTurnIndex < 0` 时（乐观气泡还在、这一轮还没有回合可归），消息流里
    没有正在进行的回合，但**前面几轮的**工具数并不代表这一轮，所以不冒充：
    此时返回 undefined，阶段行就只显示轮次文案。
  */
  const liveLooseToolCount =
    liveTurnIndex >= 0 ? turnToolCount(turns[liveTurnIndex].items) : undefined

  /*
    按项目分组。

    ★ 「无项目」必须排**最后**且有一个显式标题，而不是当成
      `undefined` 悄悄并进第一组：用户新建的对话默认都没有项目，
      让它排在前面会把真正的项目组挤到下面，而"我分好的东西在哪"
      是分组功能唯一要解决的问题。

    ★ 空项目名不建组：后端把空串归一成 `null`（见 `update_settings`），
      但历史数据里可能有 `""`。这里再挡一次 —— 一个看不见名字的
      分组会让人以为是渲染 bug。
  */
  const childrenByParent = useMemo(() => {
    const map = new Map<string, typeof sessions>()
    for (const session of sessions) {
      if (!session.parentSessionId) continue
      const bucket = map.get(session.parentSessionId)
      if (bucket) bucket.push(session)
      else map.set(session.parentSessionId, [session])
    }
    return map
  }, [sessions])

  const grouped = useMemo(() => {
    const buckets = new Map<string, typeof sessions>()
    for (const session of sessions) {
      if (session.parentSessionId) continue
      const key = session.project?.trim() || ''
      const bucket = buckets.get(key)
      if (bucket) bucket.push(session)
      else buckets.set(key, [session])
    }
    const named = [...buckets.entries()]
      .filter(([key]) => key !== '')
      .sort(([a], [b]) => a.localeCompare(b))
    const loose = buckets.get('') ?? []
    return { named, loose }
  }, [sessions])

  /* 回合页脚右侧那点元信息：模型现在是谁。 */
  const activePersonaEntry = active?.persona
    ? (personas.find((p) => p.id === active.persona) ?? null)
    : null
  const activePersona = activePersonaEntry
  const sessionMeta = activePersonaEntry
    ? t(`persona.${activePersonaEntry.id}.label`, undefined, activePersonaEntry.label)
    : null
  /* 子代理面板的数据：主对话 → 它的直接子代理；当前在子代理里时，
     面板展示同一主对话下的兄弟，并给出返回主线。 */
  const parentSession =
    active && active.parentSessionId
      ? (sessions.find((item) => item.id === active.parentSessionId) ?? null)
      : null
  const mainSession = active && active.parentSessionId ? parentSession : active
  const hubChildren = mainSession ? (childrenByParent.get(mainSession.id) ?? []) : []

  /*
    设置控件放在输入卡工具栏里（CyberStrikeAI 的形态）。

    ★ 它读的是**这个会话**的设置（`active.project` 等），不是全局默认值。
      每个会话各自记着自己的项目/人格/模型 —— 一个全局条会让人以为
      改一下所有对话都变了。
  */
  const settingsBar = active ? (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
      {/*
        窄屏时模型选择收进设置面板：输入行在手机上放不下
        「附件 + 输入 + 模型 + 齿轮 + 发送」五样东西。
      */}
      <div className="sm:hidden">
        <ProviderSelect
          providers={usableProviders}
          value={active.providerId ?? null}
          onChange={(providerId) => void updateSettings(active.id, { providerId })}
        />
      </div>

      <ProjectInput
        value={active.project ?? null}
        suggestions={projectSuggestions}
        onCommit={(project) => void updateSettings(active.id, { project })}
      />

      <button
        type="button"
        onClick={() => setPersonaOpen(true)}
        className={cn(
          'inline-flex items-center gap-1 rounded-sm border border-line px-2 py-1',
          'text-2xs text-ink-muted transition-ui duration-fast',
          'hover:border-accent/40 hover:text-ink',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
        )}
        title={t('chat.a11y.persona', undefined, '这个人格决定模型扮演什么角色')}
      >
        <UserRound size={11} strokeWidth={2} aria-hidden="true" />
        {activePersona ? t(`persona.${activePersona.id}.label`, undefined, activePersona.label) : t('persona.none', undefined, '未选择角色')}
      </button>

      <ApprovalToggle
        enabled={active.requireApproval ?? false}
        onChange={(requireApproval) => void updateSettings(active.id, { requireApproval })}
      />

      <InlineSelect
        label={t('chat.a11y.autoApprove', undefined, '自动放行')}
        title={t('chat.a11y.autoApproveHint', undefined, '在所选时间内自动批准需要确认的工具调用；到期自动恢复。')}
        value={(() => {
          const until = active.autoApproveUntil
            ? new Date(active.autoApproveUntil).getTime()
            : 0
          if (!until || until <= Date.now()) return 0
          const left = (until - Date.now()) / 3_600_000
          return left <= 2 ? 2 : left <= 4 ? 4 : 8
        })()}
        options={[
          { value: 0, label: t('chat.a.autoApproveOff', undefined, '关闭') },
          { value: 2, label: '2h' },
          { value: 4, label: '4h' },
          { value: 8, label: '8h' },
        ]}
        onChange={(hours) =>
          void updateSettings(active.id, { autoApproveHours: hours })
        }
      />

      <MultiSelect
        label={t('chat.a11y.skills', undefined, '技能')}
        icon={<BookOpen size={12} strokeWidth={2} aria-hidden="true" />}
        options={skillOptions}
        selected={active.skillNames ?? []}
        emptyHint={t('chat.a.skillsEmpty', undefined, '技能库是空的')}
        onChange={(skillNames) => void updateSettings(active.id, { skillNames })}
      />

      {/*
        ★ v1.80：恢复「MCP」挂载下拉。

        v1.79 移除它的理由是真的：当时对话引擎没有任何 MCP 客户端，
        勾选只存了一个偏好，模型既看不到工具也调不了。现在引擎在每轮
        开始前按 `mcp_server_names` 并行试连、把工具以
        `mcp__<server>__<tool>` 暴露给模型（见 `app/chat/mcp_tools.py`），
        这个控件才第一次有真实效果。

        ★ 旁边那行状态是**必须**的：只把勾选回显出来，用户仍然无法判断
          "它到底连上了没有"。`/mcp-status` 会真连一次，把已挂载/连通/
          失败原因摆出来 —— 这样「勾了没效果」在界面上是可诊断的。
      */}
      <MultiSelect
        label="MCP"
        icon={<Plug size={12} strokeWidth={2} aria-hidden="true" />}
        options={mcpOptions}
        selected={active.mcpServerNames ?? []}
        emptyHint={t('chat.a.mcpEmpty', undefined, '还没配 MCP server（在工具页加）')}
        onChange={(mcpServerNames) => void updateSettings(active.id, { mcpServerNames })}
      />
      <McpMountStatus status={mcpStatus} loading={mcpStatusLoading} />

      {/*
        ★ 「完成」与上面几个控件**同一行**、右对齐。
        之前它单独占一行（chips 在上、按钮在下），视觉上是一个台阶：
        一行 28px 的控件后面空出一整行再放按钮，既浪费高度也显得没对齐。
        窄屏 chips 换行时 `ml-auto` 会把它推到最后一个换行行的右端。
      */}
      <button
        type="button"
        onClick={() => setSettingsOpen(false)}
        className="ml-auto shrink-0 rounded-sm border border-line px-2 py-1 text-2xs text-ink-muted transition-ui duration-fast hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
      >
        {t('chat.a.settingsDone', undefined, '完成')}
      </button>
    </div>
  ) : null

  /**
   * 渲染一个会话及其子代理分支。
   *
   * 子会话是普通会话行，只是缩进一级；父会话被删时数据库 CASCADE 会一起删。
   */
  const renderSession = (session: ChatSession, depth = 0): React.ReactNode[] => {
    const children = childrenByParent.get(session.id) ?? []
    const collapsed = collapsedParents.has(session.id)
    return [
    <SessionRow
      key={session.id}
      session={session}
      active={active?.id === session.id}
      depth={depth}
      running={Boolean(session.running)}
      hasChildren={children.length > 0}
      collapsed={collapsed}
      onToggleChildren={() =>
        setCollapsedParentList((prev) =>
          prev.includes(session.id)
            ? prev.filter((id) => id !== session.id)
            : [...prev, session.id],
        )
      }
      onOpen={() => {
        setSearchParams({ session: session.id }, { replace: true })
        setShowList(false)
      }}
      onArchive={() =>
        setConfirmAction({
          kind: 'archive',
          id: session.id,
          title: session.title,
        })
      }
      onContextMenu={(anchor) =>
        setMenuAnchor({
          ...anchor,
          sessionId: session.id,
          title: session.title,
          archived: Boolean(session.archived),
          pinned: Boolean(session.pinned),
          project: session.project ?? null,
        })
      }
    />,
    ...(collapsed
      ? []
      : children.flatMap((child) => renderSession(child, depth + 1))),
    ]
  }

  return (
    <div className="flex min-h-0 flex-1">
      {/* ── 左：会话清单（窄屏收进抽屉） ── */}
      <aside
        className={cn(
          'flex w-60 shrink-0 flex-col border-r border-line bg-surface-sunken',
          // 窄屏：覆盖式浮层。`hidden` 而不是 `w-0` —— 后者仍会被读屏器读到
          showList
            ? 'absolute inset-y-0 left-0 z-30 shadow-lg'
            : listCollapsed
              ? 'hidden'
              : 'hidden md:flex',
        )}
      >
        <div className="flex items-center gap-tight border-b border-line px-3 py-2">
          <Button variant="secondary" size="sm" onClick={handleNew} className="flex-1">
            <MessageSquarePlus size={14} strokeWidth={2} aria-hidden="true" />
            {t('chat.a.newChat', undefined, '新对话')}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setShowList(false)}
            className="md:hidden"
            aria-label={t('chat.collapseList')}
          >
            <X size={14} strokeWidth={2} aria-hidden="true" />
          </Button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
          {status === 'loading' && sessions.length === 0 ? (
            <div className="flex flex-col gap-1 p-1">
              <Skeleton className="h-8" />
              <Skeleton className="h-8" />
              <Skeleton className="h-8" />
            </div>
          ) : status === 'error' && sessions.length === 0 ? (
            /*
              ★ 只有**一条都没拿到**时才显示错误块。
                已有列表时的失败只在底部挂一行 error（见下面），
                不把列表换成错误页 —— 用户很可能只是网抖了一下，
                把已看到的对话抹掉比"没刷新"严重得多。
            */
            <div className="p-1.5">
              <ErrorState
                error={error}
                onRetry={() => void loadSessions()}
                title={t('chat.a11y.noSessions', undefined, '拿不到会话列表')}
                compact
              />
            </div>
          ) : grouped.named.length === 0 && grouped.loose.length === 0 ? (
            <p className="px-2 py-6 text-center text-xs text-ink-faint">
              {t('chat.a.noSessions', undefined, '还没有对话。点上面的「新对话」开始。')}
            </p>
          ) : (
            <div className="flex flex-col gap-2">
              {grouped.named.map(([project, items]) => (
                <section key={project}>
                  <h2 className="flex items-center gap-1 px-2 py-1 text-2xs uppercase tracking-wide text-ink-faint">
                    <Folder size={10} strokeWidth={2} aria-hidden="true" />
                    <span className="truncate">{project}</span>
                  </h2>
                  <ul className="flex flex-col gap-0.5">
                    {items.map((session) => renderSession(session))}
                  </ul>
                </section>
              ))}

              {grouped.loose.length > 0 && (
                <section>
                  {/*
                    ★ 「无项目」的标题在**有**命名项目时才出现。
                      只有一堆无项目对话时，顶上一行「无项目」是纯噪音。
                  */}
                  {grouped.named.length > 0 && (
                    <h2 className="px-2 py-1 text-2xs uppercase tracking-wide text-ink-faint">
                      {t('chat.a.noProject', undefined, '无项目')}
                    </h2>
                  )}
                  <ul className="flex flex-col gap-0.5">
                    {grouped.loose.map((session) => renderSession(session))}
                  </ul>
                </section>
              )}

              {/*
                ── 「已归档」区 ────────────────────────────────────

                ★ 默认收起、有条数才出现。它是"回收站"：频率极低，但**必须
                  有入口** —— 否则「删除此对话」对用户就是单向黑洞，而
                  「彻底删除」也没有地方可以点。
              */}
              {archivedSessions.length > 0 && (
                <section className="mt-1 border-t border-line pt-1">
                  <div className="flex items-center gap-1">
                    <button
                      type="button"
                      onClick={() => setArchivedOpen((open) => !open)}
                      aria-expanded={archivedOpen}
                      className={cn(
                        'flex min-w-0 flex-1 items-center gap-1 rounded-sm px-2 py-1 text-2xs text-ink-faint',
                        'transition-ui duration-fast hover:text-ink',
                        'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
                      )}
                    >
                      {archivedOpen ? (
                        <ChevronDown size={10} strokeWidth={2} aria-hidden="true" />
                      ) : (
                        <ChevronRight size={10} strokeWidth={2} aria-hidden="true" />
                      )}
                      {t(
                        'chat.a.archived',
                        { n: archivedSessions.length },
                        `已归档（${archivedSessions.length}）`,
                      )}
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        setConfirmAction({ kind: 'purgeAll', n: archivedSessions.length })
                      }
                      aria-label={t('chat.a.purgeArchivedAria', undefined, '彻底删除全部已归档对话')}
                      title={t('chat.a.purgeArchived', undefined, '清空归档')}
                      className="shrink-0 rounded-sm p-1 text-ink-faint transition-ui duration-fast hover:text-signal-risk focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
                    >
                      <Trash2 size={11} strokeWidth={2} aria-hidden="true" />
                    </button>
                  </div>
                  {archivedOpen && (
                    <ul className="mt-0.5 flex flex-col gap-0.5">
                      {archivedSessions.map((session) => (
                        <SessionRow
                          key={session.id}
                          session={session}
                          archived
                          active={active?.id === session.id}
                          running={Boolean(session.running)}
                          onOpen={() => {
                            setSearchParams({ session: session.id }, { replace: true })
                            setShowList(false)
                          }}
                          onArchive={() => undefined}
                          onContextMenu={(anchor) =>
                            setMenuAnchor({
                              ...anchor,
                              sessionId: session.id,
                              title: session.title,
                              archived: Boolean(session.archived),
                              pinned: Boolean(session.pinned),
                              project: session.project ?? null,
                            })
                          }
                        />
                      ))}
                    </ul>
                  )}
                </section>
              )}
            </div>
          )}
        </div>
      </aside>

      {/* ── 右：消息流 + 输入框 ── */}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        {/* 顶部一行紧凑工具条（替代 80px 页面头） */}
        <div className="flex shrink-0 items-center gap-tight border-b border-line px-3 py-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setShowList(true)}
            className="md:hidden"
            aria-label={t('chat.a11y.openList', undefined, '打开会话列表')}
          >
            <MessagesSquare size={14} strokeWidth={2} aria-hidden="true" />
          </Button>
          {/* 宽屏折叠。折叠后按钮仍在工具条上，图标换成"展开"。 */}
          <Button
            variant="ghost"
            size="sm"
            onClick={toggleList}
            className="hidden md:inline-flex"
            aria-label={
              listCollapsed
                ? t('chat.a.expandList', undefined, '展开会话列表')
                : t('chat.a.collapseList', undefined, '折叠会话列表')
            }
            title={
              listCollapsed
                ? t('chat.a.expandList', undefined, '展开会话列表')
                : t('chat.a.collapseList', undefined, '折叠会话列表')
            }
          >
            {listCollapsed ? (
              <PanelLeftOpen size={14} strokeWidth={2} aria-hidden="true" />
            ) : (
              <PanelLeftClose size={14} strokeWidth={2} aria-hidden="true" />
            )}
          </Button>
          <h1 className="min-w-0 flex-1 truncate text-sm text-ink">
            {active?.title ?? t('chat.a.untitledChat', undefined, '对话')}
          </h1>
          {activePersona && (
            <button
              type="button"
              onClick={() => setPersonaOpen(true)}
              title={t(
                'chat.a.personaPromptHint',
                { summary: activePersona.summary },
                `${activePersona.summary}（角色说明已进入系统提示词）`,
              )}
              className="hidden shrink-0 rounded-sm border border-line px-1.5 py-0.5 text-2xs text-ink-muted transition-ui duration-fast hover:border-line-strong hover:text-ink sm:inline-flex"
            >
              {activePersona.label}
            </button>
          )}
          {active && hubChildren.length > 0 && (
            <button
              type="button"
              onClick={() => setAgentHubOpen(true)}
              aria-label={t('chat.agentHub.openPanel', undefined, '子代理')}
              title={t('chat.agentHub.openPanel', undefined, '子代理')}
              className={cn(
                'flex shrink-0 items-center gap-1 rounded-sm px-1.5 py-1 text-2xs',
                'max-sm:min-h-9 max-sm:min-w-9 max-sm:justify-center',
                'text-ink-faint transition-ui duration-fast hover:text-ink-muted',
                'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
              )}
            >
              <Users size={12} strokeWidth={2} aria-hidden="true" />
              <span className="hidden sm:inline">
                {t('chat.agentHub.button', { n: hubChildren.length }, `子代理 ${hubChildren.length}`)}
              </span>
            </button>
          )}
          {active && (
            <button
              type="button"
              onClick={() => setAnswersOnly((value) => !value)}
              aria-pressed={answersOnly}
              aria-label={t('chat.a11y.onlyAnswers', undefined, '只看回答')}
              title={
                answersOnly
                  ? t('chat.a.showWork', undefined, '显示工具与过程')
                  : t('chat.a.hideWork', undefined, '隐藏工具与过程，只看结论')
              }
              className={cn(
                'flex shrink-0 items-center gap-1 rounded-sm px-1.5 py-1 text-2xs',
                'max-sm:min-h-9 max-sm:min-w-9 max-sm:justify-center',
                'transition-ui duration-fast',
                answersOnly ? 'bg-accent/12 text-accent' : 'text-ink-faint hover:text-ink-muted',
                'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
              )}
            >
              {answersOnly ? (
                <EyeOff size={12} strokeWidth={2} aria-hidden="true" />
              ) : (
                <Eye size={12} strokeWidth={2} aria-hidden="true" />
              )}
              <span className="hidden sm:inline">{t('chat.onlyAnswers')}</span>
            </button>
          )}
          {active && (
            <span className="shrink-0 text-2xs text-ink-faint">
              {/*
                ★ 把乐观气泡计入条数。不记的话，点下发送的那几秒里屏幕上
                  右侧明明已经有一句话，右上角却还是「0 条消息」——
                  与空态文案是同一个形状的谎报（数字说没有，画面说已有）。
              */}
              {t('chat.messageCount', {
                n: messages.length + (sendingHere && pendingUserContent !== null ? 1 : 0),
              })}
            </span>
          )}
        </div>

        {/*
          题目条（CTF 页重做第 2 步）：从看板「查看」进来的会话，顶部固定
          一条题目状态 —— 进来的人带着「这题现在怎样了」的问题，翻回看板
          找答案会把他从正在读的过程里拽走。

          ★ 由**会话本身**的 challengeId 决定，不看来源页：刷新、换标签页
            打开同一个会话，这条都还在。
          ★ 题目被删时后端给 `challenge: null`，但 challengeId 仍有值 ——
            那种情况要出声（见 MissingChallengeBar），不能静默消失。
        */}
        {active?.challengeId &&
          (active.challenge ? (
            <ChallengeBar challenge={active.challenge} />
          ) : (
            <MissingChallengeBar />
          ))}

        {/* 消息流 */}
        <div className="relative flex min-h-0 flex-1 flex-col">
          <div ref={setScrollerRef} className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
            {!active ? (
              <div className="mx-auto flex max-w-[62ch] flex-col items-start gap-bay py-6">
                <EmptyState
                  icon={Sparkles}
                  title={t('chat.a11y.askHint', undefined, '有什么要查的、要做的？')}
                  description={
                    <>
                      {t(
                        'chat.a.emptyDescription',
                        undefined,
                        '这个对话框里的模型能直接调用平台的能力：发起扫描、看扫描结果、 翻技能库、试 MCP 连接。它做完会在下面贴出做了什么。',
                      )}
                    </>
                  }
                  action={
                    <Button onClick={() => void handleNew()} variant="primary" size="sm">
                      <MessageSquarePlus size={14} strokeWidth={2} aria-hidden="true" />
                      {t('chat.a.startChat', undefined, '开始新对话')}
                    </Button>
                  }
                />
                <div className="w-full">
                  <p className="mb-1.5 text-2xs uppercase tracking-wide text-ink-faint">
                    {t('chat.a.canAsk', undefined, '可以这样问')}
                  </p>
                  {/* disabled：点一下会建会话 + 发一轮消息，连点会开两个并发会话。 */}
                  <StarterPrompts
                    disabled={pickingStarter}
                    expanded={startersExpanded}
                    onToggle={() => setStartersExpanded((value) => !value)}
                    onPick={(prompt) => void handlePickStarter(prompt)}
                  />
                </div>
              </div>
            ) : (
              <div className="mx-auto flex w-full max-w-[880px] flex-col gap-5">
                {/*
                ★ 空态必须把乐观气泡算进来。只看 messages.length 的话，
                  用户点下发送的那 3 秒里，屏幕上会同时出现"这个对话还是空的"
                  和右侧自己刚发出去的那句话 —— 一句话既在又不在。
              */}
                {/*
                题目会话的第一屏：**先把过程摆出来**，再说"可以聊"。
                没有这一段的话，用户看到的是「解题中」+「这个对话还是空的」
                —— 两句话互相拆台，读起来就是"坏了"。
              */}
                {parentSession && (
                  <div
                    className="flex items-center gap-2 rounded-md border border-line/70 bg-surface-raised/20 px-3 py-2 text-xs text-ink-muted"
                    data-agent-breadcrumb
                  >
                    <Users size={12} strokeWidth={2} aria-hidden="true" className="text-ink-faint" />
                    <span className="min-w-0 truncate">
                      {t(
                        'chat.agentHub.childOf',
                        { title: parentSession.title },
                        `子代理 · 由「${parentSession.title}」派出`,
                      )}
                    </span>
                    <button
                      type="button"
                      onClick={() => setSearchParams({ session: parentSession.id }, { replace: true })}
                      className="ml-auto shrink-0 text-2xs text-ink-faint transition-ui duration-fast hover:text-ink"
                    >
                      {t('chat.agentHub.backToMain', undefined, '返回主线')}
                    </button>
                  </div>
                )}

                {active?.challengeId && active.challenge && (
                  <div className="rounded-md border border-line bg-surface px-3 py-2.5">
                    <ChallengeProcess
                      scanId={(liveChallenge?.scanId ?? active.challenge.scanId) || null}
                      running={challengeRunning}
                    />
                  </div>
                )}

                {messages.length === 0 && !(sendingHere && pendingUserContent !== null) && (
                  <>
                    <p className="py-4 text-center text-xs text-ink-faint">
                      {active?.challengeId
                        ? t(
                            'chat.a.emptyChallenge',
                            undefined,
                            '这道题由平台在沙箱里解，过程在上面实时更新。也可以直接在下面问它。',
                          )
                        : t(
                            'chat.a.emptyConversation',
                            undefined,
                            '这个对话还是空的。说点什么，或者回到会话列表挑另一个。',
                          )}
                    </p>
                    {/*
                    ★ 题目会话里不摆"可以这样问"那几条通用开场白：
                      它们是给"我该拿这个对话框干什么"的人看的，而从看板
                      点进来的人心里已经有一个具体问题（这道题怎样了）。
                      摆在那儿只会把刚出现的解题过程挤下去。
                  */}
                    {!active?.challengeId && (
                      <StarterPrompts
                        disabled={pickingStarter}
                        expanded={startersExpanded}
                        onToggle={() => setStartersExpanded((value) => !value)}
                        onPick={(prompt) => setDraft(prompt)}
                      />
                    )}
                  </>
                )}

                {/* 长会话只加载最后一页；更早的从服务端按游标翻，见 loadEarlierMessages */}
                {active?.hasEarlier && (
                  <div className="flex justify-center" data-load-earlier>
                    <Button
                      variant="ghost"
                      size="sm"
                      loading={loadingEarlier}
                      loadingLabel={t('chat.a.loading', undefined, '加载中…')}
                      onClick={() => void handleLoadEarlier()}
                    >
                      {t('chat.a.loadEarlier', undefined, '加载更早的消息')}
                    </Button>
                  </div>
                )}

                {(active?.compactions ?? []).some(
                  (c) => c.firstKeptSeq < (messages[0]?.seq ?? Number.POSITIVE_INFINITY),
                ) && (
                  <div
                    data-context-compaction
                    className="rounded-lg border border-border bg-surface-raised/40 px-3 py-2 text-xs text-muted"
                  >
                    {t(
                      'chat.a.compactedBanner',
                      undefined,
                      '上下文已压缩：更早的消息只以摘要供模型使用，原始记录仍保留。',
                    )}
                  </div>
                )}

                {turns.map((turn, index) => {
                  const isLive = index === liveTurnIndex
                  /*
                  ★ 人格标签只挂在**最新一回合**上。

                    数据契约里没有"这条消息是哪个人格说的"（人格存在
                    会话上，改一次就只记最新值）。把当前人格盖到每个历史
                    回合上，等于凭空断言一件没记录的事；只在最新一回合显示，
                    至少它是造出这一轮的那份配置（刚改人格、还没发新消息时
                    仍有误差，所以文案只说人格名，不说"这一轮用的"）。
                */
                  const turnMeta = !isLive && index === turns.length - 1 ? sessionMeta : null
                  const startSeq = turn.user?.seq ?? turn.items[0]?.seq ?? 0
                  const nextSeq = turns[index + 1]?.user?.seq ?? Number.POSITIVE_INFINITY
                  const boundary = active?.compactions?.find(
                    (c) => c.firstKeptSeq >= startSeq && c.firstKeptSeq < nextSeq,
                  )
                  return (
                    <section
                      key={turn.user?.id ?? turn.items[0]?.id ?? `turn-${index}`}
                      className="flex flex-col gap-2.5"
                      data-conversation-turn
                      data-turn-live={isLive || undefined}
                    >
                      {boundary && (
                        <div
                          data-context-compaction
                          className="rounded-lg border border-border bg-surface-raised/40 px-3 py-2 text-xs text-muted"
                        >
                          {t(
                            'chat.a.compactedDivider',
                            { seq: String(boundary.firstKeptSeq) },
                            `上下文压缩边界：从 #${boundary.firstKeptSeq} 起保留原文；更早消息只以摘要参与。`,
                          )}
                        </div>
                      )}
                      {turn.user && <MessageRow message={turn.user} />}
                      {(turn.items.length > 0 || isLive) && (
                        <AssistantTurn
                          items={turn.items}
                          live={isLive}
                          streamingText={isLive ? streamingText : null}
                          phaseText={isLive ? phaseLabel(phase, t) : null}
                          liveTool={isLive && phase?.phase === 'tool' ? (phase.tool ?? null) : null}
                          liveToolArgs={isLive && phase?.phase === 'tool' ? (phase.args ?? null) : null}
                          liveToolProgress={
                            isLive &&
                            phase?.phase === 'tool' &&
                            liveToolProgress?.tool === phase.tool
                              ? liveToolProgress
                              : null
                          }
                          liveToolOutput={
                            isLive &&
                            phase?.phase === 'tool' &&
                            liveToolOutput?.tool === phase.tool
                              ? (liveToolOutput?.text ?? null)
                              : null
                          }
                          progress={isLive ? { tools: turnToolCount(turn.items) } : null}
                          interjections={isLive ? interjections : []}
                          meta={turnMeta}
                          sessionId={active?.id}
                          deciding={deciding}
                          hideWork={answersOnly}
                          startedAt={turn.user?.createdAt ?? null}
                          onDecide={(messageId, approve) => void handleDecide(messageId, approve)}
                          onFork={(seq) => void handleFork(seq)}
                        />
                      )}
                    </section>
                  )
                })}

                {/*
                乐观气泡（v0.97）：用户刚发出去、服务端还没回的那句话。

                ★ 位置固定在"已有消息之后、正在想之前"——那正是它在时间上
                  的位置。画在别处（比如紧跟最后一条）都会让转圈出现在
                  用户消息上方，读起来像"模型先想了、我才问"。

                ★ 用 `role="status"` 的容器之外单独画，不用 MessageRow：
                  那条路要求一个完整的 `ChatMessage`（id/seq/createdAt），
                  伪造那些字段就会引出"真消息回来后按 id 替换失败、
                  界面上留一条消不掉的假气泡"这个失败模式。
              */}
                {sendingHere && pendingUserContent !== null && (
                  <div className="flex justify-end" data-pending-bubble="true">
                    <div className="bg-accent/12 max-w-[520px] rounded-lg rounded-br-xs px-3.5 py-2">
                      <p className="whitespace-pre-wrap break-words text-sm leading-[1.5] text-ink">
                        {pendingUserContent}
                      </p>
                    </div>
                  </div>
                )}

                {/*
                ★ v0.100：跑的过程中用户插的话在**回合内部**画
                （见 `AssistantTurn` 的 `interjections`）。

                不能在这里无条件渲染：`interjections` 是 store 级全局字段，
                跑在别的会话时它也会在，画出来就成了"这个对话里也有一句
                我没说过的话"。只有 `sendingHere` 时才属于本会话。
              */}

                {/*
                ★★ 正在跑、但还没有回合可归的那一段（用户消息还没落库，
                或者会话里一条消息都没有）。

                用的是同一个 `AssistantTurn` —— 否则"跑的时候"和
                "跑完之后"是两套排版，消息一落库整段内容会跳位。
                两者的差别只在 `live`：不画页脚、正文末尾带光标、
                阶段文案用 `role="status"` 持续播报。
              */}
                {sendingHere && liveTurnIndex < 0 && (
                  <AssistantTurn
                    items={[]}
                    live
                    streamingText={streamingText}
                    phaseText={phaseLabel(phase, t)}
                    progress={{ tools: liveLooseToolCount }}
                    interjections={interjections}
                    hideWork={answersOnly}
                  />
                )}
                {/*
                ★ v1.66：**已删除** v1.61 那条「服务端仍在后台推进这一轮：第 N 轮
                · 已用 M 次工具…」的底部蓝条。

                用户原话（m01705）：「可以不要显示…这个吗，CyberStrikeAI 刷新
                网页也不会有这个」。CyberStrikeAI 的做法是把「哪个会话在跑」标在
                **左侧会话列表**上（条目一个小转圈点 + 「运行中」），而不是在聊天
                区底部常驻一条带轮次/工具数的横幅。

                所以运行态的可见性改由两处承担：
                  ① 左侧每条 `SessionRow` 上的转圈点（服务端 `running` 字段，
                     v1.66，跨刷新/跨浏览器权威）；
                  ② 输入框 placeholder 与「中断」按钮（`turnRunningHere`）——
                     停在某个正在跑的会话里时，仍明确提示「这一轮还在跑…想停下用
                     中断」，但不再重复画那条带数字的蓝条。
              */}
              </div>
            )}

            {!isAtBottom && (
              <button
                type="button"
                onClick={() => scrollToLatest()}
                className={cn(
                  'absolute bottom-3 left-1/2 -translate-x-1/2 rounded-md border border-line bg-surface px-3 py-1',
                  'text-2xs text-ink-muted shadow-sm transition-ui duration-fast hover:text-ink',
                  'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
                )}
                data-jump-to-bottom
              >
                <ArrowDown size={11} strokeWidth={2} aria-hidden="true" className="mr-1 inline" />
                {t('chat.a.backToBottom', undefined, '回到底部')}
              </button>
            )}
          </div>
        </div>

        {/* 错误条：只占一行，不弹窗。它不阻止用户继续输入 */}
        {error && (
          <div
            className="shrink-0 border-t border-signal-risk/30 bg-signal-risk/5 px-3 py-1.5"
            data-error-code={error.code}
          >
            <div className="flex items-start gap-2">
              <p role="alert" className="flex-1 text-2xs text-signal-risk">
                {errorMessage(error)}
              </p>
              {/*
                ★ v1.22：重试入口直接放在错误条上。
                  用户看到"密钥无效"之后唯一能做的是去设置页改密钥，
                  然后回来点这里 —— 如果重试入口在别处（或根本没有），
                  他只能把问题重打一遍。服务端会把这一轮从断点接着跑
                  （`/turns/{id}/resume`），不会重复发用户消息。
              */}
              {retryableRunId && (
                <button
                  type="button"
                  data-error-retry={retryableRunId}
                  className="shrink-0 rounded border border-signal-risk/40 px-2 py-0.5 text-2xs text-signal-risk hover:bg-signal-risk/10 disabled:opacity-50"
                  disabled={resuming || sendingHere}
                  onClick={() => void handleRetryError()}
                >
                  {resuming
                    ? t('chat.a.retrying', undefined, '正在重试…')
                    : t('chat.a.retry', undefined, '重试')}
                </button>
              )}
            </div>
          </div>
        )}

        {/* 中断提示条（v1.19）：服务端说这一轮还能继续，就把"继续"交到用户手上 */}
        {stuckTurn && (
          <div
            className="shrink-0 border-t border-signal-warn/30 bg-signal-warn/5 px-3 py-1.5"
            data-stuck-turn={stuckTurn.id}
          >
            <p className="text-2xs text-signal-warn">
              {t('chat.a.stuckPrefix', undefined, '上一轮没有跑完')}
              {stuckTurn.lastError ? `（${stuckTurn.lastError}）` : ''}
              {t(
                'chat.a.stuckSuffix',
                undefined,
                '，可以接着做。接手会从断点恢复沙箱与上下文，首条输出可能需要一会儿。',
              )}
            </p>
            <button
              type="button"
              className="mt-1 rounded border border-signal-warn/40 px-2 py-0.5 text-2xs text-signal-warn hover:bg-signal-warn/10 disabled:opacity-50"
              disabled={resuming || sendingHere}
              title={
                sendingHere
                  ? t('chat.a.stuckRunningTitle', undefined, '这一轮还在跑，结束后才能接手')
                  : resuming
                    ? t('chat.a.stuckResumingTitle', undefined, '正在恢复上下文')
                    : t('chat.a.stuckResumeTitle', undefined, '从断点恢复沙箱与上下文')
              }
              onClick={() => void handleResume()}
            >
              {sendingHere
                ? t('chat.a.stuckRunning', undefined, '这一轮还在跑…')
                : resuming
                  ? t('chat.a.stuckResuming', undefined, '正在接手（恢复上下文）…')
                  : t('chat.a.stuckResume', undefined, '继续这一轮')}
            </button>
          </div>
        )}

        {/* 计划条（v1.89）：模型写进 chat_todos 的步骤，只读展示 */}
        {active && <ChatPlanStrip sessionId={active.id} refreshKey={messages.length} />}

        {/* 输入卡（单行形态）：附件、输入、模型、设置、发送同一行 */}
        <div className="shrink-0 px-3 pb-3 pt-4" data-testid="composer-dock">
          <div
            className="mx-auto w-full max-w-[880px] rounded-lg border border-line bg-surface shadow-[0_6px_22px_-16px_rgba(15,23,42,0.28)] transition-shadow duration-200 focus-within:border-line-strong focus-within:shadow-[0_10px_30px_-18px_rgba(15,23,42,0.34)]"
            data-testid="composer-card"
          >
            {/*
              ★ v1.75：设置默认收进齿轮，输入区只占一行。
              项目/人格/审批/技能/MCP 不是每句话都要改的东西，常驻会
              把输入框挤成三行（用户原话："太难用了"）。
            */}
            {active && settingsOpen && (
              <div className="border-b border-line/70 px-2 py-1.5" data-composer-settings>
                {settingsBar}
              </div>
            )}
            {mention && (
              <ToolMention
                tools={tools}
                query={mention}
                onPick={handlePickMention}
                onClose={() => setMention(null)}
              />
            )}
            <div className="flex items-end gap-1 p-1.5">
              {/*
              ★ v1.75：附件按钮收进输入行左侧（回形针图标）。
              旧形态是输入框上方一条「附件」横栏 —— 与设置条一起把
              输入区撑成三行。有附件时芯片仍在行内换行显示。
            */}
              {active && (
                <AttachmentBar
                  inline
                  attachments={stagedAttachments}
                  uploading={uploading}
                  onPick={(file) => void handleUpload(file)}
                  onRemove={dropStagedAttachment}
                  // ★ v1.65：只在本会话有轮次在跑时才禁用附件（跑着不能改附件）。
                  //   别的会话在跑不影响这里。
                  disabled={sendingHere}
                />
              )}
              <textarea
                ref={composerRef}
                value={draft}
                onChange={(e) => {
                  setDraft(e.target.value)
                  setMention(parseMentionQuery(e.target.value, e.target.selectionStart ?? 0))
                }}
                // 光标移动也要重算：用户可能把光标移到前面一个 `@` 后面继续敲。
                // 只在 keyup/click 上做，不做 selectionchange 轮询 —— 后者
                // 对每个字符都触发一次，而这里只需要"用户主动挪了光标"。
                onKeyUp={(e) => {
                  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                    const el = e.currentTarget
                    setMention(parseMentionQuery(el.value, el.selectionStart ?? 0))
                  }
                }}
                onClick={(e) => {
                  const el = e.currentTarget
                  setMention(parseMentionQuery(el.value, el.selectionStart ?? 0))
                }}
                onPaste={(e) => {
                  const pasted = Array.from(e.clipboardData.files)
                  if (pasted.length === 0 || sendingHere) return
                  // 粘贴图片/文件：不再把文件名当文本插进输入框。
                  e.preventDefault()
                  void handleUpload(pasted[0])
                }}
                onBlur={() => setMention(null)}
                onKeyDown={(e) => {
                  /*
                  Enter 发送、Shift+Enter 换行。
                  ⚠️ 必须同时排除输入法组合态（`e.nativeEvent.isComposing`）：
                    中文用户敲拼音时按 Enter 是"上屏候选词"，不是"发送"。
                    不排除的话，每打一个词就会把半截拼音发出去 —— 这是中文
                    界面上最常见也最恼人的一类 bug。
                */
                  /*
                  Esc 先关补全层。不给这条的话，用户想放弃补全只能
                  按方向键把光标挪走，或者把 `@` 删掉。
                */
                  if (e.key === 'Escape' && mention) {
                    e.preventDefault()
                    setMention(null)
                    return
                  }
                  if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault()
                    void handleSend()
                  }
                }}
                rows={1}
                spellCheck={false}
                autoCorrect="off"
                autoCapitalize="off"
                autoComplete="off"
                placeholder={
                  !active
                    ? t('chat.ph.noConversation')
                    : active.archived
                      ? t('chat.ph.archived')
                      : sendingHere
                        ? t('chat.ph.sending')
                        : serverRunning
                          ? stopRequested
                            ? t('chat.ph.stopRequested')
                            : serverTurn
                              ? runningProgressText(
                                  t,
                                  (serverTurn.roundIndex ?? 0) + 1,
                                  serverTurn.toolsUsed ?? 0,
                                )
                              : t('chat.ph.serverRunning')
                          : sendingElsewhere
                            ? // v1.65：别的会话在跑**不再阻止**发消息（后端支持多会话
                              //   并发）。这里只做一句轻提示，Enter 照常发送。
                              t('chat.ph.elsewhere')
                            : t('chat.ph.idle')
                }
                /*
                ★★ v0.100：跑的时候**不再禁用**输入框。

                改造前这里是 `disabled={!active || sending}` —— 而用户的原话
                正是「正在想的时候不能插话」。禁用的理由当时是
                "同一时刻只发一条"（`sending` 是全局布尔，两条并发会让
                seq 乱序）。但**插话不是发第二条**：它不启新一轮，
                只是往库里写一条 user 消息，由正在跑的那一轮在下一轮
                开头读到（见后端 `interject` 端点的 docstring）。

                所以这里放开输入，但**发送键的语义变了**（见 `canSend` 与
                `handleSend`）：跑的时候 Enter 是"插话"，不是"发消息"。
                文案必须跟着变，否则用户会以为自己在开第二轮。
              */
                disabled={!active}
                aria-label={t('chat.a11y.input', undefined, '输入消息')}
                className={cn(
                  'max-h-40 min-h-9 min-w-0 flex-1 resize-none overflow-y-auto rounded-md bg-transparent px-2 py-2 field-sizing-content',
                  'text-base text-ink placeholder:text-ink-faint',
                  'focus:outline-none',
                  'disabled:cursor-not-allowed disabled:opacity-60',
                )}
              />
              {active && (
                <div className="hidden shrink-0 sm:block">
                  <ProviderSelect
                    align="right"
                    quiet
                    providers={usableProviders}
                    value={active.providerId ?? null}
                    onChange={(providerId) => void updateSettings(active.id, { providerId })}
                  />
                </div>
              )}
              {active && (
                <button
                  type="button"
                  onClick={() => setSettingsOpen((value) => !value)}
                  aria-expanded={settingsOpen}
                  aria-label={t('chat.a11y.settings', undefined, '对话设置')}
                  title={t('chat.a11y.settings', undefined, '对话设置')}
                  className={cn(
                    'flex h-9 w-9 shrink-0 items-center justify-center rounded-sm',
                    'max-sm:min-h-10 max-sm:min-w-10',
                    'text-ink-faint transition-ui duration-fast hover:bg-ink-ghost/10 hover:text-ink',
                    settingsOpen && 'bg-accent/10 text-accent',
                    'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
                  )}
                >
                  <Settings2 size={14} strokeWidth={2} aria-hidden="true" />
                </button>
              )}
              {/*
              ★ v1.25：「中断」入口。只在**当前会话**正在跑时出现 ——
                跑在别的会话里时这里根本没有可中断的轮次（见 `sendingElsewhere`）。
              ★ v1.49：`turnRunningHere` 也包含服务端说还在跑（本地 done 帧
                丢了）的情况 —— 那种时候用户更需要「中断」这个出口。
            */}
              {turnRunningHere && (
                <Button
                  onClick={() => {
                    // ★ v1.68：每次打开清掉上一次回执，避免看到过时/别的会话的反馈。
                    clearLastInterrupt()
                    setInterruptOpen(true)
                  }}
                  variant="ghost"
                  size="sm"
                  disabled={interrupting}
                  aria-label={t('chat.a11y.interrupt', undefined, '中断')}
                  data-interrupt-open
                >
                  {t('chat.a.interrupt', undefined, '中断')}
                </Button>
              )}
              <Button
                onClick={() => void handleSend()}
                disabled={!canSend}
                /*
                ★ 跑着的时候这个按钮是「插话」，不该显示转圈：
                转圈意味着"这个按钮正在忙、点不动"，而它此刻恰恰**能**点。
                转圈只该出现在真正提交出去的那一下（`interjecting`）。
              */
                loading={interjecting}
                size="sm"
                className="h-9 shrink-0 max-sm:min-h-10"
                aria-label={
                  turnRunningHere
                    ? t('chat.a.interject', undefined, '插话')
                    : t('chat.a.send', undefined, '发送')
                }
              >
                <Send size={14} strokeWidth={2} aria-hidden="true" />
                {turnRunningHere
                  ? t('chat.a.interject', undefined, '插话')
                  : t('chat.a.send', undefined, '发送')}
              </Button>
            </div>
          </div>
          {(contextUsage || user?.role !== 'admin') && (
            <div className="mx-auto mt-1 flex w-full max-w-[880px] items-center justify-between gap-2 text-2xs text-ink-faint">
              {contextUsage ? (
                <span
                  data-context-usage
                  title={`${
                    contextUsage.source === 'provider'
                      ? `${formatTokenCount(contextUsage.usedTokens)} / ${formatTokenCount(contextUsage.windowTokens)} tokens（最近一次调用上报）`
                      : `${formatTokenCount(contextUsage.usedTokens)} / ${formatTokenCount(contextUsage.windowTokens)} tokens（字符估算，含系统提示词与工具 schema）`
                  }${contextUsage.compactionCount > 0 ? ` · 已压缩 ${contextUsage.compactionCount} 次` : ''}`}
                >
                  {t(
                    'chat.a.contextUsage',
                    { percent: String(contextUsage.percent) },
                    `上下文 ${contextUsage.percent}%`,
                  )}
                </span>
              ) : (
                <span />
              )}
              {user?.role !== 'admin' && (
                <span className="text-right">
                  {t(
                    'chat.a.readonlyNotice',
                    undefined,
                    '你是只读角色：可以查询和看结果，但不能让它建扫描或改技能库。',
                  )}
                </span>
              )}
            </div>
          )}
        </div>
      </div>

      <PersonaDialog
        open={personaOpen}
        personas={personas}
        current={active?.persona ?? null}
        onPick={(personaId) => void handlePickPersona(personaId)}
        onClose={() => setPersonaOpen(false)}
      />

      {/* ── v1.15：会话右键菜单 ─────────────────────────────── */}
      <SessionContextMenu
        anchor={menuAnchor}
        archived={Boolean(menuSession?.archived)}
        pinned={Boolean(menuSession?.pinned)}
        projects={projectSuggestions}
        currentProject={menuSession?.project ?? null}
        onRename={() => menuSession && openRename({ id: menuSession.id, title: menuSession.title })}
        onFork={() => {
          if (!menuSession) return
          void forkSession(menuSession.id).then((created) => {
            if (!created) return
            setSearchParams({ session: created.id }, { replace: true })
            setShowList(false)
            showToast({ message: t('chat.a.forked', undefined, '已创建分支。'), tone: 'ok' })
          })
        }}
        onViewAttackChain={() => menuSession && void openAttackChain(menuSession)}
        onViewFindings={() => menuSession && openFindings(menuSession)}
        onTogglePin={() => {
          if (menuSession) void pinSession(menuSession.id, !menuSession.pinned)
        }}
        onDownload={(detail) => {
          if (menuSession) void handleDownload(menuSession.id, detail)
        }}
        onCompact={() => {
          if (!menuSession) return
          void compactSession(menuSession.id).then((result) => {
            if (!result) return
            showToast({
              message: t(
                'chat.a.compacted',
                { before: String(result.tokensBefore), after: String(result.tokensAfter) },
                `已压缩上下文：约 ${result.tokensBefore} → ${result.tokensAfter} tokens。`,
              ),
              tone: 'ok',
            })
          })
        }}
        onMoveTo={(project) => {
          if (menuSession) void handleMoveTo(menuSession.id, project)
        }}
        onMoveToNew={() => menuSession && setNewGroupFor([menuSession.id])}
        onBatchManage={() => setBatchOpen(true)}
        onArchive={() =>
          menuSession &&
          setConfirmAction({ kind: 'archive', id: menuSession.id, title: menuSession.title })
        }
        onUnarchive={() => {
          if (menuSession) void unarchiveSession(menuSession.id)
        }}
        onPurge={() =>
          menuSession &&
          setConfirmAction({ kind: 'purge', id: menuSession.id, title: menuSession.title })
        }
        onClose={() => setMenuAnchor(null)}
      />

      {/* ── 攻击链 / 关联漏洞（只读洞察） ───────────────────── */}
      <SessionAttackChainModal
        open={chainView.open}
        onClose={() => setChainView((view) => ({ ...view, open: false }))}
        sessionTitle={chainView.title}
        messages={chainView.messages}
        loading={chainView.loading}
        error={chainView.error}
      />
      <SessionFindingsModal
        open={findingsView.open}
        onClose={() => setFindingsView((view) => ({ ...view, open: false }))}
        sessionTitle={findingsView.title}
        scanIds={findingsView.scanIds}
        items={findingsView.items}
        loading={findingsView.loading}
        error={findingsView.error}
        onRetry={() => void loadFindings(findingsView.sessionId, findingsView.title)}
      />

      {/* ── 重命名对话框 ───────────────────────────────────── */}
      {/* ── 中断当前步骤（v1.25）────────────────────────────── */}
      <Modal
        open={interruptOpen}
        onClose={() => setInterruptOpen(false)}
        title={t('chat.a11y.interruptHint', undefined, '中断当前步骤')}
        widthClass="max-w-xl"
        footer={
          <div className="flex flex-wrap justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => setInterruptOpen(false)}>
              {t('chat.a.close', undefined, '关闭')}
            </Button>
            <Button
              variant="danger"
              size="sm"
              disabled={interrupting}
              data-interrupt-stop
              onClick={() => void submitInterrupt('stop')}
            >
              {t('chat.a.stopAll', undefined, '彻底停止')}
            </Button>
            <Button
              variant="primary"
              size="sm"
              disabled={interrupting}
              data-interrupt-continue
              onClick={() => void submitInterrupt('continue')}
            >
              {t('chat.a.interruptContinueBtn', undefined, '中断并继续')}
            </Button>
          </div>
        }
      >
        <div className="flex flex-col gap-3 text-xs leading-relaxed text-ink-muted">
          <p>
            <span className="font-medium text-ink"><T k="chatmsg.interruptContinue"/></span>
            {t(
              'chat.a.interruptContinueDesc',
              undefined,
              '：他上一条输出会立刻停下，你写的说明会并入上下文，然后他会接着做。中途改方向用这个。',
            )}
          </p>
          <p>
            <span className="font-medium text-ink"><T k="chatmsg.interruptStop"/></span>
            {t('chat.a.interruptStopDesc', undefined, '：这一轮就地结束，后台不会再自动接着跑。')}
          </p>
          <p className="text-2xs text-ink-faint">
            {t(
              'chat.a.interruptProcessNote',
              undefined,
              '注意：如果当前正在执行一个工具命令，它要跑完才会停 —— 我们没法中断一个已经在跑的子进程。',
            )}
          </p>
          <label className="flex flex-col gap-1">
            <span className="text-2xs text-ink-faint"><T k="chatmsg.interruptNote"/></span>
            <textarea
              value={interruptNote}
              onChange={(event) => setInterruptNote(event.target.value)}
              rows={4}
              aria-label={t('chat.a11y.interruptNote', undefined, '中断说明')}
              placeholder={t('chat.a11y.interruptNotePlaceholder', undefined, '例如：别扫端口了，先去看 /api/login 的鉴权逻辑')}
              className="w-full resize-y rounded-sm border border-line bg-surface px-2.5 py-2 text-sm text-ink placeholder:text-ink-faint focus:outline-none focus:ring-2 focus:ring-accent/40"
            />
          </label>
          {/* ★ v1.68：明确回执 —— 用户点完按钮就能看到"到底点没点上"，
              而不是"点了没反应"。409「当前没有在跑的回复」等失败也原样显示。 */}
          {interrupting && (
            <p className="text-2xs text-ink-faint" data-interrupt-feedback="pending">
              {t('chat.a.interruptSending', undefined, '正在发送中断请求…')}
            </p>
          )}
          {!interrupting && lastInterrupt && (
            <p
              className={lastInterrupt.ok ? 'text-2xs text-accent' : 'text-2xs text-signal-risk'}
              data-interrupt-feedback={lastInterrupt.ok ? 'ok' : 'err'}
              role="status"
            >
              {lastInterrupt.message}
            </p>
          )}
        </div>
      </Modal>

      <Modal
        open={renameTarget !== null}
        onClose={() => setRenameTarget(null)}
        title={t('chat.a11y.rename', undefined, '重命名对话')}
        widthClass="max-w-md"
        footer={
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => setRenameTarget(null)}>
              {t('chat.a.cancel', undefined, '取消')}
            </Button>
            <Button
              variant="primary"
              size="sm"
              disabled={renaming}
              onClick={() => void submitRename()}
            >
              {t('chat.a.save', undefined, '保存')}
            </Button>
          </div>
        }
      >
        <input
          value={renameDraft}
          onChange={(event) => setRenameDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') void submitRename()
          }}
          aria-label={t('chat.a11y.title', undefined, '对话标题')}
          className="h-9 w-full rounded-sm border border-line bg-surface px-2 text-sm text-ink"
          // 打开就把光标放进输入框、并全选 —— 重命名十次有九次要整条换掉
          autoFocus
          onFocus={(event) => event.currentTarget.select()}
        />
      </Modal>

      {/* ── 批量管理 ───────────────────────────────────────── */}
      <SessionBatchModal
        open={batchOpen}
        sessions={sessions}
        projects={projectSuggestions}
        busy={batchBusy}
        onClose={() => setBatchOpen(false)}
        onMoveTo={handleBatchMoveTo}
        onCreateGroup={handleCreateGroup}
        onArchive={handleBatchArchive}
      />

      {/* ── v1.20：新建分组（右键「移动到分组 → 新建分组…」与批量管理共用） ── */}
      <GroupNameDialog
        open={newGroupFor !== null}
        existing={projectSuggestions}
        busy={batchBusy}
        onConfirm={(name) => void handleCreateGroup(newGroupFor ?? [], name)}
        onCancel={() => setNewGroupFor(null)}
      />

      {/* ── 危险操作二次确认 ───────────────────────────────── */}
      {/*
        ★ 归档也要确认，这一点**偏离**了 `ConfirmDialog` 注释里那张表
          （"归档可撤销 → 不要确认"）。原因是 v0.106/v0.108 之后归档会
          **当场销毁沙箱与附件**：`/work` 里的产物、上传的原始文件都回不来，
          所以它实际上不可撤销。既然不可撤销，就该先问一句 ——
          让用户以为能后悔，比多一次点击糟得多。
      */}
      <ConfirmDialog
        open={confirmAction !== null}
        title={
          confirmAction?.kind === 'purgeAll'
            ? t('chat.a.confirmPurgeAllTitle', { n: confirmAction.n }, '清空已归档对话？')
            : confirmAction?.kind === 'purge'
              ? t('chat.a.confirmPurgeTitle', undefined, '彻底删除这个对话？')
              : t('chat.a.confirmDeleteTitle', undefined, '删除这个对话？')
        }
        description={
          confirmAction?.kind === 'purgeAll'
            ? t(
                'chat.a.confirmPurgeAllBody',
                { n: confirmAction.n },
                `已归档的 ${confirmAction.n} 个对话及其全部消息将被**永久删除**，无法恢复。`,
              )
            : confirmAction?.kind === 'purge'
              ? t(
                  'chat.a.confirmPurgeBody',
                  { title: confirmAction.title },
                  `「${confirmAction.title}」及其全部消息将被**永久删除**，无法恢复。`,
                )
            : t(
                'chat.a.confirmArchiveBody',
                { title: confirmAction?.title ?? '' },
                `「${confirmAction?.title ?? ''}」会移入「已归档」，对话里的沙箱文件与上传的附件会被立即清理。`,
              )
        }
        confirmLabel={
          confirmAction?.kind === 'purgeAll'
            ? t('chat.a.purgeArchived', undefined, '清空归档')
            : confirmAction?.kind === 'purge'
              ? t('chat.a.purge', undefined, '永久删除')
              : t('chat.a.delete', undefined, '删除')
        }
        onCancel={() => setConfirmAction(null)}
        onConfirm={() => {
          const action = confirmAction
          setConfirmAction(null)
          if (!action) return
          if (action.kind === 'purgeAll') {
            void purgeArchivedSessions().then((deleted) => {
              if (deleted !== null) {
                showToast({
                  message: t(
                    'chat.a.purgedAll',
                    { n: deleted },
                    `已删除 ${deleted} 个已归档对话`,
                  ),
                  tone: 'ok',
                })
              }
            })
          } else if (action.kind === 'purge') {
            void purgeSession(action.id)
          } else {
            void handleArchive(action.id)
          }
        }}
      />
      <AgentHub
        open={agentHubOpen}
        onClose={() => setAgentHubOpen(false)}
        parent={parentSession}
        children={hubChildren}
        runningIds={sendingSessionIds}
        onOpen={(session) => {
          setSearchParams({ session: session.id }, { replace: true })
          setShowList(false)
          setAgentHubOpen(false)
        }}
        onGuide={(id, text) => interject(id, text)}
        onStop={(id) => {
          void interruptTurn(id, 'stop')
        }}
      />
    </div>
  )
}

/**
 * 侧栏里的一行会话。
 *
 * 抽出来是因为分组之后它出现两次（命名项目下 / 无项目下），
 * 复制两份必然漂 —— 一处加了悬停样式另一处忘了。
 */
function SessionRow({
  session,
  active,
  running,
  depth = 0,
  onOpen,
  onArchive,
  onContextMenu,
  archived = false,
  hasChildren = false,
  collapsed = false,
  onToggleChildren,
}: {
  session: ChatSession
  active: boolean
  /** 服务端 running，或本浏览器刚发起、服务端列表还没刷新。 */
  running: boolean
  /** 子代理分支缩进层级。 */
  depth?: number
  onOpen: () => void
  onArchive: () => void
  /** 右键（以及"⋯"按钮）打开上下文菜单。 */
  onContextMenu: (anchor: { x: number; y: number }) => void
  /** 已归档的行：不显示归档按钮，改由菜单里的「恢复/彻底删除」处理。 */
  archived?: boolean
  /** 有子代理时显示折叠箭头。 */
  hasChildren?: boolean
  collapsed?: boolean
  onToggleChildren?: () => void
}) {
  const { t } = useI18n()
  /*
    ★ 菜单按钮给出锚点坐标时，用**按钮自己**的位置而不是 (0,0)：
      键盘用户（Tab 到"⋯"再回车）没有鼠标坐标，用 (0,0) 会让菜单
      出现在窗口左上角，离他按的地方几百像素远。
  */
  function openMenuFromButton(element: HTMLElement) {
    const rect = element.getBoundingClientRect()
    onContextMenu({ x: rect.left, y: rect.bottom })
  }

  return (
    <li>
      <div
        className={cn(
          'group flex items-center gap-1 rounded-sm px-2 py-1.5',
          'transition-ui duration-fast',
          depth > 0 && 'ml-3 border-l border-line/60 pl-3',
          active ? 'bg-surface-raised' : 'hover:bg-surface-raised/60',
        )}
        // 整行都可以右键 —— 目标越大会越好点，而"必须精确点到标题"很烦
        onContextMenu={(event) => {
          event.preventDefault()
          onContextMenu({ x: event.clientX, y: event.clientY })
        }}
      >
        {hasChildren && (
          <button
            type="button"
            onClick={onToggleChildren}
            aria-expanded={!collapsed}
            aria-label={
              collapsed
                ? t('chat.a.expandChildren', undefined, '展开子代理')
                : t('chat.a.collapseChildren', undefined, '收起子代理')
            }
            title={
              collapsed
                ? t('chat.a.expandChildren', undefined, '展开子代理')
                : t('chat.a.collapseChildren', undefined, '收起子代理')
            }
            className="shrink-0 rounded-xs p-0.5 text-ink-faint transition-ui duration-fast hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
          >
            {collapsed ? (
              <ChevronRight size={11} strokeWidth={2} aria-hidden="true" />
            ) : (
              <ChevronDown size={11} strokeWidth={2} aria-hidden="true" />
            )}
          </button>
        )}
        <button
          type="button"
          onClick={onOpen}
          className="min-w-0 flex-1 text-left"
          aria-current={active ? 'page' : undefined}
        >
          <span className="flex items-center gap-1">
            {running && (
              // ★ v1.66：正在跑的会话在列表里画一个转圈点 + 「运行中」。
              //   这样一眼能看出哪个会话在跑（服务端事实，跨刷新权威），
              //   取代了聊天区底部那条常驻蓝条（用户不要它）。
              <span
                className="inline-block h-2.5 w-2.5 shrink-0 animate-spin rounded-full border-[1.5px] border-accent/30 border-t-accent"
                title={t('chat.a11y.running', undefined, '运行中')}
                aria-label={t('chat.a11y.running', undefined, '运行中')}
                data-session-running="true"
              />
            )}
            {session.pinned && (
              // 📌 用 Unicode 而不是 SVG：它是**状态标记**，不需要跟主题变色，
              // 而一个 12px 的图标会跟标题抢注意力。与 CyberStrikeAI 一致。
              <span className="shrink-0 text-2xs" title={t('chat.a11y.pinned', undefined, '已置顶')} aria-label={t('chat.a11y.pinned', undefined, '已置顶')}>
                📌
              </span>
            )}
            {/* 空标题没有兜底时，列表里会出现只有「0 条」没有名字的一行 ——
                看起来像渲染坏了。未命名也要能被指认。 */}
            <span className="truncate text-xs text-ink">
              {session.title.trim() || t('chat.untitled')}
            </span>
          </span>
          <span className="block text-2xs text-ink-faint">
            {running ? `${t('chat.running')} · ` : ''}
            {/* 「条」单独出现不知道数的是什么；这一列数的是消息（含工具调用记录） */}
            {t('chat.messageCount', { n: session.messageCount })}
          </span>
        </button>
        {/*
          ★ 归档按钮只在悬停/聚焦时出现，但要**始终可聚焦**：
            用 `opacity-0 group-hover:opacity-100` 而不是
            `hidden group-hover:block` —— 后者让键盘用户永远
            拿不到它（display:none 的元素不可聚焦）。
        */}
        {!archived && (
          <button
            type="button"
            onClick={onArchive}
            className={cn(
              'rounded-xs p-1 text-ink-faint opacity-0 transition-ui duration-fast',
              'hover:text-signal-risk focus:opacity-100 group-hover:opacity-100',
              'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
            )}
            aria-label={t(
              'chat.a.archiveAria',
              { title: session.title.trim() || t('chat.a.unnamedChat', undefined, '未命名对话') },
              `归档对话「${session.title.trim() || '未命名对话'}」`,
            )}
            title={t('chat.a11y.archive', undefined, '归档这个对话')}
          >
            <Archive size={12} strokeWidth={2} aria-hidden="true" />
          </button>
        )}
        {/*
          "⋯" 按钮是右键菜单的**可发现入口**：右键本身不可发现 ——
          用户不试就不会知道有这些功能（这正是"你忘记做了"的来源）。
        */}
        <button
          type="button"
          onClick={(event) => openMenuFromButton(event.currentTarget)}
          className={cn(
            'rounded-xs p-1 text-ink-faint opacity-0 transition-ui duration-fast',
            'hover:text-ink focus:opacity-100 group-hover:opacity-100',
            'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
          )}
          aria-label={t(
            'chat.a.menuAria',
            { title: session.title.trim() || t('chat.a.unnamedChat', undefined, '未命名对话') },
            `打开对话「${session.title.trim() || '未命名对话'}」的菜单`,
          )}
          aria-haspopup="menu"
          title={t('chat.a11y.more', undefined, '更多操作')}
        >
          <MoreHorizontal size={12} strokeWidth={2} aria-hidden="true" />
        </button>
      </div>
    </li>
  )
}

export default ChatPage
