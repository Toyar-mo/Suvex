/**
 * ReactBits · DotGrid  (ts-tailwind 变体)
 * 出处: https://reactbits.dev/backgrounds/dot-grid
 * 源文件: https://github.com/DavidHDev/react-bits/blob/main/src/ts-tailwind/Backgrounds/DotGrid/DotGrid.tsx
 * 取用日期: 2026-02-26 · 许可证见同目录 README.md
 *
 * 本地改动（逐条列出，方便日后与上游 diff 同步）：
 *   1. 删掉 'use client' —— 那是 Next.js 指令，Vite 下是噪音。
 *   2. 去掉上游外层 <section> 的 p-4：那是 demo 的留白，作为全幅背景时会
 *      让 canvas 内缩 16px，边缘露出底色。
 *   3. onMove/onClick 里的 canvasRef.current! 加了空值提前返回 —— 上游用非空
 *      断言，在卸载竞态（组件已卸载但监听器还没摘）时会抛错。
 *   4. 节流函数补了 trailing 调用：上游只保留首个事件，鼠标停下后光晕会停在
 *      50ms 前的位置上，视觉上像卡住。
 *   5. 时间戳与未使用参数不再走短路求值：`a && b()` 改成 if，ESLint 的
 *      no-unused-expressions 会拦，且短路写法在读的时候要多想一秒。
 *   6. `hexToRgb` 同时接受 `#rrggbb` 与 `rgb(r,g,b)`（见该函数上方那段，
 *      这是一个实测出来的真 bug）。
 *   7. ★ **自研物理替代 gsap + InertiaPlugin**（2026-10-16）。上游的
 *      「惯性滑出 + 弹性回位」两个补间改成闭式解 + 半隐式欧拉，
 *      积分放进**本文件已有的那个 rAF 循环**里，不新开循环。
 *      理由与推导见下方「自研物理」那段。
 *   8. 节流函数去掉了 `any` 与 `this` 透传（上游签名是
 *      `(...args: any[])` + `this: any`）。这里只当普通函数用，
 *      泛型版既保住了行为，也让 `src/` 下的显式 `any` 保持 0 处。
 *
 * 依赖：**无**（只依赖 React 与浏览器 API）。
 *
 * ⚠️ 上游用 `gsap` + `gsap/InertiaPlugin` 做物理。本项目**刻意不引入它** ——
 *    gsap 用的是 GreenSock 自有许可（非 OSI 认可的开源许可），
 *    见 `DEPENDENCIES.md` §4.3 与 `effects/README.md`。
 *    下面第 7 条改动就是为此：效果保留，依赖不回来。
 */
import React, { useRef, useEffect, useCallback, useMemo } from 'react';

/*
  ── 本地改动 4：节流补 trailing ────────────────────────────────

  上游只保留首个事件（leading only）。鼠标停下之后，光晕会停在上一次
  通过节流时的位置上 —— 视觉上像"卡住了"，而那 50ms 的差看起来
  像是丢帧。补一个 trailing 调用，事件停下时再补最后一帧。
*/
function throttle<A extends unknown[]>(func: (...args: A) => void, limit: number) {
  let lastCall = 0;
  let trailing: ReturnType<typeof setTimeout> | null = null;
  let lastArgs: A;
  return (...args: A) => {
    const now = performance.now();
    const invoke = () => {
      lastCall = performance.now();
      func(...lastArgs);
    };
    lastArgs = args;
    if (now - lastCall >= limit) {
      if (trailing) {
        clearTimeout(trailing);
        trailing = null;
      }
      invoke();
      return;
    }
    // 事件停下了也要补一帧，否则光晕停在最后一次节流通过的位置
    if (!trailing) {
      trailing = setTimeout(() => {
        trailing = null;
        invoke();
      }, limit - (now - lastCall));
    }
  };
}

/** 一个点阵格。前五个字段与上游逐字一致，`_inertiaApplied` 的语义见下。 */
export interface Dot {
  cx: number;
  cy: number;
  xOffset: number;
  yOffset: number;
  /**
   * 「正在飞就不重复推」的闸门。
   *
   * 上游在**推出去**时置 true，在**弹回结束**时置 false —— 也就是说
   * 从推出去到弹回来这一整段时间里，这个点都不会被再次推动。
   * 少了它，快速划过的鼠标会每一帧给同一个点重新赋值目标，
   * 点会黏在光标上、永远弹不回来。
   */
  _inertiaApplied: boolean;

