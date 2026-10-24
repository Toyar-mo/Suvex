import { describe, expect, it } from 'vitest'

/**
 * 文件树的摊平逻辑。
 *
 * ## 为什么要单独测这一层
 *
 * `buildTree` 是纯函数（`props → 行列表`），但它有三个容易写错的地方，
 * 而且**错了不会报错**，只会画出错的树：
 *
 *   1. 目录的直属文件数算成了"递归总数"（多一层就全错）
 *   2. 同名目录被重复插入（`a/x.md` 与 `a/y/z.md` 共享 `a`）
 *   3. 正文那一行被算进文件数，或 `SKILL.md` 出现在附件里
 *
 * 这三条都不影响编译、不影响类型，只在界面上给出错的数。
 *
 * ⚠️ 这个测试刻意**不 import 页面**（避免拉进 React/图标依赖），
 *   而是把同样的算法在这里复述一份？—— 不。那样测的是复述，不是实现。
 *   所以下面直接 import 真实现；`buildTree` 因此被 `export`。
 */
import { buildTree } from '@/components/kernel/skill-tree'

describe('buildTree', () => {
  it('没有附件时只有正文一行', () => {
    const rows = buildTree({})
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ path: null, label: 'SKILL.md', depth: 0, isDir: false })
  })

  it('正文永远在第一行（它是这一篇技能本身）', () => {
    const rows = buildTree({ 'a.md': '', 'b.md': '' })
    expect(rows[0].path).toBeNull()
  })

  it('顶层文件平铺，深度为 0', () => {
    const rows = buildTree({ 'a.md': '', 'b.md': '' })
    const files = rows.filter((row) => !row.isDir)
    expect(files.map((row) => row.label)).toEqual(['SKILL.md', 'a.md', 'b.md'])
    expect(files.every((row) => row.depth === 0)).toBe(true)
  })

  it('子目录生成目录行，其下文件缩进一级', () => {
    const rows = buildTree({ 'scripts/probe.sh': '' })
    const dir = rows.find((row) => row.isDir)
    expect(dir).toMatchObject({ path: 'scripts', label: 'scripts', depth: 0 })
    const file = rows.find((row) => row.path === 'scripts/probe.sh')
    expect(file).toMatchObject({ label: 'probe.sh', depth: 1 })
  })

  it('★ 多个文件共享同一目录时，目录只出现一次', () => {
    const rows = buildTree({ 'a/x.md': '', 'a/y/z.md': '' })
    const dirs = rows.filter((row) => row.isDir)
    expect(dirs.filter((row) => row.path === 'a')).toHaveLength(1)
  })

  it('★ 目录计数是「直属文件数」，不是递归总数', () => {
    // a/ 下直属 1 个文件（x.md），另有子目录 b/ 里 1 个
    const rows = buildTree({ 'a/x.md': '', 'a/b/y.md': '' })
    const a = rows.find((row) => row.path === 'a' && row.isDir)
    const b = rows.find((row) => row.path === 'a/b' && row.isDir)
    expect(a?.count).toBe(1)
    expect(b?.count).toBe(1)
  })

  it('文件按路径排序，树的顺序稳定', () => {
    const rows = buildTree({ 'z.md': '', 'a.md': '', 'm/x.md': '' })
    const filePaths = rows.filter((row) => !row.isDir).map((row) => row.path)
    expect(filePaths).toEqual([null, 'a.md', 'm/x.md', 'z.md'])
  })

  it('三层路径的深度正确', () => {
    const rows = buildTree({ 'a/b/c.txt': '' })
    expect(rows.find((row) => row.path === 'a')?.depth).toBe(0)
    expect(rows.find((row) => row.path === 'a/b')?.depth).toBe(1)
    expect(rows.find((row) => row.path === 'a/b/c.txt')?.depth).toBe(2)
  })
})
