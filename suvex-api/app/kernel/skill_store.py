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


def _safe_attachment_rel(relative: str) -> str:
    """校验一个附件相对路径，返回规范化后的 posix 形式。

    ## 这是 v0.90 新增的第二道边界

    附件名来自 HTTP 请求（页面上的「新建文件」输入框）。它会被拼到
    技能包目录下面再写文件 —— 如果形如 `../../prompts/skills/x.md`，
    写下去就**覆盖了内置技能**，而那只会在下一次跑题时才表现为
    "模型的行为变得奇怪"，diff 里什么都看不出来。

    判据（四级，缺一不可）：

      1. 不含反斜杠之外的怪字符 —— 按 `/` 切段后每段都过正则；
      2. 不接受绝对路径（以 `/` 开头）；
      3. 不接受 `..` 或 `.` 段；
      4. 不接受与主文件同名（`SKILL.md` 由正文写，不该从这个入口进）。

    ⚠️ 第 1 条用的正则是**白名单**：只允许 `[A-Za-z0-9_]` 开头的
    `[A-Za-z0-9._-]*`。这不是"过滤危险字符"，是"只接受我认识的名字"。
    本项目在附件删除那里栽过黑名单一次，这里不重复。
    """
    raw = (relative or "").strip().replace("\\", "/")
    if not raw:
        raise SkillWriteError("附件名不能为空。")
    if raw.startswith("/"):
        raise SkillWriteError(f"附件名 `{relative}` 不能是绝对路径。")
    if len(raw) > 200:
        raise SkillWriteError(f"附件名太长（{len(raw)} 字符，上限 200）。")

    parts = raw.split("/")
    if len(parts) > 3:
        raise SkillWriteError(f"附件 `{relative}` 目录层数太多（最多 3 段，如 `scripts/extra.sh`）。")
    for part in parts:
        if not part:
            raise SkillWriteError(f"附件名 `{relative}` 里有空路径段。")
        if part in {".", ".."}:
            raise SkillWriteError(f"附件名 `{relative}` 不能包含 `.` 或 `..` 段。")
        if not _ATTACHMENT_SEG_RE.match(part):
            raise SkillWriteError(
                f"附件路径段 `{part}` 不合法。",
                detail="每段只能用字母、数字、下划线、点、连字符，且以字母数字或下划线开头。",
            )

    if parts[-1] == "SKILL.md" and len(parts) == 1:
        raise SkillWriteError("`SKILL.md` 是主文件名，正文请写在正文框里（它会生成 SKILL.md）。")
    return "/".join(parts)


def _write_attachments(package_dir: Path, files: dict[str, str]) -> tuple[str, ...]:
    """把附件写到技能包目录，返回落盘的相对路径元组。

    ## 顺序：先校验**全部**，再写任何一个

    如果边校验边写，遇到第 3 个附件不合法时前两个已经落盘了 ——
    用户看到的是"保存失败"，而磁盘上却多了一半的文件。
    下一次打开这一篇会看到那些"幽灵附件"，并且它们会进模型上下文。

    所以这里先把整张表校验完，全部合法才开始写。
    """
    if not files:
        return ()

    if len(files) > MAX_ATTACHMENT_COUNT:
        raise SkillWriteError(f"附件太多了（{len(files)} 个，上限 {MAX_ATTACHMENT_COUNT}）。")

    normalized: list[tuple[str, str]] = []
    total = 0
    for raw_name, content in files.items():
        rel = _safe_attachment_rel(raw_name)
        text = content if isinstance(content, str) else str(content)
        total += len(text.encode("utf-8"))
        normalized.append((rel, text))

    if total > MAX_ATTACHMENT_BYTES:
        raise SkillWriteError(
            f"附件总大小 {total / 1024 / 1024:.1f} MB，超过上限 {MAX_ATTACHMENT_BYTES // 1024 // 1024} MB。"
        )

    # 去重（`a/b` 与 `./a/b` 之类在规范化后可能撞在一起）
    seen: set[str] = set()
    for rel, _ in normalized:
        if rel in seen:
            raise SkillWriteError(f"附件 `{rel}` 重复出现。")
        seen.add(rel)

    written: list[str] = []
    for rel, text in normalized:
        target = package_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # 与正文同一套原子写：半截文件会被当成真附件读给模型
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp, target)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        written.append(rel)

    return tuple(sorted(written))


