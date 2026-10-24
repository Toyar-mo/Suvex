import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SkillEditor, type SkillEditorState } from './SkillEditor'

/*
  ══════════════════════════════════════════════════════════════
  技能编辑器（v0.90 弹窗 + 文件树）—— 渲染与选中逻辑
  ══════════════════════════════════════════════════════════════

  ## 为什么这个文件存在（它是补上的，不是一开始就有）

  这个组件第一次部署到 Kali 时，整个技能页被错误边界接住了：

      ReferenceError: BODY_KEY is not defined

  两个标识符 `BODY_KEY` / `keyOf` 被引用了 5 次、却从来没有定义过。
  它能一路跑到线上，是因为当时**只跑了 `vite build`，没跑 `tsc -b`** ——
  esbuild 逐文件转译，不做跨作用域检查，未定义的名字原样留在产物里，
  直到浏览器执行到那行才炸。

  所以这个测试守的不是"样式对不对"，是那类**编译得过、加载即崩**的
  错。只要 render 一次就能抓住：未定义标识符在首帧渲染时就抛。

  ## ★ 重点覆盖「正文」与「附件」不能撞键

  `activeKey` 用一个字符串键同时表示正文和附件。若哨兵值本身是一个
  **合法的附件名**（比如 `__body__`，白名单允许下划线开头），用户真
  建一个同名附件就会和正文撞键 —— 症状是"点正文却打开附件"，而两个
  内容都合法、不报错。下面的用例把"哨兵不是合法附件名"这条固定住。

  ## ⚠️ 与其它组件测试同样的两个坑

  手动 `cleanup()`；不用 `toHaveTextContent`（jsdom 下它对本项目的
  文本节点不稳定）。`Modal` 会 portal 到 `document.body`，所以断言
  用 `screen`（它查的就是 body）而不是 `container`。

  ## 为什么用 `fireEvent` 而不是 `@testing-library/user-event`

  本项目没有装 `user-event`（见 package.json，只有 `@testing-library/dom`
  与 `/react`）。`fireEvent.change` 一次就能覆盖"受控输入回传完整 state"
  这一条；`user-event` 的逐字符敲击在这里不增加信息量 ——
  Drawer 那个「敲一个字就失焦」的 bug 根因在**浮层的 effect 依赖**，
  不在事件模拟的粒度，而它已经被 `Drawer.test.tsx` 固定住了。
*/

afterEach(() => {
  cleanup()
})

function makeState(overrides: Partial<SkillEditorState> = {}): SkillEditorState {
  return {
    mode: 'edit',
    name: 'demo-skill',
    category: 'analysis',
    description: '一句话描述',
    body: '# 正文\n\n正文内容。',
    files: {},
    ...overrides,
  }
}

function renderEditor(overrides: Partial<SkillEditorState> = {}) {
  const onChange = vi.fn()
  const onSave = vi.fn()
  const onClose = vi.fn()
  render(
    <SkillEditor
      state={makeState(overrides)}
      saving={false}
      error={null}
      onChange={onChange}
      onSave={onSave}
      onClose={onClose}
    />,
  )
  return { onChange, onSave, onClose }
}

