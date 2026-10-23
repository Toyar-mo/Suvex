"""`facts` 领域：扫描过程的**项目黑板**（v0.79）。

对外只导出两样东西：

* `models.ScanFact` —— 表结构
* `service` —— 校验 + UPSERT + 索引块渲染

## 为什么校验和渲染都在这一层，而不是分散到 kernel

`kernel` 只该知道「有这个工具可以调」，不该知道 fact_key 该长什么样、
confidence 有哪三态、索引块多少字符截断。把这些留在 `facts` 里，
`kernel` 那边只是一次函数调用 —— 将来接别的引擎时（比如换掉 multiagent），
校验规则不会跟着蒸发。
"""

from __future__ import annotations

from app.facts.models import FACT_CATEGORIES, FACT_CONFIDENCES, ScanFact

__all__ = ["FACT_CATEGORIES", "FACT_CONFIDENCES", "ScanFact"]
