import { ThemeToggle } from './ThemeToggle'
import { StatusDot } from '@/components/ui/StatusDot'
import { useFleetStatus } from '@/data/fleet'
import { formatCompact, formatPercent } from '@/lib/format'
import { cn } from '@/lib/cn'

/**
 * 顶部全局状态条：几个任务在跑 / 连的哪个 provider / token 用量。
 *
 * 信息取舍（这是全局常驻的一条，每个页面都要让出这一行高度）：
 *   - agent 状态放最左，它是这个产品最该被瞟到的数字。
 *   - provider 只显示短名 + model，不显示 endpoint、key 之类的配置细节 ——
 *     那些属于 Settings。
 *   - token 用量显示"用了多少 / 预算"，不显示"还剩多少"：
 *     需要判断的是"还能不能再开一个扫描"，除法比减法直观。
 *
 * 窄屏策略：**逐级减字，而不是横向滚动。**
 *   第一版把整条做成 overflow-x-auto，320px 下确实不溢出了，但滚动条被我
 *   隐藏了 —— 信息还在 DOM 里，用户却不知道要往右划。等于把信息藏起来，
 *   比直接省略更糟。现在按断点做渐进的省略：
 *     base(<640)  ● 3 运行中 · DeepSeek · —
 *     sm(≥640)    补上 queued、token 百分比、统计窗口
 *     lg(≥1024)   补上 model 名
 *   每个断点下这一条都**恰好放得下**，不需要滚动。
 *   省略规则：先去掉修饰词（"个"），再去掉派生量（百分比），
 *   最后才动 provider —— 因为 provider 决定了"能不能跑"，比用量更重要。
 *
 * ## 任务 7：三段数据的确定性不同，所以显示方式也不同
 *
 *   · 任务数、provider —— **真实数据**（见 `data/fleet.ts` 的来源表）。
 *   · token 用量 —— 契约里没有这个端点，`tokens` 恒为 null。
 *     这里如实写「用量未接入」，而不是留一个看着像真的数字。
 *     原来那一栏写的是 `184.3k / 1M · 18%`，**是编的** ——
 *     而它会被当成"这个月还能跑多少"的依据。
 */
