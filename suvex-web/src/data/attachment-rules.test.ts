import { describe, expect, it } from 'vitest'

import { validateAttachmentName } from './skills'

/**
 * 前后端附件名校验规则一致性。
 *
 * ## 这条测试防的是什么
 *
 * 附件名在两处被校验：前端（不让人白填表单）与后端（真正的安全边界）。
 * 两份正则若漂移，失效方向有**两种**，都很难在别处发现：
 *
 * | 漂移方向 | 症状 |
 * |---|---|
 * | 前端更严 | 用户填一个后端明明接受的名字，被前端拦住 —— "这里不让填？" |
 * | 前端更松 | 用户填完点保存，才收到 400 |
 *
 * 前者是真 bug（功能被挡住），后者只是体验差。所以下面这组用例的
 * 期望值与后端 `_safe_attachment_rel()` 的**实测输出**逐条对齐
 * （实测脚本：`_tmp/crosscheck_backend.py`）。
 *
 * ⚠️ 这不是"再抄一遍正则"——用例期望值是后端跑出来的事实，
 *   后端将来若改规则，这里会红，提醒同步前端。
 */
const BACKEND_ACCEPTS = [
  'a.md',
  'checklist.md',
  'scripts/probe.sh',
  'a/b/c.txt',
  '_x.md',
  'form-1.json',
  'a.b.c.md',
  'docs/SKILL.md',
]

const BACKEND_REJECTS = [
  'SKILL.md',
  '/etc/passwd',
  'a/../b.md',
  './a.md',
  '../a.md',
  'a//b.md',
  '.hidden',
  '-x.md',
  'a/b/c/d.txt',
  'a b.md',
  '中文.md',
  'a:b.md',
]

describe('validateAttachmentName · 与后端实测输出一致', () => {
  it('后端接受的，前端也必须接受（否则用户填不进合法名字）', () => {
    for (const name of BACKEND_ACCEPTS) {
      expect(validateAttachmentName(name), `后端接受但前端拒绝了：${name}`).toBeNull()
    }
  })

  it('后端拒绝的，前端也必须拒绝（否则用户填完才吃 400）', () => {
    for (const name of BACKEND_REJECTS) {
      expect(validateAttachmentName(name), `后端拒绝但前端放过了：${name}`).not.toBeNull()
    }
  })
})
