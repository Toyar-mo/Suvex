"""题目附件的下载与落盘。

## 这个模块解决什么

同步只把 `attachments` 清单存进 JSONB，而**文件从来没被拉下来** ——
于是解题时 `/work/attachments` 是空的，agent 看到"没有附件"，
只能靠题面文字猜里面有什么。而很多题的核心恰恰就在附件里
（一个 ELF、一份流量包、一段密文），猜是猜不出来的。

这个模块把清单变成磁盘上的真实文件。

## ★ 三条硬约束，每一条都挡一类真实的事故

### 1. 只允许 **CTF2 官方域名**

附件 URL 是平台给的，但**题目内容可控** —— 出题人完全可以在
`attachments` 里放一条指向任意地址的链接（"附件见 http://…"）。
不校验 host 就等于让出题人决定我们的服务端去访问哪里：
内网地址、云元数据、我们自己的 API 都在射程内。

⚠️ 注意这里**刻意不传 `allowed_extra`**：题目环境地址是**解题时**的
访问目标，不是附件的来源。把它加进附件的白名单会让"某个环境的
地址恰好被写进附件 URL"变成一条可走通的路径 —— 而那条路径上
没有任何理由存在。

### 2. 大小上限（单个 + 单次同步总量）

没有上限时，一个 5 GB 的"附件"能把磁盘写满 —— 而磁盘满是
**整个服务**不可用，不只是这次同步失败（与 `scans/uploads.py`
里记的是同一条）。

### 3. 文件名**净化**，而且是白名单式

落盘路径由**服务端**从题目标识生成，文件名只取基名并过滤掉
一切路径分隔符与上跳。

⛔ 不做"过滤 `..`"那种黑名单：本项目的附件删除那一次已经栽过
路径穿越，而正确的形状从来是"先确定合法集合，再判在不在里面"。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from app.kernel.client import KernelError, assert_allowed_target

logger = logging.getLogger("suvex.ctf2")

# ── 上限 ──────────────────────────────────────────────────
#
# 单个附件 50 MiB：CTF 附件可能是几十 MB 的镜像或字典，
# 所以比漏洞附件（10 MiB）宽得多 —— 与 `scans/uploads.py` 取同一个值。
MAX_FILE_BYTES = 50 * 1024 * 1024

# 单次同步下载的总量上限。**必须独立于单文件上限**：
# 一道题 20 个附件、一场赛事 50 道题，单文件都在限内，
# 总量却可以是几十 GB。
MAX_TOTAL_BYTES = 200 * 1024 * 1024

# 一道题最多下多少个附件。防"一个附件字段里塞一千条 URL"。
MAX_FILES_PER_CHALLENGE = 30

# 单个文件的下载超时（秒）。
DOWNLOAD_TIMEOUT = 60.0

#: 附件指纹 sidecar 的后缀（2026-10-12 补）。
#:
#: ⚠️ 它有两个用途，**必须共用一个常量**：
#:   1. `_fingerprint_path` 用它拼指纹文件名；
#:   2. `runner._attachment_paths` 用它**排除**指纹文件 ——
#:      否则 `<name>.sha256` 会出现在给模型的附件清单里，
#:      而模型会去分析一个只有哈希值的文件（看起来像题目给的）。
FINGERPRINT_SUFFIX = ".sha256"

# 文件名里**允许**的字符集合。
#
# ★ 白名单而不是黑名单：黑名单（"去掉 `../`"）永远漏一种编码，
# 而漏掉的那次就是一次路径穿越。这里只放行"在一个文件名里
# 合理出现"的字符，其余全部换成下划线。
#
# ⚠️ 中文字符要保留（题目附件叫「题目描述.txt」是常态），
# 所以不是 `[A-Za-z0-9._-]` 那么窄 —— 而是"非控制字符、
# 非分隔符、非上跳"。
_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]")

# 名字里出现这些片段就整体替换成 hash：它们要么是上跳，
# 要么在某些文件系统上有特殊含义。
_UNSAFE_SEGMENTS = ("..", "~", "$", "%", "&", ";", "`", "'", '"')


@dataclass(slots=True)
class DownloadResult:
    """一次附件下载的账。"""

    downloaded: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=list)
    bytes_written: int = 0

    def summary(self) -> str:
        parts = [f"附件 {self.downloaded}"]
        if self.skipped:
            parts.append(f"跳过 {self.skipped}")
        if self.failed:
            parts.append(f"失败 {len(self.failed)}")
        return " · ".join(parts)


@dataclass(frozen=True, slots=True)
class PendingDownload:
    """一条待下载的附件（**纯数据**，由同步阶段算好交给协程）。

    ★ 为什么把它做成纯数据而不在协程里查数据库：见
    `service.sync` 的三段式分工 —— 协程里只做网络，不碰 Session。
    """

    challenge_id: str
    url: str
    name: str


def attachment_root(challenge_id: str) -> Path:
    """一道题的附件落盘目录。

    ⚠️ 放在 `scan_root/_ctf2_attachments/<challengeId>/`：
    与扫描产物（`scan_root/<scanId>/`）**分开**，与
    `scans/runner.scan_upload_dir` 是同一条理由 —— 附件的生命周期
    跟着**题目**走（重跑还要用），而扫描产物会随保留期被清掉。

    ⚠️ 目录名用**我们自己的 challenge id**，不用平台的 remote_id：
    后者是别人家的标识，可能含任意字符，而它出现在一个文件系统
    路径里是我们不该接受的输入。
    """
    from app.core.config import get_settings

    return get_settings().scan_root / "_ctf2_attachments" / challenge_id


def sanitize_filename(raw: str, *, fallback: str = "attachment") -> str:
    """把一个（可能来自平台、也可能来自 URL 的）名字净化成**安全的基本名**。

    规则（按顺序）：

    1. 百分号解码（URL 里常是 `%E9%A2%98%E7%9B%AE.txt`）；
    2. 只取 basename（`a/b/c.txt` → `c.txt`）—— 一次就消掉全部目录成分；
    3. 控制字符与文件系统特殊字符换成 `_`；
    4. 去掉首尾的点与空白（`.bashrc`、`..`、`   `）；
    5. 含上跳/特殊片段 → 整体换成内容 hash（保扩展名）；
    6. 空 → `fallback`。

    ⚠️ 第 5 条不是"多余的一层"：一个叫 `..` 的文件在步骤 4 之后
    会变成空串，而**空串拼进路径**会得到一个目录本身 ——
    那意味着后面 `write_bytes` 会失败，而失败原因看起来像权限问题。
    """
    text = unquote(raw or "").strip()
    # ② basename：同时处理两种分隔符（URL 用 `/`，Windows 风格可能给 `\`）
    text = text.replace("\\", "/").split("/")[-1]
    # ③ 不安全字符
    text = _UNSAFE_CHARS.sub("_", text)
    # ④ 首尾的点与空白
    text = text.strip(". ")
    # ⑤ 片段级的兜底
    if not text or any(segment in text for segment in _UNSAFE_SEGMENTS) or text in {".", ".."}:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        suffix = Path(text).suffix if text else ""
        # 扩展名要能保留（`.bin` / `.tar.gz` 影响 agent 怎么处理它），
        # 但也要净化 —— 它同样是外部输入。
        suffix = _UNSAFE_CHARS.sub("", suffix)[:12]
        text = f"{fallback}-{digest}{suffix}"
    # 文件名的长度上限（多数文件系统 255 字节，中文一篇占 3 字节）
    return text[:150]


def name_from_url(url: str, *, index: int) -> str:
    """从 URL 猜一个文件名。猜不出就用序号。

    ⚠️ 只用于**清单里没给名字**的情况。清单给了名字就以它为准 ——
    那才是出题人希望 agent 看到的名字（题面里常常引用它）。
    """
    path = urlsplit(url).path
    candidate = path.rstrip("/").split("/")[-1]
    if not candidate:
        return f"attachment-{index}"
    return sanitize_filename(candidate, fallback=f"attachment-{index}")


def plan_downloads(
    challenge_id: str, attachments: Any, *, limit: int = MAX_FILES_PER_CHALLENGE
) -> list[PendingDownload]:
    """把题目 JSON 里的 `attachments` 变成待下载清单。

    ## 平台可能给的三种形状，都要认

    * `["http://…/a.zip", "http://…/b.bin"]` —— 裸 URL 数组；
    * `[{"name": "a.zip", "url": "http://…"}, …]` —— 带名字的对象；
    * `[{"filename": "a.zip", "download_url": "http://…"}, …]` —— 换个键名。

    认不出来就**跳过那一条**（而不是猜）。一条附件认不出来只让
    那道题少一个文件，而"猜一个 URL"会让服务端去访问一个
    我们没打算访问的地址。
    """
    if not isinstance(attachments, list):
        return []

    planned: list[PendingDownload] = []
    for index, item in enumerate(attachments[:limit]):
        url = ""
        name = ""
        if isinstance(item, str):
            url = item.strip()
        elif isinstance(item, dict):
            for key in ("url", "download_url", "downloadUrl", "href", "link"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    url = value.strip()
                    break
            for key in ("name", "filename", "file_name", "title"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    name = value.strip()
                    break
        if not url:
            continue
        planned.append(
            PendingDownload(
                challenge_id=challenge_id,
                url=url,
                name=sanitize_filename(name) if name else name_from_url(url, index=index),
            )
        )
    return planned


def _already_there(target: Path, *, expected_size: int | None) -> bool:
    """已经下过、且大小一致 —— 跳过。

    ## ⚠️ 这条判据是**不完整**的（如实记，2026-10-12）

    只比大小会漏掉一类真实情况：**平台改了附件但大小恰好一样**。
    那时我们会跳过下载，而容器里挂着的是**旧内容** —— 症状是
    "模型分析的附件与题面对不上"，而排查方向会跑到模型身上。

    为什么不在这里加哈希校验：**下载前我们没有基准哈希可比**。
    平台只给 `content-length`，不给 checksum。所以"跳过"这个决定
    只能在"大小一致"这个信息上做。

    ## 指纹记在**下载之后**（见 `_write_fingerprint`）

    大小一致时**不再重下**（省带宽），但会顺手核对一次已有文件的
    指纹是否与记录一致 —— 若不一致说明本地那份被改过（或上次下载
    没写完），那时**必须重下**。这一层是廉价的：算一个已存在文件的
    SHA256 不需要网络。

    ## 参数

    `expected_size` 为 `None` 表示平台没给 `content-length`。那时
    **只判"文件存在"** —— 这是最弱的一档，所以下面会明确区分。
    """
    if not target.is_file():
        return False
    if expected_size is None:
        return True
    if target.stat().st_size != expected_size:
        return False
    # ★ 大小一致 —— 再用指纹确认本地那份是完整的。
    #
    # 没有指纹记录时（老数据、或首次升级）**按"已存在"处理**：
    # 不能因为缺一份 sidecar 就把所有附件重下一遍（那可能是几十 MB
    # × 上百道题）。缺记录是"不知道"，而"不知道"不该导致大批量重下。
    recorded = _read_fingerprint(target)
    if recorded is None:
        return True
    return recorded == _sha256_of(target)


def _fingerprint_path(target: Path) -> Path:
    """指纹文件的路径。与附件同目录，名字加 `.sha256` 后缀。

    ⚠️ 用**同名 + 后缀**而不是集中存一个索引文件：索引文件要处理
    并发写与损坏恢复，而"每个附件旁边一个小文件"天然没有这些问题 ——
    删附件时顺手删它，不需要任何同步逻辑。

    ⚠️ 这个后缀同时被 `runner._attachment_paths` 用来**排除**指纹文件
    （否则它会出现在给模型的附件清单里）。改后缀要同时改那边 ——
    所以两处都引用同一个常量 `FINGERPRINT_SUFFIX`。
    """
    return target.with_name(target.name + FINGERPRINT_SUFFIX)


def _sha256_of(path: Path) -> str:
    """算文件的 SHA256（分块读，别把大文件一次读进内存）。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_fingerprint(target: Path) -> str | None:
    """读已记录的指纹。没有或读不出来时返回 `None`。"""
    path = _fingerprint_path(target)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    # 格式：`sha256:<hex>`。认不出就当作没有。
    if not value.startswith("sha256:"):
        return None
    return value[len("sha256:") :] or None


