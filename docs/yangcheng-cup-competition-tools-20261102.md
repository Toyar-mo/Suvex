# 羊城杯比赛日主链路升级（v1.46）

> 用户指令（m03871）：**「全升级，我主要比赛那天是用对话页面」**
> 前置评估：`docs/yangcheng-cup-agent-assessment-20261102.md`

## 背景：为什么"对话页"是主链路，而它原先打不了比赛

比赛当天（2026-11-07 12:00–15:00 Agent 赛道；测试赛 11-03 14:00–20:00）用户主要**用对话页**操作。
但对话页的 16 个工具里：

- `list_challenges` / `start_ctf_solve` —— 操作的是**本平台 `KernelChallenge` 题库**（`/ctf2` 看板那条链路），**不是**比赛平台（`ycb-test.dasctf.com`）的题；
- **没有**任何工具能取比赛平台的题、也**没有**工具能交 flag。

所以对话页 Agent 面对比赛平台是"瞎的"。本次（v1.46）补上原生工具，并落地评估报告里的 P0/P1。

## 一、新增两个原生比赛工具（方案 A）

### `fetch_competition_challenges`
- 拉比赛平台题库：题号（`remoteId`）、标题、分值、分类、是否已解、是否已开放。
- 给 `remoteId` 就顺带查那道题的**靶机连接信息**（IP/端口/账号密码），省一次工具往返。
- 实现：`app/chat/competition.py::list_tasks` → `SlabMatchDriver.fetch_challenges`。

### `submit_flag`
- 把答案提交到比赛平台。
- **自动剥 `DASCTF{}` / `flag{}` 花括号**（赛事只收花括号内的内容；交整串会判错白扣次数）。
- 四态返回，都翻成人话：
  - `is_solved` → ✅ 正确，别再重复交；
  - `already_solved` → 早已解出；
  - `accepted=False`（平台没受理/限流）→ ⚠️ **没有消耗错误次数**，可重试；
  - 受理但判错 → ❌ 真错一次。
- 每题最多提交 50 次（赛事规则），超 `answer_max_length` 时提前拦，不浪费额度。

### 失败翻译（`friendly_driver_error`）
比赛当天最常见的失败不是"网络断"而是"accesskey 没开通/配错"——平台会回 `无权操作`
这类业务错误。本次把这种情况**单独翻成可行动的话**：

> 比赛平台拒绝了这次请求（accesskey 没有交易/agent 接口的权限，或者绑错了账号）。
> 请到「设置 → 解题 / 比赛平台」确认 AccessKey 是本队的比赛账号发的、且已在平台上启用 agent 访问。平台原话：无权操作

而真正的链路错误（超时等）保持原样，不会误导用户去改配置。

## 二、方案 B：提示词教 run_shell + curl 兜底

`app/chat/prompts.py` 新增一节「## 打比赛（羊城杯这类平台）」：
- 首选原生工具路径；
- 兜底：`run_shell` + `curl -H "X-Agent-AccessKey: $KEY" .../slab-match/api/v1/agent/ctf/exercise-list`，
  提交 `POST .../answer-panel/answer -d '{"exerciseId":123,"flag":"花括号内内容"}'`；
- 明确警告：curl 路只看 `code=="00000"`（HTTP 200 也可能是失败）、自己剥花括号、**别把 accesskey 写进可下载产物**。

依据：对话沙箱 `unrestricted=True` → `--network suvex-agent`（`runner.py`）**有出站网**，curl 打比赛 API 可行。

## 三、评估报告的 P0/P1 落地

| 项 | 结论 | 改动 |
|---|---|---|
| **G2 错误提交上限 10→50** | ✅ 已改 | `app/kernel/service.py` `MAX_WRONG_SUBMISSIONS_CEILING = 50`；`app/settings/models.py` CHECK 字面量 `<= 50`；**新迁移** `alembic/versions/20261102_1900_f2c8a71d9b64_wrong_submissions_ceiling_50.py`（drop+create CHECK）。 |
| **G3 flag 外壳剥离** | ✅ 已改（**只在比赛平台提交边界**） | `competition.strip_flag_shell`。**不动**全局 `validate_answer` —— 证据：`tests/kernel/test_runner.py:316` 断言 CTF2 路径保留 `DASCTF{...}`，改全局会破坏内置 CTF2。 |
| **G1 driver_for 接线** | 本次不接线，但**对话路径已绕开**它 | `competition.resolve_driver`：先试 `driver_for`，返回 None 时直接造 `SlabMatchDriver`（比赛场景要的就是它），不依赖那条"下一轮才接"的配置驱动路径。 |
| G4 并发配额 | 已是独立配额（v0.22 修） | 仅 docstring 过时（G7），不影响比赛。 |

