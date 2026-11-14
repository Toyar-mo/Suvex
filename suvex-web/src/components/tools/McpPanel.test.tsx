/**
 * `McpPanel` 的 v1.62 http 传输表单**渲染**验证。
 *
 * ## 为什么要这条
 *
 * `tools.test.ts` 断言的是 `validateDraft` / `parseHeaders` 这些**纯函数**的
 * 行为。它们证明不了"用户在界面上**真的能**选 http、填 url、看不到 command 框"
 * —— 那正是本文件补的另一半：把组件真的渲染出来，按用户路径点一遍。
 *
 * 用户当场报的 bug 就是这条链路的反面：贴了一条 `{type:"http", url:...}`，
 * 界面却只给了一个 command 框（逼他填可执行文件），于是他/模型拿 `npx`
 * 去 spawn 进程、连不上远端 URL，报 "stdout 已关闭"。
 *
 * 这里守的是：
 *   1. 新增连接时默认 stdio、有 command 框、没有 url / 请求头框；
 *   2. 切成 http → 出现 url 框 + 请求头框，command 框消失；
 *   3. 已保存的 http 连接在列表里显示 `http <url>`（而不是空命令行）；
 *   4. http 缺 url 点"添加" → 就地报错，不回调 onSave。
 *
 * ⚠️ `vitest.config.ts` 关了 `globals`，自动 cleanup 不注册，手动 `cleanup()`。
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { McpPanel } from './McpPanel'
import type { McpServer, McpServerDraft } from '@/data/tools'

afterEach(() => {
  cleanup()
})

function httpServer(over: Partial<McpServer> = {}): McpServer {
  return {
    name: 'ctf2-suvex',
    type: 'http',
    command: '',
    url: 'https://ctf2.dasctf.com/api/ai/v1/mcp',
    args: [],
    notes: '',
    allowedTools: null,
    hasEnv: false,
    hasHeaders: true,
    callTimeout: null,
    ...over,
  }
}

function renderPanel(servers: McpServer[] = [], onSave = vi.fn()) {
  render(<McpPanel servers={servers} saving={false} error={null} onSave={onSave} onTest={vi.fn()} />)
  return onSave
}

describe('McpPanel：连接模板', () => {
  it('从模板添加 Burp 会预填 http 与端点', () => {
    renderPanel()
    fireEvent.click(screen.getByRole('combobox', { name: '从模板添加' }))
    fireEvent.click(screen.getByRole('option', { name: 'Burp Suite' }))

    expect(screen.getByDisplayValue('burp')).toBeTruthy()
    expect(
      screen.getByDisplayValue('http://127.0.0.1:9876/sse'),
    ).toBeTruthy()
  })
})

describe('McpPanel v1.62：http 传输表单', () => {
  it('新增连接默认 stdio：有 command 框、没有 url 框', () => {
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /添加连接/ }))
    // stdio 有「命令（可执行文件）」框
    expect(screen.getByText(/命令（可执行文件）/)).toBeTruthy()
    // 没有 url 输入框
    expect(screen.queryByPlaceholderText(/example.com\/api\/mcp/)).toBeNull()
  })

  it('★ 切成 http → 出现 url 框 + 请求头框，command 框消失', () => {
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /添加连接/ }))
    fireEvent.click(screen.getByRole('combobox', { name: '传输类型' }))
    fireEvent.click(screen.getByRole('option', { name: 'http（连远端端点）' }))
    // url 框出现
    expect(screen.getByPlaceholderText(/example.com\/api\/mcp/)).toBeTruthy()
    // 请求头框出现
    expect(screen.getByText(/请求头（每行 KEY: VALUE）/)).toBeTruthy()
    // command 框消失
    expect(screen.queryByText(/命令（可执行文件）/)).toBeNull()
  })

  it('已保存的 http 连接在列表里显示 http <url> 与 HTTP 徽标', () => {
    renderPanel([httpServer()])
    expect(screen.getByText('HTTP')).toBeTruthy()
    expect(screen.getByText(/http https:\/\/ctf2\.dasctf\.com\/api\/ai\/v1\/mcp/)).toBeTruthy()
    // 带请求头徽标（hasHeaders=true）
    expect(screen.getByText('带请求头')).toBeTruthy()
  })

  it('http 缺 url 点「添加」→ 就地报错，不回调 onSave', () => {
    const onSave = renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /添加连接/ }))
    // 填名字 + 切 http，但 url 留空
    fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('combobox', { name: '传输类型' }))
    fireEvent.click(screen.getByRole('option', { name: 'http（连远端端点）' }))
    fireEvent.click(screen.getByRole('button', { name: /^添加$/ }))
    expect(onSave).not.toHaveBeenCalled()
    expect(screen.getByText(/http 连接要填 url/)).toBeTruthy()
  })

  it('★ http 填了合法 url → 提交的草稿 type=http、url 保留、command 为空', () => {
    const onSave = renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /添加连接/ }))
    fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'ctf2' } })
    fireEvent.click(screen.getByRole('combobox', { name: '传输类型' }))
    fireEvent.click(screen.getByRole('option', { name: 'http（连远端端点）' }))
    fireEvent.change(screen.getByPlaceholderText(/example.com\/api\/mcp/), {
      target: { value: 'https://ctf2.dasctf.com/api/ai/v1/mcp' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^添加$/ }))
    expect(onSave).toHaveBeenCalledTimes(1)
    const submitted = onSave.mock.calls[0][0] as McpServerDraft[]
    expect(submitted).toHaveLength(1)
    expect(submitted[0].type).toBe('http')
    expect(submitted[0].url).toBe('https://ctf2.dasctf.com/api/ai/v1/mcp')
    expect(submitted[0].command).toBe('')
  })
})
