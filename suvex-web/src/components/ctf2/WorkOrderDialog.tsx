import { loadCtf2WorkOrder, type Ctf2Challenge } from '@/data/ctf2'
import { writeupErrorMessage } from '@/lib/writeup'
import { ClipboardList } from 'lucide-react'
import { useEffect, useState } from 'react'
import {
  WriteupDialogShell,
  type WriteupDialogState,
} from '@/components/writeup/WriteupDialogShell'

/**
 * 处置建议工单对话框（v0.41，应急响应复盘产物）。
 *
 * ## 与 `WriteupDialog` 是**同一个外壳、同一个三态**，只是端点不同
 *
 * 复用 `WriteupDialogShell` 不是省事，而是必须：工单与题解在**呈现上**
 * 是同一类东西（一份后端拼好的 markdown + 一个下载按钮 + 一句可能有
 * 的失败说明）。抄一份弹窗的后果是两份外壳迟早漂移，而症状是
 * "题解的下载按钮在右边、工单的在左边"。
 *
 * ## ★ 它和题解的差别全在**语义**上，不在版式上
 *
 * | | 题解 | 工单 |
 * | --- | --- | --- |
 * | 是什么 | 叙事文档，通读 | 待办清单，逐条批 |
 * | 谁有 | 每道解过的题 | 只有应急响应任务 |
 * | 拿到 409 意味着 | 没解过 / 太早了 | **这不是应急响应任务** |
 * | 内容里有动作吗 | 有攻击动作（已发生） | 只有建议（未发生） |
 *
 * ## ⚠️ 失败态的两句话必须分开
 *
 * 工单的 409 只有一种成因（这类任务不产出工单），但它的**补救动作**
 * 取决于读者是谁：如果他在看一道 pwn 题，正确反应是"我不该在这里找工单"；
 * 如果他在看一道应急响应题，正确反应是"等它跑完"。这两句都由后端
 * 消息承担，前端原样显示即可 —— 不要换成统一的"工单不可用"。
 *
 * ## 不预判"这道题有没有工单"
 *
 * 与 `WriteupDialog` 同一条纪律：入口露不露由卡片那侧的
 * `canHaveWorkOrder` 决定（展示规则），而**能不能真拿到**只有后端知道。
 * 前端多猜一层，就会把"还没跑完"说成"没有这个功能"。
 */
export interface WorkOrderDialogProps {
  /** 要展示工单的那道题。**由调用方条件挂载**，所以这里不会是 null */
  challenge: Ctf2Challenge
  onClose: () => void
}

export function WorkOrderDialog({ challenge, onClose }: WorkOrderDialogProps) {
  const [state, setState] = useState<WriteupDialogState>({ phase: 'loading' })

  useEffect(() => {
    let alive = true
    void loadCtf2WorkOrder(challenge.id)
      .then((data) => {
        if (alive) setState({ phase: 'ready', markdown: data.markdown, filename: data.filename })
      })
      .catch((err) => {
        // ★ 不吞：后端那句中文说明的是"这不是应急响应任务"还是"还没跑完"。
        if (alive) setState({ phase: 'error', message: writeupErrorMessage(err) })
      })
    return () => {
      alive = false
    }
  }, [challenge.id])

  return (
    <WriteupDialogShell
      title={challenge.title || '未命名题目'}
      fallbackFilename={`IR-WorkOrders-${challenge.id}.md`}
      icon={ClipboardList}
      state={state}
      // ★ 与题解的「这道题还没有题解」刻意不同：工单缺失是**正常**的
      //   （绝大多数题不是应急响应任务），措辞不能像出错了。
      errorTitle="这次复盘没有产出工单"
      onClose={onClose}
    />
  )
}

export default WorkOrderDialog
