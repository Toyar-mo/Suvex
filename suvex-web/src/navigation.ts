import {
  Activity,
  AlarmClock,
  BookOpen,
  Bot,
  ClipboardCheck,
  Cpu,
  Network,
  FileText,
  Flag,
  KeyRound,
  FolderOpen,
  Layers,
  MessagesSquare,
  Radar,
  ScanSearch,
  Settings2,
  ShieldAlert,
  Siren,
  Wrench,
  type LucideIcon,
} from 'lucide-react'

/**
 * 导航信息架构 —— 单一事实来源。
 *
 * 侧栏、移动端底栏、路由表、页面标题、aria 描述全部读这一份。
 * 加一个入口只改这里一处，避免「加了导航但忘了加路由」这类漂移。
 *
 * 文案语言：中文（全站界面文案统一中文，见 design-system §3）。
 * 路径与 id 保持英文 —— 它们是标识符，不是文案，改了会让 URL 变得不可读
 * 也不便于和代码/日志对照。
 */

/**
 * 导航分组。
 *
 * 14 个入口平铺时，第一次进来的人不知道先点哪个；分组给的是**判断顺序**，
 * 不是装饰。四组的划分依据是「同一件事的不同阶段」：
 *
 *   运行      —— 系统现在在做什么（看、指挥、等结果）
 *   资产与产出 —— 它做完了留下什么（证据、报告、文件）
 *   自动化    —— 不用人盯的那部分（定时、应急、比赛）
 *   能力与配置 —— 它能干什么、怎么配（工具、技能、设置）
 *
 * 组名必须短：侧栏 208px 放得下，折叠态会隐藏。
 */
export type NavGroupId = 'run' | 'output' | 'automation' | 'config'

export const NAV_GROUPS: ReadonlyArray<{ id: NavGroupId; label: string }> = [
  { id: 'run', label: '运行' },
  { id: 'output', label: '资产与产出' },
  { id: 'automation', label: '自动化' },
  { id: 'config', label: '能力与配置' },
]

export interface NavItem {
  /** 稳定 id，用作 React key 与测试钩子。标识符，不翻译 */
  id: string
  /** 路由路径。标识符，不翻译 */
  path: string
  /** 属于哪一组（侧栏与「更多」面板按组渲染，见 NAV_GROUPS） */
  group: NavGroupId
  /** 侧栏与底栏显示名 */
  label: string
  /** 超长时用的短名（窄屏底栏）。不传则用 label */
  shortLabel?: string
  /** lucide 图标。统一 18px / strokeWidth 1.75，见 SidebarNavItem */
  icon: LucideIcon
  /** 这个页面回答什么问题 —— 页面头部与占位说明共用同一句话，不重复写两遍 */
  blurb: string
  /** 是否常驻移动端底部 tab；false 的收进「更多」面板 */
  inMobileTabBar: boolean
  /**
   * 这个页面是否**占满视口**（内部自己滚动），而不是让 document 滚动。
   *
   * 默认 false —— 全站都是文档滚动（理由见 AppShell 的滚动策略注释）。
   * 只有「智能体活动」是 true：它是一块常驻工作台，事件流需要一个
   * 自包含的滚动区，滚到顶/底不该带动整页，而且页面头会白占 80px。
   *
   * 放在这里而不是在组件里判断路由：导航信息架构是这一页的唯一事实来源，
   * 加页面时它自己声明形态，布局层不需要维护一份"哪些路由特殊"的名单。
   */
  fillsViewport?: boolean
}

