# 对话把自创 XML 协议（`<action>`/`<analysis>`）漏进气泡、且动作不执行（v1.41）

## 现象（用户截图）

- turn 停在计划句：「（这一轮停在了下一步上：「继续」我接着执行 —— 上面那句是计划，不是结果。）」，47 步·18 工具，需要点「继续」。
- 同一轮的消息正文里**裸露出内部协议**：`</action>`、`<action>{"tool": "run_shell", "args": {...}}</action>`、`<analysis>Wait for result — this will show me whether the image contains readable text.</analysis>`，141 步·35 个工具。

## 根因（线上实测，会话 `chat_01M690GSFE4SSP0DB5BHR0BNDK`）

模型 deepseek-v4.1-flash 在长链解题里**没走 `ACTION:` 文本协议**，而是自创了 XML 风格：

```
<analysis> I need to just run one command. </analysis>
<action> {"tool": "run_shell", "args": {"command": "..."}} </action>
```

而对话侧所有「解析 / 剥离 / 推送」逻辑只认 `ACTION:` / `FINAL:`：

- `parse_actions`（`app/kernel/runner.py:5072`，经 `_iter_json_blocks`/`_MARKER = ^\s*(ACTION|FINAL)\s*[:：]`）→ **XML `<action>` 解析不出来**；
- `_narration_before_action`（`engine.py:1427`）→ **切不掉 XML 块**，整段原样落库；
- `HoldbackBuffer`（`app/chat/holdback.py`，`PROTOCOL_MARKERS=("ACTION","FINAL")`）→ **流式时拦不住 `<action>`**，实时推给前端。

后果三层，全部静默：

1. **不执行**：`parse_actions` 认不出 XML → 动作从未执行。该会话 **576 个 `<action>` 标签，只有 54 条工具结果**（其余来自原生 `tool_calls` 经 `_with_native_actions` 合成执行）。
2. **不隐藏**：整段 XML 原样落进 process 面消息，用户看到裸 `<action>` + JSON。
3. **烧纠偏、停在计划句**：模型以为做了事、系统判它"只说话没行动"，反复纠偏 —— `turn_01M6ANHCYQ…` 记 `round_index=7 / tools_used=45 / nudges_used=95 / tool_nudges_used=93 / resume_attempts=3`，`last_error=本轮停在下一步计划上，尚未给出可交付结论`。这正是用户看到的"停在下一步的计划上"。

## 修法

在对话侧加一层归一化，把自创 XML **改写成文本协议**、删掉推理块，让下游逻辑原样复用：

`app/chat/engine.py` 新增：

```python
_XML_ACTION_RE = re.compile(r"<action>\s*(\{.*?\})\s*</action>", re.DOTALL)
_XML_ANALYSIS_RE = re.compile(r"<analysis>.*?</analysis>", re.DOTALL)

def _normalize_xml_actions(text: str) -> str:
    if not text or ("<action>" not in text and "<analysis>" not in text):
        return text
    text = _XML_ANALYSIS_RE.sub("", text)                    # 推理不进正文
    return _XML_ACTION_RE.sub(lambda m: "\nACTION: " + m.group(1) + "\n", text)  # 动作改写成 ACTION:

def _has_protocol_marker_or_xml(text: str) -> bool:
    if "<action>" in (text or ""):
        return True
    return _has_protocol_marker(text)
```

接线：

- 主循环拿到 `raw_text = reply.text or ""` 后**立即** `raw_text = _normalize_xml_actions(raw_text)`，再做 `split_final` / `_narration_before_action` / `parse_actions`。于是 XML 动作**被执行**、XML 文本**被剥离**。
- `_with_native_actions` 的判据从 `_has_protocol_marker` 改为 `_has_protocol_marker_or_xml`：正文已有 XML 动作时不再合成 `ACTION:`，避免重复执行。

`app/chat/holdback.py`：

- 新增 `_XML_PROTOCOL_TAGS = ("<action>","</action>","<analysis>","</analysis>")`。
- `_verdict` 里：**当前行**（到第一个换行为止）出现任一标签 → `_PROTOCOL`（锁死）；行尾是标签前缀（`<act`、`<analy`）→ `_UNDECIDED`（待定，不先放半截）。这样流式时 XML 也不漏。

## 验证（真跑）

- 新增 `suvex-api/tests/chat/test_xml_actions.py`（6 项）：单个动作改写并可被 `parse_actions` 解析、`<analysis>` 整块删除、多动作全部解析、归一化后 `_narration_before_action` 无任何 XML/JSON 泄漏（人话保留）、无 XML 时逐字节不变、`_has_protocol_marker_or_xml` 识别 XML。
- `test_holdback.py` 新增 3 项：`<action>` 块流式锁死且标签前正文可放、逐字符增量（`<act`+`ion>`）也锁死、`<analysis>` 流式锁死。
- `pytest tests/chat/test_xml_actions.py tests/chat/test_holdback.py` → **35 passed**。
- `pytest tests/chat`（全量）→ **304 passed, 1 skipped / 222.73s**，无回归。
- `ruff check` 改动文件：仅剩 8 条**预存**告警（I001 导入顺序 / B905 zip strict / F821 emit / F841 note，均在既有代码，非本次引入）；新增函数与测试无新增告警。

## 部署与线上验证

- 部署到 Kali 生产 `/root/suvex`（复制部署）：备份 `engine.py.bak-20261101-v141`/`holdback.py.bak-20261101-v141` → `suvex-sandbox install -m 644` 覆盖 → 生产 sha 与本地逐一相等：`engine.py=6d68387a73ddd7416c7e6902b47aa8fb4f4de62ad64fb0cf87de3e4e4727cf60`、`holdback.py=6c5ce9dc4617e1701ede5b72efd5fc95b876723a95334ec8c35187c073f0b25a`。
- `nsenter ... systemctl restart suvex-api` → `Application startup complete`（2026-11-01 01:12:21）、`39 张表`、无 traceback；`systemctl is-active`=active；`GET /api/v1/health`=200 `{"status":"ok","version":"0.1.0"}`。
- 线上功能验证（生产 venv，`PYTHONPATH=/root/suvex/suvex-api .venv/bin/python`）：用含 `<action>{...}</action>`、`<analysis>...</analysis>` 的样本文本调生产代码，输出 `HAS_XML_ACTION_TAG:False`、`HAS_ANALYSIS_TAG:False`、`PARSED:[('run_shell',{'command':'python3 /work/suc.py'}),('run_shell',{'command':'echo EXIT=$?'})]`、`NARRATION_LEAK:False`、`NARRATION_HEAD:'脚本没输出，再跑一次。'`、`MARKER_OR_XML:True` —— 证明生产上 XML 被剥离、动作可被解析执行、人话保留、无正文泄漏。

## 说明

- 这是**兼容层**，不改 `app/kernel/runner.py` 的 `_MARKER`/`parse_actions`（解题侧共用，改它风险大）。对话侧把 XML 归一化后，解析/剥离/推送三处自然一致。
- 历史已落库的 XML 消息不会自动清洗（那是过去的数据）；此后新产生的轮次不再泄漏、且 XML 动作会被执行。
