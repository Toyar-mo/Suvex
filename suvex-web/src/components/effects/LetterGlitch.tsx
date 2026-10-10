/**
 * ReactBits · LetterGlitch  (ts-tailwind 变体)
 * 出处: https://reactbits.dev/backgrounds/letter-glitch
 * 源文件: https://github.com/DavidHDev/react-bits/blob/main/src/ts-tailwind/Backgrounds/LetterGlitch/LetterGlitch.tsx
 * 取用日期: 2026-02-26 · 许可证见同目录 README.md
 *
 * 本地改动：
 *   1. 删掉上游的 react-hooks/exhaustive-deps 忽略注释：本项目用 flat config 且
 *      对拷贝件关闭了该规则，留着注释只会让 ESLint 报 "unused disable directive"。
 *   2. letterSpacing 显式写死为 0 —— 上游依赖 canvas 默认值，父级若改了 CSS
 *      letter-spacing，canvas 会跟着变，网格列宽算的是 charWidth=10，对不上就会错位。
 *   3. 全部 props 给了默认值（上游把 props 类型写成必填但又有默认值，用法矛盾）。
 *   4. `useRef(Date.now())` → `useRef(0)`，时间基准改在 effect 里取。渲染期调
 *      Date.now() 是不纯的读取，并发渲染下会拿到不可预期的值（react-hooks/purity）。
 *
 * 依赖：无（纯 Canvas 2D）。
 */
import { useRef, useEffect } from 'react';

interface Rgb {
  r: number;
  g: number;
  b: number;
}

const FALLBACK_RGB: Rgb = { r: 255, g: 255, b: 255 };

