import { describe, expect, it } from 'vitest'

import {
  disabledCoreTools,
  groupTools,
  healthOf,
  parseAllowedTools,
  parseArgs,
  parseEnv,
  parseTimeout,
  toolHealth,
  validateDraft,
  type KernelTool,
  type McpServerDraft,
} from './tools'

/*
  ══════════════════════════════════════════════════════════════
  tools.ts 的纯函数（v0.82）
  ══════════════════════════════════════════════════════════════

  ## 这个文件守什么

  工具管理页把「这个智能体此刻能调什么」摊开给用户看。页面里最容易
  出错的不是布局，而是**说错话**：

    · 一个开着但依赖没配的工具，说成"被关掉了" → 用户去翻开关，翻不到
    · 一条非法连接名提交上去才报错 → 用户填了三行才被告知第一行就不对
    · 编辑连接时把 `env` 冲成空 → 凭据静默消失，几天后才炸

  这三件都在这里的纯函数里判，所以测它们比测渲染划算：
  同一份判据被页面多处复用（列表行、确认框、表单校验），
  测函数就覆盖了每一处。

  ## 为什么不测 store（`loadTools` / `saveMcpServers`）

  那些函数的价值全在"调了哪个端点、失败时怎么降级"，属于网络契约，
  由组件测试 + 后端 `tests/test_kernel_mcp.py` 两侧一起守。
  这里只测不碰网络的那部分 —— 它们才是判据。
*/

// ─────────────────────────────────────────────
// 造数据
// ─────────────────────────────────────────────

/** 造一个最小可用的内置工具。只填被测函数会读的字段。 */
function makeTool(over: Partial<KernelTool> = {}): KernelTool {
  return {
    name: 'run_command',
    label: '执行命令',
    group: '执行',
    summary: '在沙箱里跑一条命令',
    params: 'command',
    enabled: true,
    core: false,
    requiresConnection: '',
    ready: true,
    reason: '',
    // ★ v0.99：`defaultLimitation` 是必填字段（后端一直返回它）。
    //   夹具不补这个字段，tsc 会报"缺属性" —— 那正是它该报的。
    defaultLimitation: '',
    ...over,
  }
}

/** 造一条最小可用的连接草稿。 */
function makeDraft(over: Partial<McpServerDraft> = {}): McpServerDraft {
  return {
    name: 'demo',
    command: 'npx',
    args: [],
    env: {},
    notes: '',
    allowedTools: null,
    preserveEnv: false,
    callTimeout: null,
    ...over,
  }
}

// ─────────────────────────────────────────────
// toolHealth：四种状态必须分得开
// ─────────────────────────────────────────────

describe('toolHealth：每种"用不了"都要说清是哪一种', () => {
  it('开着且就绪 → ready', () => {
    expect(toolHealth(makeTool({ enabled: true, ready: true }))).toBe('ready')
  })

  it('★ 被关掉 → disabled（哪怕它依赖也配着）', () => {
    // 关掉的工具即便 ready=true 也应该是 disabled：
    // 用户该做的是去打开开关，不是去查连接。
    expect(toolHealth(makeTool({ enabled: false, ready: true }))).toBe('disabled')
  })

  it('★ 开着但没配依赖 → unconfigured（不是 disabled）', () => {
    // 这一条是整个页面存在的理由。合成一个灰点，用户就不知道
    // 该去开关里找还是去 MCP 连接里找。
    const tool = makeTool({
      enabled: true,
      ready: false,
      requiresConnection: 'demo',
      reason: '未找到名为 demo 的连接',
    })
    expect(toolHealth(tool)).toBe('unconfigured')
  })

  it('★ 有依赖声明、但依赖本身不可用（白名单挡住等）→ 仍是 unconfigured', () => {
    // 边界：判据只看「有没有声明依赖」，不看 reason 具体是什么。
    // 这条与上一条只在 reason 上不同，用来钉住"不按 reason 分岔"
    // —— 一旦有人开始按 reason 文案判状态，这里会红。
    const tool = makeTool({
      enabled: true,
      ready: false,
      requiresConnection: 'demo',
      reason: '白名单未包含 recon_scan',
    })
    expect(toolHealth(tool)).toBe('unconfigured')
  })

  it('开着、没有依赖声明却不 ready → misconfigured', () => {
    // 理论不该发生（内置工具不该有"没依赖又不就绪"），
    // 但真发生了要归到 misconfigured 而不是假装 ready。
    const tool = makeTool({ enabled: true, ready: false, requiresConnection: '' })
    expect(toolHealth(tool)).toBe('misconfigured')
  })

  it('★ 四个状态互不相等（防将来被合成一个）', () => {
    const states = new Set([
      toolHealth(makeTool({ enabled: true, ready: true })),
      toolHealth(makeTool({ enabled: false, ready: false })),
      toolHealth(makeTool({ enabled: true, ready: false, requiresConnection: 'x' })),
      toolHealth(makeTool({ enabled: true, ready: false, requiresConnection: '' })),
    ])
    expect(states.size).toBe(4)
  })
})

