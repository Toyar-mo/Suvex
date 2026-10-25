import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import {
  ToolMention,
  parseMentionQuery,
  resolveMentions,
} from './ToolMention'
import type { ChatTool } from '@/data/chat'

/**
 * `@工具` 的判定逻辑（v0.94 第 4 项）。
 *
 * ## 这一组测试在防什么
 *
 * 两类错误都不会让页面崩，只会让功能"看起来有但不工作"：
 *
 * 1. **弹层在错误的时候出现**：用户写邮件地址 `a@b.com`、写代码
 *    `user@host`，弹出一个工具列表挡住输入框。用户不会去报这个 bug，
 *    他只会觉得这个功能很烦然后不用。
 * 2. **发出去的工具名是错的**：`@list_scans_extra` 被认成 `list_scans`，
 *    于是提示词里多了一条"用户点名要用 list_scans"，而用户其实点的
 *    是别的东西 —— 模型的下一轮会朝错误方向去，用户无从得知为什么。
 *
 * 所以断言的粒度是"边界在哪"，不是"大概能用"。
 */

function tool(name: string, available = true): ChatTool {
  return {
    name,
    // ★ `label` 与 `name` 必须**完全不相交**（不能是名字的子串）。
    //   组件同时渲染 `<code>{name}</code>` 与 `<span>{label}</span>`；
    //   夹具里写 `${name} 的标题` 会让 `getByText('list_scans')` 同时命中
    //   `<code>` 和外层 `<span>`（后者的 textContent 含该串），报
    //   "found multiple elements" —— 那是夹具的问题，不是组件的问题。
    label: '标题文本',
    description: `${name} 的说明`,
    params: '无',
    scope: null,
    available,
  }
}

const TOOLS: ChatTool[] = [
  tool('list_scans'),
  tool('create_scan'),
  tool('save_skill'),
  tool('add_mcp_server'),
  // 没有权限的那个：@ 它不该出现在补全里，也不该被写进提示词。
  tool('delete_skill', false),
]

describe('parseMentionQuery：什么时候该弹补全层', () => {
  it('行首一个 @ 就开始补全', () => {
    expect(parseMentionQuery('@', 1)).toEqual({ start: 0, query: '' })
  })

  it('空格后的 @ 也算，取光标前那一段', () => {
    const text = '帮我用 @run'
    expect(parseMentionQuery(text, text.length)).toEqual({ start: 4, query: 'run' })
  })

  it('换行后的 @ 也算（多行输入里换行就是新的语句边界）', () => {
    const text = '第一行\n@list'
    expect(parseMentionQuery(text, text.length)).toEqual({ start: 4, query: 'list' })
  })

  // ── 下面四条是"不该弹"的反面 ────────────────────────────────
  it('邮箱地址里的 @ 不弹（前面不是空白）', () => {
    const text = 'a@b.com'
    expect(parseMentionQuery(text, text.length)).toBeNull()
  })

  it('代码里的 user@host 不弹', () => {
    const text = 'ssh user@host'
    expect(parseMentionQuery(text, text.length)).toBeNull()
  })

  it('@ 后面已经敲了空格就不弹（这句写完了）', () => {
    const text = '@run_shell 跑一下'
    expect(parseMentionQuery(text, text.length)).toBeNull()
  })

  it('光标之后的内容不参与（在中间补字不该弹层）', () => {
    // 光标停在 `@run` 之后，但正文后面还有内容 —— 只要光标前是完整的提及
    // 且没有空白，仍然应该弹（用户就是在那儿补字）。这里断言的是
    // "用的是 caret 而不是 text.length"：如果实现按整串判断，就会因为
    // 后面有空格而返回 null。
    const text = '@run 后面的字'
    expect(parseMentionQuery(text, 4)).toEqual({ start: 0, query: 'run' })
  })

  it('超长的不弹（没有工具名会那么长，弹出来一定是误触）', () => {
    const text = '@' + 'x'.repeat(30)
    expect(parseMentionQuery(text, text.length)).toBeNull()
  })
})

describe('resolveMentions：发出去时带哪些工具名', () => {
  it('整词命中', () => {
    expect(resolveMentions('用 @list_scans 看看', TOOLS)).toEqual(['list_scans'])
  })

  it('一行里点两个，都带上，且不重复', () => {
    const text = '@list_scans 然后再 @create_scan，最后 @list_scans 一次'
    expect(resolveMentions(text, TOOLS)).toEqual(['list_scans', 'create_scan'])
  })

  it('★ 前缀不算命中：@list_scans_extra 不是 list_scans', () => {
    // 这是最容易被写错的一条。用 `includes` 实现的话这里会返回
    // ['list_scans']，于是模型被要求"优先用 list_scans"，
    // 而用户打的是一个根本不存在的工具。
    expect(resolveMentions('@list_scans_extra', TOOLS)).toEqual([])
  })

  it('★ 只认有权限的工具：@delete_skill 不带上', () => {
    // 带上它会让提示词说"用户希望优先用 delete_skill"，而执行时
    // 闸门会拦下来 —— 模型会以为自己失败了。
    expect(resolveMentions('@delete_skill', TOOLS)).toEqual([])
  })

  it('邮箱里的 @ 不解析成提及', () => {
    expect(resolveMentions('发到 a@list_scans.com', TOOLS)).toEqual([])
  })

  it('长名字优先：@add_mcp_server 不被拆成别的东西', () => {
    expect(resolveMentions('@add_mcp_server', TOOLS)).toEqual(['add_mcp_server'])
  })

  it('没有工具时返回空数组（不崩）', () => {
    expect(resolveMentions('@list_scans', [])).toEqual([])
  })
})

describe('ToolMention 渲染', () => {
  it('空查询时列出所有可用工具，不含不可用的那个', () => {
    render(
      <ToolMention
        tools={TOOLS}
        query={{ start: 0, query: '' }}
        onPick={vi.fn()}
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByRole('listbox', { name: '可用工具' })).toBeTruthy()
    expect(screen.getByText('list_scans')).toBeTruthy()
    expect(screen.queryByText('delete_skill')).toBeNull()
  })

  it('按已敲的字母过滤', () => {
    render(
      <ToolMention
        tools={TOOLS}
        query={{ start: 0, query: 'scan' }}
        onPick={vi.fn()}
        onClose={vi.fn()}
      />,
    )
    const options = screen.getAllByRole('option').map((el) => el.textContent ?? '')
    expect(options.some((t) => t.includes('list_scans'))).toBe(true)
    expect(options.some((t) => t.includes('create_scan'))).toBe(true)
    expect(options.some((t) => t.includes('save_skill'))).toBe(false)
  })

  it('没有匹配项时明确说"没有匹配的工具"，而不是什么都不显示', () => {
    render(
      <ToolMention
        tools={TOOLS}
        query={{ start: 0, query: 'zzz' }}
        onPick={vi.fn()}
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByText('没有匹配的工具')).toBeTruthy()
  })

  it('★ 用 mousedown 而不是 click 选中（否则输入框先失焦）', () => {
    const onPick = vi.fn()
    render(
      <ToolMention
        tools={TOOLS}
        query={{ start: 0, query: '' }}
        onPick={onPick}
        onClose={vi.fn()}
      />,
    )
    const first = screen.getAllByRole('option')[0]
    // click 不该触发：真实浏览器里 click 之前会先 blur，那时 draft 可能
    // 已经被清掉了（比如触发了发送）。
    fireEvent.click(first)
    expect(onPick).not.toHaveBeenCalled()

    fireEvent.mouseDown(first)
    expect(onPick).toHaveBeenCalledWith('list_scans')
  })
})
