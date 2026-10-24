"""自定义技能的 CRUD（页面上的「技能管理」）。

## 为什么单独一个模块，不塞进 `skills.py`

`skills.py` 是**只读的发现与加载**层（它跑在引擎热路径上，每次
`load_skill` 都会走）。而这里是**写路径**：校验、落盘、删除、改状态。
两者的失效后果完全不同 ——

  · 读层出错的后果是"技能列不全"，引擎照样跑；
  · 写层出错的后果是"用户写的东西没了"或"写进了一个不该写的路径"。

混在一起会让读层也被迫 import 写层的依赖（`tempfile` / 校验 / 锁），
而那正是我们一直想避免的"为了一个可选能力给热路径加负担"。

## ★ 唯一的硬约束：**只能写 `CUSTOM_SKILLS_ROOT` 之下**

内置技能在 `prompts/skills/`，它随代码分发、部署时整目录覆盖。
一旦允许写那里，用户创建的技能会在**下一次部署时静默消失**。

所以本模块的每一个写操作都先过 `_resolve_target()`：
它把"分类 + 名字"拼成路径，**断言最终路径确实落在自定义目录之内**。
这一步不是"防御性编程"——它是这个模块存在的理由本身。
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

#: 分类名与技能名的合法形状。
#:
#: ⚠️ 刻意**不允许**大写、空格、`..`、斜杠：
#: 它们会变成路径分隔或需要转义，而"名字里带路径"是目录穿越的入口。
#: 技能名本来也是模型在 `load_skill({"name": ...})` 里写的标识符，
#: 用小写与连字符对模型最友好。
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

MAX_CONTENT_CHARS = 200_000  # 单篇技能上限（正文 + frontmatter）
MAX_DESCRIPTION_CHARS = 2_000


class SkillWriteError(Exception):
    """写路径的可读错误（带中文原因，路由层直接回给界面）。

    ## 为什么 `__str__` 要把 detail 拼上

    错误信封里 `message` 是界面**唯一**会显示的那一句（前端的
    `errorMessage()` 只取 `message`，`details` 只用来标红字段）。
    所以"原因和下一步"如果只放在 `detail` 里，用户在界面上就**看不到**——
    他只会读到"不能删"，然后不知道该怎么办。

    `message` 留作短标签（日志/审计里好扫），`str(exc)` 是给人看的完整话。
    """

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        if self.detail:
            return f"{self.message}{self.detail}"
        return self.message


def _custom_root() -> Path:
    """每次读一遍环境变量 —— 测试要能 monkeypatch 它。"""
    from app.kernel.skills import CUSTOM_SKILLS_ROOT

    return CUSTOM_SKILLS_ROOT


def _resolve_target(category: str, name: str) -> Path:
    """把「分类 + 名字」解析成一个**确定在自定义目录内**的文件路径。

    ## 这段是整个模块的安全边界

    四道检查缺一不可：

      1. 名字与分类的形状（正则）—— 挡 `..`、`/`、空格；
      2. 拼出来的路径必须 `.resolve()` 后仍在根目录之内 ——
         挡符号链接把目标指到外面；
      3. 目标不能是**已存在的符号链接** —— 否则写它会写到链接指向的地方；
      4. 名字不能与**内置技能**同名 —— 内置优先，同名等于永远不生效。

    ⚠️ 第 2 条必须用 `resolve()` 后再比，不能用字符串 `startswith`：
    `custom/../../prompts/skills/x` 的字符串前缀是 `custom`，
    但它 resolve 之后在外面。
    """
    if not _CATEGORY_RE.match(category):
        raise SkillWriteError(
            f"分类名 `{category}` 不合法。",
            detail="只能用 小写字母、数字、下划线、连字符，且以字母或数字开头。",
        )
    if not _NAME_RE.match(name):
        raise SkillWriteError(
            f"技能名 `{name}` 不合法。",
            detail="只能用 小写字母、数字、下划线、连字符，且以字母或数字开头（≤64 字符）。",
        )

    root = _custom_root()
    target = root / category / f"{name}.md"

    # 第 2 条：resolved 之后必须仍在根内
    try:
        root_resolved = root.resolve()
        target_resolved = target.resolve()
    except OSError as exc:  # noqa: BLE001 - 路径不可解析就是不可用
        raise SkillWriteError(f"路径不可解析：{exc}") from exc
    if root_resolved != target_resolved and root_resolved not in target_resolved.parents:
        raise SkillWriteError(
            "拒绝写入：目标路径不在自定义技能目录之内。",
            detail=f"这一条挡的是目录穿越（`{target}`）。",
        )

    # 第 3 条：目标已存在且是符号链接 → 拒绝（否则写的是它指向的地方）
    if target.is_symlink():
        raise SkillWriteError(
            f"`{target}` 是一个符号链接，拒绝写入。",
            detail="请先人工确认它指向哪里。",
        )

    return target


def _render(*, name: str, description: str, body: str, category: str) -> str:
    """拼出 SKILL.md 的完整内容（frontmatter + 正文）。

    ⚠️ `description` 用 **YAML 折叠标量**（`>-`）而不是直接把原文塞进一行：
    描述里常有换行与中文标点，单行写法会让 YAML 解析出意外结果
    （本项目的 `_parse_frontmatter` 就是为此专门支持了块标量）。
    """
    desc_lines = [line.strip() for line in description.strip().splitlines() if line.strip()]
    desc_block = "\n".join(f"  {line}" for line in desc_lines) or "  "
    return (
        "---\n"
        f"name: {name}\n"
        f"category: {category}\n"
        "description: >-\n"
        f"{desc_block}\n"
        "---\n\n"
        f"{body.strip()}\n"
    )


def create_or_update_skill(
    *,
    name: str,
    category: str,
    description: str,
    body: str,
    must_exist: bool = False,
) -> Path:
    """创建或覆盖一篇**自定义**技能，返回落盘路径。

    ## 为什么写盘用「临时文件 + 原子替换」

    直接 `write_text()` 在写到一半时崩（磁盘满、进程被杀）会留下一个
    半截文件 —— 而它仍会被 `discover_skills` 当成一篇技能收进去，
    结果是模型读到一段断掉的正文。用 `os.replace` 保证"要么旧的、要么新的"。

    ## `must_exist`

    编辑已有技能时传 True：名字拼错时应当报"没有这篇"，
    而不是**悄悄新建一篇**。
    """
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillWriteError(
            f"描述太长（{len(description)} 字符，上限 {MAX_DESCRIPTION_CHARS}）。"
        )
    if len(body) > MAX_CONTENT_CHARS:
        raise SkillWriteError(
            f"正文太长（{len(body)} 字符，上限 {MAX_CONTENT_CHARS}）。"
        )
    if not body.strip():
        raise SkillWriteError("正文不能为空 —— 空技能会让模型读到一段空内容。")

    target = _resolve_target(category, name)

    # 内置技能同名 → 拒绝（内置优先，写了也永远不生效）
    from app.kernel.skills import discover_skills

    if any(skill.name == name for skill in discover_skills()):
        raise SkillWriteError(
            f"已有一篇**内置**技能叫 `{name}`。",
            detail=(
                "内置技能随代码分发、同名时优先，所以自定义的同名技能不会生效。"
                "请换一个名字。"
            ),
        )

    if must_exist and not target.is_file():
        raise SkillWriteError(
            f"没有找到叫 `{name}` 的自定义技能（分类 `{category}`）。"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    text = _render(name=name, description=description, body=body, category=category)

    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except Exception:
        # 失败时清掉临时文件，别在目录里留垃圾
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise

    logger.info("技能已写入：%s（%d 字符）", target, len(text))
    return target


def delete_skill(*, name: str, category: str) -> None:
    """删除一篇自定义技能。

    ⚠️ 只删自定义目录里的文件。内置技能删不掉 —— 它随代码分发，
    删了下次部署又回来，而"删了又回来"比"删不掉"更让人困惑。
    """
    from app.kernel.skills import discover_skills

    if any(skill.name == name for skill in discover_skills()):
        raise SkillWriteError(
            f"`{name}` 是**内置**技能，不能删。",
            detail="它随代码分发，删掉下次部署会回来。要停用它，请关掉对应工具或改提示词。",
        )

    target = _resolve_target(category, name)
    if not target.is_file():
        raise SkillWriteError(f"没有找到叫 `{name}` 的自定义技能（分类 `{category}`）。")

    target.unlink()
    # 目录空了就顺手删掉，免得界面上留一个空分类
    # 还有别的技能时 rmdir 会失败，正常
    with contextlib.suppress(OSError):
        target.parent.rmdir()
    logger.info("技能已删除：%s", target)


def read_custom_skill(*, name: str, category: str) -> str:
    """读一篇自定义技能的**全文**（含 frontmatter）—— 编辑时回填用。

    ⚠️ 只允许读自定义的：内置技能的正文由 `load_skill` 给模型，
    而这条是给**编辑器回填**的，两者受众不同（一个是模型，一个是人）。
    """
    target = _resolve_target(category, name)
    if not target.is_file():
        raise SkillWriteError(f"没有找到叫 `{name}` 的自定义技能（分类 `{category}`）。")
    try:
        return target.read_text(encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        raise SkillWriteError(f"读不了 `{name}`：{exc}") from exc


def split_content(text: str) -> tuple[str, str]:
    """把一把 SKILL.md 文本拆成 `(description, body)`。

    给"从别处粘贴一份 SKILL.md 进来"这个场景用 —— 用户不需要手工
    把 frontmatter 拆开。拆不出来时把全文当正文、描述留空。
    """
    from app.kernel.skills import _parse_frontmatter, _strip_frontmatter

    fm = _parse_frontmatter(text)
    body = _strip_frontmatter(text) if fm else text
    return fm.get("description", "").strip(), body.strip()


def ensure_custom_root() -> Path:
    """确保自定义技能目录存在（首次创建技能时调用）。"""
    root = _custom_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def purge_unused_dirs() -> int:
    """删掉自定义目录下的空分类目录，返回删掉几个。

    单独一个函数而不是内联：界面上的"清空"按钮会调它，
    而"删空目录"这个动作不该混进 `delete_skill` 的主流程。
    """
    root = _custom_root()
    if not root.is_dir():
        return 0
    removed = 0
    for child in sorted(root.iterdir(), reverse=True):
        if child.is_dir() and not any(child.iterdir()):
            try:
                child.rmdir()
                removed += 1
            except OSError:
                continue
    return removed


__all__ = [
    "MAX_CONTENT_CHARS",
    "MAX_DESCRIPTION_CHARS",
    "SkillWriteError",
    "create_or_update_skill",
    "delete_skill",
    "ensure_custom_root",
    "purge_unused_dirs",
    "read_custom_skill",
    "split_content",
]
