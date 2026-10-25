import react from '@vitejs/plugin-react'
import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'

// tsconfig.node.json 是 `module: nodenext`，相对导入必须带扩展名；
// `allowImportingTsExtensions` 已开，Vite 侧的 esbuild 也直接吃 .ts。
import { requireTypecheck } from './build/require-typecheck.ts'

// 版本号从 package.json 读，避免在代码里维护第二份（会漂移）
const pkg = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf-8')) as {
  version: string
}

/*
  ─────────────────────────────────────────────────────────────
  开发服务器代理 `/api` → 后端
  ─────────────────────────────────────────────────────────────

  有了它，前端代码里**不出现后端的主机名和端口**：页面请求自己所在的
  那个 origin（`window.location.origin` + `/api/v1/...`），由这里转发出去。
  所以无论用 `127.0.0.1:5173`、`192.168.44.128:5173` 还是别的地址打开，
  都能正确找到后端 —— 换台机器不需要改任何文件。

  三个附带好处，都是"写死地址"做不到的：

  * **没有 CORS**：浏览器看到的是同源请求，不做预检，后端也不需要
    在 `SUPEX_CORS_ORIGINS` 里逐个列出访问者来源（那个列表每换个 IP
    就要改一次，正是"本机好使、交付到别处就登不上"的常见成因）。
  * **Cookie 必然带得上**：同源请求不受 `SameSite` 的跨 site 限制。
  * **SSE 也走同一条路**：事件流的地址由同一个 base 拼出，不用单独配。

  ### 目标地址从哪来

  `VITE_API_PROXY_TARGET`（写在 `.env.local`，**不**入库）。
  不配时回落到本机 8000 —— 也就是后端默认监听的端口，于是
  "拉下来就能跑"是成立的。后端换了端口才需要配这一项，
  而它只影响**这一台开发机**，不进代码。

  ### 为什么 `changeOrigin` 必须是 `false`

  ★ 这一项**不能**改成 `true`，否则所有写操作都会 403。

  后端有一道 CSRF 校验（`app/auth/deps.py` 的 `enforce_csrf`）：Cookie 通道的
  写请求，其 `Origin` 必须落在允许集合里。那个集合是「配置的 CORS 源 +
  **本请求自己的源**」—— 而"自己的源"是用 `request.url.netloc` 算的，
  **也就是 `Host` 头**。

  `changeOrigin: true` 会把 `Host` 改写成代理目标（`127.0.0.1:8000`），
  于是后端以为自己叫 `http://127.0.0.1:8000`，而浏览器发来的 `Origin`
  是 `http://192.168.44.128:5173` —— 两边对不上，判定为跨源，**403
  `CROSS_ORIGIN_BLOCKED`**。

  表现很迷惑人：页面能打开、列表能读、登录也成功（登录时还没有会话，
  不走这道检查），**只有"点下去要写数据"的动作全挂** —— 生成报告、
  创建扫描、改漏洞状态、归档、删除，一个都做不了。而用 curl 手测
  却是通的（curl 不带 `Origin` 头，走的是另一条分支）。

  保持原始 `Host`，后端算出的"自身源"就等于浏览器的 `Origin`，
  同源判定成立，白名单一个都不用加。这也正是这个方案能"换任何地址
  访问都不用改配置"的前提。

  ### SSE 需要注意的地方

  Vite 的代理基于 http-proxy，默认不缓冲响应体，所以 `text/event-stream`
  能实时透传。**不要**在这一层加 `compression` 之类的中间件 ——
  那会把流式响应攒成整块再发，表现是"事件流一直空白，刷新后才一次性出现"。
*/
export default defineConfig(({ mode }) => {
  // 第三个参数传空串 = 读取所有变量（不只是 `VITE_` 前缀的），
  // 这样 `vite.config.ts` 里也能读到 `VITE_API_PROXY_TARGET`。
  const env = loadEnv(mode, process.cwd(), '')
  const proxyTarget = env.VITE_API_PROXY_TARGET?.trim() || 'http://127.0.0.1:8000'

  /*
    ─────────────────────────────────────────────────────────────
    允许哪些 Host 访问这个开发服务器
    ─────────────────────────────────────────────────────────────

    ★ 为什么需要它：把开发服务器通过隧道（cloudflared 等）暴露出去时，
    Vite 会拒绝 Host 不是 localhost/IP 的请求：

        403 Blocked request. This host ("xxx.trycloudflare.com") is not allowed.

    症状是页面**直接打不开**，而那条错误只在响应体里 ——
    服务端看不出任何异常（它是一次正常的 403，日志里只有一行 403）。

    ★★ 为什么不直接写 `allowedHosts: true`

    那会把 Vite 的 **DNS rebinding 防护整个关掉**，而在这个项目里它的代价
    比一般项目大。原因是一条本项目的刻意设计：

      · 代理那里 `changeOrigin: false`（必须如此，见文件头），
        也就是 **Host 头被原样透传给后端**；
      · 而后端的 CSRF 判定把「本请求自己的源」算成由 Host 派生的值
        （`app/auth/deps.py` 的 `_allowed_origins`）。

    于是「浏览器发出的 Host 正好等于后端认定的自身源」成了同源判定的基础。
    攻击者只要让自己的域名解析到这台机器（DNS rebinding 的标准手法），
    受害者的浏览器就会带着 `Host: evil.com` 打到开发服务器上 ——
    而一旦放行任意 Host，那个页面就成了与 API **同源**的页面，
    CSRF 那道检查在它面前自然成立。`allowedHosts` 挡的正是这一步。

    ★ 所以做成**配置项**：既不写死（写死就没有下次），也不放开

    临时隧道（trycloudflare 的 quick tunnel）每次重启都换一个随机域名。
    配在 `VITE_ALLOWED_HOSTS`（逗号分隔，读 `.env.local`）：

      · `VITE_ALLOWED_HOSTS=palace-november-off-door.trycloudflare.com`
        —— 最紧，但那串随机名一变就得改；
      · `VITE_ALLOWED_HOSTS=.trycloudflare.com`
        —— **前导点表示"允许该域下的任意子域"**。对 quick tunnel 是合适的：
          攻击者**无法**让 `*.trycloudflare.com` 解析到你的机器
          （那个域由 Cloudflare 控制），所以上面那条 rebinding 路径走不通；
      · 不配 = 保持 Vite 默认（只放行 localhost 与 IP 字面量）。

    ⚠️ 改完必须**重启** suvex-web：Vite 只在启动时读一次配置。
  */
  const allowedHosts = (env.VITE_ALLOWED_HOSTS ?? '')
    .split(',')
    .map((host) => host.trim())
    .filter(Boolean)

  /*
    ─────────────────────────────────────────────────────────────
    要不要开 HMR（热更新）
    ─────────────────────────────────────────────────────────────

    ★ 为什么需要这个开关：**隧道上必须关掉**。

    Vite 的 HMR 走 WebSocket。通过隧道（cloudflared quick tunnel 等）访问时
    那条 WebSocket 建立不起来，而 `@vite/client` 的处理方式是：

        socket 关闭 → 轮询 __vite_ping → 服务器一应答就 location.reload()

    于是变成**刷新死循环**：加载 → WS 失败 → 轮询 → 刷新 → …

    症状极具迷惑性：页面一直转圈、`#root` 始终是空的、资源请求**全部 200**
    且没有一条失败记录（因为每次轮询都是一次正常的请求），
    而**服务端日志里什么都看不到**。从服务端侧基本查不出来 ——
    唯一的分辨办法是拿同一个页面在局域网源站上对比：那边是稳的。

    关掉之后页面稳定；代价是改文件不再自动热更，手动刷新即可。
    对"通过隧道**使用**这个界面"的场景这个代价是对的：远程要的是成品，
    不是开发过程。（真要在隧道上开发，该换的是生产构建，而不是 HMR。）
  */
  const disableHmr = (env.VITE_DISABLE_HMR ?? '').trim() === '1'

  return {
    plugins: [
      react(),
      /*
        产物必须来自类型检查过的代码 —— 见 build/require-typecheck.ts。
        `npx vite build` 也能产出 dist，不挂这个插件的话它不做跨作用域检查。
      */
      requireTypecheck(),
    ],
    define: {
      __APP_VERSION__: JSON.stringify(pkg.version),
    },
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      // 只在**配了**的时候才覆盖，否则保持 Vite 默认的防护（见上面的长注释）
      ...(allowedHosts.length > 0 ? { allowedHosts } : {}),
      // 隧道上必须关（见上面 disableHmr 的长注释）
      ...(disableHmr ? { hmr: false } : {}),
      proxy: {
        '/api': {
          target: proxyTarget,
          // ★ 必须 false —— 见文件头「为什么 changeOrigin 必须是 false」。
          // 改成 true 会让后端算错"自己的源"，所有写操作 403。
          changeOrigin: false,
        },
      },
      watch: {
        // 某些编辑器（以及 AI 工具的写入器）用「同目录临时文件 + 原子替换」保存，
        // 会在源码目录里短暂建出 .xxx.tmpdir/ 之类的中间文件。
        // Vite 的 watcher 去 watch 这些马上就被删掉的文件时，Windows 上会抛
        // EBUSY 并直接杀死 dev server（不是警告，是进程级崩溃）。忽略掉它们。
        ignored: ['**/.*.tmpdir/**', '**/*.tmp', '**/.~*'],
      },
    },
  }
})
