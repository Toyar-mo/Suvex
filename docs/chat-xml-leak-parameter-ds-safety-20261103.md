# 正文里漏出 `</parameter>` / `<ds_safety>`，甚至拼进要执行的命令（v1.52）

## 现象（用户 m05620 截图 +「输出内容是不是后面就乱了」）

直播流里三条脏东西：

1. `</parameter>` / `<p/parameter>` 出现在正文里：
   ```
   ...np.sort(M[ti])[-2]))\n<p/parameter>把 16 块 tile 分别...
   ... 2>/dev/null</parameter>继续：确认持久区里的拿到了 scriptobjects.bin...
   ```
2. 一整段 `<ds_safety>[用户未成年]否 [分类]其他 [判定]…[规则]无</ds_safety>`
   直接显示在回答里。
3. 英文碎句 `Let me continue. I need to check state and extract...` 换着说法复读 4 遍。

## 第 1、2 条：两个真 bug（不是"乱码"那么无害）

### Bug A —— `</parameter>` 漏出，而且**拼进了要执行的命令**

线上实测（会话 `chat_01M6BWQ69MZJNBAB7C7QMVXPV2`）：

- seq794 正文结尾是 `... 2>/dev/null</parameter> 继续：确认…` —— 存进 DB 时
  `</parameter>` **还在**（`has_param=true`）。
- seq818 更严重，是工具返回的 **stderr**：
  ```
  [stderr] bash: line 1: /parameter: No such file or directory
  ```
  即这个残留标记被当成了 shell 命令的一部分执行，bash 真的去找一个叫
  `/parameter` 的可执行文件。**这不是显示问题，是会跑错命令的问题。**

**根因（`engine._normalize_xml_actions` 的守卫）**：函数开头有个"要不要动手"
的判断，只有正文含下列任一子串才进入清洗：

```python
("<action>", "<analysis>", "<result>", "</result>", "</action>",
 "<parameter", "<invoke>", "</invoke")
```

问题就在这：守卫里是 **`"<parameter"`（开标签前缀）**，**没有 `"</parameter>"`**。
所以「只有收尾 `</parameter>`、没有开标签」的正文会**原样 return**，后面的
`_XML_STRAY_TAG_RE` 根本没机会执行。

复现（用 seq794 的真实文本）：

```
RAW_HAS:  True   # 原文有 </parameter>
NORM_HAS: True   # 规范化后还在 —— 就是这里漏的
```

而单独测 `_XML_STRAY_TAG_RE.sub("", "</parameter>")` 是能删干净的 —— 说明不是
正则错，是**守卫把整条路径短路了**。这是 v1.48 的遗留：当时把 `"<parameter"`
加进守卫引用了开标签，忘了收尾形态。

**修法**：不再手维护两套标签清单。新增 `_XML_ANY_PROTOCOL_TAG_RE`（和
`_XML_STRAY_TAG_RE` **同一个**标签集合），守卫改用它判：

```python
if not text or not (_XML_ANY_PROTOCOL_TAG_RE.search(text) or _is_dsml(text)):
    return text
```

`_XML_STRAY_TAG_RE` 也补上 `ds_safety`，两者保持同步。

### Bug B —— `<ds_safety>` 自检块整段进正文、并随历史回灌

模型自己发明了一套"安全自检" XML：
```
<ds_safety>[用户未成年]否
[分类]其他
[判定]用户输入为继续分析 CTF 加密游戏的技术请求，不涉及政治敏感内容。
[规则]无</ds_safety>
```
v1.41/v1.44/v1.48 都不认识这个标签 → 原样进正文（DB seq791 确认已落库），
而且会**随历史回灌**给模型 —— 和 v1.44 的 `<result>` 同一个自我强化坑。

**修法**：新增 `_XML_DS_SAFETY_RE = re.compile(r"<ds_safety\b[^>]*>.*?</ds_safety>", re.DOTALL)`，
在 `_normalize_xml_actions` 里和 `<analysis>`/`<result>` 一起整块删掉；落单的
收尾标记由 stray 正则兜。

### Bug C（同源）—— `holdback` 缺标签，导致"落库干净、界面脏"

前两条修的是**落库**。但用户看到的是**流式**。`holdback._XML_PROTOCOL_TAGS`
（控制直播流逐 token 放行）当时只有 `action`/`analysis`/`result` ——
**v1.48 的 `parameter`/`invoke` 与本次的 `ds_safety` 都忘了加**。所以即便落库
是干净的，浏览器里照样能看到 `</parameter>`。

**修法**：`_XML_PROTOCOL_TAGS` 补 `"<parameter"`/`"</parameter"`/`"<invoke"`/
`"</invoke"`/`"<ds_safety"`/`"</ds_safety"`（用不带 `>` 的开放前缀，收尾和带
属性形态都命中）。

> 教训：**"要不要清洗"的判据集合必须和"清洗器认识的标签集合"是同一份**。
> 两处手抄清单一定会漂移——v1.48 漏 `</parameter>`、v1.52 漏 holdback 就是
> 同一个病。现在 engine 侧用同一个正则兜住；holdback 侧单独一组，但注释里
> 明确写了"engine 加了新标签，这里必须同步"。

## 第 3 条：英文碎句复读（未在本次修复，如实说明）

`Let me continue. I need to check state...` 换着说法出现 4 遍、且没有命令块。
这是**模型把 reasoning 泄进 answer 通道**、并且没有输出 `ACTION:`。本次的
v1.50 纠偏 + v1.51 命令直执行都**抓不到它**（没有命令块可抽、也没有"接下来"
这类下一步提示可判）。要治它得走"零工具轮次的叙述重试"通道
（`MAX_NARRATION_NUDGES`），但那一档会误伤正常的长结论。**本次不动**，留作
一个已知项：它不影响正确性（不执行错命令、不丢结论），只是让回答啰嗦。

## 验证

- `tests/chat/test_xml_actions.py` +6 条（共 27 passed）：线上 seq794 真实形态、
  `</parameter>` 必须删净、**命令里不能再出现 `/parameter`**、`<ds_safety>` 整块删
  而保留前后人话、裸收尾标记删掉、`_has_protocol_marker_or_xml` 认得。
- `tests/chat/test_holdback.py` +1 条（共 30 passed）：三种标签都必须流式锁死。
- **G0 RED→GREEN 两处都验过**：
  - engine 守卫短路 → `test_xml_actions.py` **17 failed / 10 passed**；还原 → 27 passed。
  - holdback 去掉新标签 → `test_parameter_and_ds_safety_tags_block_live_stream` FAILED；还原 → 30 passed。
- 全量回归：`pytest tests/chat tests/test_chat.py tests/test_chat_stream.py`
  → **464 passed, 1 skipped**（v1.51 后 457，本次 +7）。
- `ruff check`：仅剩 **6 条预存告警**（SIM103/B905/F821×3/F841，HEAD 上就有，本次无新增）。

## 边界

- 已落库的历史脏文本（seq791/794/818 那些）**不回溯清洗**——库里是历史记录，
  不改写；新的一轮起正文干净。
- 第 3 条英文复读未修，见上。