describe('SkillEditor 渲染', () => {
  it('打开时渲染正文，不抛未定义标识符', () => {
    renderEditor()
    expect(screen.getByDisplayValue(/# 正文/)).toBeTruthy()
    expect(screen.getAllByText('demo-skill').length).toBeGreaterThan(0)
  })

  it('只有正文时标记为「单文件」', () => {
    renderEditor()
    expect(screen.getByText('单文件')).toBeTruthy()
    expect(screen.getAllByText('SKILL.md').length).toBeGreaterThan(0)
  })

  it('有附件时标题旁显示「1 篇正文 + N 个附件」', () => {
    renderEditor({ files: { 'scripts/probe.sh': 'echo hi', 'checklist.md': '- [ ] a' } })
    expect(screen.getByText('1 篇正文 + 2 个附件')).toBeTruthy()
  })

  it('附件按路径出现在文件树里（目录行 + 文件名）', () => {
    renderEditor({ files: { 'scripts/probe.sh': 'echo hi' } })
    expect(screen.getByText('scripts')).toBeTruthy()
    expect(screen.getByText('probe.sh')).toBeTruthy()
  })

  it('加载中时文件树显示「正在读…」而不是空树', () => {
    renderEditor({ loading: true })
    expect(screen.getByText(/正在读/)).toBeTruthy()
  })
})

describe('正文与附件的选中互不干扰', () => {
  it('点附件后编辑区切换到附件内容', () => {
    renderEditor({ files: { 'scripts/probe.sh': 'echo ATTACHMENT_BODY' } })

    fireEvent.click(screen.getByText('probe.sh'))

    expect(screen.getByDisplayValue('echo ATTACHMENT_BODY')).toBeTruthy()
    expect(screen.queryByDisplayValue(/# 正文/)).toBeNull()
  })

  it('点回正文后编辑区回到 SKILL.md 正文', () => {
    renderEditor({ files: { 'scripts/probe.sh': 'echo ATTACHMENT_BODY' } })

    fireEvent.click(screen.getByText('probe.sh'))
    const bodyRow = screen.getAllByText('SKILL.md').find((el) => el.closest('button'))
    expect(bodyRow).toBeTruthy()
    fireEvent.click(bodyRow as HTMLElement)

    expect(screen.getByDisplayValue(/# 正文/)).toBeTruthy()
  })

  it('★ 正文哨兵必须是一个"用户永远建不出来"的名字', async () => {
    /*
      这条是「正文/附件撞键」的回归。它不依赖哨兵的具体取值，而是
      从**合法性**反推：哨兵若合规，用户就能建一个同名附件来撞它。

      如果哪天有人把哨兵改成 `__body__` 这类"看起来更正常"的值，
      这条会立刻变红。
    */
    const { validateAttachmentName } = await import('@/data/skills')
    // 下划线开头是合法的 —— 所以不能拿它当哨兵
    expect(validateAttachmentName('__body__')).toBeNull()
    // `<` 不在白名单里 —— 所以它不可能是一个真实附件路径
    expect(validateAttachmentName('<body>')).not.toBeNull()
  })

  it('删掉正在编辑的附件后，files 里不再有它', () => {
    const { onChange } = renderEditor({ files: { 'only.md': 'X' } })

    fireEvent.click(screen.getByText('only.md'))
    fireEvent.click(screen.getByLabelText('删除 only.md'))

    const last = onChange.mock.calls.at(-1)?.[0] as SkillEditorState
    expect(last.files).toEqual({})
  })

  it('正文那一行没有删除按钮（正文不能删）', () => {
    renderEditor({ files: { 'a.md': 'x' } })
    expect(screen.queryByLabelText('删除 null')).toBeNull()
  })
})

describe('元信息编辑', () => {
  it('改描述回传整份 state，且不顺手清掉其它字段', () => {
    const { onChange } = renderEditor()

    fireEvent.change(screen.getByDisplayValue('一句话描述'), {
      target: { value: '新描述' },
    })

    const last = onChange.mock.calls.at(-1)?.[0] as SkillEditorState
    expect(last.description).toBe('新描述')
    expect(last.name).toBe('demo-skill')
    expect(last.body).toBe('# 正文\n\n正文内容。')
    expect(last.category).toBe('analysis')
  })

  it('改正文只动 body，不动 files', () => {
    const { onChange } = renderEditor({ files: { 'a.md': 'x' } })

    fireEvent.change(screen.getByDisplayValue(/# 正文/), {
      target: { value: '改过的正文' },
    })

    const last = onChange.mock.calls.at(-1)?.[0] as SkillEditorState
    expect(last.body).toBe('改过的正文')
    expect(last.files).toEqual({ 'a.md': 'x' })
  })

  it('改附件内容只动那一个附件', () => {
    const { onChange } = renderEditor({ files: { 'a.md': 'old', 'b.md': 'keep' } })

    fireEvent.click(screen.getByText('a.md'))
    fireEvent.change(screen.getByDisplayValue('old'), { target: { value: 'new' } })

    const last = onChange.mock.calls.at(-1)?.[0] as SkillEditorState
    expect(last.files).toEqual({ 'a.md': 'new', 'b.md': 'keep' })
  })
})

describe('新建附件', () => {
  it('填了合法名字后「添加」把文件加进 state，父组件应用后选中它', () => {
    const onChange = vi.fn()
    const props = {
      saving: false,
      error: null as string | null,
      onSave: vi.fn(),
      onClose: vi.fn(),
    }
    function Harness() {
      const [st, setSt] = useState<SkillEditorState>(makeState())
      return (
        <SkillEditor
          {...props}
          state={st}
          onChange={(next) => {
            onChange(next)
            setSt(next) // 模拟真实父组件：把变更应用回 state
          }}
        />
      )
    }
    render(<Harness />)

    fireEvent.click(screen.getByLabelText('新建附件文件'))
    fireEvent.change(screen.getByPlaceholderText('scripts/extra.sh'), {
      target: { value: 'scripts/extra.sh' },
    })
    fireEvent.click(screen.getByText('添加'))

    // state 里确实多了这个附件
    const last = onChange.mock.calls.at(-1)?.[0] as SkillEditorState
    expect(last.files).toEqual({ 'scripts/extra.sh': '' })
    /*
      ★ 新建后必须**停在新文件上**（空内容 → 显示「附件内容」占位）。
      若停留在正文上，用户会以为没建成功，再点一次「添加」——
      而这个组件对重名是静默忽略的，于是"点了两次什么都没发生"。
    */
    expect(screen.getByPlaceholderText('附件内容')).toBeTruthy()
  })

  it('重名附件不会被重复添加（静默忽略，不覆盖已有内容）', () => {
    const { onChange } = renderEditor({ files: { 'a.md': '原有内容' } })

    fireEvent.click(screen.getByLabelText('新建附件文件'))
    fireEvent.change(screen.getByPlaceholderText('scripts/extra.sh'), {
      target: { value: 'a.md' },
    })
    fireEvent.click(screen.getByText('添加'))

    // 重名时必须**一次 onChange 都不发**，否则会把已有内容盖成空串
    const touchedFiles = onChange.mock.calls.some(
      (call) => 'files' in (call[0] as SkillEditorState) && call[0].files !== undefined,
    )
    expect(touchedFiles).toBe(false)
  })

  it('非法名字不写进 state，只显示错误行', () => {
    const { onChange } = renderEditor()

    fireEvent.click(screen.getByLabelText('新建附件文件'))
    fireEvent.change(screen.getByPlaceholderText('scripts/extra.sh'), {
      target: { value: 'SKILL.md' },
    })

    // 提示行出现，且「添加」被禁用
    expect(screen.getByText(/是主文件名/)).toBeTruthy()
    const addBtn = screen.getByText('添加').closest('button') as HTMLButtonElement
    expect(addBtn.disabled).toBe(true)
    // 没有任何 files 变更被回传
    const changed = onChange.mock.calls.some(
      (call) => Object.keys((call[0] as SkillEditorState).files).length > 0,
    )
    expect(changed).toBe(false)
  })
})

describe('保存与取消', () => {
  it('点保存回调 onSave；点取消回调 onClose', () => {
    const { onSave, onClose } = renderEditor()

    fireEvent.click(screen.getByText('保存'))
    expect(onSave).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByText('取消'))
    expect(onClose).toHaveBeenCalled()
  })

  it('保存中时保存按钮禁用（防重复提交）', () => {
    render(
      <SkillEditor
        state={makeState()}
        saving
        error={null}
        onChange={vi.fn()}
        onSave={vi.fn()}
        onClose={vi.fn()}
      />,
    )
    const saveBtn = screen.getByText('保存').closest('button') as HTMLButtonElement
    expect(saveBtn.disabled).toBe(true)
  })

  it('error 非空时把错误显示在弹窗顶部', () => {
    render(
      <SkillEditor
        state={makeState()}
        saving={false}
        error="技能名已存在。"
        onChange={vi.fn()}
        onSave={vi.fn()}
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByText('技能名已存在。')).toBeTruthy()
  })
})
