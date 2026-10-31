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
        data = rows.json()
        got = {(row["sessionId"], row["path"]) for row in data}
        assert got == {(a, "report.md"), (b, "out/flag.txt")}
        # 每条都带会话身份，前端表格才有「会话」列可显示。
        titles = {row["sessionId"]: row["sessionTitle"] for row in data}
        assert titles[a] == "会话甲" and titles[b] == "会话乙"
        assert all(row["source"] == "workspace" for row in data)

    def test_filters_by_session_id(self, ready_client: TestClient, ws_root: Path) -> None:
        a = _session(ready_client, "甲")
        b = _session(ready_client, "乙")
        _write(a, "a.txt")
        _write(b, "b.txt")

        rows = ready_client.get("/api/v1/chat/files", params={"sessionId": a}).json()
        assert [row["path"] for row in rows] == ["a.txt"]

    def test_filters_by_name_substring(self, ready_client: TestClient, ws_root: Path) -> None:
        session = _session(ready_client, "s")
        _write(session, "report-final.md")
        _write(session, "notes.md")

        rows = ready_client.get("/api/v1/chat/files", params={"name": "REPORT"}).json()
        assert [row["path"] for row in rows] == ["report-final.md"]

    def test_rejects_unknown_source(self, ready_client: TestClient, ws_root: Path) -> None:
        got = ready_client.get("/api/v1/chat/files", params={"source": "nope"})
        assert got.status_code == 400, got.text

    def test_archived_session_files_disappear(
        self, ready_client: TestClient, ws_root: Path
    ) -> None:
        session = _session(ready_client, "将被归档")
        _write(session, "keep.txt")
        assert len(ready_client.get("/api/v1/chat/files").json()) == 1

        archived = ready_client.delete(f"/api/v1/chat/sessions/{session}")
        assert archived.status_code == 200, archived.text

        assert ready_client.get("/api/v1/chat/files").json() == []


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
        assert ready_client.get("/api/v1/chat/files").json() == []

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
