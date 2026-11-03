# 平台启用弹窗改居中 + 使用中平台给「同步题目」入口 + 英文碎句复读修复（v1.59）

2026-11-03

## 用户反馈（m07049）

> 哪有启用？把这个 ui 优化一下，别贴在右边，外部阻塞给他完善修复，之前记录的
> 「英文碎句复读」（Let me continue... ×3）修了。

三件事，逐条说明。

---

## 一、「哪有启用？」——两个真实的 UX 缺口

### 缺口 1：平台一旦「使用中」，「启用」按钮就消失了

看原来的逻辑（`PlatformPanel.tsx`）：`启用` 按钮的显示条件是
`item.id && !item.active && item.status === 'verified'` —— 也就是说**只有
"已识别但还没启用"的平台才有「启用」**。用户截图里的 `DASCTF Pro（slab-match）`
已经是「使用中」，于是整行只剩「删除」，用户当然找不到「启用」。

而且即便平台使用中，用户想做的"让 CTF 页出题"**没有任何可见按钮**可点
（以前得切到 CTF 页找工具条那个「同步比赛题」）。

**改法**：给**使用中的平台**加一个显式的「同步题目」按钮（`RotateCw` 图标，
调 `syncCompetition()`）。这样：
- 未启用的平台 → 看到「启用」；
- 已启用的平台 → 看到「同步题目」（随时能手动把题拉一遍，平台抖动时能重试）。

### 缺口 2：识别/启用表单贴在右边（抽屉），不好用

原来是 `Drawer`（右侧 34rem 竖长抽屉）。这个表单是「地址 + AccessKey +
大段接口文档」的**多字段输入**，塞进窄竖条里正文编辑区只剩几行 —— 那正是
`Modal` 组件 docstring 里写明该用居中弹窗的形态（"多字段表单" vs "看一条详情"
是两种语义）。

**改法**：`Drawer` → `Modal`（居中、`max-w-2xl`）。`Modal` 已经照抄了 Drawer
的焦点修法（onClose 进 ref 不进依赖，避免"敲一个字就失焦"），无障碍四件事
（Esc 关闭 / 焦点移入移出 / 锁 body 滚动 / portal 到 body）都齐。

---

## 二、「外部阻塞给他完善修复」——平台 accesskey 无权，代码改不了，只能如实+可行动

先说结论：**这个阻塞不在我们代码里，修不了**，但把"用户看到的"完善了。

### 实测证据（2026-11-03，线上生产账号）

```
GET https://pro.dasctf.com/slab-match/api/v1/agent/ctf/exercise-list
  Header: X-Agent-AccessKey: ak_****XyhT
→ HTTP 200 {"code":"40403","message":"无权操作","data":{"redirectUrl":null}}
```

- 换错 header（`Authorization: Bearer`、`X-AccessKey`、query 传 key）→
  平台回 `40001 方法参数传递不正确`（说明 header 名字是对的）；
- 用对的 header → `40403 无权操作`；
- `match-info` / `overview` 同样 `40403`。

即：**请求确实打到了平台、header 也对、平台认出了这个 key，但平台侧没给这个
key 开 agent 接口的权限**。这是平台账号权限问题，不是代码。

### 我们做的"完善"

1. **错误文案可行动**：`friendly_driver_error` 已经把 `无权操作` 翻成
   「比赛平台拒绝了这次请求（accesskey 没有 agent 接口的权限，或绑错账号）。
   请到「设置 → 解题 / 比赛平台」确认…平台原话：无权操作」。
2. **启用后自动同步的结果会用 toast 报出来**：`syncCompetition()` 会带上
   `result.message` 与 `result.errors[0]`，同步失败（含 40403）时以
   `tone:'risk'` 弹出，用户当场就知道"题没拉到，原因是 key 无权"，而不是
   切到 CTF 页看到空列表一头雾水。
3. **随时可重试**：「同步题目」按钮常驻，平台重新授权后点一下即可，无需重接。

### 要真正出题，需要你做的一件事

到 `pro.dasctf.com` 给这个 AccessKey（`ak_****XyhT`）**开 agent 接口权限**，
或在「平台接入」里换一个**已授权**的 key 再启用一次。一旦 key 有效，点
「启用」→ CTF 页自动出题（v1.58 的自动拉题已接线并有测试）。

---

## 三、「英文碎句复读」修复

### 线上原句

```
Let me continue.
I need to check the state.
Let me continue. I need to check the state.
```

### 根因

`nudge.looks_unfinished(text)` 是「零工具轮次」的纠偏判据，它要求
`len(text) < MAX_UNFINISHED_CHARS(200)` **且** 命中中文 `_INTENT` 正则
（"我先/接下来/让我"等）。这三句英文既没有中文意图词、又不到 200 字，于是
`looks_unfinished=False`；而这一轮 `tools_used==0` 又走不到工具分支 →
`should_nudge=False` → 整轮 `done`，界面停在"让我继续"上。

### 修法

`suvex-api/app/chat/nudge.py` 新增 `_EN_INTENT`：

```python
_EN_INTENT = re.compile(
    r"\b(let me|let us|let's|i'll|i will|i need to|i'm going to|i am going to|"
    r"now i|next i|next,|first,? i|first let me)\b",
    re.IGNORECASE,
)
```

`looks_unfinished` 改为 `_INTENT.search(body) or _EN_INTENT.search(body)`。
**仍受 200 字长度闸约束** —— 完整的长英文结论（顺带一句 "Let me explain"）不会
被打回，与中文 `_INTENT` 同一套弱信号处理。

### 验证

- `tests/chat/test_plan_detection.py` 新增 2 条：
  `test_english_intent_fragments_count_as_unfinished`（5 条英文碎句 → True）+
  `test_english_complete_answers_are_not_flagged`（短结论/长英文 → False）。
- **G0 RED→GREEN 验过**：把 `or _EN_INTENT.search(body)` 去掉 → 1 failed /
  25 passed；还原 → 26 passed。
- 全量 `pytest tests/chat tests/test_chat_stream.py` → **461 passed, 1 skipped**；
  `tests/test_chat.py` + 两个 kernel 用例 → **79 passed, 1 skipped**。
- 前端：`tsc -b` / `eslint` 干净，settings 20 + kernel 23 = 43 passed，
  `npm run build` 成功（`index-DPTC9kQX.js`）。
- 部署 Kali：`router.py` sha `de51ff58…`、`nudge.py` sha `014c5d9e…`（与本地
  相等），`Application startup complete`（PID 838348），health 200；dist 已同步。
- 线上 prod 探针：`EN_FRAG: True / SHORT_DONE: False / LONG_EN: False`。

---

## 改动文件

| 文件 | 改动 |
| --- | --- |
| `suvex-web/src/components/settings/PlatformPanel.tsx` | Drawer→Modal；使用中平台加「同步题目」按钮；`handleSync` |
| `suvex-api/app/chat/nudge.py` | 新增 `_EN_INTENT`；`looks_unfinished` 认英文意图 |
| `suvex-api/tests/chat/test_plan_detection.py` | 2 条英文碎句用例 |