def _prune_stale_attachments(package_dir: Path, keep: tuple[str, ...]) -> int:
    """删掉包目录里不在 `keep` 里的附件，返回删了几个。

    ⚠️ **绝不删 `SKILL.md`**，也绝不删包目录之外的东西：这里只遍历
    包目录本身，且只删 `keep` 之外的**文件**。

    为什么必须做这一步：`PUT` 的附件语义是全量替换。不清理的话，
    用户"删掉一个附件"之后它会留在磁盘上，下一次保存又跟着被读回来 ——
    界面上删不掉的东西，比没有删除按钮更糟。
    """
    keep_set = set(keep) | {"SKILL.md"}
    removed = 0
    for child in sorted(package_dir.rglob("*"), reverse=True):
        if not child.is_file():
            continue
        rel = child.relative_to(package_dir).as_posix()
        if rel in keep_set:
            continue
        if child.name.endswith(".tmp"):
            continue  # 上次崩溃留下的临时文件交给 purge_unused_dirs 那条路
        try:
            child.unlink()
            removed += 1
        except OSError:
            continue
    # 清掉空目录（`scripts/` 里最后一个文件被删后不留空壳）
    for child in sorted(package_dir.rglob("*"), reverse=True):
        if child.is_dir():
            with contextlib.suppress(OSError):
                child.rmdir()
    return removed


#: ★ v0.90：附件总字节上限（一个技能包全部附件之和）。
#:
#: 为什么要有：附件是**随技能一起分发进模型上下文**的东西之外，还会
#: 落盘占部署目录。不设上限的话一个 POST 能塞几十 MB，而"技能包"
#: 本来的量级是几 KB 的文本。20 MB 足够放脚本与检查表。
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
#: 附件个数上限。
MAX_ATTACHMENT_COUNT = 50

#: ★ v0.90：附件相对路径的合法形状。
#:
#: 允许一层子目录（`scripts/extra.sh`），因为分目录放脚本是自然的；
#: 但**不允许** `..`、不允许绝对路径、不允许空段、不允许以 `.` 开头的段。
#: 每段只允许字母数字与 `._-`：附件是给人看的脚本/清单，文件名里不该有
#: 空格或引号（它们会让后续的 shell 引用变复杂）。
_ATTACHMENT_SEG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")


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
    return f"---\nname: {name}\ncategory: {category}\ndescription: >-\n{desc_block}\n---\n\n{body.strip()}\n"


def create_or_update_skill(
    *,
    name: str,
    category: str,
    description: str,
    body: str,
    files: dict[str, str] | None = None,
    must_exist: bool = False,
) -> Path:
    """创建或覆盖一篇**自定义**技能，返回落盘的 SKILL.md 路径。

    ## 为什么写盘用「临时文件 + 原子替换」

    直接 `write_text()` 在写到一半时崩（磁盘满、进程被杀）会留下一个
    半截文件 —— 而它仍会被 `discover_skills` 当成一篇技能收进去，
    结果是模型读到一段断掉的正文。用 `os.replace` 保证"要么旧的、要么新的"。

    ## `must_exist`

    编辑已有技能时传 True：名字拼错时应当报"没有这篇"，
    而不是**悄悄新建一篇**。

    ## ★ v0.90：`files` 是**全量替换**语义

    `None` = 不碰附件（保持磁盘上的原样）；`{}` = 这篇没有附件（删掉现有的）。

    为什么要区分 `None` 和 `{}`：路由层把 `PUT` 的 `files` 声明成可空，
    前端"只改正文"时不会带这个字段 —— 那种情况下若按 `{}` 处理，
    用户改一个字就会把所有附件删光，而他并没有要求删除。

    ## 落盘顺序（重要）

      1. 校验全部附件（一个不合法就整体失败，不写任何文件）
      2. 写附件
      3. 写 SKILL.md
      4. 清理 `keep` 之外的旧附件

    第 3 步在附件之后：`SKILL.md` 存在 = 这个包是有效技能。先写主文件
    再写附件的话，中途失败会留下"能被发现层认出来、但附件缺失"的包，
    而那会被模型读到一段指向不存在文件的正文。
    """
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillWriteError(f"描述太长（{len(description)} 字符，上限 {MAX_DESCRIPTION_CHARS}）。")
    if len(body) > MAX_CONTENT_CHARS:
        raise SkillWriteError(f"正文太长（{len(body)} 字符，上限 {MAX_CONTENT_CHARS}）。")
    if not body.strip():
        raise SkillWriteError("正文不能为空 —— 空技能会让模型读到一段空内容。")

    target = _resolve_target(category, name)

    # 内置技能同名 → 拒绝（内置优先，写了也永远不生效）
    from app.kernel.skills import discover_skills

    if any(skill.name == name for skill in discover_skills()):
        raise SkillWriteError(
            f"已有一篇**内置**技能叫 `{name}`。",
            detail=("内置技能随代码分发、同名时优先，所以自定义的同名技能不会生效。请换一个名字。"),
        )

    # 目标可能是单文件（旧形态 `<name>.md`）或包目录（`<name>/SKILL.md`）
    package_dir = target.parent / name
    existing_main = _existing_main(target, name)
    if must_exist and existing_main is None:
        raise SkillWriteError(f"没有找到叫 `{name}` 的自定义技能（分类 `{category}`）。")

    # ① 先校验附件（不落盘）
    pending: dict[str, str] | None = None
    if files is not None:
        pending = dict(files)
        for raw_name in pending:
            _safe_attachment_rel(raw_name)
        if len(pending) > MAX_ATTACHMENT_COUNT:
            raise SkillWriteError(f"附件太多了（{len(pending)} 个，上限 {MAX_ATTACHMENT_COUNT}）。")

    written: tuple[str, ...] = ()
    if pending:
        # 有附件 → 必须是目录包形态（单文件 `<name>.md` 放不下附件）
        if target.is_file():
            # 从单文件升级成目录包：先删旧文件，避免 `<name>.md` 与
            # `<name>/` 同时存在（发现层会把它俩认成两篇同名技能）
            target.unlink()
        package_dir.mkdir(parents=True, exist_ok=True)
        written = _write_attachments(package_dir, pending)
        target = package_dir / "SKILL.md"
    elif existing_main is not None:
        # ★ 不带附件改动（`files=None`）时必须写回它**现在所在的形态**。
        #
        # 这里曾经只写 `_resolve_target()` 给出的单文件路径 —— 于是"目录包
        # 只改一个字的正文"会在旁边**新建**一个 `<name>.md`，而包目录原封
        # 不动。后果有三层：① 改动没生效（发现层同名先到先得，读到的可能
        # 还是旧包）；② 磁盘上出现 `<name>.md` 与 `<name>/` 两篇同名技能，
        # 日志里只有一句 WARNING；③ 用户再存一次附件，升级分支又把
        # `<name>.md` unlink 掉 —— 他刚做的正文修改就这么没了。
        # 而且这三层**都不报错**。
        target = existing_main

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

    # ④ 清掉这轮没带回来的旧附件（`files={}` 也走这里 → 全部清空）
    if pending is not None and (package_dir / "SKILL.md").is_file():
        _prune_stale_attachments(package_dir, keep=written)

    logger.info("技能已写入：%s（正文 %d 字符，附件 %d 个）", target, len(text), len(written))
    return target


