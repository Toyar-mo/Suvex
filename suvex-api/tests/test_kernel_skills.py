"""技能包（`app/kernel/skills.py`）与 `load_skill` 工具的测试。

三组：
1. 纯逻辑 —— frontmatter 解析、清单渲染、按名查、未命中回清单、目录缺失不崩。
2. 真实随仓库分发的技能包 —— 现有整批能被发现、name 全局唯一、
   与镜像实装工具对得上（这条防的是「vendor 了讲不存在工具的 skill」那类回归），
   且不含未渲染的 Jinja 控制流（搬入的是上游模板，我们没渲染器）。
3. 工具接线 —— `load_skill` 在 TOOL_NAMES 里、dispatch 能路由到它、
   提示词占位符被替换、缺目录时提示词仍可用。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.kernel import runner, skills

# ── 组 1：纯逻辑 ──────────────────────────────────────────────


def _write_skill(root: Path, relpath: str, name: str, desc: str, body: str) -> Path:
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return p


def test_parse_frontmatter_basic():
    fm = skills._parse_frontmatter("---\nname: x\ndescription: hello world\n---\nbody")
    assert fm == {"name": "x", "description": "hello world"}


def test_parse_frontmatter_absent_returns_empty():
    assert skills._parse_frontmatter("no frontmatter here") == {}


def test_strip_frontmatter_removes_header_keeps_body():
    out = skills._strip_frontmatter("---\nname: x\n---\n\n# Title\nbody")
    assert out.startswith("# Title")
    assert "name: x" not in out


def test_strip_frontmatter_without_closing_returns_original():
    text = "---\nname: x\n# never closed\nbody"
    # 找不到闭合的 ---，原样返回（宁可多给 YAML 也别误删正文）
    assert skills._strip_frontmatter(text) == text


def test_discover_skills_finds_and_sorts(tmp_path: Path):
    _write_skill(tmp_path, "b/two.md", "two", "second", "body2")
    _write_skill(tmp_path, "a/one.md", "one", "first", "body1")
    found = skills.discover_skills(tmp_path)
    assert [s.name for s in found] == ["one", "two"]  # a/ 在 b/ 前
    assert found[0].category == "a"


def test_discover_skills_skips_files_without_name(tmp_path: Path):
    (tmp_path / "README.md").write_text("# just a readme\n", encoding="utf-8")
    _write_skill(tmp_path, "x/real.md", "real", "d", "b")
    found = skills.discover_skills(tmp_path)
    assert [s.name for s in found] == ["real"]


def test_discover_skills_missing_dir_returns_empty(tmp_path: Path):
    # 目录不存在返回空列表，不抛 —— 技能包是可选增强，缺了引擎照常跑。
    assert skills.discover_skills(tmp_path / "nope") == []


def test_render_index_groups_and_lists(tmp_path: Path):
    _write_skill(tmp_path, "vuln/xss.md", "xss", "跨站脚本", "b")
    _write_skill(tmp_path, "tool/nmap.md", "nmap", "端口扫描", "b")
    found = skills.discover_skills(tmp_path)
    index = skills.render_index(found)
    assert "【tool】" in index and "【vuln】" in index
    assert "`xss`：跨站脚本" in index
    assert "`nmap`：端口扫描" in index


def test_render_index_empty_when_no_skills(tmp_path: Path):
    assert skills.render_index([]) == ""


def test_load_skill_text_hit_returns_body_without_frontmatter(tmp_path: Path):
    _write_skill(tmp_path, "a/foo.md", "foo", "desc", "# Foo body\ncontent")
    found = skills.discover_skills(tmp_path)
    out = skills.load_skill_text("foo", skills=found)
    assert "# 技能包：foo" in out
    assert "Foo body" in out
    assert "description:" not in out  # frontmatter 已剥


def test_load_skill_text_miss_returns_available_list(tmp_path: Path):
    _write_skill(tmp_path, "a/foo.md", "foo", "d", "b")
    found = skills.discover_skills(tmp_path)
    out = skills.load_skill_text("nope", skills=found)
    assert "没有名为 `nope`" in out
    assert "foo" in out  # 回了可用清单，模型能纠正拼写


def test_load_skill_text_truncates_oversized(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(skills, "MAX_SKILL_CHARS", 20)
    _write_skill(tmp_path, "a/big.md", "big", "d", "x" * 200)
    found = skills.discover_skills(tmp_path)
    out = skills.load_skill_text("big", skills=found)
    assert "已截断" in out


# ── 组 2：真实随仓库分发的技能包 ────────────────────────────


def _real_skills() -> list[skills.Skill]:
    return skills.discover_skills(skills.SKILLS_ROOT)


def test_real_skills_dir_exists():
    assert skills.SKILLS_ROOT.is_dir(), f"技能包目录不存在：{skills.SKILLS_ROOT}"


def test_real_skills_present():
    found = _real_skills()
    assert len(found) >= 20, f"技能包太少（{len(found)}），vendor 可能没随仓库分发"


def test_real_skills_names_unique():
    found = _real_skills()
    names = [s.name for s in found]
    assert len(names) == len(set(names)), f"技能包重名：{names}"


def test_real_skills_each_has_description():
    for s in _real_skills():
        assert s.description, f"技能包 {s.name} 缺 description"


#: Jinja **控制流语句**。只认这些 —— 因为只有它们会「切分支 / 展开」，
#: 残留下来会让模型读到一条它无法执行的条件指令。
#:
#: ★ 判据收窄了两轮，两次都是假红，值得记下来：
#: 1. 第一版连 `{{ }}` 一起扫 → 12 篇被点名。但 `{{7*7}}`（ssti）、
#:    `{{cycler.__init__...}}`（fastapi）是 SSTI 的**载荷**，是正向教学内容；
#:    `{__schema{types{...}}}`（graphql）是查询语法。删掉就把技能改错了。
#: 2. 第二版改成扫 `{% %}` + `{# #}` → `ssti.md` 又假红：它讲的是
#:    「注释语法差异」这个判据本身（`{# #}` Jinja vs `<%# %>` ERB vs
#:    `{* *}` Smarty），`{#` 出现在**散文与载荷**里，不是模板指令。
#:
#: 所以只钉真正会切分支的语句关键字。模板切分支只会用这些。
_JINJA_STATEMENTS = re.compile(
    r"\{%[-+]?\s*(if|elif|else|endif|for|endfor|set|block|endblock|"
    r"include|macro|endmacro|raw|endraw|filter|endfilter|with|endwith)\b"
)


def test_real_skills_have_no_unrendered_template_syntax():
    """★ 防「把上游模板原样搬进来」这类回归 —— 模型会读到字面的 `{% if %}`。

    ## 为什么这条必须存在

    上游 `strix/skills/` 里的 `.md` 是 **Jinja 模板**：同一个文件按运行时
    能力切分支（例如 `{% if supports_images %}` 决定要不要教模型「截图 +
    view_image」这套动作）。strix 加载时渲染，所以它那侧永远看不到标记。

    我们**没有**模板引擎：`app/kernel/skills.py` 的 `load_skill_text` 只做
    frontmatter 剥离和长度截断，`render_index` 也只生成索引。于是原样搬进来
    的模板标记会**一路漏到模型眼前** —— 而它读起来像指令（"if ... else ...
    endif"），模型很可能会把两个分支都当动作去做，或者干脆卡住。

    2026-10-27 实际抓到一处：`analysis/source_aware_discovery.md` 结尾残留
    `{% if coverage_tools %}` / `{% else %}` / `{% endif %}`。我们有
    `record_coverage`，所以正确做法是把 if 分支的正文**摊平**成无条件正文
    （已改），而不是留着模板让模型自己猜。

    ## 判据只钉「会切分支的语句」，不钉表达式与散文

    收窄过两轮，两次都是假红，理由写在 `_JINJA_STATEMENTS` 上方 ——
    一句话：`{{7*7}}` 是 SSTI 的载荷、`{# #}` 在 ssti.md 里是被讲解的
    语法，它们都不是模板指令。只有 `{% if %}` 这类**控制流**才会在未渲染
    时仍然读作指令。

    ## 为什么扫全文而不是只扫某个目录

    搬入是按目录分批做的（`tooling/`、`vulnerabilities/`、`analysis/` …），
    任何一批都可能带进模板。扫全文才能挡住"下一批"。
    """
    root = skills.SKILLS_ROOT
    offenders: list[str] = []
    for path in sorted(root.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        hits = sorted({m.group(0).strip() for m in _JINJA_STATEMENTS.finditer(text)})
        if hits:
            rel = path.relative_to(root).as_posix()
            offenders.append(f"{rel}（含 {'、'.join(hits)}）")
    assert not offenders, (
        "技能库里残留未渲染的 Jinja 控制流语句 —— 我们没有模板引擎，模型会读到"
        "字面的 `{% if %}` 并当成一条它无法执行的条件指令。把对应分支的正文"
        "摊平（保留我们真有那个能力的那一支）：\n  " + "\n  ".join(offenders)
    )


def test_real_skills_no_skill_for_uninstalled_tools():
    """★ 防「vendor 了讲镜像没装的工具的 skill」这类回归。

    ## 判据从"黑名单"改成了"对账"

    原来这条写死一个 `forbidden` 集合
    （`{"nuclei", "katana", "naabu", "subfinder", "semgrep", "hurl", "httpx"}`），
    理由是"镜像里没有这些工具"。那个理由**当时是对的**，但 2026-10-26 我们
    为了别的目的（skill 在教模型跑这些命令，而镜像里没有 → 提示词在撒谎）
    把它们真装进了 `docker/suvex-sandbox/Dockerfile`，于是黑名单变成了
    "禁止给已装好的工具写文档"——这显然不是它想守的东西。

    黑名单的根病是**两个事实各写一遍**：镜像装了什么（Dockerfile）和
    允许讲什么（这里）。只要两边不同步，就红一次，而红的时候读的人无法
    判断是"文档错了"还是"名单过期了"——这次就撞上了，浪费了一轮排查。

    现在改成**对账**：拿 `prompts/skills/tooling/` 下真实存在的每一篇，
    去 Dockerfile 的安装清单里查它对应的包。任何一篇讲着镜像装不上的
    工具 → 红，且报错直接说出缺哪个包。Dockerfile 是唯一事实源，
    这里不再维护第二份名单。

    `_TOOL_PACKAGE_ALIASES` 只记录**包名与命令名不一致**的那几个
    （同名的不放进来）。它是必要的一层，不是名单：只有真正"名字对不上"
    时才需要写一行，而那种情况本身就该被显式记录下来 —— 比如
    `httpx` 这个坑（Kali 把 PD 的 httpx 打包成 `httpx-toolkit`，
    但装出来的二进制仍叫 `httpx`，而 `pip install httpx` 是另一个东西）。
    """
    dockerfile = (
        Path(__file__).resolve().parents[1] / "docker" / "suvex-sandbox" / "Dockerfile"
    )
    assert dockerfile.is_file(), f"找不到沙箱镜像的 Dockerfile：{dockerfile}"
    installed = _packages_installed_in(dockerfile)

    tooling_dir = skills.SKILLS_ROOT / "tooling"
    assert tooling_dir.is_dir(), f"找不到 tooling 技能目录：{tooling_dir}"

    tooling_skills = sorted(p.stem for p in tooling_dir.glob("*.md"))
    assert tooling_skills, "tooling 目录下没有任何 skill —— vendor 可能没随仓库分发"

    missing: list[str] = []
    for name in tooling_skills:
        package = _TOOL_PACKAGE_ALIASES.get(name, name)
        if package not in installed:
            missing.append(f"{name}（期望镜像安装 `{package}`）")

    assert not missing, (
        "以下 tooling skill 教的工具，沙箱镜像里**没装** —— 提示词在教模型"
        "用一个不存在的命令，模型只会拿到 `command not found` 然后"
        f"把排查方向带偏。请在 Dockerfile 里装上，或删掉这些 skill：\n  "
        + "\n  ".join(missing)
    )


# 包名与 skill 名**不一致**的那几个。同名的不必列进来 —— 那一层由
# Dockerfile 与文件名直接对上，不需要人记。这里只有"确实对不上"才有条目，
# 每一条都得写清为什么。
_TOOL_PACKAGE_ALIASES: dict[str, str] = {
    # Kali 把 ProjectDiscovery 的 httpx 打包成 `httpx-toolkit`（避让 Python
    # 的 `httpx` 库），但**装出来的二进制仍叫 `/usr/bin/httpx`**。
    # 见 Dockerfile 里「那个'两个同名工具'的坑」那一段。
    "httpx": "httpx-toolkit",
    # 属性测试库的 Debian 包名带 `python3-` 前缀，导入名才是 `hypothesis`。
    "hypothesis": "python3-hypothesis",
    # 技能文件名用下划线（与上游 skill 名一致），而装的**命令**是连字符：
    # Dockerfile `ln -sf /opt/agent-browser/agent-browser /usr/local/bin/agent-browser`。
    # npm 包本身叫 `agent-browser`（连字符），skill 名却是 `agent_browser`
    # —— 上游就是这么起的名，改文件名会让已加载过它的模型对不上。
    "agent_browser": "agent-browser",
}


def _packages_installed_in(dockerfile: Path) -> set[str]:
    """从 Dockerfile 里捞出镜像**能提供**的命令名（小写）。

    两个来源，缺一不可：

    ## 来源 1：apt / pip 装的包

    Dockerfile 的安装行是这么写的：

    ```
    RUN apt-get update && apt-get install -y --no-install-recommends \\
        nmap whatweb ffuf \\
        # 中间还可能夹着整行注释
        nuclei feroxbuster \\
    ```

    包名跨多行、中间还夹注释。所以先把 `\\` + 换行拼成空格，再按逻辑行
    取包名。**整行注释必须先剔掉** —— 这个项目的注释会详细解释"为什么装
    这个包"，里面出现过大量包名（包括明确决定**不装**的 `semgrep`），
    不剔注释就会把"讨论过"当成"装上了"，这条测试也就失去意义。

    ## 来源 2：手工放进 `/usr/local/bin` 的二进制

    ★ 2026-10-27 补。有些工具**不能**用 apt 装：

    - `hurl` —— Kali 源里的 `hurl` 是另一个工具（十六进制/URL 编解码器），
      而且连 `/usr/bin/hurl` 都不提供。装它等于让"按包名对账"通过、而模型
      拿到 `unrecognized option`。正确做法是从 GitHub Releases 取官方
      tarball，`install` 到 `/opt/hurl/`，再软链进 `/usr/local/bin/hurl`。
    - `agent-browser` —— npm 包，只取 tarball 里的单个二进制，同样软链进
      `/usr/local/bin`。

    这类安装**一行 `apt-get install` 都没有**。只看来源 1 的话，它们会被
    判成"镜像里没有"，于是有人会去删掉本来正确的 skill —— 或者更糟，
    为了让测试变绿而往 apt 里塞一个错包（`hurl` 就是这个剧本）。

    所以这里把 `ln -sf … /usr/local/bin/<name>` 与
    `install -m … /usr/local/bin/<name>` 的目标名也算作"已安装"。
    判据是**命令在 PATH 上能不能被调用**，而不是"它怎么来的"。

    ⚠️ 这仍然不等于"跑起来没坏"：共享库缺失、软链指错，只有构建期
    `--version` 自检（Dockerfile 里每段安装末尾都有）才抓得住。这条测试
    守的是"有没有人管"，构建自检守的是"管不管用"。
    """
    import re

    raw = dockerfile.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("#")
    )
    joined = re.sub(r"\\\n", " ", body)

    # 这些词出现在安装行里但不是包名（`apt-get install -y` 的语法片段）。
    not_packages = {
        "apt", "apt-get", "get", "install", "pip", "pip3", "y", "yes", "no",
        "recommends", "update", "upgrade", "python", "python3", "m", "run",
        "and", "then", "clean", "rm", "var", "lib", "cache", "true",
    }
    found: set[str] = set()
    for match in re.finditer(
        r"(?:apt-get\s+install|apt\s+install|pip\s+install)[^\n]*", joined
    ):
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._+-]*", match.group(0)):
            lowered = token.lower()
            if lowered not in not_packages:
                found.add(lowered)

    # 来源 2：软链 / 显式 install 到 /usr/local/bin 的二进制。
    # 只认这个前缀 —— 它是 PATH 上的标准位置，能证明"命令可被调用"。
    for match in re.finditer(r"/usr/local/bin/([A-Za-z0-9][A-Za-z0-9._+-]*)", joined):
        found.add(match.group(1).lower())

    return found


def test_real_skills_core_pentest_playbooks_present():
    """答辩故事需要的几篇必须在（工具协同 + 闭环纪律）。"""
    names = set(skills.skills_by_name(_real_skills()))
    for required in ("sql-injection", "ssrf", "idor", "nmap", "ffuf", "sqlmap",
                     "counterevidence", "severity-calibration"):
        assert required in names, f"缺少关键技能包：{required}"


# ── 组 3：工具接线 ──────────────────────────────────────────


def test_load_skill_in_tool_names():
    assert "load_skill" in runner.TOOL_NAMES


async def test_dispatch_load_skill_routes_to_skills_module(monkeypatch):
    captured: dict[str, str] = {}

    def fake_load(name: str, **_kw):
        captured["name"] = name
        return "SKILL-BODY"

    monkeypatch.setattr(runner.skills_mod, "load_skill_text", fake_load)

    # dispatch_tool 的 load_skill 分支不碰 sandbox/http，用 None 占位即可。
    out = await runner.dispatch_tool(
        "load_skill", {"name": "sql_injection"},
        spec=None, sandbox=None, http=None,  # type: ignore[arg-type]
    )
    assert out == "SKILL-BODY"
    assert captured["name"] == "sql_injection"


async def test_dispatch_load_skill_missing_name_arg(monkeypatch):
    seen: dict[str, str] = {}

    def fake_load(name: str, **_kw):
        seen["name"] = name
        return f"没有名为 `{name}` 的技能包。可用技能包：x, y"

    monkeypatch.setattr(runner.skills_mod, "load_skill_text", fake_load)
    out = await runner.dispatch_tool(
        "load_skill", {}, spec=None, sandbox=None, http=None,  # type: ignore[arg-type]
    )
    assert seen["name"] == ""  # 缺参数按空名，回可用清单
    assert "可用技能包" in out


def test_system_prompt_placeholder_replaced():
    text = runner.load_system_prompt()
    assert "{{SKILL_INDEX}}" not in text, "占位符没被替换"
    # 真实技能清单里至少能看到一篇的名字
    assert "`sql-injection`" in text or "sql-injection" in text


def test_system_prompt_advertises_load_skill():
    text = runner.load_system_prompt()
    assert "load_skill" in text


def test_system_prompt_survives_missing_skills_dir(monkeypatch, tmp_path: Path):
    """技能目录缺失时提示词仍可用（占位符换成一句说明，不是崩）。"""
    monkeypatch.setattr(runner.skills_mod, "SKILLS_ROOT", tmp_path / "nope")
    text = runner.load_system_prompt()
    assert "{{SKILL_INDEX}}" not in text
    assert "没有附带技能包" in text
    assert "load_skill" in text  # 工具说明仍在
