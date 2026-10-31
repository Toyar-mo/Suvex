# 对话附件超限提示显示「请求失败」的修复（v1.38.1）

## 现象

用户在**对话页**点「附件」传一个 96.9 MB 的 zip，页面弹出：

> 请求失败，请稍后重试。

这句话**没有任何信息量**——它既不说文件太大，也不说上限是多少、该往哪儿传。用户以为平台坏了。

## 根本原因

`suvex-web/src/data/chat.ts` 的 `uploadAttachment()` 里有两处前端预判（超过 5 MB / 空文件），都用了**类型断言**把裸对象当错误塞进 state：

```ts
// 修复前（chat.ts:988-1002）
patch({
  error: {
    code: 'VALIDATION_FAILED',
    message: `文件太大（…）。单个附件上限 5 MB。`,
  } as ApiError,          // ← 只是骗过 TypeScript，运行时是裸对象
})
```

而页面渲染错误条走 `errorMessage()`（`suvex-web/src/lib/api.ts:335-339`），它的第一步是：

```ts
if (error instanceof ApiError) return error.message   // 裸对象 → false
if (error instanceof Error && error.message) return error.message
return '请求失败，请稍后重试。'                          // ← 落到这里
```

所以精心写好的「文件太大…上限 5 MB」文案被丢掉，用户看到的是兜底句。

这与 v1.22 在 `TURN_ERROR` 分支（`chat.ts:724-731` 的注释）已经踩过、并明确警告过的坑是**同一个**——只是上传预判这两个分支漏改了。

## 修复

把两处 `{...} as ApiError` 改成 `new ApiError(code, message, status)`，并给超限文案补上**下一步行动**（对话附件上限 5 MB；大文件去「文件管理」页，上限 512 MB）：

```ts
// 修复后
patch({
  error: new ApiError(
    'VALIDATION_FAILED',
    `文件太大（${(file.size / 1024 / 1024).toFixed(1)} MB）。对话附件是给模型看的一次性文件，` +
      `单个上限 5 MB。若想传大文件，请到左侧「文件管理」页上传（上限 512 MB）。`,
    400,
  ),
})
```

空文件分支同理改成 `new ApiError('VALIDATION_FAILED', '文件是空的，没有内容可以给模型看。', 400)`。

## 回归测试（G0 RED → GREEN）

`suvex-web/src/data/chat.test.ts` 新增 `describe('上传预判的报错必须是真 ApiError（v1.38.1）')`，两条：

1. **超过 5 MB 不发请求 + error 是 ApiError + 文案含「5 MB」且不含「请求失败」**：断言 `err instanceof ApiError`、`err.message` 含 `5 MB`、`calls` 里没有任何 `/attachments` 请求（预判在网络之前短路）。
2. **空文件的 error 也是 ApiError 实例**。

RED（改源码前）：2 failed，`expected { code: 'VALIDATION_FAILED', … } to be an instance of ApiError` —— 精确复现线上现象。
GREEN（改源码后）：2 passed。

全套：`npx vitest run src/data/chat.test.ts` → **40 passed**；`npx tsc -b` 通过；`npx eslint src/data/chat.ts src/data/chat.test.ts` 0 error；`npm run build` 成功。

## 部署与线上验证

- 构建产物：`ChatPage-COZyANyj.js`、`index-sUeye8IQ.js`。
- 部署方式：**tar 打包 → 解到临时目录 → `find -mindepth 1 -delete` 清空挂载点内容 → `cp -a` 填入**，**绝不 `rm -rf` dist 目录本身**（v1.38 时曾因删目录破坏 nginx bind mount 导致站点 404）。部署后 `findmnt` 确认挂载完好，站点 200。
- 线上实测（Playwright，viewer `zzviewer88`）：对话页新建会话 → 点「附件」→ 选 97 MB 文件 → 弹出正确文案：

  > 文件太大（97.0 MB）。对话附件是给模型看的一次性文件，单个上限 5 MB。若想传大文件，请到左侧「文件管理」页上传（上限 512 MB）。

  且 network 无任何 `/attachments` 请求。截图：`artifacts/chat-upload-guard-v1381.png`。

## 附带的配置调整（同一轮）

- `client_max_body_size` 从 200m 提升到 **512m**（`/etc/nginx/sites-available/suvex-frontend.conf:73`），与 `workspaces.py:46 MAX_DOWNLOAD_BYTES = 512MB` 对齐。实测：210 MB 上传 201 通过（旧配置会 413）、513 MB 413。`nginx -t` 通过、reload 成功。
- 确认 `/etc/fstab` 的 bind mount 行（`/root/suvex/suvex-web/dist /opt/suvex/suvex-web/dist none bind 0 0`）真实有效：umount 后仅用 `mount /opt/suvex/suvex-web/dist` 即可挂回、站点恢复 200。

## 分工说明（两个上传入口别混）

| 入口 | 路由 | 上限 | 用途 |
|---|---|---|---|
| 对话页「附件」 | `POST /chat/sessions/{id}/attachments` | **5 MB** | 给模型看的一次性附件（1h TTL、发消息即消费） |
| 文件管理页「上传文件」 | `POST /chat/sessions/{id}/files` | **512 MB** | 存进会话工作区 `uploads/`，长期留存、可列/可下/可删 |

大文件应走「文件管理」页——现在超限提示会直接告诉用户这一点。
