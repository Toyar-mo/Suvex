"""Suvex 后端。

按领域拆包：`auth` / `providers` / `scans` / `findings` / `reports` /
`events` / `settings` / `overview`，每个包内部结构统一为
`models.py`（表）→ `schemas.py`（出入参）→ `service.py`（业务）→ `router.py`（HTTP）。

为什么按领域而不是按层（controllers/services/models 三个大目录）：
一次改动通常只涉及一个领域，按领域分就能在一个目录里看完
「表长什么样、接口收什么、业务怎么判」；按层分则每次都要跳三个目录。
契约 §2.5 那种「两个形状别搞混」的坑，在按领域分之后是**同目录内**的事，
更容易在对账时发现。

`app/core/` 放跨领域的公共件：配置、数据库、鉴权、错误、限流、ID。
它不依赖任何领域包 —— 依赖方向单向，避免环形导入。
"""
