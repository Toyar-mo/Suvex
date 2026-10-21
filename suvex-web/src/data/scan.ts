import type { Severity } from './findings'

/**
 * 扫描的领域模型。
 *
 * 与 data/overview.ts 里那个精简版 Scan 的关系：
 *   那个是 Overview 的**投影**（只为了算"平均耗时"这类指标）。
 *   这里是完整的扫描对象，Scans 页用。两者共用 id 与 status 的取值，
 *   所以扫描集合只有一份（scans-store），Overview 读的是它的投影。
 */

// ─────────────────────────────────────────────
// 目标
// ─────────────────────────────────────────────

/**
 * Suvex 支持五类目标 —— 这个区分不是 UI 分类，而是**真的走不同流程**：
 *   - url：已部署应用的动态测试
 *   - repo：Git 仓库，先克隆再看代码
 *   - path：本地代码路径，直接读
 *   - service：裸 TCP 端口，直接收发原始字节
 *   - artifact：**只有附件、没有地址**（逆向 / 纯文件 Misc / 密码学）
 * 后几类会做白盒/混合分析，所以目标类型必须在数据里留下来，
 * 它决定了这次扫描用了哪些能力。
 *
 * ★ 为什么必须多出第五类 `artifact`，而不是"让用户随便填个占位地址"：
 *
 * CTF 里有一大类题目根本没有可连的东西 —— 给你一个 ELF、一个流量包、
 * 一段密文，flag 就藏在**文件本身**里。而「目标地址」是必填的，于是
 * 这类题**建不出来**，用户只能填一个假地址，然后看着 agent 对着一个
 * 不存在的主机白跑一轮。那不是绕过限制，那是把一次必然失败的扫描
 * 伪装成一次正常的扫描。
 *
 * ⚠️ 它的特别之处在于**它不是"另一种地址"，而是没有地址**：
 *   · `target` 必须是**空字符串**（后端也这么要求），用户不该填任何东西；
 *   · 必填的是**附件** —— 走向导「任务背景」那一步的上传，最终作为
 *     `attachmentStagingId` 提交；缺附件时后端返 400；
 *   · **执行层没有对应物**：后端把附件解压成一个目录，然后按 `path`
 *     （本地代码路径）的形态交给执行后端。换句话说 `artifact` 是
 *     **建扫描时**的形态，不是**执行时**的形态 —— 所以执行层一行都不用改。
 * 这段推理的完整版在后端 `suvex-api/app/core/vocab.py` 的
 * `TargetKind.ARTIFACT` 上，两处口径必须一致。
 */
export type TargetKind = 'url' | 'repo' | 'path' | 'service' | 'artifact'

/**
 * 这次扫描**要达成什么**（任务 12）。
 *
 * ★ **与 `TargetKind` 是两个正交的维度**，不能合并：
 *
 *   · `TargetKind` 回答「目标是**什么形态**」（网站 / 仓库 / 本地目录），
 *     它决定**怎么测**（黑盒发包还是白盒读代码）。
 *   · `ScanGoal` 回答「**怎样算成功**」。
 *
 * 一个 CTF 靶机是 `url` 形态，扫法和普通网站**完全一样** ——
 * 区别只在于"必须拿到 flag 才算解出"。合并进 `TargetKind` 会让
 * "目标形态"与"任务目标"互相污染。
 */
export type ScanGoal = 'findVulns' | 'captureFlag' | 'verifyFix'

/** 判据的中文名。写成**任务目标**而不是枚举名 —— 它是给用户选的 */
export const SCAN_GOAL_LABEL: Record<ScanGoal, string> = {
  findVulns: '例行安全评估',
  captureFlag: 'CTF 夺旗（拿到 flag）',
  verifyFix: '复测（确认已知问题已修）',
}

/** 每个判据的说明 —— 让用户明白选它之后 agent 的取舍会怎么变 */
export const SCAN_GOAL_HINT: Record<ScanGoal, string> = {
  findVulns: '尽可能找出真实存在的问题，按严重度定级并给修复建议。',
  captureFlag: '优先深挖能通向 flag 的路径，而不是广撒网枚举低危问题。',
  verifyFix: '逐一验证已知问题是否已修复，新发现单独列在最后。',
}

