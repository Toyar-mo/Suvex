"""Pydantic 基类：字段名 camelCase ↔ ORM 属性 snake_case。

契约 §1.1 的硬要求是「字段名 camelCase，与前端 TypeScript 字段完全一致，
前端不做字段名转换层」。而 Python 一侧的规矩是 snake_case。
这两件事都要满足，于是需要一层**自动**映射 —— 手写 alias 的话，
每加一个字段就多一次「忘了写 alias，于是它永远读出默认值」的机会，
而那种错误不报错，只是值不对。

做法：`alias_generator` **只设 `validation_alias`**。

* 校验（读）时用 snake_case → 能直接 `model_validate(orm_object)`；
* 序列化（写）时没有 serialization_alias，于是用字段名本身 = camelCase → 出参正确。

如果两边都设成 snake_case，出参就会变成 `scan_id`，前端拿不到 `scanId` ——
这是这种映射最容易踩的一次，而且症状是"字段全变 null"，不指向 alias。

⚠️ 别把 `alias_generator=to_camel` 反过来用。那样字段名要写成 snake_case，
出参才对 —— 但契约要求的是**字段名与前端逐字一致**，
让代码里的名字和契约里的名字长得不一样，对账时就要在脑子里做一次翻译。
"""

from __future__ import annotations

import re

from pydantic import AliasGenerator, BaseModel, ConfigDict

_FIRST_CAP = re.compile(r"(?<!^)(?=[A-Z])")


def to_snake_case(name: str) -> str:
    """`scanId` → `scan_id`；`expiresAt` → `expires_at`；`id` → `id`。"""
    return _FIRST_CAP.sub("_", name).lower()


class CamelModel(BaseModel):
    """所有出入参的基类。

    * 字段名 = 契约里的 camelCase 名字（逐字一致）；
    * `from_attributes=True` → 可以直接从 ORM 对象构造；
    * `validation_alias` 自动指向 snake_case 属性 → 不用手写 alias；
    * `populate_by_name=True` → 两种写法都能当构造参数传（测试里方便）。
    """

    model_config = ConfigDict(
        alias_generator=AliasGenerator(validation_alias=to_snake_case),
        from_attributes=True,
        populate_by_name=True,
    )


class StrictModel(CamelModel):
    """请求体用：不认识的字段直接报错。

    `extra="ignore"`（默认）会把拼错的字段**静默丢掉**，
    表现是「我明明传了 includePoc=true 但报告里没有 PoC」。
    请求体宁可严格：多传一个字段是调用方的 bug，早报早改。

    响应体**不用**这个基类 —— 后端多返回一个字段不该让前端解析失败
    （那是没有版本协商的 API 里唯一可行的前向兼容方式）。
    """

    model_config = ConfigDict(
        alias_generator=AliasGenerator(validation_alias=to_snake_case),
        from_attributes=True,
        populate_by_name=True,
        extra="forbid",
    )
