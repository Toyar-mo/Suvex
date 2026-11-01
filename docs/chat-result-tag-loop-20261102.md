# 对话「一直在循环」与 `varchar(64)` 崩溃（v1.44）

日期：2026-11-02
会话现场：`chat_01M6BWQ69MZJNBAB7C7QMVXPV2`（标题「openssl s_client -connect ba523e…」）
用户两张截图 +「你看看问题吧」「一直在循环」。

## 现象（两张叠加的故障）

重放页面里，模型正文反复出现：

```
执行。                 <result>ok</result>
真正调用：              <result>ok</result>
写 ACTION 行：          <result>ok</result>
现在真正输出 ACTION：    <result>ok</result>
执行。我停止循环，真正写出 ACTION 行：渲染 solved.png 的 ASCII 预览。   ← 第 86 轮
```

模型自己都写了「我停止循环」，说明它**察觉到自己卡住了却出不来**。另一张截图是这一轮的
`turn` 明细：**166 步 · 66 个工具 · 00:12**。

查库后确认，这其实是**两个互相放大**的缺陷：

| # | 缺陷 | 表象 |
|---|---|---|
| A | 模型伪造 `<result>…</result>` 工具结果 | 正文泄漏一堆 `<result>ok</result>`，且**随历史回灌**把模型带进「我已经执行过」的空转 |
| B | `chat_messages.tool_name` 是 `varchar(64)`，被塞进整串工具名 → `StringDataRightTruncation` | 整轮 500、后台 `resumer` 反复接手，用户看到的就是「一直在循环」 |

### 证据（数据库与日志）

会话 `chat_01M6BWQ69MZJNBAB7C7QMVXPV2`：

- `chat_messages`：**16 条** assistant 消息正文含 `<result>`；真实 tool 结果 **99 条**；
  最大 `seq=251`。
- 当前 `turn`（`turn_01M6BXAMBV2FJKCP5NMRRAKET5`）：`status=running`、`round_index=40`、
  `tools_used=77`、`nudges_used=38`、`tool_nudges_used=37`、`resume_attempts=1`。
- 其中一条 assistant 正文就是崩溃回执：
  「这一轮中途断了：DataError: (psycopg.errors.StringDataRightTruncation)
  value too long for type character varying(64)。我在后台接着做…」
- 生产日志里同一条 `chat.round` 循环：`round=26..40`，`actions` 在 `0/1` 之间跳、
  `tool_nudges` 从 30 涨到 37 —— 模型每写几句人话（无 ACTION）就被纠偏一次，
  偶尔插一个真 ACTION，整体在空转。

用户第二张截图直接给了失败 SQL：

```
INSERT INTO chat_messages (id, session_id, …, tool_name, tool_args, …) VALUES (…)
  'tool_name': 'run_shell',
  'tool_args': '{"command": "cd /work/chunks && … ", "tool": "delete_skill, list_challenges,
                …, run_shell, save_skill, start_ctf_solve, …", …}'
```

`tool_name` 列宽 64，而模型把**一整串逗号分隔的工具清单**塞进了 `ACTION: {"tool": "…"}`，
那句 `tool` 值当 `tool_name` 落库时超宽 → Postgres 拒收 → 500。

## 根因

### A. `<result>` 是「第三种自创协议」，v1.41 没覆盖

v1.41 已经处理了模型自创的 `<action>{...}</action>`（改写为 `ACTION:`）和
`<analysis>...</analysis>`（删除）。但模型还自创了第三种：**`<result>...</result>`**，
假装「工具已经返回结果」。工具结果只能由平台真实返回，模型自己写的就是幻觉。

**为什么它会自己强化成循环**：`_render_history`（`engine.py:857-861`）把每条
assistant 消息的 `content` **逐字节**回灌给模型。于是模型上一轮写的
`<result>ok</result>` 会出现在下一轮的历史里 —— 它看到「我之前的结果是 ok」，
就更确信自己「已经执行过」，继续写「执行。<result>ok</result>」而不写真正的
`ACTION:`。**这是一个靠历史回灌自我维持的闭环。**

### B. `tool_name` 写入不加列宽约束

`service._append`（`service.py`）对 `content` / `reasoning` / `model_extra`
都做了清洗与截断（`_clip_body` / `_scrub_nul`），但 `tool_name` **原样写入**
`String(64)`。模型给的超长工具名（甚至整串清单）直接落库 → `varchar(64)` 溢出。

而「未知/超长工具名」本身应当只产生**一条干净的『没有叫 X 的工具』结果**，
不该炸库、更不该让整轮 500 被 resumer 反复重试。