/**
 * 目标类型的中文名。
 *
 * ⚠️ **键顺序就是向导里 radio 的显示顺序** —— `TargetStep` 直接
 * `Object.keys(TARGET_KIND_LABEL)` 渲染，不另排一遍。所以新加的类型
 * 想放在哪里，就必须在**这里**放在哪里，改了顺序等于改了所有用户
 * 按位置记住的那个选择位置。
 */
export const TARGET_KIND_LABEL: Record<TargetKind, string> = {
  url: '已部署的应用',
  repo: 'Git 仓库',
  path: '本地代码路径',
  service: 'TCP 服务',
  // ★ `artifact` 排最后：它是新加的第五类，插在中间会让"第三个是本地路径"
  //   这类位置记忆全部错位，而错位的代价是选错目标类型后跑出一轮空扫描。
  artifact: '题目附件',
}

/** 目标类型的提示语 —— 表单里做占位符与即时校验都读它 */
export const TARGET_KIND_HINT: Record<TargetKind, { placeholder: string; example: string }> = {
  url: {
    placeholder: 'https://api.example.com',
    example: 'https://api.acme.internal',
  },
  repo: {
    placeholder: 'https://github.com/org/repo',
    example: 'https://github.com/acme/backend',
  },
  path: {
    placeholder: '/srv/app 或 D:\\projects\\acme',
    example: '/srv/app',
  },
  service: {
    // ★ 占位符直接写"也可以粘贴命令"：CTF 平台给的就是一条连接命令，
    //   让用户从里面手工抄出 host:port 是白费一遍手，而且抄错很难归因。
    placeholder: '主机:端口，也可直接粘贴平台的连接命令',
    example: '1.2.3.4:9999',
  },
  artifact: {
    /*
      ⚠️ 这两个字段在 `artifact` 下**不会被显示** —— `TargetStep` 把整个
      目标地址输入框换成了说明块（见 WizardSteps），因为这里根本没有
      可填的东西。

      但仍然必须给全，两个理由：
        · 类型是 `Record<TargetKind, …>`，缺字段过不了 typecheck；
        · `validate-target` 与 `WizardSteps` 都按同一形状读它，
          少了字段就是把"这次改动很干净"变成一个运行时的 undefined。
      占位符文案也刻意写成"不需要填写"而不是留空字符串：
      万一将来它在某个分支被顺带渲染出来，读起来也是一句正确的话。
    */
    placeholder: '不需要填写',
    example: '这类题目只需要上传附件',
  },
}

// ─────────────────────────────────────────────
// 技能范围
// ─────────────────────────────────────────────

/**
 * 可选的检测技能包。用户勾选它决定 agent 有哪些能力可用。
 *
 * 每个技能带 applicableKinds：**不是所有技能对所有目标都有意义** ——
 * 对一个纯 URL 做容器配置检查是跑不出东西的。表单据此禁用不相关的项并说明原因，
 * 比让用户勾完再发现"什么都没扫到"要好。
 */
export type SkillId =
  | 'web'
  | 'api'
  | 'container'
  | 'cloud'
  | 'dependencies'
  | 'secrets'
  // ★ 与后端 `core.vocab.SkillId.PROTOCOL` 逐字一致（后端有白名单校验，
  //   拼错会在提交时被 400 打回：`未知的技能：protocol`）。
  | 'protocol'

export interface Skill {
  id: SkillId
  label: string
  description: string
  /** 适用哪些目标类型。空数组表示全适用 */
  applicableKinds: TargetKind[]
  /** 需要凭据吗 —— 影响表单是否提示"这一步需要额外配置" */
  needsCredentials: boolean
}

