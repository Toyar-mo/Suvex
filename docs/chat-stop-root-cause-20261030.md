# 对话「跑到一半就结束」：线上证据与修复（2026-10-30）

## 线上库里的两段会话（只读查询）

查询命令（Kali，只读，未改库）：

```
docker exec suvex-db psql -U suvex -d suvex -Atc \
  "select session_id,id,status,round_index,nudges_used,tools_used,truncations_used,tool_nudges_used,last_error \
   from chat_turn_runs where session_id in (...) order by started_at"
```

| session | turn_run | status | round_index | tools_used | nudges | truncations |
| --- | --- | --- | --- | --- | --- | --- |
| `chat_01M6556ES13EGH8NN712252SPB` | `turn_01M6556EW1TSK1RGTKDGFSZ880` | done | 17 | 9 | 9 | 0 |
| `chat_01M64ANA9DQVW1FGWMPEZY6NZD` | `turn_01M64AP77A367R2W7MYHQMNQ0X` | done | 4 | 2 | 2 | 0 |
| `chat_01M64ANA9DQVW1FGWMPEZY6NZD` | `turn_01M64ASB9P8R762QC6VZKTS9TM` | done | 3 | 1 | 2 | 0 |

`chat_messages` 的最后几条（`seq|role|surface|content`）：

```
chat_01M6556ES13EGH8NN712252SPB|19|assistant|process|反汇编到手，逻辑清楚了：...
chat_01M6556ES13EGH8NN712252SPB|20|tool|process|GNU_STACK ... GNU_RELRO ...
chat_01M6556ES13EGH8NN712252SPB|21|assistant|answer|NX 开着（栈 RW 非 E），无 PIE，Partial RELRO，没有 __stack_chk_fail 符号。本地 libc 是 2.43，我先把 gadget 和连通性都确认下来。
```

```
chat_01M64ANA9DQVW1FGWMPEZY6NZD|7|assistant|process|没有 PIE（EXEC 类型），两次 read 输入。我看下反汇编。
chat_01M64ANA9DQVW1FGWMPEZY6NZD|8|tool|process|PIE: No PIE ... canary ...
chat_01M64ANA9DQVW1FGWMPEZY6NZD|9|assistant|answer|主逻辑在 0x40070b，看它。
```

两轮 turn_run 都是 `done`，但最后一条 assistant 是**计划句/过程句**（“我看下反汇编”“我先把 gadget …确认下来”），之后没有工具调用、没有结论。界面因此看起来“跑到一半就结束”，而数据库认为这一轮正常完成。

## 根因

1. **计划句被当成结论**（`chat_01M64ANA...`，seq 9）：`nudge.py` 的计划句判据此前漏掉“我看下…”“我先把…确认下来”这类裸动词/短句。该判据已在工作区修复（`_CLAUSE_BREAK` / `_PLAN_BARE_VERB` / `_RESULT_MARKERS`），本轮未回退，并新增 `tests/chat/test_plan_detection.py` 守着。
2. **原生工具调用被丢弃**（`chat_01M6556...`）：上游 OpenAI 兼容网关回 `choices[0].delta.tool_calls` + `finish_reason="tool_calls"`、正文为空。旧 `providers/llm.py::stream_delta` 只读 `delta.content`，工具调用整段丢失；`kernel/runner.py::LlmReply` 也没有承载它的字段。引擎拿到 `text='' actions=[] finish='tool_calls'`，进空回复分支，额度耗尽后在 `already_spoke and not at_limit` 处静默 return —— turn_run 仍标 `done`。

## 修复后的行为

- `providers/llm.py` 新增 `tool_calls_delta()` / `tool_calls_from_message()`，把 OpenAI `tool_calls` 与 Anthropic `tool_use`（含分片 `input_json_delta`）归一化为 `{index,id,name,arguments}`；认不出返回 `[]`，不抛。
- `kernel/runner.py` 的 `LlmReply` 新增 `tool_calls` 字段；`chat()` / `chat_stream(..., tool_calls=...)` 按 index 累积后回填，不合成文本（避免打断 `extract_json`）。
- `chat/engine.py` 在正文无 `ACTION|FINAL` 标记时，把原生调用合成成以换行开头的 `ACTION: {"tool":...,"args":...}` 行（最多 5 条）；正文里已有的协议文本不重复追加。
- `run_turn` 与 `resume_approved` 的空回复分支：`finish_reason` 属于 `tool_calls/tool_use/function_call` 时，纠偏额度用尽后追加 `render_native_tool_note()` 可见消息再返回，任何情况下不再静默收尾。

