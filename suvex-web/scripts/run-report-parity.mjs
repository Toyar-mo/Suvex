/**
 * 运行「预览与导出一致性」检查。
 *
 *   npm run verify:report
 *
 * 为什么需要它：本轮验收标准是"预览效果和导出内容一致"。
 * 这条标准如果只靠人眼比对，每次改动报告文案都会重新欠一次债。
 * 这个脚本把那条标准变成可复跑的命令。
 *
 * 实现要点：**借 Vite 自己的模块加载器加载真实源码**，
 * 而不是把逻辑复制一份到脚本里。复制意味着"源码改了但脚本没跟上"，
 * 那种检查比没有检查更危险 —— 它会给出绿色的假保证。
 *
 * 依赖 Vite 的原因只有一个：源码里用的是 `@/...` 别名（vite.config.ts 定义），
 * node 不认识。用 ssrLoadModule 就等于让**和浏览器完全相同的解析规则**去加载它。
 */
import { fileURLToPath, pathToFileURL, URL } from 'node:url'

const projectRoot = fileURLToPath(new URL('..', import.meta.url))

// vite 从项目自己的 node_modules 里取。用运行时算出的绝对路径动态导入，
// 而不是硬编码在 import 语句里 —— 后者换台机器就失效。
const viteEntry = pathToFileURL(`${projectRoot}/node_modules/vite/dist/node/index.js`).href
const { createServer } = await import(viteEntry)

const server = await createServer({
  root: projectRoot,
  configFile: false,
  appType: 'custom',
  logLevel: 'error',
  server: { middlewareMode: true },
  resolve: {
    alias: {
      // 与 vite.config.ts 保持一致。这是唯一一处需要人工同步的映射，
      // 所以写死在这里而不是去猜路径。
      '@': `${projectRoot}/src`,
    },
  },
})

try {
  await server.ssrLoadModule('/scripts/report-parity.mts')
} catch (err) {
  console.error('\n[一致性检查执行失败]')
  console.error(err)
  process.exitCode = 1
} finally {
  await server.close()
}
