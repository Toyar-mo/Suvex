# 「问一句停一次 / 一直在循环」根因与修复（v1.49）

2026-11-02。用户截图反馈：对话页每一轮都停在「继续」按钮上，点继续也没用；
另一处显示模型输出 `</｜DSML｜ parameter>`、`</｜DSML｜ invoke>` 这类标记，正文
反复「执行。<result>ok</result>」，13 步 4 工具跑了 22 分钟。用户原话：
「一直在循环」「问一句停一次，你改动的啥」。

## 现场（线上会话取证）

会话 `chat_01M6BWQ69MZJNBAB7C7QMVXPV2`（模型 deepseek-v4.1-flash）的
`chat_turn_runs` 实查：

| turn | status | round_index | tools_used | nudges_used | 备注 |
|---|---|---|---|---|---|
| `turn_01M6E99VZ6G3CYW5XY5GAX80WZ` | **running** | 75 | 101 | 115 | 14:06→15:06 心跳仍活，跑了 60 分钟 |
| `turn_01M6EBTPW8HK0A91CTZ7EYET0W` | done | 0 | 0 | 0 | 14:50，约 27 秒 |
| `turn_01M6EBY5YK2TPFJT7SAMQC2ZSY` | done | 0 | 0 | 0 | 14:52，约 27 秒 |

`chat_messages` 里 seq 566、580 是**用户手点「继续」**产生的 `user` 消息。

## 根因（三条，互相放大）

### ① 主因：普通发消息不检查「会话是否已有在跑轮次」→ 并发双 turn

`POST /chat/sessions/{id}/messages/stream`（`router.py:407 send_message_stream`）
入口只校验「会话是否已归档」「内容是否为空」，**不查是否已有 turn 在跑**。
而 `interject`（`router.py:550`）和 `interrupt`（`router.py:598`）都先查
`OPEN_STATUSES` 才动手 —— 只有「发消息」漏了这一道。

后果：本地 SSE 的 done 帧一旦丢了（网络抖动、切页面、关掉再开），前端
`sending` 会变成 false，可服务端那一轮还在跑。用户再发一句 → 起一条**新的**
turn（round=0，无历史进展），老 turn 继续在后台干 → 两条并行。用户看到的就是
「问一句停一次」：新 turn 很快 done（啥也没干），老 turn 继续转。

### ② 旁因：残缺的 `</｜DSML｜>` 标记既不是动作也不被清理

模型自创了第三种收尾标记 `</｜DSML｜ parameter>` / `</｜DSML｜ calls>`（全角竖线
`｜` = U+FF5C）。实测这些**只有收尾标记、没有开标签、没有 `name=`**，命令体是
标记前那段裸文本 —— 无从还原成动作，也没有任何现有归一化逻辑认它。于是模型
反复写「让我写 ACTION / 现在真正运行」，永远产不出可执行动作。

（注：查 seq 599–611 的 tool 结果发现不少命令其实**执行成功了**，
说明那些「停一次」不全是协议问题；但 DSML 噪音确实会误导模型反复空转。）

### ③ 佐因：长 turn 反复换说法不收敛

即使命令在执行，模型也会在同一个死胡同里连跑几十轮 —— 同一条命令、
一模一样的结果，却在期待不同的输出。`round_index` 涨到 75、`tools_used` 到
101，没有任何新信息进账，纯烧 token 与时间。

## 修法（四件，P0–P3）

### P0：入口加「已有在跑轮次 → 409」守卫（`suvex-api/app/chat/router.py`）

`send_message_stream` 在内容校验之后、起流之前：

```python
latest = turn_runs.list_runs(db, session_id=session_id, limit=1)
if latest and latest[0].status in turn_runs.OPEN_STATUSES:
    raise ApiError.conflict(
        "TURN_ALREADY_RUNNING",
        "这个对话还有一轮正在跑。想补一句话用「插话」，想让它停下或改方向用"
        "「中断」，想接手已经断掉的那一轮点「继续」——它们都会接在同一轮上。",
    )
```