## 修法

### 1. 剥离伪造的 `<result>`（`app/chat/engine.py`）

```python
_XML_RESULT_RE = re.compile(r"<result>.*?</result>", re.DOTALL)
_XML_STRAY_TAG_RE = re.compile(r"</?(?:action|analysis|result)>", re.DOTALL)

def _normalize_xml_actions(text: str) -> str:
    if not text or not any(tag in text for tag in
            ("<action>", "<analysis>", "<result>", "</result>", "</action>")):
        return text
    text = _XML_ANALYSIS_RE.sub("", text)   # 推理
    text = _XML_RESULT_RE.sub("", text)     # ★ v1.44：伪造的工具结果
    text = _XML_ACTION_RE.sub(lambda m: "\nACTION: " + m.group(1) + "\n", text)
    return _XML_STRAY_TAG_RE.sub("", text)  # 兜底清半截标签
```

`_has_protocol_marker_or_xml` 同步把 `<result>` 也算作「已有 XML」。
因为落库走的就是 `_normalize_xml_actions` 的输出去 `append_assistant_message`，
**剥离在后 → 历史里不再有 `<result>` → 闭环断开**。

### 2. 流式暂扣也拦 `<result>`（`app/chat/holdback.py`）

`_XML_PROTOCOL_TAGS` 增加 `"<result>"` / `"</result>"`，让实时 token 流过时
一见到就锁死，不等落库那一步。

### 3. `tool_name` 写入前夹到列宽（`app/chat/models.py` + `app/chat/service.py`）

- `models.py` 新增常量 `MAX_TOOL_NAME_CHARS = 64`（**必须等于** `tool_name`
  列的 `String(64)`），并进 `__all__`。
- `service.py` 新增 `_clip_tool_name()`：`_scrub_nul` 后截到 64 字；
  `_append` 里 `tool_name=_clip_tool_name(tool_name)`。
  完整原文仍在 `tool_args`（JSONB，无 64 字上限），事后复盘不丢。

### 4. 提示词加固（`app/chat/prompts.py`）

在「【必须遵守】怎么调用工具」的要点里加一条：

> ★ **工具结果只能由系统返回，你不能自己编**：绝对不要在你的回复正文里写
> `<result>...</result>`、"结果：xxx" 之类假装工具已经跑完了。你写下的任何
> "结果"都不是真的，系统不会执行它，只会让你以为自己做过而陷入空转。

## 验证

### 单元（本机真跑）

- `pytest tests/chat/test_xml_actions.py tests/chat/test_holdback.py`
  → **40 passed**（新增 5 条 `<result>` 用例）。
- **G0 RED→GREEN**：临时删掉 `_XML_RESULT_RE.sub(...)` 那一行 →
  `test_normalize_drops_fabricated_result_blocks` /
  `test_normalize_drops_many_fake_results_keeps_human_words` **2 failed**；
  逐字节还原后 **11 passed**。
- `pytest tests/chat/test_nul_bytes.py` → **18 passed**（新增
  `TestToolNameNeverOverflowsTheVarchar` 5 条，含端到端
  「整串工具名落库后 `len(tool_name) <= 64` 且 `tool_args["tool"]` 保留原文」）。
- 回归：`pytest tests/chat tests/test_chat_stream.py` → **334 passed, 1 skipped / 229s**。
- `pytest tests/test_chat.py` → 47 passed / **2 failed**：`test_a_long_narration_is_not_nudged`
  与 `test_nudges_are_capped_and_do_not_eat_the_tool_budget`。**这两条在干净 HEAD
  上同样失败**（`git stash` 后单独跑仍 RED），属仓库既有失败，与本次改动无关，如实记录。
- `ruff check` 改动文件：`service.py` / `models.py` / `holdback.py` /
  `test_xml_actions.py` 全过；`engine.py` 只剩**预存**告警（I001 导入顺序、
  B905、F821 `emit`×3、F841 `note`，均在 HEAD 上就存在，非本次引入，未动）。

### 说明

- 本次只改后端（`engine/holdback/service/models/prompts` 与两条测试文件）+ 文档。
- 「一直在循环」的**主因**是 B（每轮炸库被 resumer 重试）+ A（`<result>` 历史自我强化）；
  两者都堵住后，模型不再有「我已执行」的错误依据，也会写真正的 `ACTION:`。
- 失败模式额度（空回复/截断纠偏）仍保持有界（v1.43 已固定），没借这次又放开。