## 四、验证（全部真跑）

### 单元/回归测试
- `tests/chat/test_competition_tools.py` → **29 passed**：
  - `strip_flag_shell` 表驱动 13 例（剥 `DASCTF{}`/`flag{}`/空格；不剥 `abc123`/`cat /flag`/IP/时间戳/哈希/嵌套/空壳/空串）+ UUID 长 flag；
  - 两个工具无平台时的可行动报错；假驱动验 list / 带靶机 / submit 剥壳 / 判错 / 未受理不扣次数 / 空 / 超长；
  - `friendly_driver_error`：平台拒绝 → 指向 accesskey（不出现"网络"）、英文 permission 词同样识别、超时保持通用文案；
  - 工具声明 + `_TOOL_IMPLS` 注册一致（16 declared / 16 impl）。
- `tests/kernel/test_wrong_submission_ceiling.py` → **11 passed**：
  - 天花板 == 50；`_assert_wrong_ceiling` 接受 0/1/3/10/49/50、拒绝 -1/51/100；
  - **真写库**：`max_wrong_submissions=50` 通过、`=51` 触发 `IntegrityError`（迁移真的生效的运行时证据）。
- **回归 bundle**（competition + xml_actions + kernel/test_schemas + wrong_ceiling + test_chat）→ **101 passed**。
- **全量** `pytest tests/chat tests/test_chat_stream.py` → **362 passed, 1 skipped**。
- ruff：新文件全过；`engine.py` 仅剩 **8 条预存告警**（HEAD 上同样存在，本次未新增）。

### 部署 Kali（复制部署）
- 备份：`/root/suvex-sandbox-backups/v146-20261102-062514/`。
- 安装 6 个文件（`app/chat/{competition,engine,prompts}.py`、`app/kernel/service.py`、`app/settings/models.py`、迁移文件），**prod sha 与本地逐一相等**。
- `alembic upgrade head`：`Running upgrade b8d3f52a67e1 -> f2c8a71d9b64`；`alembic_version=f2c8a71d9b64`；**DB 约束实查**：`CHECK (((max_wrong_submissions >= 0) AND (max_wrong_submissions <= 50)))`。
- `systemctl restart suvex-api` → `Application startup complete`、39 张表、`is-active=active`、`GET /api/v1/health`=200。

### 线上验证（prod venv + 真 HTTP）
- 工具注册：`GET /chat/tools` 返回 **16 个**，含 `fetch_competition_challenges`、`submit_flag`。
- **能力边界（真实）**：线上有一个 active 的 `KernelAccount`（平台自己的 CTF2 账号，`platform_id=None`、token 非空），`resolve_driver` 退化成 `SlabMatchDriver` 打默认 `ycb-test.dasctf.com`，平台真实返回 `无权操作` → 工具翻成上文的可行动提示（`HAS_ACCESSKEY_HINT=True`、`HAS_SETTINGS_HINT=True`）。
- 这说明：**接口链路已通到比赛平台**，只等比赛账号的 accesskey 授权；一旦绑定正确 accesskey，即可取题/交 flag。
- 清理了 2 个验证会话。

## 五、比赛当天怎么用（对话页）

1. 「设置 → 解题 / 比赛平台」绑定本队比赛账号的 **AccessKey**（`X-Agent-AccessKey`），地址 `https://ycb-test.dasctf.com`。
2. 对话页直接说："列出比赛平台的题" → 模型调 `fetch_competition_challenges`。
3. 解出后："把 `<remoteId>` 的 flag 交上去：`DASCTF{...}`" → 模型调 `submit_flag`（自动剥壳、计额度、失败可诊断）。
4. 若 accesskey 没开通 agent 权限，工具会直接告诉你，而不是让模型瞎猜。

## 六、未做（如实记录）

- **G1 配置驱动 `driver_for` 真正接线**：仍靠对话路径绕开，没动那条链路（风险：配置驱动平台在 `/ctf2` 看板路径上仍未生效；比赛用对话页不受影响）。
- **G6 审计导出 JSONL**（对话记录备审）：尚未做。当前依赖数据库 `chat_messages` 与容器 `/work/persist` 产物。
- **G5 回合/并发调优**：`MAX_TOOL_ROUNDS` 已在上个版本 v1.43 放开到 100000；并发 `maxConcurrentSolves` 默认 3 可调。本次未再动。
- **前端工具列表展示**：`/chat/tools` 已含新工具；对话页工具清单渲染随另一条未提交 UI 分支，未单独改动。
