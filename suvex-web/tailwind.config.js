import defaultTheme from 'tailwindcss/defaultTheme'

/**
 * Suvex · design tokens
 * 决策与理由见 src/design-system.md，改 token 前先读它。
 *
 * 分层约定：
 *   1. 原始色阶（accent / signal）  —— 只能改这里，不要在组件里写死 hex
 *   2. 语义层（canvas/surface/ink/line）—— 组件只用这一层
 *   3. 组件里禁止出现裸色值
 */

/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      /*
        ─────────────────────────────────────────────────────────────
        语义色**全部走 CSS 变量**，两套值定义在 `src/index.css`
        的 `:root`（暗色）与 `[data-theme="light"]`（亮色）里。
        ─────────────────────────────────────────────────────────────

        ## 为什么改成变量

        原来这里写死 hex，于是"换主题"等于改这个文件 —— 而组件层的类名
        本来就是语义的（`bg-surface` / `text-ink-muted`），它们**一行都不用改**。
        全站实测只有 8 处显式依赖明度阶梯，改动面很小。

        ## ★★ 值必须是 `rgb(var(--x) / <alpha-value>)` 这个形状

        不能写成 `var(--suvex-surface)`。**两种写法的差别是致命的，而且不报错**：

            aaa: 'var(--aaa)'                          →  bg-aaa/50 **整条规则消失**
            bbb: 'rgb(var(--bbb) / <alpha-value>)'     →  rgb(var(--bbb) / 0.5)  ✅

        这是实机跑 `tailwindcss` CLI 验过的（tailwindcss 3.4.19）。
        透明度修饰符在 v3 里靠 `<alpha-value>` 占位符注入，普通 `var()`
        无法参与这个替换 —— 而 Tailwind **不会给任何警告**，只是不生成那条类。
        症状是：`bg-surface-raised/50`、`bg-accent/[0.07]` 这些 hover 与衬底
        在浏览器里**静默失效**，看起来像"设计就是这样"。
        所以 `--suvex-*` 里存的是 **RGB 通道三元组**（`20 23 30`），不是 hex。

        ## 两套值在哪

        `src/index.css`：`:root` = 暗色（**保持现状**），`[data-theme="light"]` = 亮色。
        `:root` 放暗色是刻意的兜底方向 —— JS 万一没跑起来，界面退化成今天的样子，
        而不是先闪一屏白。
      */
      colors: {
        // ── 语义表面层 ──
        canvas: 'rgb(var(--suvex-canvas) / <alpha-value>)', // 画布底：页面最深处
        surface: {
          DEFAULT: 'rgb(var(--suvex-surface) / <alpha-value>)', // 默认面板
          raised: 'rgb(var(--suvex-surface-raised) / <alpha-value>)', // 抬升（卡片、悬浮行）
          overlay: 'rgb(var(--suvex-surface-overlay) / <alpha-value>)', // 浮层（弹层、下拉）
          sunken: 'rgb(var(--suvex-surface-sunken) / <alpha-value>)', // 下沉（代码块、日志井）
        },

        // ── 描边：深色界面里层级靠 1px 线，不靠阴影 ──
        //
        // 📌「不靠阴影」这条在亮色下**同样成立**：`shadow-*` 那几档在配置里
        //    定义了但全站 0 处使用。亮色主题因此不需要一套阴影体系 ——
        //    层级继续由 1px 线与极轻的表面差表达。这是刻意的，不是漏做：
        //    给亮色另配一套阴影正是最容易做"换皮"的地方。
        line: {
          DEFAULT: 'rgb(var(--suvex-line) / <alpha-value>)', // 常规分隔
          strong: 'rgb(var(--suvex-line-strong) / <alpha-value>)', // 需要读出来的边界
          accent: 'rgb(var(--suvex-line-accent) / <alpha-value>)', // 强调态边界
        },

        // ── 文字：四级，别再多 ──
        //
        // ★ 三级文字（faint）的对比度必须按它可能出现的**最差承载面**验，
        //   不是按默认面板验 —— 两个主题里"最差"是**相反**的那一端：
        //     · 暗色：最亮承载面 surface-overlay。原来取 #7A808C 对 surface
        //       有 4.8:1 达标，落到 overlay 上只剩 4.21:1，挂了。
        //     · 亮色：最**暗**承载面 surface-sunken。照搬暗色的 #6B7280
        //       只有 4.23:1，同样挂。现在取 #616773（对四个面 4.98–5.68:1）。
        //   同一个 token 在不同容器里时达标时不达标，就是这条没做。
        ink: {
          DEFAULT: 'rgb(var(--suvex-ink) / <alpha-value>)', // 主文
          muted: 'rgb(var(--suvex-ink-muted) / <alpha-value>)', // 次要
          faint: 'rgb(var(--suvex-ink-faint) / <alpha-value>)', // 三级 / 占位
          ghost: 'rgb(var(--suvex-ink-ghost) / <alpha-value>)', // 仅装饰 / 禁用，禁止承载信息
        },

        // ── 主色：violet ──
        //
        // ⚠️ 注意 `strong` 这个名字（原来叫 `bright`）。
        //
        // 「亮色主题里比 base **更深**」这件事让 `bright` 变成了一个**错名** ——
        // 它在暗色下确实更亮，在亮色下反而更深。而 `line-strong` 早就确立了
        // "strong = 更醒目的那个"这个跨主题成立的口径，所以这里跟着改。
        // 语义中性是这类 token 名的唯一要求：名字描述**作用**，不描述**明度**。
        //
        // （`ink-faint` / `ink-ghost` / `accent-deep` 不用改：它们描述的是相对的
        //   强弱远近，两个主题下都成立。）
        accent: {
          DEFAULT: 'rgb(var(--suvex-accent) / <alpha-value>)',
          strong: 'rgb(var(--suvex-accent-strong) / <alpha-value>)', // hover / 强调
          deep: 'rgb(var(--suvex-accent-deep) / <alpha-value>)', // 按下 / 实心按钮底
          dim: 'rgb(var(--suvex-accent-dim) / <alpha-value>)', // 低饱和衬底
          wash: 'rgb(var(--suvex-accent-wash) / <alpha-value>)', // 极低对比衬底（选中行、tag 底）
        },

        // ── 语义信号：只用于「结论」，不用于装饰。刻意保留绿/琥珀，因为
        //    漏洞严重度是行业既有语言，改掉反而增加阅读成本 ──
        //
        // ⚠️ 亮色下这四个都**不能照搬**：它们是按暗底选的"发光的"色，
        //    放到白底上对比度全部腰斩。亮色那套（#127A4B / #9A5B00 /
        //    #B02A1E / #1F55B0）对白实测 5.37 / 5.43 / 6.57 / 7.04:1 ——
        //    全部够当**文字**用，不只是够当圆点。
        signal: {
          ok: 'rgb(var(--suvex-signal-ok) / <alpha-value>)', // 已确认 / 通过
          warn: 'rgb(var(--suvex-signal-warn) / <alpha-value>)', // 待确认 / 中危
          risk: 'rgb(var(--suvex-signal-risk) / <alpha-value>)', // 高危 / 失败
          info: 'rgb(var(--suvex-signal-info) / <alpha-value>)', // 中性提示
        },

        // ── 语法高亮：**独立于 signal**。
        //
        //    为什么不让代码块直接复用 signal 色？因为两件事的目标正好相反：
        //      · signal 是为「状态语义」选的，要求彼此**语义**可辨（绿=通过，琥珀=待确认）；
        //      · 语法色是为「连续文本里的快速分词」选的，要求彼此**视觉**可辨。
        //    早先直接复用就踩了坑：string 借 signal-ok、number 借 signal-warn，
        //    亮度比只有 1.10 —— 对红绿色盲（约 6% 男性）两者都往黄灰塌陷。
        //
        //    ★ 这组值按两条约束解出来，**两个主题各解一遍**（底色不同）：
        //      1. 每个色对**各自主题的 sunken** ≥ 4.9:1；
        //      2. 语义上最易混的对要拉开亮度比 —— number↔string、
        //         function↔property（两个蓝）。
        //    实测：暗色 1.80 / 1.79（对 #0A0B0F），亮色 1.77 / 1.73（对 #EEF0F3）。
        //
        //    ⚠️ 亮色那套是**重新解**的，不是把暗色调亮：浅底上"≥4.9:1"把可用亮度
        //       压到 0.138 以下的一个窄带里，只调亮度会让 number 与 string
        //       的亮度比掉到 1.05 —— 正是这条约束要防的事。
        syntax: {
          plain: 'rgb(var(--suvex-syntax-plain) / <alpha-value>)',
          comment: 'rgb(var(--suvex-syntax-comment) / <alpha-value>)',
          keyword: 'rgb(var(--suvex-syntax-keyword) / <alpha-value>)',
          string: 'rgb(var(--suvex-syntax-string) / <alpha-value>)',
          number: 'rgb(var(--suvex-syntax-number) / <alpha-value>)',
          function: 'rgb(var(--suvex-syntax-function) / <alpha-value>)',
          property: 'rgb(var(--suvex-syntax-property) / <alpha-value>)',
          variable: 'rgb(var(--suvex-syntax-variable) / <alpha-value>)',
          operator: 'rgb(var(--suvex-syntax-operator) / <alpha-value>)',
        },
      },

      fontFamily: {
        // 拉丁用 Geist，中文回退到 Noto Sans SC。
        // 顺序很重要：Geist 放前面，数字/ID/代码标识符走几何无衬线；
        // 汉字不在 Geist 里，会自然落到 Noto Sans SC。
        // 两个都是自托管可变字体，不走 CDN（可能在内网环境跑）。
        sans: ['Geist Variable', 'Noto Sans SC Variable', ...defaultTheme.fontFamily.sans],
        // 等宽同理：JetBrains Mono 没有汉字，中文回退到 Noto Sans SC
        mono: ['JetBrains Mono Variable', 'Noto Sans SC Variable', ...defaultTheme.fontFamily.mono],
      },

      fontSize: {
        // [size, { lineHeight, letterSpacing }]
        //
        // ⚠️ 这套字阶是给中文调的，和拉丁文的惯例相反：
        //
        //   1. **负字距全部去掉。** 拉丁大字收紧字距是光学补偿，因为字母间本来
        //      有留白；汉字是满宽方块，收紧会让笔画相互碰撞。原来 4xl 的
        //      -0.035em 在中文标题上会明显挤在一起。
        //   2. **行高比拉丁文更松。** 汉字没有 x-height 的下伸空间，行距不足时
        //      上下两行会糊成一片。拉丁 1.5 倍可读，中文要 1.7 倍以上。
        //   3. 小字号给一点点正字距（0.01–0.02em），让密集的小字透气。
        '2xs': ['0.6875rem', { lineHeight: '1.125rem', letterSpacing: '0.02em' }],
        xs: ['0.75rem', { lineHeight: '1.25rem', letterSpacing: '0.01em' }],
        sm: ['0.8125rem', { lineHeight: '1.375rem', letterSpacing: '0.01em' }],
        base: ['0.875rem', { lineHeight: '1.5625rem' }],
        lg: ['1rem', { lineHeight: '1.75rem' }],
        xl: ['1.25rem', { lineHeight: '1.875rem' }],
        '2xl': ['1.5rem', { lineHeight: '2.25rem' }],
        '3xl': ['2rem', { lineHeight: '2.75rem' }],
        '4xl': ['2.75rem', { lineHeight: '3.5rem' }],
      },

      spacing: {
        // 4px 基准的语义别名：窄容器内距 / 组间距 / 页面留白 三档就够
        tight: '0.5rem', // 8  — 组内
        panel: '1rem', // 16 — 面板内距
        gutter: '1.5rem', // 24 — 页面边距
        bay: '2.5rem', // 40 — 区块之间
        rail: '3.5rem', // 56 — 侧栏宽
      },

      borderRadius: {
        // 小半径：工程感，不是消费级 App。pill 只给状态点用
        xs: '2px',
        sm: '4px',
        DEFAULT: '6px',
        md: '8px',
        lg: '12px',
        pill: '999px',
      },

      boxShadow: {
        // 深色 UI 的层级主要靠 line + surface，阴影只负责「离开平面」
        // 一律带 y 偏移 + 柔和扩散，不做零偏移光晕
        panel: '0 1px 2px rgba(0,0,0,0.5), 0 8px 24px -14px rgba(0,0,0,0.75)',
        lift: '0 2px 4px rgba(0,0,0,0.5), 0 16px 40px -20px rgba(0,0,0,0.85)',
        overlay: '0 4px 8px rgba(0,0,0,0.55), 0 24px 64px -28px rgba(0,0,0,0.9)',
        well: 'inset 0 1px 0 rgba(255,255,255,0.03)', // 下沉井的顶边反光
      },

      /*
        ── 动效属性集 ──────────────────────────────────────────────
        `transition-ui` 是**交互反馈的默认属性集**：颜色、透明度、阴影、transform。
        刻意不含 width / height / margin / padding 这些会触发布局的属性 ——
        布局动画会连带 reflow，在高频交互的列表里是实打实的掉帧来源。
        需要布局动画时显式写 `transition-[width]`（全站只有侧栏折叠一处）。

        它替掉了两种写法：
          · `transition-colors`  —— 加不了 transform 的按压缩放/位移反馈；
          · `transition-all`     —— 顺手把布局属性也带上了，是掉帧的常见来源。
        见 design-system.md §5.1。
      */
      transitionProperty: {
        ui: 'color, background-color, border-color, text-decoration-color, fill, stroke, opacity, box-shadow, transform',
      },

      transitionTimingFunction: {
        // 唯一入场曲线：指数减速。名字叫 enter 不是"进入视口"，
        // 而是"从已可见的初态收敛到终态" —— 所以它同时用在入场和 settle 上。
        enter: 'cubic-bezier(0.16, 1, 0.3, 1)',
        // 出场 / 收起。与 enter 成对，保证"出现慢、消失快"的方向感一致。
        exit: 'cubic-bezier(0.4, 0, 1, 1)',
        // 状态切换（选中/开关/展开）。对称曲线 —— 状态变化没有方向，
        // 用 enter 那种强减速会让"切回去"显得比"切过去"迟钝。
        state: 'cubic-bezier(0.32, 0, 0.24, 1)',
      },

      transitionDuration: {
        // 四档，各有明确职责（见 design-system.md §5.1 的对照表）。
        // instant 是按压反馈：必须比"感知到点击"更快，否则像卡了一下。
        instant: '80ms', // 按压 / hover 的即时确认
        fast: '120ms', // 颜色、透明度、图标旋转
        base: '200ms', // 状态切换、尺寸、数据条
        slow: '420ms', // **仅**背景层首帧淡入。别用在交互元素上
      },

      keyframes: {
        'stream-in': {
          // 位移只走 4px、时长 240ms —— 这是高频刷新的流，不是每次进一个新区块。
          // 位移再大或时长再长，一屏里同时有几条在动就会晕。
          from: { opacity: '0', transform: 'translateY(4px)' },
          to: { opacity: '1', transform: 'none' },
        },
        breath: {
          '0%, 100%': { opacity: '0.35' },
          '50%': { opacity: '0.85' },
        },
        // 抽屉入场。这是全站**唯一**带方向感的入场动效 ——
        // 位移表达了"从右边来"，这本身就是信息（用户知道怎么关掉它）。
        // 其它地方一律只用淡入 + 极小位移，不表达方向。
        'drawer-in': {
          from: { opacity: '0', transform: 'translateX(16px)' },
          to: { opacity: '1', transform: 'none' },
        },
        // 批量操作栏入场：从下方升起，提示"它是因为你的操作才出现的"
        'rise-in': {
          from: { opacity: '0', transform: 'translateY(8px)' },
          to: { opacity: '1', transform: 'none' },
        },
        // 路由切换。**纯 CSS**，不再依赖 gsap/ScrollTrigger。
        //
        // ★ 只动 opacity 是错的（任务 8 复核修正）——
        //   先说明为什么曾经这么写，再说明为什么改掉：
        //
        //   原来只有透明度，理由是"切页面时用户注意力已经在新内容上，
        //   再叠位移会让换页变成一件要等的事"。这个理由本身成立，
        //   但它忽略了一件事：**`from { opacity: 0 }` 是一个"不可见的初态"**。
        //   而实测（隐藏标签页冷启动）：
        //     · `document.timeline.currentTime` **恒为 0**；
        //     · 动画停在 from 态，计算 opacity = **0**，`playState` 却报 running；
        //     · 于是**整页不可见** —— 内容在 DOM 里（实测 innerText 2363 字符），
        //       但一个像素都不显示。
        //   这正是本项目反复栽过的那个坑：**初态不可见 + 依赖调度器放行**。
        //
        //   改成"只动 transform"之后，时间轴即便冻结，元素也只是停在
        //   一个 6px 的位移上 —— **可见性完全不依赖动画是否推进**。
        //   6px 是克制过的值：够读成"内容落位了"，又不会让页面显得在晃。
        //
        //   本条与 HeroTitle 用的是同一条纪律，见 design-system §5.1
        //   的"动效不能挡住功能"第 4 条。
        'page-in': {
          from: { transform: 'translate3d(0, 6px, 0)' },
          to: { transform: 'none' },
        },
        // 微反馈：选中 / 成功 / 新出现的勾。从略小收到刚好，
        // 读起来是"落定"，而不是"弹出来"（弹跳是消费级 App 的语言，§4）。
        'settle-in': {
          from: { opacity: '0', transform: 'scale(0.86)' },
          to: { opacity: '1', transform: 'none' },
        },
        // 状态切换的一次性高亮：底色提亮后回落到透明。
        // 用于"这一行刚被你改过"——它是一次性提示，不是常驻状态色。
        'flash-accent': {
          from: { backgroundColor: 'rgba(139, 124, 246, 0.16)' },
          to: { backgroundColor: 'rgba(139, 124, 246, 0)' },
        },
        // 首页行为流的连接线：自上而下画一次然后停住。
        // 它画的是"agent 在往下推进"，因此**不循环** ——
        // 循环流转的装饰线会持续抢注意力，而这里要的是"已经走完了"这个事实。
        'trace-y': {
          from: { transform: 'scaleY(0)' },
          to: { transform: 'scaleY(1)' },
        },
        // 骨架屏的呼吸。**替掉 Tailwind 默认的 animate-pulse** ——
        // 后者是 opacity 1→0.5，在近黑底上会让骨架块几乎消失，
        // 读起来像"页面在闪"而不是"内容正在来"。这里收窄到 0.55→1，
        // 并且用 state 曲线（对称）—— 骨架是持续状态，没有方向。
        // 首页 Hero 标题的逐词落定。
        //
        // ★ 只动 transform，**不动 opacity** —— 这是刻意的，两个原因：
        //
        //   1. **LCP/FCP。** 浏览器计算 LCP 时会**跳过 opacity: 0 的元素**。
        //      一个"从透明淡入"的大标题会把 LCP 推后整整一段动画时长，
        //      而验收明确要求"首屏动效不能拖慢 FCP"。文字全程满不透明，
        //      首帧就已经是最终像素，任何指标都不受影响。
        //   2. **零失败模式。** 文字默认态就是可见的（类名里没有 opacity-0），
        //      CSS 万一没加载 / 动画被禁用 / 时间轴被节流，文字都完整在。
        //      "先隐藏、等某物放行"的那种设计在这里被彻底排除。
        //
        //   位移取 0.22em（约 4px @ 18px / 13px @ 60px）：跟随字号缩放，
        //   大标题上不会显得小气，小屏上也不会晃。再大就变成"文字在飞"。
        'word-settle': {
          from: { transform: 'translate3d(0, 0.22em, 0)' },
          to: { transform: 'none' },
        },
        // 装饰层的淡入。**只给"装饰"用，绝不给内容用。**
        //
        // 它动的是 opacity，也就是"不可见的初态"。按 §5.1 的纪律，
        // 内容绝不能用这种写法（实测：时间轴冻结时计算 opacity 为 0，
        // 整层不可见）。但装饰层可以用，理由是**去掉它不丢任何信息**：
        // 背景晚出现、或不出现，用户该读到的文字一个都不少。
        //
        // 当前唯一的使用者是 HeroBackdrop（首页背景的迟到淡入）。
        // 判断标准：如果这一层消失了，用户会不会漏掉信息？
        // 会 → 用 `page-in`（只动 transform）；不会 → 才可以用这个。
        'fade-in': {
          from: { opacity: '0' },
          to: { opacity: '1' },
        },
        skeleton: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.55' },
        },
      },

      animation: {
        'stream-in': 'stream-in 240ms cubic-bezier(0.16, 1, 0.3, 1) both',
        breath: 'breath 2.6s ease-in-out infinite',
        'drawer-in': 'drawer-in 200ms cubic-bezier(0.16, 1, 0.3, 1) both',
        'rise-in': 'rise-in 200ms cubic-bezier(0.16, 1, 0.3, 1) both',
        /*
          ⚠️ 这一条**刻意不带 `both`**（对比其它几个动画），原因是实测的：
          `fill: both` 会把最后一个关键帧一直"应用"着，于是动画结束后
          `getComputedStyle(el).transform` 返回 `matrix(1, 0, 0, 1, 0, 0)` ——
          一个**恒等矩阵**。而 spec 规定：只要 transform 不是 `none`，
          元素就成为 `position: fixed` 后代的**包含块**。
          旧版 gsap 正是栽在这上面（见 PageTransition 的注释）。

          去掉 `both` 后：动画结束即不再有 transform，包含块随之消失。
          而它本来也不需要 `both` —— `both` 的作用是让"延迟期间"保持 from 态，
          这条动画没有 delay，所以 `both` 只带来了副作用。
        */
        'page-in': 'page-in 140ms cubic-bezier(0.16, 1, 0.3, 1)',
        'fade-in': 'fade-in 420ms cubic-bezier(0.16, 1, 0.3, 1) both',
        'settle-in': 'settle-in 160ms cubic-bezier(0.16, 1, 0.3, 1) both',
        'flash-accent': 'flash-accent 900ms cubic-bezier(0.16, 1, 0.3, 1) both',
        'trace-y': 'trace-y 700ms cubic-bezier(0.16, 1, 0.3, 1) both',
        skeleton: 'skeleton 1.8s cubic-bezier(0.32, 0, 0.24, 1) infinite',
        // `both` 保留：它让动画在 delay 期间就应用 from 状态（即那个 0.22em 位移），
        // 于是错位的词是"等在下方"而不是"先停在原位再突然跳下去"。
        // 注意这只影响 transform，不影响可见性 —— 文字全程可见。
        'word-settle': 'word-settle 500ms cubic-bezier(0.16, 1, 0.3, 1) both',
      },
    },
  },
  plugins: [],
}
