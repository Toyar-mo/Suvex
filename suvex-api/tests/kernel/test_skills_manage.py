"""技能管理（自定义技能的增删改 + 安全边界）测试（v0.88）。

## 这一组在钉什么

技能管理有一条**与其它 CRUD 不同**的硬约束：

    内置技能在 `prompts/skills/`，随代码分发、部署时整目录覆盖。
    自定义技能必须写在**另一个**目录里，否则用户建的东西会在
    下一次部署时**静默消失**。

所以这里测的不是"能不能存"，而是三件后果严重的事：

1. **写路径的边界**：名字/分类里带 `..`、`/`、绝对路径时，必须被挡住 ——
   而这条边界不是"防御性编程"，它是这个功能能不能存在的前提；
2. **内置技能只读**：改/删内置技能必须被拒，且原因要说清"部署会覆盖"；
3. **同名不同源**：内置与自定义同名时**内置优先**，且要记 warning
   （否则"我建的那个没生效"完全无从解释）。

## 为什么不用真 HTTP 起服务测路径边界

写路径的边界是**文件系统层**的判断（`_resolve_target`）。用 HTTP 测
需要造 5 种非法输入 × 起服务，而那一条判断的执行路径与 `POST` 完全一样。
所以边界用直调测（快、失败信息准），HTTP 层只测"接线是否接通"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.kernel import skill_store
from app.kernel import skills as skills_mod

API = "/api/v1"


@pytest.fixture(autouse=True)
def _isolated_custom_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个用例一个独立的自定义技能目录。

    ⚠️ 必须**同时** patch 两处：
      · `skills.CUSTOM_SKILLS_ROOT` —— 读取路径（`discover_custom_skills`）；
      · `skill_store._custom_root` —— 写入路径（它是个函数，
        每次现读 `skills.CUSTOM_SKILLS_ROOT`；这里也 patch 一份，
        免得将来有人把它改成模块级缓存而我们没覆盖到）。
    """
    custom = tmp_path / "custom_skills"
    monkeypatch.setattr(skills_mod, "CUSTOM_SKILLS_ROOT", custom)
    monkeypatch.setattr(skill_store, "_custom_root", lambda: custom)
    return custom


# ══════════════════════════════════════════════════════════
# 1. 写路径边界（这一组最重要）
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "name",
    ["../evil", "..", "a/b", "a\\b", "/etc/passwd", ".hidden", "MyName", "with space", ""],
)
def test_rejects_bad_skill_names(name: str) -> None:
    """★ 非法技能名必须被拒。

    它们会变成文件路径的一段。允许 `..` 或 `/` 就是**目录穿越**的入口 ——
    而那个目录之外是随代码分发的内置技能与整个部署目录。
    """
    with pytest.raises(skill_store.SkillWriteError):
        skill_store.create_or_update_skill(
            name=name, category="demo", description="x", body="正文"
        )


@pytest.mark.parametrize("category", ["../prompts", "..", "a/b", "/abs", "Bad", ""])
def test_rejects_bad_categories(category: str) -> None:
    """分类同样会变成路径的一段，同样要挡。"""
    with pytest.raises(skill_store.SkillWriteError):
        skill_store.create_or_update_skill(
            name="ok-name", category=category, description="x", body="正文"
        )


def test_target_always_stays_inside_custom_root(_isolated_custom_root: Path) -> None:
    """★ 解析出来的路径**必须**落在自定义目录之内（这是安全边界本身）。

    用合法名字走一遍，断言 `resolve()` 后仍在根目录下 ——
    万一将来有人放宽了正则，这条会红。
    """
    target = skill_store._resolve_target("demo", "ok-name")
    root = _isolated_custom_root.resolve()
    resolved = target.resolve()
    assert root == resolved or root in resolved.parents


def test_create_writes_a_loadable_skill(_isolated_custom_root: Path) -> None:
    """创建后**能被 `all_skills()` 读到**，且 source 是 custom。

    这条挡的是"写进去了但读不到"—— 比如路径拼错、或发现逻辑没扫那个目录。
    """
    skill_store.create_or_update_skill(
        name="my-check",
        category="demo",
        description="一句话描述\n第二行",
        body="# 标题\n\n正文内容",
    )
    found = {s.name: s for s in skills_mod.all_skills()}
    assert "my-check" in found
    assert found["my-check"].source == "custom"
    # 折叠标量的描述要能正确读回（多行应当折叠成一行带空格）
    assert "一句话描述" in found["my-check"].description
    assert "第二行" in found["my-check"].description


