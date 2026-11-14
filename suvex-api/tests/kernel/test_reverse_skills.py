"""Imported reverse-skill modules must fit the Suvex sandbox.

Source: zhaoxuya520/reverse-skill (MIT). The upstream router/bootstrap/MCP
instructions were removed; these tests keep the adapted copies from drifting
back into "commands that do not exist in this image".
"""

from __future__ import annotations

import pytest

from app.kernel import skills

NAMES = ("js-reverse", "protocol-reverse", "ghidra-reverse", "apk-reverse")


@pytest.fixture(scope="module")
def by_name():
    return skills.skills_by_name()


@pytest.mark.parametrize("name", NAMES)
def test_imported_skill_is_discovered_and_loadable(by_name, name: str) -> None:
    assert name in by_name
    text = skills.load_skill_text(name, skills=list(by_name.values()))
    assert text.lstrip().startswith("#")
    assert len(text) > 600


@pytest.mark.parametrize("name", NAMES)
def test_no_upstream_router_or_missing_tool_commands(by_name, name: str) -> None:
    text = skills.load_skill_text(name, skills=list(by_name.values()))
    for forbidden in ("master-route", "case-init", "bootstrap-reverse", "jshookmcp"):
        assert forbidden not in text, f"{name} 仍在调用上游路由/自举：{forbidden}"
    for forbidden in ("frida -U", "adb devices", "pm install"):
        assert forbidden not in text, f"{name} 在教镜像里不存在的移动端命令：{forbidden}"


def test_imported_skills_are_in_model_index() -> None:
    prompt = skills.render_index()
    assert all(f"`{name}`" in prompt for name in NAMES)