  /* ── 以下为本地改动 7 新增的物理状态 ── */

  /** 速度（px/s）。仅回弹阶段用（惯性阶段是解析式，不需要速度） */
  vx: number;
  vy: number;
  /** 惯性阶段的目标偏移（推出去要滑到的地方） */
  targetX: number;
  targetY: number;
  phase: 'idle' | 'inertia' | 'return';
  /** 进入当前阶段后经过的秒数（回弹阶段用于兜底超时） */
  phaseT: number;
  /** 回弹开始时记录的幅度 —— 落定判据按它的比例算，于是 returnDuration 保住了原意 */
  returnAmp: number;
}

/*
  ══════════════════════════════════════════════════════════════
  本地改动 7：自研物理（替代 gsap + InertiaPlugin）
  ══════════════════════════════════════════════════════════════

  ## gsap 在这里只做两件事（逐行读过，就这两处）

    ① gsap.to(dot, { inertia: { xOffset: pushX, yOffset: pushY, resistance },
                     onComplete: () => { …② } })
    ② gsap.to(dot, { xOffset: 0, yOffset: 0,
                     duration: returnDuration, ease: 'elastic.out(1,0.75)' })

  外加 `gsap.killTweensOf(dot)`（打断在飞的补间）。
  **渲染主循环、点阵几何、hexToRgb、throttle、buildGrid 全都不依赖 gsap。**

  ## 为什么用闭式解而不是照搬 gsap 的曲线

  gsap 是黑盒时间轴：想回答"多久落定"只能试。而这两件事都有闭式解，
  于是时间是可以**算出来**的 —— 这正是本项目对动效的要求（§5.1：
  动效要能被测，不能靠"看着回来了"）。

  ### ① 惯性滑出 = 指数逼近（不是弹簧）

      x(t) = target + (x₀ - target)·e^{-λt}

  **单调收敛、不振荡** —— 这正是"滑出去、然后停住"的手感；
  用弹簧做这一段会变成"滑过去再荡回来"，与上游手感不同。

  λ 由 `resistance` 推出：λ = resistance / 150。
  调用点传 900 → λ = 6 → 从 175px 滑到 0.5px 约 **0.98s**。
  `resistance` 越大越"涩"、滑得越短 —— 方向与 InertiaPlugin 一致。

  ### ② 弹性回位 = 欠阻尼弹簧

      a = -k·x - c·v ,   k = ω² ,   c = 2ζω

  ζ = **0.35** → 过冲 ≈ 31%（e^{-πζ/√(1-ζ²)} = e^{-1.174}）。
  肉眼可见的"弹一下"，但不夸张。

  ω 由 `returnDuration` 推出，让这个 prop **保住原意**：
  阻尼振动的包络是 e^{-ζωt}，衰减到初始幅度的 1%（ln100 = 4.6）所需
  时间是 4.6/(ζω)。令它等于 `returnDuration`：

      ω = 4.6 / (ζ · returnDuration)

  `returnDuration` = 1.6 → ω ≈ 8.21 → 周期 2π/ω ≈ **0.77s**，
  1.6s 内衰减到 1%。上游的 `elastic.out(1, 0.75)` 同样是
  "几个来回 + 1.6s 落定"，手感对齐。

  ## 三条数值上的保障（要求里的"不许 NaN / 发散 / 飞出去不回来"）

  1. **dt 夹住**：标签页切回来时 `dt` 可能是好几秒，直接积分会炸。
     夹到 1/30s，再按 1/120s 切子步 —— 半隐式欧拉的稳定条件是
     ω·dt ≪ 2，这里 ω/120 ≈ 0.07，余量很大。
  2. **落定判据按幅度比例**，不是绝对值：偏移缩到回弹起始幅度的 1%
     才算完。于是 `returnDuration` 对小推、大推都是同一个时间量级。
  3. **兜底超时**：回弹阶段超过 `4 × returnDuration` 无条件归零并重开闸门。
     这样"点被卡在飞行状态、永远弹不回来"在结构上不可能发生 ——
     即使将来有人把常量改成发散的组合。
     另有非有限值检查（NaN/Infinity 直接复位），双保险。
*/
const INERTIA_LAMBDA_DIVISOR = 150;
/**
 * λ 的下限（1/s）。
 *
 * ⚠️ 这是被测试抓出来的一个真 bug 的修复（2026-10-16）：
 *    最初写的是 `Math.max(1, resistance) / 150`，想的是"非法值给个兜底"，
 *    但 `resistance = 1` → λ = 0.0067，衰减时间常数 **150 秒** ——
 *    惯性阶段实际上永远结束不了，点会停在半路，`_inertiaApplied`
 *    永远不放开，那个点从此再也不会响应鼠标。
 *
 *    正确的兜底是**把 λ 夹在一个能收敛的区间**，而不是夹 resistance 本身。
 *    λ = 1 对应时间常数 1s（最坏情况几秒内滑停），而真实调用点传
 *    resistance 900 → λ = 6，离这个下限很远，不改变实际手感。
 */
