"""分期交付：未实现端点的统一占位。

任务 1 是**脚手架 + 认证**任务。契约里已经定义了完整的 API 面（八个领域），
但它们的业务逻辑属于后续任务。这些端点在这里**注册但不实现**，
返回 `501 NOT_IMPLEMENTED`，而不是干脆不注册。

## 为什么不注册（让它们 404）

1. **404 会说谎**。`NOT_FOUND` 的语义是「这个资源不存在」，
   而真实情况是「端点还没做」。对调用方来说这是两种完全不同的处理：
   前者该换 id，后者该等版本。
2. **OpenAPI 文档会缺一块**。前端联调时看不到端点清单，
   只能靠读契约文档猜哪些能用 —— 而"文档说的"和"接口实际有的"
   正是本项目反复踩的那类漂移。
3. **注册即声明**。这些函数签名就是「这个端点收什么、返回什么形状」的
   可执行记录，实现时把它们替换成真实函数体即可，路由不用动。

## 为什么带「计划在后端任务 N 中交付」

因为调用方（前端开发者、后续任务的执行者）需要知道**要等多久**。
只写「尚未实现」会让人去翻任务清单；写清任务号，答案就在响应里。
"""

from __future__ import annotations

from typing import NoReturn

from app.core.errors import ApiError


def pending(planned_task: str, what: str) -> NoReturn:
    """抛 501。`planned_task` 是任务编号（如 `"2"`），`what` 是中文功能名。"""
    raise ApiError.not_implemented(planned_task, what)
