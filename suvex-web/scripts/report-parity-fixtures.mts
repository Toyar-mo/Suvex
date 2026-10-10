/**
 * 「预览与导出一致性」检查的**测试数据**。
 *
 * ## 为什么单独一个文件，而不是继续 import mock 数据源
 *
 * 任务 7 把全站的 mock 数据源都删了（`findings-mock.ts` / `scans-mock.ts`），
 * 应用运行时不再有任何假数据。但这个检查仍然需要一批**输入** ——
 * 它验的是「同一份 IR 被两个消费者读，内容有没有丢」，
 * 那是纯函数的事，与数据从哪来无关。
 *
 * 让检查脚本去 import 应用的数据模块有两个后果：
 *   1. 检查会随应用数据一起被删掉（就是刚刚发生的事）；
 *   2. 检查的绿色变成"当前恰好有这批数据"，而不是"渲染器覆盖完整"。
 *
 * 所以 fixture 独立放在这里，**故意写死**：它要覆盖的是**分支**，
 * 不是数据量。每一条都对应下面某个具体检查项，删任何一条都会有检查失去意义。
 *
 * ## 这里的每条数据各负责什么
 *
 * | 条目 | 覆盖 |
 * | --- | --- |
 * | `fnd_sqli` | 有 PoC、多行推理、有修复建议、两个受影响位置、人工处置记录 |
 * | `fnd_xss` | **无** PoC（null）、有推理、**无**修复建议 → 附录 C 的"未包含"分支 |
 * | `fnd_idor` | 证据里含 ` ``` ` → Markdown 围栏必须加长，否则表格/正文被吞 |
 * | `fnd_cookie` | 已判误报 + 人工处置记录 → 「只含已确认」时被排除 |
 * | `fnd_info` | 最低档（info）→ distribution 只含有值的项 |
 *
 * 扫描两条：一条 `completed`、一条 `aborted` ——
 * 附录 A 要能如实标出"不完整（已终止）"。
 */
import type { Finding } from '@/data/finding'
import type { Scan } from '@/data/scan'

/** 两个不同目标，让 `scope.kind === 'target'` 有真实的可选值 */
export const FIXTURE_SCANS: Scan[] = [
  {
    id: 'scan_01J8X2',
    name: 'acme-web 全量检测',
    target: 'acme-web',
    targetKind: 'url',
    status: 'completed',
    skills: ['web', 'api'],
    providerId: 'prov_fixture',
    startedAt: '2026-02-14T09:00:00.000Z',
    finishedAt: '2026-02-14T09:42:00.000Z',
    durationSec: 2520,
    agentCount: 4,
    findings: { critical: 1, high: 1, medium: 1 },
    failureReason: null,
    progress: 1,
    runId: 'run_fixture_x2',
  },
  {
    id: 'scan_01J8V4',
    name: 'auth.acme.internal 复测',
    target: 'auth.acme.internal',
    targetKind: 'url',
    status: 'aborted',
    skills: ['web'],
    providerId: 'prov_fixture',
    startedAt: '2026-02-13T14:00:00.000Z',
    finishedAt: '2026-02-13T14:18:00.000Z',
    durationSec: 1080,
    agentCount: 2,
    findings: { low: 1, info: 1 },
    failureReason: '操作者终止',
    progress: 0.4,
    runId: 'run_fixture_v4',
  },
]

/**
 * 多行文本**故意是必须的**：Markdown 渲染器对正文走硬换行
 * （`\n` → 两个空格 + 换行），单行文本测不出"比对方式错了"
 * 这一类假红（见 report-parity.mts 的 containsText 说明）。
 */
