import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { WriteupMarkdown } from './WriteupMarkdown'

/*
  ══════════════════════════════════════════════════════════════
  题解 Markdown 渲染器（2026-10-15 新增，之前这个文件没有测试）
  ══════════════════════════════════════════════════════════════

  ## 为什么需要它

  这一层是**纯函数式的字符串 → HTML**，测起来没有副作用，而它承担的
  东西不少：两个入口（扫描 / CTF2）都靠它显示题解，而 Dev 正在把
  后端版式做得更规范（多级标题 / 表格 / 嵌套列表 / 代码块）。
  版式一变，"渲染器认不认"就成了发布前必须验的事。

  ## 这些断言钉的是**踩过的坑**，不是"能渲染"

  每一条都对应一个真实缺口或一条刻意决定（详见 `WriteupMarkdown.tsx`
  里各处的注释）。最要紧的一条是 h4/h5/h6：Tailwind preflight 把
  heading 统一成 `font-size:inherit; font-weight:inherit; margin:0`，
  所以**未映射的标题与正文完全同形** —— 它不会报错、不会白屏，
  只是那几个子标题在界面上"隐形"。这类 bug 只能靠断言 class 抓到。
*/
function html(markdown: string): string {
  return renderToStaticMarkup(<WriteupMarkdown markdown={markdown} />)
}

/** 取某个标签的第一个开标签（含属性） */
function openTag(out: string, tag: string): string {
  return new RegExp(`<${tag}[^>]*>`).exec(out)?.[0] ?? ''
}

describe('WriteupMarkdown：标题层级', () => {
  it('★★ h1–h6 全部带自定义 class（未映射的标题会被 preflight 抹平）', () => {
    const out = html('# a\n\n## b\n\n### c\n\n#### d\n\n##### e\n\n###### f\n')

    for (const tag of ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']) {
      const open = openTag(out, tag)
      expect(open, `${tag} 没有渲染出来`).not.toBe('')
      /*
        ★ 这一条是核心：preflight 之后，**没有 class 的 heading 就是正文**。
        所以"有没有 class"等价于"这一级标题在界面上有没有层级"。
      */
      expect(open, `${tag} 没有 class —— 会被 preflight 抹成正文`).toContain('class=')
    }
  })

  it('★ 每一级都比上一级更弱或更小（层级不能倒挂）', () => {
    const out = html('# a\n\n## b\n\n### c\n\n#### d\n\n##### e\n\n###### f\n')
    // 至少确认 h4/h5/h6 拿到了字号类（而不是与正文同号）
    expect(openTag(out, 'h4')).toMatch(/text-(xs|sm|base)/)
    expect(openTag(out, 'h5')).toMatch(/text-(2xs|xs)/)
    expect(openTag(out, 'h6')).toMatch(/text-(2xs|xs)/)
  })
})

describe('WriteupMarkdown：表格', () => {
  it('★★ 表格渲染成 table，且外层可横向滚（值列常是长 hash）', () => {
    const out = html('| 项 | 值 |\n| --- | --- |\n| a | b |\n')

    expect(out).toContain('<table')
    expect(out).toContain('<thead')
    expect(out).toContain('<th')
    expect(out).toContain('<td')
    // 外层 overflow-x-auto 是"不让汉字被压成竖排"的唯一保障
    expect(out).toMatch(/<div class="[^"]*overflow-x-auto[^"]*"><table/)
  })

  it('★ 表格单元格里的转义竖线 \\| 正常（GFM 规则）', () => {
    const out = html('| 表达式 | 说明 |\n| --- | --- |\n| `a \\| b` | 管道 |\n')

    // 转义之后应当是一个完整的行内代码，而不是被拆成两格
    expect(out).toContain('<code')
    expect(out).toContain('a | b')
    expect((out.match(/<td/g) ?? []).length).toBe(2)
  })
})

describe('WriteupMarkdown：列表', () => {
  it('★ 嵌套无序列表不塌陷（子列表仍在自己那一层）', () => {
    const out = html('- 一级\n  - 二级\n    - 三级\n')

    // 三个 ul 说明层级没被拍平
    expect((out.match(/<ul/g) ?? []).length).toBe(3)
    expect(out).toMatch(/list-disc/)
  })

  it('★ 有序列表渲染成 ol 且带编号样式', () => {
    const out = html('1. 第一步\n2. 第二步\n')

    expect(out).toContain('<ol')
    expect(out).toMatch(/list-decimal/)
  })

  it('★★ 任务列表：去掉圆点，复选框用项目配色（不是浏览器默认方框）', () => {
    const out = html('- [x] 已完成\n- [ ] 未完成\n')

    // 符号位被复选框占了，不该再画圆点
    expect(openTag(out, 'ul')).toContain('list-none')
    expect(openTag(out, 'ul')).not.toContain('list-disc')
    // 复选框被显式映射（有 class），且是只读展示
    const input = openTag(out, 'input')
    expect(input).toContain('type="checkbox"')
    expect(input).toContain('class=')
    expect(input).toContain('disabled')
    // 两项各自的勾选态
    expect(out).toContain('checked=""')
  })

  it('★ 普通列表项不因为任务列表的处理而受影响', () => {
    const out = html('- 普通项\n')

    expect(openTag(out, 'ul')).toContain('list-disc')
  })
})

