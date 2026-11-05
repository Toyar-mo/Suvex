"""对话持久工作区（`/work/persist`）与下载端点（v1.36）。

## 这个模块守什么

用户实机遇到：模型在沙箱里写完报告，用户在平台上问"怎么下载"，而那个
沙箱已经被空闲回收 —— 文件随容器一起没了。修法是给每个会话在宿主上挂一个
工作区到 `/work/persist`，并加一个下载端点从**宿主**读文件。

这里钉三件事：
1. 挂载点字面量与 `app/kernel/runner.PERSIST_DIR` 一致（跨模块隐形契约）。
2. `resolve_file` 的安全边界：`..`、绝对路径、目录、符号链接逃逸、越界都拒。
3. HTTP 端点：能列、能下载，且**别人的会话 / 越界路径**拿不到。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.chat import workspaces as chat_workspaces


@pytest.fixture()
def ws_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把工作区根指到临时目录（`root()` 每次调用都重读环境变量）。"""
    target = tmp_path / "workspaces"
    monkeypatch.setenv("SUPEX_CHAT_WORKSPACES", str(target))
    return target


class TestMountAgreement:
    def test_mount_point_matches_kernel_persist_dir(self) -> None:
        """对话侧与内核侧的持久区路径必须是同一个字符串。

        两边是独立字面量（`app/chat/` 刻意不 import 昂贵的 `app/kernel`），
        只能靠测试钉住 —— 写错一个字符，容器里就多一个没人看的空目录。
        """
        from app.kernel.runner import PERSIST_DIR

        assert chat_workspaces.PERSIST_MOUNT_DIR == PERSIST_DIR

    def test_mount_point_is_under_work(self) -> None:
        assert chat_workspaces.PERSIST_MOUNT_DIR.startswith("/work/")


class TestEnsureWorkspace:
    def test_create_true_makes_directory(self, ws_root: Path) -> None:
        path = chat_workspaces.ensure_workspace("chat_abc", create=True)
        assert path is not None
        assert path.is_dir()
        assert path.parent == ws_root

    def test_create_false_returns_none_when_absent(self, ws_root: Path) -> None:
        assert chat_workspaces.ensure_workspace("chat_missing") is None


class TestListFiles:
    def test_lists_relative_posix_paths_sorted(self, ws_root: Path) -> None:
        base = chat_workspaces.ensure_workspace("chat_list", create=True)
        assert base is not None
        (base / "b.txt").write_text("b", encoding="utf-8")
        (base / "out").mkdir()
        (base / "out" / "a.md").write_text("a", encoding="utf-8")

        paths = [f.path for f in chat_workspaces.list_files("chat_list")]

        assert paths == ["b.txt", "out/a.md"], "相对路径、POSIX 分隔、按路径排序"

    def test_absent_workspace_lists_empty(self, ws_root: Path) -> None:
        assert chat_workspaces.list_files("chat_none") == []

    def test_list_files_sanitizes_non_utf8_filenames(self, ws_root: Path) -> None:
        """★ v1.68.3：文件名含非 UTF-8 字节时，list_files 必须返回**可 JSON
        序列化**的字符串，不能抛 UnicodeEncodeError（否则整个「文件管理」页 500）。

        实测（2026-11-05）：某个 CTF 产物文件名是 GBK 乱码字节，被
        `surrogateescape` 映射成孤立代理项，写 JSON 时炸。这条在底层
        `_safe_display` 上直接验证（不依赖在某个 OS 上能否造出这种文件名）。
        """
        import json

        # 孤立代理项（surrogateescape 会把 b"\xff\xfe" 映射成这种）
        raw = "x_" + b"\xff\xfe".decode("utf-8", "surrogateescape") + ".txt"
        safe = chat_workspaces._safe_display(raw)
        # 关键：能 utf-8 无损编码成 JSON（不抛 surrogates not allowed）
        json.dumps({"path": safe, "name": safe})
        # 且可读（坏字节变成替换符），不残留代理项
        assert "\udcff" not in safe
        assert "\ufffd" in safe  # 替换符 U+FFFD

    @pytest.mark.skipif(
        os.name == "nt",
        reason="Windows 无法创建含非 UTF-8 字节的文件名，这条在 Linux 上跑",
    )
    def test_resolve_file_matches_sanitized_display_path(self, ws_root: Path) -> None:
        """★ v1.68.3：前端把列表给出的**显示名**（带 `�`）带回来下载/删除时，
        `resolve_file` 应能反查到真实文件，而不是 404。（Linux-only：需真实坏字节文件名。）
        """
        base = chat_workspaces.ensure_workspace("chat_res", create=True)
        assert base is not None
        bad_name = "y_" + os.fsdecode(b"\xff\xfe") + ".bin"
        (base / bad_name).write_bytes(b"payload")

        files = chat_workspaces.list_files("chat_res")
        display_path = files[0].path  # 已 sanitize（含 �）
        resolved = chat_workspaces.resolve_file("chat_res", display_path)
        assert resolved is not None, "显示名必须能反查回真实文件"
        assert resolved.read_bytes() == b"payload"


