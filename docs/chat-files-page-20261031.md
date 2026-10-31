# 文件管理页（v1.37）—— 跨会话的对话产物索引

> 目标：照 CyberStrikeAI v1.17.17「文件管理」页的信息架构，做一个**把对话产出的报告/脚本/证据当一等公民**的页面。
> 用户原话（m00982）：`做一个文件管理页，你看CyberStrikeAI的做`。
> 前置：v1.36 已让对话沙箱的 `/work/persist` 映射到宿主 `$SUPEX_CHAT_WORKSPACES/<session_id>/`，并提供了会话级列举/下载端点。本页在它之上做**跨会话汇总**。

## 1. 为什么单开一页

对话页是「按时间线读一次交互」，文件管理是「按文件横向检索」——两者正交。
用户常记得"我让它做过一个 pcap 分析"，却不记得在哪个会话里；让他逐个会话翻记录找路径，是把检索成本推给用户。这一页就是那个索引。

## 2. 信息架构（对齐 CyberStrikeAI）

- 筛选：会话 ID / 项目 ID / 文件名 / 来源（全部｜工作目录）
- 分组方式：不分组（平铺）｜按会话｜按项目
- 工具条：搜索、导出当前结果（CSV，带 UTF-8 BOM）、刷新
- 表格列：日期 / 会话 / 子路径 / 文件名 / 来源 / 大小 / 修改时间 / 操作
- 行操作：复制路径（复制 `/work/persist/<path>`，粘给模型即可引用）、下载、删除（二次确认）
- 空态 / 错误态 / 加载骨架齐全；`PageHeader` 复用全局标题与说明。

## 3. 后端

### 3.1 新端点

`GET /api/v1/chat/files`
- query：`sessionId`（可选）、`projectId`（可选）、`name`（可选，子串、忽略大小写）、`source`（`all`｜`workspace`，其它 → 400）
- 返回：`list[ChatFileItemOut]`，按 `modifiedAt` 倒序，上限 `MAX_LISTED_FILES=5000`
- 语义：给定 `sessionId` 时只列该会话（并 `get_session_or_404`）；否则列当前用户名下所有未归档会话；`project` 精确匹配；每行 `source="workspace"`。

`DELETE /api/v1/chat/sessions/{session_id}/files?path=<相对路径>`
- 204；`resolve_file` 后 `unlink`；文件不存在/越界 → 404；删除失败（OSError）→ 400。

### 3.2 Schema

```python
class ChatFileItemOut(CamelModel):
    sessionId: str
    sessionTitle: str
    project: str | None = None
    source: str
    path: str
    name: str
    sizeBytes: int
    modifiedAt: float
```

### 3.3 安全

沿用 `app/chat/workspaces.py::resolve_file` 的防护：只允许会话工作区内、拒空/`.`/`..`/带冒号段/绝对路径、`resolve()` 后必须仍在工作区内、仅普通文件、单文件 ≤512MB。跨用户会话不可见（列会话按 `created_by` 过滤，取文件前 `get_session_or_404`）。

## 4. 前端

- `src/data/files.ts`：`listFiles(filters)`（空条件不下推 URL）、`deleteFile`、`formatBytes/formatTimestamp/formatDate`、`subPathOf`（根目录 → `(根目录)`）
- `src/pages/FilesPage.tsx`：上述 UI；下载链接复用 `src/lib/workFiles.ts::workspaceFileUrl`
- `src/navigation.ts`：新增 `files` 导航项（`FolderOpen`，不进移动底栏）
- `src/pages/index.tsx`：`export const FilesPage = lazy(...)`
- `src/App.tsx`：`PAGE_COMPONENTS` 增加 `files: FilesPage`（**漏键 = 侧栏点得动、页面全白**，`navigation.test.ts` 会断言）

## 5. 测试

- 后端 `suvex-api/tests/chat/test_files_router.py`：9 项（跨会话列举带会话身份、`sessionId`/`name` 过滤、未知 `source` 400、归档后消失、删除成功、删除缺失 404、穿越 404、他人会话 404）→ **9 passed / 16.8s**
- 后端回归 `tests/chat/{test_files_router,test_workspaces,test_session_lifecycle,test_turns_api,test_sandbox}.py` → **79 passed, 1 skipped / 93.4s**
- `ruff check app/chat/router.py app/chat/schemas.py`：仅剩**预存** `encode_frame` F401（非本次引入）
- 前端 `src/data/files.test.ts` 10 项 + `src/navigation.test.ts` → **19 passed**；`tsc -b` 通过；`eslint` 改动文件 0 error；`npm run build` 成功（`FilesPage-C_aAH8ly.js`）

## 6. 部署（Kali `/root/suvex`，复制部署）

- 备份生产 `router.py`/`schemas.py` → `*.bak-20261031-151021`
- `suvex-sandbox install -m 644` 覆盖，生产 sha 与本地一致：
  - `router.py  = 8ebe89129c5549a985193915e9ed69da436d668cd40da4576275428f46138bd4`
  - `schemas.py = cb6f793968c65716b4735702964a027b794c682428c598a0899a3273a010e0d4`
- 前端 `dist` 整份替换（含 `FilesPage-C_aAH8ly.js`），`chown -R kali:kali`
- `systemctl restart suvex-api` → `Application startup complete`（10-31 03:11:10）、`39 张表`、无 traceback、`is-active=active`
- `GET /api/v1/health` → 200 `{"status":"ok","version":"0.1.0"}`

## 7. 线上验收（真实产物）

- API 实测（viewer 登录，cookie `/tmp/ck.txt`；演示会话 `chat_01M68CTR6Z6HTWSDT9QWHKEH6W`，宿主工作区放了 3 个文件）：
  - `GET /chat/files` → 3 行，字段 camelCase `path/name/sizeBytes/modifiedAt` 正确
  - `GET .../files/download?path=reports%2Fpwn-writeup.md` → 200、43 字节、内容一致
  - `path=..%2F..%2Fetc%2Fpasswd` → 404
  - `DELETE .../files?path=nope.txt` → 404
- 页面实测：`http://192.168.44.128:8080/files` 登录后渲染表格（日期/会话/子路径/文件名/来源/大小/修改时间/操作），3 行、下载与删除按钮齐全；截图 `artifacts/files-page-v137.png`（已视觉复核）。
- 浏览器控制台 4 条 403 均为 `settings/{tokens,notices/rule,notices/channels,sandbox}` —— 只读账号访问管理员接口，**预存**、与本页无关。

## 8. 限制 / 未做

- 「点链接下载」在对话消息里的渲染（`MessageRow`/`AssistantTurn`）随另一条未提交的前端 UI 重写分支一起落，本页不依赖它。
- 来源目前只有「工作目录」一种（`source=workspace`）；CyberStrikeAI 的「工具输出」在本系统不是独立存储，暂无第二来源。
- 不含上传：上传入口仍在对话页（`POST /chat/sessions/{id}/attachments`）；本页只做检索/下载/删除。若有需要，后续可加"上传到某会话工作区"。
- 工作区文件随会话**归档/删除**清理（`drop_workspace`），所以本页只索引"存活会话"的产物。
