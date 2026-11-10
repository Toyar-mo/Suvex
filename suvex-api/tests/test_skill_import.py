"""技能 zip 导入（v1.83）。"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.kernel import skill_import
from app.kernel.skill_store import SkillWriteError


def _zip(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_import_parses_frontmatter_and_attachments(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        skill_import, "create_or_update_skill", lambda **kwargs: captured.update(kwargs)
    )

    out = skill_import.import_zip(
        _zip(
            {
                "my-skill/SKILL.md": "---\nname: my-skill\ndescription: 示例\n---\n\n正文\n",
                "my-skill/scripts/a.sh": "echo hi\n",
            }
        )
    )

    assert out["name"] == "my-skill"
    assert captured["description"] == "示例"
    assert captured["files"] == {"scripts/a.sh": "echo hi\n"}


def test_reject_without_main_file() -> None:
    with pytest.raises(SkillWriteError):
        skill_import.import_zip(_zip({"a.txt": "x"}))


def test_reject_non_zip() -> None:
    with pytest.raises(SkillWriteError):
        skill_import.import_zip(b"not a zip")