守卫用例：`suvex-api/tests/chat/test_native_tool_calls.py`（19 条，覆盖流式/非流式、混合正文、并行调用、坏 JSON、Anthropic 分片、引擎级可见消息与前端 delta 泄漏；含「正文与 `finish_reason` 同帧」回归）。

## 修复后的真实会话证据（2026-10-30 生产复跑）

部署内容（`/root/suvex`，重启后 `Application startup complete`）：

| 文件 | sha256 |
| --- | --- |
| `app/providers/llm.py` | `b679afb32f0f98e97a887969291af675eee36912fc0b32c8dc180eaa04af3d2d` |
| `app/kernel/runner.py` | `3497e0ecd7cdf9dadbd05d8fd03d927235da4d71c82ac92c770eeaccf0227138` |
| `app/chat/engine.py` | `fce44a0d0b5398b6a6de8d541f2089eb3ee4413f66cbc8d256075cb800b91562` |
| `app/chat/prompts.py` | `ec3a6c88a70bca528735b2a132a470d63145baeacc199cecb31e1cc7ff5fea8f` |

### 长任务：不再中途停住

会话 `chat_01M65EVKPRKYYZVJ3Q1D1S9SAW`（标题「CTF pwn 复跑（自动验证）」）连续运行 **18 分钟以上**，
消息数 91+，`tools_used` 30+，phase 推进到 `round 44`，期间持续出现 `run_shell` 与正文流式增量，
**没有**落到「计划句 + 空回复 → 静默 return」的旧出口。该会话在取证时仍在运行，因此不作为
「整轮正常收尾」的证据，只证明**不再提前停住**。

### 短任务：整轮正常收尾

会话 `chat_01M65FKZZHBR20XGBEDD454QDN`（标题「CTF pwn 短任务对照（自动验证）」，只做本地静态分析、不连远程）：

| 字段 | 值 |
| --- | --- |
| turn_run | `turn_01M65FM01ZXCBTYXRNZ0NGC1Q6` |
| status | `done` |
| round_index | 32 |
| tools_used | 28 |
| nudges_used / tool_nudges_used | 3 / 3 |
| truncations_used | 0 |
| last_error | 空 |
| 消息数 | 62 |
| 末条 | `seq=62, role=assistant, surface=answer, length=3353` |

末条正文以「全部已完成，只做了本地静态分析和本地动态验证，**没有连接任何远程**。」开头，
随后给出保护、两次 `read` 的溢出点、偏移 `0xb8 (184)`、gadget、libc 线索与 `/work/exploit.py` 的
结论。工具输出里本地 `PWNED_42` + `uid=0(root)` 已实际拿到（`seq=57`）。

这两条会话证明：原生 `tool_calls` 现在会被执行、空回复不再吞掉整轮、计划句不会被当成结论
落地，且整轮能走到 `done + surface=answer` 的完整结论。远程靶机当前可达性不稳定（`seq=73`
时 9999 端口可连但无 banner），因此「线上对话直接解出 flag」仍受目标环境影响，
与本轮修复的停机问题分开记录。

## 收尾质量：一轮只能有一条结论（v1.33）

上一版上线后，短任务复跑 `chat_01M65JDSK3NFNBW58AD0RAMHC6` 出现了**两条** `surface=answer`：
`seq74`（3393 字）与 `seq77`（1547 字），而 `turn_run.resume_attempts=1`。

### 根因

1. `turn_runs.claim()` 的条件 UPDATE 只检查「状态未结束 + 未到重试上限」，**没有重新检查
   心跳是否仍然过期**。后台扫描在长工具调用期间看到旧心跳，就把一轮正在跑的对话抢走。
2. 原执行体在**本进程**里其实还活着（单 worker 部署，resumer 与请求同进程），于是两个执行体
   各跑一遍、各自落一条结论。
3. `_collapse_turn_answers()` 只看自己的 `result.messages`：接手方看不到原执行体的那条
   `answer`，折叠自然漏掉。

### 修复

- `app/chat/turn_runs.py`：新增进程内活跃租约 `mark_active()/unmark_active()/is_active()`；
  `stale_runs()` 与 `claim()` 都会跳过本进程里正在跑的轮次；`claim()` 的条件 UPDATE 补上
  `heartbeat_at <= now - STALE_SECONDS`，即使跨进程也要二次确认心跳真的过期。
- `app/chat/engine.py`：`_run_guarded()` 在开跑前登记活跃、在 `finally` 里清除；
  `_collapse_turn_answers()` 改为按「会话最后一条用户消息之后的所有 assistant 消息」折叠，
  不再依赖某个执行体的 `result.messages`。后完成者会把对方那条 `answer` 降为 `process`。