describe('healthOf：逐行算一次，结果按工具名索引', () => {
  it('每个工具都有一条', () => {
    const tools = [
      makeTool({ name: 'a' }),
      makeTool({ name: 'b', enabled: false }),
      makeTool({ name: 'c', ready: false, requiresConnection: 'm' }),
    ]
    expect(healthOf(tools)).toEqual({
      a: 'ready',
      b: 'disabled',
      c: 'unconfigured',
    })
  })

  it('空清单得到空对象（不能是 undefined）', () => {
    expect(healthOf([])).toEqual({})
  })

  it('重名时后者覆盖前者（后端按名字去重，这里不制造第二种语义）', () => {
    const tools = [makeTool({ name: 'x', enabled: true }), makeTool({ name: 'x', enabled: false })]
    expect(healthOf(tools)).toEqual({ x: 'disabled' })
  })
})

// ─────────────────────────────────────────────
// groupTools：顺序跟 groups 走，空分组要保留
// ─────────────────────────────────────────────

describe('groupTools：分组顺序以 groups 为准', () => {
  it('顺序严格跟 groups，不跟工具出现顺序', () => {
    const tools = [makeTool({ name: 'a', group: '乙' }), makeTool({ name: 'b', group: '甲' })]
    const out = groupTools(tools, ['甲', '乙'])
    expect(out.map((g) => g.group)).toEqual(['甲', '乙'])
    expect(out[0].tools.map((t) => t.name)).toEqual(['b'])
    expect(out[1].tools.map((t) => t.name)).toEqual(['a'])
  })

  it('★ 空分组保留（"这一栏没工具"≠"没这一栏"）', () => {
    // 丢掉空分组会掩盖配置问题：本该有工具的栏位消失了，
    // 界面看起来只是"少了一块"，没人会去查。
    const out = groupTools([makeTool({ group: '甲' })], ['甲', '空的', '乙'])
    expect(out).toHaveLength(3)
    expect(out[1]).toEqual({ group: '空的', tools: [] })
  })

  it('分组内保持原相对顺序', () => {
    const tools = [
      makeTool({ name: '1', group: 'g' }),
      makeTool({ name: '2', group: 'g' }),
      makeTool({ name: '3', group: 'g' }),
    ]
    expect(groupTools(tools, ['g'])[0].tools.map((t) => t.name)).toEqual(['1', '2', '3'])
  })

  it('groups 为空 → 空数组（不把工具丢进"未分组"这种凭空造的分栏）', () => {
    expect(groupTools([makeTool()], [])).toEqual([])
  })

  it('工具的分组不在 groups 里 → 不出现在任何栏（不静默塞进某一栏）', () => {
    const out = groupTools([makeTool({ name: 'x', group: '不存在' })], ['甲'])
    expect(out).toEqual([{ group: '甲', tools: [] }])
  })
})

// ─────────────────────────────────────────────
// disabledCoreTools：决定要不要二次确认
// ─────────────────────────────────────────────

