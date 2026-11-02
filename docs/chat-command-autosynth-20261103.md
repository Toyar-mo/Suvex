# 贴了命令没写 ACTION：直接执行，不再纠偏往返（v1.51）

## 背景：v1.50 救回了"停"，但代价是"每两步走一步"

v1.50 修好了「正文贴了命令却没走 `ACTION:` → 静默收尾」的缺陷（新增
`nudge.has_unactioned_command`，与长度无关地纠偏一次）。它确实救回了
「还是会停」。

但线上实测（会话 `chat_01M6BWQ69MZJNBAB7C7QMVXPV2`）跑起来后，日志与
`chat_turn_runs` 暴露出它的**代价**：

```
round=0   raw_len=1105 text_len=1104 actions=0 nudges=0
round=1   raw_len=1113 text_len=0    actions=1 nudges=1  finish='tool_calls'
round=2   raw_len=478  text_len=477  actions=0 nudges=1
round=3   raw_len=834  text_len=0    actions=1 nudges=2
...
round=33  tools=16  nudges=17
```

**近一半的模型调用是纯贴命令的轮次**：模型说一句「接下来 dump」，把命令贴
成正文（`actions=0`）→ 被 v1.50 纠一下 → 下一轮才用原生 tool_call 补执行。
比赛按**用时**计分，这等于把一半时间扔了。用户看直播流就是「第 8 轮·模型在
推理…」——不是死了，是每两步走一步、慢。

同时确认：那个 turn 并没有"停"——它在 `round 26→33 / tools 12→16` 稳步推进，
只是中间夹着一次很重的 `python3 -c` 全量扫描（`for i in range(len(d)-16)` ×
`struct.unpack_from`，约 5000 万次 Python 迭代，容器里 `timeout 300` 兜着）。
这说明 v1.50 的"不停"是成立的，剩下的是**效率**问题。

## 修法：把那段命令直接变成 `run_shell` 动作

既然命令已经在正文里、结构明确（`cd /work ...` / `python3 -c "..."` /
`R=$(ls rb/*/retoc)`），就直接翻译成 `ACTION: {"tool":"run_shell","args":{"command": ...}}`，
省掉「纠偏 → 模型重发」那一整次往返。**两轮并一轮。**

### 新增 `nudge.extract_trailing_command(text) -> str | None`

从"说了要干、贴了命令、没写 ACTION"的正文里抠出那段命令。

### 安全边界（这是危险操作，不是普通工具函数）

**自动执行模型写的命令**必须极窄，四条守卫缺一不可：

| 守卫 | 作用 |
| --- | --- |
| 无 `ACTION:` / `FINAL:` | 走了协议就不掺和 |
| 不含结论性措辞（`已解出/拿到了/找到 flag/结论是/没法/做完了`…） | 那是解释不是动作 |
| 正文要有下一步提示（`接下来/现在/下面/然后/执行/试试`…） | 确认它真打算跑 |
| 不含破坏性片段（`rm -rf /`、`mkfs`、`dd of=/dev/`、`curl … \| bash`…） | 宁可不动 |

任一条不满足就返回 `None`，**交回 v1.50 的纠偏路径**（模型自己用
tool_call 重发）。宁可多花一次调用，不可错跑一条命令。

### engine 接线

主循环与 resume_approved 循环各一处，都在 `parse_actions` 之后：

```python
if not actions and not at_limit and not said_final:
    _synth_cmd = nudge_policy.extract_trailing_command(text)
    if _synth_cmd:
        actions = [("run_shell", {"command": _synth_cmd})]
```

合成出动作后，后面的执行循环照常跑（工具结果照常进历史、`tools_used`
照常 +1、停滞检测照常看签名）。

## 验证

- `tests/chat/test_command_autosynth.py`（10 条）：线上 seq677 真实形态抠命令、
  只留命令不留叙述、围栏块、有 ACTION 不处理、结论/带 flag 不执行、无意图
  提示不执行、构造的 4 条破坏性命令一律拒绝、无命令/空串、命令块后接叙述
  时截断、`R=$(...)` 赋值行也算命令行。
- `tests/chat/test_command_autosynth_e2e.py`（2 条，引擎级）：monkeypatch
  `run_shell` 为记录调用的假实现 → 贴命令的正文**真的被执行一次**（参数就是
  那段命令），且只用了两次模型调用（拿命令 + 收尾），**没有额外纠偏往返**；
  无「接下来」提示的正文不执行。
- **G0 RED→GREEN**：把 engine 两处合成块临时停用 → `test_pasted_command_is_executed_without_a_nudge` FAILED；还原 → 12 passed。
- 全量回归：`pytest tests/chat tests/test_chat.py tests/test_chat_stream.py`
  → **457 passed, 1 skipped**（v1.50 后是 445，本次 +12）。
- `ruff check`：nudge/两个新测试 **All checks passed**；`engine.py` 仅剩
  **6 条预存告警**（I001/B905/F821×3/F841，HEAD 有、本次未增）。

## 边界与如实说明

- 只有"有下一步提示 + 末尾是命令块 + 不含结论/破坏性措辞"才自动执行；其余
  一律退回纠偏。保守优先。
- 若模型把命令藏在一段叙述中间（前后都有大段文字），或写成英文句子
  （"I'll run feroxbuster"），仍不会自动执行 —— 那一档由 v1.50 纠偏 +
  `looks_unfinished` 兜。
- 不保证题一定解得出来：这是流程效率修复（省往返、少空转），不是解题能力
  修复。这道题本身卡在逆向脚本对象（`ScriptObjects` chunk / `PuzzleScriptHost`
  偏移），属模型思路层面。