def _write_fingerprint(target: Path, digest: str) -> None:
    """把指纹写到附件旁边。写失败**不影响下载结果**。

    ⚠️ 指纹是**辅助信息**（用于判断"要不要重下"），它丢了只会让
    下次多下一次，不会让解题出错。所以吞 `OSError` 并记一条 warning。
    """
    try:
        _fingerprint_path(target).write_text(f"sha256:{digest}\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("写附件指纹失败（不影响本次下载）：%s", exc)


def _is_official(url: str) -> bool:
    """附件 URL 必须落在 CTF2 官方域名下。**不传 `allowed_extra`**（见模块 docstring）。"""
    try:
        assert_allowed_target(url)
        return True
    except KernelError:
        return False


async def download_all(
    token: str,
    pending: list[PendingDownload],
    *,
    client: httpx.AsyncClient | None = None,
) -> DownloadResult:
    """下载清单里的全部附件。**单条失败不中断整批。**

    ⚠️ `token` 只用于 `Authorization` 头（附件可能有权限要求），
    绝不进 URL、不进日志 —— 与 `client.py` 同一条纪律。
    """
    result = DownloadResult()
    if not pending:
        return result

    owns_client = client is None
    # ★ `trust_env=False` —— 这里**不是** SSRF 面（每条 URL 先过
    #   `_is_official()`，目标是官方域名），但请求带
    #   `Authorization: <token>`：`trust_env` 默认 `True` 会让下载走
    #   环境变量里的 `HTTP_PROXY`，那等于**把这个 token 交给那个代理**。
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(DOWNLOAD_TIMEOUT), trust_env=False
    )
    try:
        for item in pending:
            if result.bytes_written >= MAX_TOTAL_BYTES:
                # 出声，不静默截断：用户要知道"这次同步只下了一部分"。
                result.failed.append(
                    f"已达到单次同步的附件总量上限（{MAX_TOTAL_BYTES // 1024 // 1024} MB），"
                    f"剩余的 {len(pending) - result.downloaded - result.skipped} 条未下载"
                )
                break
            try:
                await _download_one(http, token, item, result)
            except Exception as exc:  # noqa: BLE001 - 一条失败不该中断整批
                logger.warning("下载附件失败 %s：%s", item.url, exc)
                result.failed.append(f"{item.name}：{exc}")
    finally:
        if owns_client:
            await http.aclose()
    return result