describe('disabledCoreTools：只有 core 工具才值得弹窗', () => {
  it('关掉的里面有 core → 返回那些 core', () => {
    const tools = [
      makeTool({ name: 'run_command', core: true }),
      makeTool({ name: 'list_requests', core: false }),
    ]
    const hit = disabledCoreTools(tools, ['run_command', 'list_requests'])
    expect(hit.map((t) => t.name)).toEqual(['run_command'])
  })

  it('只关了非 core → 空数组（不弹窗）', () => {
    const tools = [makeTool({ name: 'list_requests', core: false })]
    expect(disabledCoreTools(tools, ['list_requests'])).toEqual([])
  })

  it('一个都没关 → 空数组', () => {
    const tools = [makeTool({ core: true })]
    expect(disabledCoreTools(tools, [])).toEqual([])
  })

  it('★ 名字大小写不同不算命中（工具名是标识符，不模糊匹配）', () => {
    const tools = [makeTool({ name: 'run_command', core: true })]
    expect(disabledCoreTools(tools, ['RUN_COMMAND'])).toEqual([])
  })

  it('返回的是工具对象本身，不是名字（调用方要读 label 写措辞）', () => {
    const tool = makeTool({ name: 'run_command', core: true, label: '执行命令' })
    const [hit] = disabledCoreTools([tool], ['run_command'])
    expect(hit).toBe(tool)
    expect(hit.label).toBe('执行命令')
  })
})

// ─────────────────────────────────────────────
// validateDraft：就地报错，别等后端 400
// ─────────────────────────────────────────────

describe('validateDraft：提交前先挡一遍', () => {
  it('合法草稿 → null', () => {
    expect(validateDraft(makeDraft())).toBeNull()
  })

  it('连接名为空 → 报错并说明为什么不能空', () => {
    const msg = validateDraft(makeDraft({ name: '' }))
    expect(msg).toBeTruthy()
    expect(msg).toContain('连接名')
  })

  it('连接名只有空格 → 也报错（trim 后为空）', () => {
    expect(validateDraft(makeDraft({ name: '   ' }))).toBeTruthy()
  })

  it.each(['有中文', '带 空格', 'a/b', 'a+b', 'a@b'])(
    '连接名含非法字符 "%s" → 报错（模型要拿它当标识符）',
    (name) => {
      expect(validateDraft(makeDraft({ name }))).toBeTruthy()
    },
  )

  it.each(['demo', 'demo_v2', 'mock.server', 'a-b-c', 'A1'])('连接名 "%s" 合法', (name) => {
    expect(validateDraft(makeDraft({ name }))).toBeNull()
  })

  it('command 为空 → 报错并说明它是什么', () => {
    const msg = validateDraft(makeDraft({ command: '' }))
    expect(msg).toBeTruthy()
    expect(msg).toContain('command')
  })

  it('★ 连接名的问题优先于 command（从上到下第一个错才算，避免一次报一堆）', () => {
    const msg = validateDraft(makeDraft({ name: '', command: '' }))
    expect(msg).toContain('连接名')
  })

  it('env / notes / args 为空不影响校验（都是可选的）', () => {
    expect(
      validateDraft(makeDraft({ env: {}, notes: '', args: [], allowedTools: null })),
    ).toBeNull()
  })
})

// ─────────────────────────────────────────────
// 解析器：界面上是自由文本，落库前必须结构化
// ─────────────────────────────────────────────

describe('parseArgs：空格分隔 → 数组', () => {
  it('普通串按空白切开', () => {
    expect(parseArgs('-y @modelcontextprotocol/server-filesystem /tmp')).toEqual([
      '-y',
      '@modelcontextprotocol/server-filesystem',
      '/tmp',
    ])
  })

  it('★ 空串得到空数组，不是 [""]（后者会让子进程多一个空参数）', () => {
    expect(parseArgs('')).toEqual([])
  })

  it('多余空白被吞掉（手抖多敲几个空格不该变成空参数）', () => {
    expect(parseArgs('  a   b  ')).toEqual(['a', 'b'])
  })

  it('只有空白 → 空数组', () => {
    expect(parseArgs('   \t\n ')).toEqual([])
  })
})

