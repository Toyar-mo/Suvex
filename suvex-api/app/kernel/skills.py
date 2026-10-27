"""按需加载的「技能包」（skill packs）—— 学 strix 的 `load_skill` 机制。

## 这是什么、为什么需要

模型的上下文有限，而渗透/应急响应用的**具体手法**（某类漏洞怎么验、某个
命令行工具怎么调、某份证据怎么读）是海量的。把它们整篇塞进系统提示词，
既撑爆上下文，又会让模型在无关的题材上分心。

strix 的解法是：**技能包 = 一篇 markdown 知识**，模型按需用 `load_skill`
拉取当前要用的那一篇。它比"把所有知识都写进提示词"好在两点：

  1. 上下文只装当前任务相关的那一篇，而不是全部；
  2. 知识可以独立演进（加一篇新 skill = 加一个 .md 文件，不改代码）。

## 与 STRIX 的关系：只借格式，不引依赖

技能包的 markdown（frontmatter `name`/`description` + 正文）**来源是
strix 的开源 skills 目录**（Apache-2.0，见各文件头部），我们挑选与**本镜像
实际装了的工具**对得上的那一批拷进 `prompts/skills/`，随代码分发。
我们**不** import strix、不依赖它的运行环境 —— 镜像里没有 strix 也能用。

⚠️ 挑选时的一条硬纪律（踩过的坑）：**只 vendor 与镜像真实工具对得上的
playbook**。把讲镜像里没有的命令的 skill 塞进来，等于教模型用不存在的
命令 —— 正是我们反复强调的「工具在镜像里 ≠ 你能用」那个反例。

## 工具可用性必须对着 Dockerfile 说话，不能凭记忆

这条纪律最早是"发现一堆工具没装，于是砍掉一半技能"的产物。但它有个
反向陷阱：**工具装上之后，这份注释仍写着"没装"，于是新技能白白被拒**。
2026-10-26 就发生过 —— 当时 `nuclei`/`katana`/`naabu`/`subfinder` 确实
没装（下面提到的原始版本就是这么写的），后来按用户指令把它们连同
`httpx-toolkit`/`hurl`/`gitleaks`/`trufflehog` 一起装进镜像，注释却没人改。

所以现在的判据是**对着 `docker/suvex-sandbox/Dockerfile` 推导**，而不是
读这段注释：

  · `apt-get install` 的参数列表 = 镜像里会有的包
  · 包名 ≠ 二进制名的情况单独映射（`httpx-toolkit` → `httpx`、
    `binutils` → `strings`/`objdump`/`readelf`…、`dnsutils` → `dig`/`host`）

据此对账的脚本：`_tmp/triage_skills_v2.py`（分诊，只打印）与
`_tmp/import_skills.py`（按结论导入）。两者共用同一份常量，避免两套标准漂移。

### 当前状态（2026-10-26）

`prompts/skills/` 共 **76 篇**：原有 34 篇 + 从 strix 上游导入 42 篇
（Apache-2.0，来源标注写在各文件 frontmatter **之后**的 HTML 注释里）。

仍**未装**因而仍是缺口的两样（skill 里提到、镜像里没有）：

  · `semgrep` —— Kali 源返回 HTTP 404（实测），只能 pip 装；而 Kali 的
    python3 已是 **3.14**，`ropper` 依赖的 `filebytes` 用了 3.12 移除的
    `ast.Str`，装了就崩 —— 所以不轻易加 pip 包。`tooling/semgrep.md`
    与提到它的两篇（`source_aware_sast`、`django`）因此**没有导入**。
  · `amass` —— 默认跑很久，`subfinder` 已覆盖同一需求，故不装。

## 目录结构

    prompts/skills/
      analysis/          —— 闭环纪律（confirmed/ruled_out/needs_follow_up）
      cloud/             —— AWS / Azure / GCP / Kubernetes
      custom/            —— 按需组合的专项（API 规格测试、供应链混淆等）
      frameworks/        —— FastAPI / NestJS / Next.js 等框架打法
      methodology/       —— 方法与流程
      protocols/         —— OAuth / GraphQL 等协议
      reconnaissance/    —— 攻击面发现
      technologies/      —— 具体技术栈（Firebase / Supabase / AD 等）
      tooling/           —— 单工具命令行打法（nmap / ffuf / sqlmap / httpx…）
      vulnerabilities/   —— 各类漏洞的验证手法

`name` 取文件名（去 `.md`），全局唯一 —— 它在 `load_skill` 的参数里出现，
必须短、好敲、无歧义。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: 技能包根目录。与 `prompts/` 其余文件同级，随部署分发。
SKILLS_ROOT: Path = Path(__file__).resolve().parent.parent.parent / "prompts" / "skills"

#: `load_skill` 单次返回的最大字符数。技能包最长约 19KB，全给也没问题，
#: 但设一个上限防止有人塞了一篇论文进来把上下文一次撑爆。
MAX_SKILL_CHARS: int = 24_000

#: 渲染进提示词的「可用技能清单」条目上限（防清单本身很长）。
#: 清单只列 name + 一句话描述，不列全文 —— 全文靠 `load_skill` 按需拉。
MAX_INDEX_ENTRIES: int = 200


#: 技能包目录里主文件的固定文件名（v0.90，学 CyberStrikeAI 的 `SKILL.md`）。
#:
#: ## 为什么必须有个固定名，不能"目录里随便哪个 .md 都算主文件"
#:
#: 一个技能包可能带好几份 markdown（`FORMS.md`、`scripts/extra.sh` 的说明等）。
#: 如果发现层认"目录下第一个 .md"，那么**文件名的字典序**就会悄悄决定
#: 哪一篇是主文件 —— 加一个 `A-notes.md` 就能把主文件顶掉，而主文件
#: 承载的是 `name`/`description`，于是清单里的描述会莫名其妙变成别的内容。
#:
#: 固定名把这件事变成确定的：主文件永远是 `SKILL.md`，其余都是附件。
SKILL_MAIN_FILE: str = "SKILL.md"


@dataclass(frozen=True)
class Skill:
    """一篇技能包。

    ## 两种形态（v0.90 起并存）

    1. **单文件**（v0.88 及以前、以及全部 31 篇内置技能）：
       `<category>/<name>.md`
    2. **目录包**（v0.90 起，学 CyberStrikeAI）：
       `<category>/<name>/SKILL.md` + 任意附件

    第 2 种存在的理由：一篇技能常常需要**配套文件** —— 一份表单清单、
       一段可复制的脚本、一张检查表。塞进同一个 .md 会让正文臃肿到
       模型每次 `load_skill` 都得全读一遍；分文件后模型可以只要它要的那份。

    ⚠️ 两种形态在**发现层**是等价的：都产出一个 `Skill`。差别只在
    `files`（附件清单）与 `package_dir`（附件所在的目录，单文件形态为 None）。
    """

    name: str
    category: str
    description: str
    path: Path
    #: ★ v0.88：来源 —— `builtin`（随代码分发、只读）或 `custom`（页面上创建、可编辑）。
    #:
    #: 这个字段决定了界面上那一篇能不能被改/删：
    #: 内置技能改了会被下次部署覆盖（所以只读），自定义的随便改。
    source: str = "builtin"
    #: ★ v0.90：技能包的目录（仅目录包形态；单文件形态为 None）。
    package_dir: Path | None = None
    #: ★ v0.90：附件清单（相对于 `package_dir` 的 posix 路径，不含 `SKILL.md`）。
    #: 空元组 = 没有附件（单文件形态永远如此）。
    files: tuple[str, ...] = ()

    def read(self) -> str:
        """读正文（剥掉 frontmatter —— 模型不需要看 YAML 头）。"""
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:  # noqa: BLE001 - 读不到就是读不到，如实返回
            logger.warning("读技能包 %s 失败：%s", self.path, exc)
            return f"（技能包 `{self.name}` 读取失败：{exc}）"
        return _strip_frontmatter(text)

    def read_file(self, relative: str) -> str:
        """读一个**附件**的全文。

        ## 这个方法是安全边界的一部分

        附件名来自模型（`load_skill({"file": "..."})`）或界面，两者都不可信。
        所以这里**不接受绝对路径、不接受 `..`**，且 resolve 之后必须仍在
        包目录之内 —— 与 `skill_store._resolve_target` 同一套判据。

        ⚠️ 不用"过滤 `..`"的黑名单写法：`a/../../etc/passwd` 里把 `..`
        删掉会得到 `a/etc/passwd`（一个完全不同的、可能存在的路径）。
        正确形状是"先确定合法集合，再判在不在里面"—— 本项目在附件删除
        那次已经栽过一次黑名单。
        """
        if self.package_dir is None:
            raise ValueError(f"`{self.name}` 是单文件技能，没有附件。")
        raw = (relative or "").strip().replace("\\", "/")
        if not raw or raw.startswith("/") or ".." in raw.split("/"):
            raise ValueError(f"附件名 `{relative}` 不合法。")
        if raw not in self.files:
            raise ValueError(f"`{self.name}` 没有附件 `{raw}`。")

        target = (self.package_dir / raw).resolve()
        root = self.package_dir.resolve()
        if root != target and root not in target.parents:
            raise ValueError(f"附件名 `{relative}` 越出了技能包目录。")

        try:
            return target.read_text(encoding="utf-8")
        except OSError as exc:  # noqa: BLE001
            return f"（附件 `{raw}` 读取失败：{exc}）"


def _strip_frontmatter(text: str) -> str:
    """去掉开头的 YAML frontmatter（`---` … `---`），保留正文。

    只有「第一行是 `---`」时才认为是 frontmatter。找不到闭合的 `---`
    就原样返回 —— 宁可多给一段 YAML，也不要把正文误删。
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[i + 1 :]).lstrip("\n")
    return text


