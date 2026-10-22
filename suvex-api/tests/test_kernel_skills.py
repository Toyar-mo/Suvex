"""技能包（`app/kernel/skills.py`）与 `load_skill` 工具的测试。

三组：
1. 纯逻辑 —— frontmatter 解析、清单渲染、按名查、未命中回清单、目录缺失不崩。
2. 真实随仓库分发的技能包 —— 现有 22 篇能被发现、name 全局唯一、
   与镜像实装工具对得上（这条防的是「vendor 了讲不存在工具的 skill」那类回归）。
3. 工具接线 —— `load_skill` 在 TOOL_NAMES 里、dispatch 能路由到它、
   提示词占位符被替换、缺目录时提示词仍可用。
"""

from __future__ import annotations

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


def test_real_skills_no_skill_for_uninstalled_tools():
    """★ 防「vendor 了讲镜像没装的工具的 skill」这类回归。

    实测镜像里没有 nuclei/katana/naabu/subfinder/semgrep/hurl，
    httpx 是 Python 库的 CLI 而非 ProjectDiscovery 的工具。这些 tooling
    skill 一旦被 vendor 进来，就在教模型用不存在的命令 —— 正是我们反复
    强调的反例。所以断言这些名字**不存在**于我们的技能包里。
    """
    names = set(skills.skills_by_name(_real_skills()))
    forbidden = {"nuclei", "katana", "naabu", "subfinder", "semgrep", "hurl", "httpx"}
    leaked = names & forbidden
    assert not leaked, f"vendor 了镜像里没有的工具的 skill：{leaked}"


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
