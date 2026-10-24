"""技能管理的 HTTP 端点（v0.88）。

## 为什么单独一个路由模块，不并进 `kernel/router.py`

两个理由，第二个是决定性的：

1. **职责不同**：`kernel/router.py` 那一组回答"解题内核怎么跑"（账号、
   赛事、题目、队列）。技能管理回答"模型能查到哪些知识" —— 它是
   **知识库的编辑面**，与解题流水线无关。
2. **那个文件已经 1600+ 行，且是多会话并行编辑的热点**。往里插一整段
   会持续撞车（实测：本轮三次编辑都因为"file changed since it was read"
   失败）。独立模块把冲突面降到最小 —— 这不是洁癖，是并行开发的现实约束。

## 鉴权：读 read、写 admin

与工具清单那条的理由**不同**：这里写的是**内容**，而内容会进模型上下文。
技能正文是"教模型怎么做事"的文本，能改它的人就能改变智能体的行为，
包括让它绕过某条纪律。这与 `PUT /kernel/settings` 同级。

## ⚠️ 内置技能只读

它们随代码分发、部署时整目录覆盖。允许写会让用户创建的技能
在下次部署时**静默消失**（见 `skill_store.py` 文件头）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import Identity, require_admin, require_read
from app.core.db import get_db
from app.core.errors import ApiError
from app.core.vocab import AuditAction
from app.kernel.schemas import (
    KernelSkillCategoryOut,
    KernelSkillOut,
    KernelSkillsOut,
    SkillCreateIn,
    SkillDetailOut,
    SkillUpdateIn,
    SkillUsageListOut,
    SkillUsageOut,
)

router = APIRouter(prefix="/kernel", tags=["Kernel · Skills"])

ReadDep = Depends(require_read)
AdminDep = Depends(require_admin)
DbDep = Depends(get_db)

#: 技能调用统计最多回溯多少条工具调用事件。
#:
#: ★ 为什么要有个上限：这条查询要按 `detail->>'tool'` 过滤，那个字段
#:   上没有索引（它是 JSONB 内部的一个键），所以它**扫**事件表。
#:   不设上限的话，"打开技能页"的代价会随历史运行次数线性上升 ——
#:   而那和"我想看一眼有哪些技能"没有关系。
#:
#: 取 20000 的理由：一次扫描的工具调用量级在几十到几百，20000 条覆盖
#: 大约上百次扫描。超出的部分界面会明说"只统计了最近 N 条"，不假装全量。
MAX_USAGE_EVENTS = 20_000


def _find_skill(name: str):  # noqa: ANN202 - 返回 Skill dataclass
    """按名字找一篇技能（内置或自定义）。找不到抛 404。"""
    from app.kernel import skills as skills_mod

    by_name = skills_mod.skills_by_name(skills_mod.all_skills())
    skill = by_name.get(name)
    if skill is None:
        raise ApiError.not_found(f"没有叫 `{name}` 的技能。")
    return skill


@router.get(
    "/skills",
    response_model=KernelSkillsOut,
    summary="技能库清单（load_skill 可加载的速查卡）",
)
def list_kernel_skills(_identity: Identity = ReadDep) -> KernelSkillsOut:
    """列内置 + 自定义技能，按分类分组。

    ★ `read` 级：回的是静态文本清单（名字 + 一句话描述），
    不含凭据、不改状态、不触发执行。

    ⚠️ 不回 `path`：那是宿主文件系统路径，界面用不到且会泄露部署结构。

    ⚠️ 空分类**照样列出来**：那说明"这一栏本该有东西但打包漏了"，
    而不是"没有这一栏"。两者在界面上是两回事。
    """
    from app.kernel import skills as skills_mod

    items = skills_mod.all_skills()
    grouped: dict[str, list[dict]] = {}
    for skill in items:
        grouped.setdefault(skill.category, []).append(
            {
                "name": skill.name,
                "category": skill.category,
                "description": skill.description,
                # ★ 清单里带上来源，前端才能直接判断"这一篇能不能改/删"。
                #   不带的话，界面要为每一篇再发一次详情请求才知道，
                #   而那些请求还会把自定义技能的正文一起传回来。
                "source": getattr(skill, "source", "builtin"),
            }
        )
    for bucket in grouped.values():
        bucket.sort(key=lambda item: item["name"])
    return KernelSkillsOut(
        total=len(items),
        categories=[
            KernelSkillCategoryOut(
                category=category,
                skills=[KernelSkillOut(**skill) for skill in bucket],
            )
            for category, bucket in sorted(grouped.items())
        ],
    )


@router.get(
    "/skills/usage",
    response_model=SkillUsageListOut,
    summary="技能调用统计（哪几篇真的被 load_skill 拉过）",
)
def kernel_skills_usage(_identity: Identity = ReadDep, db: Session = DbDep) -> SkillUsageListOut:
    """统计每篇技能被 `load_skill` 拉过几次、最后一次是什么时候。

    ## 这个数**证明什么、不证明什么**

    证明：模型请求过这一篇（活动流里有对应的 `tool_call` 事件）。
    不证明：它读进去了、它照做了、它因此解出了题。

    ★ 所以界面上的措辞必须是「被加载」，不能写「有用」「命中」「采纳」——
    那是从这个数里推不出来的东西。把它写大一点很好看，但它就是编的。

    ## ⚠️ 路由顺序：这个端点必须注册在 `/skills/{name}` **之前**

    FastAPI 按注册顺序匹配。放后面的话 `GET /kernel/skills/usage` 会先撞上
    `/skills/{name}`，把 `usage` 当成一个技能名 → 404「没有叫 usage 的技能」。
    这个失败的形状很坏：端点**在**，只是永远回 404。
    下面 `list_kernel_skills` 之后紧跟本函数，就是为了这个。

    ## 数据来源

    `activity_events` 里 `type='tool_call'` 且 `detail.tool='load_skill'` 的行。
    `detail.command` 是模型传的参数 JSON（`{"name": "..."}`），在 Python 侧
    解出名字 —— 不在 SQL 里解，因为 JSON 内层路径的写法在 PostgreSQL
    与 SQLite 上是两套语法，写在 SQL 里会让这段只在生产能跑、测试跑不了。
    """
    import json

    from app.events.models import ActivityEvent

    tool = ActivityEvent.detail["tool"].as_string()
    stmt = (
        select(ActivityEvent.detail, ActivityEvent.at)
        .where(ActivityEvent.type == "tool_call", tool == "load_skill")
        .order_by(ActivityEvent.seq.desc())
        .limit(MAX_USAGE_EVENTS)
    )

    loads: dict[str, int] = {}
    last: dict[str, object] = {}
    sampled = 0
    for detail, at in db.execute(stmt).all():
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
            # 解不出名字的行**跳过但不隐藏**：`sampledEvents` 里仍然计入它，
            # 所以界面上的比例是诚实的。
            continue
        loads[name] = loads.get(name, 0) + 1
        # 按 seq 降序遍历，第一次见到的就是这个技能最近的一次。
        last.setdefault(name, at)

    items = [
        SkillUsageOut(
            name=name,
            loads=count,
            lastLoadedAt=last[name].isoformat() if last.get(name) else None,
        )
        for name, count in sorted(loads.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return SkillUsageListOut(sampledEvents=sampled, since=None, items=items)


@router.get(
    "/skills/{name}",
    response_model=SkillDetailOut,
    summary="单篇技能详情（自定义的含正文）",
)
def get_kernel_skill(name: str, _identity: Identity = ReadDep) -> SkillDetailOut:
    """看一篇技能的详情。

    ⚠️ **只有自定义技能回正文。** 内置的正文由 `load_skill` 给模型，
    页面不需要全文（可能几百 KB），回显它也没有用 —— 内置的改不了。
    """
    from app.kernel import skill_store

    skill = _find_skill(name)
    source = getattr(skill, "source", "builtin")
    body = ""
    if source == "custom":
        try:
            raw = skill_store.read_custom_skill(name=name, category=skill.category)
            _, body = skill_store.split_content(raw)
        except skill_store.SkillWriteError:
            body = ""  # 读不到就给空，界面显示"读不到正文"
    return SkillDetailOut(
        name=skill.name,
        category=skill.category,
        description=skill.description,
        source=source,
        body=body,
        lineCount=(body.count("\n") + 1) if body else 0,
    )


@router.post(
    "/skills",
    response_model=SkillDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建一篇自定义技能",
)
def create_kernel_skill(
    payload: SkillCreateIn,
    request: Request,
    identity: Identity = AdminDep,
    db: Session = DbDep,
) -> SkillDetailOut:
    """新建自定义技能（**只写自定义目录**）。

    ★ 留痕：技能正文会进模型上下文，改它能改变智能体行为 ——
    事后要能回答"这个技能是谁加的"。
    """
    from app.kernel import skill_store

    try:
        skill_store.ensure_custom_root()
        skill_store.create_or_update_skill(
            name=payload.name,
            category=payload.category,
            description=payload.description,
            body=payload.body,
        )
    except skill_store.SkillWriteError as exc:
        raise ApiError.validation(str(exc), {"detail": exc.message}) from exc

    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.KERNEL_SKILL_CREATE,
        summary=f"新建技能 {payload.category}/{payload.name}",
        detail={
            "name": payload.name,
            "category": payload.category,
            "chars": len(payload.body),
        },
    )
    return SkillDetailOut(
        name=payload.name,
        category=payload.category,
        description=payload.description,
        source="custom",
        body=payload.body,
        lineCount=payload.body.count("\n") + 1,
    )


@router.put(
    "/skills/{name}",
    response_model=SkillDetailOut,
    summary="改一篇自定义技能",
)
def update_kernel_skill(
    name: str,
    payload: SkillUpdateIn,
    request: Request,
    identity: Identity = AdminDep,
    db: Session = DbDep,
) -> SkillDetailOut:
    """改自定义技能。

    ⚠️ **分类不可改**：它决定文件在哪，改分类等于"删一个建一个"，
    那会让"编辑"产生副作用（原文件消失）。换分类请删了重建。
    """
    from app.kernel import skill_store

    skill = _find_skill(name)
    if getattr(skill, "source", "builtin") != "custom":
        raise ApiError.validation(
            f"`{name}` 是**内置**技能，不能改。",
            {"name": "内置技能随代码分发、部署时会被覆盖。想改请新建一篇自定义的。"},
        )
    try:
        skill_store.create_or_update_skill(
            name=name,
            category=skill.category,
            description=payload.description,
            body=payload.body,
            must_exist=True,
        )
    except skill_store.SkillWriteError as exc:
        raise ApiError.validation(str(exc), {"detail": exc.message}) from exc

    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.KERNEL_SKILL_UPDATE,
        summary=f"更新技能 {skill.category}/{name}",
        detail={"name": name, "category": skill.category, "chars": len(payload.body)},
    )
    return SkillDetailOut(
        name=name,
        category=skill.category,
        description=payload.description,
        source="custom",
        body=payload.body,
        lineCount=payload.body.count("\n") + 1,
    )


@router.delete(
    "/skills/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删一篇自定义技能",
)
def delete_kernel_skill(
    name: str,
    request: Request,
    identity: Identity = AdminDep,
    db: Session = DbDep,
) -> None:
    """删自定义技能。

    内置的删不掉 —— 它随代码分发，删了下次部署又回来，
    而"删了又回来"比"删不掉"更让人困惑。
    """
    from app.kernel import skill_store

    skill = _find_skill(name)
    try:
        skill_store.delete_skill(name=name, category=skill.category)
    except skill_store.SkillWriteError as exc:
        raise ApiError.validation(str(exc), {"detail": exc.message}) from exc

    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.KERNEL_SKILL_DELETE,
        summary=f"删除技能 {skill.category}/{name}",
        detail={"name": name, "category": skill.category},
    )


__all__ = ["router"]
