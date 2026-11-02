# 对话「还是会停」：正文里贴了命令却没走 ACTION（v1.50）

## 现象

用户 2026-11-02 23:56 截图（会话「openssl s_client -connect ba523e…」）：
一屏 23:15 的 81 步·24 工具之后，又冒出一条 666 字的回复，末尾是一段
`cd /work/... && python3 -c "..."` 的**裸 shell 命令**，没有 `ACTION:`，
界面停在「继续」按钮上。用户原话：「**还是会停**」。

v1.49 已经修了「问一句停一次」的三个成因（并发双轮、DSML 标记、原地打转），
这是**第四个形态**，v1.49 没覆盖到。

## 生产日志证据（决定性）

10:53:25，`journalctl -u suvex-api`：

```
chat.round round=0 raw_len=667 text_len=666 actions=0 said_final=False
           truncated=False tools=0 nudges=0 tool_nudges=0 empty_nudges=0
           reasoning_len=0 finish='stop' raw_keys=[]
chat.giveup no_action said_final=False plan_like=False plan_bounces=0
           nudges=0 tool_nudges=0 truncations=0 at_limit=False text_len=666
```

`nudges=0` 说明模型**一次纠偏都没吃**就 `giveup` 收尾了。对照
`chat_turn_runs`，这条 turn 记 `round_index=0 / tools_used=0 / status=done`
—— 用户点一次「继续」就新建一轮、立刻 done、界面再停，如此往复。

数据库里那条正文（seq 660）逐字是：

```
抱歉，上一条漏了 ACTION。我现在做一件从未检查过的事：**对全部图像做 LSB 隐写提取**，
这是 CTF 里最常见的置入 flag 方式。

cd /work/persist/chisaki && python3 -c "
import numpy as np
...
"
```

## 根因

模型「说了要干、把命令贴成正文、却没写 `ACTION:`」。引擎里有三条「这一轮
没动作要不要再推一次」的判据，**全部漏掉**：

| 判据 | 位置 | 为什么漏 |
| --- | --- | --- |
| `looks_like_plan` | `nudge.py` | 没有落在收尾三句窗口里的「先/现在/接下来」 |
| `looks_unfinished` | `nudge.py:87` | `len(text) >= MAX_UNFINISHED_CHARS(200)` → 直接 False（被当成"长结论"） |
| `tools_used > 0` | `should_nudge` | 这一轮 `tools_used=0`，该分支够不着 |

`should_nudge` 返回 False → `nudges=0` → 直接走 `chat.giveup no_action`
返回，`result.needs_continue` 没置 → turn 记 `done`。

> 注意 v1.45 修的是**相反方向**的误判（长结论里的「然后我把…过了一遍」被
> 当成计划句）。这里是另一个形态：长正文里夹了**可执行的命令**、却没用协议。
> 两者都是"长度"这一个维度判不准 —— 长度本来就不该是这里的判据。

## 修法

### 1. `nudge.has_unactioned_command(text)`（新增）

正文里**有 shell 命令、却没有 `ACTION:`/`FINAL:`** → True。两种形态都认：

- 围栏代码块（```）里的命令行；
- **裸写在正文里**的命令行（线上 seq 660 就是这种，没有围栏）。

`_SHELL_LINE` 锚在行首、只认命令式开头词（`cd/ls/cat/python3/curl/objdump/./…`），
避免把叙述里"提到 objdump"误判成要执行。

### 2. `should_nudge` 加一条分支

在 `tools_used > 0` 之后、narration 额度判断之前：

```python
if has_unactioned_command(text):
    return tool_nudges_used < max_tool_nudges
```

**与长度无关** —— 这是比任何长度/意图词都硬的「我要执行」信号。额度用
`tool_nudges`（默认 300），不被"纯问答"那 2 次预算卡死。

### 3. `engine.py` 增量计数对齐

nudge 分支原来 `elif tools_used > 0: tool_nudges_used += 1`。若不补，
`has_unactioned_command` 分支判定用 `tool_nudges_used`、却从不累加它 →
`tool_nudges_used < max_tool_nudges` 永远为真 → **死循环**。改为
`elif tools_used > 0 or nudge_policy.has_unactioned_command(text):`（主循环
与 resume_approved 循环两处都改）。

### 4. 纠偏话术对准病因

`render_format_reminder(code_block_only=False)` 新增参数。`code_block_only=True`
时开头换成：

> 你把命令写进了代码块/正文，但它没有被执行 —— 我看到了你贴的 shell 命令，
> 但**那只是文字**，没有 `ACTION:` 行，工具一个都没有运行…

比原来那句笼统的「你上一条回复没有包含动作」更能把模型拉回协议。

## 验证

- 新增 `tests/chat/test_unactioned_command.py`（7 条）：
  - `test_live_seq660_has_an_unactioned_command` —— 线上真实文本（逐字取自 DB）必须判 True；
  - `test_the_page_stop_case_is_nudged_even_though_it_is_long` —— 666 字、零工具、非计划，`should_nudge` 必须 True（旧逻辑三判据全漏）；
  - 反例：正文里"提到 objdump"的完整结论 → False；纯配置的围栏块 → False；已写 ACTION 的 → False；空串/空白 → False。
- **G0 RED→GREEN**：把 `should_nudge` 里那条分支临时改 `if False and …` →
  `test_the_page_stop_case_is_nudged_even_though_it_is_long` FAILED（`assert False is True`）；
  还原 → 7 passed。
- 全量回归：`pytest tests/chat tests/test_chat.py tests/test_chat_stream.py`
  → **445 passed, 1 skipped**（v1.49 后是 393，本次 +52 含新增与既有）。
- `ruff check`：nudge/prompts/新测试 **All checks passed**；`engine.py` 仅剩
  **6 条预存告警**（I001/B905/F821×3/F841，HEAD 上就有，本次未新增）。
- 探针：`has_unactioned_command(LIVE_SEQ660)=True`、`should_nudge(...)=True`、
  `render_format_reminder(code_block_only=True)` 含「代码块」「没有被执行」；
  叙述型结论 `PROSE_FLAGGED=False`。

## 边界与如实说明

- 误判的代价 = 多一次模型调用（让它改用 ACTION 或说"做完了"）；漏判的代价 =
  用户又得手打「继续」、整轮白跑。后者更贵，故宁可多推。
- 「改了就一定能把这道题解出来」**不保证** —— 它治的是"贴了命令不执行"的
  流程缺陷，题本身能不能解出取决于模型思路。
- 只认"像命令"的行首词；如果模型把命令写成一句英文叙述（"然后我运行
  feroxbuster 扫一遍"），仍可能漏 —— 这类已由 `looks_unfinished` 的意图词
  尽力兜，没法穷尽。
