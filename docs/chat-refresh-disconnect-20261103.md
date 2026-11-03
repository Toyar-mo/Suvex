# v1.54：刷新网页会中断会话

## 现象（用户原话）

> 刷新网页会中断会话

截图里那一轮：正文已经给出**完整结论**（"拼图部分附带结论：16 块按 `0.15` 顺序拼接代价
116.98 vs 随机最优 1253.09，identity 就是正确画面（角色插画，非文字）"），
下面却挂着一行

> （这一轮中途断了：CancelledError。我在后台接着做；也可以发「继续」催我一下。）

`203 步 · 76 个工具 · 1 个失败 · 08:40` —— **跑了 8 分 40 秒、76 个工具**的活儿，
被一次刷新打断，标签落成"中途断了"。

## 根因

链路很短，四步：

1. 浏览器刷新 → 这条 SSE 连接断掉；
2. `StreamingResponse` 关掉响应生成器；
3. `suvex-api/app/chat/stream.py::stream_turn` 的 `finally`：

   ```python
   finally:
       # v1.54 之前：
       if not task.done():
           task.cancel()
   ```

   把这个正跑着的 `run_turn` 任务**取消**；
4. `suvex-api/app/chat/engine.py::_run_guarded_active`（engine.py:3287）接
   `BaseException`（刻意接，因为客户端断开抛的是 `CancelledError`）→
   `finish_from_error(...)` 落 **interrupted** + 追加一条
   `INTERRUPTED_NOTE`（"这一轮中途断了…我在后台接着做"）。

**矛盾点**：这套代码的设计是"轮次是 durable 的、断了会被后台接手"
（`turn_runs` 有 lease / heartbeat / resumer，v1.35 起 `finish_incomplete`）。
可 `task.cancel()` 直接把这轮**杀掉了**——它没机会跑完，只能靠 90 秒心跳
过期后被 resumer 捡起来重跑，中间是空转。用户看到的就是"刷新 = 中断"。

## 修法

`stream.py` 的 `finally` **不再 cancel**：

```python
finally:
    # 客户端断开只是「不再往这条已死的连接推帧」，不是「停止干活」。
    # 让它在后台跑完，自己落 done / interrupted / cancelled。
    pass
```

理由：
- 一轮一旦跑起来（有 `turn_run` 行、有 heartbeat、沙箱在跑、工具可能有
  **副作用**），就不该因为"看的人换了页面"而中断。断开 ≠ 停止。
- 真要停止，本来就有一条显式路径：`/interrupt`（用户点「中断」）→
  `result.stopped` → 落 `cancelled`。那才是"别再跑"。
- `emit` 往断掉的连接 `queue.put` 不会抛（队列在内存里），只是没人取；
  任务跑完即 GC，不泄漏。

⚠️ **不能写 `return`**：`finally` 里的 `return` 会**静默吞掉**正在传播的
异常（ruff B012）。那样 `run_turn` 抛的错就不会变成 `error` 帧，前端只会
看到连接无声断掉——正好破坏 `stream_turn` docstring 要求的"失败要带着原因
到达人眼前"。所以用 `pass` 收尾。

## 前端配合（无需改动）

前端用 fetch 读流（`data/chat-stream.ts`）。刷新后它会重新 `GET /chat/sessions/{id}`
+ `/turns` 拉最新状态；此时那一轮仍在后台 running，界面会显示"这一轮还在跑
（服务端）…"，跑完自动看到结果。若要主动停，点「中断」。

## 验证

- 新增 `tests/test_chat_stream.py::test_stream_turn_does_not_cancel_the_turn_when_the_client_disconnects`：
  生成器只取第一帧就 `aclose()`（模拟刷新断开），断言 runner **跑到了终点**
  （`finished` 事件被 set）、且**没有**收到 `CancelledError`。
- 对照 `::test_stream_turn_still_emits_done_when_not_disconnected`：没断开时
  照常发 `done`（防止改动误伤正常路径）。
- **G0 RED→GREEN 都验过**：把 `finally` 改回 `task.cancel()` →
  上面那条 **1 failed**；还原 → 4 passed。
- 全量：`pytest tests/chat tests/test_chat.py tests/test_chat_stream.py`
  → **473 passed, 1 skipped**（v1.53 后 471，本次 +2）。
- `ruff check app/chat/stream.py` → **All checks passed**（B012 已按上面说明
  改为 `pass`，不是忽略）。

## 边界（如实说明）

- 断开后这一轮**继续烧模型/token**直到跑完——这是"不中断"的必然代价，
  也是 durable 的本意。要止损就点「中断」（落 cancelled，后台不再接手）。
- 只改"客户端断开"这一条路径；`/interrupt`、审批等待、超时等语义不变。
- 历史已落库的 `interrupted` 行不回溯（它们本来就该由 resumer 接手）。
