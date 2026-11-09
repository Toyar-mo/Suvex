"""技能调用统计的**唯一实现** —— 页面端点与对话工具共用。

## 为什么单独一个模块

这段逻辑原来内联在 `app/kernel/skills_router.py` 的 `GET /kernel/skills/usage`
里。现在对话工具 `list_skills(includeUsage=true)` 也要它 —— 如果两处各写
一份，迟早分叉：页面说"被加载 12 次"、对话说"3 次"，而差异来自
`MAX_USAGE_EVENTS` 或过滤条件被改了一边。统计数字对不上，用户没有任何
办法判断哪个是真的。

所以：查询、截断判定、名字解析都收在这里；router 负责转成响应模型，
engine 负责转成给模型看的文本。

## 口径

`activity_events` 里 `type='tool_call'` 且 `detail.tool='load_skill'` 的行。
`detail.command` 是模型传的参数 JSON（`{"name": "..."}`），在 Python 侧
解出名字 —— 不在 SQL 里解，因为 JSON 内层路径的写法在 PostgreSQL 与
SQLite 上是两套语法，写在 SQL 里会让这段只在生产能跑、测试跑不了。

它证明「请过这一篇」，不证明「读进去了」或「照做了」—— 给模型和给用户
的措辞都必须停在"被加载"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

#: 技能调用统计最多回溯多少条工具调用事件。
#:
#: 这条查询按 `detail->>'tool'` 过滤，那个键上没有索引，所以它**扫**事件表。
#: 不设上限的话，"打开技能页"的代价会随历史运行次数线性上升。
MAX_USAGE_EVENTS = 20_000


@dataclass(frozen=True)
class SkillUsageRow:
    """一篇技能的用量。`last_loaded_at` 是**最后一次**被请求的时刻。"""

    name: str
    loads: int
    last_loaded_at: datetime | None


@dataclass(frozen=True)
class SkillUsageReport:
    """一次统计的完整结果（含"这个数是不是截断的"这个事实）。"""

    rows: list[SkillUsageRow] = field(default_factory=list)
    #: 实际扫过的事件条数（含解析不出名字的 —— 比例因此是诚实的）。
    sampled: int = 0
    #: 更早的事件确实没算进来（多取一条判定出来的）。
    truncated: bool = False
    #: 本次使用的上限，界面用它说"只统计了最近 N 条"。
    limit: int = MAX_USAGE_EVENTS


def collect_skill_usage(db: Session, *, limit: int = MAX_USAGE_EVENTS) -> SkillUsageReport:
    """扫最近 `limit` 条 `load_skill` 调用，按技能名聚合。

    多取一条用来判定截断：拿到的行数 > 上限，说明更早的还有。这样
    "正好打满 20000 条"不会被误报成截断。
    """
    from app.events.models import ActivityEvent

    tool = ActivityEvent.detail["tool"].as_string()
    stmt = (
        select(ActivityEvent.detail, ActivityEvent.at)
        .where(ActivityEvent.type == "tool_call", tool == "load_skill")
        .order_by(ActivityEvent.seq.desc())
        .limit(limit + 1)
    )
    fetched = db.execute(stmt).all()
    truncated = len(fetched) > limit
    fetched = fetched[:limit]

    loads: dict[str, int] = {}
    last: dict[str, datetime] = {}
    sampled = 0
    for detail, at in fetched:
        sampled += 1
        raw = (detail or {}).get("command") if isinstance(detail, dict) else None
        name = ""
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                name = str(parsed.get("name") or "").strip()
        if not name:
            # 解不出名字的行**跳过但不隐藏**：`sampled` 里仍然计入它。
            continue
        loads[name] = loads.get(name, 0) + 1
        # 按 seq 降序遍历，第一次见到的就是这个技能最近的一次。
        last.setdefault(name, at)

    rows = [
        SkillUsageRow(name=name, loads=count, last_loaded_at=last.get(name))
        for name, count in sorted(loads.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return SkillUsageReport(rows=rows, sampled=sampled, truncated=truncated, limit=limit)


__all__ = [
    "MAX_USAGE_EVENTS",
    "SkillUsageReport",
    "SkillUsageRow",
    "collect_skill_usage",
]
