"""「文件管理」页的跨会话文件端点（v1.37）。

## 这个模块守什么

v1.36 让单个会话的产物能从宿主工作区下载；v1.37 加了一个**跨会话**的
清单页（照 CyberStrikeAI 的文件管理版式）。这里钉四件事：

1. 列表能跨会话把文件带出来，且带对会话身份（id / title / project）。
2. 三个过滤（sessionId / name / source）在服务端生效。
3. 删除只删工作区里的真实文件，且越界/他人会话一律拒。
4. 列表只含**未归档**会话（归档的目录早被 `drop_workspace` 删了）。
"""

from __future__ import annotations

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


def _session(client: TestClient, title: str) -> str:
    created = client.post("/api/v1/chat/sessions", json={"title": title})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _write(session_id: str, relpath: str, content: bytes = b"x") -> None:
    base = chat_workspaces.ensure_workspace(session_id, create=True)
    assert base is not None
    target = base / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


class TestListAllFiles:
    def test_lists_files_across_sessions(self, ready_client: TestClient, ws_root: Path) -> None:
        a = _session(ready_client, "会话甲")
        b = _session(ready_client, "会话乙")
        _write(a, "report.md", b"ra")
        _write(b, "out/flag.txt", b"rb")

        rows = ready_client.get("/api/v1/chat/files")
        assert rows.status_code == 200, rows.text
        data = rows.json()["items"]
        got = {(row["sessionId"], row["path"]) for row in data}
        assert got == {(a, "report.md"), (b, "out/flag.txt")}
        # 每条都带会话身份，前端表格才有「会话」列可显示。
        titles = {row["sessionId"]: row["sessionTitle"] for row in data}
        assert titles[a] == "会话甲" and titles[b] == "会话乙"
        assert all(row["source"] == "workspace" for row in data)

    def test_truncation_is_reported_not_silent(
        self, ready_client: TestClient, ws_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 超过上限时 `truncated=true` + `limit`，不能静默截断。

        原来响应是裸数组，截断标记算完就丢了 —— 界面把 5000 当成
        "一共就这些"，用户会以为旧产物丢了。
        """
        monkeypatch.setattr(chat_workspaces, "MAX_LISTED_FILES", 1)
        session = _session(ready_client, "多文件")
        _write(session, "a.txt")
        _write(session, "b.txt")

        body = ready_client.get("/api/v1/chat/files").json()

        assert body["truncated"] is True
        assert body["limit"] == 1
        assert len(body["items"]) == 1

    def test_filters_by_session_id(self, ready_client: TestClient, ws_root: Path) -> None:
        a = _session(ready_client, "甲")
        b = _session(ready_client, "乙")
        _write(a, "a.txt")
        _write(b, "b.txt")

        rows = ready_client.get("/api/v1/chat/files", params={"sessionId": a}).json()["items"]
        assert [row["path"] for row in rows] == ["a.txt"]

    def test_filters_by_name_substring(self, ready_client: TestClient, ws_root: Path) -> None:
        session = _session(ready_client, "s")
        _write(session, "report-final.md")
        _write(session, "notes.md")

        rows = ready_client.get("/api/v1/chat/files", params={"name": "REPORT"}).json()["items"]
        assert [row["path"] for row in rows] == ["report-final.md"]

    def test_rejects_unknown_source(self, ready_client: TestClient, ws_root: Path) -> None:
        got = ready_client.get("/api/v1/chat/files", params={"source": "nope"})
        assert got.status_code == 400, got.text

    def test_archived_session_files_disappear(
        self, ready_client: TestClient, ws_root: Path
    ) -> None:
        session = _session(ready_client, "将被归档")
        _write(session, "keep.txt")
        assert len(ready_client.get("/api/v1/chat/files").json()["items"]) == 1

        archived = ready_client.delete(f"/api/v1/chat/sessions/{session}")
        assert archived.status_code == 200, archived.text

        assert ready_client.get("/api/v1/chat/files").json()["items"] == []


class TestDeleteFile:
    def test_deletes_a_file_and_drops_it_from_list(
        self, ready_client: TestClient, ws_root: Path
    ) -> None:
        session = _session(ready_client, "删除")
        _write(session, "tmp.txt")

        got = ready_client.delete(
            f"/api/v1/chat/sessions/{session}/files", params={"path": "tmp.txt"}
        )
        assert got.status_code == 204, got.text
        assert ready_client.get("/api/v1/chat/files").json()["items"] == []

    def test_delete_missing_is_404(self, ready_client: TestClient, ws_root: Path) -> None:
        session = _session(ready_client, "缺")
        chat_workspaces.ensure_workspace(session, create=True)
        got = ready_client.delete(
            f"/api/v1/chat/sessions/{session}/files", params={"path": "nope.txt"}
        )
        assert got.status_code == 404, got.text

    def test_delete_rejects_traversal(self, ready_client: TestClient, ws_root: Path) -> None:
        session = _session(ready_client, "越界")
        chat_workspaces.ensure_workspace(session, create=True)
        got = ready_client.delete(
            f"/api/v1/chat/sessions/{session}/files",
            params={"path": "../../../etc/passwd"},
        )
        assert got.status_code == 404, got.text

    def test_other_users_session_is_not_found(
        self, viewer_client: TestClient, ws_root: Path
    ) -> None:
        got = viewer_client.delete(
            "/api/v1/chat/sessions/chat_does_not_exist/files", params={"path": "a.txt"}
        )
        assert got.status_code == 404, got.text


class TestSaveFile:
    """工作区写入（v1.38 上传的底层函数）。"""

    def test_writes_into_uploads_dir(self, ws_root: Path) -> None:
        saved = chat_workspaces.save_file("chat_x", "notes.txt", b"hello")
        assert saved.path == "uploads/notes.txt"
        assert saved.size_bytes == 5
        base = chat_workspaces.ensure_workspace("chat_x")
        assert base is not None
        assert (base / "uploads" / "notes.txt").read_bytes() == b"hello"

    def test_sanitizes_path_separators(self, ws_root: Path) -> None:
        # 浏览器给的 filename 不可信：`../../etc/passwd` 不能逃出工作区。
        saved = chat_workspaces.save_file("chat_x", "../../etc/passwd", b"x")
        assert saved.path.startswith("uploads/")
        # 路径分隔符被压平：".." 只是文件名里的两个点，不再是一段父目录。
        assert "/" not in saved.path[len("uploads/"):]
        assert saved.path.rsplit("/", 1)[-1].endswith("etc_passwd")
        lists = [item.path for item in chat_workspaces.list_files("chat_x")]
        assert lists == [saved.path]
        # 真正的检验：文件确实落在 uploads/ 下，没有跑到工作区外。
        base = chat_workspaces.ensure_workspace("chat_x")
        assert base is not None
        assert (base / saved.path).is_file()

    def test_does_not_overwrite_same_name(self, ws_root: Path) -> None:
        first = chat_workspaces.save_file("chat_x", "a.txt", b"one")
        second = chat_workspaces.save_file("chat_x", "a.txt", b"two")
        assert first.path != second.path
        base = chat_workspaces.ensure_workspace("chat_x")
        assert base is not None
        assert (base / "uploads" / "a.txt").read_bytes() == b"one"
        assert (base / second.path).read_bytes() == b"two"

    def test_rejects_empty(self, ws_root: Path) -> None:
        with pytest.raises(ValueError):
            chat_workspaces.save_file("chat_x", "empty.txt", b"")

    def test_rejects_oversize(self, ws_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chat_workspaces, "MAX_DOWNLOAD_BYTES", 4)
        with pytest.raises(ValueError):
            chat_workspaces.save_file("chat_x", "big.bin", b"12345")


class TestUploadEndpoint:
    def test_upload_shows_up_in_lists_and_is_downloadable(
        self, ready_client: TestClient, ws_root: Path
    ) -> None:
        session = _session(ready_client, "上传")
        payload = "# 结论\n".encode()

        got = ready_client.post(
            f"/api/v1/chat/sessions/{session}/files",
            files={"file": ("报告.md", payload, "text/markdown")},
        )
        assert got.status_code == 201, got.text
        body = got.json()
        assert body["path"] == "uploads/报告.md"
        assert body["sizeBytes"] == len(payload)

        # 会话级列表和跨会话列表都能看到它。
        per_session = ready_client.get(f"/api/v1/chat/sessions/{session}/files").json()
        assert [row["path"] for row in per_session] == ["uploads/报告.md"]
        cross = ready_client.get("/api/v1/chat/files").json()["items"]
        assert [row["path"] for row in cross] == ["uploads/报告.md"]

        # 下载回来字节一致。
        dl = ready_client.get(
            f"/api/v1/chat/sessions/{session}/files/download",
            params={"path": "uploads/报告.md"},
        )
        assert dl.status_code == 200, dl.text
        assert dl.content == payload

    def test_upload_empty_file_is_400(
        self, ready_client: TestClient, ws_root: Path
    ) -> None:
        session = _session(ready_client, "空文件")
        got = ready_client.post(
            f"/api/v1/chat/sessions/{session}/files",
            files={"file": ("empty.txt", b"", "text/plain")},
        )
        assert got.status_code == 400, got.text

    def test_upload_to_archived_session_is_400(
        self, ready_client: TestClient, ws_root: Path
    ) -> None:
        session = _session(ready_client, "归档后")
        assert ready_client.delete(f"/api/v1/chat/sessions/{session}").status_code == 200

        got = ready_client.post(
            f"/api/v1/chat/sessions/{session}/files",
            files={"file": ("a.txt", b"x", "text/plain")},
        )
        assert got.status_code == 400, got.text

    def test_upload_to_other_users_session_is_404(
        self, viewer_client: TestClient, ws_root: Path
    ) -> None:
        got = viewer_client.post(
            "/api/v1/chat/sessions/chat_does_not_exist/files",
            files={"file": ("a.txt", b"x", "text/plain")},
        )
        assert got.status_code == 404, got.text
