import { ThemeToggle } from './ThemeToggle'
import { StatusDot } from '@/components/ui/StatusDot'
import { useFleetStatus } from '@/data/fleet'
import { formatCompact } from '@/lib/format'

/**
 * 顶部全局状态条：几个任务在跑 / 连的哪个 provider / token 用量。
 *
 * 信息取舍（这是全局常驻的一条，每个页面都要让出这一行高度）：
 *   - agent 状态放最左，它是这个产品最该被瞟到的数字。
 *   - provider 只显示短名 + model，不显示 endpoint、key 之类的配置细节 ——
 *     那些属于 Settings。
 *   - token 用量显示"花了多少"，**不显示"还剩多少"**：
 *     平台不替用户管配额，单价随模型/供应商/时段变，没有分母就不摆除法。
 *
 * 窄屏策略：**逐级减字，而不是横向滚动。**
 *   第一版把整条做成 overflow-x-auto，320px 下确实不溢出了，但滚动条被我
 *   隐藏了 —— 信息还在 DOM 里，用户却不知道要往右划。等于把信息藏起来，
 *   比直接省略更糟。现在按断点做渐进的省略：
 *     base(<640)  ● 3 运行中 · DeepSeek · —
 *     sm(≥640)    补上 queued、用量数字、统计口径
 *     lg(≥1024)   补上 model 名
 *   每个断点下这一条都**恰好放得下**，不需要滚动。
 *   省略规则：先去掉修饰词（"个"），再去掉派生量（占比），
 *   最后才动 provider —— 因为 provider 决定了"能不能跑"，比用量更重要。
 *
 * ## 三段数据的确定性不同，所以显示方式也不同
 *
 *   · 任务数、provider —— **真实数据**（见 `data/fleet.ts` 的来源表）。
 *   · token 用量 —— **也是真实数据**（`GET /overview` 的 `ctf2TotalTokens`，
 *     与总览页 LLM 消耗卡同源）。
 *
 *     ⚠️ 这里曾经写「用量统计未接入」并恒显这行字，理由是"契约里没有
 *     用量端点" —— 那句话在 10-10 写下时是对的，但 10-15 的 `GET /overview`
 *     已经带上了用量字段。于是那一栏成了一**句假话**：数据就在同一个端点里。
 *     2026-10-17 接上真实值，`null` 的含义从"没有这个能力"改成"还没数"。
 */
export function TopStatusBar() {
  const { agents, provider, providerStatus, tokens } = useFleetStatus()
  const busy = agents.running > 0

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
              没有 provider 时要说的话**取决于为什么没有** —— 三种处境
              用同一句话是错的，这是实测到的缺陷，不是理论顾虑：

                · `loading` → 「读取中」。还没拉完就说"未配置"是在抢答。
                · `error`   → 「读不到模型配置」。**这不是"没配"**。
                  viewer 早先就撞在这个分支上：`GET /settings/*` 要 admin，
                  四路 403 让 `accounts` 一直是空的，于是状态条告诉他
                  「未配置 · 去「设置」添加模型服务」—— 而系统里配着
                  默认 provider，且他去设置页只会拿到一整块加载失败。
                  把那句话照原样留着，等于**因为他没权限而谎报系统状态**。
                · `ready` + null → 「未配置 · 去「设置」添加模型服务」。
                  这才是真的没配（新装的系统就是这样），提醒他是对的。
            */
            <span className="text-xs text-ink-faint">
              {providerStatus === 'loading' ? (
                <>
                  <span className="hidden sm:inline">读取中</span>
                  <span className="sm:hidden">—</span>
                </>
              ) : providerStatus === 'error' ? (
                <>
                  <span className="hidden sm:inline">读不到模型配置</span>
                  <span className="sm:hidden">读不到</span>
                </>
              ) : (
                <>
                  <span className="hidden sm:inline">未配置 · 去「设置」添加模型服务</span>
                  <span className="sm:hidden">未配置</span>
                </>
              )}
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
              {/*
                ★ 这里**没有** `预算` 与 `占比` 了（v0.36 起）。
                  旧版显示 `184.3k / 1M · 18%`，其中 `1M` 是编的 ——
                  平台不替用户管配额，单价又随模型/供应商/时段变，
                  写死一个额度会让人误判"还能跑多少"。
                  没有分母，就不摆除法：只报"花了多少"。
              */}
              <span className="hidden text-xs text-ink-faint sm:inline">token</span>
              {/*
                窗口说明前加分隔符。视觉评审实测发现 "3.7M token 近 24 小时" 里
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
              `null` 现在表达的是**还没有数**（未加载完，或库里一条 usage 都没有），
              不表达"平台没有这个能力" —— 数据就在 `GET /overview` 里。

              所以写"暂无"而不是"未接入"：后者会让看过代码的人以为端点还没做，
              而它其实从 10-15 就在了。
            */
            <span className="text-xs text-ink-faint">
              <span className="hidden sm:inline">用量暂无数据</span>
              <span className="sm:hidden">用量 —</span>
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
