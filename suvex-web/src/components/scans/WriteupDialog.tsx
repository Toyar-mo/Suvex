import { loadWriteup } from '@/data/scans-store'
import type { Scan } from '@/data/scan'
import { useEffect, useState } from 'react'
import { writeupErrorMessage } from '@/lib/writeup'
import {
  WriteupDialogShell,
  type WriteupDialogState,
} from '@/components/writeup/WriteupDialogShell'

/**
 * CTF 题解（Writeup）对话框 —— **扫描入口**。
 *
 * 一次夺旗扫描解出 flag 之后，后端能产出一份 Markdown 题解 ——
 * 这个弹窗把它显示出来，并给两个出口：**下载 .md** 与 **复制全文**。
 *
 * ## 为什么是弹窗，不是详情页
 *
 * 题解是**一次扫描的附属产物**，看它的动作发生在列表上（"这道题是怎么解出来的"），
 * 看完就走。为它开一个路由意味着：多一个页面、多一套"从列表怎么进来/怎么回去"
 * 的导航状态，而用户并不会把题解当成一个"要去的地方"。
 *
 * ## ★ 2026-10-14 起它只是**取数的壳**
 *
 * 渲染与外壳都抽到了 `components/writeup/`（`WriteupMarkdown` +
 * `WriteupDialogShell`）—— 因为 CTF2 的题解也要显示同一种东西，
 * 而 CTF2 解题**不走 strix**（它没有 `penetration_test_report.md`），
 * 所以取数路径完全不同、呈现必须相同。
 *
 * ⚠️ 这个文件现在只做三件事：读 `scan.id`、调 `loadWriteup`、
 *    把三态交给外壳。**不要再往这里加渲染逻辑** —— 那会让两个入口
 *    重新开始漂移。
 *
 * ## 挂载方式：由调用方**条件挂载**（`{writeupScan && <WriteupDialog …/>}`）
 *
 * 这样组件每次打开都是新实例，初始态可以直接是 `loading`，
 * 不必在 effect 里"重置"一次（重置会多渲染一轮，而且在挂载瞬间
 * 打开第二条扫描时会先闪一眼上一条的题解）。
 */
export interface WriteupDialogProps {
  /** 要展示题解的那次扫描。**由调用方条件挂载**，所以这里不会是 null */
  scan: Scan
  onClose: () => void
}

export function WriteupDialog({ scan, onClose }: WriteupDialogProps) {
  const [state, setState] = useState<WriteupDialogState>({ phase: 'loading' })

  /*
    拉题解。**初始态已经是 loading**（组件每次打开都是新实例），
    所以这里不写 `setState({ phase: 'loading' })` ——
    在 effect 里同步改状态会多渲染一轮，而且那是"重置"，不是"加载"。

    `alive` 守卫：请求回来时弹窗可能已经关了（用户按了 Esc）。
    React 18 起对卸载后 setState 不再警告，但这里的状态变化会触发
    一次真实的渲染尝试 —— 挡住它，代价是两行。
  */
  useEffect(() => {
    let alive = true
    void loadWriteup(scan.id)
      .then((data) => {
        if (alive) setState({ phase: 'ready', markdown: data.markdown, filename: data.filename })
      })
      .catch((err) => {
        // ★ 不吞：后端那句中文要原样到用户眼前（见外壳里三态说明）
        if (alive) setState({ phase: 'error', message: writeupErrorMessage(err) })
      })
    return () => {
      alive = false
    }
  }, [scan.id])

  /*
    ★ 题目名怎么取。

    优先 `scan.name`，但它**可能只是目标地址的复制品** —— 向导里名称留空时
    后端拿 target 顶上，所以库里 `name === target` 是多数。判据与 ScanList
    里的 `named` 完全一致：只有用户真的起了一个不同的名字，才用名称。

    ⚠️ 两处判据必须一致：不一致的话，同一个扫描在列表里叫 A、在题解弹窗里叫 B，
       而用户是**按列表那一行**点进来的。

    最后那个兜底是给 `artifact` 目标的：它没有地址，`target` 是空串，
    名称也可能为空 —— 那时显示的是一句"未命名题目"，而不是一个空标题。
  */
  const displayName = scan.name && scan.name !== scan.target ? scan.name : scan.target
  const title = displayName || '未命名题目'

  return (
    <WriteupDialogShell
      title={title}
      fallbackFilename={`CTF-Writeup-${scan.id}.md`}
      state={state}
      onClose={onClose}
    />
  )
}

export default WriteupDialog
