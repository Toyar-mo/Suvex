import { loadCtf2Writeup, type Ctf2Challenge } from '@/data/ctf2'
import { writeupErrorMessage } from '@/lib/writeup'
import { FileText } from 'lucide-react'
import { useEffect, useState } from 'react'
import {
  WriteupDialogShell,
  type WriteupDialogState,
} from '@/components/writeup/WriteupDialogShell'

/**
 * CTF 题解（Writeup）对话框 —— **CTF2 入口**。
 *
 * ## 与 `scans/WriteupDialog` 是**同一个外壳、两条取数路径**
 *
 * 版式必须相同（用户不该觉得"扫描的题解和 CTF2 的题解不是同一个产品"），
 * 但数据来源完全不同：
 *
 * | | 扫描入口 | 本入口 |
 * | --- | --- | --- |
 * | 端点 | `GET /scans/{id}/writeup` | `GET /ctf2/challenges/{id}/writeup` |
 * | 正文从哪来 | strix 报告的版式重排 | `runner` 的 transcript + 结论 |
 * | 失败语义 | 不是夺旗 / 报告被清理 | 从没解过 / 解过但早于 v0.36 |
 *
 * 所以取数留在各自的小组件里，呈现交给 `WriteupDialogShell` ——
 * 把取数塞进外壳会让它必须认识两种调用方式，那就又变回"两份逻辑写在一个文件里"。
 *
 * ## 挂载方式：由调用方**条件挂载**（`{writeupChallenge && <WriteupDialog …/>}`）
 *
 * 与扫描侧完全一致：每次打开都是新实例，初始态直接是 `loading`，
 * 不必在 effect 里"重置"一次（重置会多渲染一轮，而且打开第二道题时
 * 会先闪一眼上一道的题解）。
 *
 * ## ★ 这里**不做**"这道题有没有题解"的预判
 *
 * 入口该不该露出由 `canHaveWriteup` 在卡片那一侧决定（那是一条**展示**
 * 规则）；而一旦打开，能不能真的有，只有后端知道 —— 它那句中文区分了
 * "从没解过"与"解过但早于 v0.36"，前端猜一个只会把两种处境说成一种。
 */
export interface WriteupDialogProps {
  /** 要展示题解的那道题。**由调用方条件挂载**，所以这里不会是 null */
  challenge: Ctf2Challenge
  onClose: () => void
}

export function WriteupDialog({ challenge, onClose }: WriteupDialogProps) {
  const [state, setState] = useState<WriteupDialogState>({ phase: 'loading' })

  /*
    拉题解。**初始态已经是 loading**（组件每次打开都是新实例），
    所以这里不写 `setState({ phase: 'loading' })`。

    `alive` 守卫：请求回来时弹窗可能已经关了（用户按了 Esc）。
    React 18 起对卸载后 setState 不再警告，但这里的状态变化会触发
    一次真实的渲染尝试 —— 挡住它，代价是两行。
  */
  useEffect(() => {
    let alive = true
    void loadCtf2Writeup(challenge.id)
      .then((data) => {
        if (alive) setState({ phase: 'ready', markdown: data.markdown, filename: data.filename })
      })
      .catch((err) => {
        // ★ 不吞：后端那句中文要原样到用户眼前（见上面"不预判"那段）
        if (alive) setState({ phase: 'error', message: writeupErrorMessage(err) })
      })
    return () => {
      alive = false
    }
  }, [challenge.id])

  /*
    ★ 标题用 `challenge.title`，**不回退到 `remoteId` 或空串**。

    扫描侧那里有一段"name 可能只是 target 的复制品"的判断；CTF2 没有
    这个问题（标题是平台给的题名，一定是人话）。而后端拼下载文件名时
    也用的同一个字段，所以两处的"这道题叫什么"天然一致。
  */
  return (
    <WriteupDialogShell
      title={challenge.title || '未命名题目'}
      // 后端一定会给 `filename`；这个兜底只在它为空时生效（不该发生）。
      fallbackFilename={`CTF-Writeup-${challenge.id}.md`}
      icon={FileText}
      state={state}
      // ★ 失败态标题与扫描侧**刻意不同**：这里的两种失败都是"这道题本身
      //   还没产出题解"，而不是"这次扫描没有可看的题解"。
      errorTitle="这道题还没有题解"
      onClose={onClose}
    />
  )
}

export default WriteupDialog