const MIN_INERTIA_LAMBDA = 1;
const RETURN_DAMPING_RATIO = 0.35;
const RETURN_SETTLE_LN100 = 4.6;
const RETURN_SETTLE_FRACTION = 0.01;
const INERTIA_DONE_PX = 0.5;
const MAX_STEP_DT = 1 / 30;
const PHYSICS_SUBSTEP = 1 / 120;
const RETURN_HARD_CAP_FACTOR = 4;

/** 由 props 解出的物理参数。`advanceDot` 只吃这个，不读 props。 */
export interface DotPhysicsParams {
  /** 惯性阶段的指数衰减率（1/s） */
  lambda: number;
  /** 回弹弹簧的 ω（rad/s） */
  omega: number;
  /** 整个飞行阶段的兜底超时（s），惯性 + 回弹一起算 */
  hardCap: number;
}

export function resolvePhysicsParams(
  resistance: number,
  returnDuration: number
): DotPhysicsParams {
  const safeDuration = Math.max(0.05, returnDuration);
  const rawLambda =
    (Number.isFinite(resistance) ? resistance : INERTIA_LAMBDA_DIVISOR) /
    INERTIA_LAMBDA_DIVISOR;
  return {
    // 夹在 [MIN_INERTIA_LAMBDA, ∞)。非有限值退回默认档（λ = 5）。
    lambda: Number.isFinite(rawLambda) ? Math.max(MIN_INERTIA_LAMBDA, rawLambda) : 5,
    omega: RETURN_SETTLE_LN100 / (RETURN_DAMPING_RATIO * safeDuration),
    hardCap: safeDuration * RETURN_HARD_CAP_FACTOR
  };
}

/**
 * 推进一个点 `dt` 秒。**纯函数** —— 只改传进来的 dot，不碰 DOM / rAF / props。
 *
 * ★ 为什么单独抽出来：物理是这一层最容易悄悄坏掉的部分
 *   （NaN、发散、点卡在飞行状态），而它在浏览器里只能靠"看着回来了"判断。
 *   抽成纯函数之后可以用单元测试钉死收敛性与兜底行为，
 *   而**积分仍然只在那一个 rAF 循环里被调用**（§5.1 的动效纪律）。
 *
 * @returns 该点这一帧是否仍在飞行（供调用方决定要不要继续步进）
 */
