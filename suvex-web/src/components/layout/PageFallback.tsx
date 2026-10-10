/**
 * 路由懒加载的兜底。
 *
 * ★ 为什么它必须透传高度（和 PageTransition 同一条 flex 链）：
 *
 *   「智能体活动」是 fillsViewport 页面 —— AppShell 锁死 h-dvh，
 *   高度靠 `flex-1 min-h-0` 一路传到页面内部的滚动区。
 *   如果这个兜底块是个普通 div（display:block、不参与 flex 伸缩），
 *   它会把那条链**掐断**：加载完成后的瞬间，内部滚动区拿不到约束，
 *   clientHeight 会等于 scrollHeight，于是"不在底部"这个状态永远不出现 ——
 *   自动跟随 / 暂停 / 回到最新 全部静默失效，而表面上完全看不出问题。
 *
 *   这个 bug 在本项目已经因为同样的原因（PageTransition 用 min-h-dvh、
 *   包裹层 display:block）发生过两次。所以兜底块的类名不是随便写的：
 *   `flex min-h-0 flex-1 flex-col` 必须和 PageTransition 完全一致。
 *
 * 视觉上刻意做成**静止的骨架**而不是转圈：
 *   本地 chunk 加载通常只有几十毫秒，转圈会一闪而过，比不动更刺眼。
 *   骨架在中长延迟下才是"内容正在来"的正确信号。
 *
 * ★ 任务 8 改：`animate-pulse`（Tailwind 默认）→ `animate-skeleton`。
 *   默认 pulse 是 opacity 1→0.5，在近黑底上骨架块几乎消失，
 *   读起来像"页面在闪"而不是"内容正在来"；而且它的曲线与时长
 *   都不在设计系统里（动效一致性检查要的就是这种游离值）。
 *   见 tailwind.config.js 的 skeleton keyframes。
 */
export function PageFallback() {
  return (
    <div
      className="flex min-h-0 flex-1 flex-col px-gutter py-bay"
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <span className="sr-only">正在加载页面</span>
      {/* 页头占位：和 PageHeader 的高度节奏对齐，避免加载完成后内容整体跳动 */}
      <div className="mb-bay">
        <div className="h-6 w-32 animate-skeleton rounded-xs bg-surface-raised" />
        <div className="mt-tight h-4 w-72 max-w-full animate-skeleton rounded-xs bg-surface" />
      </div>
      <div className="flex flex-col gap-tight">
        <div className="h-16 animate-skeleton rounded-md bg-surface" />
        <div className="h-16 animate-skeleton rounded-md bg-surface" />
        <div className="h-16 animate-skeleton rounded-md bg-surface" />
      </div>
    </div>
  )
}

export default PageFallback
