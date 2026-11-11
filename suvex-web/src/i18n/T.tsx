import { Fragment } from 'react'

import { useI18n } from './index'

/**
 * 行内文案组件：`<T k="findings.view" />`。
 *
 * 为什么需要它：散落在 JSX 文本节点里的中文（表格头、空态句子）如果都要
 * `t()`，就得给每个小组件加 hook；而有些文本节点在 memo 化的子组件、
 * 甚至 map 回调里，加 hook 的位置很容易错（这轮就踩过两次）。
 * 组件形式把 hook 收在一处，调用点只留一个标签。
 *
 * ⚠️ 只用于**文本节点**。属性（aria-label / title / placeholder）仍然用 `t()`。
 */
export function T({
  k,
  vars,
  fallback,
}: {
  k: string
  vars?: Record<string, string | number>
  fallback?: string
}) {
  const { t } = useI18n()
  return <Fragment>{t(k, vars, fallback)}</Fragment>
}
