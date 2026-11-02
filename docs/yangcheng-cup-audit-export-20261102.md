# 羊城杯 P1/P2/P3 收尾（v1.47）

> 承接 `docs/yangcheng-cup-competition-tools-20261102.md`（v1.46 原生比赛工具）。
> 本轮把评估报告里剩下的 P1/P2/P3 落地：**P1 评估（决定不接线）+ P2 审计导出（已实现上线）+ P3 文档修正**。

## P1：`driver_for` 配置驱动接线 —— 评估结论「赛前不接」

完整评估见 **`docs/yangcheng-cup-driver-wiring-assessment-20261102.md`**。一句话：

- `/ctf2` 看板那条老路（`service.sync` / `runner`）直连 `KernelClient`（复合键
  `(practice_id, challenge_id)` / 三级 competition→stage→challenge）；
- 新驱动协议 `PlatformDriver` 是**单一不透明 `handle`**；
- 两者结构不同，接线要么写一层永远对不齐的 adapter，要么重构 4291 行 `service.py`
  + 8911 行 `runner.py` 的身份模型 —— 赛前（测试赛 11-03、决赛 11-07）风险远大于收益；
- **比赛当天主力是对话页**，而对话页（`app/chat/competition.py`）**已经**绕过
  `KernelClient` 直连 `SlabMatchDriver`，不依赖这次接线。

**所以 P1 的落地物 = 评估文档 + 文档修正（P3）**，不动核心链路。

## P2：对话记录 JSONL 审计导出（已实现，已上线）

赛事要求「保留完整 AI 对话记录备审」。此前对话只有页面能看、导不出来。新增：

### 端点

```
GET /api/v1/chat/sessions/{session_id}/export
→ 200 application/x-ndjson，Content-Disposition: attachment; filename="session-<id>.jsonl"
```

- 第一行：会话头 `{"type":"session","id","title","createdAt","project","persona","providerId","archived"}`
- 之后每行一条消息：`{"type":"message","uuid","parentUuid","seq","timestamp","role","surface","text",...}`
  - 可选字段：`reasoning`、`toolName`、`toolArgs`、`toolFailed`、`approvalStatus`/`approvalDecidedBy`/`approvalDecidedAt`
  - `parentUuid` 构成消息父链（首条为 `null`，其余指向上一条 `uuid`），便于按序重放。
- 形状对齐常见编码 agent 的 `.jsonl` 记录习惯（Claude Code 那类），评委可直接读。

### 实现要点

- `app/chat/router.py::export_session`：**流式** `StreamingResponse`（一行一 yield），
  长会话不一次性 dump 全量到内存。
- 只读：不落库、不改会话。**归档会话也能导出**（备审常要旧归档记录），但仍受归属
  校验（`get_session_or_404(created_by=_owner(identity))`，他人会话 404）。
- 时间用 `to_iso`（统一 `...Z` 毫秒格式，与全站一致）。

### 验证（真跑）

- 单测 `tests/chat/test_export_session.py` **5 passed**：头行/消息行、父链首尾相接、
  工具字段（toolName/toolArgs/toolFailed/reasoning/surface）往返、归档可导、缺失 404。
- 全量 `tests/chat tests/test_chat_stream.py` **370 passed, 1 skipped**（原 362 + 新增）。
- **线上实测**（Kali prod，viewer 登录，真 HTTP）：
  - `content-type: application/x-ndjson`、`content-disposition: attachment; filename="session-....jsonl"`；
  - 播种 1 user + 1 assistant(tool) 后导出 **3 行**：session 头 / user「第一句」/ assistant
    「答一句」且 `toolName=run_shell`、`parentUuid` 指向 user 的 `msg_...`；
  - 缺失会话 → **404**；
  - 验证用会话已 archive(200)+purge(200) 清理。

## P3：文档修正（G7）

`app/kernel/drivers/__init__.py` 原写「在 `registry.py` 的 `DRIVERS` 里注册」——
**`registry.py` 从未存在**。已改正为：真正的驱动选择入口是
`app.kernel.platforms.driver_for()`，并注明现状（目前只有 chat competition 在调它，
`/ctf2` 老路仍直连 `KernelClient`），指向 P1 评估文档。

## 部署与验证

- 改动文件（仅 2 个生产文件）：`app/chat/router.py`、`app/kernel/drivers/__init__.py`。
- Kali `/root/suvex`（复制部署）：备份 → `install -m 644` → prod sha 与本地逐一相等：
  - `router.py` = `d2ee46d1445c175012a915595fc34fea175169fd6febc9489868f616c5ed9e4c`
  - `drivers/__init__.py` = `6cb5fb93c3498ab235ab3430c24e41ef939254ce758f81e6335b97f0a2d11354`
- `nsenter ... systemctl restart suvex-api` → `Application startup complete`（11-02 06:45:56）、
  39 张表、`is-active=active`、`GET /api/v1/health` = `{"status":"ok","version":"0.1.0"}`。

## 本轮未做（属于「赛后版本级重构」，非阻塞比赛）

- P1 的**真正** `/ctf2` 接线（`service.sync`/`runner` 改走 `PlatformDriver`）：见评估文档第 5 节，
  建议赛后按「handle + 归属账号」重做题目身份模型，另立版本。
- G5 回合/并发调优：当前 `MAX_TOOL_ROUNDS=100000`（v1.43 已放开进度）、
  `maxConcurrentSolves` 默认 3 可调 1–16，比赛够用，无需再动。
