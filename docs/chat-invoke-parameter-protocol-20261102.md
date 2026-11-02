# 对话工具调用第三种自创协议：`<invoke>` / `<parameter>`（v1.48）

## 现象

用户让"跟踪这个对话，这道题解的很困难吗"。跟踪的会话是 `chat_01M6BWQ69MZJNBAB7C7QMVXPV2`
（标题「openssl s_client -connect ba523e…」，共 441 条消息）—— 正是 v1.44 修过 `<result>`
伪造工具结果的同一个会话的**下一轮**：

- turn `turn_01M6E99VZ6G3CYW5XY5GAX80WZ`：`status=running`、`round_index=101`、
  `tools_used=50`、`nudges_used=45`、`tool_nudges_used=45`（心跳正常，不是崩溃）；
- 该轮 `seq 385–441` 里，**同一个失败模式刷了 9 次以上**：
  `工具结果 = 参数错误：需要 command`（工具被调起，但参数没送到）；
- 模型自己写了 4 次「抱歉，前面空转太多」「我一直在细节里绕圈」「浪费了大量回合」。

结论：这道题**本身很难**（UE 资源逆向 + AES 载荷，已排除"拼图排列/背景版权图/明文 flag"
三条路，收敛到"`p.bin` = 16 字节头 + 1408 字节 = 88 个 AES 块，等密钥"）；但**执行效率被
第三种自创协议拖垮** —— 大量动作因为参数没送到而空转。

## 根因

模型（deepseek-v4.1-flash）在这轮改用 **Anthropic 工具调用的 XML 形态**：

```
<parameter name="command">cd /work && python3 - <<'EOF' ... EOF</parameter>
<parameter name="tool">run_shell</parameter>
</invoke>
```

特点与前三套都不同：

- **没有 JSON 外层**：不是 `<action>{"tool":...}</action>`，参数体是**纯文本** bash 脚本
  （可含换行、heredoc、引号、花括号）；
- **开标签 `<invoke>` 常常缺失**，只有 `</invoke>` 收尾；工具名有时写在
  `<invoke name="run_shell">` 的属性里，有时写成 `<parameter name="tool">`；
- 落库后逐字节回灌给模型，与 v1.44 的 `<result>` 循环同一种"自我强化"结构。

v1.41（`<action>/<analysis>`）和 v1.44（`<result>`）都不认这套，于是：**不执行 + 不隐藏 +
烧纠偏**。`parse_actions` 只认 `ACTION: {...}`，`<parameter>` 解析不出来 → 动作从未执行；
`_narration_before_action` 也切不到 → 标签原样进正文。

## 修法（`suvex-api/app/chat/engine.py`）

新增 `_parameter_blocks_to_actions(text)`：按 `<parameter name="X">...</parameter>` 逐组抓值
（**不当 JSON 解析**，参数体原文逐字节保留），合成一行
`ACTION: {"tool": .., "args": {..}}`，交给后面所有逻辑原样复用。

- 工具名：先取 `name="tool"` 参数；没有则读组前最近的 `<invoke name="X">` 属性；都没有
  → **不猜**（整组原样留给兜底清理，宁可少执行一个，也不执行一个参数错的动作）；
- 参数名 `args` 若存在，先当 JSON 试解析合入，其余 `<parameter name="X">` 的值覆盖进去；
- `_normalize_xml_actions` 的触发条件与 stray-tag 兜底都扩到 `invoke`/`parameter`；
- `_has_protocol_marker_or_xml` 同样认 `<parameter`/`<invoke`/`</invoke>`，避免再合成原生动作。

## 验证

- 新增 5 条测试（`tests/chat/test_xml_actions.py`）：合成为动作、heredoc+花括号**逐字节保留**、
  `<invoke name>` 属性写法、普通文本不误伤、`_has_protocol_marker_or_xml` 识别。
- G0 RED → GREEN：修复前 4 failed / 12 passed；修复后 **16 passed**。
- 全量 `pytest tests/chat tests/test_chat_stream.py` → **375 passed, 1 skipped**（原 370 + 新 5）。
- 真实样本端到端（本机 + 线上 prod venv）：把现场 `seq 433`（无 `<invoke>` 开标签）与一条
  带 heredoc 的真实形态喂进 `_normalize_xml_actions` → `parse_actions` 都解析出
  `("run_shell", {"command": ...})`，命令原文逐字节相等，`LEAK=False`（无任何 `<parameter>`/
  `<invoke>`/`<action>`/`<result>` 残留），人话保留。
- ruff：`engine.py` 仍是 **8 条预存告警**（HEAD 上就有，非本次引入），无新增。

## 已上线

- 提交 `37217e75`；推送 `fd6a3a81..37217e75`。
- 部署 Kali `/root/suvex`：备份 `engine.py.bak-v148-*`；`install -m 644` 覆盖，prod sha
  = `2656a6ea14955cabe8b97366f9361295dee03af719c67fc5a042b40c68ad3846`（与本地相等）；
  `systemctl restart suvex-api` → `Application startup complete`（09:40:45）、39 张表、
  `is-active=active`、`GET /api/v1/health`=200 `{"status":"ok","version":"0.1.0"}`。
- 线上 prod venv 探针：`DETECT=True / LEAK=False / ACTIONS=[("run_shell", {...heredoc...})] / PROD_OK`。

## 限制（如实说明）

- 这次修的是**协议识别**（让 `<parameter>` 动作能被执行、不再空转），**不保证这道题能解出来**——
  题目本身难（AES 密钥还没找到），本修复只提升执行效率。
- 若模型漏写工具名（该轮 `seq 281` 就是只有 `</invoke>` 没有 `name="tool"` 也没有
  `<invoke name>`），实现**故意不猜**：这种残缺调用仍会留在正文里给人看，但不会被执行。
- 历史已经落库的 `<parameter>` 文本不会回洗（与 v1.41/v1.44 同口径，只影响新回复）。