class TestResolveFileSecurity:
    def test_accepts_plain_and_nested_file(self, ws_root: Path) -> None:
        base = chat_workspaces.ensure_workspace("chat_ok", create=True)
        assert base is not None
        (base / "report.md").write_text("hi", encoding="utf-8")
        (base / "out").mkdir()
        (base / "out" / "flag.txt").write_text("flag", encoding="utf-8")

        assert chat_workspaces.resolve_file("chat_ok", "report.md") is not None
        assert chat_workspaces.resolve_file("chat_ok", "out/flag.txt") is not None

    @pytest.mark.parametrize(
        "bad",
        ["", ".", "..", "../etc/passwd", "out/../../x", "/etc/passwd", "a/../../b", "C:evil"],
    )
    def test_rejects_traversal_and_absolute(self, ws_root: Path, bad: str) -> None:
        base = chat_workspaces.ensure_workspace("chat_bad", create=True)
        assert base is not None
        (base / "out").mkdir()
        assert chat_workspaces.resolve_file("chat_bad", bad) is None

    def test_rejects_directory(self, ws_root: Path) -> None:
        base = chat_workspaces.ensure_workspace("chat_dir", create=True)
        assert base is not None
        (base / "sub").mkdir()
        assert chat_workspaces.resolve_file("chat_dir", "sub") is None

    def test_rejects_missing(self, ws_root: Path) -> None:
        chat_workspaces.ensure_workspace("chat_miss2", create=True)
        assert chat_workspaces.resolve_file("chat_miss2", "nope.txt") is None

    @pytest.mark.skipif(os.name == "nt", reason="Windows 建符号链接需要特权")
    def test_rejects_symlink_escape(self, ws_root: Path, tmp_path: Path) -> None:
        base = chat_workspaces.ensure_workspace("chat_link", create=True)
        assert base is not None
        secret = tmp_path / "secret.txt"
        secret.write_text("top secret", encoding="utf-8")
        (base / "leak").symlink_to(secret)

        assert chat_workspaces.resolve_file("chat_link", "leak") is None


class TestDropWorkspace:
    def test_removes_directory(self, ws_root: Path) -> None:
        base = chat_workspaces.ensure_workspace("chat_drop", create=True)
        assert base is not None
        (base / "x.txt").write_text("x", encoding="utf-8")

        chat_workspaces.drop_workspace("chat_drop")

        assert not base.exists()


class TestDownloadEndpoint:
    def test_list_and_download_a_workspace_file(self, ready_client: TestClient, ws_root: Path) -> None:
        created = ready_client.post("/api/v1/chat/sessions", json={"title": "产物"})
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]

        base = chat_workspaces.ensure_workspace(session_id, create=True)
        assert base is not None
        (base / "report.md").write_bytes("这是报告正文".encode())

        listed = ready_client.get(f"/api/v1/chat/sessions/{session_id}/files")
        assert listed.status_code == 200, listed.text
        rows = listed.json()
        assert [row["path"] for row in rows] == ["report.md"]
        assert rows[0]["name"] == "report.md"

        got = ready_client.get(
            f"/api/v1/chat/sessions/{session_id}/files/download",
            params={"path": "report.md"},
        )
        assert got.status_code == 200, got.text
        assert got.content == "这是报告正文".encode()

    def test_download_rejects_traversal(self, ready_client: TestClient, ws_root: Path) -> None:
        created = ready_client.post("/api/v1/chat/sessions", json={"title": "越界"})
        session_id = created.json()["id"]
        chat_workspaces.ensure_workspace(session_id, create=True)

        got = ready_client.get(
            f"/api/v1/chat/sessions/{session_id}/files/download",
            params={"path": "../../../etc/passwd"},
        )
        assert got.status_code == 404, got.text

    def test_other_users_workspace_is_not_found(self, viewer_client: TestClient, ws_root: Path) -> None:
        """会话归属由 `created_by` 过滤：访问不存在的会话一律 404。"""
        got = viewer_client.get("/api/v1/chat/sessions/chat_does_not_exist/files")
        assert got.status_code == 404, got.text