- 新增回归：`test_claim_refuses_a_fresh_heartbeat`、`test_claim_refuses_a_turn_active_in_this_process`、
  `test_collapse_demotes_answers_from_another_executor`、`test_collapse_does_not_touch_the_previous_turn_answer`。

### 部署与复跑

| 文件 | sha256 |
| --- | --- |
| `app/chat/turn_runs.py` | `4803e57bb6f689365555fd2828521441bc30bb78d1be24cc6d0548c399de09ab` |
| `app/chat/engine.py` | `33c473bf55020bdfc7efa6d591fabfb5985766dfe3f9bd87cd6e61bbe5a920b9` |

Kali `pytest tests/chat tests/test_chat_stream.py tests/kernel/test_runner.py`：
**477 tests / 0 failures / 0 errors / exit 0**。

生产复跑（同一短任务脚本，重启后 `Application startup complete`）：

| 字段 | 值 |
| --- | --- |
| session | `chat_01M65KWMCFDM3EBM2EDJFNSVSK` |
| turn_run | `turn_01M65KWMG5BKZ9KK6QRFRW5DHJ` |
| status | `done` |
| resume_attempts | **0**（没有再被后台抢走） |
| round_index | 30 |
| tools_used | 33 |
| 消息数 | 65 |
| assistant `answer` 数 | **1** |
| 末条 | `seq=65, role=assistant, surface=answer`，给出完整静态分析结论 |
| 耗时 | 186.1s |

这组数字**不能**单独证明活跃租约生效：这次复跑全程没有超过 90 秒的单步，心跳从未过期，
旧代码在这种情况下同样不会抢走这一轮。真正能区分新旧代码的是下面的受控慢工具复跑。

### v1.33.1：resumer 自己接手的轮次也要登记租约

独立复核发现 v1.33 只补了请求入口：`resumer.resume_one()` 直接调用 `engine.run_turn()`，
不经过 `_run_guarded()`，所以 `mark_active()` 从未执行。resumer 跑长工具时心跳照样会过期，
`is_resumable()` 也照样会返回「可继续」，用户点黄条就会在本进程里再起一个执行体。

修复：

- `app/chat/turn_runs.py`：`is_resumable()` 增加 `is_active()` 判据；`stale_runs()` 改为先过滤
  活跃轮次再截断，避免活跃行把真正过期的行挤出 `LIMIT` 窗口。
- `app/chat/resumer.py`：`resume_one()` 在抢占成功后 `mark_active()`，并用 `finally` 清除；
  覆盖 `run_turn`、折叠、异常记账与 `finish` 的全过程。
- 新增回归：`test_resume_one_marks_the_run_active_while_it_runs`；
  `test_claim_refuses_a_turn_active_in_this_process` 增加 `is_resumable(...) is False` 断言；
  `test_resume_one_releases_the_lease_when_run_turn_raises` 覆盖异常路径的 `finally` 释放。
- 模块说明同步：`turn_runs.py` 顶部不再把 `is_resumable()` 描述为「纯函数」，改为
  「状态/心跳/重试次数 + 本进程活跃租约」，避免下一个人按错误前提改判据。

反向验证：在副本里去掉 `is_resumable()` 的活跃判据与 `resume_one()` 的 `mark_active()` 后，
前两条用例分别报 `assert True is False` 与 `assert [False] == [True]`；把 `finally` 里的
`unmark_active()` 换成 `pass` 后，异常路径用例报 `assert True is False`，退出码均为 1。

### 受控慢工具复跑（心跳过期时手动继续必须被拒）

脚本让模型先执行 `sleep 120`（`timeout_seconds=300`），在工具运行期间等待心跳超过
`STALE_SECONDS=90`，然后调用手动继续接口：

| 字段 | 值 |
| --- | --- |
| session | `chat_01M65P9WJNJYHDTRJTEAHQ1TA3` |
| turn_run | `turn_01M65P9WMNAHH28REM5CEXDCCB` |
| 发起继续时状态 | `running`，`heartbeat_age=95.8s`，`resume_attempts=0` |
| 继续接口 | **HTTP 400** `VALIDATION_FAILED`「这一轮不需要继续（可能已经跑完、或者还在跑）。」 |
| 最终 status | `done` |
| `resume_attempts` | **0**（没有被接手） |
| assistant `answer` 数 | **1** |
| `sleep 120` 工具调用 | **1** 次（没有重复副作用） |
| 消息数 | 6 |

同一脚本在修复前的行为是：心跳过期 + 活跃轮次不被识别 → `resume` 进入 `claim()` 并起第二个
执行体。这次请求在 `is_resumable()` 就被拒，说明「心跳旧但人还在跑」的路径已经被堵住。

