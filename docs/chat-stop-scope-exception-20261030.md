# 对话停机修复：范围例外记录（2026-10-30）

## 结论

本轮修复的**缺陷根因不在对话层**，而在模型接入的 wire 层：

| 文件 | 在这一轮里做了什么 | 为什么不能只改 `app/chat` |
| --- | --- | --- |
| `suvex-api/app/providers/llm.py` | `stream_delta` 以前只读 `delta.content`，把网关返回的 `delta.tool_calls` 整段丢弃；新增 `tool_calls_delta` / `tool_calls_from_message` 把两套 wire 形状归一化 | 丢数据发生在 SSE 解析这一层。对话层拿到的已经是 `text=''`，无论怎么改都**无法知道**上游其实调了工具 |
| `suvex-api/app/kernel/runner.py` | `LlmReply` 增加 `tool_calls`；`chat_stream` 按 `index` 累积原生调用；收尾原因改为「只要出现就记录」，不再用 `delta` 是否为空门控 | 同上：`LlmReply` 与流式累积器是数据的唯一出口 |
| `suvex-api/app/chat/engine.py` | 把原生调用合成 `ACTION:` 文本、空回复兜底、可见提示 | 本就在原定范围内 |
| `suvex-api/app/chat/prompts.py` | 新增 `render_native_tool_note` | 本就在原定范围内 |

如果不碰前两个文件，只能做一件事：检测到 `finish_reason='tool_calls'` 时给用户一条「平台不支持」的提示，
对话仍然解不了题。用户诉求是**让对话真正跑完并解题**，所以必须修 wire 层。

## 谁批的、依据是什么

- 决策人：本轮执行者（root / Metis），2026-10-30。
- 依据：运行治理目录中的 host-g5-reviewer-abbafe.json（不在目标工作区）的 P1 指出这两个文件超出 canonical ownedPaths；
  该评审同时允许「重新登记**或**记录一条显式的范围例外」。这里选择后者，因为缺陷位置本身
  决定了范围：换到别的层修只会得到提示语，得不到行为修复。
- 受影响路径清单（本轮实际改动）：
  - `suvex-api/app/providers/llm.py`
  - `suvex-api/app/kernel/runner.py`
  - `suvex-api/app/chat/engine.py`
  - `suvex-api/app/chat/prompts.py`
  - `suvex-api/tests/chat/test_native_tool_calls.py`
  - `suvex-api/tests/chat/test_turn_persistence.py`
  - `docs/chat-stop-root-cause-20261030.md`
  - `suvex-api/app/chat/nudge.py`（在 owned paths 内，无需例外）
  - `suvex-api/app/chat/service.py`（在 owned paths 内，无需例外）
  - `suvex-api/tests/chat/test_session_lifecycle.py`（在 owned paths 内，无需例外）

## 约束

- 这条例外**只覆盖上述文件**，不构成对 `suvex-api/app/kernel` 或 `app/providers` 其它文件的通用许可。
- wire 层改动必须由真实帧证明（`delta.tool_calls` 分片、`finish_reason='tool_calls'`、
  正文与收尾原因同帧三种形态），不能只靠单元测试自证。
- 回归边界：`pytest tests/chat tests/test_chat_stream.py tests/kernel/test_runner.py` 必须全绿。
- `suvex-web` 的改动不属于 chat-stop lane；若归属变更需重新登记。