export const FIXTURE_FINDINGS: Finding[] = [
  {
    id: 'fnd_sqli',
    scanId: 'scan_01J8X2',
    severity: 'critical',
    category: 'injection',
    title: '搜索接口存在 SQL 注入',
    target: 'acme-web',
    verification: 'verified',
    status: 'confirmed',
    discoveredAt: '2026-02-14T09:12:00.000Z',
    detail: {
      description:
        'GET /search 的 q 参数被直接拼进 SQL 语句。\n攻击者可读取任意表，包括用户凭据表。',
      affected: ['GET /search?q=', 'lib/db/query.ts:41'],
      reasoning:
        '把 q 参数换成单引号之后，接口响应变成 500，错误信息里带出了数据库方言与表名。\n随后用布尔盲注确认了条件真假会改变响应长度。\n两条独立证据都指向同一个拼接点。',
      evidence:
        "HTTP/1.1 500 Internal Server Error\nContent-Type: text/html\n\nYou have an error in your SQL syntax near ''1''' at line 1",
      poc: {
        language: 'python',
        code: "import requests\n\npayload = \"' OR 1=1 -- \"\nr = requests.get('https://acme-web/search', params={'q': payload})\nprint(r.status_code, len(r.text))\n",
      },
      remediation:
        '改用参数化查询，不要拼接字符串。\n同时给数据库账号收回对 information_schema 的读取权限。',
    },
    timeline: [
      {
        id: 'tl_sqli_1',
        at: '2026-02-14T09:12:00.000Z',
        actor: 'agent',
        kind: 'detected',
        summary: '扫描发现：搜索接口存在 SQL 注入',
        from: null,
        to: null,
      },
      {
        id: 'tl_sqli_2',
        at: '2026-02-14T10:05:00.000Z',
        actor: 'user',
        kind: 'status_changed',
        summary: '状态由「待确认」改为「已确认」',
        from: 'new',
        to: 'confirmed',
      },
    ],
  },
  {
    id: 'fnd_xss',
    scanId: 'scan_01J8X2',
    severity: 'medium',
    category: 'xss',
    title: '评论内容未转义直接渲染',
    target: 'acme-web',
    verification: 'partial',
    status: 'new',
    discoveredAt: '2026-02-14T09:20:00.000Z',
    detail: {
      description: '评论正文在列表页以 innerHTML 渲染，未做转义。',
      affected: ['POST /comments'],
      reasoning: '提交带标签的内容后，页面源码里原样出现了该标签。\n但没有绕过 CSP，因此只能算部分验证。',
      evidence: "<div class=\"comment\"><script>alert(1)</script></div>",
      // 这一条**没有**可复现脚本 —— 附录 C 要如实写「未包含」，而不是当作已包含
      poc: null,
      // 也**没有**修复建议 —— 同上
      remediation: null,
    },
    timeline: [
      {
        id: 'tl_xss_1',
        at: '2026-02-14T09:20:00.000Z',
        actor: 'agent',
        kind: 'detected',
        summary: '扫描发现：评论内容未转义直接渲染',
        from: null,
        to: null,
      },
    ],
  },
  {
    id: 'fnd_idor',
    scanId: 'scan_01J8X2',
    severity: 'high',
    category: 'broken_access_control',
    title: '订单接口可用他人 ID 越权读取',
    target: 'acme-web',
    verification: 'verified',
    status: 'new',
    discoveredAt: '2026-02-14T09:31:00.000Z',
    detail: {
      description: 'GET /orders/{id} 只校验登录，不校验归属。',
      affected: ['GET /orders/{id}'],
      reasoning: '用 A 账号的会话读取 B 账号的订单号，返回了完整内容。',
      /*
        ★ 这段证据**必须**内含一行三个反引号。
          否则 Markdown 渲染器不需要加长围栏，report-parity 的
          「代码围栏成对闭合」那一项就永远是绿的 —— 而它是空转的绿。
      */
      evidence:
        'HTTP/1.1 200 OK\nContent-Type: application/json\n\n{"id":2,"email":"bob@acme.internal"}\n\n```json\n{"debug": true}\n```',
      poc: {
        language: 'bash',
        code: "curl -s -b cookies.txt https://acme-web/orders/2\n",
      },
      remediation: '在查询里加上 owner_id = 当前会话用户 的条件。',
    },
    timeline: [
      {
        id: 'tl_idor_1',
        at: '2026-02-14T09:31:00.000Z',
        actor: 'agent',
        kind: 'detected',
        summary: '扫描发现：订单接口可用他人 ID 越权读取',
        from: null,
        to: null,
      },
    ],
  },
  {
    id: 'fnd_cookie',
    scanId: 'scan_01J8V4',
    severity: 'low',
    category: 'misconfiguration',
    title: '会话 Cookie 缺少 SameSite 属性',
    target: 'auth.acme.internal',
    verification: 'none',
    status: 'false_positive',
    discoveredAt: '2026-02-13T14:06:00.000Z',
    detail: {
      description: '登录响应里的会话 Cookie 未设置 SameSite。',
      affected: [],
      reasoning: 'Set-Cookie 头里没有 SameSite。\n但该域名没有跨站入口，实际不可利用。',
      evidence: 'Set-Cookie: session=abc; Path=/; HttpOnly',
      poc: null,
      remediation: '补上 SameSite=Lax。',
    },
    timeline: [
      {
        id: 'tl_cookie_1',
        at: '2026-02-13T14:06:00.000Z',
        actor: 'agent',
        kind: 'detected',
        summary: '扫描发现：会话 Cookie 缺少 SameSite 属性',
        from: null,
        to: null,
      },
      {
        id: 'tl_cookie_2',
        at: '2026-02-13T15:20:00.000Z',
        actor: 'user',
        kind: 'status_changed',
        summary: '状态由「待确认」改为「误报」',
        from: 'new',
        to: 'false_positive',
      },
    ],
  },
  {
    id: 'fnd_info',
    scanId: 'scan_01J8V4',
    severity: 'info',
    category: 'info_disclosure',
    title: '响应头暴露了服务端框架版本',
    target: 'auth.acme.internal',
    verification: 'partial',
    status: 'new',
    discoveredAt: '2026-02-13T14:11:00.000Z',
    detail: {
      description: 'X-Powered-By 里带了具体版本号。',
      affected: [],
      reasoning: '版本号可被用来匹配已知漏洞，但本身不构成问题。',
      evidence: 'X-Powered-By: Express/4.18.2',
      poc: null,
      remediation: '关掉 X-Powered-By。',
    },
    timeline: [
      {
        id: 'tl_info_1',
        at: '2026-02-13T14:11:00.000Z',
        actor: 'agent',
        kind: 'detected',
        summary: '扫描发现：响应头暴露了服务端框架版本',
        from: null,
        to: null,
      },
    ],
  },
]
