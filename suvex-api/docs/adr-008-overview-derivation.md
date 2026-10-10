# ADR-008：`/overview` 的投影冲突，与两处被"绿灯"掩盖的历史遗留

- **状态**：已采纳（后端任务 6 的收尾）
- **相关**：契约 §2.5、§3.4.2、§3.6、§9

## 这件事是怎么被发现的

任务 6 的正式交付物是**报告**。做完报告、跑完整套验收之后，
我回头读验收脚本自己的输出，看到一行：

```
[PASS] 未实现端点返回 501 并说明计划任务
       全局统计（Overview）尚未实现（计划在后端任务 6 中交付）。
```

**任务 6 已经做完了。** 而 `/overview` 的消息还在说"计划在任务 6 中交付"。

那一刻暴露的是三件事叠在一起：

1. 契约 §9 把 `/overview` 排在任务 6，而任务 6 的实际内容是报告
   —— **排期本身错了**；
2. 那个 501 消息从此是假话，但**一个永远返回 501 的端点自己不会喊**；
3. 最糟的是：**我的验收脚本把这个 501 断言成了 `[PASS]`**。
   它钉的是"机制在"（501 + 任务号），恰好把"承诺已经过期"洗成了绿灯。

第 3 条是这条 ADR 真正的主题。一个**只检查机制还在、不检查承诺是否兑现**
的断言，会随着时间推移从"验证"退化成"背书"。

## 决定一：实现 `/overview`，而不是改排期

技术上没有障碍 —— 它需要的每一样东西任务 5 就已经交付了：
`findings.overview_metrics()`（内部调 `stats()`，是统计的唯一来源）、
带 `agentPhase` 的事件、带 `durationSec` 的扫描。

所以选择实现它。**全部派生，不新增任何表**：

| 字段 | 来源 |
| --- | --- |
| `metrics.*` | `findings.overview_metrics()` → `stats()` |
| `activeScans` / `hasAnyScan` | `scans` 表（与 `/scans`、`/scans/queue` 同表） |
| `avgScanDurationSec` | 已完成扫描的 `duration_sec` |
| `severityBreakdown` | `findings.overview_metrics()`（**含误报**） |
| `activity` | `activity_events`（完整事件 → 投影） |

### 三个 `null` 的语义是这次实现的真正难点

契约 §3.6 用 ⚠️ 标了它们，而它们难在**写错不会报错**：

* `newHighSeverityDelta: null` = **没有上周数据**。返回 0 会被读成"与上周持平"；
* `avgScanDurationSec: null` = **没有已完成扫描**。返回 0 会让用户以为扫描瞬间完成。

0 秒、0 变化都是**看起来完全合理**的数字。所以两条各有一个专门的测试
（`test_empty_state_returns_nulls_not_zeros`、`test_new_high_severity_delta_*`）。

### `avgScanDurationSec` 只算 `completed`

不含 `failed` / `aborted`：那两次的耗时回答的是"多久放弃的"，
与"扫描一次要多久"不是同一个问题 —— 混进去会把这个数字拉低，
而它恰恰是用来做容量预估的。

## 决定二：契约 §2.5 与 §3.6 自相矛盾，选 §3.6

契约 §2.5（自己用 ⚠️ 标了"最容易出错的一处"）说：

> Overview 的 6 种投影**由前端从完整事件派生**，不要后端再发一套精简流。

而 §3.6 的 `/overview` 响应里 `activity` 写的是「投影形状，**见 §2.5**」。

**后者指向前者，前者否定了后者。** 必须择一：

| 选 | 后果 |
| --- | --- |
| §2.5 | `activity` 发完整事件 —— 但 `OverviewData.activity` 是**已定稿的扁平类型**（`subject` 而非 `detail`），前端接线时类型直接不匹配 |
| §3.6 | 后端发投影 —— 与 §2.5 的建议背道而驰 |

**选 §3.6**，因为 §2.5 反对后端投影的**依据**在一处细节上不成立：

§2.5 的理由是「投影是展示层视图模型，后端产出它等于把展示逻辑搬到后端，
UI 一改摘要规则就要动后端」。但看它自己的字段表 ——
**投影与完整事件的 `summary` 是同一个字段**，投影真正丢掉的只是 `detail`
（换成扁平的 `subject`）。也就是说**"摘要怎么截"本来就在产生事件的那一侧**，
后端并没有多承担什么。

而 `OverviewData` 是已经定稿的前端类型。让响应对不上它，前端要么改类型、
要么在页面里**再写一次投影** —— 后者正是 §2.5 原本想避免的。

契约里 §2.5 的建议已按此**收窄为「除 `/overview` 外」**，并写明了理由。

### 实现上踩到的第一个坑：按 `seq` 排序

投影做完，活动流是**按 `seq`（入库顺序）倒序**的。测试全绿 ——
因为测试里事件都是顺序造的，入库顺序与发生时刻一致。

