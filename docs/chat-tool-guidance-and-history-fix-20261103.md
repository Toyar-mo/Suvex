# v1.53：引导模型先用现成工具 + 又修两个真缺陷

## 一、引导模型别自己搓暴力扫描（prompts）

线上事故（会话 `chat_01M6BWQ69MZJNBAB7C7QMVXPV2`）：模型为在 2.7MB 的
`scriptobjects.bin` 里找特征，手写了一段

```python
for i in range(len(d) - 16):
    struct.unpack_from('<16i', d, i)
```

——约 **5000 万次纯 Python 迭代**，在容器里跑满一个核好几分钟（`timeout 300`
兜着），期间界面显示"（无输出）"。这不是算力问题，是**思路问题**：明明有
`strings` / `xxd` / `objdump` / `readelf` / `binwalk` 这些 C 实现的现成工具。

`suvex-api/app/chat/prompts.py` 在「### 缺工具就自己装，别回头说「没装」」
之后新增一节：

**### 先找现成工具，别自己搓暴力扫描**

- 点出上面那个事故样例（`for i in range(len(d)-16)`）；
- 给一张对照表：找字符串→`strings`/`grep -a`、看结构→`objdump -d`/
  `readelf -S`、解包→`binwalk -e`/`unzip`、hex 视图→`xxd`/`hexdump`；
- 判断标准一句话：**一条命令跑超过 10 秒，先停下来想"有没有现成工具"**，
  而不是加大 `timeout` 硬等。

回归：`tests/test_prompt_tool_names.py` + `tests/chat/test_plan_detection.py`
→ 28 passed（提示词里工具名与实现一一对应的守护测试仍通过）。

## 二、又两个真缺陷（同一段代码，查缺陷时挖出来的）

### 缺陷 A：认不出工具名的 `<parameter>` 组，body 会漏成正文并**被当命令执行**

`engine._parameter_blocks_to_actions`（v1.48）的规则是：一组 `<parameter>`
如果认不出 `name="tool"`、前面也没有 `<invoke name="X">` 开标签，就**不猜**
它是哪个工具、整组原样保留"交给兜底删标签"。但兜底只删**标签**，**body 留着**：

```
<parameter name="command">ls -la /work</parameter>   （没有 tool / invoke 名）
→ 兜底后正文里只剩： ls -la /work
```

这本来是"不执行、只显示"的保守选择。但 **v1.51 的
`extract_trailing_command` 上线后，这段裸露的 body 成了一行像命令的正文** ——
被它当"贴了命令没写 ACTION"直接拿去 `run_shell` 执行了。**一个本该"不猜、
不执行"的残片，绕了一圈被自动执行了**，恰好是 v1.51 四道守卫想避免的事。

**修法**：认不出工具名的整组，**连 body 一起丢掉**（不只是删标签）。理由：
一段认不出归属的 `<parameter>` 参数块没有任何独立价值，留着只会被下游误吃。
真正有工具名的组照旧合成 `ACTION:`（行为不变）。

### 缺陷 B：历史滑窗切开后，首条可能不是 `user` → Anthropic provider 400

`chat_service.list_messages(limit=N)` 是 `order_by(seq.desc()).limit(N)` 再
reverse —— 取的是**最近 N 条**。会话超过 N 条时，窗口从对话中间切开，**第一条
可能是 `assistant`（半句残话）或 `tool`**。

- `_render_history` 原样把它们拼成 `messages`，于是送模型的 `messages`
  **以 assistant/tool 开头**。
- Anthropic 风格 Messages API **硬性要求首条是 `user`**（否则 400
  "first message must be user role"）。平台环境说明白纸黑字写着
  "国内所有大模型均可用" —— 一旦换成 Anthropic 风格 provider 就整轮失败。
- 即便 provider 宽容（如当前的 deepseek），模型也看不到用户最早的问题，
  "凭空接话"。

**修法**（`_render_history` 末尾）：丢掉开头的非 `user` 消息，直到第一条
`user`（那些都是半截上下文，丢了无害）：

```python
while out and out[0]["role"] != "user":
    out.pop(0)
```

配套兜底（两处调用点）：若丢完变空（极端：小窗口里全是残句），塞一条最小
`user` 占位 `{"role":"user","content":"（继续之前的工作。）"}`，避免拿空数组
调模型必然 400。

## 验证

- `tests/chat/test_xml_actions.py` +4（31 passed）：认不出工具名的组连 body
  一起丢、不能被 `extract_trailing_command` 当命令、有工具名的组行为不变、
  DSML 形态同样丢。
- `tests/chat/test_history_budget.py` +3（8 passed）：窗口从 assistant 开头
  → 首条变 user；从 tool 开头 → 首条变 user；已以 user 开头 → 逐字节不动。
- **G0 RED→GREEN 都验过**：
  - 缺陷 A：把"整组丢弃"改回"只删标签" → `test_xml_actions.py` **4 failed / 27 passed**；还原 → 31 passed。
  - 缺陷 B：删掉 `while out[0]["role"] != "user"` → `test_history_budget.py` **1 failed**；还原 → 8 passed。
- 全量回归：`pytest tests/chat tests/test_chat.py tests/test_chat_stream.py`
  → **471 passed, 1 skipped**（v1.52 后 464，本次 +7）。
- `ruff check`：engine/prompts/nudge/两个测试仅剩 **6 条预存告警**
  （SIM103/B905/F821×3/F841，HEAD 上就有），本次无新增。

## 边界

- 缺陷 B 的 `user`-first 兜底只在**送模型前**做，不改库里存的消息、不改界面。
- 历史脏数据不回溯。
