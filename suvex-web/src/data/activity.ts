/**
 * Agent Activity 的领域模型 —— 全站事件的单一事实来源。
 *
 * 设计要点：**事件是一个 discriminated union，不是一个「消息 + 可选字段」的包。**
 *
 * 为什么：不同类型的载荷字段完全不同（工具调用有命令与退出码，推理只有一段文字，
 * PoC 有代码与语言），把它们塞进一个扁平结构会得到一堆可选字段，
 * 然后在渲染处写 `if (event.command)` 这种判断。用 union 之后，
 * TypeScript 会在 switch 里精确收窄，加一个新事件类型时所有需要处理它的地方
 * 都会编译报错——这是可维护性上最实在的收益。
 *
 * Overview 页消费的是同一批事件的「摘要视图」（summary + type + at），
 * 所以不需要两套模型。
 */

// ─────────────────────────────────────────────
// 智能体：这是「一队智能体」，不是单一进程
// ─────────────────────────────────────────────

/**
 * 智能体在扫描流程里承担的角色。Suvex 是多 agent 协作，角色决定它做什么。
 *
 * `unknown` 是**如实表达"不知道"**，不是占位符（任务 11 新增）：
 * 角色的唯一来源是 `GET /scans/{id}/agents`，而事件载荷里没有这个字段 ——
 * 所以在「全部活动」（全局流）下，从事件流派生出来的 agent **确实**无从得知
 * 角色。标成"未分类"，而不是猜一个：猜错会让人以为某个 agent 真的在做
 * 漏洞利用，而那是会改变处置优先级的判断。
 */
export type AgentRole = 'recon' | 'scanner' | 'exploiter' | 'verifier' | 'reporter' | 'unknown'

export type AgentStatus = 'running' | 'idle' | 'blocked' | 'done' | 'failed'

export interface Agent {
  /** 标识符，如 "agent-03"。不翻译 */
  id: string
  role: AgentRole
  status: AgentStatus
  /** 这个 agent 当前在负责的目标，如 "api.acme.internal" */
  target: string
  /** 所属扫描。多个 agent 同属一次扫描时它们是一队 */
  scanId: string
  /** 累计完成的动作数 —— 左侧列表用它体现「干了多少活」 */
  eventCount: number
  /** 最近一次动作的时间（ISO），用于排序 */
  lastActiveAt: string
}

export const AGENT_ROLE_LABEL: Record<AgentRole, string> = {
  recon: '信息收集',
  scanner: '漏洞扫描',
  exploiter: '漏洞利用',
  verifier: '结果验证',
  reporter: '报告整理',
  // 全局流下角色无从得知（见 `AgentRole` 的说明），如实标注
  unknown: '未分类',
}

export const AGENT_STATUS_LABEL: Record<AgentStatus, string> = {
  running: '运行中',
  idle: '空闲',
  blocked: '被阻塞',
  done: '已完成',
  failed: '失败',
}

// ─────────────────────────────────────────────
// 事件类型
// ─────────────────────────────────────────────

/**
 * 事件类型。对应 brief 要求的四类标签（工具调用/推理/发现/告警）再细分：
 *   - tool_call      工具调用
 *   - reasoning      推理
 *   - discovery      发现
 *   - poc_generated  生成 PoC（发现之后、验证之前的关键一步）
 *   - verified       已验证
 *   - blocked        告警（被沙箱/策略拦下）
 *   - failed         失败
 *   - completed      完成
 */
export type ActivityEventType =
  | 'tool_call'
  | 'reasoning'
  | 'discovery'
  | 'poc_generated'
  | 'verified'
  | 'blocked'
  | 'failed'
  | 'completed'

/** 严重度只用在 discovery/verified 上，其余事件没有严重度概念 */
type WithSeverity = { severity?: 'critical' | 'high' | 'medium' | 'low' | 'info' }

/** 各类事件的载荷。这是 union 的判别核心 */
export type EventDetail =
  | {
      kind: 'tool_call'
      /** 工具名，如 nmap / sqlmap / nuclei。标识符，不翻译 */
      tool: string
      /** 实际执行的命令。等宽显示，不换行折行破坏可读性 */
      command: string
      exitCode: number | null
      /**
       * 单次调用的耗时（毫秒）。
       *
       * ⚠️ **恒为 `null`**（契约 §4.2）：外部工具只给 `result.success` 布尔
       *    与调用区间，没有单次调用的退出码与耗时；编一个 `0` 会让人以为
       *    那是真的进程退出码。
       *
       *    ★ 这里原来声明的是 `number`（非空）—— 类型与真实数据不符。
       *      接真实 SSE 之前看不出来（mock 事件都带着数字），
       *      接上之后活动流里每一条工具调用都渲染成 **"nullms"**：
       *      因为 `null < 1000` 在 JS 里是 `true`。
       *      收窄成 `number | null` 之后，TypeScript 会强制每个消费方处理它。
       */
      durationMs: number | null
      /** 原始输出尾部若干行。日志原样，不翻译 */
      output?: string
    }
  | {
      kind: 'reasoning'
      /** 推理片段。多行文本，允许换行 */
      text: string
      /** 这一步推理在做什么判断，用于折叠时显示 */
      intent: string
    }
  | ({
      kind: 'discovery'
      /** 涉及的端点/参数 */
      subject: string | null
      /** 支撑这个发现的证据，来自真实响应 */
      evidence: string
      /** 置信度：低置信的发现需要人复核 */
      confidence: 'low' | 'medium' | 'high'
    } & WithSeverity)
  | {
      kind: 'poc_generated'
      /** 语言标识，如 python / bash / http —— 决定代码块的高亮（暂不做，先显示） */
      language: string
      /** 验证脚本源码。原样保留，绝不翻译 */
      code: string
      target: string
    }
  | {
      kind: 'verified'
      /** 复现方式的人话描述 */
      method: string
      /** 复现证据 */
      evidence: string
    }
  | {
      kind: 'blocked'
      /** 被拦下的动作 */
      attempted: string
      /** 触发的策略名 */
      policy: string
      /** 为什么拦 —— 这句话要能让用户判断是否该放行 */
      reason: string
    }
  | {
      kind: 'failed'
      attempted: string
      /** 失败原因。保持原始错误信息，不翻译 */
      error: string
    }
  | {
      kind: 'completed'
      /** 这一阶段的结果摘要 */
      result: string
    }