const LetterGlitch = ({
  glitchColors = ['#2b4539', '#61dca3', '#61b3dc'],
  glitchSpeed = 50,
  centerVignette = false,
  outerVignette = true,
  smooth = true,
  lightMode = false,
  backgroundColor,
  className = '',
  characters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ!@#$&*()-_+=/[]{};:<>.,0123456789'
}: {
  glitchColors?: string[];
  glitchSpeed?: number;
  centerVignette?: boolean;
  outerVignette?: boolean;
  smooth?: boolean;
  lightMode?: boolean;
  backgroundColor?: string;
  className?: string;
  characters?: string;
}) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const animationRef = useRef<number | null>(null);
  const letters = useRef<
    {
      char: string;
      rgb: Rgb;
      fromRgb: Rgb;
      targetRgb: Rgb;
      colorProgress: number;
    }[]
  >([]);
  const grid = useRef({ columns: 0, rows: 0 });
  const context = useRef<CanvasRenderingContext2D | null>(null);
  const lastGlitchTime = useRef(0);

  const lettersAndSymbols = Array.from(characters);

  const fontSize = 16;
  const charWidth = 10;
  const charHeight = 20;

  const getRandomChar = () => {
    return lettersAndSymbols[Math.floor(Math.random() * lettersAndSymbols.length)];
  };

  const getRandomColor = () => {
    return glitchColors[Math.floor(Math.random() * glitchColors.length)];
  };

  const hexToRgb = (hex: string) => {
    const shorthandRegex = /^#?([a-f\d])([a-f\d])([a-f\d])$/i;
    hex = hex.replace(shorthandRegex, (_m, r, g, b) => {
      return r + r + g + g + b + b;
    });

    const result = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
    return result
      ? {
          r: parseInt(result[1], 16),
          g: parseInt(result[2], 16),
          b: parseInt(result[3], 16)
        }
      : null;
  };

  // Interpolation happens in numbers, and the CSS string is only built at
  // paint time. Previously the formatted `rgb(...)` string was stored back
  // on the letter and fed to hexToRgb on the next frame, which returned null
  // and froze the transition after a single step.
  const mixRgb = (start: Rgb, end: Rgb, factor: number): Rgb => ({
    r: Math.round(start.r + (end.r - start.r) * factor),
    g: Math.round(start.g + (end.g - start.g) * factor),
    b: Math.round(start.b + (end.b - start.b) * factor)
  });

  const rgbToCss = ({ r, g, b }: Rgb) => `rgb(${r}, ${g}, ${b})`;

  // An unparseable entry in glitchColors must not stall the animation.
  const getRandomRgb = (): Rgb => hexToRgb(getRandomColor()) || FALLBACK_RGB;

  const calculateGrid = (width: number, height: number) => {
    const columns = Math.ceil(width / charWidth);
    const rows = Math.ceil(height / charHeight);
    return { columns, rows };
  };

  const initializeLetters = (columns: number, rows: number) => {
    grid.current = { columns, rows };
    const totalLetters = columns * rows;
    letters.current = Array.from({ length: totalLetters }, () => {
      const rgb = getRandomRgb();
      return {
        char: getRandomChar(),
        rgb,
        fromRgb: rgb,
        targetRgb: getRandomRgb(),
        colorProgress: 1
      };
    });
  };

  const drawLetters = () => {
    const canvas = canvasRef.current;
    if (!context.current || !canvas || letters.current.length === 0) return;
    const ctx = context.current;
    const { width, height } = canvas.getBoundingClientRect();
    ctx.clearRect(0, 0, width, height);
    ctx.font = `${fontSize}px monospace`;
    ctx.textBaseline = 'top';
    ctx.letterSpacing = '0px';

    letters.current.forEach((letter, index) => {
      const x = (index % grid.current.columns) * charWidth;
      const y = Math.floor(index / grid.current.columns) * charHeight;
      ctx.fillStyle = rgbToCss(letter.rgb);
      ctx.fillText(letter.char, x, y);
    });
  };

  const updateLetters = () => {
    if (!letters.current || letters.current.length === 0) return;

    const updateCount = Math.max(1, Math.floor(letters.current.length * 0.05));

    for (let i = 0; i < updateCount; i++) {
      const index = Math.floor(Math.random() * letters.current.length);
      if (!letters.current[index]) continue;

      letters.current[index].char = getRandomChar();
      // A new transition starts from the colour currently on screen, so a
      // letter picked again mid-fade continues instead of jumping.
      letters.current[index].fromRgb = letters.current[index].rgb;
      letters.current[index].targetRgb = getRandomRgb();

      if (!smooth) {
        letters.current[index].rgb = letters.current[index].targetRgb;
        letters.current[index].colorProgress = 1;
      } else {
        letters.current[index].colorProgress = 0;
      }
    }
  };

  const handleSmoothTransitions = () => {
    let needsRedraw = false;
    letters.current.forEach(letter => {
      if (letter.colorProgress < 1) {
        letter.colorProgress += 0.05;
        if (letter.colorProgress > 1) letter.colorProgress = 1;

        letter.rgb = mixRgb(letter.fromRgb, letter.targetRgb, letter.colorProgress);
        needsRedraw = true;
      }
    });

    if (needsRedraw) {
      drawLetters();
    }
  };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const resizeCanvas = () => {
      const parent = canvas.parentElement;
      if (!parent) return;

      const dpr = window.devicePixelRatio || 1;
      const rect = parent.getBoundingClientRect();

      canvas.width = rect.width * dpr;
      canvas.height = rect.height * dpr;

      canvas.style.width = `${rect.width}px`;
      canvas.style.height = `${rect.height}px`;

      if (context.current) {
        context.current.setTransform(dpr, 0, 0, dpr, 0, 0);
      }

      const { columns, rows } = calculateGrid(rect.width, rect.height);
      initializeLetters(columns, rows);
      drawLetters();
    };

    const animate = () => {
      const now = Date.now();
      if (now - lastGlitchTime.current >= glitchSpeed) {
        updateLetters();
        drawLetters();
        lastGlitchTime.current = now;
      }

      if (smooth) {
        handleSmoothTransitions();
      }

      animationRef.current = requestAnimationFrame(animate);
    };

    context.current = canvas.getContext('2d');
    resizeCanvas();
    lastGlitchTime.current = Date.now();
    animate();

    let resizeTimeout: ReturnType<typeof setTimeout>;

    const handleResize = () => {
      clearTimeout(resizeTimeout);
      resizeTimeout = setTimeout(() => {
        cancelAnimationFrame(animationRef.current as number);
        resizeCanvas();
        animate();
      }, 100);
    };

    window.addEventListener('resize', handleResize);

    return () => {
      clearTimeout(resizeTimeout);
      cancelAnimationFrame(animationRef.current!);
      window.removeEventListener('resize', handleResize);
    };
    // glitchSpeed / smooth 变了要重建动画循环，是故意的
  }, [glitchSpeed, smooth]);

  return (
    <div
      className={`relative h-full w-full overflow-hidden ${className}`}
      style={{ backgroundColor: backgroundColor || (lightMode ? '#ffffff' : '#000000') }}
    >
      <canvas ref={canvasRef} className="block h-full w-full" />
      {outerVignette && (
        <div
          className="pointer-events-none absolute left-0 top-0 h-full w-full"
          style={{
            background: lightMode
              ? 'radial-gradient(circle, rgba(255,255,255,0) 58%, rgba(255,255,255,0.96) 100%)'
              : 'radial-gradient(circle, rgba(0,0,0,0) 60%, rgba(0,0,0,1) 100%)'
          }}
        ></div>
      )}
      {centerVignette && (
        <div
          className="pointer-events-none absolute left-0 top-0 h-full w-full"
          style={{
            background: lightMode
              ? 'radial-gradient(circle, rgba(255,255,255,0.9) 0%, rgba(255,255,255,0) 60%)'
              : 'radial-gradient(circle, rgba(0,0,0,0.8) 0%, rgba(0,0,0,0) 60%)'
          }}
        ></div>
      )}
    </div>
  );
};

export default LetterGlitch;