describe('parseAllowedTools：逗号分隔，空 = null（全放）', () => {
  it('逗号切开并 trim', () => {
    expect(parseAllowedTools('recon_scan, run_poc ,extract_secrets')).toEqual([
      'recon_scan',
      'run_poc',
      'extract_secrets',
    ])
  })

  it('★ 空串 → null 而不是 []', () => {
    // [] 的语义是"一个都不许"，null 才是"全放"。
    // 把"没填"当成"全禁"，会让一条刚建的连接什么也调不动。
    expect(parseAllowedTools('')).toBeNull()
  })

  it('只有分隔符/空白 → null', () => {
    expect(parseAllowedTools(' , , ')).toBeNull()
  })

  it('单个名字 → 长度 1 的数组', () => {
    expect(parseAllowedTools('recon_scan')).toEqual(['recon_scan'])
  })

  it('不按空格切（工具名里可能有空格吗？不，但输入习惯是逗号）', () => {
    expect(parseAllowedTools('a b')).toEqual(['a b'])
  })
})

describe('parseEnv：每行 KEY=VALUE，坏行忽略', () => {
  it('正常若干行', () => {
    expect(parseEnv('API_KEY=abc\nDEBUG=1')).toEqual({ API_KEY: 'abc', DEBUG: '1' })
  })

  it('★ 值里可以有 =（只按第一个 = 切）', () => {
    expect(parseEnv('TOKEN=a=b=c')).toEqual({ TOKEN: 'a=b=c' })
  })

  it('★ 值里的 = 不能被截断，否则 token 会变成半个', () => {
    // base64 结尾的 = 是最常见的真实情形
    expect(parseEnv('TOKEN=abcd==')).toEqual({ TOKEN: 'abcd==' })
  })

  it('# 开头的行是注释，忽略', () => {
    expect(parseEnv('# 这里是凭据\nKEY=v')).toEqual({ KEY: 'v' })
  })

  it('空行忽略', () => {
    expect(parseEnv('\n\nKEY=v\n\n')).toEqual({ KEY: 'v' })
  })

  it('没有 = 的行忽略', () => {
    expect(parseEnv('NOT_AN_ASSIGNMENT\nKEY=v')).toEqual({ KEY: 'v' })
  })

  it('= 在开头（空 key）的行忽略', () => {
    expect(parseEnv('=value')).toEqual({})
  })

  it('key 与 value 两侧空白被 trim', () => {
    expect(parseEnv('  KEY  =  value  ')).toEqual({ KEY: 'value' })
  })

  it('★ 空 value 是合法的（有些开关只要存在即可）', () => {
    expect(parseEnv('FLAG=')).toEqual({ FLAG: '' })
  })

  it('空串 → 空对象', () => {
    expect(parseEnv('')).toEqual({})
  })

  it('后出现的同名 key 覆盖先出现的', () => {
    expect(parseEnv('K=1\nK=2')).toEqual({ K: '2' })
  })
})

// ─────────────────────────────────────────────
// parseTimeout（v0.83）
// ─────────────────────────────────────────────
//
// 三个语义必须分清，混一个就会配出错的连接：
//   空 = 不配置（用内核默认 60 秒） / 数字 = 这条连接的超时 / 非法 = 按不配置处理

describe('parseTimeout', () => {
  it('空串 → null（= 用内核默认，不是 0 秒）', () => {
    // 返回 0 的话后端 `Field(gt=0)` 会 422，而用户只是清空了输入框
    expect(parseTimeout('')).toBeNull()
    expect(parseTimeout('   ')).toBeNull()
  })

  it('正常数字原样保留', () => {
    expect(parseTimeout('300')).toBe(300)
    expect(parseTimeout(' 120 ')).toBe(120)
    expect(parseTimeout('0.5')).toBe(0.5)
  })

  it('★ 非法输入 → null，不是 NaN 也不是 0', () => {
    // NaN 会让 `NaN > 0` 恒为 false，行为静默走偏
    for (const raw of ['abc', '0', '-5', 'NaN', 'Infinity']) {
      expect(parseTimeout(raw)).toBeNull()
    }
  })

  it('★ 超过后端上限（1800）→ null，避免提交上去换 422', () => {
    expect(parseTimeout('1800')).toBe(1800)
    expect(parseTimeout('1801')).toBeNull()
    expect(parseTimeout('99999')).toBeNull()
  })
})
