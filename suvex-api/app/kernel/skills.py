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
playbook**。实测本镜像里 `nuclei`/`katana`/`naabu`/`subfinder`/`semgrep`
都**没装**，`httpx` 是 Python 库的 CLI 而不是 ProjectDiscovery 的 httpx 工具。
把讲这些工具的 skill 塞进来，等于教模型用不存在的命令 —— 正是我们反复
强调的「工具在镜像里 ≠ 你能用」那个反例。所以只留了真正能跑的那几个。

## 目录结构

    prompts/skills/
      analysis/          —— 闭环纪律（confirmed/ruled_out/needs_follow_up）
      tooling/           —— nmap / ffuf / sqlmap 的命令行打法
      vulnerabilities/   —— 各类漏洞的验证手法
      protocols/         —— GraphQL 等协议
      reconnaissance/    —— 攻击面发现

`name` 取文件名（去 `.md`），全局唯一 —— 它在 `load_skill` 的参数里出现，
必须短、好敲、无歧义。
"""

from __future__ import annotations

import logging
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


@dataclass(frozen=True)
class Skill:
    """一篇技能包。"""

    name: str
    category: str
    description: str
    path: Path

    def read(self) -> str:
        """读正文（剥掉 frontmatter —— 模型不需要看 YAML 头）。"""
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:  # noqa: BLE001 - 读不到就是读不到，如实返回
            logger.warning("读技能包 %s 失败：%s", self.path, exc)
            return f"（技能包 `{self.name}` 读取失败：{exc}）"
        return _strip_frontmatter(text)


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


def discover_skills(root: Path | None = None) -> list[Skill]:
    """扫描技能包目录，返回全部 skill（按 category 再 name 排序，稳定）。

    ⚠️ 目录缺失返回**空列表**而不是抛错：技能包是可选的增强，缺了它
    整个引擎应当照常工作（只是 `load_skill` 会说"没有可用的技能包"）。
    把可选增强做成硬依赖，是"少一个目录就起不来"那类事故的根源。
    """
    base = root or SKILLS_ROOT
    if not base.is_dir():
        logger.warning("技能包目录不存在：%s（load_skill 将无内容可返回）", base)
        return []
    found: list[Skill] = []
    for path in sorted(base.rglob("*.md")):
        category = path.parent.name
        name = path.stem
        # README 之类不是技能包，跳过（没有 frontmatter name 的也当说明文件）。
        try:
            head = path.read_text(encoding="utf-8")[:2000]
        except OSError:
            continue
        fm = _parse_frontmatter(head)
        if not fm.get("name"):
            continue
        found.append(
            Skill(
                name=fm.get("name", name) or name,
                category=category,
                description=fm.get("description", "").strip(),
                path=path,
            )
        )
    found.sort(key=lambda s: (s.category, s.name))
    logger.info("发现 %d 篇技能包（%s）", len(found), base)
    return found


def skills_by_name(skills: list[Skill] | None = None) -> dict[str, Skill]:
    """name → Skill 映射（同名冲突时后者覆盖，并记 warning —— 名字必须唯一）。"""
    items = skills if skills is not None else discover_skills()
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
    items = skills if skills is not None else discover_skills()
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


def load_skill_text(name: str, *, skills: list[Skill] | None = None) -> str:
    """按名字取一篇技能包的正文（给 `load_skill` 工具用）。

    - 名字未命中：返回**可用清单**（不是空字符串）—— 模型拼错名字时
      需要看到"到底有哪些"，否则它会反复猜。
    - 命中：返回正文，超 `MAX_SKILL_CHARS` 截断并如实标注。
    """
    table = skills_by_name(skills)
    key = (name or "").strip()
    skill = table.get(key)
    if skill is None:
        available = ", ".join(sorted(table)) or "（无）"
        return f"没有名为 `{key}` 的技能包。可用技能包：{available}"
    body = skill.read()
    if len(body) > MAX_SKILL_CHARS:
        body = body[:MAX_SKILL_CHARS] + f"\n\n……（已截断，原文 {len(body)} 字符）"
    return f"# 技能包：{skill.name}（{skill.category}）\n\n{body}"


__all__ = [
    "SKILLS_ROOT",
    "MAX_SKILL_CHARS",
    "Skill",
    "discover_skills",
    "skills_by_name",
    "render_index",
    "load_skill_text",
]