export function advanceDot(dot: Dot, dtRaw: number, p: DotPhysicsParams): boolean {
  if (dot.phase === 'idle') return false;

  // ① dt 夹住：标签页切回来时可能是好几秒
  const dt = Math.min(dtRaw, MAX_STEP_DT);

  // ② 子步：半隐式欧拉的稳定条件是 ω·dt ≪ 2
  const steps = Math.min(8, Math.max(1, Math.ceil(dt / PHYSICS_SUBSTEP)));
  const h = dt / steps;

  dot.phaseT += dt;

  /*
    ③ 兜底超时 —— **两个阶段都要管**（这是测试抓出来的第二个真 bug）。

    最初只写在回弹分支里，于是"惯性阶段因为 λ 太小而永远滑不完"这种
    情况完全没有兜底：点停在半路、`_inertiaApplied` 永不放开、
    那个点从此不响应鼠标。现在提前到两个分支之前统一判。
  */
  if (dot.phaseT > p.hardCap) {
    resetDot(dot);
    return false;
  }

  if (dot.phase === 'inertia') {
    for (let s = 0; s < steps; s++) {
      const decay = Math.exp(-p.lambda * h);
      dot.xOffset = dot.targetX + (dot.xOffset - dot.targetX) * decay;
      dot.yOffset = dot.targetY + (dot.yOffset - dot.targetY) * decay;
    }
    // 滑停 → 转回弹。此时速度已经很小，回弹从静止开始。
    const remaining = Math.hypot(dot.xOffset - dot.targetX, dot.yOffset - dot.targetY);
    if (remaining < INERTIA_DONE_PX) {
      dot.xOffset = dot.targetX;
      dot.yOffset = dot.targetY;
      dot.vx = 0;
      dot.vy = 0;
      dot.phase = 'return';
      dot.returnAmp = Math.max(1, Math.hypot(dot.xOffset, dot.yOffset));
      /*
        ④ 回弹阶段的计时从**现在**算。

        最初直接沿用 `phaseT`（从推出去那一刻就在累加），于是惯性阶段
        花掉的时间会把回弹的兜底预算吃掉 —— 惯性用了 1s 的话，
        回弹只剩 hardCap-1s 可用，而 hardCap 是按 returnDuration 算的，
        本该是"回弹自己"的预算。
        症状是回弹被过早掐断（点还没弹完就归零）。
      */
      dot.phaseT = 0;
    }
    return true;
  }

  // phase === 'return'：欠阻尼弹簧
  const k = p.omega * p.omega;
  const c = 2 * RETURN_DAMPING_RATIO * p.omega;
  for (let s = 0; s < steps; s++) {
    const ax = -k * dot.xOffset - c * dot.vx;
    const ay = -k * dot.yOffset - c * dot.vy;
    dot.vx += ax * h;
    dot.vy += ay * h;
    dot.xOffset += dot.vx * h;
    dot.yOffset += dot.vy * h;
  }

  /*
    ⑤ 落定判据用**能量**（偏移 + 速度），不是瞬时位置。

    欠阻尼弹簧的 x(t) 会周期性地穿过 0，而"此刻 x ≈ 0"不代表停下来了
    （此时速度最大，它马上还要弹到另一侧）。按瞬时位置判，
    理论上会在某次过零时把点提前掐断。

    ⚠️ 但要如实说明：**在调用点实际用的参数下（returnDuration 1.6s），
    这两种判据行为等价** —— 每帧位移约 9.9px，而落定阈值只有 0.72px，
    采样点永远跨过那个过零窗口，采不到"|x| 很小而速度很大"的瞬间。
    实测（反向验证，2026-10-16）：把它换回瞬时位置判据，
    12 条测试**全绿** —— 也就是说这不是一个"已发生的 bug 的修复"。

    那为什么还留着能量判据：它对**所有** ω 都成立，而位置判据只在
    ω 足够大时碰巧成立。临界点在 returnDuration ≈ 22s 附近
    （每帧位移 ≈ 阈值）。测试里有一条专门跑 returnDuration = 25s
    的用例把这条边界钉住 —— 那一档下位置判据会真的误判。

    速度要归一化成"等效位移"：弹簧的振幅 A 满足
      v_max = A·ω  →  等效位移 = |v| / ω
    于是能量判据 = hypot(x, v/ω)，与 x 同量纲，可以直接和振幅比。
  */
  const energy = Math.hypot(dot.xOffset, dot.vx / p.omega, dot.yOffset, dot.vy / p.omega);
  if (energy <= dot.returnAmp * RETURN_SETTLE_FRACTION) {
    resetDot(dot);
    return false;
  }
  return true;
}

/** 归零并重开闸门。落定、兜底、以及非有限值都用它。 */
export function resetDot(dot: Dot): void {
  dot.xOffset = 0;
  dot.yOffset = 0;
  dot.vx = 0;
  dot.vy = 0;
  dot.phase = 'idle';
  dot.phaseT = 0;
  dot._inertiaApplied = false;
}

/** 把一个点推出去（mousemove 与 click 两个入口共用）。 */
export function pushDot(dot: Dot, targetX: number, targetY: number): void {
  dot._inertiaApplied = true;
  dot.targetX = targetX;
  dot.targetY = targetY;
  dot.vx = 0;
  dot.vy = 0;
  dot.phase = 'inertia';
  dot.phaseT = 0;
}

export interface DotGridProps {
  dotSize?: number;
  gap?: number;
  baseColor?: string;
  activeColor?: string;
  proximity?: number;
  speedTrigger?: number;
  shockRadius?: number;
  shockStrength?: number;
  maxSpeed?: number;
  resistance?: number;
  returnDuration?: number;
  className?: string;
  style?: React.CSSProperties;
}