def test_body_cannot_be_blank(_isolated_custom_root: Path) -> None:
    """空正文必须被拒 —— 空技能会让模型读到一段空内容。"""
    with pytest.raises(skill_store.SkillWriteError):
        skill_store.create_or_update_skill(
            name="empty", category="demo", description="x", body="   \n  "
        )


def test_oversized_content_is_rejected(_isolated_custom_root: Path) -> None:
    """超大正文被拒（防止把整个模型上下文一次吃满）。"""
    with pytest.raises(skill_store.SkillWriteError):
        skill_store.create_or_update_skill(
            name="huge",
            category="demo",
            description="x",
            body="a" * (skill_store.MAX_CONTENT_CHARS + 1),
        )


# ══════════════════════════════════════════════════════════
# 2. 内置技能只读
# ══════════════════════════════════════════════════════════


def test_cannot_create_with_a_builtin_name(_isolated_custom_root: Path) -> None:
    """★ 不能建一篇与内置同名的自定义技能。

    内置优先，所以同名的那篇**永远不会生效** —— 与其让它静默失效，
    不如直接拒绝并说清原因。
    """
    builtin_name = skills_mod.discover_skills()[0].name
    with pytest.raises(skill_store.SkillWriteError) as exc:
        skill_store.create_or_update_skill(
            name=builtin_name, category="demo", description="x", body="正文"
        )
    assert "内置" in str(exc.value)


def test_cannot_delete_a_builtin_skill(_isolated_custom_root: Path) -> None:
    """★ 内置技能删不掉，且原因要说清"部署会覆盖、要停用请改工具开关"。"""
    builtin = skills_mod.discover_skills()[0]
    with pytest.raises(skill_store.SkillWriteError) as exc:
        skill_store.delete_skill(name=builtin.name, category=builtin.category)
    message = str(exc.value)
    assert "内置" in message
    # 必须给出下一步（用户该怎么做），而不是只拒绝
    assert "部署" in message


def test_builtin_skills_are_still_present(_isolated_custom_root: Path) -> None:
    """删内置的尝试失败后，内置技能**必须还在**（不能删一半）。"""
    builtin = skills_mod.discover_skills()[0]
    with pytest.raises(skill_store.SkillWriteError):
        skill_store.delete_skill(name=builtin.name, category=builtin.category)
    assert builtin.path.is_file(), "内置技能文件被删掉了 —— 这是不可接受的"


# ══════════════════════════════════════════════════════════
# 3. 同名冲突：内置优先
# ══════════════════════════════════════════════════════════


def test_builtin_wins_on_name_collision(_isolated_custom_root: Path) -> None:
    """★ 同名时内置优先，且自定义那篇被**忽略**（不是覆盖）。

    ## 为什么这条重要

    内置技能经过评审、随代码分发；自定义技能是运行时由人写的。
    若让自定义覆盖内置，就等于"任何能开设置页的人都能悄悄改掉
    内置技能的行为" —— 而技能正文会进模型上下文，那是一处不该有的后门。

    ⚠️ 这里**绕过** `create_or_update_skill` 的同名检查（它会直接拒绝），
    手工往目录里写一份同名文件 —— 模拟"文件是通过别的方式放进去的"
    （比如有人直接 scp 上去）。防线必须在读取侧也成立，不能只靠写入侧。
    """
    builtin = skills_mod.discover_skills()[0]
    target = skills_mod.CUSTOM_SKILLS_ROOT / "sneaky" / f"{builtin.name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"---\nname: {builtin.name}\ndescription: 伪造的\n---\n\n恶意正文\n",
        encoding="utf-8",
    )

    found = {s.name: s for s in skills_mod.all_skills()}
    assert found[builtin.name].source == "builtin", (
        "自定义的同名技能覆盖了内置的 —— 这是一处后门"
    )


# ══════════════════════════════════════════════════════════
# 4. 编辑与删除
# ══════════════════════════════════════════════════════════


def test_update_requires_existing(_isolated_custom_root: Path) -> None:
    """编辑一篇不存在的技能 → 报"没有这篇"，**不能悄悄新建**。

    这是"编辑"与"新建"最容易混的一处：`must_exist=True` 保证
    名字拼错时会报错，而不是在用户不知情时多出一篇。
    """
    with pytest.raises(skill_store.SkillWriteError):
        skill_store.create_or_update_skill(
            name="ghost", category="demo", description="x", body="正文", must_exist=True
        )