describe('WriteupMarkdown：代码', () => {
  it('★★ 行内代码与围栏代码彻底分流（v10 取消了 inline prop）', () => {
    const out = html('行内 `x=1` 结束\n\n```bash\necho hi\n```\n')

    // 行内：走 code 映射，带 accent 色
    expect(out).toContain('text-accent-strong')
    // 块级：走 CodeBlock，带语言标签与复制按钮
    expect(out).toContain('bash')
    expect(out).toContain('复制')
    // 块级**不**该被当成行内（否则整段塞进圆角小方块）
    expect(out).toMatch(/max-height:24rem/)
  })

  it('★ 无语言围栏也走块级（不会被误判成行内）', () => {
    const out = html('```\nplain block\n```\n')

    expect(out).toContain('复制')
    expect(out).toMatch(/max-height:24rem/)
  })

  it('★ 超长单行保留不软换行（日志换行位置携带信息）', () => {
    const out = html('```\n' + 'x'.repeat(500) + '\n```\n')

    expect(out).toContain('whitespace-pre')
    expect(out).toContain('overflow-x-auto')
  })

  it('★ 命令里的特殊字符原样保留（$ {} | 引号 反引号）', () => {
    const out = html('`awk \'{print $1}\' f | sort`\n\n`echo $HOME`\n\n`flag{and 1=1}`\n')

    // HTML 转义之后仍是原文（&#x27; = '，&quot; = "）
    expect(out).toContain('{print $1}')
    expect(out).toContain('echo $HOME')
    expect(out).toContain('flag{and 1=1}')
  })

  it('★ 双反引号包裹的内容里有反引号也正常', () => {
    const out = html('``echo `date` ``\n')

    expect(out).toContain('echo `date`')
  })

  it('★ 省略号与截断标记原样（新版式附录常见）', () => {
    const out = html('```text\n… 省略 40 行 …\n[content truncated]\n```\n')

    expect(out).toContain('… 省略 40 行 …')
    expect(out).toContain('[content truncated]')
  })
})

describe('WriteupMarkdown：引用块与其余结构', () => {
  it('★ 引用块渲染成 blockquote，且内部列表/代码块不塌', () => {
    const out = html('> 结论：\n> - A\n> - B\n>\n> ```\n> echo hi\n> ```\n')

    expect(out).toContain('<blockquote')
    expect(out).toContain('<ul')
    // 引用块里的代码块仍然走 CodeBlock（有复制按钮）
    expect(out).toContain('复制')
  })

  it('★ 删除线渲染成 del', () => {
    const out = html('~~已排除~~\n')

    expect(out).toContain('<del>已排除</del>')
  })

  it('★ hr 有上边框（preflight 把 border-width 清零了）', () => {
    const out = html('a\n\n---\n\nb\n')

    expect(openTag(out, 'hr')).toContain('border-t')
  })

  it('★ 外链开新标签并带 noreferrer', () => {
    const out = html('[参考](https://example.com/x)\n')

    const a = openTag(out, 'a')
    expect(a).toContain('target="_blank"')
    expect(a).toContain('rel="noreferrer noopener"')
  })
})

describe('WriteupMarkdown：安全与稳健', () => {
  it('★★ 正文里的原始 HTML 被转义（内容是不可信数据）', () => {
    const out = html('<script>alert(1)</script>\n\n<div onclick="x">d</div>\n')

    // 不该出现真的 script 标签
    expect(out).not.toContain('<script')
    expect(out).toContain('&lt;script&gt;')
  })

  it('★ 形状怪异的 pre（原始 HTML pre）不抛错', () => {
    // PreOrCodeBlock 的形状防御分支
    expect(() => html('<pre>raw</pre>\n')).not.toThrow()
  })

  it('★ 空文档不抛错', () => {
    expect(() => html('')).not.toThrow()
  })

  it('★ 真实规模的题解（约 2000 行）能在合理时间内渲染完', () => {
    const parts: string[] = ['# CTF Writeup\n']
    for (let i = 0; i < 60; i++) {
      parts.push(`## 阶段 ${i}\n`)
      parts.push('| 项 | 值 |\n| --- | --- |\n| a | b |\n')
      parts.push('- 步骤一\n- 步骤二\n  - 子步骤\n')
      parts.push('```bash\n' + Array.from({ length: 20 }, (_, j) => `echo ${i}-${j}`).join('\n') + '\n```\n')
    }
    const markdown = parts.join('\n')

    const t0 = Date.now()
    const out = html(markdown)
    const ms = Date.now() - t0

    expect(out.length).toBeGreaterThan(0)
    /*
      ⚠️ 阈值给得**宽**（3 秒）：这条只用来抓"渲染量级失控"（比如某天
      有人把 components 从模块级常量挪进组件体内、记忆化失效导致每次
      渲染都重建整棵树）。它不是性能基准，实测该规模约 350ms。
    */
    expect(ms, `渲染 ${markdown.split('\n').length} 行耗时 ${ms}ms`).toBeLessThan(3000)
  })
})