/*
  ── 本地改动 6：`hexToRgb` 同时接受 `#rrggbb` 与 `rgb(r,g,b)` ──────

  ★ 为什么必须改（2026-10-12 实测的真实 bug）

  上游这个函数只认十六进制，解析失败时**静默返回纯黑** `{0,0,0}`：

      function hexToRgb(hex) {
        const m = hex.match(/^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i)
        if (!m) return { r: 0, g: 0, b: 0 }   // ← 这里
      }

  而 `HeroBackdrop` 传进来的是**从 CSS 变量拼出的 `rgb(...)`**：

      dotBase:   `rgb(${readTriple('--suvex-line-strong', '44 50 61')})`     → "rgb(44,50,61)"
      dotActive: `rgb(${readTriple('--suvex-accent-strong', '169 155 255')})` → "rgb(169,155,255)"

  它必须这么传：同一份 token 既要喂 CSS 又要喂 canvas，而 token 存的是
  RGB 通道三元组（`44 50 61`），拼成 `rgb()` 是唯一两边都能用的形式。

  于是两个 `rgb(...)` 都解析成 `{0,0,0}`，下面的插值恒为 `rgb(0,0,0)`：

      const r = Math.round(0 + (0 - 0) * t)   // 恒等于 0

  **症状与主题相关，所以极易误判成"暗色主题的样式问题"：**

  | 主题 | 底色 | 鼠标靠近时 | 看起来 |
  | ---- | ---- | ---------- | ------ |
  | 亮色 | `244,245,247` 浅灰 | 点变**纯黑** | 像"被点亮"，**看着是对的** |
  | 暗色 | `8,9,12` 近黑 | 点变**纯黑** | 与背景同色 → **一片全黑** |

  亮色下它甚至"更好看"了（黑点在浅底上对比强），所以这个 bug 藏了很久 ——
  两个主题里有一个是**碰巧对**的。

  ## 为什么扩展解析而不是改调用方

  改 `HeroBackdrop` 去传十六进制也能修，但那样要做一次
  `rgb(44,50,61)` → `#2c323d` 的反向转换，等于把"token 是三元组"
  这个约定在调用方再实现一遍。而让**解析函数容忍两种格式**更小、
  且不改变任何既有行为（十六进制路径逐字保留）。

  ⚠️ 正则用 `[,\s]+` 分隔而不是只认逗号：CSS 的现代写法允许
  `rgb(44 50 61)`（空格分隔），而 `readTriple` 返回的三元组正是这种形状 ——
  将来若有人直接把它拼进 `rgb()`，这里也不会再踩一次同样的坑。
*/
function hexToRgb(color: string) {
  const hex = color.match(/^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i);
  if (hex) {
    return {
      r: parseInt(hex[1], 16),
      g: parseInt(hex[2], 16),
      b: parseInt(hex[3], 16)
    };
  }
  const rgb = color.match(/^rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/i);
  if (rgb) {
    return { r: Number(rgb[1]), g: Number(rgb[2]), b: Number(rgb[3]) };
  }
  return { r: 0, g: 0, b: 0 };
}