async def _download_one(
    http: httpx.AsyncClient, token: str, item: PendingDownload, result: DownloadResult
) -> None:
    """下载一个附件。见 `download_all` 的说明。"""
    if not _is_official(item.url):
        # ★ 这是**拒绝**而不是"警告后继续"。见模块 docstring 第 1 条：
        #   不校验 host 等于让出题人决定我们去访问哪里。
        raise KernelError(
            f"附件地址不在 CTF2 官方域名下，已拒绝下载：{urlsplit(item.url).hostname}"
        )

    target_dir = attachment_root(item.challenge_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / item.name

    # 目录已经建好，但 `item.name` 是净化过的 —— 这里再断言一次
    # 它没有跳出 target_dir。这是"第二处表达"而不是冗余：
    # 上面那次净化是本模块的规则，而这一条是**文件系统层面的事实**，
    # 两者都在，将来谁改坏了净化规则都还会被挡住。
    if target.resolve().parent != target_dir.resolve():
        raise KernelError(f"附件名净化后仍然跳出了目录：{item.name!r}")

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with http.stream("GET", item.url, headers=headers) as response:
        if response.status_code >= 400:
            raise KernelError(f"平台返回 {response.status_code}")

        declared = response.headers.get("content-length")
        expected = int(declared) if declared and declared.isdigit() else None
        if expected is not None and expected > MAX_FILE_BYTES:
            raise KernelError(
                f"附件超过单文件上限（{expected // 1024 // 1024} MB > "
                f"{MAX_FILE_BYTES // 1024 // 1024} MB）"
            )
        if _already_there(target, expected_size=expected):
            result.skipped += 1
            return

        written = 0
        # 流式写：一次性 `read()` 一个 200 MB 的附件会把内存打满，
        # 而那是**整个服务**的可见后果。
        #
        # ★ 顺手算 SHA256（2026-10-12 补）：字节已经在手上流过，
        #   所以这一次哈希**不额外读盘**。它的用途是让下次同步能判断
        #   "本地这份是否完整、是否还是平台给的那一份" —— 见
        #   `_already_there` 里那段"只比大小是不完整的"说明。
        digest = hashlib.sha256()
        try:
            with target.open("wb") as handle:
                async for chunk in response.aiter_bytes(64 * 1024):
                    written += len(chunk)
                    if written > MAX_FILE_BYTES:
                        raise KernelError("附件在下载过程中超过了单文件上限，已中止")
                    digest.update(chunk)
                    handle.write(chunk)
        except Exception:
            # 半截文件比没有更坏：agent 会把它当成完整的附件去分析，
            # 而失败原因是"文件损坏"，方向完全错。所以删掉。
            target.unlink(missing_ok=True)
            # 指纹也要一起删 —— 留着一个指向已删除文件的指纹，
            # 下次会用它去比对一个不存在的文件（无害但没意义）。
            _fingerprint_path(target).unlink(missing_ok=True)
            raise

        _write_fingerprint(target, digest.hexdigest())

    result.downloaded += 1
    result.bytes_written += written
    logger.info("附件已下载：%s（%d 字节）", item.name, written)


__all__ = [
    "DOWNLOAD_TIMEOUT",
    "MAX_FILE_BYTES",
    "MAX_FILES_PER_CHALLENGE",
    "MAX_TOTAL_BYTES",
    "DownloadResult",
    "PendingDownload",
    "attachment_root",
    "download_all",
    "name_from_url",
    "plan_downloads",
    "sanitize_filename",
]