export interface ActivityEvent {
  /** 事件主键。标识符 */
  id: string
  scanId: string
  agentId: string
  type: ActivityEventType
  /**
   * 一行摘要。**可以是任意事件的折叠态**，所以它必须能独立读懂 ——
   * 用户扫视时不会点开每一条。
   */
  summary: string
  /** 结构化载荷。可能为 null（例如纯状态事件） */
  detail: EventDetail | null
  /** ISO 8601 */
  at: string
}

// ─────────────────────────────────────────────
// 事件源（传输层抽象）
// ─────────────────────────────────────────────

/**
 * 事件源的连接状态。
 *
 * ⚠️ 它原来在 `activity-mock.ts` 与 `useActivityStream.ts` 里
 * **各定义了一遍** —— 两份联合类型迟早会漂移，而且 TypeScript
 * **不会报错**（它们是两个恰好取值相同的类型，不是同一个）。
 * 现在收敛到这里，两处都从这里导入。
 */
export type StreamStatus = 'connecting' | 'live' | 'paused' | 'disconnected'

/**
 * 事件源的接口。
 *
 * 这个抽象是刻意的（契约 §7）：`ActivityPage` 与 `useActivityStream`
 * 只认这个接口，所以"换成真实 SSE"是一次**实现替换**，上层一行不改 ——
 * 任务 7 兑现了这一点：真实实现进来时，两个消费方确实一行没动。
 */
export interface EventSourceHandle {
  /** 开始产生事件 */
  start: () => void
  /** 停止（组件卸载时调用） */
  stop: () => void
  /** 订阅事件；返回取消订阅函数 */
  subscribe: (listener: (event: ActivityEvent) => void) => () => void
  /**
   * 订阅**连接状态**；返回取消订阅函数。
   *
   * ★ **注册时会立即用当前状态回调一次** —— 与 `useSyncExternalStore`
   *   的语义一致。这样调用方订阅完就知道当前状态，不必另外再读一次，
   *   也就不会漏掉"订阅之后、第一次变化之前"那一段。
   *
   * ★ 它**曾经是可选的**，理由是"mock 源不实现它，调用方退回 mount 时读一次"。
   *   mock 源（`activity-mock.ts`）已经删除，而真实 SSE 一定需要它：
   *   连接状态会自己变（`onopen` → live、`onerror` → disconnected），
   *   不订阅的话界面会**永远停在"连接中"** —— 用户根本看不出到底连上没有。
   *
   *   可选性去掉之后，类型系统保证任何新的事件源都得上报状态变化；
   *   而那条"源不支持订阅时读一次"的兜底路径（连同它依赖的 `getStatus`）
   *   一并删了 —— 它已经是死代码，留着只会让人以为还存在第二种事件源。
   */
  subscribeStatus: (listener: (status: StreamStatus) => void) => () => void
}

// ─────────────────────────────────────────────
// 类型标签与视觉映射（展示层共用）
// ─────────────────────────────────────────────

export const EVENT_TYPE_LABEL: Record<ActivityEventType, string> = {
  tool_call: '工具调用',
  reasoning: '推理',
  discovery: '发现',
  poc_generated: '生成 PoC',
  verified: '已验证',
  blocked: '告警',
  failed: '失败',
  completed: '完成',
}

/**
 * 事件类型的色调。
 *
 * 克制原则：八种事件只给三种颜色 ——
 *   告警/失败 → risk（需要立刻注意）
 *   发现/生成 PoC/已验证 → accent 或 ok（有价值产出）
 *   工具调用/推理/完成 → 中性灰（日常动作，不该抢注意力）
 *
 * 如果八种各给一个颜色，这一页就变成了彩虹，而用户真正需要一眼看到的
 * 「出事了」反而淹没在里面。
 */
export const EVENT_TYPE_TONE: Record<ActivityEventType, 'neutral' | 'accent' | 'ok' | 'risk'> = {
  tool_call: 'neutral',
  reasoning: 'neutral',
  discovery: 'accent',
  poc_generated: 'accent',
  verified: 'ok',
  blocked: 'risk',
  failed: 'risk',
  completed: 'ok',
}