const DotGrid: React.FC<DotGridProps> = ({
  dotSize = 16,
  gap = 32,
  baseColor = '#5227FF',
  activeColor = '#5227FF',
  proximity = 150,
  speedTrigger = 100,
  shockRadius = 250,
  shockStrength = 5,
  maxSpeed = 5000,
  resistance = 750,
  returnDuration = 1.5,
  className = '',
  style
}) => {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const dotsRef = useRef<Dot[]>([]);
  const pointerRef = useRef({
    x: 0,
    y: 0,
    vx: 0,
    vy: 0,
    speed: 0,
    lastTime: 0,
    lastX: 0,
    lastY: 0
  });

  const baseRgb = useMemo(() => hexToRgb(baseColor), [baseColor]);
  const activeRgb = useMemo(() => hexToRgb(activeColor), [activeColor]);

  const circlePath = useMemo(() => {
    if (typeof window === 'undefined' || !window.Path2D) return null;

    const p = new Path2D();
    p.arc(0, 0, dotSize / 2, 0, Math.PI * 2);
    return p;
  }, [dotSize]);

  const buildGrid = useCallback(() => {
    const wrap = wrapperRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return;

    const { width, height } = wrap.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;

    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    const ctx = canvas.getContext('2d');
    if (ctx) ctx.scale(dpr, dpr);

    const cols = Math.floor((width + gap) / (dotSize + gap));
    const rows = Math.floor((height + gap) / (dotSize + gap));
    const cell = dotSize + gap;

    const gridW = cell * cols - gap;
    const gridH = cell * rows - gap;

    const extraX = width - gridW;
    const extraY = height - gridH;

    const startX = extraX / 2 + dotSize / 2;
    const startY = extraY / 2 + dotSize / 2;

    const dots: Dot[] = [];
    for (let y = 0; y < rows; y++) {
      for (let x = 0; x < cols; x++) {
        const cx = startX + x * cell;
        const cy = startY + y * cell;
        dots.push({
          cx,
          cy,
          xOffset: 0,
          yOffset: 0,
          _inertiaApplied: false,
          vx: 0,
          vy: 0,
          targetX: 0,
          targetY: 0,
          phase: 'idle',
          phaseT: 0,
          returnAmp: 1
        });
      }
    }
    dotsRef.current = dots;
  }, [dotSize, gap]);

  /*
    物理积分 —— **在下面那个已有的 rAF 循环里调用**，不新开循环。

    ⚠️ 只步进 phase !== 'idle' 的点。一屏约 2000 个点，而任意时刻
       处在飞行中的只有光标附近的那几十个 —— 遍历全部再判断是白花的
       开销，而 idle 的点本来就不动。

    积分本身在 `advanceDot`（纯函数，可单测），这里只负责遍历与调用。
  */
  const stepPhysics = useCallback(
    (dtRaw: number) => {
      const params = resolvePhysicsParams(resistance, returnDuration);
      for (const dot of dotsRef.current) {
        if (dot.phase === 'idle') continue;
        advanceDot(dot, dtRaw, params);

        // 非有限值（NaN/Infinity）直接复位 —— 宁可少一个点的动画，
        // 也不要一个算坏的点把整块画布带偏（NaN 会让 translate 整条失效）
        if (
          !Number.isFinite(dot.xOffset) ||
          !Number.isFinite(dot.yOffset) ||
          !Number.isFinite(dot.vx) ||
          !Number.isFinite(dot.vy)
        ) {
          resetDot(dot);
        }
      }
    },
    [resistance, returnDuration]
  );

  useEffect(() => {
    if (!circlePath) return;

    let rafId: number;
    let lastFrame = 0;
    const proxSq = proximity * proximity;

    const draw = (now: number) => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;

      // 物理积分就在这个循环里 —— 不新开 rAF（§5.1 的动效纪律）
      const dt = lastFrame ? (now - lastFrame) / 1000 : 0;
      lastFrame = now;
      if (dt > 0) stepPhysics(dt);

      ctx.clearRect(0, 0, canvas.width, canvas.height);

      const { x: px, y: py } = pointerRef.current;

      for (const dot of dotsRef.current) {
        const ox = dot.cx + dot.xOffset;
        const oy = dot.cy + dot.yOffset;
        const dx = dot.cx - px;
        const dy = dot.cy - py;
        const dsq = dx * dx + dy * dy;

        let style = baseColor;
        if (dsq <= proxSq) {
          const dist = Math.sqrt(dsq);
          const t = 1 - dist / proximity;
          const r = Math.round(baseRgb.r + (activeRgb.r - baseRgb.r) * t);
          const g = Math.round(baseRgb.g + (activeRgb.g - baseRgb.g) * t);
          const b = Math.round(baseRgb.b + (activeRgb.b - baseRgb.b) * t);
          style = `rgb(${r},${g},${b})`;
        }

        ctx.save();
        ctx.translate(ox, oy);
        ctx.fillStyle = style;
        ctx.fill(circlePath);
        ctx.restore();
      }

      rafId = requestAnimationFrame(draw);
    };

    rafId = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(rafId);
  }, [proximity, baseColor, activeRgb, baseRgb, circlePath, stepPhysics]);

  useEffect(() => {
    buildGrid();
    let ro: ResizeObserver | null = null;
    if ('ResizeObserver' in window) {
      ro = new ResizeObserver(buildGrid);
      if (wrapperRef.current) ro.observe(wrapperRef.current);
    } else {
      (window as Window).addEventListener('resize', buildGrid);
    }
    return () => {
      if (ro) ro.disconnect();
      else window.removeEventListener('resize', buildGrid);
    };
  }, [buildGrid]);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const canvas = canvasRef.current;
      if (!canvas) return;

      const now = performance.now();
      const pr = pointerRef.current;
      const dt = pr.lastTime ? now - pr.lastTime : 16;
      const dx = e.clientX - pr.lastX;
      const dy = e.clientY - pr.lastY;
      let vx = (dx / dt) * 1000;
      let vy = (dy / dt) * 1000;
      let speed = Math.hypot(vx, vy);
      if (speed > maxSpeed) {
        const scale = maxSpeed / speed;
        vx *= scale;
        vy *= scale;
        speed = maxSpeed;
      }
      pr.lastTime = now;
      pr.lastX = e.clientX;
      pr.lastY = e.clientY;
      pr.vx = vx;
      pr.vy = vy;
      pr.speed = speed;

      const rect = canvas.getBoundingClientRect();
      pr.x = e.clientX - rect.left;
      pr.y = e.clientY - rect.top;

      for (const dot of dotsRef.current) {
        const dist = Math.hypot(dot.cx - pr.x, dot.cy - pr.y);
        if (speed > speedTrigger && dist < proximity && !dot._inertiaApplied) {
          /*
            本地改动 7：上游这里是
              gsap.killTweensOf(dot)
              gsap.to(dot, { inertia: { xOffset: pushX, yOffset: pushY, resistance }, … })
            现在只是**把目标写进点的状态**，真正的积分在 rAF 循环里做。

            「打断在飞的补间」这个语义由 `_inertiaApplied` 闸门承担：
            闸门关着时这个点根本进不来（见上面的条件），
            所以不需要 killTweensOf 那一步。
          */
          pushDot(dot, dot.cx - pr.x + vx * 0.005, dot.cy - pr.y + vy * 0.005);
        }
      }
    };

    const onClick = (e: MouseEvent) => {
      const canvas = canvasRef.current;
      if (!canvas) return;

      const rect = canvas.getBoundingClientRect();
      const cx = e.clientX - rect.left;
      const cy = e.clientY - rect.top;
      for (const dot of dotsRef.current) {
        const dist = Math.hypot(dot.cx - cx, dot.cy - cy);
        if (dist < shockRadius && !dot._inertiaApplied) {
          const falloff = Math.max(0, 1 - dist / shockRadius);
          // 同上：只写目标，积分在 rAF 循环里
          pushDot(dot, (dot.cx - cx) * shockStrength * falloff, (dot.cy - cy) * shockStrength * falloff);
        }
      }
    };

    const throttledMove = throttle(onMove, 50);
    window.addEventListener('mousemove', throttledMove, { passive: true });

    /*
     * 本地改动（任务 8）：`shockStrength` 为 0 时**根本不注册 click 监听**。
     *
     * 上游无条件注册，且在 onClick 里对**每一个点**算一次距离。
     * 后果有两个，都属于「为了炫技牺牲可用性」：
     *   1. 它是挂在 `window` 上的 —— 于是页面上**每一次真实交互**
     *      （点按钮、勾 checkbox、展开事件行）都会在背景下掀起一次冲击波。
     *      用户点"启动扫描"时背景炸一下，这不是反馈，是噪音。
     *   2. 每次点击都要遍历全部点阵（1080p / gap 30 下约 2000 个点）
     *      并逐点算 hypot，然后把命中的点交给动画层建补间。
     *
     * 所以这里改成：strength 为 0 = 不注册，连遍历都省掉（见 AgentField 的调用点）。
     * 光晕跟随（mousemove）保留 —— 那才是"这是可交互的现场"的有效信号，
     * 而且它是节流过的。
     */
    if (shockStrength > 0) {
      window.addEventListener('click', onClick);
    }

    return () => {
      window.removeEventListener('mousemove', throttledMove);
      window.removeEventListener('click', onClick);
    };
  }, [maxSpeed, speedTrigger, proximity, resistance, returnDuration, shockRadius, shockStrength]);

  return (
    <div ref={wrapperRef} className={`relative h-full w-full ${className}`} style={style}>
      <canvas ref={canvasRef} className="pointer-events-none absolute inset-0 h-full w-full" />
    </div>
  );
};

export default DotGrid;