- `ApiError.conflict(code, message)` 只接两个参数（不要传 details）。
- 测试 `tests/chat/test_turns_api.py`：`test_sending_a_message_while_a_turn_is_running_is_rejected`
  （有 open turn → 409、code==`TURN_ALREADY_RUNNING`、且没多出 turn）+
  `test_sending_a_message_is_allowed_after_the_running_turn_finishes`
  （turn done 后放行，证明闸不会误伤）。

### P1：DSML 标记兜底归一化（`suvex-api/app/chat/engine.py` + `holdback.py`）

```python
_DSML_TAG_RE = re.compile(r"</?\s*[｜|]\s*DSML\s*[｜|]\s*[A-Za-z_]+[^>]*>", re.DOTALL)
def _is_dsml(text): return bool(text) and bool(_DSML_TAG_RE.search(text))
```

`_normalize_xml_actions` 第一步把 DSML 标签翻译成半角（`</｜DSML｜ parameter>`
→ `</parameter>`，`<｜DSML｜ invoke name="X">` → `<invoke name="X">`，`calls` → 删），
然后交给既有的 `<parameter>`/`<invoke>` 解析链路 —— 开标签齐全的能还原成动作，
只有收尾标记的则被当噪声删掉、不泄漏。`holdback.py` 的 `_verdict` 也把
`｜DSML｜` / `|DSML|` 当协议标记拦下（流式不泄漏）。

正则坑：最初写成 `[A-Za-z_]+\s*>`，漏掉了带属性的
`<｜DSML｜ parameter name="command">`；改为 `[A-Za-z_]+[^>]*>` 保留属性。

### P2：原地打转检测（`suvex-api/app/chat/prompts.py` + `engine.py`）

只认**最硬的信号**：连续 `MAX_REPEATED_ACTIONS`（默认 5）次执行了
**完全相同**的（工具 + 参数）。命中后**一次性**喂回 `render_stall_reminder`
（「你已经把 `X` 这条命令连续执行了 N 次…换个思路 / 承认走不通交用户 /
给诚实结论」），`stall_warned` 保证本轮不重复。

- 签名 `_action_signature(name, args)` 用 `json.dumps(args, sort_keys=True)`，
  字典顺序无关；命令改一个字节就是不同签名（那是"试了不同策略"，不算打转）。
- 不掺长度/语义启发式 —— 技术性长文里误判率太高（见 `nudge.looks_like_plan`
  那些坑）。
- 测试：`tests/chat/test_stall_detection.py`（9 例，签名稳定性/阈值/文案）+
  `tests/chat/test_stall_e2e.py`（2 例，跑 5 次相同动作后第 6 次模型调用确实
  收到提醒、且只收到一次；4 次不触发）。

### P3：前端服务端在跑时按钮变「插话」（`suvex-web/src/pages/ChatPage.tsx`）

本地 `sending` 之外，再读服务端真值：`lastRuns` 里最近一条是 `running` 时
`serverRunning=true`，`turnRunningHere = sendingHere || serverRunning`。此时
输入框 placeholder 提示「这一轮还在跑（服务端）…」，按钮文案是「插话」，
Enter 走 `interject` 而不是 `sendMessage`（避免再起一轮），并露出「中断」出口。

- 测试 `src/pages/ChatPage.test.tsx`：服务端 `running` → 按钮显示「插话」、
  Enter 调 `interject`、`sendMessage` 不被调用。G0 RED：把
  `turnRunningHere` 改回 `sendingHere` → 该用例 FAILED。

## 验证

- 后端 `pytest tests/chat tests/test_chat_stream.py` → **393 passed, 1 skipped**（本
  次新增 18 条：P0 2、P1 5、P2 11）。
- 前端 `npx vitest run` → **1116 passed (75 files)**；`npx tsc -b` 通过；
  `npx eslint src/pages/ChatPage.tsx` 0 error（1 个预存 react-refresh warning）。