export const SKILLS: Skill[] = [
  {
    id: 'web',
    label: 'Web 应用',
    description: '表单、认证、会话、注入类问题。',
    applicableKinds: ['url'],
    needsCredentials: false,
  },
  {
    id: 'api',
    label: 'API 接口',
    description: '按 OpenAPI / GraphQL 描述逐个接口测鉴权与输入处理。',
    // ⚠️ 不给 artifact：它要**对着跑起来的接口发请求**，而 artifact 下
    //   没有任何在网线上的东西 —— 附件里就算躺着一份 OpenAPI 描述，
    //   描述的接口本身也不存在。web / container / cloud 是同一条理由
    //   （它们测的是运行中的服务或已部署的环境），所以这四类都不带 artifact。
    applicableKinds: ['url', 'repo', 'path'],
    needsCredentials: false,
  },
  {
    id: 'container',
    label: '容器配置',
    description: 'Dockerfile、compose、K8s 清单里的权限与配置缺陷。',
    applicableKinds: ['repo', 'path'],
    needsCredentials: false,
  },
  {
    id: 'cloud',
    label: '云配置',
    description: 'IaC 模板里的公开桶、过宽 IAM、缺失加密。',
    applicableKinds: ['repo', 'path'],
    needsCredentials: true,
  },
  {
    id: 'dependencies',
    label: '依赖漏洞',
    description: '比对锁文件里的组件版本与已知漏洞库。',
    // ★ 含 artifact：这个技能的全部输入是**文件**（锁文件、清单），
    //   不碰网络。逆向题解包出来的 vendor/、node_modules、
    //   requirements.txt 一样值得过一遍已知漏洞库 —— 组件是不是有已知
    //   漏洞，和这份代码是克隆来的还是上传来的没有关系。
    //   而且 artifact 必须至少有一个可用技能，否则用户在第 2 步
    //   勾不出任何东西，整个目标类型就是一条死路。
    applicableKinds: ['repo', 'path', 'artifact'],
    needsCredentials: false,
  },
  {
    id: 'secrets',
    label: '密钥泄露',
    description: '在代码与配置里找硬编码的凭据、令牌、私钥。',
    // ★ 含 artifact，理由与 dependencies 同：它是在**文件**里找东西的，
    //   判据只有"这段文本像不像凭据"，与文件怎么来的无关。
    //   纯文件 Misc 与逆向题里最常见的收获就是硬编码密钥与内嵌配置。
    applicableKinds: ['repo', 'path', 'artifact'],
    needsCredentials: false,
  },
  {
    id: 'protocol',
    label: '协议分析',
    description: '裸 TCP 端口的协议逆向与二进制交互（Pwn、网络 Misc、Crypto 预言机）。',
    /*
      ★ 唯一一个 applicableKinds 只含 service 的技能（任务 20）。

      ## 为什么必须新增它，而不是给某个已有技能加上 service

      `service`（裸 TCP）加进来时**漏了技能映射** —— 前六个技能没有一个
      适用于它，于是向导第 2 步六项全部置灰、一项都勾不了，而下一步又要求
      "至少勾一项"：**整个目标类型是一条死路**，用户卡在那里出不去。

      ⚠️ 这个坑 `artifact` 当初已经踩过一次（见上面 dependencies 的注释：
         「否则用户在第 2 步勾不出任何东西，整个目标类型就是一条死路」），
         `service` 又踩了一遍。

      ## 那为什么不给 web 或 api 加上 service

      因为技能的实质是**改变 agent 的侧重**（后端 `SKILL_FOCUS` 会把它
      翻成指令里的一句话）。`web` 翻出来是「Web 应用层漏洞（注入、XSS、
      越权、SSRF）」，`api` 是「API 接口（认证、授权、参数校验）」——
      而裸 TCP 上**没有 HTTP 层、没有接口描述**，那句话会把 agent
      往一个不存在的方向带。**技能配错了比不配更糟。**

      所以补的是一个语义上真的存在的能力：对着裸端口做协议逆向、
      构造二进制输入、定位边界与逻辑缺陷。
    */
    applicableKinds: ['service'],
    needsCredentials: false,
  },
]

/**
 * 某个技能是否适用于某类目标。
 *
 * ★ 放在数据层而不是组件里，因为它有**两个**调用点：表单渲染技能列表时
 *   用它分区（适用 / 不适用），切换目标类型时用它剔掉不再适用的已勾选项。
 *   两处各写一遍必然会漂移，而漂移的症状最难查 —— 界面上「API 接口」
 *   明明躺在"不适用"那一区、复选框是空的，提交上去的 `skills` 里却带着它。
 *   （它也过不了 `react-refresh/only-export-components`：组件文件里
 *   导出普通函数会让 fast refresh 失效。）
 *
 * 空数组表示全适用 —— 见 `Skill.applicableKinds` 的说明。
 */