export const NAV_ITEMS: NavItem[] = [
  {
    id: 'overview',
    path: '/overview',
    group: 'run',
    label: '总览',
    icon: Radar,
    blurb: '全局态势：此刻在跑什么，已经发现了什么。',
    inMobileTabBar: true,
  },
  {
    id: 'chat',
    path: '/chat',
    group: 'run',
    label: '对话',
    icon: MessagesSquare,
    blurb: '用一句话让它动手：发起扫描、看结果、改技能库、接工具。',
    /*
      ★ 为什么「对话」是一级入口，且紧挨着「总览」排在第二位：

        前面（总览）回答"现在是什么状态"，而紧接着的问题必然是
        "那你说我该做什么"。对话页就是那个下一步动作 —— 用户带着
        总览上看到的一行异常过来，一句话就能让它去查、去扫、去改。
        这两页是同一个决策过程的上下句，中间隔一个"扫描任务"就断了。

      ★ 为什么不排在第一位（v0.93 初版排过）：
        我们要评的是"平台有没有在干活"，第一屏必须先是**事实**
        （跑了几个、发现了什么），而不是一个空输入框。空对话页对新用户
        没有任何信息量，把它放第一会让落地首屏看起来像在等指令而已。

      ★ 为什么它是 `fillsViewport` 而不是文档滚动：
        与「智能体活动」同一条理由 —— 它是一块**工作台**，不是一篇文档。
        页面头会占掉 80px 且滚动时完全无用；消息流要的是自包含的滚动区，
        滚到顶/底不该带动整页。

      ★ 进了移动端底栏：对话是**随时会用**的入口，与总览/扫描同一档使用
        频率。底栏因此是 5 项（总览/对话/扫描/活动/漏洞）—— 手机上 5 项
        是常见上限，再增就该换「更多」了。
    */
    inMobileTabBar: true,
    fillsViewport: true,
  },
  {
    id: 'scans',
    path: '/scans',
    group: 'run',
    label: '扫描任务',
    icon: ScanSearch,
    blurb: '对目标发起扫描，并跟踪每一次运行从排队到出报告的全程。',
    inMobileTabBar: true,
  },
  {
    id: 'activity',
    path: '/activity',
    group: 'run',
    label: '智能体活动',
    shortLabel: '活动',
    icon: Activity,
    blurb: '每个运行中智能体的实时推理与工具调用流。',
    inMobileTabBar: true,
    fillsViewport: true,
  },
  {
    id: 'findings',
    path: '/findings',
    group: 'output',
    label: '漏洞发现',
    icon: ShieldAlert,
    blurb: '每一条漏洞及其证据、验证脚本（PoC）与处置状态。',
    inMobileTabBar: true,
  },
  {
    id: 'assets',
    path: '/assets',
    group: 'output',
    label: '资产总览',
    shortLabel: '资产',
    icon: Layers,
    blurb: '我有哪些目标，每个现在能不能信 —— 包括那些从没被真正检查过的。',
    /*
      ★ 为什么它是一级入口，而不是"漏洞页的一个分组"：
        漏洞页回答「我发现了什么」，这一问题它**结构上**答不上来 ——
        「这台机器我从没检查过」不存在于任何一条漏洞里，它只存在于扫描记录里。
        最危险的形态是「看起来干净」：扫了 5 次全挂的目标，漏洞数是 0，
        在漏洞页与「确实什么都没发现」长得一模一样。把这个判读藏进二级页，
        等于让用户永远看不到它。

      ★ 没有进移动端底栏：底栏只放日常盯的那几个（总览/扫描/活动/漏洞）。
        资产总览是**周期性**打开的（每周过一遍、交付前过一遍），
        不是每秒看一眼的驾驶舱。它进「更多」面板。
    */
    inMobileTabBar: false,
  },
  {
    id: 'schedules',
    path: '/schedules',
    group: 'automation',
    label: '定时复查',
    shortLabel: '复查',
    icon: AlarmClock,
    blurb: '哪些目标在自动复查，以及哪几条计划其实已经不工作了。',
    /*
      ★ 为什么「定时复查」是一级入口，而不是资产页里的一个开关：

        资产页回答「我有哪些目标、每个现在能不能信」。它能把
        「上次扫描是三个月前」标出来 —— 但**标出来之后没有下一步动作**。
        用户看到 12 个目标结论陈旧，唯一能做的是手工一个一个点"再扫一次"。
        这一页就是那个下一步动作：让复查自己发生。

      ★ 为什么它和「扫描」不是同一个入口：
        扫描页是**手动的、一次性的、我要现在就看结果**；
        定时复查是**无人值守的、反复的、我要它自己发生**。
        这两件事的操作节奏完全不同（一个是"发起到看到结果"，一个是
        "排好就不管"），合并会让两种意图互相干扰。

      ★ 没进移动端底栏：底栏只放日常盯的那几个（总览/扫描/活动/漏洞）。
        复查是**周期性**打开的（每周过一遍、发现计划挂了才来看）。
        它进「更多」面板 —— 与资产总览同一条理由。
    */
    inMobileTabBar: false,
  },
  {
    id: 'ir',
    path: '/ir',
    group: 'automation',
    label: '应急响应',
    icon: Siren,
    blurb: '把一批日志与证据包交给智能体，还原入侵时间线并给出处置建议。',
    /*
      ★ 与 CTF2 同一条理由没有进移动端底栏：它是**按事件触发**的功能
        （出事时才开），不是日常盯的那些。它进「更多」面板。

      ★ 但它**必须**是侧栏一级入口，不能像 v0.41 那样藏在 CTF2 页里。
        判据不是"重不重要"，而是**它是不是另一件事**：
        侧栏是"系统有哪几件事可做"的清单，而「应急响应」回答的问题是
        「昨天那台机器到底发生了什么」，与「CTF2 上有哪些题」既不同源
        也不同期。把它挂在 CTF2 页里，等于告诉用户这是一道特殊的题 ——
        而它恰恰**不是**题（没有 flag、没有靶机、没有平台）。
        藏在二级页里的功能项，用户找不到就等于没有。
    */
    inMobileTabBar: false,
  },
  {
    id: 'kernel',
    path: '/kernel',
    group: 'automation',
    // ★ 侧栏显示名用「CTF」而不是「Suvex 内核」：这一项对用户来说就是
    //   「去 ctf2.dasctf.com 拉题、解题」，用域名叫得明白。但内部跑的是
    //   自研的 Suvex 内核（id/path 仍是 kernel），只是对外不把这层名字
    //   露在导航上 —— 内核是实现，不是这个入口要回答的问题。
    label: 'CTF',
    shortLabel: 'CTF',
    icon: Flag,
    blurb: '从 ctf2.dasctf.com 拉取赛事与题目，在独立沙箱里由模型解题。',
    // ★ 没有进移动端底栏：底栏只放最常用的那几个，而解题内核是
    //   周期性使用的功能（比赛期间高频、平时不开）。它进「更多」面板。
    inMobileTabBar: false,
  },
  {
    id: 'reports',
    path: '/reports',
    group: 'output',
    label: '报告',
    icon: FileText,
    blurb: '把一次完成的扫描整理成可以直接交付给他人的报告。',
    inMobileTabBar: false,
  },
  {
    id: 'credentials',
    path: '/credentials',
    group: 'output',
    label: '凭据',
    icon: KeyRound,
    blurb: '扫描中拿到的凭据：加密保存、脱敏展示、复用打分。',
    inMobileTabBar: false,
  },
  {
    id: 'files',
    path: '/files',
    group: 'output',
    label: '文件管理',
    shortLabel: '文件',
    icon: FolderOpen,
    blurb: '对话里产出的报告、脚本、证据都在哪，多大，随时下载。',
    /*
      ★ 为什么单开一页，而不是塞进「对话」侧栏：
        对话页是「按时间线读一次交互」，文件管理是「按文件横向找东西」——
        两者正交。用户常常记得"我让它做过一个 pcap 分析"，却不记得在哪个
        会话里；让他逐个会话翻聊天记录找路径，是把检索成本推给用户。
        这一页就是那个「把产物当一等公民」的索引。
      ★ 没进移动端底栏：它是**回看/取件**用途（做完事回来拿东西），
        与总览/对话/扫描/活动/漏洞这种"日常驾驶舱"不同期。进「更多」。
    */
    inMobileTabBar: false,
  },
  {
    id: 'tools',
    path: '/tools',
    group: 'config',
    label: '工具管理',
    shortLabel: '工具',
    icon: Wrench,
    blurb: '三层能力：内置动作（可开关）、外部 MCP 连接、镜像里的命令行工具。',
    /*
      ★ 没有进移动端底栏：工具管理是**低频、按需**的动作（接一个新工具、
        临时关掉某个能力），不是日常盯的那几个。它进「更多」面板。

      ★ 但它必须是侧栏一级入口，而不是塞进设置页 ——
        判据与「应急响应」那条一致：**它是不是另一件事**。
        设置页四块都在回答"系统怎么跑"（模型/沙箱/通知/账号），
        而这一页回答"能力边界是什么、为什么有几个不可用"，
        它还要管外部进程的拉起与试连。塞进设置页只会让那一页更长更混。
    */
    inMobileTabBar: false,
  },
  {
    id: 'intranet',
    path: '/intranet',
    group: 'run',
    label: '内网会话',
    shortLabel: '内网',
    icon: Network,
    blurb: '人工登记的立足点台账：webshell / 反弹 / ssh；只记录，不自动连接。',
    inMobileTabBar: false,
  },
  {
    id: 'approvals',
    path: '/approvals',
    group: 'config',
    label: '审批记录',
    shortLabel: '审批',
    icon: ClipboardCheck,
    blurb: '跨会话查看工具调用的批准/拒绝记录：谁、什么工具、什么参数、何时。',
    inMobileTabBar: false,
  },
  {
    id: 'models',
    path: '/models',
    group: 'config',
    label: '模型管理',
    shortLabel: '模型',
    icon: Cpu,
    blurb: '模型接入点与按成功判据的路由：换模型、验证连通性、配置默认回退。',
    inMobileTabBar: false,
  },
  {
    id: 'agents',
    path: '/agents',
    group: 'config',
    label: 'Agent',
    shortLabel: 'Agent',
    icon: Bot,
    blurb: '可选角色清单：内置只读，自定义可增删；绑定当前是配置面，执行器接入后生效。',
    inMobileTabBar: false,
  },
  {
    id: 'skills',
    path: '/skills',
    group: 'config',
    label: '技能库',
    shortLabel: '技能',
    icon: BookOpen,
    blurb: '模型手边的速查卡：有哪些、写了什么、哪几篇真的被拉下来用过。',
    /*
      ★ 为什么「技能库」是一级入口，而不是工具管理页的一个区块：

        v0.87 它确实在那里 —— 作为工具页底部一份**只读清单**。那个位置
        答得了「有哪些」，答不了另外两问：这些文本写了什么、哪一篇真的
        被用过。而技能正文直接进模型上下文，是**能改变智能体行为**的东西，
        部署者必须能编辑它、也必须能看出哪一篇是死文本。

      ★ 为什么它和「工具管理」不是同一件事：
        工具页是**拨开关**（13 个固定工具 + 外部 MCP 连接，改的是"开不开"）；
        技能页是**写文章**（31 篇正文，改的是"写了什么"）。
        前者的风险是"关错了变弱"，后者的风险是"改错了模型照着错的做法走"。
        两件事的操作节奏与风险面都不同，合在一起会让两边都变浅。

      ★ 没进移动端底栏：它与工具管理同属**低频、按需**的动作
        （建一个新技能、回头改一篇）。底栏只放日常盯的那几个。
    */
    inMobileTabBar: false,
  },
  {
    id: 'settings',
    path: '/settings',
    group: 'config',
    label: '设置',
    icon: Settings2,
    blurb: '控制台怎么跑：模型、沙箱、解题、通知与权限。改哪一块存哪一块。',
    inMobileTabBar: false,
  },
]

