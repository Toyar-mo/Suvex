# 对话沙箱产物下载（v1.36）

## 背景：用户问"附件在沙箱，我在平台怎么下载"

2026-10-30，用户发来一张对话截图：某一轮里模型解出了 flag、写好了一份
CTF pwn 报告，最终答复里写"平台工具清单里没有提交报告的接口，所以报告
停在文件层面"。用户接着问「附件在沙箱，我在平台怎么下载呢」。

核查结论（当时）：

- 对话沙箱的工作目录 `/work` 是**容器内部**的（`sandbox.py` 当时
  `workspace_dir=None`，不挂宿主）；容器被空闲回收后 `/work` 里的文件
  一起消失。实机 `docker ps` 已看不到那个会话的容器 —— **文件确实没了**。
- 平台**没有**从沙箱往外拿文件的端点：`/chat/sessions/{id}/attachments`
  只能"用户 → 沙箱"（上传），不能"沙箱 → 用户"（下载）。
- 所以唯一能挽回的是聊天消息里的报告**正文**（可以复制），文件本身取不回来。

## 设计：会话级宿主工作区 + 下载端点

给每个对话会话在**宿主**上建一个目录，以 `:rw` 挂进容器
`/work/persist`（复用 `app/kernel/runner.py` 里 `workspace_dir` 的既有能力，
解题侧早就在用）。于是：

1. 模型写进 `/work/persist/` 的产物**不随容器生灭**（容器回收后仍在宿主）；
2. 平台**直接从宿主目录**读文件下载（不需要 `docker exec`，容器没了也能下）；
3. 会话归档 / 彻底删除时，连同这个目录一起删。

新增/改动文件：

| 文件 | 改动 |
| --- | --- |
| `suvex-api/app/chat/workspaces.py` | **新增**。`ensure_workspace` / `list_files` / `resolve_file` / `drop_workspace`；`PERSIST_MOUNT_DIR="/work/persist"`；根目录 env `SUPEX_CHAT_WORKSPACES`（默认 `tmp/suvex-chat-workspaces`） |
| `suvex-api/app/chat/sandbox.py` | `_new_sandbox` 里 `workspace_dir=chat_workspaces.ensure_workspace(session.id, create=True)`（原为 `None`）；建不出来则退回不挂、不阻断启动 |
| `suvex-api/app/chat/router.py` | 新增 `GET /chat/sessions/{id}/files`（列表）、`GET /chat/sessions/{id}/files/download?path=...`（下载）；`archive_session` / `purge_session` 清理工作区 |
| `suvex-api/app/chat/schemas.py` | 新增 `ChatFileOut`（path / name / sizeBytes / modifiedAt） |
| `suvex-api/app/chat/prompts.py` | 新增「给用户下载的产物：放 `/work/persist/`」一节，明确"要留存/下载的产物必须写到 `/work/persist/`，其余位置的容器回收即失、平台下载不到" |
| `suvex-api/tests/chat/test_workspaces.py` | **新增**：挂载点跨模块一致、目录创建、列文件、路径安全（`..`/绝对/目录/盘符/符号链接逃逸）、HTTP 列表与下载、越界 404 |

## 安全边界（下载端点）

全部在 `workspaces.resolve_file`：

- 只允许会话工作区之下的**普通文件**；拒绝 `..`、拒绝带冒号的段（Windows 盘符 / ADS）、
  `resolve()` 后再确认仍在工作区内（挡符号链接逃逸）、只接受 `is_file()`、超
  `MAX_DOWNLOAD_BYTES`（512MB）拒。
- 会话归属由 `get_session_or_404(created_by=...)` 保证 —— 别人的会话是 404。
- 列表跳过目录与符号链接。

## 验证（本机真跑）

- `pytest tests/chat/test_workspaces.py -q -o addopts= -p no:cacheprovider`
  → **21 passed, 1 skipped**（跳过的是 Windows 建符号链接需要特权）。
- `pytest tests/chat tests/test_chat_stream.py` → **297 passed, 1 skipped / exit 0**
  （原 277 + 新增 20）。
- 前端（改动在未被提交的 UI 分支里，见「限制」）：
  `vitest run src/lib/workFiles.test.ts src/components/chat/MessageRow.test.tsx
  src/components/chat/AssistantTurn.test.tsx` → **21 passed**；
  `tsc -b` 通过。

## 部署与线上验证

- 后端 5 个文件复制到 Kali `/root/suvex`，重启 `suvex-api`，轮询
  `Application startup complete` 后 `GET /api/v1/health` = 200。
- 线上用真实会话验证：登录 → 建/取一个会话 → 在宿主
  `<workspaceRoot>/<session_id>/` 放一个文件 → `GET .../files` 能看到 →
  `GET .../files/download?path=` 拿回字节一致。
- 生产建议把 `SUPEX_CHAT_WORKSPACES` 指到持久盘（默认在 `/tmp`，重启即失）。

## 限制 / 未做

- **前端"点链接下载"**：`workFiles.ts` + `MessageRow` 的链接渲染已完成并通过
  单测/类型检查，但对话页 UI（`ChatPage.tsx`、`MessageRow.tsx`、`AssistantTurn.tsx`
  等）目前是另一条未提交分支的新文件 —— 为不把那条分支混进本次后端提交，
  前端链接化随该 UI 分支一起落。后端端点与提示词已生效：模型新产物会写进
  `/work/persist/`，用 `GET /chat/sessions/{id}/files` 即可列/下。
- **历史产物救不回来**：功能上线前被回收的沙箱，`/work` 里的文件已经没了。
- 工作区目录随会话删除清理；不做全局定时清理（与附件同一取舍）。

## 部署记录（2026-10-31 生产 Kali `/root/suvex`）

- 5 个文件覆盖后，生产 sha 与源逐一相等：`workspaces.py ef06548b…c9`、
  `sandbox.py aa4e29cc…47`、`router.py 4c0f8b5e…5c`、`schemas.py 015edea4…e0`、
  `prompts.py 88b655da…2c`。
- `/etc/suvex/api.env` 增加 `SUPEX_CHAT_WORKSPACES=/root/suvex/chat-workspaces`
  （持久盘，避免默认 `/tmp` 重启即失），目录权限 755。
- 重启：日志出现 `Application startup complete`（10-31 02:01:56）、
  `39 张表`、无 traceback；`is-active=active`；`GET /api/v1/health` = 200
  `{"status":"ok","version":"0.1.0"}`。
- 线上复现（viewer 登录，临时会话 `chat_01M688TSXC7GMWTJGWAN7M20DR`）：
  - 宿主放 `reports/pwn-分析.md`（93B，sha `d855763a…baa`）；
  - `GET /chat/sessions/{id}/files` → 200，
    `[{"path":"reports/pwn-分析.md","sizeBytes":93,...}]`；
  - `GET /files/download?path=reports/pwn-分析.md` → 200、93B，
    下载文件 sha `d855763a…baa`（与宿主一致）；
  - `path=../../etc/passwd` → 404；
  - `purge` 后宿主工作区目录自动消失（`drop_workspace` 生效）。
- 沙箱接线实测：`sandbox._new_sandbox(SimpleNamespace(id=...))` 的
  `workspace_dir = /root/suvex/chat-workspaces/<sid>`（确实读到了 env）。
- **真实挂载实测**：起一个沙箱容器，在容器里对 `/work/persist/reports/live.txt`
  写 `live-through-mount`，宿主 `<workspaceRoot>/<sid>/reports/live.txt`
  立即读到同样内容（rc=0）；`drop_sandbox` 后容器与宿主目录都清理干净。