export function isApplicable(skill: Skill, kind: TargetKind): boolean {
  return skill.applicableKinds.length === 0 || skill.applicableKinds.includes(kind)
}

// ─────────────────────────────────────────────
// 扫描
// ─────────────────────────────────────────────

/**
 * 扫描授权声明 —— **必须与后端 `SUPEX_SCAN_AUTHORIZATION_STATEMENT`
 * 逐字一致**。
 *
 * ⚠️ 服务端会**逐字比对**这句话（契约 §5.3）：措辞不一致时返
 * `400 VALIDATION_FAILED`，而不是 403。这是刻意的 ——
 * **防止前端"顺手勾一个默认值"**：用户必须看到、并同意那**具体一句话**，
 * 留痕才有意义（记录下的是一条明确的声明，不是一个布尔）。
 *
 * ★ 所以这是一个**跨仓库对账点**（另外两处是 provider 的默认 base URL、
 *   报告页的标签与措辞表）。改一边必须同时改另一边，
 *   否则新建扫描会在提交那一刻直接 400，而报错看起来像是网络问题。
 */
export const SCAN_AUTHORIZATION_STATEMENT = '我有权对此目标进行安全测试'

export type ScanStatus = 'queued' | 'running' | 'completed' | 'failed' | 'aborted'

export const SCAN_STATUS_LABEL: Record<ScanStatus, string> = {
  queued: '排队中',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  aborted: '已终止',
}

/**
 * 状态的视觉色调。
 * 只有 running 用 accent 呼吸，failed/aborted 用 risk，其余中性或 ok。
 * 与 Activity 页的状态语言一致，不另立一套。
 */
export const SCAN_STATUS_TONE = {
  queued: 'idle',
  running: 'accent',
  completed: 'ok',
  failed: 'risk',
  aborted: 'idle',
} as const satisfies Record<ScanStatus, string>

/** 漏洞数按严重度分档 —— 列表里用紧凑徽标显示，不需要完整分布 */
export type SeverityCounts = Partial<Record<Severity, number>>

export interface Scan {
  id: string
  /** 用户可读的扫描名。默认取目标的主机名，允许改 */
  name: string
  target: string
  targetKind: TargetKind
  status: ScanStatus
  /** 勾选的技能范围 */
  skills: SkillId[]
  /** 使用的 LLM provider id —— 与 Settings 配置对应 */
  providerId: string
  /** ISO 8601 */
  startedAt: string
  finishedAt: string | null
  /** 秒。running 时为 null，由 startedAt 推算 */
  durationSec: number | null
  /**
   * 这次扫描参与过的智能体数。
   *
   * ★ 服务端维护的**汇总列**，不是列表时现算的（一屏几十条，
   *   逐条 count 会变成几十个查询）。所以它必须是真的 ——
   *   一旦腐烂，显示出来的是 `0` 而不是报错，页面上读起来
   *   就是一句理直气壮的"0 个智能体"。
   */
  agentCount: number
  /**
   * 发现汇总。列表的"漏洞数摘要"读它（`ScanList` 的严重度小标签、
   * ScansPage 底部的"累计发现 M 条漏洞"）。
   *
   * ★ 同样是服务端维护的汇总列。口径：**按严重度分档** + **排除误报**，
   *   没有的档位键不存在（`Partial`）。所以"把一条漏洞标成误报"
   *   会让这个映射变小 —— 有意的。
   *
   * ⚠️ 任务 7（前端接入）撞上过它腐烂的样子：后端刷新的函数写好了
   *   却没有调用点，于是扫描列表说"未发现漏洞"，而漏洞页写着两条。
   *   详见 `suvex-api/docs/verification-report.md` §5。
   */
  findings: SeverityCounts
  /** 失败/终止的原因。其他状态为 null */
  failureReason: string | null
  /** 进度 0–1。仅 running/queued 有意义 */
  progress: number | null
  /**
   * strix 侧的运行标识（`strix_runs/<runId>/`）。
   *
   * ★ 后端在**任务 3** 就加了这个字段，而前端类型一直没跟上 ——
   *   于是"能拿去 strix 那边对账"这件事在前端是隐形的（字段被静默丢掉）。
   *   未启动或未能识别时为 null。
   */
  runId: string | null

