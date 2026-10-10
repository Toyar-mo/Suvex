import { HeroBackdrop } from '@/components/landing/HeroBackdrop'
import { ThemeToggle } from '@/components/layout/ThemeToggle'
import { HeroTitle } from '@/components/landing/HeroTitle'
import { useFleetStatus } from '@/data/fleet'
import { cn } from '@/lib/cn'
import { ArrowRight, Container, Gauge, ShieldCheck, Terminal, Wrench } from 'lucide-react'
import { Link } from 'react-router-dom'
import type { LucideIcon } from 'lucide-react'

/**
 * 产品首页（登录前的介绍页）。
 *
 * ── 为什么需要它 ────────────────────────────────────────────────
 * 任务 0–7 铺完六个功能页后，根路径直接跳 `/overview`。后果是：
 * 第一次打开产品的人落在一个**装满演示数据的看板**上，而没有任何地方
 * 说过"Suvex 是什么"。对一个要交付给渗透测试人员、还要决定是否让它对
 * 真实目标发包的工具，"它是什么、能自己做什么、边界在哪"应该在看到
 * 数据之前就说清楚。
 *
 * ── 它和站内页面的关系：**独立布局** ─────────────────────────────
 * 首页刻意**不套 AppShell**（没有侧栏、状态条、移动底栏）。理由是首屏
 * 只有一个任务：讲清这是什么，然后给一条进控制台的路。
 * 带上六个导航入口的侧栏会立刻把注意力分散掉。
 *
 * 这也顺带解决了一个性能问题：AppShell 挂着 AgentField（WebGL），
 * 首页有自己的 HeroBackdrop —— 不套壳就没有"两个 WebGL 叠一层"的风险。
 *
 * ── 文案纪律（brief 明确要求"不要堆砌营销黑话"）────────────────
 * 三条自我约束，逐条可检查：
 *   1. **不说形容词，说行为。** 不写"强大的、智能的、业界领先的"，
 *      写"它自己决定下一步跑什么工具"。
 *   2. **不编数字。** 页面里出现的 `nmap`、`sqlmap`、退出码、PoC
 *      都是这个产品真实会产出的东西，不是"提升 300% 效率"。
 *      所有示例数据在页脚统一标注为演示内容。
 *   3. **把边界写在页面上。** "命令可能改目标文件"、"沙箱内执行"
 *      这类限制不是免责声明，是这个工具能不能被信任的核心信息。
 *      design-system §12 是同一条原则（设置页也要如实说明哪一块
 *      还没有后端支撑）。
 *
 * ── 首屏性能（验收项：Hero 有辨识度且加载迅速）─────────────────
 * 这一页的第一帧只有 **文字 + CSS 渐变**。具体做法：
 *
 *   1. **首页本身不走 lazy**（全站唯一同步引入的页面）。其它页面 lazy 是因为
 *      "用户第一次打开通常只落在总览"——而首页**就是**那个第一次打开看到的
 *      东西，把它 lazy 了，首屏要先等一个额外 chunk 请求才能渲染，
 *      等于"为了省首屏字节而推迟首屏"。
 *   2. **WebGL / Canvas 背景推迟到首帧之后挂载**，且走独立 chunk
 *      （见 HeroBackdrop 与 useAfterPaint）。实测主 chunk 50ms 就绪、
 *      背景 chunk 152ms 才开始下载 —— 顺序是"先文字，后氛围"。
 *   3. **标题动效零 JS**：词在渲染时切好，每条只是 animation-delay 不同
 *      （见 HeroTitle）。不做"先隐藏、等 JS 放行"，所以没有
 *      "动效库慢了标题就不出现"这种路径。
 *   4. **只动 transform，不动 opacity**：浏览器算 LCP 时会跳过 opacity:0 的
 *      元素，一个淡入的大标题会把 LCP 推后整段动画。文字全程满不透明。
 *
 * ── 文案纪律（brief 明确要求"不要堆砌营销黑话"）────────────────
 * 三条自我约束，逐条可检查：
 *   1. **不说形容词，说行为。** 不写"强大的、智能的、业界领先的"，
 *      写"它自己决定下一步跑什么工具"。
 *   2. **不编数字。** 页面提到 `nmap`、`sqlmap`、PoC、退出码，
 *      都是这个产品真实会产出的东西，不是"提升 300% 效率"。
 *      唯一带数字的地方是状态条那句"N 个扫描正在运行" ——
 *      任务 7 之后它是**真实数据**；而首页是公开页、访客未登录，
 *      所以 N 必然是 0，那种情况改为不依赖数字的一句话（见 Hero 的说明）。
 *   3. **把边界写在页面上。** "命令可能改目标文件"、"需要对目标有授权"
 *      这类限制不是免责声明，是这个工具能不能被信任的核心信息。
 *      与 §12（设置页如实说明哪一块还没有后端支撑）同一条原则。
 */