### v1.33.1 部署与回归

| 文件 | sha256 |
| --- | --- |
| `app/chat/turn_runs.py` | `d3ced943d77f0a6501f22fb9c4d9e9a381abff89c8649c3e8c4437aa5e32e6a8` |
| `app/chat/resumer.py` | `8463e9ac0abcd5fb1d7fc2d33b4e8fc5e84a023356c66d01fe2c1af27b9afd1b` |
| `app/chat/engine.py` | `33c473bf55020bdfc7efa6d591fabfb5985766dfe3f9bd87cd6e61bbe5a920b9` |

Kali `pytest tests/chat tests/test_chat_stream.py tests/kernel/test_runner.py`：
**479 tests / 0 failures / 0 errors / exit 0**。生产环境已写入上述文件并重启
（`Application startup complete`，`/api/v1/health` 200）。

### v1.34：完成标记之后还挂着「接下来」也要打回

v1.33.1 通过独立复核后仍留下一个同类漏判：`looks_like_plan()` 只要看到
`_RESULT_MARKERS`（`已拿到`、`反汇编如下`、`输出如下`…）就直接返回 `False`，
不再看这句话的结尾。于是下面三句都会以 `surface=answer` 落库并结束整轮：

```text
FINAL: 已拿到 libc 版本，接下来跑 one_gadget。
FINAL: 反汇编如下：main 在 0x40070b。接下来写 exploit。
FINAL: 输出如下：uid=0。接下来读 /flag。
```

修复（`app/chat/nudge.py`）：

- 新增 `_has_trailing_plan()`：按分句扫描，先把完成标记从每个分句里剥掉，再判断剩下的
  部分有没有计划动作。这样既能抓住完成标记**之后**的「接下来」，也能抓住写在完成标记
  **之前**的计划，例如 `接下来写 exploit，已拿到 libc 版本。`。
- 新增 `_SECOND_PERSON_TRAILING`：`已解出 flag{…}，接下来你可以提交。` 是给用户的
  用法说明，不是模型自己的下一步，不打回。
- `_PLAN_BARE_VERB` 允许 `我` 前缀，并单独识别「（我）先把」；修复
  「我看下反汇编。」「我先把 gadget … 确认下来。」的漏判。
- `_RESULT_MARKERS` 补 `分析如下`，避免「我分析如下」被当成计划。

回归：`test_plan_detection.py` 新增完成标记后接计划、计划写在完成标记前、真结论不打回、
第一人称裸动词、辅助函数空边界五组；`test_turn_persistence.py` 新增引擎级用例
`test_plan_after_a_result_marker_is_bounced`。独立复核后又补两组：
`test_last_step_wording_is_a_plan`（`还差最后一步：打远程…`、`接下来连接靶机…`、
`最后一步：上传 exploit…`）与 `test_conclusion_heading_is_not_a_plan`
（`已解出 flag{…}，接下来是结论：偏移 0xb8。` 不能打回），并给 `_PLAN_START` 加
`还差|差一步|最后一步`、`_PLAN_BARE_VERB` 加 `连接|上传|下载|提交|发送|打`、
新增 `_CONCLUSION_LEAD`。反向验证：在 `_has_trailing_plan()` 开头插入 `return False`
后完成标记用例变红；去掉新增起点词/动词/结论标题豁免后两条新用例同时变红；
还原后哈希回到 `005b5eff…`，定向 82 条全绿。

| 项 | 值 |
| --- | --- |
| `app/chat/nudge.py` sha256 | `005b5eff06e58c666c8b1dd4bf84795fdd25f94b50796a1d0694c175d9f2fb41` |
| Kali 定向回归 | 82 tests / 0 failures / exit 0 |
| Kali 全量回归 | **486 tests / 0 failures / 0 errors / exit 0** |
| `nudge.py` 自 HEAD 起新增语句行覆盖 | **20 / 20 = 100.00%**（排除文档字符串） |
| 生产部署 | `/root/suvex/suvex-api/app/chat/nudge.py` 哈希一致；pid 3163515，`Application startup complete` 04:41:12 EDT；`/api/v1/health` 200 |

生产复跑（最终版本，分句扫描 + 完成标记剥离）：

| 字段 | 值 |
| --- | --- |
| session | `chat_01M65ZH9F2VD7HXZ3A4KT165GM` |
| turn_run | `turn_01M65ZH9JR5JFMYZ6JC7BNA90Z` |
| status | `done` |
| resume_attempts | **0** |
| round_index / tools_used | 17 / 17 |
| nudges / tool_nudges / truncations | 1 / 1 / 0 |
| 消息数 / assistant 消息数 | 36 / 18 |
| assistant `answer` 数 | **1**（末条 `seq=36`，2748 字，含 `PWNED`；同轮 `seq=26/31` 工具输出为本地 shell 验证） |
| 耗时 | 701.8s |