  /*
    ── v0.13（任务 9）：归档 / 软删除 / 终止方 ──

    ⚠️ 用**时间戳**不用布尔：布尔丢掉"什么时候归档的"，
      而"失败 30 天后自动归档"这条规则要能事后解释。
      与后端 `ScanOut` 逐字一致（契约 §2.1）。
  */
  archivedAt: string | null
  deletedAt: string | null
  /** 谁终止的。判定不出来是 `'unknown'` —— 后端不猜。 */
  terminatedBy: 'user' | 'system' | 'unknown' | null

  /*
    ── v0.27：这条记录是不是别的子系统借来挂事件流的 ──

    `null` = 一次正常的扫描（绝大多数记录）；`'ctf2'` = CTF2 解题
    借来挂活动流的壳。

    ★ 它的用途只有一个，但很要紧：**藏掉「重跑 / 续跑 / 终止」**。

    起因是一次实测的误操作 —— 用户在扫描列表里对 `[CTF2] WEB5[中等]`
    点了「重跑」（那个按钮属于扫描模块，与 CTF2 的解题链路毫无关系）：

      · `rerun_scan` 忠实复制了 `target = ctf2:1cbe06be-…`；
      · strix 只收 URL / 仓库 / 目录 / 域名 / IP，当场拒绝它；
      · argparse 以**用法错误**退出（码 2），而 `EXIT_FINDINGS` **也是 2**
        —— 那条什么都没干的记录被记成「已完成 · 0 秒」；
      · 题目状态一个字没变，而界面上写着"已完成"。

    ⚠️ 后端**也**拦了同一件事（`scans/service.require_launchable_scan`
      返回 `409 SCAN_IS_CTF2_SHELL`）。两层都要有：API 可以被直接调，
      而"点了一个必然静默失败的按钮"不该只靠界面没画出来避免。
  */
  origin: string | null

  /*
    ── v0.17（任务 11）：卡死检测 / 可达性预检 ──

    `stalledAt` 非空 = 后端调度器判定「它很久没有新事件了」。

    ★ 判定权在**后端**，前端只负责显示。用 `lastEventAt` 在前端算
      「静默了多久」是可以的（相对时长不敏感钟差），但**是否算卡死**
      必须由后端说了算 —— 前端各算各的会得到"同一屏里两条记录
      一个标了一个没标"的怪象。
  */
  stalledAt: string | null
  /** 最近一条活动事件的时刻。没有事件时为 null（此时静默时间从 `startedAt` 算） */
  lastEventAt: string | null
  /**
   * 目标可达性预检的一句人话。
   *
   * ⚠️ **只有创建扫描的响应带它**，其余端点一律 null —— 它是"刚刚那次
   *   创建观察到的事实"，不是一个可以事后查询的属性。所以它不会被
   *   存进 `scans-store` 的列表里，而是创建完立刻弹一次 toast。
   */
  notice: string | null

  /*
    ── v0.19（任务 12）：任务背景与成功判据 ──

    `brief` 是**用户提供的任务上下文**：CTF 题面与提示、
    渗透测试的范围与测试账号、复测时要确认哪几条。

    ★ 它是这次改动里最要紧的一个字段 —— 起因是一个很实在的抱怨：
      「只给网址让他扫描太没意义了」。题面里的信息量（考点、提示、
      **诱饵**、flag 格式）远大于我们能猜的。

    ⚠️ 它**可能含凭据**（"测试账号 admin:xxx"）。后端把它写进
      指令**文件**而不是命令行（否则 `ps aux` 里能看到全文）；
      前端也**不要**把它打进 console.log。
  */
  brief: string | null
  /** 成功判据。见 `ScanGoal` —— 它改变 agent 的取舍，不只是报告格式。 */
  goal: ScanGoal

  /*
    ── 任务 18：CTF 夺旗的结果 ──

    ★ 为什么要单独一个字段，而不是从漏洞里读：
      **flag 不是漏洞。** 实测那次逆向题扫描 `findings` 是空的
      （逆向题本来就没有"存在什么问题"可言），而 flag 明明拿到了 ——
      它此前只活在报告文件和 `finish_scan` 事件的 JSON detail 里，
      而那条事件的摘要会被截断，flag 正好落在截断点之后。
      用户的反馈就是四个字：「没看到 flag」。

    ⚠️ `null` 有**三种**含义，渲染时不能都说成"没找到"：
      · 这次不是夺旗任务（看 `goal`）；
      · 是夺旗任务但 agent 确实没拿到；
      · 拿到了，但报告里的形状不唯一、后端**拒绝猜**（见 `scans/flag.py`）。
      第三种是"我们不确定"，不是"没有"。所以措辞要留余地。
  */
  flag: string | null