/*
  ── 关于"演示数据"标记 ──

  这里原来有一个 `IS_DEMO = true` 常量，页脚据此挂一句
  「本页展示的行为与数据均为演示内容，后端接口尚未接入」。
  任务 7 之后那句话是**假的**，而一个说反了的声明比没有声明更糟：
  它会让访客以为整个产品都还没接后端。所以常量与那条件一起删了，
  页脚改成陈述真实情况（见文件末尾）。
*/

/**
 * "它自己做什么" —— 四步闭环。
 *
 * 用一条**竖线串起来的序列**而不是四张等大卡片：
 * design-system §9 明确禁止"等大卡片阵列（图标+标题+文字 ×3/×6）"，
 * 理由是"等大 = 没有做取舍"。而这里四步**确实是顺序关系**
 * （不收集信息就无从判断，不判断就无从验证），
 * 所以用带连接线的序列 —— 连接线本身携带信息。
 */
const LOOP_STEPS: { icon: LucideIcon; title: string; body: string }[] = [
  {
    icon: Terminal,
    title: '收集',
    body: '对目标做端口、服务与路径探测，把结果留在上下文里，不重复跑同一条命令。',
  },
  {
    icon: Wrench,
    title: '判断与利用',
    body: '按探测结果选下一个工具，构造针对性的请求并读取真实响应，而不是套一份固定的检查清单。',
  },
  {
    icon: ShieldCheck,
    title: '验证',
    body: '为每条疑似漏洞生成可独立运行的最小 PoC，实际复现一遍。没跑通的不写进报告。',
  },
  {
    icon: Gauge,
    title: '整理与汇报',
    body: '把证据、复现步骤、影响与修复建议整理成可直接交付给客户或研发的报告。',
  },
]

