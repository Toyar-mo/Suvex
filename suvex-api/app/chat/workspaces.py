"""对话沙箱的**持久工作区**（v1.36）。

## 这个模块解决什么问题

对话沙箱里的 `run_shell` 能写文件，但 `/work` 是**容器的**：
容器一被回收（空闲 30 分钟、归档、进程重启），里面写出来的东西就没了。
用户上一轮辛苦跑出来的报告、脚本、证据、截图，下一轮想下载时已经
不存在 —— 这是 2026-10-30 实机遇到的真实场景（模型写完 CTF 报告，
用户问"我在平台怎么下载"，而那个沙箱已经被空闲回收）。

解决方式是给每个会话在**宿主**上建一个目录，以 `:rw` 挂进容器的
`/work/persist`（`app/kernel/runner.py` 里 `workspace_dir` 的既有能力，
解题侧早就在用）。这样：

* 写进 `/work/persist/` 的产物**不随容器生灭** —— 容器回收后仍在宿主；
* 平台可以**直接读宿主目录**把产物下载给用户（不需要 docker exec）；
* 会话归档/彻底删除时，连同这个目录一起删（不留"删了会话文件还在"）。

## 为什么是子目录 `/work/persist` 而不是 `/work` 本身

`/work` 在受限档是 tmpfs、在无限制档是容器可写层；把它整个换成宿主
挂载会让 `/work/attachments` 那个子挂载失效（Docker 按路径深度挂载，
见 `runner.py` 里 `_argv` 那段说明）。挂到 `/work/persist` 则两不相干。

## 与 `attachments` 的关系

`attachments` 管"用户 → 沙箱"（只读挂到 `/work/attachments`）；
这个模块管"沙箱 → 用户"（读写挂到 `/work/persist`）。两棵树，各自
在会话归档时清理。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: 沙箱里持久区挂载点。提示词里也写这个字符串 —— 两处必须一致。
PERSIST_MOUNT_DIR = "/work/persist"

#: 单次下载的字节上限。产物可能是数据集/镜像，给宽一点；再大就让用户
#: 自己想办法（平台不是网盘）。
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024

#: 列表里最多返回多少个文件。防止一个会话里堆了几万个文件把响应撑爆。
MAX_LISTED_FILES = 5000

_ROOT_ENV = "SUPEX_CHAT_WORKSPACES"


def root() -> Path:
    """工作区根目录。

    默认在系统临时目录下，可用 `SUPEX_CHAT_WORKSPACES` 覆盖。
    ★ 为什么用临时目录而不是仓库里的 `uploads/`：对话产物是会话级
      耗材，与扫描附件（长期保留）分开，清理规则才不会互相纠缠。
    """
    raw = os.environ.get(_ROOT_ENV, "").strip()
    if raw:
        return Path(raw)
    return Path(tempfile.gettempdir()) / "suvex-chat-workspaces"


def _session_dir(session_id: str) -> Path:
    # ★ `session_id` 来自 URL。与 `attachments._session_dir` 用同一套
    #   白名单：路径拼接是"上游一旦有 bug 就变任意文件读写"的位置。
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
    return root() / safe


def ensure_workspace(session_id: str, *, create: bool = False) -> Path | None:
    """拿到会话工作区的宿主路径；`create=True` 时建出来。

    返回 `None` 表示目录不存在（且没要求创建）。`sandbox._create_container`
    需要"先建再挂"，理由与交接目录相同：挂一个不存在的宿主路径，Docker
    会替你建一个空目录，而"空目录"和"没挂上"在容器里长得一模一样。
    """
    directory = _session_dir(session_id)
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory if directory.is_dir() else None


@dataclass(frozen=True)
class WorkspaceFile:
    """工作区里的一个文件（相对路径，POSIX 分隔符）。"""

    path: str
    name: str
    size_bytes: int
    modified_at: float


def list_files(session_id: str) -> list[WorkspaceFile]:
    """列出这个会话工作区里的所有**普通文件**（递归）。

    按相对路径排序，让前端展示稳定。跳过目录与符号链接（符号链接可能
    指到工作区外，下载它会变成一个越权读取通道）。
    """
    directory = _session_dir(session_id)
    if not directory.is_dir():
        return []
    out: list[WorkspaceFile] = []
    base = directory.resolve()
    for path in sorted(directory.rglob("*")):
        if len(out) >= MAX_LISTED_FILES:
            break
        try:
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
        except OSError:
            continue
        # 双保险：即使有符号链接混进来，也不列工作区外的目标。
        if base != resolved and base not in resolved.parents:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append(
            WorkspaceFile(
                path=path.relative_to(directory).as_posix(),
                name=path.name,
                size_bytes=stat.st_size,
                modified_at=stat.st_mtime,
            )
        )
    return out


def resolve_file(session_id: str, relpath: str) -> Path | None:
    """把外部给的相对路径解析成工作区内的**真实文件**，否则 `None`。

    这是下载端点的安全边界：
    * 拒绝绝对路径、拒绝 `..`（目录穿越）；
    * `resolve()` 之后再确认结果仍在工作区内（挡住符号链接逃逸）；
    * 只接受**普通文件**（不返回目录、设备、fifo）；
    * 超过 `MAX_DOWNLOAD_BYTES` 的也拒（避免一个请求拖垮进程内存）。
    """
    raw = (relpath or "").replace("\\", "/").strip()
    if not raw:
        return None
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    # 拒绝盘符/ADS 之类带冒号的段（Windows 上 "C:x" 会绕成另一条盘符路径）。
    if any(":" in part for part in parts):
        return None

    directory = _session_dir(session_id)
    if not directory.is_dir():
        return None
    base = directory.resolve()
    try:
        target = directory.joinpath(*parts).resolve()
    except OSError:
        return None
    if target != base and base not in target.parents:
        return None
    try:
        if not target.is_file() or target.is_symlink():
            return None
        if target.stat().st_size > MAX_DOWNLOAD_BYTES:
            return None
    except OSError:
        return None
    return target


def drop_workspace(session_id: str) -> None:
    """删掉这个会话的工作区目录（归档/彻底删除时调用）。"""
    directory = _session_dir(session_id)
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)


__all__ = [
    "MAX_DOWNLOAD_BYTES",
    "MAX_LISTED_FILES",
    "PERSIST_MOUNT_DIR",
    "WorkspaceFile",
    "drop_workspace",
    "ensure_workspace",
    "list_files",
    "resolve_file",
    "root",
]
