---
name: js-reverse
description: 前端 JavaScript 签名/加密参数逆向：浏览器观察、运行时采样、源码还原、Node 补环境复现与证据化输出。
---

<!--
  Adapted from zhaoxuya520/reverse-skill (MIT License).
  Source: https://github.com/zhaoxuya520/reverse-skill/tree/main/skills/js-reverse
  Changes: removed upstream routing/self-bootstrap/MCP mapping and
  field-journal; mapped to Suvex sandbox `browser` + `run_shell` only.
-->

# 前端 JavaScript 逆向

## 什么时候用

- 定位登录/接口签名、加密参数、风控字段；
- 找出某个 XHR/fetch/WebSocket 的触发点与参数来源；
- 把浏览器里观察到的算法带回 Node 做本地复现；
- 处理 source map、压缩/混淆代码、WASM 或 worker 中的算法。

不适用：纯二进制/APK 反编译（转 `ctf-reverse`、`apk-reverse`、`ghidra-reverse`）。

## 环境

Suvex 沙箱只有：

- `browser`：真实 Chromium，`open / snapshot / eval / get / wait / screenshot`；
- `run_shell`：Node、Python、curl、文件读写；
- `/work/persist`：跨容器回收的产物目录。

**没有**外部 JS MCP、CDP 断点服务。不要调用它们；
需要运行时观察时用 `browser eval` 注入 hook。

## 工作流

### 1. Observe：先定位请求与脚本

1. `browser open <url>`，用 `snapshot` 看页面与网络相关按钮。
2. 在页面里列出脚本与资源：

```js
[...document.scripts].map(s => s.src).filter(Boolean)
```

3. 找到目标请求后，记录：URL、method、headers、body、触发动作、响应差异。
4. 搜索关键词（`sign`、`token`、`encrypt`、`nonce`、`timestamp`、
   `md5`、`aes`、`subtle`、`wasm`）时优先搜已加载脚本，不要把整站下载下来。

### 2. Capture：最小化运行时采样

在 `browser eval` 里做最小 hook，避免污染页面：

```js
// 记录 fetch 入参与返回；不要改写业务结果
const orig = window.fetch;
window.__probe = [];
window.fetch = async (...args) => {
  const req = args[0];
  const url = typeof req === 'string' ? req : req?.url;
  const init = args[1] || {};
  const out = await orig.apply(window, args);
  window.__probe.push({ url, init, status: out.status });
  return out;
};
```

需要函数入参时，等页面触发一次后读取 `window.__probe`；不要在循环里
反复重装 hook。

### 3. Rebuild：把算法带回本地

- 从 source map / 格式化后的脚本提取纯函数，优先复制最小依赖闭包；
- 用 Node 补环境：`window`、`document`、`location`、`navigator`、
  `crypto`、`TextEncoder` 只补实际用到的字段；
- 先用固定输入对齐浏览器输出，再逐步替换环境；
- 产物写到 `/work/persist/js-reverse/`：脚本、输入样本、运行命令、输出摘要。

```bash
mkdir -p /work/persist/js-reverse
node /work/persist/js-reverse/repro.js
```

### 4. Verify：证据闭环

至少留下：

- 一次浏览器真实请求（URL/headers/body/响应状态）；
- 一次本地复现的相同输出；
- 两者的差异说明（时间戳/nonce 之类可变量要单独列出）。

## 常见坑

- **只看到 minified 名字**：先找 source map，再看调用栈；不要凭变量名猜。
- **签名只在 worker / WASM 里**：先定位 worker 脚本或 `.wasm` 加载点，
  再决定是否用 Node/WASI 复现。
- **反调试**：页面可能检测 devtools/函数 toString；用最小 hook，
  不要整页替换。
- **时间/随机数**：把可变量固定，分别验证算法与随机源。
- **加密 API**：`crypto.subtle` 是异步的；本地复现必须保持 async 顺序。
