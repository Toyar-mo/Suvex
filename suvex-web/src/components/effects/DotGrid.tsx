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
 *   5. gsap 时间戳与未使用参数不再走短路求值：`a && b()` 改成 if，ESLint 的
 *      no-unused-expressions 会拦，且短路写法在读的时候要多想一秒。
 *
 * 依赖：gsap + gsap/InertiaPlugin。
 * 注意：InertiaPlugin 在 gsap 3.13+ 起已包含在公开 npm 包内（gsap/InertiaPlugin），
 * 无需再安装 gsap-trial 或会员包。
 */
import React, { useRef, useEffect, useCallback, useMemo } from 'react';
import { gsap } from 'gsap';
import { InertiaPlugin } from 'gsap/InertiaPlugin';

gsap.registerPlugin(InertiaPlugin);

const throttle = (func: (...args: any[]) => void, limit: number) => {
  let lastCall = 0;
  let trailing: ReturnType<typeof setTimeout> | null = null;
  let lastArgs: any[] = [];
  return function (this: any, ...args: any[]) {
    const now = performance.now();
    const invoke = () => {
      lastCall = performance.now();
      func.apply(this, lastArgs);
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
};

interface Dot {
  cx: number;
  cy: number;
  xOffset: number;
  yOffset: number;
  _inertiaApplied: boolean;
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

  于是两个 `rgb(...)` 都解析成 `{0,0,0}`，第 196-199 行的插值恒为 `rgb(0,0,0)`：

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
        dots.push({ cx, cy, xOffset: 0, yOffset: 0, _inertiaApplied: false });
      }
    }
    dotsRef.current = dots;
  }, [dotSize, gap]);

  useEffect(() => {
    if (!circlePath) return;

    let rafId: number;
    const proxSq = proximity * proximity;

    const draw = () => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
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

    draw();
    return () => cancelAnimationFrame(rafId);
  }, [proximity, baseColor, activeRgb, baseRgb, circlePath]);

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
          dot._inertiaApplied = true;
          gsap.killTweensOf(dot);
          const pushX = dot.cx - pr.x + vx * 0.005;
          const pushY = dot.cy - pr.y + vy * 0.005;
          gsap.to(dot, {
            inertia: { xOffset: pushX, yOffset: pushY, resistance },
            onComplete: () => {
              gsap.to(dot, {
                xOffset: 0,
                yOffset: 0,
                duration: returnDuration,
                ease: 'elastic.out(1,0.75)'
              });
              dot._inertiaApplied = false;
            }
          });
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
          dot._inertiaApplied = true;
          gsap.killTweensOf(dot);
          const falloff = Math.max(0, 1 - dist / shockRadius);
          const pushX = (dot.cx - cx) * shockStrength * falloff;
          const pushY = (dot.cy - cy) * shockStrength * falloff;
          gsap.to(dot, {
            inertia: { xOffset: pushX, yOffset: pushY, resistance },
            onComplete: () => {
              gsap.to(dot, {
                xOffset: 0,
                yOffset: 0,
                duration: returnDuration,
                ease: 'elastic.out(1,0.75)'
              });
              dot._inertiaApplied = false;
            }
          });
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
     *      并逐点算 hypot，然后把命中的点交给 gsap 建补间。
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