/** 默认落地页 —— 根路径与未知路径都回这里 */
export const DEFAULT_PATH = '/overview'

export const MOBILE_TAB_ITEMS = NAV_ITEMS.filter((item) => item.inMobileTabBar)

/** 「更多」面板里列出的条目（移动端底栏放不下的那些） */
export const MOBILE_MORE_ITEMS = NAV_ITEMS.filter((item) => !item.inMobileTabBar)

/**
 * 按 `NAV_GROUPS` 的顺序分组。空组不返回 —— 某个组全被移走时，
 * 侧栏不该留一个孤零零的标题。
 */
export function groupNavItems(
  items: ReadonlyArray<NavItem> = NAV_ITEMS,
): Array<{ id: NavGroupId; label: string; items: NavItem[] }> {
  return NAV_GROUPS.map((group) => ({
    ...group,
    items: items.filter((item) => item.group === group.id),
  })).filter((group) => group.items.length > 0)
}

/**
 * 判断某个导航项是否处于激活态。
 * 用前缀匹配而非全等：后续会有 /findings/:id、/scans/:id 这类子路由，
 * 进到详情页时父级入口要保持高亮。
 */
export function isNavItemActive(item: NavItem, pathname: string): boolean {
  return pathname === item.path || pathname.startsWith(`${item.path}/`)
}

/** 按 pathname 找当前导航项；未命中（未知路径）返回 undefined */
export function findNavItem(pathname: string): NavItem | undefined {
  return NAV_ITEMS.find((item) => isNavItemActive(item, pathname))
}