def test_update_then_read_round_trips(_isolated_custom_root: Path) -> None:
    """建 → 改 → 读回，内容是最新的（且旧内容不残留）。"""
    skill_store.create_or_update_skill(
        name="edit-me", category="demo", description="v1", body="第一版"
    )
    skill_store.create_or_update_skill(
        name="edit-me", category="demo", description="v2", body="第二版", must_exist=True
    )
    raw = skill_store.read_custom_skill(name="edit-me", category="demo")
    assert "第二版" in raw
    assert "第一版" not in raw
    assert "v2" in raw


def test_delete_removes_file_and_empty_dir(_isolated_custom_root: Path) -> None:
    """删掉最后一篇时，空分类目录也要清掉（否则界面上留一个空栏）。"""
    skill_store.create_or_update_skill(
        name="gone", category="tmpcat", description="x", body="正文"
    )
    category_dir = _isolated_custom_root / "tmpcat"
    assert category_dir.is_dir()

    skill_store.delete_skill(name="gone", category="tmpcat")
    assert not category_dir.exists(), "空分类目录没被清掉"


def test_delete_keeps_dir_when_others_remain(_isolated_custom_root: Path) -> None:
    """同一分类下还有别的技能时，目录**不能**被删。"""
    skill_store.create_or_update_skill(name="a", category="c1", description="x", body="A")
    skill_store.create_or_update_skill(name="b", category="c1", description="x", body="B")
    skill_store.delete_skill(name="a", category="c1")
    assert (_isolated_custom_root / "c1").is_dir()
    assert {s.name for s in skills_mod.discover_custom_skills()} == {"b"}


# ══════════════════════════════════════════════════════════
# 5. HTTP 接线（只测"通没通"，边界由上面那组覆盖）
# ══════════════════════════════════════════════════════════


def test_skills_endpoints_are_registered() -> None:
    """5 个技能端点都在 OpenAPI 里（漏注册的症状是前端 404）。"""
    from app.main import create_app

    paths = create_app().openapi()["paths"]
    assert "get" in paths.get("/api/v1/kernel/skills", {})
    assert "post" in paths.get("/api/v1/kernel/skills", {})
    detail = paths.get("/api/v1/kernel/skills/{name}", {})
    assert {"get", "put", "delete"} <= set(detail)


