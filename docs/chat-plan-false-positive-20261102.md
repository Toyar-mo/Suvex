# v1.45：长结论被误判成「计划句」→ 整轮标成没答完（`_PLAN_START` 漏了完成态守卫）

日期：2026-11-02
版本：v1.45
状态：已修复、已部署、已线上验证

## 现象

用户在对话里拿到一段**已经给出了结论**的长回答，界面上却多出一句
「（这一轮停在了下一步上：「继续」我接着执行 —— 上面那句是计划，不是结果。）」，
整轮被标成"没答完"（`needs_continue=True`）。用户视角就是"明明答完了，怎么说我停在计划上"。

## 根因

`app/chat/nudge.py::looks_like_plan()` 在**长正文**（≥ `MAX_UNFINISHED_CHARS`=200）时
只扫收尾窗口（最后 `TRAILING_PLAN_CLAUSES`=3 个句子），交给
`_has_trailing_plan()` 按分句扫描。后者命中任一条件即判为"计划句"：

- `_PLAN_VERB`：话语标记 **紧邻**动词（`然后我要看…`），且动词后带
  `(?!了|到|出|过|完…)` 的**完成态守卫** —— 命中即真计划。
- `_PLAN_BARE_VERB`：句首裸动词（`看反汇编…`），同样带完成态守卫。
- `_PLAN_START`：**只有**句首话语标记（`先/现在/接下来/下一步/然后/接着…`），
  **完全没有**完成态守卫。

缺口就在 `_PLAN_START`：它只认"然后/接着/下一步"这几个词，不看后面那句话其实
是**已经做完**的陈述。于是：

    …（>200 字技术结论）… 然后我把所有 gadget 都过了一遍，结论如上。

收尾句「**然后**我把…过了一遍」命中了 `_PLAN_START` 的「然后」→ 判为计划句 →
`plan_like_final=True` → 零工具轮次走 `render_plan_exhausted_note()` 分支
（`engine.py` 约 2334 行）→ 追加"这一轮停在了下一步上" + `needs_continue=True`。

`_PLAN_BARE_VERB` 早就为"过了/完了"这类完成态加了 `(?!了|过|完|到|出|好)`
守卫（见 nudge.py 里那段长注释：「看了」「查到了」「跑完了」是**已经发生**的事），
`_PLAN_START` 这条路径漏了同一个道理。

## 修法

`app/chat/nudge.py` 新增：

- `_COMPLETED_ASPECT`：动词 + 体标记（了/过/完/到/出/好）的完成态正则。
- `_clause_is_completed(clause)`：判断分句是不是"已经做完"的陈述。

`_has_trailing_plan()` 里把原来的

    if _PLAN_START.search(cleaned) or _PLAN_VERB.search(cleaned):
        return True

拆成：

    if _PLAN_VERB.search(cleaned):          # 标记+紧邻动词，已有完成态守卫 → 真计划
        return True
    if _PLAN_START.search(cleaned) and not _clause_is_completed(cleaned):
        return True                          # 只有标记、且不是完成态 → 真计划

即：`_PLAN_START` 命中后，若该分句是**完成态**，不再算计划。

### 为什么不会过修（放过真计划）

- `_PLAN_VERB` 分支完全没动：`然后我要看 libc 版本`（"看"后无"了/过/完"）仍命中。
- `_PLAN_START` 分支只是多了一个"排除完成态"的条件：`下一步跑 one_gadget`
  （无体标记）仍命中。
- 新增对照用例 `test_long_forward_looking_tail_after_marker_is_still_a_plan`
  钉住这一点。

## 验证

G0 RED → GREEN：

- 临时把 `_has_trailing_plan` 改回旧的一行条件 →
  `tests/chat/test_plan_detection.py::test_long_completed_action_tail_is_not_a_plan`
  **FAILED**（1 failed, 23 passed）。
- 还原修复 → **24 passed**。

回归用例（新增，`tests/chat/test_plan_detection.py`）：

- `test_long_completed_action_tail_is_not_a_plan`：4 条"然后/接着/最后 + 完成态"的
  长正文收尾句，全部 `looks_like_plan(...) is False`。
- `test_long_forward_looking_tail_after_marker_is_still_a_plan`：3 条对照，仍 `True`。

既有失败用例的处理：

- `tests/test_chat.py::test_a_long_narration_is_not_nudged` —— 这条用例**本来就是**
  在守这个 bug（它的 docstring 明写"长文里顺带一句『我先看了一下』不算停在半路"），
  之前的失败恰好是 v1.35 引入的回归。修复后**自动转绿**，无需改断言。
- `tests/test_chat.py::test_nudges_are_capped_and_do_not_eat_the_tool_budget` ——
  它的核心不变量 `len(gateway.seen) == 1 + MAX_NARRATION_NUDGES`（模型调用次数有上限）
  一直**通过**；失败的是 `result.messages` 的条数断言，因为它没数 v1.35 起补的
  那条收尾实话。已把断言更新为
  `["user"] + ["assistant"]*(1+MAX_NARRATION_NUDGES) + ["assistant"]` 并加注释说明
  多出来的那条是 `render_plan_exhausted_note()`。

套件：

- `pytest tests/chat/test_plan_detection.py` → **24 passed**
- `pytest tests/chat/test_plan_detection.py tests/test_chat.py::test_a_long_narration_is_not_nudged tests/test_chat.py::test_nudges_are_capped_and_do_not_eat_the_tool_budget` → **26 passed**

## 相关既有失败（与本修复无关，如实记录）

`tests/test_chat.py::test_a_long_narration_is_not_nudged` 之外，另有 2 条既有红
（`git stash` 到干净 HEAD `01a99897`/`77a1e21b` 同样失败）—— 本次修复后其中
`test_a_long_narration_is_not_nudged` 已转绿；`test_nudges_are_capped...` 已按新行为
更新断言。

## 部署

- 本机 `app/chat/nudge.py` sha256=`b0152107d37be44141529b41c7966c47243cfff7769ac6245128cd81defdd48c`。
- 生产 `/root/suvex`（非 git，复制部署）：`install -m 644` 覆盖
  `suvex-api/app/chat/nudge.py`，备份 `nudge.py.bak-v145-<ts>`。
  部署后 prod sha256 与本地**逐一相等**：`b0152107…dd48c`。
- `nsenter ... systemctl restart suvex-api` → 日志 `Application startup complete`
  （重启后约 2 轮内出现），`systemctl is-active suvex-api`=`active`，
  `GET /api/v1/health` = **200**。

## 线上验证

prod venv（`/root/suvex/suvex-api/.venv/bin/python`）探针，对部署后的 `nudge` 模块：

```
COMPLETED1_not_plan=True    # 长正文 + "然后我把所有 gadget 都过了一遍，结论如上。" → 不是计划
COMPLETED2_not_plan=True    # 长正文 + "接着我把所有配置文件都读完了，结论如下。" → 不是计划
PLAN1_is_plan=True          # 长正文 + "然后我要看 libc 版本，接着写 exploit。" → 仍是计划
PLAN2_is_plan=True          # 长正文 + "下一步跑 one_gadget 找 gadget。" → 仍是计划
ALL_OK= True
```

## 套件

- `pytest tests/chat` → **315 passed, 1 skipped**
- `pytest tests/test_chat.py tests/test_chat_stream.py` → **66 passed**（含此前失败的 2 条）
- `ruff check app/chat/nudge.py tests/chat/test_plan_detection.py tests/test_chat.py` → All checks passed