export function TopStatusBar() {
  const { agents, provider, tokens } = useFleetStatus()
  const busy = agents.running > 0

  const tokenRatio = tokens?.budget ? tokens.used / tokens.budget : null

  return (
    <div
      data-status-bar=""
      className="flex h-11 shrink-0 items-center gap-panel border-b border-line bg-surface/80 px-gutter backdrop-blur-sm"
    >
      <div className="flex min-w-0 flex-1 items-center gap-gutter">
        {/* 1 · 任务数 */}
        <div className="flex shrink-0 items-center gap-tight">
          <StatusDot tone={busy ? 'accent' : 'idle'} pulse={busy} />
          <span className="mono-ui tabular text-ink" data-numeric>
            {agents.running}
          </span>
          <span className="text-xs text-ink-muted">
            {/*
              窄屏省略量词。中文比英文紧凑（"3 个扫描运行中" 比 "3 scans running"
              短一半），所以断点比英文版可以更靠后 —— 但真的到 320px 时
              仍需省掉"个"，否则会和用量那一段挤在一起。

              ★ 写「**扫描**运行中」而不是「运行中」（任务 11）。
              这个数字数的是**扫描任务数**，不是智能体实例数 —— 见
              `data/fleet.ts` 文件头：契约里唯一能问到 agent 的端点需要
              一次扫描一个请求，而状态条在每个页面都渲染，所以这里用
              服务端权威的扫描数。语义没问题，**但原来的文案省掉了主语**，
              于是它和活动页左侧的「智能体 X/Y」撞在一起：两个数字都读作
              "智能体"，一个 0 一个 6，看起来就像状态没同步。
              补上"扫描"两个字，歧义就没了。
            */}
            <span className="hidden sm:inline">个扫描运行中</span>
            <span className="sm:hidden">扫描运行中</span>
          </span>
          {agents.queued > 0 && (
            <span className="tabular hidden text-xs text-ink-faint sm:inline" data-numeric>
              · {agents.queued} 排队
            </span>
          )}
        </div>

        <Divider />

        {/* 2 · provider */}
        <div className="flex shrink-0 items-center gap-tight">
          <span className="hidden text-xs text-ink-muted sm:inline">模型</span>
          {provider ? (
            <>
              <span className="text-xs font-medium text-ink">{provider.label}</span>
              <span className="mono-ui hidden text-ink-faint lg:inline">{provider.model}</span>
            </>
          ) : (
            /*
              没有 provider 是**真实状态**（新装的系统就是这样）。
              不能拿一个假的模型名盖住它 —— 用户此刻需要知道的恰恰是
              "还没配，去设置里加一个"，而这一条是全局常驻的，
              它是这个提醒最合适的落点。
            */
            <span className="text-xs text-ink-faint">
              <span className="hidden sm:inline">未配置 · 去「设置」添加模型服务</span>
              <span className="sm:hidden">未配置</span>
            </span>
          )}
        </div>

        <Divider />

        {/* 3 · 用量 */}
        <div className="flex shrink-0 items-center gap-tight">
          {tokens ? (
            <>
              <span className="mono-ui tabular text-ink" data-numeric>
                {formatCompact(tokens.used)}
              </span>
              {tokens.budget ? (
                <>
                  <span className="mono-ui tabular hidden text-ink-faint sm:inline" data-numeric>
                    / {formatCompact(tokens.budget)}
                  </span>
                  {/*
                    百分比前必须有分隔符。实测发现 "184.3k / 1M 18%" 会被连读成
                    "1M 18%" 一个整体 —— 三段数字挤在一起时，等宽字体反而让边界更模糊。
                    用圆点：这里表达的是"用量 / 预算 · 占比"三者的并列，
                    和左侧 "3 个运行中 · 1 排队" 是同一种关系，所以用同一个符号。
                  */}
                  <span aria-hidden="true" className="hidden text-xs text-ink-ghost sm:inline">
                    ·
                  </span>
                  <span
                    className={cn(
                      'tabular hidden text-xs sm:inline',
                      // 超过 80% 才提醒。平时不动用颜色，否则这一条永远在喊
                      tokenRatio && tokenRatio >= 0.8 ? 'text-signal-warn' : 'text-ink-faint',
                    )}
                    data-numeric
                  >
                    {formatPercent(tokenRatio ?? 0)}
                  </span>
                </>
              ) : (
                <span className="hidden text-xs text-ink-faint sm:inline">token</span>
              )}
              {/*
                窗口说明前加分隔符。视觉评审实测发现 "18% 近 24 小时" 里
                两个不同语义的量被连读成了一个。用竖线而不是圆点：
                圆点在这个状态条里已经被当作并列项的连接符用了（"3 个运行中 · 1 排队"），
                这里表达的是"上面那组数的统计口径"，是不同的关系。
              */}
              <span
                aria-hidden="true"
                className="hidden h-3 w-px shrink-0 bg-line lg:inline-block"
              />
              <span className="hidden text-xs text-ink-faint lg:inline">
                {tokens.windowLabel}
              </span>
            </>
          ) : (
            /*
              ★ 契约里没有 token 用量端点（见 data/fleet.ts 的说明）。
                如实写"未接入"，而不是留一个看起来像真的数字 ——
                这一栏原来显示 `184.3k / 1M · 18%`，那是编的，
                而它会被用户当成"这个月还能跑多少"的依据。
            */
            <span className="text-xs text-ink-faint">
              <span className="hidden sm:inline">用量统计未接入</span>
              <span className="sm:hidden">用量未接入</span>
            </span>
          )}
        </div>
      </div>

      {/*
        ★ 主题开关挂在**右端**，而不是塞进左边那三段里。

        左边三段是**系统状态**（在跑什么 / 能不能跑 / 用了多少），是一条按断点
        「逐级减字」的连续信息带；主题开关是一个随时可点的**控件**，
        混进去会破坏那条省略规则，还会被读成"第四段状态"。

        间距沿用这一行自己的 `gap-panel`（见根元素），不另起一套 ——
        它与左端共享同一条 h-11 中轴，靠右站才读得出"这里是与状态无关的操作"。

        ⚠️ 它是 `shrink-0` 的：窄屏下优先压缩左侧可省略的文字，
          而不是把这个唯一的切换入口压变形。
      */}
      <ThemeToggle />
    </div>
  )
}

/** 状态条内部的竖线分隔 —— 用 1px 线而不是圆点，线不抢注意力 */
function Divider() {
  return <span aria-hidden="true" className="h-3.5 w-px shrink-0 bg-line" />
}

export default TopStatusBar
