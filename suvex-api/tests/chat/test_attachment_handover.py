"""对话附件「交接给沙箱」这条链路的测试。

## 这条链路为什么需要测试

它有一条**跨模块的字面量约定**，而那条约定错了不会报警，只会表现为
一句模型说的话："`/work/attachments/` 是空的，全盘 find 也搜不到"。

三个模块各持一段：

| 谁 | 持有什么 |
|---|---|
| `app/chat/attachments.py` | `HANDOVER_MOUNT_DIR = "/work/attachments"`（**容器内**路径） |
| `app/kernel/runner.py` | `ATTACHMENT_DIR = "/work/attachments"`（挂载点）+ `_argv` 里 `-v <host>:<ATTACHMENT_DIR>:ro` |
| `app/chat/sandbox.py` | 把 `handed_over_dir(session_id)` 传给 `SandboxContainer(attachments_dir=...)` |

只要有一处写成 `/work/attachment`（少个 s）或者 `:ro` 被删掉，功能就静默
失效：文件在容器里看不到，而**所有单元测试都还是绿的** —— 因为每个模块
自己内部是自洽的。

这就是那个原始缺陷的形状（v0.108 之前）：`read_text` 读完就 `discard()`，
附件从来没进过沙箱，而 `sandbox.py` 压根没传 `attachments_dir`。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.chat import attachments as chat_attachments


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把附件根目录指到临时目录。

    ★ 必须设环境变量而不是改模块常量：`_root()` 每次调用都重读
      `SUPEX_CHAT_ATTACHMENTS`，所以设了之后连已 import 的模块也生效。
    """
    target = tmp_path / "attachments"
    monkeypatch.setenv("SUPEX_CHAT_ATTACHMENTS", str(target))
    return target


def _store(root: Path, session_id: str, filename: str, content: bytes) -> str:
    """落一个暂存附件，返回它的 id。"""
    import uuid

    ident = uuid.uuid4().hex[:8]
    chat_attachments.store(
        session_id=session_id,
        filename=filename,
        content=content,
        attachment_id=ident,
    )
    return ident


class TestMountPathAgreement:
    """三处字面量必须一致 —— 这是整条链路唯一的"隐形契约"。"""

    def test_chat_and_kernel_agree_on_mount_point(self) -> None:
        """对话侧和内核侧的挂载点必须是同一个字符串。

        为什么这条值得单独一条测试：两边各自都能跑，谁也不会 import 谁
        （`app/kernel/runner.py` 的 import 成本极高，`app/chat/` 刻意不依赖它）。
        于是它们是**两个独立字面量**，只能靠测试钉住。
        """
        from app.kernel.runner import ATTACHMENT_DIR

        assert chat_attachments.HANDOVER_MOUNT_DIR == ATTACHMENT_DIR

    def test_mount_point_is_under_work(self) -> None:
        """挂载点必须在 `/work` 下。

        `/work` 是沙箱里唯一可写的工作区，模型的所有相对路径都从那儿出发。
        挂到 `/attachments` 会让它在两个"根"之间来回切，实测表现是模型
        一会儿写 `/work/x` 一会儿写 `x`，然后说"文件不见了"。
        """
        assert chat_attachments.HANDOVER_MOUNT_DIR.startswith("/work/")


class TestHandOver:
    """`hand_over()` 把暂存区的文件搬进交接区。"""

    def test_moves_file_and_returns_container_path(self, root: Path) -> None:
        sid = "chat_test0001"
        ident = _store(root, sid, "capture.pcapng", b"\x0a\x0d\x0d\x0a" + b"\x00" * 64)

        handed = chat_attachments.hand_over(session_id=sid, attachment_ids=[ident])

        assert len(handed) == 1
        name, container_path = handed[0]
        # 显示名是用户的原文件名（去掉 id 前缀），不是 `attachment_1`
        assert name == "capture.pcapng"
        # 容器内路径以挂载点为前缀，后面跟**带 id 的真实文件名**
        assert container_path.startswith(chat_attachments.HANDOVER_MOUNT_DIR + "/")
        assert container_path.endswith(".pcapng")

    def test_file_actually_leaves_staging(self, root: Path) -> None:
        """搬走，不是复制 —— 否则暂存区会一路撞 `MAX_STAGED_PER_SESSION`。"""
        sid = "chat_test0002"
        ident = _store(root, sid, "a.log", b"hello")

        chat_attachments.hand_over(session_id=sid, attachment_ids=[ident])

        staged = chat_attachments.list_staged(session_id=sid)
        assert [s.id for s in staged] == [], "交接之后暂存区应该是空的"

    def test_bytes_survive_the_move(self, root: Path) -> None:
        """搬完之后字节必须一模一样。

        这条看着像废话，但它防的是「用文本模式读写」这类事故 ——
        一个 pcapng 如果中间被当 UTF-8 解码再过一遍，长度就对不上了，
        而模型只会看到 tcpdump 报 "truncated dump file"。
        """
        sid = "chat_test0003"
        payload = bytes(range(256)) * 4
        ident = _store(root, sid, "bin.dat", payload)

        handed = chat_attachments.hand_over(session_id=sid, attachment_ids=[ident])

        container_path = handed[0][1]
        on_disk = root / "_handed_over" / "chat_test0003" / Path(container_path).name
        assert on_disk.is_file()
        assert on_disk.read_bytes() == payload

    def test_missing_id_is_skipped_not_raised(self, root: Path) -> None:
        """找不到的 id 跳过。

        与 `read_text` 同一条理由：前端开两个标签页时另一个可能已经用掉了。
        整轮请求因为少一个附件而 400，比少一个附件糟得多。
        """
        sid = "chat_test0004"
        handed = chat_attachments.hand_over(
            session_id=sid, attachment_ids=["deadbeef"]
        )
        assert handed == []

    def test_repeated_upload_of_same_name_does_not_collide(self, root: Path) -> None:
        """同名文件传两次，两份都要留下。

        `os.replace` 会让后一份覆盖前一份 —— 但真实文件名带 id 前缀
        （`<id>-capture.pcapng`），所以两份是不同的路径。这条测试钉住
        那个前缀确实进了文件名，否则第二次上传会静默吞掉第一次的文件。
        """
        sid = "chat_test0005"
        first = _store(root, sid, "same.pcapng", b"first")
        second = _store(root, sid, "same.pcapng", b"second")

        handed = chat_attachments.hand_over(
            session_id=sid, attachment_ids=[first, second]
        )

        assert len(handed) == 2
        names = {Path(p).name for _, p in handed}
        assert len(names) == 2, "两份同名附件必须落在两个不同路径上"


