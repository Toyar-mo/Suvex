# v1.38：文件管理页补「上传文件」

## 背景

v1.37 的「文件管理」页照 CyberStrikeAI 的版式做了表格（跨会话列出 / 下载 /
删除对话产物），但**漏了 CyberStrikeAI 工具栏上的「上传文件」按钮** —— 用户
反馈「文件上传功能没做啊」。这一版把它补上：用户在页面上传文件，写进某个
会话的**持久工作区**，之后和其它产物一样可列、可下、可删。

## 为什么不复用 `/attachments`

对话页早就有一个上传端点 `POST /chat/sessions/{id}/attachments`，但那是
**「用户 → 模型」的一次性耗材**：

| | `/attachments` | `/files`（本次新增） |
|---|---|---|
| 方向 | 用户喂给模型看，发消息时把正文读进上下文 | 用户把文件**存进**会话工作区 |
| 落点 | 暂存区 → 发消息后交接进只读 `/work/attachments` | `/work/persist/uploads/`（宿主 `:rw`） |
| TTL | 1 小时，发消息即消费 | 无，随会话归档/删除才清 |
| 用途 | 让模型看一份文件 | 存档 / 在「文件管理」页管理 |

「文件管理」页要的是后者：传完不用马上发消息，之后在列表里能看到、能下载、
能删。两者语义不同，硬用一个会两头别扭（附件那条还有"暂存满了"的护栏，
对存档用途是干扰）。所以新增独立的 `POST /chat/sessions/{id}/files`。

## 后端改动

### `app/chat/workspaces.py` — `save_file()`

新增 `SavedFile(path, name, size_bytes)` 与 `save_file(session_id, filename, content)`：

- 落 `<workspace>/uploads/`（宿主侧），与模型自己写的 `reports/`、`scripts/`
  分开，谁是谁一目了然；
- 文件名过 `attachments.sanitize_name`（同一套白名单：去 `/` `\`、去控制字符、
  去前导点、截断超长），**不信任**浏览器给的 `filename`；
- 空内容 → `ValueError("文件是空的。")`；
- 超过 `MAX_DOWNLOAD_BYTES`（512MB，与下载同一上限）→ `ValueError`；
- `open(candidate, "xb")` **原子**创建（并发上传同名也不互相覆盖），撞名改
  `<stem>-<8位hex>.<ext>`，最多 100 次否则报错（磁盘乱了就该让人看见）；
- 落盘后 `resolve()` 复核仍在工作区内，否则 `unlink` + `ValueError`。

### `app/chat/schemas.py` — `ChatFileUploadOut`

`path / name / sizeBytes`（camelCase）。

### `app/chat/router.py` — `POST /chat/sessions/{session_id}/files`

`201`，`multipart/form-data`，字段名 `file`。`get_session_or_404` → 归档会话
400「这个会话已归档，不能再往工作区里放文件。」→ `await file.read()` →
`save_file`，`ValueError` 翻成 400。

### `app/auth/deps.py` — CSRF multipart 登记（关键坑）

`enforce_csrf` 拒绝**一切** `multipart/form-data` 写请求，只有显式登记在
`MULTIPART_WRITE_ENDPOINTS` 里的端点放行。漏登记的症状是上传被 400
「写操作必须使用 Content-Type: application/json」挡掉 —— 那条消息与真正原因
（忘了登记）看起来毫无关系。**本次确实踩到了**：`tests/chat/test_files_router.py`
的上传用例先返回 400，才想起登记表。

登记表用的是**子路由模板**（`request.scope["route"].path`，**不带** `/api/v1`
前缀），参数名必须逐字对齐 `{session_id}`。已追加：

```python
("POST", "/chat/sessions/{session_id}/files"),
```

自检由 `tests/test_findings_p6.py::test_...` 扫 OpenAPI 逐个核对登记表，漏登记
会红（本次它也确实拦了一下，直到登记后才绿）。

## 前端改动

### `src/data/files.ts` — `uploadFile(sessionId, file)`

`FormData` + `form.append('file', file, file.name)`，走 `api.upload`（multipart）。

⚠️ **不能** `api.post(path, formData)`：`post` 会 `JSON.stringify` 请求体，
而 `JSON.stringify(new FormData()) === "{}"` —— 服务端收到一个没有文件的请求
却**不报错**，只是列表是空的。`api.upload` 是专门为 multipart 留的方法。

### `src/pages/FilesPage.tsx` — 「上传文件」

- 工具条最左加「上传文件」按钮（primary）+ 隐藏 `<input type="file" multiple>`；
- **工作区按会话隔离，这一页没有"当前会话"**，所以上传目标取筛选栏里的
  「会话 ID」（`draft.sessionId || applied.sessionId`）；没填就 toast 提示
  「先在上面填一个会话」；
- 支持多选，逐个上传，成功计数 toast，失败按文件名汇总 toast；
- 上传完成后 `load(applied)` 刷新列表。

## 测试

- `suvex-api/tests/chat/test_files_router.py` 新增 `TestSaveFile`（5 项）+
  `TestUploadEndpoint`（4 项）。`pytest tests/chat/test_files_router.py` →
  **18 passed / 30.29s**。
- 连同 `test_workspaces.py`、`test_findings_p6.py`（multipart 自检）→
  **71 passed, 1 skipped / 85.91s**。
- 前端：`src/data/files.test.ts` 新增 2 项（multipart 字段名、会话 id 编码）→
  `vitest run src/data/files.test.ts src/navigation.test.ts` → **21 passed**；
  `tsc -b` 通过；`eslint` 改动文件 0 error；`npm run build` 成功
  （产物 `dist/assets/FilesPage-DyVsq7NM.js`，含「上传文件」字样）。

## 限制

- 上传只进**会话工作区**（`uploads/`），不是"平台文件库"；要跨会话共享仍得
  靠复制路径或下载再传。
- 单个文件 512MB 上限（与下载同一闸门）。
- 本页仍**不含**从会话工作区"移动/重命名"之类的高级文件操作。
