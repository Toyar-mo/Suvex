"""技能包**附件**的测试（v0.90）。

## 这一组在钉什么

v0.90 把"一篇技能 = 一个 `.md` 文件"扩成"一篇技能 = 一个**目录包**"
（学 CyberStrikeAI：`<name>/SKILL.md` + `<name>/scripts/x.sh`）。

这个改动有三个地方会**静默**出错，而它们都不会在正常手工试用里暴露：

1. **升级形态后旧判断失效**。`_resolve_target()` 只算得出单文件形态的
   路径 `<category>/<name>.md`，而目录包的正文在 `<category>/<name>/SKILL.md`。
   于是所有 `target.is_file()` 判断对目录包永远为假 —— 表现是
   "技能明明在，但读/改/删全都说它不存在"。这是本项目反复踩过的形状：
   新形态加进来了，旧代码里"判断存在"的那一句没跟着改。

2. **`None` 与 `{}` 混为一谈**。`files=None` 是"不动附件"，
   `files={}` 是"这篇没有附件（清空）"。折叠的后果是"用户只改了一个字，
   附件全没了"，而他并没有要求删除。

3. **附件名是第二道目录穿越入口**。它来自页面输入框，会被拼到包目录下面
   再写文件；形如 `../../prompts/skills/x.md` 会**覆盖内置技能**，
   而那只会在下一次跑题时表现为"模型行为变怪"，diff 里看不出来。

## 覆盖策略

边界用直调（快、失败信息准），"接线是否接通"用 HTTP 走一遍。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.kernel import skill_store
from app.kernel import skills as skills_mod

API = "/api/v1"


@pytest.fixture(autouse=True)
def _isolated_custom_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """每个用例一个独立的自定义技能目录（与 test_skills_manage 同款）。"""
    custom = tmp_path / "custom_skills"
    monkeypatch.setattr(skills_mod, "CUSTOM_SKILLS_ROOT", custom)
    monkeypatch.setattr(skill_store, "_custom_root", lambda: custom)
    return custom


def _pkg(custom_root: Path, category: str, name: str) -> Path:
    """目录包在磁盘上的位置。"""
    return custom_root / category / name


# ══════════════════════════════════════════════════════════
# 1. 形态：带附件的技能落成目录包，且能被发现层认出来
# ══════════════════════════════════════════════════════════


def test_attachments_land_next_to_skill_md(_isolated_custom_root: Path) -> None:
    """带附件时落成 `<name>/SKILL.md` + `<name>/scripts/x.sh`。"""
    target = skill_store.create_or_update_skill(
        name="demo",
        category="analysis",
        description="d",
        body="正文",
        files={"scripts/x.sh": "echo hi"},
    )

    pkg = _pkg(_isolated_custom_root, "analysis", "demo")
    assert target == pkg / "SKILL.md", f"主文件不在包目录里：{target}"
    assert (pkg / "scripts" / "x.sh").read_text(encoding="utf-8") == "echo hi"


def test_package_is_discovered_with_its_file_list(_isolated_custom_root: Path) -> None:
    """★ 目录包必须被 `discover_custom_skills()` 认出来，且带出附件清单。

    这条挡的是第 1 类静默失败：发现层仍按 `*.md` 递归扫，
    如果哪天改成只扫一层，目录包就会整片消失而不报错。
    """
    skill_store.create_or_update_skill(
        name="demo",
        category="analysis",
        description="d",
        body="正文",
        files={"scripts/x.sh": "echo hi", "checklist.md": "- [ ] a"},
    )

    found = {s.name: s for s in skills_mod.discover_custom_skills()}
    assert "demo" in found, "目录包没被发现层认出来"
    demo = found["demo"]
    assert demo.files == ("checklist.md", "scripts/x.sh"), demo.files
    assert demo.package_dir is not None, "目录包的 package_dir 不该是 None"
    assert demo.path.name == "SKILL.md"


def test_plain_skill_stays_a_single_file(_isolated_custom_root: Path) -> None:
    """★ 不带附件时**仍是单文件**（31 篇内置技能全是这个形态）。

    不能因为加了附件能力就让所有技能都变成目录 —— 那会让
    "没附件的技能"也散成一堆目录，且改变既有部署的磁盘形态。
    """
    target = skill_store.create_or_update_skill(
        name="plain", category="analysis", description="d", body="正文"
    )
    assert target == _isolated_custom_root / "analysis" / "plain.md"
    assert not (target.parent / "plain").exists(), "不该建出多余的包目录"


# ══════════════════════════════════════════════════════════
# 2. `None` 与 `{}` 的语义差别（最容易写错的地方）
# ══════════════════════════════════════════════════════════


def test_none_keeps_existing_attachments(_isolated_custom_root: Path) -> None:
    """★ `files=None` = 不动附件。

    这是"用户在页面上只改了一个字"的场景。如果它按 `{}` 处理，
    保存一次就把他没打算删的附件全删了 —— 而那**不会报错**。
    """
    skill_store.create_or_update_skill(
        name="demo",
        category="analysis",
        description="d",
        body="第一版",
        files={"scripts/x.sh": "echo hi"},
    )

    skill_store.create_or_update_skill(
        name="demo",
        category="analysis",
        description="d",
        body="第二版",
        files=None,  # ← 只改正文
    )

    pkg = _pkg(_isolated_custom_root, "analysis", "demo")
    assert (pkg / "scripts" / "x.sh").is_file(), "只改正文却把附件删了"
    body = (pkg / "SKILL.md").read_text(encoding="utf-8")
    assert "第二版" in body


def test_empty_dict_clears_attachments(_isolated_custom_root: Path) -> None:
    """★ `files={}` = 这篇没有附件（清空）。"""
    skill_store.create_or_update_skill(
        name="demo",
        category="analysis",
        description="d",
        body="正文",
        files={"scripts/x.sh": "echo hi"},
    )

    skill_store.create_or_update_skill(
        name="demo",
        category="analysis",
        description="d",
        body="正文",
        files={},
    )

    pkg = _pkg(_isolated_custom_root, "analysis", "demo")
    assert not (pkg / "scripts" / "x.sh").exists(), "传了 {} 却没清掉附件"


def test_replacing_attachment_set_drops_the_old_ones(_isolated_custom_root: Path) -> None:
    """全量替换：新的一批进来，旧的那批（不在新表里）要删掉。

    否则页面上"保存成功"之后，磁盘上还留着一份看不见的旧附件 ——
    而它仍会进模型上下文。
    """
    skill_store.create_or_update_skill(
        name="demo",
        category="analysis",
        description="d",
        body="正文",
        files={"old.sh": "old", "keep.md": "keep"},
    )
    skill_store.create_or_update_skill(
        name="demo",
        category="analysis",
        description="d",
        body="正文",
        files={"keep.md": "keep"},
    )

    pkg = _pkg(_isolated_custom_root, "analysis", "demo")
    assert not (pkg / "old.sh").exists(), "旧附件没被清掉"
    assert (pkg / "keep.md").is_file(), "还在表里的附件被误删了"


# ══════════════════════════════════════════════════════════
# 3. 附件名是第二道目录穿越入口
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "bad",
    [
        "../../prompts/skills/analysis/counterevidence.md",  # 覆盖内置技能
        "../escape.md",
        "/etc/passwd",
        "scripts/../../escape.sh",
        ".",
        "..",
        "",
        "a//b.sh",
        "scripts/with space.sh",
        "a/b/c/d/e.sh",  # 段数超限
        "SKILL.md",  # 主文件名不该从这个入口进
    ],
)
def test_rejects_unsafe_attachment_names(bad: str, _isolated_custom_root: Path) -> None:
    """★ 非法附件名必须整单拒绝，且**一个文件都不写**。"""
    with pytest.raises(skill_store.SkillWriteError):
        skill_store.create_or_update_skill(
            name="demo",
            category="analysis",
            description="d",
            body="正文",
            files={bad: "x"},
        )

    pkg = _pkg(_isolated_custom_root, "analysis", "demo")
    assert not pkg.exists(), f"被拒绝了却仍建出了目录：{list(pkg.rglob('*'))}"


def test_builtin_skill_cannot_be_overwritten_by_an_attachment(
    _isolated_custom_root: Path,
) -> None:
    """★ 最坏情况：附件路径指向**内置技能**，必须挡住且内置文件不变。

    这条不是重复上面那条 parametrize —— 它断言的是**后果**：
    内置文件的内容在尝试前后必须一模一样。
    """
    builtin = skills_mod.discover_skills()[0]
    before = builtin.path.read_bytes()

    with pytest.raises(skill_store.SkillWriteError):
        skill_store.create_or_update_skill(
            name="demo",
            category="analysis",
            description="d",
            body="正文",
            files={f"../../../prompts/skills/{builtin.category}/{builtin.name}.md": "PWNED"},
        )

    assert builtin.path.read_bytes() == before, "内置技能文件被附件写坏了"


def test_one_bad_attachment_writes_none_of_them(_isolated_custom_root: Path) -> None:
    """★ 整单原子性：第 2 个附件不合法时，第 1 个也不能落盘。

    边校验边写的后果是"保存失败，但磁盘上多了一半文件"，
    而用户下次打开会看到那些幽灵附件，它们还会进模型上下文。
    """
    with pytest.raises(skill_store.SkillWriteError):
        skill_store.create_or_update_skill(
            name="demo",
            category="analysis",
            description="d",
            body="正文",
            files={"good.sh": "echo ok", "../bad.sh": "x"},
        )

    pkg = _pkg(_isolated_custom_root, "analysis", "demo")
    assert not (pkg / "good.sh").exists(), "合法的那一个也不该写进去"


# ══════════════════════════════════════════════════════════
# 4. 升级/降级形态：单文件 ↔ 目录包
# ══════════════════════════════════════════════════════════


def test_single_file_upgrades_to_package_without_leaving_both(
    _isolated_custom_root: Path,
) -> None:
    """★ 单文件加附件后，旧的 `<name>.md` 必须消失。

    两者同时存在时，发现层会把它俩认成**两篇同名技能**，
    而 `load_skill` 只会取到其中一篇 —— 用户改的那篇可能不生效。
    """
    skill_store.create_or_update_skill(
        name="demo", category="analysis", description="d", body="旧正文"
    )
    single = _isolated_custom_root / "analysis" / "demo.md"
    assert single.is_file()

    skill_store.create_or_update_skill(
        name="demo",
        category="analysis",
        description="d",
        body="新正文",
        files={"scripts/x.sh": "echo hi"},
    )

    assert not single.exists(), "升级成目录包后旧单文件还在"
    found = [s for s in skills_mod.discover_custom_skills() if s.name == "demo"]
    assert len(found) == 1, f"同一个技能被认成了 {len(found)} 篇"


def test_update_finds_a_package_skill(_isolated_custom_root: Path) -> None:
    """★ 目录包必须能被 `must_exist=True` 的更新找到。

    这条直接挡第 1 类静默失败：`_resolve_target()` 只算得出单文件路径，
    若"判断存在"仍用 `target.is_file()`，目录包的每次编辑都会报
    "没有找到叫 xxx 的技能"。
    """
    skill_store.create_or_update_skill(
        name="demo",
        category="analysis",
        description="d",
        body="第一版",
        files={"a.md": "a"},
    )

    # 不抛错即通过（抛错就是那个 bug）
    skill_store.create_or_update_skill(
        name="demo",
        category="analysis",
        description="d",
        body="第二版",
        files={"a.md": "a"},
        must_exist=True,
    )
    body = (_pkg(_isolated_custom_root, "analysis", "demo") / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "第二版" in body


def test_delete_removes_the_whole_package(_isolated_custom_root: Path) -> None:
    """删目录包要连附件一起删 —— 留下附件就是"删不掉的孤儿"。"""
    skill_store.create_or_update_skill(
        name="demo",
        category="analysis",
        description="d",
        body="正文",
        files={"scripts/x.sh": "echo hi"},
    )

    skill_store.delete_skill(name="demo", category="analysis")

    assert not _pkg(_isolated_custom_root, "analysis", "demo").exists()
    assert not any(s.name == "demo" for s in skills_mod.discover_custom_skills())


# ══════════════════════════════════════════════════════════
# 5. `load_skill(file=...)` 读附件
# ══════════════════════════════════════════════════════════


def test_load_attachment_by_relative_path(_isolated_custom_root: Path) -> None:
    """模型带 `file` 时要能读到附件正文。"""
    skill_store.create_or_update_skill(
        name="demo",
        category="analysis",
        description="d",
        body="主文件正文",
        files={"scripts/x.sh": "echo hi"},
    )

    text = skills_mod.load_skill_text("demo", file="scripts/x.sh")
    assert "echo hi" in text


def test_load_missing_attachment_lists_what_exists(_isolated_custom_root: Path) -> None:
    """拼错附件名时回可用清单 —— 模型才知道该重拉哪一个。"""
    skill_store.create_or_update_skill(
        name="demo",
        category="analysis",
        description="d",
        body="主文件正文",
        files={"scripts/x.sh": "echo hi"},
    )

    text = skills_mod.load_skill_text("demo", file="scripts/nope.sh")
    assert "scripts/x.sh" in text, f"拼错时没回可用清单：{text!r}"


def test_load_attachment_cannot_escape_the_package(_isolated_custom_root: Path) -> None:
    """★ 读也要挡穿越 —— 写挡住而读没挡，等于能读部署目录。

    构造一个真实存在的"包外文件"来试：只要能读到它，就是穿越成功。
    """
    secret = _isolated_custom_root.parent / "secret.txt"
    secret.write_text("TOP-SECRET", encoding="utf-8")

    skill_store.create_or_update_skill(
        name="demo",
        category="analysis",
        description="d",
        body="正文",
        files={"a.md": "a"},
    )

    text = skills_mod.load_skill_text("demo", file="../../secret.txt")
    assert "TOP-SECRET" not in text, f"读穿越成功了：{text!r}"


# ══════════════════════════════════════════════════════════
# 6. HTTP 接线：页面能建出带附件的技能
# ══════════════════════════════════════════════════════════


def test_create_with_files_via_http(ready_client) -> None:  # noqa: ANN001
    """`POST /kernel/skills` 带 `files` 时要落成目录包，且详情回得出清单。"""
    resp = ready_client.post(
        f"{API}/kernel/skills",
        json={
            "name": "http-pkg",
            "category": "demo",
            "description": "d",
            "body": "正文",
            "files": {"scripts/x.sh": "echo hi"},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["files"] == ["scripts/x.sh"], body

    detail = ready_client.get(f"{API}/kernel/skills/http-pkg")
    assert detail.status_code == 200, detail.text
    assert detail.json()["files"] == ["scripts/x.sh"]


def test_update_without_files_keeps_them_via_http(ready_client) -> None:  # noqa: ANN001
    """★ HTTP 层：`PUT` 不带 `files` 字段 = 不动附件。

    这条挡的是"路由层把缺省值折叠成 `{}`"——那样界面上只改正文
    就会静默清空附件。
    """
    ready_client.post(
        f"{API}/kernel/skills",
        json={
            "name": "http-keep",
            "category": "demo",
            "description": "d",
            "body": "第一版",
            "files": {"scripts/x.sh": "echo hi"},
        },
    )

    resp = ready_client.put(
        f"{API}/kernel/skills/http-keep",
        json={"description": "d", "body": "第二版"},  # ← 不带 files
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["files"] == ["scripts/x.sh"], "只改正文却把附件清空了"


def test_update_with_empty_files_clears_them_via_http(ready_client) -> None:  # noqa: ANN001
    """显式传 `files: {}` 才清空 —— 与上一条正好构成一对。"""
    ready_client.post(
        f"{API}/kernel/skills",
        json={
            "name": "http-clear",
            "category": "demo",
            "description": "d",
            "body": "正文",
            "files": {"scripts/x.sh": "echo hi"},
        },
    )

    resp = ready_client.put(
        f"{API}/kernel/skills/http-clear",
        json={"description": "d", "body": "正文", "files": {}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["files"] == [], "传了 {} 却没清空"