def test_create_via_http_returns_custom_source(ready_client) -> None:  # noqa: ANN001
    """`POST /kernel/skills` 能建，回执的 source 是 custom。"""
    resp = ready_client.post(
        f"{API}/kernel/skills",
        json={
            "name": "http-made",
            "category": "demo",
            "description": "通过 HTTP 建的",
            "body": "# 正文\n内容",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "http-made"
    assert body["source"] == "custom"


def test_cannot_edit_builtin_via_http(ready_client) -> None:  # noqa: ANN001
    """★ 改内置技能 → 400，且原因说明"部署会覆盖"。"""
    builtin_name = skills_mod.discover_skills()[0].name
    resp = ready_client.put(
        f"{API}/kernel/skills/{builtin_name}",
        json={"description": "试图改", "body": "试图改"},
    )
    assert resp.status_code in (400, 422), resp.text
    assert "内置" in resp.text


# ══════════════════════════════════════════════════════════
# 6. 存储位置（防"部署覆盖"）
# ══════════════════════════════════════════════════════════


def test_custom_root_is_outside_builtin_skills_dir() -> None:
    """★ 自定义技能目录**不能**在内置技能目录里。

    这是整个功能能存在的前提：内置目录随部署被整目录覆盖，
    写进去的东西下次升级就没了 —— 而且**静默消失**，没有报错。
    """
    import os

    # 用默认值（不受 fixture 的 monkeypatch 影响）
    default_custom = Path(
        os.environ.get("SUPEX_CUSTOM_SKILLS")
        or (Path(skills_mod.__file__).resolve().parent.parent.parent / "custom_skills")
    )
    builtin_root = Path(skills_mod.__file__).resolve().parent.parent.parent / "prompts" / "skills"
    builtin_resolved = builtin_root.resolve()
    custom_resolved = default_custom.resolve()
    assert custom_resolved != builtin_resolved
    assert builtin_resolved not in custom_resolved.parents, (
        "自定义技能目录落在内置技能目录里面 —— 部署时会连同用户创建的内容一起被覆盖"
    )


# ══════════════════════════════════════════════════════════
# 7. 调用统计（"技能状态"）
# ══════════════════════════════════════════════════════════
#
# 这一组钉两件事：
#   1. **路由顺序**：`/skills/usage` 必须注册在 `/skills/{name}` 之前，
#      否则它永远回 404「没有叫 usage 的技能」。这个失败的形状很坏 ——
#      端点**在**，只是不可达。
#   2. **数的口径**：只有 `tool_call` + `tool='load_skill'` 的事件算数，
#      别的工具、别的事件类型都不能混进来（否则"被加载次数"毫无意义）。


def _emit_load_skill(db, challenge, name: str) -> None:  # noqa: ANN001
    """造一条 `load_skill` 工具调用事件。

    ★ 直接调 `emit_tool_call` 而不是手写 ActivityEvent 行：
    那个函数是**事件的唯一出口**（它负责 NUL 清洗等），绕过它造的
    测试数据会和真实数据形状不一致 —— 那时测试绿而线上错。
    """
    from app.kernel import events as kernel_events

    kernel_events.emit_tool_call(
        db,
        challenge,
        tool="load_skill",
        command=json.dumps({"name": name}, ensure_ascii=False),
        output="（技能正文）",
    )


def test_usage_route_is_registered_before_name_route() -> None:
    """★ `/skills/usage` 必须排在 `/skills/{name}` 前面。

    反过来的话 `GET /kernel/skills/usage` 会先撞上 `/skills/{name}`，
    把 `usage` 当成一个技能名 → 404。端点存在却永远不可达，
    而且错误信息（"没有叫 usage 的技能"）会把排查方向带偏到数据上。
    """
    from app.kernel.skills_router import router

    paths = [route.path for route in router.routes]
    assert "/kernel/skills/usage" in paths, "统计端点没注册"
    assert paths.index("/kernel/skills/usage") < paths.index("/kernel/skills/{name}"), (
        "统计端点注册在 /skills/{name} 之后 —— 它会被当成技能名，永远回 404"
    )


def test_list_includes_source(ready_client) -> None:  # noqa: ANN001
    """★ 清单里必须带 `source`。

    前端要据此决定"这一篇能不能改/删"。不带的话，界面要么全当只读
    （自定义的改不了），要么全当可改（点了内置的报错）—— 两种都错。
    """
    resp = ready_client.get(f"{API}/kernel/skills")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] > 0, "内置技能一篇都没读到 —— 打包路径有问题"
    sources = {
        skill["source"] for group in body["categories"] for skill in group["skills"]
    }
    assert sources == {"builtin"}, f"没有自定义技能时应当全是 builtin，实际 {sources}"


def test_usage_counts_only_load_skill(
    ready_client, db_session, ctf2_challenge, ctf2_provider  # noqa: ANN001
) -> None:
    """★ 只统计 `load_skill`，别的工具不能混进来。"""
    from app.kernel import events as kernel_events

    skill_name = skills_mod.discover_skills()[0].name
    _emit_load_skill(db_session, ctf2_challenge, skill_name)
    _emit_load_skill(db_session, ctf2_challenge, skill_name)
    # 干扰项：同名参数但是别的工具 —— 绝不能算进技能调用数。
    kernel_events.emit_tool_call(
        db_session,
        ctf2_challenge,
        tool="run_shell",
        command=json.dumps({"command": f"cat {skill_name}"}),
        output="x",
    )
    db_session.commit()

    resp = ready_client.get(f"{API}/kernel/skills/usage")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    counts = {item["name"]: item["loads"] for item in body["items"]}
    assert counts.get(skill_name) == 2, f"应当数到 2 次，实际 {counts}"
    assert sum(counts.values()) == 2, f"有非 load_skill 的事件混进统计：{counts}"


def test_usage_reports_last_loaded_at(
    ready_client, db_session, ctf2_challenge, ctf2_provider  # noqa: ANN001
) -> None:
    """最近一次加载时刻要有值（界面上"最后一次：…"靠它）。"""
    skill_name = skills_mod.discover_skills()[0].name
    _emit_load_skill(db_session, ctf2_challenge, skill_name)
    db_session.commit()

    resp = ready_client.get(f"{API}/kernel/skills/usage")
    items = {item["name"]: item for item in resp.json()["items"]}
    assert skill_name in items
    assert items[skill_name]["lastLoadedAt"], "最近加载时刻为空"
    assert items[skill_name]["lastLoadedAt"].startswith("20"), (
        f"时刻格式不像 ISO8601：{items[skill_name]['lastLoadedAt']}"
    )


def test_usage_survives_unparsable_command(
    ready_client, db_session, ctf2_challenge, ctf2_provider  # noqa: ANN001
) -> None:
    """★ 解不出名字的事件**不能让整页 500**。

    真实数据里 `command` 是被裁剪过的（`_clip`，上限 MAX_TEXT）——
    一个超长的参数会被截断，于是 `json.loads` 必然失败。
    这时应当跳过那一条继续算，而不是整份统计挂掉。
    """
    from app.kernel import events as kernel_events

    kernel_events.emit_tool_call(
        db_session,
        ctf2_challenge,
        tool="load_skill",
        command='{"name": "truncated-at-the-wrong-pla',
        output="x",
    )
    db_session.commit()

    resp = ready_client.get(f"{API}/kernel/skills/usage")
    assert resp.status_code == 200, resp.text
    # 那条解不出来，所以物品列表里没有它，但 sampledEvents 记账了。
    assert resp.json()["sampledEvents"] >= 1


def test_usage_is_readable_by_viewer(ready_client) -> None:  # noqa: ANN001
    """统计是只读的 —— 只读角色也要能看（它不含凭据、不改状态）。"""
    resp = ready_client.get(f"{API}/kernel/skills/usage")
    assert resp.status_code == 200, resp.text



# ── v0.90：附件内容读回端点 ────────────────────────────────
#
# ★ 这一组测的是**编辑器能不能安全保存**，不是"端点存不存在"。
#
#   背景：`PUT` 的 `files` 是全量替换语义。编辑器要"只改正文"时，
#   必须把现有附件内容原样带回去 —— 而详情接口只给文件名清单，没内容。
#   若这个端点拿不到内容，前端唯一的选择就是保存时不带 `files`，
#   而那样用户就永远删不掉附件；若带 `{}`，改一个字就静默清空全部附件。


def test_files_route_is_registered() -> None:
    """附件内容端点在 OpenAPI 里（漏注册 = 前端 404）。"""
    from app.main import create_app

    paths = create_app().openapi()["paths"]
    assert "get" in paths.get("/api/v1/kernel/skills/{name}/files", {})


def test_files_route_does_not_shadow_detail(ready_client) -> None:  # noqa: ANN001
    """★ 加了 `/files` 之后，`GET /skills/{name}` 仍必须工作。

    路由顺序错了的典型症状：`/skills/{name}` 把 `/skills/x/files`
    当成 name="x/files" 匹配掉，于是附件端点永远 404。
    这里两条都打一遍，确保它们互不吃掉。
    """
    ready_client.post(
        f"{API}/kernel/skills",
        json={
            "name": "route-probe",
            "category": "demo",
            "description": "路由探测",
            "body": "# 正文",
        },
    )
    detail = ready_client.get(f"{API}/kernel/skills/route-probe")
    assert detail.status_code == 200, detail.text
    assert detail.json()["name"] == "route-probe"

    files = ready_client.get(f"{API}/kernel/skills/route-probe/files")
    assert files.status_code == 200, files.text


def test_files_returns_real_content(ready_client) -> None:  # noqa: ANN001
    """带附件建的技能，读回的附件**内容**与写入的一致。

    ★ 断言的是内容相等，不是"长度 > 0" —— 后者在返回了文件名或
      空串时也能通过，而那正是会让编辑器保存时清空附件的形状。
    """
    payload = {
        "name": "with-attach",
        "category": "demo",
        "description": "带附件",
        "body": "# 正文\n见附件",
        "files": {
            "checklist.md": "# 清单\n- [ ] 第一项\n",
            "scripts/probe.sh": "#!/bin/sh\necho hi\n",
        },
    }
    created = ready_client.post(f"{API}/kernel/skills", json=payload)
    assert created.status_code == 201, created.text

    resp = ready_client.get(f"{API}/kernel/skills/with-attach/files")
    assert resp.status_code == 200, resp.text
    files = resp.json()["files"]
    assert files["checklist.md"] == payload["files"]["checklist.md"]
    assert files["scripts/probe.sh"] == payload["files"]["scripts/probe.sh"]
    # SKILL.md 本身不算附件 —— 它是正文，由详情接口给
    assert "SKILL.md" not in files


def test_files_is_empty_for_single_file_skill(ready_client) -> None:  # noqa: ANN001
    """不带附件的技能 → `{}`，不是 404。

    ★ 区别很重要：`{}` 表示"这篇确实没有附件"（编辑器因此可以安全地
      保存空 files = 清空），而 404 会让编辑器以为"读失败了，别动附件"。
    """
    ready_client.post(
        f"{API}/kernel/skills",
        json={
            "name": "no-attach",
            "category": "demo",
            "description": "无附件",
            "body": "# 正文",
        },
    )
    resp = ready_client.get(f"{API}/kernel/skills/no-attach/files")
    assert resp.status_code == 200, resp.text
    assert resp.json()["files"] == {}


def test_files_404_for_builtin(ready_client) -> None:  # noqa: ANN001
    """内置技能的附件不可编辑 → 404（不是空 {}）。

    空 `{}` 会被误读成"这篇没有附件"，而内置技能可能有随代码分发的附件。
    回 404 才是实话："这里没有你可以改的东西"。
    """
    builtin_name = skills_mod.discover_skills()[0].name
    resp = ready_client.get(f"{API}/kernel/skills/{builtin_name}/files")
    assert resp.status_code == 404, resp.text


def test_files_404_for_missing_skill(ready_client) -> None:  # noqa: ANN001
    """不存在的技能 → 404。"""
    resp = ready_client.get(f"{API}/kernel/skills/does-not-exist/files")
    assert resp.status_code == 404, resp.text


def test_roundtrip_preserves_attachments_when_body_edited(ready_client) -> None:  # noqa: ANN001
    """★ 核心场景：拉内容 → 只改正文 → 原样带回 → 附件还在。

    这就是编辑器真实要做的事。少了这个端点，这条链路走不通。
    """
    ready_client.post(
        f"{API}/kernel/skills",
        json={
            "name": "edit-body",
            "category": "demo",
            "description": "改正文",
            "body": "# 旧正文",
            "files": {"keep.md": "别丢我"},
        },
    )

    files = ready_client.get(f"{API}/kernel/skills/edit-body/files").json()["files"]
    assert files == {"keep.md": "别丢我"}

    updated = ready_client.put(
        f"{API}/kernel/skills/edit-body",
        json={"description": "改正文", "body": "# 新正文", "files": files},
    )
    assert updated.status_code == 200, updated.text

    after = ready_client.get(f"{API}/kernel/skills/edit-body/files").json()["files"]
    assert after == {"keep.md": "别丢我"}, "只改正文却把附件弄丢了"
    detail = ready_client.get(f"{API}/kernel/skills/edit-body").json()
    assert detail["body"].endswith("# 新正文")


def test_omitting_files_keeps_attachments(ready_client) -> None:  # noqa: ANN001
    """★ `files` 省略（None）= 不动附件；`{}` = 清空。两者必须不同。

    这条固定住后端的 `files: dict | None` 语义 —— 若哪天有人把
    "省略"当成 `{}` 处理，编辑器"只改描述"就会静默删光附件。
    """
    ready_client.post(
        f"{API}/kernel/skills",
        json={
            "name": "omit-probe",
            "category": "demo",
            "description": "省略",
            "body": "# 正文",
            "files": {"a.md": "内容 A"},
        },
    )

    # 省略 files → 附件应保留
    resp = ready_client.put(
        f"{API}/kernel/skills/omit-probe",
        json={"description": "只改描述", "body": "# 正文"},
    )
    assert resp.status_code == 200, resp.text
    kept = ready_client.get(f"{API}/kernel/skills/omit-probe/files").json()["files"]
    assert kept == {"a.md": "内容 A"}, "省略 files 不应删附件"

    # 显式 {} → 附件应清空
    resp = ready_client.put(
        f"{API}/kernel/skills/omit-probe",
        json={"description": "清空附件", "body": "# 正文", "files": {}},
    )
    assert resp.status_code == 200, resp.text
    cleared = ready_client.get(f"{API}/kernel/skills/omit-probe/files").json()["files"]
    assert cleared == {}, "显式传 {} 应当清空附件"


def test_list_reports_file_count(ready_client) -> None:  # noqa: ANN001
    """清单里带 fileCount，界面据此标出"这一篇不只是一段文本"。"""
    ready_client.post(
        f"{API}/kernel/skills",
        json={
            "name": "counted",
            "category": "demo",
            "description": "有附件",
            "body": "# 正文",
            "files": {"x.md": "1", "y.md": "2", "sub/z.md": "3"},
        },
    )
    resp = ready_client.get(f"{API}/kernel/skills")
    assert resp.status_code == 200, resp.text
    counts = {
        item["name"]: item["fileCount"]
        for group in resp.json()["categories"]
        for item in group["skills"]
    }
    assert counts["counted"] == 3