def _parse_frontmatter(text: str) -> dict[str, str]:
    """极简 frontmatter 解析：取 `key: value` 标量，**支持 YAML 多行折叠**。

    ## ★ v0.87：为什么必须支持 `>-`（实测发现的缺陷）

    第一版只认单行 `key: value`。而引入的一批方法论技能用 YAML 折叠标量：

        description: >-
          验证铁律:搜索≠漏洞,confirmed Fact须证据…
          想象力拉满+单步验证零容忍。

    于是解析结果是 `description = ">-"` —— **描述变成了两个字面字符**。
    这个失效很隐蔽：技能照样被 `discover_skills` 收进清单（`name` 是单行的），
    `load_skill` 也能返回全文，只有"列表里看到的描述"是错的。
    而描述正是模型判断"该不该加载这一篇"的唯一依据。

    ⚠️ **不引 PyYAML**：本模块在 `load_skill` 热路径上，而 PyYAML 是
    可选依赖。要支持的只是 `>` / `>-` / `|` 三种块标量 —— 手写二十行
    比引依赖更稳。

    ## 支持的范围（超出的不猜）

    - `key: value` 单行；
    - `key: >-` / `>` / `|` + 后续**缩进的**续行；
    - 续行遇到下一个顶格 `key:` 或 `---` 结束。

    嵌套结构（`metadata:` 下的 `tags:` 等）**不展开** —— 我们不读它们。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    i = 1
    total = len(lines)
    while i < total:
        line = lines[i]
        if line.strip() == "---":
            break
        # 只处理顶层键（顶格且含冒号）。缩进行属于上一个键的续行，
        # 已在块标量分支里被消费；走到这里说明它没有归属，跳过。
        if line[:1] not in (" ", "\t") and ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value in (">-", ">", "|", "|-", ">+", "|+"):
                block: list[str] = []
                i += 1
                while i < total:
                    nxt = lines[i]
                    if nxt.strip() == "---":
                        break
                    if nxt[:1] not in (" ", "\t") and nxt.strip():
                        break
                    block.append(nxt.strip())
                    i += 1
                if value.startswith(">"):
                    # 折叠：单换行变空格
                    out[key] = " ".join(part for part in block if part).strip()
                else:
                    # 保留：原样换行
                    out[key] = "\n".join(block).strip()
                continue
            out[key] = value
        i += 1
    return out


def _attachment_list(package_dir: Path) -> tuple[str, ...]:
    """列出一个技能包里的附件（相对 posix 路径，不含主文件）。

    ⚠️ 跳过隐藏文件与 `__pycache__` 之类的噪音：它们不是"给模型看的附件"，
    列进清单会让界面出现一排没意义的条目。跳过的是**噪音**，不是安全过滤
    （安全过滤在 `read_file` 里，用白名单）。
    """
    out: list[str] = []
    for child in sorted(package_dir.rglob("*")):
        if not child.is_file():
            continue
        rel = child.relative_to(package_dir).as_posix()
        if rel == SKILL_MAIN_FILE:
            continue
        parts = rel.split("/")
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        out.append(rel)
    return tuple(out)


def _scan_one_root(base: Path, source: str) -> list[Skill]:
    """扫描**一个**根目录，两种形态都认。

    ## ★ 这里有个必须处理的陷阱：包内附件不能被当成独立技能

    第一版发现层是 `rglob("*.md")` —— 对单文件形态完全正确。
    但目录包形态下 `<category>/<name>/FORMS.md` 也是一个 `.md`，
    于是它会**被当成一篇独立技能**收进清单：

      · 名字取自 frontmatter（很多附件根本没有 → 跳过，运气好）
      · 有 frontmatter 的呢？它会出现在模型看到的「可用技能清单」里，
        而它其实只是另一篇技能的一部分 —— 模型会去 `load_skill` 它，
        读到的是一段没有上下文的片段。

    所以扫描前要先算出"哪些目录是技能包"，这些目录**内部的其它 .md
    一律不当技能**。判据只有一条：目录里有 `SKILL.md`。
    """
    if not base.is_dir():
        return []

    # 先标出所有技能包目录（含 SKILL.md 的目录），它们内部不再是技能
    package_dirs: set[Path] = set()
    for main in base.rglob(SKILL_MAIN_FILE):
        if main.is_file():
            package_dirs.add(main.parent)

    found: list[Skill] = []

    # ── 目录包形态 ──────────────────────────────────────
    for pkg in sorted(package_dirs):
        main = pkg / SKILL_MAIN_FILE
        try:
            head = main.read_text(encoding="utf-8")[:2000]
        except OSError:
            continue
        fm = _parse_frontmatter(head)
        # 名字优先取 frontmatter，否则退回目录名 —— 目录名是人在文件系统上
        # 起的，比"没有名字"有用得多（单调单文件形态会跳过无名文件，
        # 因为那里无名就等于"这是个说明文档"；而这里 SKILL.md 的存在
        # 已经明确表示"这是一个技能包"）。
        name = (fm.get("name") or pkg.name).strip()
        if not name:
            continue
        found.append(
            Skill(
                name=name,
                category=fm.get("category") or pkg.parent.name,
                description=fm.get("description", "").strip(),
                path=main,
                source=source,
                package_dir=pkg,
                files=_attachment_list(pkg),
            )
        )

    # ── 单文件形态 ──────────────────────────────────────
    for path in sorted(base.rglob("*.md")):
        if path.name == SKILL_MAIN_FILE and path.parent in package_dirs:
            continue  # 包主文件，上面已处理
        # 落在某个技能包目录内部的其它 .md 是附件，不是技能
        if any(parent in package_dirs for parent in path.parents):
            continue
        skill = _skill_of(path, source=source)
        if skill is not None:
            found.append(skill)

    found.sort(key=lambda s: (s.category, s.name))
    return found


def discover_skills(root: Path | None = None) -> list[Skill]:
    """扫描**内置**技能包目录，返回全部 skill（按 category 再 name 排序，稳定）。

    ⚠️ 目录缺失返回**空列表**而不是抛错：技能包是可选的增强，缺了它
    整个引擎应当照常工作（只是 `load_skill` 会说"没有可用的技能包"）。
    把可选增强做成硬依赖，是"少一个目录就起不来"那类事故的根源。
    """
    base = root or SKILLS_ROOT
    if not base.is_dir():
        logger.warning("技能包目录不存在：%s（load_skill 将无内容可返回）", base)
        return []
    found = _scan_one_root(base, "builtin")
    logger.info("发现 %d 篇技能包（%s）", len(found), base)
    return found


# ══════════════════════════════════════════════════════════
# 自定义技能（v0.88）：与内置技能分开存放
# ══════════════════════════════════════════════════════════
#
# ## 为什么必须分两个目录（这是本模块最关键的一条设计）
#
# 内置技能在 `prompts/skills/`，它**随代码分发** —— 部署时用
# `tar xzf ... -C /root/suvex` 整目录覆盖。
#
# 如果允许用户在页面上创建技能、直接写进那个目录，那么**下一次部署
# 就会把用户写的东西覆盖掉**。而这不是"可能发生"，是每次升级都会发生。
#
# 症状会非常难查：用户创建的技能在升级后**静默消失**，没有任何报错，
# 也没有任何地方记录过"它曾经存在"。
#
# 所以自定义技能放在**独立的可写目录**（默认 `SUPEX_CUSTOM_SKILLS`，
# 未设时用 `app/../custom_skills`，且**不在部署 tar 的覆盖范围内**）。
# 两层在读取时合并，冲突以内置为准（内置是随代码来的，不该被同名覆盖）。

#: 自定义技能目录。可用 `SUPEX_CUSTOM_SKILLS` 覆盖（部署时指到持久区）。
CUSTOM_SKILLS_ROOT: Path = Path(
    os.environ.get("SUPEX_CUSTOM_SKILLS") or (Path(__file__).resolve().parent.parent.parent / "custom_skills")
)


def _skill_of(path: Path, *, source: str) -> Skill | None:
    """把一个 .md 文件解析成 Skill；没有 frontmatter `name` 的返回 None。"""
    try:
        head = path.read_text(encoding="utf-8")[:2000]
    except OSError:
        return None
    fm = _parse_frontmatter(head)
    if not fm.get("name"):
        return None
    return Skill(
        name=fm["name"],
        category=fm.get("category") or path.parent.name,
        description=fm.get("description", "").strip(),
        path=path,
        source=source,
    )


def discover_custom_skills(root: Path | None = None) -> list[Skill]:
    """扫描**自定义**技能目录。

    目录不存在返回空列表（不是错误）：绝大多数部署不会创建自定义技能。
    """
    base = root or CUSTOM_SKILLS_ROOT
    return _scan_one_root(base, "custom")


def all_skills(*, include_custom: bool = True) -> list[Skill]:
    """内置 + 自定义，**内置优先**。

    ## 为什么内置优先（同名时）

    内置技能随代码分发、经过评审；自定义技能是运行时由人写的。
    同名冲突时若让自定义覆盖内置，就等于"任何能打开设置页的人都能
    悄悄改掉内置技能的行为" —— 而技能正文会进模型上下文，
    那是一处不该有的后门。

    冲突时记 warning，让"我建的那个没生效"能被看见，而不是静默丢弃。
    """
    builtin = discover_skills()
    if not include_custom:
        return builtin
    custom = discover_custom_skills()
    seen = {s.name for s in builtin}
    merged = list(builtin)
    for skill in custom:
        if skill.name in seen:
            logger.warning("自定义技能 %r 与内置技能同名，已忽略（内置优先）", skill.name)
            continue
        merged.append(skill)
    merged.sort(key=lambda s: (s.category, s.name))
    return merged


def skills_by_name(skills: list[Skill] | None = None) -> dict[str, Skill]:
    """name → Skill 映射（同名冲突时后者覆盖，并记 warning —— 名字必须唯一）。"""
    items = skills if skills is not None else all_skills()
    out: dict[str, Skill] = {}
    for s in items:
        if s.name in out:
            logger.warning("技能包重名：%s（%s 与 %s）", s.name, out[s.name].path, s.path)
        out[s.name] = s
    return out


def render_index(skills: list[Skill] | None = None) -> str:
    """渲染「可用技能清单」—— 进系统提示词，让模型知道有哪些可按需拉。

    只列 `name: 一句话描述`，按 category 分组。全文不进提示词 ——
    那是 `load_skill` 的活。清单太长时截断并如实说明。
    """
    items = skills if skills is not None else all_skills()
    if not items:
        return ""

    by_cat: dict[str, list[Skill]] = {}
    for s in items:
        by_cat.setdefault(s.category, []).append(s)

    lines: list[str] = []
    shown = 0
    for cat in sorted(by_cat):
        lines.append(f"\n【{cat}】")
        for s in by_cat[cat]:
            if shown >= MAX_INDEX_ENTRIES:
                lines.append(f"……（其余 {len(items) - shown} 篇未列出）")
                return "\n".join(lines)
            desc = s.description or "（无描述）"
            lines.append(f"- `{s.name}`：{desc}")
            shown += 1
    return "\n".join(lines)


def load_skill_text(
    name: str,
    *,
    file: str = "",
    skills: list[Skill] | None = None,
) -> str:
    """按名字取一篇技能包的正文（给 `load_skill` 工具用）。

    - 名字未命中：返回**可用清单**（不是空字符串）—— 模型拼错名字时
      需要看到"到底有哪些"，否则它会反复猜。
    - 命中：返回正文，超 `MAX_SKILL_CHARS` 截断并如实标注。
    - ★ v0.90：`file` 非空时读**附件**（目录包形态）。

    ## 为什么附件清单要贴在正文**末尾**，而不是只放在清单里

    模型加载一篇技能时看到的是这一个字符串。如果附件名只出现在
    「可用技能清单」（提示词里那份）里，它就**必须同时记住清单和正要读的
    这一篇的附件名**才能拼出第二次调用 —— 而清单可能有 200 条。

    贴在末尾的意思：读完正文就顺手看到"这篇还带了三份文件，分别是…"，
    下一次 `load_skill({"name": "...", "file": "..."})` 里那个 `file`
    写什么，是在眼前抄的。

    ⚠️ 附件清单要在正文**之后**给，不能在之前 —— 模型先要的是正文，
    先给一列文件名会把正文挤到后面。
    """
    table = skills_by_name(skills)
    key = (name or "").strip()
    skill = table.get(key)
    if skill is None:
        available = ", ".join(sorted(table)) or "（无）"
        return f"没有名为 `{key}` 的技能包。可用技能包：{available}"

    wanted = (file or "").strip()

    # ── 读附件 ─────────────────────────────────────────
    if wanted:
        try:
            text = skill.read_file(wanted)
        except ValueError as exc:
            if skill.files:
                listing = "、".join(f"`{f}`" for f in skill.files)
                return f"{exc} 这一篇的附件有：{listing}"
            return f"{exc} 这一篇没有附件。"
        if len(text) > MAX_SKILL_CHARS:
            text = text[:MAX_SKILL_CHARS] + f"\n\n……（已截断，原文 {len(text)} 字符）"
        return f"# 技能包附件：{skill.name} / {wanted}\n\n{text}"

    # ── 读主文件 ───────────────────────────────────────
    body = skill.read()
    if len(body) > MAX_SKILL_CHARS:
        body = body[:MAX_SKILL_CHARS] + f"\n\n……（已截断，原文 {len(body)} 字符）"

    tail = ""
    if skill.files:
        listing = "\n".join(f"- `{f}`" for f in skill.files)
        tail = (
            "\n\n---\n"
            "这一篇还有附件。需要哪一份就再调一次：\n"
            f'`load_skill({{"name": "{skill.name}", "file": "<文件名>"}})`\n'
            f"{listing}"
        )
    return f"# 技能包：{skill.name}（{skill.category}）\n\n{body}{tail}"


__all__ = [
    "SKILLS_ROOT",
    "SKILL_MAIN_FILE",
    "MAX_SKILL_CHARS",
    "Skill",
    "discover_skills",
    "discover_custom_skills",
    "all_skills",
    "skills_by_name",
    "render_index",
    "load_skill_text",
]