  /*
    ── v0.44：这条记录对应的 CTF2 题分类 ──

    ★ 它只解决一件事：**列表里的一行怎么称呼自己**。

      `origin` 能说明"这是 ctf2 借的壳"，但区分不出「一道 CTF 题」与
      「一次应急响应复盘」—— 而这两者在那张表里长得一模一样：
      `targetKind` 是 `service`（显示成「TCP 服务」）、`goal` 是
      `captureFlag`（零条目时的措辞在说"夺旗"）、名字带 `[CTF2] `。
      一次日志复盘被渲染成「TCP 服务 · 夺旗」不是说错了一点，是
      **整行都在说一件没发生过的事**：那次任务没有对任何地址发过包。

    ⚠️ 只有 `origin === 'ctf2'` 时可能有值，普通扫描恒为 `null`。
    ⚠️ 历史记录里可能拿不到（题目被删、`scanId` 为 NULL）—— 那时
      要按"当作普通 CTF2 题"降级，不能因为缺这个字段就不渲染行。

    ★ 三个判据收在 `scanDisplay()` 里，不要在各调用点自己 `.startsWith()`：
      调用点已经有列表、写操作回执、详情三处，漏掉任意一处就是
      "同一行数据在不同页面上说法不同"。
  */
  ctf2Category?: string | null
}

/** 一次扫描里所有严重度的总数 */
export function totalFindings(counts: SeverityCounts): number {
  return Object.values(counts).reduce((sum, n) => sum + (n ?? 0), 0)
}

/*
  ── v0.44：一行扫描记录该怎么称呼自己 ──

  ★ 为什么要有这个函数，而不是在 `ScanList` 里就地 `if`：

    同一行数据会被三个地方渲染 —— 扫描列表（`ScanList`）、
    写操作回执（续跑/重跑成功后那一条）、扫描详情。
    三处各写一遍 `startsWith('ir-')` 的必然结果是漏一处，
    而漏掉的那处会把一次日志复盘说成「TCP 服务 · 夺旗」。

  ★ 为什么不复用 `SCAN_GOAL_LABEL` / `TARGET_KIND_LABEL` 里的词：
    那两个表是**用户新建任务时的选项名**（"CTF 夺旗（拿到 flag）」），
    它们回答的是"我要它做什么"。而这里要回答的是"这条记录是什么"，
    两者在 IR 上恰好必须不同 —— 一次复盘既不是夺旗、也不以 TCP 服务
    为目标。共用一套词会让其中一个场景永远说错话。

  ★ 为什么判前缀而不是判等：`ir-root-cause` / `ir-timeline` /
    `ir-scope` 是三个分类，将来还会加。判等的话每加一个就要改这里，
    而漏改的表现是新分类静默退回「TCP 服务」—— 看起来完全正常。
    这与后端 `ctf2/notes.py::is_ir_category` 用 `IR_CATEGORY_PREFIX`
    是同一个判据（两边各自实现是因为跨语言，值必须一致：
    `app/core/vocab.py:1018` 的 `ir-`）。
*/
const IR_CATEGORY_PREFIX = 'ir-'

/** 一次应急响应复盘在界面上的称呼 */
export interface ScanDisplay {
  /** 展示用的名字（IR 已经去掉 `[CTF2] ` 前缀并换成 `[IR] `） */
  name: string
  /** 「目标形态」那一格：IR 固定为「应急响应复盘」，其余走 `TARGET_KIND_LABEL` */
  kindLabel: string
  /** 这一次是不是应急响应复盘 —— 零条目措辞、报告入口都靠它分叉 */
  isIr: boolean
}

/**
 * 把一条扫描记录翻译成界面上那几个字。
 *
 * `ctf2Category` 缺失（老数据、题目被删）时按普通记录处理：
 * 宁可把一次 IR 显示成「TCP 服务」（难看但不错），
 * 也不要把一次普通 CTF 显示成「应急响应复盘」（那是说错话）。
 */
