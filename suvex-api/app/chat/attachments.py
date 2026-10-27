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

#: 交接目录在**沙箱里**挂到哪儿。
#:
#: 刻意用 `/work/attachments` 而不是别的路径：`/work` 是沙箱里唯一可写的
#: 工作区（无限制档是容器层，受限档是 tmpfs），模型的所有相对路径操作都
#: 从那儿出发。挂到 `/attachments` 会让它在两个"根"之间来回切。
#:
#: ⚠️ 这个名字与 `app.kernel.runner.ATTACHMENT_DIR`（`/work/attachments`）
#:    必须一致 —— 解题档把题目附件挂在这儿，对话档把交接附件挂在这儿，
#:    两边挂同一个路径意味着**同一段提示词在两种场景下都对**。有一条测试
#:    钉住这个一致性（`tests/chat/test_attachment_handover.py`）。
HANDOVER_MOUNT_DIR = "/work/attachments"

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
            # 二进制：明确说读不了，**并且**说清文件其实还在哪儿。
            #
            # ★ 这句曾经是"如果它是靶标附件，请走「扫描任务」——那里有沙箱"。
            #   那是 v0.107 之前的事实：对话页没有沙箱。现在有了（无限制档），
            #   再这么说就是**把用户赶去别的地方做本来这里就能做的事**。
            #   而且更糟：模型会顺着这句话说"这里做不了，你去扫描任务"，
            #   而它就站在一个能跑 binwalk 的沙箱里。
            out.append(
                (
                    name,
                    f"（这是二进制文件（{len(raw)} 字节），正文里读不了它的内容。"
                    f"原始文件已挂到 `{HANDOVER_MOUNT_DIR}/`（只读），"
                    "可以用 `run_shell` 去分析它：`file`、`xxd`、`binwalk`、"
                    "`tcpdump -r`、`strings` 都可以。）",
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


#: 交接给沙箱的附件目录（**会话级**，独立于暂存区）。
#:
#: ## 为什么不能直接挂暂存区
#:
#: 暂存区有三个"耗材"性质：`sweep()` 按 mtime 一小时清、`MAX_STAGED_PER_SESSION`
#: 卡 30 个、发消息后被 `discard()` 删掉。挂在沙箱里的目录必须是**稳定**的
#: —— 一个 pcapng 如果因为 mtime 到点被 sweep 掉，模型上一轮还在 `tcpdump`
#: 下一轮就 file not found，那比一开始就没有还糟。
#:
#: 所以分两层：暂存区（可清、有配额）+ 交接区（会话活多久就在多久）。
#: `hand_over()` 把文件从暂存区搬到交接区。
def handed_over_dir(session_id: str) -> Path:
    """这个会话的附件交接目录（宿主侧路径）。

    ⚠️ 它**可能不存在**（这个会话还没发过带附件的消息）。调用方必须按
       "存在才挂"处理 —— `SandboxContainer._argv` 就是这么做的，因为挂一个
       不存在的宿主路径会让 Docker 建一个空目录，而"空目录"与"没挂上"
       在容器里长得一模一样。
    """
    return _root() / "_handed_over" / _session_dir(session_id).name


def hand_over(*, session_id: str, attachment_ids: list[str]) -> list[tuple[str, str]]:
    """把暂存附件**搬**到交接目录，返回 `(显示名, 容器内路径)`。

    ## 为什么是「搬」而不是「复制」

    复制的话暂存区里那份没人删，就会一路撞 `MAX_STAGED_PER_SESSION`
    （30 个）—— 用户传第 31 个附件时被拒，而他其实一个都没落下。搬走
    则暂存区自然腾空。

    ## 为什么还要**同时**返回容器内路径

    因为模型要的是路径，用户要的是文件名。`solver-<id>-x.pcapng` 这种
    带 id 前缀的名字在容器里避不开（同名文件传两次不能互相覆盖），但
    提示词里必须告诉模型文件名长什么样、在哪个目录，否则它得先 `ls`
    一轮才知道有什么。

    ⚠️ 找不到的 id **跳过**（不抛）—— 与 `read_text` 同一条理由：
       前端开两个标签页时另一个可能已经用掉了。少一个比整个请求 400 好。
    """
    out: list[tuple[str, str]] = []
    target_dir = handed_over_dir(session_id)
    for ident in attachment_ids:
        path = _find(session_id=session_id, attachment_id=ident)
        if path is None:
            logger.warning("对话附件不存在，跳过交接：session=%s id=%s", session_id, ident)
            continue
        name = path.name.split("-", 1)[1] if "-" in path.name else path.name
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / path.name
            # `os.replace` 而不是 `shutil.move`：同一个文件系统内它是原子的，
            # 而 shutil.move 会先尝试 rename、失败再退回 copy+unlink ——
            # 那条回退路径在"沙箱正在读这个文件"时会读到半个文件。
            os.replace(path, target)
        except OSError:
            # 搬不动（权限/跨设备）：退回只读文本路径，别让整轮失败。
            logger.warning("交接对话附件失败，保留在暂存区：%s", path, exc_info=True)
            continue
        out.append((name, f"{HANDOVER_MOUNT_DIR}/{target.name}"))
    return out


def drop_handed_over(*, session_id: str) -> None:
    """删掉这个会话的交接目录（归档时调用）。

    ⚠️ 与 `drop_session` 分开：暂存区和工作区是两棵树，一个可能在归档时
       已经被清过了。
    """
    directory = handed_over_dir(session_id)
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)


__all__ = [
    "HANDOVER_MOUNT_DIR",
    "MAX_ATTACHMENT_BYTES",
    "MAX_ATTACHMENT_CHARS",
    "MAX_ATTACHMENTS_PER_MESSAGE",
    "MAX_STAGED_PER_SESSION",
    "STAGED_TTL_SECONDS",
    "Staged",
    "discard",
    "drop_handed_over",
    "drop_session",
    "hand_over",
    "handed_over_dir",
    "list_staged",
    "read_text",
    "sanitize_name",
    "store",
    "sweep",
]