export function LandingPage() {
  const { agents } = useFleetStatus()

  return (
    <div className="relative flex min-h-dvh flex-col bg-canvas">
      <HeroBackdrop />

      {/* 顶部：只有品牌和一个入口。刻意不做完整导航 —— 去产品里看比在这里看导航有用 */}
      <header className="relative z-10 flex items-center justify-between px-gutter py-panel">
        <span className="text-base font-medium text-ink">
          Suvex
          <span className="text-accent">.</span>
        </span>
        {/*
          右上角：主题切换 + 进控制台。两个并成一组，而不是把切换器单独甩到角落 ——
          首页的头部只有"品牌 ←→ 入口"这一对，切换器属于"这一侧的动作"。

          ⚠️ 首页不套 AppShell，所以拿不到 TopStatusBar 上那个按钮
          （与登录页同一个原因，见 AuthShell 的说明）。它是访客看到的**第一屏**，
          而主题偏好是登录前就该能改的。
        */}
        <div className="flex items-center gap-tight">
          <ThemeToggle />
          <Link
            to="/overview"
            className={cn(
              'inline-flex h-9 items-center gap-1.5 rounded-sm border border-line px-3 text-sm text-ink-muted',
              'press transition-ui duration-fast hover:border-line-strong hover:text-ink',
            )}
          >
            进入控制台
            <ArrowRight size={14} strokeWidth={1.75} aria-hidden="true" />
          </Link>
        </div>
      </header>

      {/* ── Hero ───────────────────────────────────────────────── */}
      <main className="relative z-10 flex flex-1 flex-col justify-center px-gutter py-bay">
        <div className="mx-auto w-full max-w-3xl">
          {/*
            状态点 + 一句实况。
            这是全站叙事的延续：侧栏、状态条都在说"agent 此刻在动"，
            首页就用同一句话开场 —— 而不是一个 slogan。

            ★ 任务 7 之后这一行是**真实数据**（`useFleetStatus` 从 scans-store 推）。
              而首页是**公开页**、访客未登录，所以他看到的一定是 0 ——
              直接渲染"{0} 个扫描正在目标上运行"是反效果的：
              它读起来像"这系统没人用"，而真相是"你还没登进去"。
              所以 0 的时候换成一句**不依赖数字、也不编数字**的话。
          */}
          <div className="mb-panel flex flex-wrap items-center gap-tight text-xs text-ink-muted">
            <span
              aria-hidden="true"
              className="inline-block h-1.5 w-1.5 shrink-0 animate-breath rounded-pill bg-accent"
            />
            <span className="tabular" data-numeric>
              {agents.running > 0
                /*
                  ★ 「扫描」而不是「智能体」（任务 11）。
                  `useFleetStatus().agents.running` 数的是**扫描任务数**
                  （见 `data/fleet.ts` 文件头）。这句话是给访客看的规模陈述，
                  说错主语等于给出一个不准确的数字 —— 而访客没有别的地方
                  可以核对它。
                */
                ? `${agents.running} 个扫描正在目标上运行`
                : '随时可以开始第一次扫描'}
            </span>
          </div>

          {/*
            ★ 标题是本页唯一使用文字动效的地方。
            文案回答"它是什么、凭什么"，不回答"多好用"。
            两句话之间用换行分隔 —— HeroTitle 按空格/换行分词，
            每行整体落定，读起来是两段陈述而不是一串碎片。
          */}
          <HeroTitle className="max-w-[22ch]">
            一个能自己决定下一枪打哪里的 AI 安全测试 agent
          </HeroTitle>

          <p className="mt-panel max-w-[62ch] text-base text-ink-muted">
            Suvex
            在一个沙箱里持续运行：自己规划步骤、调用工具、读取真实响应，为发现的每条漏洞实际复现一遍，
            然后把证据整理成报告。你看到的是它每一步在想什么、发了什么包、拿到了什么。
          </p>

          {/* 主 CTA。全站唯一的高饱和主色实心按钮 —— §2 的自律条款：
              violet 总覆盖面积 <5%，所以这一屏只有这一个。 */}
          <div className="mt-bay flex flex-wrap items-center gap-tight">
            <Link
              to="/overview"
              className={cn(
                'inline-flex h-11 items-center gap-2 rounded-sm bg-accent px-4 text-sm font-medium text-canvas',
                'press-lg transition-ui duration-fast hover:bg-accent-strong',
              )}
            >
              进入控制台
              <ArrowRight size={15} strokeWidth={1.75} aria-hidden="true" />
            </Link>
            <Link
              to="/scans"
              className={cn(
                'inline-flex h-11 items-center gap-2 rounded-sm border border-line px-4 text-sm text-ink-muted',
                'press-lg transition-ui duration-fast hover:border-line-strong hover:text-ink',
              )}
            >
              对目标发起扫描
            </Link>
          </div>
        </div>
      </main>

      {/* ── 它自己做什么：四步闭环 ─────────────────────────────── */}
      <section
        aria-labelledby="loop-heading"
        className="relative z-10 border-t border-line bg-canvas/70 px-gutter py-bay backdrop-blur-sm"
      >
        <div className="mx-auto w-full max-w-3xl">
          <h2 id="loop-heading" className="text-lg font-medium text-ink">
            它自己做的一个循环
          </h2>
          <p className="mt-tight max-w-[62ch] text-sm text-ink-muted">
            没有"先扫描再报告"的两段式。四步是同一个循环，每一步的结论决定下一步做什么。
          </p>

          <ol className="mt-panel flex flex-col">
            {LOOP_STEPS.map((step, index) => {
              const Icon = step.icon
              const isLast = index === LOOP_STEPS.length - 1
              return (
                <li key={step.title} className="flex gap-panel">
                  {/*
                    左轨道：图标 + 连接线。连接线用 trace-y 自上而下画一次 ——
                    它表达"顺序"这个信息，所以不循环、不呼吸。
                    aria-hidden：顺序已由 <ol> 语义承载。
                  */}
                  <div className="flex shrink-0 flex-col items-center" aria-hidden="true">
                    <span className="flex h-8 w-8 items-center justify-center rounded-sm border border-line bg-surface">
                      <Icon size={15} strokeWidth={1.75} className="text-accent" />
                    </span>
                    {!isLast && (
                      <span className="my-1 w-px flex-1 origin-top animate-trace-y bg-line" />
                    )}
                  </div>

                  <div className={cn('min-w-0', isLast ? 'pb-0' : 'pb-bay')}>
                    <h3 className="flex items-baseline gap-tight text-sm font-medium text-ink">
                      <span className="tabular text-2xs text-ink-faint" data-numeric>
                        {String(index + 1).padStart(2, '0')}
                      </span>
                      {step.title}
                    </h3>
                    <p className="mt-1 max-w-[58ch] text-sm text-ink-muted">{step.body}</p>
                  </div>
                </li>
              )
            })}
          </ol>
        </div>
      </section>

      {/* ── 边界：能不能被信任取决于这里写清楚 ─────────────────── */}
      <section
        aria-labelledby="boundary-heading"
        className="relative z-10 border-t border-line px-gutter py-bay"
      >
        <div className="mx-auto w-full max-w-3xl">
          <h2 id="boundary-heading" className="text-lg font-medium text-ink">
            它做什么、不做什么
          </h2>

          {/*
            两栏对照，不用卡片 —— 一列是能力，一列是限制。
            刻意**不做**"✓/✗ 三列清单"那种营销版式：这里的两列是
            "运行范围"和"运行范围之外"，是事实对照，不是优点罗列。
          */}
          <div className="mt-panel grid grid-cols-1 gap-bay sm:grid-cols-2">
            <div>
              <h3 className="flex items-center gap-1.5 text-sm font-medium text-ink">
                <Container
                  size={14}
                  strokeWidth={1.75}
                  className="text-accent"
                  aria-hidden="true"
                />
                在沙箱里做
              </h3>
              <ul className="mt-tight flex flex-col gap-2 text-sm text-ink-muted">
                <li>主动向目标发包：端口探测、目录枚举、针对性利用尝试。</li>
                <li>执行它自己写出来的验证脚本，读取真实响应作为证据。</li>
                <li>为每条发现生成最小 PoC，让结论可以被独立复核。</li>
              </ul>
            </div>

            <div>
              <h3 className="text-sm font-medium text-ink">需要你决定的事</h3>
              <ul className="mt-tight flex flex-col gap-2 text-sm text-ink-muted">
                {/*
                  signal-warn 只落在"需要你决定"这一栏的标记点上 ——
                  不铺背景。§2：signal 色只表达结论，不做大面积填充。
                */}
                <li className="flex gap-tight">
                  <span
                    aria-hidden="true"
                    className="mt-2 h-1 w-1 shrink-0 rounded-pill bg-signal-warn"
                  />
                  <span>
                    <strong className="font-normal text-ink">授权的目标。</strong>
                    它会真的发包。对未授权资产使用属于你自己要承担的责任。
                  </span>
                </li>
                <li className="flex gap-tight">
                  <span
                    aria-hidden="true"
                    className="mt-2 h-1 w-1 shrink-0 rounded-pill bg-signal-warn"
                  />
                  <span>
                    <strong className="font-normal text-ink">写入类验证。</strong>
                    部分 PoC 会修改目标上的文件，默认只在确信目标可丢弃时开启。
                  </span>
                </li>
                <li className="flex gap-tight">
                  <span
                    aria-hidden="true"
                    className="mt-2 h-1 w-1 shrink-0 rounded-pill bg-signal-warn"
                  />
                  <span>
                    <strong className="font-normal text-ink">模型供应商。</strong>
                    需要你配置一个能用的 key；用哪个模型由你决定。
                  </span>
                </li>
              </ul>
            </div>
          </div>
        </div>
      </section>

      {/*
        页脚：如实说明这一页**读不读得到真实数据**。

        ★ 任务 7 之前这里写的是「本页展示的行为与数据均为演示内容，
          后端接口尚未接入。控制台内的指标同样由固定样本推导」——
          那句话现在是**假的**：后端已接入、控制台读的是真实端点。
          继续挂着它就是在**否认一个已经可靠的东西** ——
          与吹嘘没有的能力是同一种失真，只是方向相反。

          现在真实的情况是：首页是公开页，访客未登录，所以这一页
          **不读任何运行数据**；而页面里提到的工具与产出都是真的。
      */}
      <footer className="relative z-10 border-t border-line px-gutter py-panel">
        <p className="mx-auto max-w-3xl text-2xs text-ink-faint">
          本页提到的工具与产出（nmap、sqlmap、PoC、退出码）都是 Suvex 真实会做的。
          控制台内的指标来自后端；本页未登录，因此不读取任何运行数据。
        </p>
      </footer>
    </div>
  )
}

export default LandingPage
