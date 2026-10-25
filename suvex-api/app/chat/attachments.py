"""对话附件的暂存与读取（v0.94）。

## 这个模块解决什么问题

对话页要能"把一份日志/配置/nmap 输出丢进去问模型"。而对话引擎**没有
沙箱**（见 `prompts.py` 里「哪些事你做不到」那一段：它不能跑命令、不能
读文件）。所以附件唯一的用途是：**把文件里的文字给模型看**。

这决定了这个模块的边界：

* 只读文本。二进制文件不解析、不渲染、不当成可执行素材 —— 没有沙箱
  可以执行它，假装"已分析"是编造。
* 有上限。一份 20MB 的日志塞进上下文会撑爆，而且每轮都要重新付费。

## 为什么是"一轮用完即弃"而不是挂在会话上

两种设计都说得通，这里选前者：

* **挂在会话上**：附件一直在上下文里。好处是"这份配置我们后面一直在
  讨论"很自然；坏处是它占着每一轮的 token，用户忘了删就一直花钱，
  而且第二轮之后模型看到的是"还在"，很难让它忘掉。
* **挂在一条消息上（这里选的）**：发出去的时候就把它拼进那条 user
  消息的正文。之后它与任何一条历史消息没有区别 —— 不需要额外的表、
  不需要"这个附件属于哪轮"的关联、也不需要担心上下文里有一份看不见
  的常驻内容。

代价是"同一个文件问三次"要传三次。这在本项目的使用场景里可接受：
真正要反复用的材料应该写成技能（那才是常驻的机制）。

## 存储

`<root>/<session_id>/<attachment_id>-<safe_name>`

* 按会话分目录：清理与删除都是目录级操作，不用扫全盘。
* 文件名前缀 `attachment_id`：同名文件传两次不会互相覆盖，且 id
  是外部句柄（前端只拿 id，不拿路径）。

## `sweep()` 为什么必须有

上传了却没发出去（用户关了页面、点了别的会话）的文件会永远留在磁盘上。
`ingest` 时顺手清掉这个会话里超过 TTL 的旧文件 —— 不需要定时任务，
因为"这个会话有人在上传"本身就是清理的触发器。

⚠️ 这是一个**尽力而为**的清理：一个从不回来的会话，它的临时文件会留到
手动清。这是刻意的取舍 —— 引入一个全局定时任务来回收几十 KB 的临时
文件，代价（多一个要监控的后台组件）大于收益。
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: 单个附件的上限。
#:
#: 5MB 的纯文本约等于 5M 字符 ≈ 200 万 token —— 远超任何模型的上下文。
#: 所以这个上限**不是**"上下文能装下"的保证，只是"别让人误传一个
#: 数据库备份"的护栏。真正进上下文的那一段在 `read_text` 里二次截断。
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

#: 一次消息里最多带几个附件。与 `ChatSendIn.attachmentIds` 的 max_length 同源。
MAX_ATTACHMENTS_PER_MESSAGE = 10

#: 一个会话里最多同时存在几个**未使用**的暂存附件。
#:
#: 与上一条不同：那个限制"一条消息用几个"，这个限制"磁盘上堆几个"。
#: 用户可能连着传十几次都没发出去。
MAX_STAGED_PER_SESSION = 30

#: 暂存多久算过期（秒）。一小时足够"传完、想一下、发出去"。
STAGED_TTL_SECONDS = 3600

#: 单个附件注入上下文时最多带多少字符。
#:
#: ★ 与 `MAX_ATTACHMENT_BYTES` 是两个不同的闸门：前者是磁盘/上传护栏，
#:   这个才是**花钱**的闸门。6 万字符 ≈ 2 万 token，一条消息里带十个
#:   这样的附件就已经很贵了，所以超出的部分要明确告诉模型"被截了"，
#:   而不是让它以为自己看到了全文。
MAX_ATTACHMENT_CHARS = 60_000

#: 文件名白名单。允许中文、空格、点、括号 —— 拒绝路径分隔符与以点开头的隐藏名。
#:
#: ★ 与 `scans/uploads.py::safe_filename` 的口径一致（那边同样拒绝
#:   `/` `\` 与 `..`），但没有直接 import：那个函数会重命名成
#:   `attachment_1` 这种丢信息的形式，而对话附件的文件名是**要给模型看的**
#:   （模型靠它判断"这是 nginx.conf 还是 access.log"）。
_UNSAFE = re.compile(r"[/\\\x00-\x1f]")
_RESERVED = {"", ".", ".."}


def _root() -> Path:
    """附件根目录。

    默认在系统临时目录下，可用 `SUPEX_CHAT_ATTACHMENTS` 覆盖。
    ★ 为什么不放 `uploads/`（那里放扫描附件）：扫描附件是**产物**，
      要长期保留并进扫描详情；对话附件是**一次性耗材**，放在一起会让
      清理规则变成两套。
    """
    Raw = os.environ.get("SUPEX_CHAT_ATTACHMENTS", "").strip()
    if Raw:
        return Path(Raw)
    return Path(tempfile.gettempdir()) / "suvex-chat-attachments"


def _session_dir(session_id: str) -> Path:
    # ★ `session_id` 来自 URL。虽然它一定是 `chat_...` 形状（由 `new_id`
    #   生成），但这里仍然过一遍白名单 —— 路径拼接是那种"上游一旦有个
    #   bug 就直接变成任意文件写入"的位置，不值得赌上游不出错。
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
    return _root() / safe


def sanitize_name(raw: str, *, fallback: str = "attachment") -> str:
    """把外部给的文件名压成一个安全且仍可辨认的名字。

    保留原名（含中文/空格）是刻意的：模型要靠文件名判断内容，而
    `attachment_1.txt` 什么信息都不给。
    """
    name = (raw or "").strip().replace("\r", "").replace("\n", "")
    name = _UNSAFE.sub("_", name)
    # `../x` 被上面替换成 `.._x`，但它仍是"以点开头"的名字，去掉开头的点。
    name = name.lstrip(".").strip()
    if name in _RESERVED:
        return fallback
    # 太长的名字在部分文件系统上会失败（255 字节，中文占 3 字节）。
    if len(name) > 80:
        stem, ext = os.path.splitext(name)
        name = stem[: 80 - len(ext)] + ext
    return name or fallback


@dataclass(frozen=True)
class Staged:
    """一个已落盘的暂存附件。"""

    id: str
    name: str
    size_bytes: int


def _entry_id(filename: str) -> str:
    """从 `<id>-<name>` 里取回 id。"""
    return filename.split("-", 1)[0]


def sweep(*, session_id: str) -> int:
    """清掉这个会话里过期的暂存文件。返回删了几个。

    判据是**文件 mtime**，不是文件名里的时间戳 —— 后者要额外解析，
    而且复制过来的一批文件会带同一个时间戳，与"什么时候上传"无关。
    """
    directory = _session_dir(session_id)
    if not directory.is_dir():
        return 0
    cutoff = time.time() - STAGED_TTL_SECONDS
    removed = 0
    for path in directory.iterdir():
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed += 1
        except OSError:  # 并发删除 / 权限 —— 不该让上传失败
            logger.debug("清暂存附件失败：%s", path, exc_info=True)
    return removed


def store(*, session_id: str, filename: str, content: bytes, attachment_id: str) -> Staged:
    """落一个暂存附件。

    ⚠️ 超限**抛 `ValueError`**，由 router 翻成 400 —— 这里不做静默截断：
      截断一个上传的文件会让用户问"我传的那半截呢"。
    """
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"文件太大（{len(content) // 1024} KB）。"
            f"单个附件上限 {MAX_ATTACHMENT_BYTES // 1024 // 1024} MB。"
        )
    if not content:
        raise ValueError("文件是空的，没有内容可以给模型看。")

    directory = _session_dir(session_id)
    directory.mkdir(parents=True, exist_ok=True)

    name = sanitize_name(filename)
    path = directory / f"{attachment_id}-{name}"
    # 用 `"xb"` 语义（open 后立即写）而不是先 mkdir 再检查存在性：
    # id 由调用方保证唯一，`x` 会在撞名时直接失败，而不是静默覆盖。
    with path.open("xb") as handle:
        handle.write(content)
    return Staged(id=attachment_id, name=name, size_bytes=len(content))


def list_staged(*, session_id: str) -> list[Staged]:
    """这个会话里当前有哪些暂存附件（供前端显示"已上传"列表）。"""
    directory = _session_dir(session_id)
    if not directory.is_dir():
        return []
    out: list[Staged] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        raw = path.name
        if "-" not in raw:
            continue
        ident, name = raw.split("-", 1)
        try:
            out.append(Staged(id=ident, name=name, size_bytes=path.stat().st_size))
        except OSError:
            continue
    return out


def _find(*, session_id: str, attachment_id: str) -> Path | None:
    directory = _session_dir(session_id)
    if not directory.is_dir():
        return None
    for path in directory.iterdir():
        if path.is_file() and _entry_id(path.name) == attachment_id:
            return path
    return None


def read_text(*, session_id: str, attachment_ids: list[str]) -> list[tuple[str, str]]:
    """读出若干个附件的文本，返回 `(显示名, 正文)`，顺序与入参一致。

    ## 三件必须做对的事

    1. **不是文本就明确说不是**。用 `utf-8` 试解，失败再试 `gbk`
       （中文环境常见的编码），都不行就给一句"这是二进制文件，我读不了"，
       而不是塞一堆替换字符（`\ufffd`）让模型去猜。
    2. **超长要截且要说**。截断但不说，模型会以为自己看到了全文，
       然后基于"日志里没有这个错误"下结论 —— 而那半截正是被截掉的。
    3. **用完即删**。这些文件在被拼进消息之后就没有第二个用途了，
       留着只是磁盘垃圾。

    找不到的 id **跳过**（不抛）：前端可能同时开了两个标签页，
    另一个已经把文件用掉了。少一个附件比整个请求 400 好 —— 但会记
    warning，因为"用户以为传了却没传"是那种必须能查到的静默失败。
    """
    out: list[tuple[str, str]] = []
    for ident in attachment_ids:
        path = _find(session_id=session_id, attachment_id=ident)
        if path is None:
            logger.warning("对话附件不存在，跳过：session=%s id=%s", session_id, ident)
            continue
        name = path.name.split("-", 1)[1] if "-" in path.name else path.name
        try:
            raw = path.read_bytes()
        except OSError:
            logger.warning("读对话附件失败：%s", path, exc_info=True)
            continue

        text: str | None = None
        for encoding in ("utf-8", "gbk"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            # 二进制：明确说读不了。不说的话模型会编一段"我看到文件里有…"。
            out.append(
                (
                    name,
                    f"（这是二进制文件（{len(raw)} 字节），对话里读不了它的内容。"
                    "如果它是靶标附件，请走「扫描任务」——那里有沙箱。",
                )
            )
            continue

        if len(text) > MAX_ATTACHMENT_CHARS:
            dropped = len(text) - MAX_ATTACHMENT_CHARS
            text = (
                text[:MAX_ATTACHMENT_CHARS]
                + f"\n\n…（原文还有 {dropped} 字未展开。"
                "如果你需要后面的部分，请让用户分段上传。）"
            )
        out.append((name, text))
    return out


def discard(*, session_id: str, attachment_ids: list[str]) -> int:
    """删掉指定的暂存附件。返回删了几个。"""
    removed = 0
    for ident in attachment_ids:
        path = _find(session_id=session_id, attachment_id=ident)
        if path is None:
            continue
        with contextlib.suppress(OSError):
            path.unlink()
            removed += 1
    return removed


def drop_session(*, session_id: str) -> None:
    """整个会话的附件目录删掉（会话归档时调用）。"""
    directory = _session_dir(session_id)
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)


__all__ = [
    "MAX_ATTACHMENT_BYTES",
    "MAX_ATTACHMENT_CHARS",
    "MAX_ATTACHMENTS_PER_MESSAGE",
    "MAX_STAGED_PER_SESSION",
    "STAGED_TTL_SECONDS",
    "Staged",
    "discard",
    "drop_session",
    "list_staged",
    "read_text",
    "sanitize_name",
    "store",
    "sweep",
]
