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


def _safe_display(value: str) -> str:
    """把文件名/相对路径里**非 UTF-8 的字节**转成可安全 JSON 序列化的字符串。

    ## 为什么需要

    实测（2026-11-05 用户「文件管理」页 500）：某个会话产物文件名是 GBK/乱码
    字节（CTF 题里解出来的 `附件\\enc.py` 之类），`os.scandir`/`Path.rglob`
    用 `surrogateescape` 把不可解码的字节映射成**孤立代理项**（U+DC80–U+DCFF）
    塞进 Python str。这个 str 写进 JSON 时 `encode('utf-8')` 会抛
    `UnicodeEncodeError: surrogates not allowed` —— 整个文件列表接口 500，
    一个坏文件名拖垮整页。

    修法：`encode('utf-8', 'surrogateescape')` 把代理项还原成原始字节，再
    `decode('utf-8', 'replace')` 得到带 `�` 的合法字符串。展示层拿到的是
    "看起来对"的名字；下载走的是 `path` 原始字节（resolve 时不受影响，因为
    文件系统层面仍是原字节）。
    """
    return value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


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
                path=_safe_display(path.relative_to(directory).as_posix()),
                name=_safe_display(path.name),
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
        target = None
    if target is not None and (target == base or base in target.parents):
        try:
            if target.is_file() and not target.is_symlink():
                if target.stat().st_size > MAX_DOWNLOAD_BYTES:
                    return None
                return target
        except OSError:
            pass
    # ★ 回退：文件名含非 UTF-8 字节时，列表接口把它显示成带 `�` 的名字
    #   （见 `_safe_display`）；前端把那个显示名原样带回来下载/删除时，
    #   直接 joinpath 对不上真实字节。这里按**显示名**在 `list_files`
    #   结果里反查真实路径 —— 否则这类文件在界面上点下载/删除都会 404。
    for item in list_files(session_id):
        if item.path == raw:
            real = directory.joinpath(*item.path.split("/"))
            try:
                real_r = real.resolve()
                if (real_r == base or base in real_r.parents) and real_r.is_file():
                    if real_r.stat().st_size > MAX_DOWNLOAD_BYTES:
                        return None
                    return real_r
            except OSError:
                return None
    return None


@dataclass(frozen=True)
class SavedFile:
    """一个刚写进工作区的文件。"""

    path: str
    name: str
    size_bytes: int


def save_file(session_id: str, filename: str, content: bytes) -> SavedFile:
    """把一个上传的字节流写进工作区（v1.38「文件管理」页的上传）。

    ## 与 `attachments.store` 的区别

    `attachments` 是"用户 → 模型，一次性耗材"（1 小时 TTL、发消息即消费）；
    这里写的是**产物**：不设 TTL、随会话归档/删除才清、在「文件管理」页
    与会话工作区里都能看到和下载。所以不能复用 `attachments`。

    ## 落在哪

    `/work/persist/uploads/`（宿主侧 `uploads/`）。刻意放 `uploads/` 子目录：
    * 与模型自己写在 `reports/`、`scripts/` 下的产物分开，谁是谁一目了然；
    * 万一同名，``<stem>-<8位hex>.<ext>`` 追加后缀，不覆盖模型已有文件
      （`resolve_file` 的名字对用户可见，静默覆盖是数据丢失）。

    ## 安全

    * 文件名过 `attachments.sanitize_name`（同一套白名单：去 `/` `\\`、
      去控制字符、去前导点、长度截断），**不信任**浏览器给的 `filename`；
    * 落盘前 `resolve()` 复核仍在工作区内（即使 sanitize 有漏也不越界）；
    * 超过 `MAX_DOWNLOAD_BYTES` 的拒（与下载同一上限，避免写满磁盘）。

    ## 为什么去重而不是覆盖

    用户可能把同一个 `nmap.xml` 传两次。覆盖会让人以为第一次那份还在，
    实际已被换掉；加后缀则两份都在，用户自己删不要的。与 `attachments.store`
    的命名取向一致（那边也带随机段）。
    """
    if not content:
        raise ValueError("文件是空的。")

    from app.chat import attachments as chat_attachments

    directory = _session_dir(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    uploads = directory / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)

    name = chat_attachments.sanitize_name(filename, fallback="upload")
    if len(content) > MAX_DOWNLOAD_BYTES:
        raise ValueError(
            f"文件太大（{len(content) / 1024 / 1024:.1f} MB）。"
            f"上限 {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB。"
        )

    base = directory.resolve()
    stem, ext = os.path.splitext(name)
    candidate = uploads / name
    # 只防"已存在"，不做无界重试：撞上 100 次说明磁盘上已经乱了，
    # 不如报错让用户看见，而不是默默生成 upload-9f3a2b1c.txt。
    for _ in range(100):
        try:
            # `x` 模式：原子地"不存在才创建"，并发两个同名上传也不会互相覆盖。
            with open(candidate, "xb") as handle:
                handle.write(content)
            break
        except FileExistsError:
            candidate = uploads / f"{stem}-{os.urandom(4).hex()}{ext}"
    else:
        raise ValueError("同名文件太多，换个名字再传。")

    resolved = candidate.resolve()
    if resolved != base and base not in resolved.parents:  # pragma: no cover - 防御
        resolved.unlink(missing_ok=True)
        raise ValueError("文件名不合法。")

    return SavedFile(
        path=resolved.relative_to(base).as_posix(),
        name=resolved.name,
        size_bytes=len(content),
    )


def drop_workspace(session_id: str) -> None:
    """删掉这个会话的工作区目录（归档/彻底删除时调用）。"""
    directory = _session_dir(session_id)
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)


__all__ = [
    "MAX_DOWNLOAD_BYTES",
    "MAX_LISTED_FILES",
    "PERSIST_MOUNT_DIR",
    "SavedFile",
    "WorkspaceFile",
    "drop_workspace",
    "ensure_workspace",
    "list_files",
    "resolve_file",
    "root",
    "save_file",
]