export function scanDisplay(scan: Scan): ScanDisplay {
  const isIr = (scan.ctf2Category ?? '').startsWith(IR_CATEGORY_PREFIX)
  if (!isIr) {
    return {
      name: scan.name,
      kindLabel: TARGET_KIND_LABEL[scan.targetKind] ?? scan.targetKind,
      isIr: false,
    }
  }
  // `[CTF2] ` 是建壳时写死的前缀（`ctf2/events.py:179`）。它说的是
  // "这条记录来自 CTF2"，而一次应急响应复盘与 CTF2 平台无关 ——
  // 留着它等于把两件事混成一件。换 `[IR] ` 而不是直接删：
  // 列表里同名的复盘会有好几条，前缀是唯一能一眼分辨的那一点信息。
  const bare = scan.name.replace(/^\[CTF2\]\s*/, '')
  return { name: `[IR] ${bare}`, kindLabel: '应急响应复盘', isIr: true }
}

/**
 * 「疑似卡死」的显示状态（任务 11）。
 *
 * ★ **判定权在后端**，这里只负责把结论翻译成人话。
 *
 * 前端不自己比时间戳：`stalledAt` 是调度器写的，阈值（`scan_stall_seconds`）
 * 也是服务端配置。前端各算各的会得到"同一屏里两条记录一个标了一个没标"
 * 的怪象 —— 而且用户改不了那个阈值，他没法解释为什么。
 *
 * `silentLabel` 用 `lastEventAt`（没有事件时退回 `startedAt`）算相对时长：
 * **相对时长不敏感服务端与客户端的钟差**，而"是否卡死"敏感 ——
 * 所以后者交给后端，前者本地算。这就是两个字段都给的理由。
 */
export interface StallInfo {
  /** 静默了多久的人话，如「已静默 12 分钟」 */
  silentLabel: string
  /** 悬停提示：解释这是什么、为什么可能只是虚惊 */
  hint: string
}

export function stallInfo(scan: Scan, now: number = Date.now()): StallInfo | null {
  /*
    ★ 必须**还在跑**才算卡死。

    一个已经 `failed`/`aborted` 的扫描即使 `stalledAt` 还挂着，
    也不该再提示"可能卡住了" —— 它已经有结论了，而"卡死"是个
    **悬而未决**的判断。后端在终态时会清掉这个标记，但这里再挡一道：
    两个地方都挡住，这个提示就不可能出现在一条已经结束的记录上。

    这个判断收在**函数内部**而不是每个调用点各写一遍 ——
    调用点已经有三处（扫描列表、活动页、将来的详情页），
    漏掉任意一处就是"同一份数据在不同页面上说法不同"。
  */
  if (scan.status !== 'running') return null
  if (!scan.stalledAt) return null

  const since = scan.lastEventAt ?? scan.startedAt
  const then = new Date(since).getTime()
  // 钟差可能让结果成负数 —— 负数秒读起来像 bug，所以夹到 0
  const seconds = Number.isFinite(then) ? Math.max(0, Math.round((now - then) / 1000)) : null

  return {
    silentLabel: seconds === null ? '已静默' : `已静默 ${humanizeSpan(seconds)}`,
    hint:
      '服务端很久没有收到这次扫描的新活动事件了。' +
      '常见原因是某个命令迟迟不返回，或目标协议与实际不符。' +
      '如果它其实还在跑（只是安静），有新事件后这个标记会自动消失。',
  }
}

/** 秒 → 「12 分钟」「1 小时 5 分」。卡死提示专用，只要一到两级。 */
function humanizeSpan(seconds: number): string {
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest === 0 ? `${hours} 小时` : `${hours} 小时 ${rest} 分`
}

/**
 * 扫描指标的**最小投影**。
 *
 * Overview 只需要这几个字段就能算出「进行中的扫描数」和「平均耗时」。
 * 单独定义它而不是让 Overview 依赖完整 Scan：真实后端里详情是另一个接口，
 * 列表接口不会返回 skills / findings 明细。现在就分开，接 API 时不用重构。
 */
export type ScanMetricsInput = Pick<
  Scan,
  'id' | 'status' | 'startedAt' | 'finishedAt' | 'durationSec' | 'agentCount'
>