这一轮中间出现过多次叙述气泡（折叠前 `surface=answer`），收尾时
`_collapse_turn_answers()` 把除最后一条之外的全部降为 `process`，所以库里最终只有一条结论；
用户在长任务进行中仍可能先看到这些叙述气泡，属于已知的展示层取舍，不影响「不再停在半句计划」。
正文 ≥200 字时 `looks_like_plan()` 不判计划（避免把长结论里顺带的一句「我先看了一下」
误打回），这类轮次由「跑过工具就继续」的纠偏路径兜底；这是既有取舍，不是本轮新增的回归。

### 仍未解决

远程靶机 `f2b13febed9811aae33577d9.tcp-ctf2.dasctf.com:9999` 的实例状态不稳定，本轮短任务
脚本刻意只做本地静态分析；「产品对话直接解出 flag」仍取决于靶机是否在线，与停机/重复执行
这两个缺陷分开记录。

---

## v1.35：后台接手路径也必须遵守「没答完不算 done」（G1-review P1）

### 缺陷

独立复核（G1-review，`P1_RESUMER_MARKS_DONE`）指出：`suvex-api/app/chat/resumer.py::resume_one()`
在 `engine.run_turn` 返回后**无条件** `turn_runs.finish(db, run, status="done")`，不看
`result.needs_continue`（停在计划句、还没给出结论）与 `result.stopped`（用户主动停）。resumer 走的是
`run_turn` 本身、**不经过** `_run_guarded_active`，所以前台已标成 `interrupted` 的轮次，在
`STALE_SECONDS=90` 心跳过期后被后台接手、若再次停在计划句上，仍会被记成 `done` ——
线上「没解出又结束」在自动接手路径上复发。

### 修法（`suvex-api/app/chat/resumer.py:121-131`）

```python
if result.stopped:
    turn_runs.finish(db, run, status="cancelled")
elif result.needs_continue:
    turn_runs.finish_incomplete(db, run)   # -> interrupted
else:
    turn_runs.finish(db, run, status="done")
```

与 `engine._run_guarded_active`（`engine.py:2836-2841`）的收尾语义保持一致：`stopped → cancelled`、
`needs_continue → interrupted`、`其余 → done`。

### 回归（`suvex-api/tests/chat/test_turn_persistence.py`）

- `test_resume_one_keeps_the_turn_incomplete_when_the_model_stops_on_a_plan`：
  `needs_continue` → `interrupted`，且 `last_error == turn_runs.NEEDS_CONTINUE_REASON`。
- `test_resume_one_keeps_a_user_stop_cancelled`：`stopped` → `cancelled`。

### RED→GREEN 证据（真跑，不是自述）

- 把收尾分支临时改回无条件 `done`：两条用例变红 ——
  `assert 'interrupted' == 'done'`、`assert 'done' == 'cancelled'`（2 failed）。
- 逐字节还原后 sha256 = `6e041809c09caf5da70f0abef01772fadcc93ad8181999356413219469ca0b5d`（前后一致）：
  两条用例通过（exit 0）。
- 证据文件：`artifacts/g0-chat-stop-repair.json`（run 目录
  `~/.metis/agent/performance-runs/perf-1589b053-.../artifacts/g0-chat-stop-repair.json` 有同名副本）。

### 回归结果（本机 Windows，PostgreSQL 127.0.0.1:55432）

| 命令 | 结果 |
| --- | --- |
| `pytest tests/chat/test_turn_persistence.py -q -k resume_one` | exit 0（4 项） |
| `pytest tests/chat tests/test_chat_stream.py -q` | **277 passed / 0 failed，exit 0**（199s） |
| `pytest tests/kernel/test_runner.py -q -k "tool or stream or llm or native or turn"` | **26 passed / 191 deselected，exit 0**（21s） |

### 未做（需另行确认）

- **未提交 git、未部署 Kali**。当前工作区 HEAD=`6bb39466`（v1.29），v1.30–v1.35 的改动整体仍未提交；
  且部分改动跨出本条 lane（`app/providers/llm.py`、`app/kernel/runner.py`），
  提交需按 `docs/chat-stop-scope-exception-20261030.md` 的范围例外一次性成组提交后再部署，
  否则单提交 lane 内文件会引用未提交的 `ChatTurnResult.needs_continue` 而变成坏提交。