def _existing_main(target: Path, name: str) -> Path | None:
    """在单文件与目录包两种形态里找出这篇技能的主文件，找不到返回 None。

    ## 为什么需要它（v0.90 的第二种形态带来的）

    `_resolve_target()` 只会算出**单文件形态**的路径 `<category>/<name>.md`。
    目录包形态的主文件在 `<category>/<name>/SKILL.md` —— 于是
    `delete_skill` / `read_custom_skill` 里那句 `target.is_file()` 对
    目录包永远为假，报的是"没有找到叫 xxx 的技能"，而它明明在。

    这正是本项目反复踩过的形状：**新形态加进来了，旧代码里"判断存在"的
    那一句没跟着改** —— 表现为"东西在、但所有操作都说它不存在"。

    所以把"这篇到底在哪"收成一个函数，写读删三处都走它。
    """
    package_main = target.parent / name / "SKILL.md"
    if package_main.is_file():
        return package_main
    if target.is_file():
        return target
    return None


def _depth(path: Path) -> int:
    """路径的段数 —— 用来把 `rglob` 的结果**由深到浅**排序。"""
    return len(path.parts)


def delete_skill(*, name: str, category: str) -> None:
    """删除一篇自定义技能（单文件形态删文件，目录包形态删整个包）。

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
    main = _existing_main(target, name)
    if main is None:
        raise SkillWriteError(f"没有找到叫 `{name}` 的自定义技能（分类 `{category}`）。")

    if main.name == "SKILL.md" and main.parent != target.parent:
        # 目录包：整个包目录一起删（附件留着就是"删不掉的孤儿"）
        #
        # ⚠️ 必须**由深到浅**先删文件、再删目录。这里曾经只 unlink 文件然后
        # 对包目录 rmdir 一次 —— 包里有 `scripts/` 这种子目录时 rmdir 会失败，
        # 而那句失败被 `contextlib.suppress(OSError)` 吞掉，于是**删成功了、
        # 返回成功、日志说"技能已删除"，但目录还在**（只剩空壳）。
        # 界面刷新后它又出现，而用户以为已经删掉了。
        package_dir = main.parent
        for child in sorted(package_dir.rglob("*"), key=_depth, reverse=True):
            if child.is_file() or child.is_symlink():
                with contextlib.suppress(OSError):
                    child.unlink()
            elif child.is_dir():
                with contextlib.suppress(OSError):
                    child.rmdir()
        with contextlib.suppress(OSError):
            package_dir.rmdir()
    else:
        main.unlink()

    # 目录空了就顺手删掉，免得界面上留一个空分类
    # 还有别的技能时 rmdir 会失败，正常
    with contextlib.suppress(OSError):
        target.parent.rmdir()
    logger.info("技能已删除：%s", main)


def read_custom_skill(*, name: str, category: str) -> str:
    """读一篇自定义技能的**全文**（含 frontmatter）—— 编辑时回填用。

    ⚠️ 只允许读自定义的：内置技能的正文由 `load_skill` 给模型，
    而这条是给**编辑器回填**的，两者受众不同（一个是模型，一个是人）。
    """
    target = _resolve_target(category, name)
    main = _existing_main(target, name)
    if main is None:
        raise SkillWriteError(f"没有找到叫 `{name}` 的自定义技能（分类 `{category}`）。")
    try:
        return main.read_text(encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        raise SkillWriteError(f"读不了 `{name}`：{exc}") from exc


def read_builtin_skill(skill) -> str:  # noqa: ANN001 - 收 Skill dataclass，避免循环导入
    """读一篇**内置**技能的全文（含 frontmatter）—— 只读，供界面查看。

    ## 为什么需要它（2026-10-26 补）

    在这之前，内置技能的正文在界面上**完全看不到**：`GET /skills/{name}`
    只给自定义技能回正文，内置的回空串加一句"改不了所以不回显"。

    那个理由只对了一半 —— 「改不了」是真的，「回显没用」是错的。
    76 篇里有 72 篇是内置的，它们才是决定模型行为的主体；让部署者
    看不见这 72 篇写了什么，等于把「技能正文会进模型上下文」这件事
    变成黑箱：能改的 4 篇看得见，真正在起作用的那 72 篇看不见。

    典型误用正是这次遇到的：一篇内置技能教了不存在的命令行工具，
    而界面上没有任何地方能发现它 —— 只能 grep 源码。

    ## 与 `read_custom_skill` 的分工

    | 函数 | 受众 | 能否改 |
    |---|---|---|
    | `read_custom_skill` | 编辑器回填 | 能（PUT 写回） |
    | `read_builtin_skill` | 界面查看 | **不能**（随代码分发） |

    两者都只回文本，不写盘；这个函数**不碰** `_resolve_target`，
    因为路径来自发现层已经校验过的 `Skill.path`，不经用户输入拼接。
    """
    path = getattr(skill, "path", None)
    if path is None:
        raise SkillWriteError("这篇技能没有可读的路径。")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        raise SkillWriteError(f"读不了这篇技能的正文：{exc}") from exc
    except UnicodeDecodeError as exc:
        raise SkillWriteError(f"这篇技能不是 UTF-8 文本：{exc}") from exc

    # 单文件形态的正文就是全文；目录包形态的 path 指向 SKILL.md，
    # 附件在 package_dir 里 —— 这里只回主文件，附件清单由详情端点另给。
    return text


def read_custom_attachments(*, name: str, category: str) -> dict[str, str]:
    """读一篇自定义技能包的全部附件（编辑回填用）。

    ⚠️ 单文件形态返回 `{}`；目录包形态返回 `{相对路径: 内容}`。

    为什么回填需要它：`PUT` 的 `files` 是全量替换语义，而前端要"只改正文"
    时得把现有附件原样带回去 —— 否则保存一次就把附件全删了。
    让前端自己拼是拼不出来的：它只有文件名，没有内容。
    """
    target = _resolve_target(category, name)
    main = _existing_main(target, name)
    if main is None or main.name != "SKILL.md":
        return {}
    package_dir = main.parent
    out: dict[str, str] = {}
    for child in sorted(package_dir.rglob("*")):
        if not child.is_file() or child.name.endswith(".tmp"):
            continue
        rel = child.relative_to(package_dir).as_posix()
        if rel == "SKILL.md":
            continue
        try:
            out[rel] = child.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # 二进制附件（图片之类）读不成文本 —— 跳过而不是整个请求失败，
            # 因为它不影响编辑正文这一件事
            continue
    return out


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
    "MAX_ATTACHMENT_BYTES",
    "MAX_ATTACHMENT_COUNT",
    "MAX_CONTENT_CHARS",
    "MAX_DESCRIPTION_CHARS",
    "SkillWriteError",
    "create_or_update_skill",
    "delete_skill",
    "ensure_custom_root",
    "purge_unused_dirs",
    "read_custom_attachments",
    "read_custom_skill",
    "split_content",
]
