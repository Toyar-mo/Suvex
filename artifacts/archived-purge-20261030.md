# 归档会话彻底删除（2026-10-30）

## 诉求
用户截图显示会话侧栏「已归档（43）」，指令：**把归档记录全删了**。确认范围后选择：**全库 98 条都删**（admin 43 条 + zzviewer88 55 条）。

## 语义（为什么走 purge 而不是 archive）
- `DELETE /chat/sessions/{id}` 只是归档（软删）。
- `POST /chat/sessions/{id}/purge` 才是物理删除，且要求会话已归档（否则 409 `CHAT_SESSION_NOT_ARCHIVED`）。见 `suvex-api/app/chat/router.py:225-263` 与 `suvex-api/app/chat/service.py:260-292`。

## 先备份（不可恢复前留回退点）
- 文件：Kali `/root/suvex-backups/chat-archived-20261030-123519.json`（2,048,465 字节）
- sha256：`91aab66aef13501ba961938882d23e339b454ca92e4fa5411f320dcb5416dcac`
- 内容：98 条 `chat_sessions` + 1265 条 `chat_messages` + 40 条 `chat_turn_runs`（全列，datetime→isoformat、bytes→base64）。

## 执行
逐条复刻 router purge 的清理顺序：先删 session 级附属表（`chat_turn_runs`）→ `service.purge_session`（删消息+会话并 commit）→ `chat_sandbox.drop_sandbox` → `chat_attachments.drop_session/drop_handed_over`。

结果（脚本回执）：
```
ARCHIVED_TO_PURGE 98
PURGED ok 98 fail 0 deleted_messages 1265
AFTER archived_remaining 0 sessions_total 20 messages_total 264
```

## 校验
- 数据库：归档会话剩余 **0**；会话总数 20（保留的是未归档的活跃会话）；消息总数 264。
- 服务健康：`GET http://127.0.0.1:8000/api/v1/health` = 200。
- 前端（zzviewer88 账号，`http://192.168.44.128:8080/chat`）：侧栏不再出现「已归档（N）」分组（0 条时该分组隐藏），截图 `artifacts/archived-purge-verification.png`。

## 回退方式（如需要）
备份 JSON 含全列快照；按表 `chat_sessions`（主键 id）、`chat_messages`/`chat_turn_runs`（session_id 外键）顺序回插即可恢复。

## 备注
- 归档时绑定的 sandbox 与附件在当初归档时已销毁；本次 purge 再清一次为幂等兜底。
- 此为物理删除，不可通过界面恢复；唯一回退是上面的备份文件。
