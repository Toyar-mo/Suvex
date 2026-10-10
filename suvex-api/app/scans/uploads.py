"""扫描附件：用户随任务一起交给 agent 的文件（任务 12 阶段 4）。

## 这个模块存在的理由

CTF 题的**核心往往在附件里** —— 一个 ELF 二进制、一份源码、一个字典、
一张流量包。只把网址交给 agent，它只能靠题面**描述**去猜那里面有什么，
而很多题光看描述是猜不出来的（"附件见下"是题面的常态）。

strix 提供了 `--workspace-file PATH[:DEST]`：
"Place a file from this machine into the sandbox workspace before the
scan starts, for example a wordlist, an API specification, or notes."

这个模块产出那些文件，`runner._workspace_files` 把它们交给命令行。

## ★ 为什么与 `findings/attachments.py` 是**两套**规则

看起来像重复代码，但两边的取舍是相反的：

| | 漏洞附件 | 扫描附件 |
| --- | --- | --- |
| 是什么 | 处置**证据**（截图、复测报告） | 题目**输入**（二进制、源码、字典） |
| 扩展名 | 白名单（要能安全地读回来） | **几乎不限制**（`.bin`、无扩展名都正常） |
| 谁读它 | 人在浏览器里看 | **机器**在沙箱里分析 |
| 生命周期 | 跟着漏洞走 | 跟着"这道题"走（重跑还要用） |
| 大小 | 10 MiB 够 | 可能要几十 MiB（镜像、大字典） |

合用一个模块的话，白名单会被迫取**两者中更宽的那个** ——
于是"处置证据"那边也收任意二进制，而那个白名单本来是为了
"下载时类型可信"。让两边各自收紧，比取宽的那个安全。

## ⚠️ 这些文件会被送进沙箱，这是**有意的**

它们会进到那个跑着安全工具的容器里，被各种解析器打开。
一个精心构造的恶意附件理论上可以攻击那些解析器 ——
**但这就是这个功能的用途**（把不信任的样本交给分析工具）。
真实边界在沙箱隔离上（见 `docs/security-checklist.md` 的 F-01/F-02），
不在这里的文件校验上。如实记一笔，免得下一个人以为这里漏了什么。
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import ApiError
from app.scans import archive
from app.scans.runner import challenge_dir, scan_upload_dir

logger = logging.getLogger("suvex.scans")

# 单个附件的字节上限。CTF 附件可能是几十 MB 的镜像或字典，
# 所以比漏洞附件（10 MiB）宽得多。
#
# ★ 但仍然**必须有上限**：没有它，一个 2 GB 的上传能把磁盘写满，
#   而磁盘满是**整个服务**不可用（不只是那个请求失败）。
MAX_BYTES = 50 * 1024 * 1024

# 一次最多几个。防止"一次传几百个"占满磁盘与请求时间。
MAX_COUNT = 10

# ⚠️ **刻意不做扩展名白名单**（与漏洞附件相反）。
#
# 理由：CTF 附件经常没有扩展名（`chall`、`libc.so.6`），
# 或者带扩展名但类型完全任意（`.raw`、`.dat`、`.img`）。
# 白名单在这里会拦掉大量**合法**的题目附件，而用户无法绕过 ——
# 只能放弃用这个功能。
#
# 那安全靠什么？
#   1. **不按内容执行**：这些文件只在沙箱内部被工具读取；
#   2. **文件名消毒**：路径分隔符与控制字符一律去掉（见下），
#      所以穿越不了目录；
#   3. **沙箱隔离**：真正的边界在那里，不在这里。
# 拦"危险扩展名"（`.exe`、`.sh`）没有意义 —— 分析二进制**就是**用途。

# 文件名里不允许出现的字符：路径分隔符、控制字符、以及 Windows
# 保留字符。**去掉**而不是替换成下划线：`..%2f..` 这类编码攻击
# 在 `%` 也被去掉之后无从构造。
_UNSAFE_IN_NAME = re.compile(r'[\\/\x00-\x1f\x7f:*?"<>|]')


def safe_filename(raw: str) -> str:
    """把用户给的文件名消毒成**一个安全的裸名**。

    做三件事：
      1. 只取最后一段（`a/b/c.bin` → `c.bin`）—— 路径分隔符进不来；
      2. 去掉控制字符与 Windows 保留字符；
      3. 去掉开头的点（隐藏文件）与结尾的点/空格（Windows 不允许）。

    ⚠️ 与漏洞附件**不同**：那边存储路径完全由 id 生成、文件名只用于显示；
    这边**必须保留原名**，因为 agent 要靠名字认东西 ——
    一个叫 `chall` 的文件和一个叫 `att_01J…` 的文件，对分析者的价值不同。

    那覆写怎么办？由 `unique_name()` 加后缀解决，不靠"存储名与显示名分离"。
    """
    name = (raw or "").strip()
    # 只取最后一段（两种分隔符都要处理：用户可能从 Windows 复制路径）
    name = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    name = _UNSAFE_IN_NAME.sub("", name)
    name = name.strip(". ")
    # 兜底：消毒完什么都不剩（比如输入是 `../`）
    return name or "attachment"


def unique_name(directory, name: str) -> str:  # noqa: ANN001
    """在目录里找一个不冲突的文件名。

    ★ 两个人都传 `chall` 时，后者**不能覆盖**前者 ——
    覆盖掉的可能是这次分析唯一正确的那个附件。

    策略：`chall` → `chall-2` → `chall-3`…（保留扩展名，在主干上加后缀）。
    """
    candidate = name
    if not (directory / candidate).exists():
        return candidate

    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    for index in range(2, 1000):
        candidate = f"{stem}-{index}{'.' + ext if ext else ''}"
        if not (directory / candidate).exists():
            return candidate
    raise ApiError.validation(
        f"同名附件太多了（{name}）", {"files": "请先重命名再上传"}
    )


def store(*, scan_id: str, filename: str, content: bytes) -> tuple[str, int]:
    """写入一个扫描附件。返回 `(消毒后的文件名, 字节数)`。"""
    return _write(directory=scan_upload_dir(scan_id), filename=filename, content=content)


def _write(*, directory, filename: str, content: bytes) -> tuple[str, int]:  # noqa: ANN001
    """写入的公共实现 —— `store` 与 `store_staged` 只差一个落点。

    ## 顺序：只写文件，**没有数据库行**

    与漏洞附件不同 —— 那边有 `finding_attachments` 表要一起维护。
    这里**刻意不建表**：这些文件的"元信息"就是文件系统本身
    （名字、大小、mtime），而多一张表就多一处会与磁盘不一致的状态
    （行在文件没了、文件在行没了）。少一张表，少一类不一致。

    ## 上限按**真实字节数**判

    路由层按 `Content-Length` 挡一道，但那可以被客户端撒谎
    （分块传输）。真正的闸门在这。
    """
    if not content:
        raise ApiError.validation("空的附件不接收", {"file": filename})
    if len(content) > MAX_BYTES:
        raise ApiError.validation(
            f"附件超过大小上限（{MAX_BYTES // (1024 * 1024)} MB）",
            {"file": f"「{filename}」实际 {len(content) / (1024 * 1024):.1f} MB"},
        )

    directory.mkdir(parents=True, exist_ok=True)

    name = unique_name(directory, safe_filename(filename))
    target = directory / name

    # ★ 最后一道：确认落点确实在目录内。
    #
    # 正则已经去掉了分隔符，所以这一步理论上永远不会失败 ——
    # 而它存在的理由正是"理论上"：将来有人放宽 `_UNSAFE_IN_NAME`
    # （比如为了支持带空格的目录名），这道检查会挡住穿越。
    # 纵深防御的代价是一行解析，漏掉的代价是任意路径写入。
    resolved = target.resolve()
    root = directory.resolve()
    if not resolved.is_relative_to(root):
        logger.error("扫描附件路径越界，已拒绝：%s", filename)
        raise ApiError.validation("附件名不合法", {"file": filename})

    resolved.write_bytes(content)
    return name, len(content)


def list_for(scan_id: str) -> list[dict]:
    """列出一次扫描的附件（按名字排序，顺序稳定）。"""
    directory = scan_upload_dir(scan_id)
    if not directory.is_dir():
        return []
    rows = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        rows.append({"name": path.name, "sizeBytes": path.stat().st_size})
    return rows


# ══════════════════════════════════════════════════════════
# 暂存区（任务 12 修正）
# ══════════════════════════════════════════════════════════
#
# ## ★ 为什么需要它 —— 一个真实的顺序死结
#
# 我第一版的设计是「先 `POST /scans` 建记录，再往
# `POST /scans/{id}/attachments` 传附件」。**这个设计是死的**，
# 而测试当场抓住了它：
#
#     POST /scans            → 状态 queued
#     （调度器立刻 `_nudge_scheduler()`，毫秒级认领）
#     POST /scans/{id}/attachments → 409 SCAN_ALREADY_STARTED
#
# 创建与启动之间的窗口是**毫秒级**的（见 `service.create_scan` 末尾
# 的 `_nudge_scheduler()`），所以"建完再传"在实践中永远来不及。
# 于是附件功能等于不存在 —— 而它看起来完全正常（端点都在、测试也过）。
#
# ## 修法：把附件放在创建**之前**
#
# 这也更符合用户的实际动作顺序：向导里先填目标、贴题面、**选附件**，
# 最后才点「启动扫描」。所以：
#
#   1. 用户在向导里选文件 → 前端立刻 `POST /scans/attachments/staging`
#      → 拿到一个 `stagingId`；
#   2. 点启动 → `POST /scans` 带上 `attachmentStagingId`；
#   3. 后端把暂存目录**移**到该扫描名下（`claim`）。
#
# ## 为什么用"移动"而不是"复制"
#
# 移动之后暂存目录就没了 —— 这天然地让"一次暂存只能被认领一次"，
# 不需要额外的状态标记。复制则要额外记录"已认领"，而那是又一处
# 会与磁盘不一致的状态。
#
# ## 没被认领的暂存文件怎么办
#
# 用户选了文件但最后没点启动 —— 那些文件会留在暂存区。
# 它们由 `sweep_staging` 按时间扫掉，而那个函数挂在
# `service.sweep_retention` 里（启动时与改保留规则时都会跑）。
#
# ⚠️ 我第一版写的是"与扫描产物同一套机制，不另建一套" —— 那是错的：
# `sweep_retention` **根本不删任何目录**（它只改数据库）。所以
# 暂存清理是**新加**的，见 `sweep_staging`。

# 暂存目录的标记名。以 `_` 开头，与 `_uploads/<scan_id>/` 区分开 ——
# 清理函数必须精确匹配它，绝不能误伤已认领的附件。
STAGING_DIRNAME = "_staging"

# 暂存批次的存活期（秒）。默认 2 小时。
#
# 「选了文件但没点启动」很常见（改主意、或者只是想看看），
# 那些文件不该永久留在磁盘上。2 小时足够覆盖"中途去查资料再回来"。
#
# ⚠️ 过期从目录 mtime 算，而往批次里传文件会更新它 ——
# 所以"分几次选文件"不会让同一批次被提前清掉。
STAGING_TTL_SECONDS = 2 * 60 * 60


def staging_dir(staging_id: str) -> Path:
    """某个暂存批次（或整个暂存区）的目录。"""
    return get_settings().scan_root / "_uploads" / STAGING_DIRNAME / safe_filename(staging_id)


def store_staged(*, staging_id: str, filename: str, content: bytes) -> tuple[str, int]:
    """把文件存进暂存区。返回 `(名字, 字节数)`。

    校验与 `store` 完全一致（大小、非空、路径安全）——
    它只是**落点不同**，安全要求一模一样。
    """
    return _write(
        directory=staging_dir(staging_id),
        filename=filename,
        content=content,
    )


def peek_staged(staging_id: str) -> list[dict]:
    """看一眼暂存批次里有哪些文件 —— **只看，不动**。

    ## 为什么需要"只看不动"

    `artifact` 模式（任务 17）要在建记录**之前**就知道附件名：
    它拿第一个附件的文件名当 `scans.target`，并据此推导扫描名
    （`tryre.zip` → `tryre`）。

    而 `claim` 是**移动**（见上面的说明），必须留到记录建好之后 ——
    那时 `scan_id` 才存在。于是顺序只能是：

        先 peek 拿名字 → 建记录 → 铺开 challenge/ → claim 收走原件

    少一步都会出问题：先 claim 就没法在建记录时知道名字，
    先 claim 再解压则一旦守卫拒绝，用户的附件已经不在暂存区了。
    """
    directory = staging_dir(staging_id) if staging_id else None
    if directory is None or not directory.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        rows.append({"name": safe_filename(path.name), "sizeBytes": path.stat().st_size})
    return rows


def build_challenge(*, scan_id: str, staging_id: str) -> list[dict]:
    """把暂存的题目附件铺开成 `challenge/` 目录 —— `artifact` 模式的目标。

    返回铺开后的文件清单（`{name, sizeBytes}`），用于日志与提示。

    ## ★ 为什么从**暂存区**读，而不是从 `_uploads/<scan_id>/`

    因为此刻 `claim` 还没跑（它在等 `scan_id`）。而更重要的是
    **这一步失败时，用户的附件必须还在**：

    `claim` 是移动。若先认领再解压，一旦守卫拒绝（zip slip / 解压超限），
    用户的题目文件就躺在 `_uploads/<scan_id>/` 而扫描又没建出来 ——
    他既不知道该重传，也无从知道刚才那次上传哪里不对。

    所以顺序是「先解压、成功了再认领」。失败时抛出去，暂存区原封不动，
    用户改完就能重试（`ApiError` 里的那句话会告诉他为什么被拒）。

    ## 为什么要整目录清干净再建

    `dest` 在正常流程下必然是全新的（同一个 `scan_id` 只走一次）。
    但仍然清一次：万一将来允许"建完再补附件"（`claim` 的注释里
    已经为那种情况留了唯一名逻辑），残留的旧文件会让这次的附件
    与上次的混在一起 —— 而 agent 分不出哪个才是这道题的。
    """
    dest = challenge_dir(scan_id)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    staged = staging_dir(staging_id) if staging_id else None
    if staged is None or not staged.is_dir():
        raise ApiError.validation("题目附件已过期或不存在，请重新上传")

    produced: list[dict] = []
    try:
        for path in sorted(staged.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            # 是压缩包就解开，不是就原样复制 —— 判断在 `archive.stage` 里。
            for relative in archive.stage(path, dest):
                produced.append(
                    {"name": relative, "sizeBytes": (dest / relative).stat().st_size}
                )
    except Exception:
        # ★ 守卫拒绝（或任何失败）时把半成品清掉。
        #
        # 留着一个"解了一半"的素材目录，比什么都没有更坏：下一次启动会
        # 拿到一份**看起来正常**的残缺题目，而 agent 会在这个基础上
        # 分析出错误结论 —— 那时已经没人记得少了一个文件。
        shutil.rmtree(dest, ignore_errors=True)
        raise

    if not produced:
        shutil.rmtree(dest, ignore_errors=True)
        raise ApiError.validation("题目附件里没有任何文件，无法作为分析目标")
    return produced


def claim(staging_id: str, scan_id: str) -> list[dict]:
    """把暂存的文件认领到某次扫描名下。返回认领到的文件列表。

    ## 幂等：认领不存在的暂存批次返回空列表，不报错

    这很重要。用户"没选附件直接启动"时前端不会传 `attachmentStagingId`；
    但如果传了一个已经认领过（或过期）的 id，**扫描仍然应该建起来** ——
    附件丢了是次要问题，让整次扫描建不出来是主要问题。

    所以这里返回空列表并记一条警告，由调用方决定要不要提示用户。
    """
    source = staging_dir(staging_id) if staging_id else None
    if source is None or not source.is_dir():
        if staging_id:
            logger.warning("暂存批次 %s 不存在或已被认领，本次扫描没有附件", staging_id)
        return []

    target = scan_upload_dir(scan_id)
    target.mkdir(parents=True, exist_ok=True)

    claimed: list[dict] = []
    for path in sorted(source.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        # 目标目录此刻是空的（新扫描），但仍走一次唯一名 ——
        # 万一将来允许创建后再补附件，这里不会静默覆盖。
        name = unique_name(target, safe_filename(path.name))
        (target / name).write_bytes(path.read_bytes())
        claimed.append({"name": name, "sizeBytes": (target / name).stat().st_size})

    # 认领 = 移动。清掉暂存目录，让"已认领"这件事由文件系统本身表达。
    shutil.rmtree(source, ignore_errors=True)
    return claimed


def sweep_staging(*, older_than_seconds: float) -> int:
    """清掉过期的暂存批次。返回清掉的批次数。

    "选了文件但最后没点启动"是常见情况（用户改主意、或者只是看看），
    那些文件不该永久留在磁盘上。
    """
    root = get_settings().scan_root / "_uploads" / STAGING_DIRNAME
    if not root.is_dir():
        return 0
    cutoff = time.time() - older_than_seconds
    removed = 0
    for path in root.iterdir():
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:  # pragma: no cover - 并发删除
            continue
    return removed


def delete_for(scan_id: str, name: str) -> None:
    """删掉一个附件。

    ⚠️ 文件名**必须再消毒一次**：它是从 URL 路径参数来的，
    直接用 `directory / name` 会允许 `../../` 穿越删除任意文件。
    """
    clean = safe_filename(name)
    target = (scan_upload_dir(scan_id) / clean).resolve()
    root = scan_upload_dir(scan_id).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ApiError.not_found("附件")
    target.unlink()


__all__ = [
    "build_challenge",
    "claim",
    "MAX_BYTES",
    "MAX_COUNT",
    "delete_for",
    "list_for",
    "peek_staged",
    "safe_filename",
    "sweep_staging",
    "STAGING_TTL_SECONDS",
    "staging_dir",
    "store",
    "store_staged",
    "unique_name",
]
