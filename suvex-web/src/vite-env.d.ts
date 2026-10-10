/// <reference types="vite/client" />

/** 由 vite.config.ts 的 define 注入，值来自 package.json 的 version */
declare const __APP_VERSION__: string

/**
 * 前端可见的环境变量（只有 `VITE_` 前缀的会被 Vite 注入）。
 *
 * ⚠️ `VITE_` 变量会被**打进客户端产物**，任何人都能在浏览器里读到。
 * 所以这里只放公开配置（服务地址），**密钥一律不放**。
 */
interface ImportMetaEnv {
  /**
   * 后端地址的**显式覆盖**，如 `http://10.0.0.5:8000`。
   *
   * ★ **通常不需要配，也不应该配。**
   *
   * 不配时 `lib/api.ts` 用**当前页面的 origin**（`window.location.origin`），
   * 请求发回前端自己所在的地址，由开发服务器的 `server.proxy`
   * （见 vite.config.ts）或生产环境的反向代理转到后端。这样代码里
   * 不出现任何主机名/端口，换机器、换端口、交付到别的环境都不用改。
   *
   * 什么时候才需要它：**前端与后端真的不同源**，且中间没有反向代理
   * 可用（开发时没配 proxy、生产上又是两个独立域名）。
   * 一旦配了，就等于把后端地址钉死在这个值上 —— 而它会被打进产物，
   * 所以是"每个部署环境各构建一次"的用法。
   */
  readonly VITE_API_BASE_URL?: string

  /*
    注：这里**没有** `VITE_STREAM_URL`。

    事件流（SSE）的地址由 `lib/api.ts` 的同一个 base 拼出来
    （见 `data/activity-stream.ts`），所以它跟着 `VITE_API_BASE_URL`
    或默认 origin 走，天然一致。曾经声明的 `VITE_STREAM_URL` 从未被
    任何代码读取 —— 一个"配了以为有用、实际不生效"的变量比没有更糟，
    所以删掉而不是留着。
  */
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
