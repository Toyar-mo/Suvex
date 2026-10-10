import { ArrowRight, Terminal } from 'lucide-react'
import { Link } from 'react-router-dom'
import { cn } from '@/lib/cn'

/**
 * 「一次扫描都还没跑过」的空状态 —— **全站共用一份**。
 *
 * ## 为什么必须共用
 *
 * 新用户第一次打开系统时，会**依次**看到好几页。如果每一页都自己写一份
 * 「还没有数据」，最容易出现的问题是：其中某一页忘了给下一步
 * （最常见的就是"只有一句灰字，没有按钮"），而用户恰好第一眼落在那页 ——
 * 他就此停住，以为系统还没做好。
 *
 * 抽成一份之后，"没扫过"这件事在四个页面上的**行动引导必然一致**：
 * 都是同一个按钮、同一个目标地址、同一句 CLI 提示。
 *
 * ## 它换的是「整页主张」，不是"把数字设成 0"
 *
 *   有数据时 —— "这是你现在的态势"
 *   没数据时 —— "从这里开始"
 *
 * 刻意避免的做法（空状态最常见的三个错误）：
 *   1. 显示一排全 0 的指标卡。全 0 会被读成"系统挂了"或"被清零了"，
 *      而不是"你还没开始"。所以调用方在拿到 `hasAnyScan === false` 时
 *      应当**整块替换**，而不是渲染 0 值。
 *   2. 放一张灰色插画 + "No data"。没有给用户下一步。
 *   3. 把 "Get started" 做成装饰性大标题。这里唯一的行动是**开始第一次扫描**，
 *      所以正文就写它，按钮就指向它。
 *
 * ## 副文案为什么由调用方传
 *
 * 四个页面要回答的是同一个问题（"我还没跑过，接下来会怎样"），
 * 但**落到这一页会看到什么**各不相同：总览说"态势会出现在这里"、
 * 活动流说"每一次工具调用会出现在这里"、漏洞库说"问题会出现在这里"。
 * 把这句也统一了，它就会变成一句与当前页面无关的废话 ——
 * 而空状态恰恰是用户最需要"它说的是这一页"的时候。
 */

export interface NoScansYetProps {
  /** 标题。默认「还没有扫描记录」 */
  title?: string
  /** 这一页自己的说明 —— 回答"跑起来之后，这里会出现什么" */
  description: string
  /**
   * 是否画虚线外框。默认画。
   *
   * 调用方在**已经有边框的容器里**渲染它时传 `false`（如扫描列表的空态位），
   * 否则会出现框套框。
   */
  bordered?: boolean
  className?: string
}

export function NoScansYet({
  title = '还没有扫描记录',
  description,
  bordered = true,
  className,
}: NoScansYetProps) {
  return (
    <div
      className={cn(
        'flex flex-col items-start',
        bordered && 'rounded-md border border-dashed border-line-strong p-bay',
        className,
      )}
    >
      <Terminal size={20} strokeWidth={1.5} className="text-ink-ghost" aria-hidden="true" />

      <h2 className="mt-panel text-lg font-medium text-ink">{title}</h2>

      <p className="mt-tight max-w-[56ch] text-sm text-ink-muted">{description}</p>

      <div className="mt-bay flex flex-wrap items-center gap-panel">
        {/*
          按钮配色是实测选的，不是挑的：
            原始方案 深紫底 + 浅字 = 4.06:1（hover 时掉到 2.74:1）—— 不达 AA。
            现在改为 主色底 + 近黑字 = 5.98:1，hover 提亮到 accent-strong = 8.36:1。
          亮紫配深字是这套 token 里唯一能同时满足"保持主色醒目"和"文字可读"的组合。
        */}
        <Link
          to="/scans"
          className="press group inline-flex items-center gap-tight rounded-sm bg-accent px-3 py-2 text-sm font-medium text-canvas transition-ui duration-fast hover:bg-accent-strong"
        >
          发起第一次扫描
          <ArrowRight
            size={15}
            strokeWidth={2}
            className="transition-transform duration-fast group-hover:translate-x-0.5"
            aria-hidden="true"
          />
        </Link>

        {/*
          保留一行等宽的命令提示：这个产品的用户是渗透测试人员，
          他们大概率更愿意从 CLI 起手；而且这行字同时说明了
          "扫描需要指定目标"这件事 —— 是一句有用的说明，不是装饰。
        */}
        <span className="mono-ui text-ink-faint">或在本地执行 suvex scan &lt;target&gt;</span>
      </div>
    </div>
  )
}

export default NoScansYet
