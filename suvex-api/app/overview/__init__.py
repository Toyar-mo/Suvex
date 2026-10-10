"""`overview` 领域（全局统计）。

**没有表**，也没有 models.py —— 这个领域的一切都是从别的领域**派生**的。

契约 §3.6 明确要求派生而不是写死：

> `activeScans`、`avgScanDurationSec`、`severityBreakdown` 都应该从集合算出来。
> 前端 mock 就是这么做的，理由是「换真实 API 时派生逻辑一行不用改」，
> 且不会出现「卡片写 12 但列表只有 10 条」。

三个 null/空值的语义也必须守住（契约 §3.6 用 ⚠️ 标了）：
`newHighSeverityDelta: null` 是「没有上周数据」而**不是** 0；
`avgScanDurationSec: null` 是「没有已完成扫描」而**不是** 0。
0 变化和无法比较是两回事；0 秒会让用户以为扫描瞬间完成。

⚠️ `service.py` 的模块 docstring 记了**契约内部的一处不一致**：
§2.5 说「Overview 的投影由前端派生，不要后端再发一套」，而 §3.6 的
`/overview` 响应里 `activity` 要的恰恰是投影形状 —— 后者指向前者，
前者否定了后者。那里写清了本实现选 §3.6 的理由，以及如实记下的代价。
读这个领域之前先看那一段。
"""