- `ruff check` 改动文件：`prompts.py`/`holdback.py`/两个新测试 → All checks
  passed；`engine.py`/`router.py` 的 9 条告警**全部是 HEAD 既有的预存告警**
  （I001/B905/F821×3/F841/F401 `encode_frame`），本次未新增。

## 部署（Kali `/root/suvex`，非 git 复制部署）

后端 4 文件（`app/chat/{engine,prompts,router,holdback}.py`）install 覆盖，prod
sha 与本地逐一核对；`nsenter ... systemctl restart suvex-api` 轮询
`Application startup complete` 后 `GET /api/v1/health`。前端在 Kali
`~/suvex/suvex-web` 上 `npm run build`，产物 `find .../dist -mindepth 1 -delete`
后 `cp -a` 同步（**清内容不删目录**，保护 bind mount）。

（具体 sha 与时间见随部署追加的「部署记录」小节。）

## 未做 / 边界

- 历史已落库的 DSML / `<result>` 文本**不回洗**（只影响未来渲染，不影响执行）。
- 「原地打转」只判**完全相同**的命令；模型换个参数继续撞墙仍可能空转，那是
  语义理解问题，不在硬信号能覆盖的范围（宁可漏判也不误伤正常长任务）。
- 这道具体题能不能解出来，取决于模型本身，不是本次修复的承诺；本次修的是
  「停一次 / 死循环」这类**系统性交互缺陷**。

## 部署记录（2026-11-02 10:38 UTC / 23:38 CST）

- 后端 4 文件 install 到 `/root/suvex/suvex-api/app/chat/`，prod sha 与本地逐一相等：
  `engine.py=6e3f51f4900f0cab…`、`prompts.py=ac03af5c14d3db83…`、
  `router.py=3af9bb1eb51f0d6f…`、`holdback.py=e201a207f3d8a221…`；备份
  `*.bak-v149-20261102-103808`。
- `nsenter … systemctl restart suvex-api` → `Application startup complete`
  （10:38:41）、is-active=active、`GET /api/v1/health`=200 `{"status":"ok","version":"0.1.0"}`。
- 前端 Kali `~/suvex/suvex-web` `npm run build` → `ChatPage-DM436kIK.js`/
  `index-0LfdswnM.js`；`find …/dist -mindepth 1 -delete` 后 `cp -a` 同步，bind
  mount `findmnt` 完好；8080 index 引用 `index-0LfdswnM.js`、`/assets/ChatPage-DM436kIK.js` 200。

## 线上验证

- **P1/P2（prod venv 探针 `/tmp/v149_probe.py`，直接跑生产代码）**：
  `P1_HAS_DSML_AFTER: False`、`P1_PARSED: [('run_shell', {'command': 'id'})]`、
  `P1_DETECT: True`、`P2_MAX_REPEATED: 5`、`P2_SIG_STABLE: True`、
  `P2_REMINDER_HAS_TOOL: True`、`PROD_OK`。
- **P0（线上 HTTP）**：给一个会话直接插一条 `running` 的 `chat_turn_runs`，再
  `POST …/messages/stream` → **409** body
  `{"error":{"code":"TURN_ALREADY_RUNNING","message":"这个对话还有一轮正在跑。想补一句话用「插话」…"}}`；
  验证后清掉那条假 turn。另注：纯 0 轮（本环境无模型）时 send 会秒 done，
  不会误触 409（实际第二次 send 得到 200 是"第一次早已 done"的时序，非缺陷）。
- **UI（Playwright + read_image 复核）**：`zzviewer88` 登录 → `/chat` 正常渲染，
  底部输入框 placeholder「先新建或打开一个对话」、按钮「发送」；新逻辑未破坏
  正常态。截图 `artifacts/chat-v149-verify.png`。
- 清理：两个验证会话 archive+purge 均 200；临时探针脚本已删。
