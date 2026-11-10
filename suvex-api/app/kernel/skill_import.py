"""技能 zip 导入（v1.83）。

对齐 ARTEX `skill_zip.go` 的边界：限制文件数/解压体积、拒绝路径穿越、
只接受文本附件；解压失败给出可读原因。技能正文与附件仍由
`skill_store.create_or_update_skill` 落盘（沿用它的原子替换/校验）。
"""

from __future__ import annotations

import io
import zipfile

from app.kernel.skill_store import SkillWriteError, create_or_update_skill

MAX_ZIP_BYTES = 5 * 1024 * 1024
MAX_FILES = 100
MAX_SINGLE_FILE = 1024 * 1024


def _text(raw: bytes, label: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillWriteError(
            f"`{label}` 不是 UTF-8 文本；技能导入只接受文本附件（二进制请走文件管理）。"
        ) from exc


def import_zip(data: bytes, *, category: str = "custom") -> dict[str, str]:
    if len(data) > MAX_ZIP_BYTES:
        raise SkillWriteError(f"压缩包太大（{len(data)} 字节，上限 {MAX_ZIP_BYTES}）。")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SkillWriteError("不是合法的 zip 压缩包。") from exc

    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if not infos:
            raise SkillWriteError("压缩包里没有文件。")
        if len(infos) > MAX_FILES:
            raise SkillWriteError(f"文件太多（{len(infos)} 个，上限 {MAX_FILES}）。")
        total = sum(info.file_size for info in infos)
        if total > MAX_ZIP_BYTES:
            raise SkillWriteError(f"解压后太大（{total} 字节，上限 {MAX_ZIP_BYTES}）。")

        mains = [i for i in infos if i.filename.replace("\\", "/").endswith("SKILL.md")]
        if len(mains) != 1:
            raise SkillWriteError("压缩包里必须正好有一个 `SKILL.md`。")
        main = mains[0]
        main_path = main.filename.replace("\\", "/")
        prefix = main_path[: -len("SKILL.md")].rstrip("/")

        from app.kernel.skills import _parse_frontmatter, _strip_frontmatter

        raw_main = _text(archive.read(main), main_path)
        meta = _parse_frontmatter(raw_main)
        body = _strip_frontmatter(raw_main)

        name = (meta.get("name") or prefix.split("/")[-1] or "").strip()
        if not name:
            raise SkillWriteError("技能名缺失：frontmatter 里没有 `name`，目录名也推不出。")
        description = (meta.get("description") or "").strip()

        files: dict[str, str] = {}
        for info in infos:
            path = info.filename.replace("\\", "/")
            if path == main_path or info.is_dir():
                continue
            if info.file_size > MAX_SINGLE_FILE:
                raise SkillWriteError(f"`{path}` 太大（上限 {MAX_SINGLE_FILE} 字节）。")
            rel = path[len(prefix) + 1 :] if prefix and path.startswith(prefix + "/") else path
            files[rel] = _text(archive.read(info), path)

        resolved_category = (meta.get("category") or category).strip() or "custom"
        create_or_update_skill(
            name=name,
            category=resolved_category,
            description=description,
            body=body,
            files=files,
        )
    return {"name": name, "category": resolved_category, "files": str(len(files))}


__all__ = ["import_zip"]
