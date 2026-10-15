# effects/ — 第三方动效组件（拷贝件）

这里的每个文件都是 **ReactBits 的源码拷贝**，不是 npm 依赖。按需拷、按需改，
升级时对着上游 commit 做 diff 即可。

来源：<https://reactbits.dev>（仓库 <https://github.com/DavidHDev/react-bits>）
取用日期：2026-02-26

## 许可证

ReactBits 采用 **MIT + Commons Clause v1.0**（`Copyright (c) 2026 David Haz`）。
要点：可以自由使用、修改、分发，**前提是随应用一起分发**；不得把组件本身单独作为
产品售卖（Commons Clause）。本项目是应用内使用，符合条件。分发时需保留版权声明。

## 清单

| 文件               | 上游变体      | 上游路径                                                    | 额外依赖 |
| ------------------ | ------------- | ----------------------------------------------------------- | -------- |
| `Threads.tsx`      | `ts-tailwind` | `src/ts-tailwind/Backgrounds/Threads/Threads.tsx`           | `ogl`    |
| `LetterGlitch.tsx` | `ts-tailwind` | `src/ts-tailwind/Backgrounds/LetterGlitch/LetterGlitch.tsx` | 无       |
| `CountUp.tsx`      | `ts-tailwind` | `src/ts-tailwind/TextAnimations/CountUp/CountUp.tsx`        | `motion` |

每个文件顶部都写了该文件的**逐条本地改动**，改之前先看那里。

### 已移除：`AnimatedContent.tsx`（任务 8）

上游路径 `src/ts-tailwind/Animations/AnimatedContent/AnimatedContent.tsx`。
它原先用在两处：路由切换（`PageTransition`）和向导步骤切换（`NewScanWizard`）。
两处都换成了纯 CSS 的 `animate-page-in`，原因见那两个文件里的注释，核心两条：

1. **它是"先隐藏、等 JS 放行"的** —— 渲染时带 `invisible`，靠 gsap 在 effect 里
   兜回 `visible`。gsap 没跑完，内容就**永远不可见且不报错**。
   在路由切换上是白屏，在向导里是"看不到要填的表单"。
2. **它把 gsap（+ ScrollTrigger）钉在主 chunk 里。** 只要有任何一个静态
   import 指向它，gsap 就进不了独立 chunk —— 构建器会直接报
   `INEFFECTIVE_DYNAMIC_IMPORT`。移除后主 chunk 从 445 kB 降到 312 kB
   （gzip 147 → 100 kB），gsap 只被懒加载的 `AgentField` 依赖。

要恢复它：按上表路径从上游取，并确认新调用点能接受第 1 条的失败模式。

### 已移除：`DotGrid.tsx`

上游路径 `src/ts-tailwind/Backgrounds/DotGrid/DotGrid.tsx`。原先用在两处：
站内 `AgentField`（第 3 层）与首页 `HeroBackdrop`（大屏点阵层），都是懒加载。

移除原因是**许可证**：它是全仓唯一依赖 `gsap` 的地方，而 `gsap@3.15.0` 的
`package.json` 写的是 `Standard "no charge" license` —— GreenSock 自有许可，
**不是 OSI 认可的开源许可**（详见 `DEPENDENCIES.md` §4.3）。它是纯装饰，
去掉不影响任何功能，所以直接移除而不是留着做例外说明。

它带来的两个副作用一并消失：`gsap` 从依赖树里消失（主 chunk 之外的那个
独立 chunk 也不再含 gsap），以及 `src/` 下最后的 3 处类型 `any`（都在它的
节流函数里）**归 0**。

要恢复它：按上游路径取回，并同时把 `gsap` 装回来 —— 恢复即意味着重新接受
那条非 OSI 许可。历史踩坑（它的点击冲击波挂在 `window` 上）仍记在
`design-system.md` §15 的 B1 与下方「两个必须知道的坑」里，供恢复时参考。

## 本项目的用法

`AgentField.tsx`（本目录内，非拷贝件）把三层叠起来当页面背景，见它的注释。
它现在由 `AppShell` **懒加载** —— 背景装饰不该挡在首屏前面。
`CountUp.tsx` 由 `components/overview/StatCard.tsx` 使用（指标数字滚动）。
`HeroBackdrop.tsx`（在 `components/landing/`）用 `Threads` 拼首页背景，
同样是懒加载 + `useAfterPaint` 推迟挂载。
`LetterGlitch.tsx` 目前**没有被挂载**，作为备选保留——它是纯文本噪点，
适合放在"日志正在滚动"的区域做局部氛围，而不是整页背景。

## 两个必须知道的坑

> 第 1 条讲的是 `DotGrid.tsx`，该组件**已移除**（原因见上）。保留它是为了
> 万一恢复时不再踩同一个坑：它的监听挂在 `window` 上，不是挂在自己的 canvas 上。

1. **DotGrid 的鼠标/点击监听挂在 `window` 上，不在自己的 canvas 上。**
   所以整页任意点击都会触发一次冲击波。在带真实交互的页面里
   **必须把 `shockStrength` 设为 `0`**（保留光晕跟随，去掉点击冲击）——
   否则用户点"启动扫描"按钮时背景会炸一下，而且每次点击都要遍历全部点阵
   （约 2000 个点）算距离。任务 8 已给它加了短路：`shockStrength` 为 0 时
   连监听都不注册。原先两处调用点（`AgentField` / `HeroBackdrop`）都传了 0。
2. **Threads 是逐像素 Perlin 噪声，纯 GPU 成本。**
   它自带 `IntersectionObserver` + `document.hidden` 双重门控，离屏/切标签会自动停。
   但**不要**在同一个视口里再叠第二个 WebGL 背景（比如 Aurora/Silk），
   两个重着色器同时跑在集显上会掉帧。
   内部渲染分辨率我们已从上游的 1920 调到 700（实测依据见 `design-system.md` §5）。
