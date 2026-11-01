# v1.43：让对话「不因为说了一句计划就停」（放开进度预算）

## 用户看到的现象

一个 CTF 长途解题会话（截图，43 步 · 19 工具）最后一行是：

> （这一轮停在了下一步上：「继续」我接着执行 —— 上面那句是计划，不是结果。）

必须手点「继续」才往下走。用户连着两次问「能不能让他不要停」，最后说
「**设置每个对话无限**」。

## 根因（不是工具轮次上限）

停在这一句的直接原因是 `MAX_PLAN_BOUNCES`（计划句打回额度）用尽了，**不是**
`MAX_TOOL_ROUNDS`（工具往返 800）。

流水线（`suvex-api/app/chat/engine.py` 计划句分支，约 2179-2325 行）：

```python
plan_like_final = bool(not actions and not at_limit and not _is_truncated(reply)
                       and nudge_policy.looks_like_plan(text))
premature_final = plan_like_final and tools_used > 0 and plan_bounces < MAX_PLAN_BOUNCES
# premature_final（额度内）→ plan_bounces += 1，补一句"还没做完"提醒，continue
# plan_like_final 且 bounces 耗尽 → render_plan_exhausted_note() + needs_continue=True + return
```

`MAX_PLAN_BOUNCES` 原来是 **4**：模型每说一次「接下来我要看主逻辑 / 下一步继续」
就被打回一次；打回 4 次后第 5 次不再打回，而是把「（这一轮停在了下一步上…）」
写进正文并让本轮以 `needs_continue=True` 结束 —— 正是用户看到的那句话。
43 步的长任务里，这种「先说计划再动手」的表达远远超过 4 次。

## 最终取值（放开「真实进度」，保留「失败模式」与「成本兜底」）

「无限」不能一刀切成「所有额度都 = 100000」。原因分成三类：

| 额度 | 含义 | 类别 | 改成 |
|---|---|---|---|
| `MAX_TOOL_ROUNDS` | 工具往返轮次（每轮都在真跑工具） | **真实进度** | 800 → **100000**（实际无限） |
| `MAX_PLAN_BOUNCES` | 计划句打回（"接下来要做什么"，每次打回给模型一次真干活的机会） | **有界但大幅放宽** | 4 → **100** |
| `MAX_NARRATION_NUDGES` | 纯叙述（只写人话不写动作）纠偏 | 失败模式 | 2（不变） |
| `MAX_TOOL_NUDGES` | 跑过工具又只写人话的重试 | 失败模式 | 300（不变） |
| `MAX_TRUNCATION_CONTINUES` | `max_tokens` 切断后"接着写" | 失败模式 | 12（不变） |
| `MAX_FINAL_CONFIRMATIONS` | `FINAL` 后打回确认 | 失败模式 | 1（不变） |
| `MAX_EMPTY_NUDGES` | 跑过工具的轮次回**空回复**重推 | 失败模式 | 6（不变） |

### 为什么 `MAX_PLAN_BOUNCES` 是 100（有界）而不是 100000（无限）

这是本任务最关键的一次纠错，记在这里：

- **第一版**把 `MAX_PLAN_BOUNCES` 也设成 100000。结果 `tests/chat` 里两条
  守护用例 `test_repeated_plan_finals_never_land_as_an_answer`、
  `test_repeated_finals_are_bounded_and_never_end_silently` **失败**，而且整包
  测试从 4 分钟涨到 **38 分钟**（2307s）。
- 读用例 docstring 才看清设计意图：它们**专门守护「打回额度必须有限，否则
  模型一直写计划句就会死循环烧钱」**。这两条用例的断言语义是「调用次数 ==
  1 + `MAX_PLAN_BOUNCES` + 1」，本身对任何**有限**值都成立；100000 之所以
  失败+超慢，是因为脚本网关要准备 `MAX_PLAN_BOUNCES+2` 条回复、循环真跑了
  近十万次、最后脚本耗尽走了空回复分支，断点对不上。
- 更要紧的是**设计事实**：计划句是"没动手"的表达，打回只是"再给一次机会"，
  一次打回本身**不是**真实进度。把它无限放开＝一个卡在计划句里的模型可以烧
  十万次调用。这与「任务做多久都不被掐断」的诉求其实无关 —— 后者由
  `MAX_TOOL_ROUNDS`（真跑工具）承担。
- 所以正确取值：`MAX_TOOL_ROUNDS` 放开到实际无限（100000），
  `MAX_PLAN_BOUNCES` 从 4 放宽到 **100**（比原来宽 25 倍，足够覆盖长途任务的
  间歇性计划句），既治好用户看到的症状，又保住"成本不会失控"的兜底。
- 验证：改回 100 后这两条用例 **3.79s 通过**，全包 `tests/chat` 回到 235s 全绿。

### 失败模式为什么保持有界

**空回复 / 截断 / 纯叙述重试不是「模型在干活」，是模型抽风或我方 token 上限**。
对这些无限重试只会每轮烧一次模型调用而用户看到同一个卡点 —— 无限化纯属烧钱。
用户要的是「任务做多久都不因为说了多少句计划、跑了多少轮被掐断」—— 这由
`MAX_TOOL_ROUNDS`（+更宽的 `MAX_PLAN_BOUNCES`）承担。

## 代价（如实告知）

- 模型可能**长跑**（几十分钟 / 上百工具轮），相应增加模型调用费用。
- 用户随时可点「停止」中断；停止后按既有逻辑记 `cancelled`（见 v1.35）。
- 失败模式兜底仍在：空回复 6 次、`FINAL` 确认 1 次、截断 12 次、计划句打回
  100 次 —— 确定性故障与死循环仍会收尾，不会真的无限烧钱。

## 测试

- `pytest tests/chat tests/test_chat_stream.py` → **325 passed, 1 skipped / 235s**。
- `pytest tests/test_chat.py` → 43 passed / **2 failed** / 34s（见下条，既有问题）。
- 两条计划句守护用例单独跑 → **2 passed / 3.79s**。

### 两条既有失败（不是本次引入）

`tests/test_chat.py::test_a_long_narration_is_not_nudged`、
`tests/test_chat.py::test_nudges_are_capped_and_do_not_eat_the_tool_budget`
在本任务改动前的干净 HEAD（`01a99897` v1.42、以及更早的 `77a1e21b` v1.41）上
**同样失败**：

```
AssertionError: assert ['user', 'assistant', 'assistant'] == ['user', 'assistant']
```

即实际多出**一条 assistant 消息**。与 v1.43、与 tesseract 都无关，属仓库既有
问题，本任务不修（不在本 lane 范围内），如实记录待后续。

## 改动文件

- `suvex-api/app/chat/engine.py`（仅常量区 2 行：`MAX_TOOL_ROUNDS` 800→100000、
  `MAX_PLAN_BOUNCES` 4→100）。

## 部署

复制 `app/chat/engine.py` 到 Kali `/root/suvex/suvex-api/`，`nsenter` 重启
`suvex-api`，轮询日志 `Application startup complete` 后 `GET /api/v1/health`
应为 200。
