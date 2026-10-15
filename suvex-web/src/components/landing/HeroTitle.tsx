import { Fragment } from 'react'
import { cn } from '@/lib/cn'

/**
 * Hero 标题的文字动效 —— **按词依次落定**。
 *
 * ════════════════════════════════════════════════════════════════
 *  为什么这里从 gsap 换成了纯 CSS（任务 8 的实测结论）
 * ════════════════════════════════════════════════════════════════
 *
 * 前两版都用 gsap + SplitText 做逐词入场，两版**都在实测中坏掉**，
 * 而且坏的方式逐级难看，值得完整记下来：
 *
 *   **第一版**：`gsap.from(words, {opacity: 0, ...})`，兜底用
 *   `gsap.set(words, {clearProps: 'all'})`。
 *   实测（后台标签页，rAF 全程不触发）：文字冻在飞行途中 ——
 *   opacity `0.657 / 0.449 / 0.169 / 1`，**半透明地停在屏幕上**。
 *   gsap 的清理同样排在 ticker 上，兜底跟着一起冻住，等于没有。
 *
 *   **第二版**：兜底改成纯 DOM `removeAttribute('style')`。
 *   当时清掉了（opacity 立刻回到 1），但**只清掉了那一刻**：
 *   被挂起的 tween 没有死，ticker 恢复后把自己算出的值又写了回去。
 *   实测复现：清理后 opacity=1，稍后再读变成 `0.496 / 0.231 / 0 / 0`。
 *
 *   **第三版**：兜底先 `tween.kill()` 再清样式（顺序是对的，理论上应该成立）。
 *   实测**依然失败**：数秒后 opacity 回到 0，内联样式重新出现
 *   `transform: translate(0%, 35%); opacity: 0;` —— 正是 `gsap.from` 的初态。
 *
 * 三次失败指向同一个结论：**`gsap.from` 的语义就是"先隐藏、等 JS 放行"，
 * 而"等 JS 放行"这件事在 rAF 被挂起时是不可靠的。** 只要动效的**初态是隐藏**、
 * 而显示依赖某个调度器推进，就存在"永远不显示"的路径。
 * 补兜底是在和这个设计对抗，而兜底本身也活在同一个被挂起的调度器里。
 *
 * ── 换成纯 CSS 之后，这个失败模式**在结构上不存在** ────────────
 *
 *   ⚠️ 一条我原本写错、后经实测更正的说法，留在这里以免重犯：
 *
 *   **错的说法**："CSS 动画由文档时间轴驱动，页面不可见时照样在时间轴上完成。"
 *   实测（Chrome，隐藏标签页，`visibilityState === 'hidden'`）：
 *     · `document.timeline.currentTime` **恒为 0**，不推进；
 *     · 新建的 CSS 动画停在 from 态（`opacity: 0`，`playState` 仍是 `running`）；
 *     · rAF 同样完全不触发。
 *   也就是说 **CSS 动画照样会随文档时间轴被挂起** —— 它并不是"免疫"的。
 *
 *   真正让这个方案安全的是下面第 1、3 条，不是"CSS 免疫挂起"：
 *
 *   1. **只动 `transform`，绝不动 `opacity`。**
 *      时间轴冻结时动画停在 from 态，而 from 态只改位移 ——
 *      文字**仍然是满不透明的**，完整可读。
 *      （对照实验：同一个被冻结的动画，`opacity` 版计算值是 `0`（不可见），
 *       `transform` 版是 `1`（可见）。这一条是两种写法唯一的实质差别。）
 *      这也是为什么 §5.1 规定"首屏文字类动效不许动 opacity" ——
 *      它有**两个**好处：不推后 LCP，以及在时间轴挂起时天然安全。
 *   2. **默认态就是可见的。** 类名里没有任何 `opacity: 0` 初态，
 *      动画只是"额外加的一段位移"。CSS 没加载 / 动画被禁用 / 时间轴挂起，
 *      文字都在。
 *   3. **不需要任何 JS 参与。** 词是 React 渲染时切好的，每条只是
 *      `animation-delay` 不同。没有动态 import、没有 SplitText、没有 ticker、
 *      没有兜底定时器 —— 也就没有需要兜底的东西。
 *
 *   对比 gsap 版：那三版的初态都是 `opacity: 0`，且由 ticker 推进 ——
 *   时间轴/rAF 一停，文字就**永远不可见**。这里的差别不在"用不用 CSS"，
 *   而在**"不可见的初态" + "依赖调度器放行"这两件事的组合**。
 *   只要避开这个组合（默认可见 + 只动 transform），用什么技术都安全。
 *
 *   顺带的收益：SplitText（3.3 kB gzip）整块从产物里消失，
 *   首页不再引用 gsap。（gsap 这一整条依赖后来也彻底移除了 —— 它最后一个
 *   使用者 DotGrid 已删，理由是 GreenSock 自有许可非 OSI 开源，见
 *   `effects/README.md` 的「已移除」节。）
 *
 * ── 按"词"而不是按"字符" ───────────────────────────────────────
 * 逐字符是 ReactTitle 类动效的常见做法，但它对**中文是错的**：
 * 汉字是满宽方块，逐字拆开会把一个词在时间轴上切碎，
 * 读起来是"自·主·运·行"四个独立单位 —— 而中文的词才是语义单位。
 * 所以按空格分词，每个词整体落定，读起来是"依次说完两句话"。
 *
 * ── 可访问性 ───────────────────────────────────────────────────
 * 读屏器念的必须是**一整句通顺的话**，而不是被拆开的词。
 * 所以：`<h1 aria-label={完整原文}>`，每个词 `<span aria-hidden="true">`。
 * 这与 SplitText 的 `aria:'auto'` 达到同样的效果，但由我们显式控制。
 *
 * ── 时长 ───────────────────────────────────────────────────────
 * 单词 500ms，错位 60ms，整段约 700ms（4 个词）。
 * 超过一秒的标题动效会让用户"等它演完"，那是动效在收税。
 */
export function HeroTitle({ children, className }: { children: string; className?: string }) {
  /*
    按空格分词。用捕获组保留分隔符会让空字符串混进数组，
    所以直接按空白切、再丢掉空项，词间用真实空格文本节点还原。
  */
  const words = children.split(/\s+/).filter(Boolean)

  return (
    <h1
      // 完整原文给读屏器；视觉上的词各自 aria-hidden，避免被逐词念出
      aria-label={children}
      className={cn(
        'text-balance text-4xl font-medium text-ink sm:text-5xl lg:text-6xl',
        className,
      )}
    >
      {words.map((word, index) => (
        <Fragment key={`${word}-${index}`}>
          <span
            aria-hidden="true"
            /*
              inline-block 是 transform 生效的前提（inline 元素不接受 transform）。
              加了它之后词之间不会自动合并空格，所以下面用显式空格文本节点分隔。
            */
            className="inline-block animate-word-settle"
            style={{ animationDelay: `${index * 60}ms` }}
          >
            {word}
          </span>
          {/* 词间空格。放在 span 外面，不参与动画，也就不会被 transform 影响 */}
          {index < words.length - 1 ? ' ' : null}
        </Fragment>
      ))}
    </h1>
  )
}

export default HeroTitle