**但验收脚本里它是红的。** 真实链路里事件是从外部库**批量导入**的，
`at` 来自外部工具的时间戳，与入库顺序不一致 ——
于是活动流顶上的不是最近发生的事。

契约 §4.4 专门强调过 `at` 是**事件发生时刻**而非入库时刻，
前端 `buildOverviewData` 也正是 `sort((a, b) => b.at.localeCompare(a.at))`。

修法是按 `at` 排序，并补了一条**专测这个分歧**的用例
（`test_activity_sorts_by_event_time_not_insertion_order`）：
刻意让先入库的事件发生时刻更晚，按 `seq` 排会得到相反次序。

> 值得记：这是我改完实现、测试全绿之后**又跑验收**才发现的。
> 单元测试用的是"我自己造的、次序干净的数据" —— 那一层
> 结构性地看不见"入库顺序 ≠ 事件时间"这件事。

## 决定三：修掉 `bySeverity` 的 `Partial` 语义偏离（任务 5 遗留）

写 `/overview` 的测试时拿"契约逐字 vs 实际响应"硬对，发现：

**契约 §3.4.2 说「为 0 的档**键不存在**」，而 `/findings/stats` 实际返回的是**

```json
{"critical": 1, "high": null, "medium": null, "low": null, "info": null}
```

原因是 `SeverityCounts` 的字段声明为 `int | None`，Pydantic 默认把 `None`
序列化成**显式的 null**，而不是省略键。

### 为什么它藏了这么久

三个"兼容"叠在一起，把它完全盖住了：

1. 前端读写都兼容（`counts[sev] ?? 0`）—— `null` 和"键不存在"在 JS 里
   经过 `?? 0` 之后是同一件事；
2. 任务 5 的**验收脚本**在比较前把假值**过滤掉**了：
   `by_severity = {k: v for k, v in ... if v}` ——
   于是 `null` 和"键不存在"在它眼里也是同一件事。**那条检查是空转的**；
3. 测试断言的是 `snapshot.bySeverity.medium is None` ——
   那是对**对象属性**的断言，而问题出在**序列化**上，两者不是一回事。

**"两边都接受"恰恰是它危险的地方**：契约说"键不存在"，响应对不上，
下一个**严格按契约**写解析逻辑的人（比如用 `Object.keys()` 数档位、
或者断言 `"medium" in body`）就会踩空。

修法是给那条路由加 `response_model_exclude_none=True`。
它在 `FindingStatsOut` 上是**精确命中**的：可空的只有 `bySeverity` 的五个字段，
`byStatus` / `byCategory` 是 `dict[str, int]`，永不为 `None`。

## 代价（如实记）

1. **契约里多了"一个例外"**。§2.5 的规则不再是无条件的 ——
   读者必须记得"`/overview` 除外"。这是选 §3.6 的直接代价，
   而另一个选项（前端自己投影）的代价是前端多一套投影逻辑。
2. **`activity` 的后端投影要跟着前端的展示意图走**。§2.5 担心的那件事
   在**这一处**是真的：如果哪天 Overview 的摘要规则变了，
   后端可能也要动。目前投影只做 `detail` → `subject` 的映射，
   规则很薄 —— 但这是个需要盯着的地方。
3. **`subject` 与前端 mock 的取值风格必然有差异**。mock 里的 `subject`
   是手写的（`'GET /debug/env'`），真实数据只能从 `detail` 派生，
   派生不出的**如实返回 `null`**，不编占位串。
4. **`/overview` 的 `avgScanDurationSec` 与 `/scans` 列表可能算出不同值** ——
   如果列表接口以后加了筛选/分页。目前两者都取全表，所以一致；
   加筛选时要注意这一条。

## 测试

新增 22 个用例（`tests/test_overview.py`）。最关键的四类：

* `test_empty_state_returns_nulls_not_zeros` —— 两个 `null` 不能被写成 0
* `test_severity_breakdown_matches_stats_endpoint` —— 与 `/findings/stats` 逐档相等
* `test_activity_sorts_by_event_time_not_insertion_order` —— 排序键是 `at` 不是 `seq`
* `test_service_uses_stats_not_a_second_count` —— 用探针证明它**确实**调了 `stats()`，
  而不是"两个数字恰好相等"（并发编辑下各自数一遍就会不等，而那正是要防的）

另外 `test_protected_endpoints.py` 里那条"501 占位"的测试**换了目标**：
从 `/overview` 换成确实未实现的 `/settings`（任务 7），
并加了断言"它还没被实现"——一旦 `/settings` 被做了，这条会红，
提醒换到下一个还没做的端点上，**而不是继续对着旧行为打勾**。

对应地新增了 `test_overview_is_not_a_stub_anymore`：钉住已经交付的端点
不再假装没做。两条成对存在。

## 若以后又要挑"未实现端点"当例子

别挑**有明确计划任务号**的。挑一条，然后跟一条"它还没被实现"的断言。
一条断言着"某个未来任务才会做的东西"的测试，会在那个任务做完之后
**悄悄变成谎话** —— 而它不会报错。