class TestHandedOverDir:
    """交接目录的定位与清理。"""

    def test_dir_is_session_scoped_and_absent_before_first_handover(
        self, root: Path
    ) -> None:
        """没发过带附件的消息时目录不存在。

        ⚠️ 这条不是洁癖：`SandboxContainer._argv` 只在
           `attachments_dir.is_dir()` 为真时才挂载。如果这里返回的路径
           总是存在（哪怕是空目录），Docker 就会挂一个空目录进去，而
           "空目录"与"没挂上"在容器里长得一模一样 —— 排查时完全分不清。
        """
        directory = chat_attachments.handed_over_dir("chat_never_used")
        assert not directory.is_dir()

    def test_drop_removes_only_that_session(self, root: Path) -> None:
        """清理必须按会话隔离 —— 删别人的附件是数据丢失。"""
        a, b = "chat_test0006", "chat_test0007"
        _store(root, a, "x.log", b"a")
        _store(root, b, "y.log", b"b")
        chat_attachments.hand_over(
            session_id=a, attachment_ids=[s.id for s in chat_attachments.list_staged(session_id=a)]
        )
        chat_attachments.hand_over(
            session_id=b, attachment_ids=[s.id for s in chat_attachments.list_staged(session_id=b)]
        )

        chat_attachments.drop_handed_over(session_id=a)

        assert not chat_attachments.handed_over_dir(a).is_dir()
        assert chat_attachments.handed_over_dir(b).is_dir(), "b 的交接目录不该被删"

    def test_drop_is_idempotent(self, root: Path) -> None:
        """重复清理不抛 —— 归档可能被点两次。"""
        chat_attachments.drop_handed_over(session_id="chat_test0008")
        chat_attachments.drop_handed_over(session_id="chat_test0008")


class TestSandboxWiring:
    """`app/chat/sandbox.py` 必须把交接目录传给容器。"""

    def test_new_sandbox_passes_handed_over_dir(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`_new_sandbox()` 必须把 `attachments_dir` 指到交接目录。

        ★ 这条测的是那个**原始缺陷本身**：v0.108 之前 `sandbox.py` 压根
          没传 `attachments_dir`，于是 `_argv` 里那段 `-v ...:ro` 从不执行，
          文件留在宿主上、容器里什么都没有。而所有测试都是绿的。

        这里不真起容器（docker 在 CI 上未必有），只截获 `SandboxContainer`
        的构造参数 —— 因为要断言的就是"有没有传"，不是"docker 起没起"。
        """
        from app.chat import sandbox as chat_sandbox
        from app.kernel import runner as kernel_runner

        captured: dict[str, object] = {}

        class _FakeContainer:
            def __init__(self, *args: object, **kwargs: object) -> None:
                captured.update(kwargs)
                self.name = "fake"

        # ⚠️ 要 patch 的是 `app.kernel.runner.SandboxContainer`，**不是**
        #   `chat_sandbox.SandboxContainer` —— `_new_sandbox` 里用的是
        #   `runner.SandboxContainer`（函数内延迟 import 进来的那个模块对象）。
        #   我第一版 patch 错了目标，测试静默通过（因为真起了一个容器？不，
        #   是 docker 不在时会抛 —— 总之断言没跑在想要的对象上）。
        monkeypatch.setattr(kernel_runner, "SandboxContainer", _FakeContainer)
        session = type("S", (), {"id": "chat_wiring01"})()

        chat_sandbox._new_sandbox(session)  # noqa: SLF001 - 要测的就是这个工厂

        assert "attachments_dir" in captured, "必须把 attachments_dir 传给容器"
        passed = Path(str(captured["attachments_dir"]))
        assert passed == chat_attachments.handed_over_dir("chat_wiring01")

    def test_read_only_mount_flag_is_present(self) -> None:
        """挂载必须是 `:ro`。

        可写挂载意味着容器能改用户上传的原始文件 —— 一份 pcapng 被
        `tcpdump -r` 之外的命令改写之后，"分析结果"就失去证据价值了。
        """
        import inspect

        from app.kernel import runner as kernel_runner

        source = inspect.getsource(kernel_runner.SandboxContainer._argv)
        assert ":ro" in source, "附件目录必须以只读方式挂载"
