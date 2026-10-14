"""把**不信任的压缩包**安全地铺开（任务 17）。

## 为什么这件事要单独一个模块，而且必须带守卫

`artifact`（只有附件的逆向 / 纯文件 Misc 题）模式要求执行后端拿到一个
**目录**当目标（strix 对单个文件直接拒绝：`Path exists but is not a
directory`），而用户交上来的往往是一个 **zip**。所以我们要在**宿主机上**
解开一个**完全不信任的压缩包**。

这与"把附件丢进沙箱、让 agent 自己解开"是**两种不同性质的风险**：

| | 在哪解 | 炸的是谁 |
|---|---|---|
| 沙箱里解 | 容器内 | 沙箱（`uploads.py` 说过：真实边界就在那儿）|
| **宿主机上解** | 跑本服务的这台机器 | **我们自己的磁盘** —— 而磁盘满是**整个服务**不可用 |

`uploads.py` 的说明里写着"恶意附件可以攻击沙箱里的解析器，但那正是这个
功能的用途"。**这句话不适用于本模块**：在宿主机上解开压缩包，风险不来自
"样本被分析"，而来自**压缩包这个容器本身** —— 穿越路径、条目爆炸、膨胀。

所以这一层的守卫不是为了防样本，是为了防**包**：

1. **zip slip** —— 条目名里写 `../../etc/cron.d/x`，解压即任意写；
2. **膨胀** —— 一个 1 MiB 的包解出 10 GiB，把磁盘填满；
3. **条目爆炸** —— 几十万个条目，光是建 inode 就能拖垮文件系统；
4. **特殊文件** —— 符号链接 / 设备节点，解出来就是一个指向别处的入口。

## ★ 一条刻意的取舍：**声明的**大小一律不信

`ZipInfo.file_size` 是压缩包里**自己写**的。构造一个头部声称 100 字节、
实际吐出 10 GiB 的包并不难。所以上限判在**真实写入的字节数**上
（边写边数，超了就中止），声明值只用来做**提前**拒绝（省掉无谓的建文件），
不作为放行的依据。这与 `uploads.py` 里"按真实字节数判"是同一条原则。

## 为什么不用 `tarfile.extractall(filter="data")`

`filter=` 是 Python 3.12 才有的（3.11 上会 `TypeError`）。本项目要能在
开发机与部署机上跑同一套代码，而两边的 Python 版本不由我们决定。
与其写一个版本分支（那会让**新旧两条路各自演化**，而安全代码最怕这个），
不如自己走一遍条目 —— 顺带让 zip 与 tar 共用同一套判据，
不会出现"zip 挡得住、tar 挡不住"这种最难发现的不一致。
"""

from __future__ import annotations

import logging
import re
import shutil
import tarfile
import zipfile
from pathlib import Path

from app.core.errors import ApiError

logger = logging.getLogger("suvex.scans")

# ── 上限 ────────────────────────────────────────────────────
#
# 数值怎么定的：附件上传本身的上限是 50 MiB（见 `uploads.py` 的
# `MAX_BYTES`），所以这些是"解压后"的容忍度，要覆盖真实的 CTF 题 ——
# 一个几十 MB 的镜像解开到几百 MB 是正常的。
#
# 但**必须有**：没有它，一个 1 MiB 的 zip bomb 就能把宿主机磁盘写满。
MAX_TOTAL_BYTES = 1024 * 1024 * 1024  # 解压后总量 1 GiB
MAX_FILE_BYTES = 256 * 1024 * 1024  # 单个解压出来的文件 256 MiB
MAX_ENTRIES = 5000  # 条目数

# 读块大小。1 MiB —— 大到不至于让"边写边数"变成性能问题，
# 小到不会为了数一个超限的条目而先把它整个读进内存。
_CHUNK = 1024 * 1024

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def is_archive(path: Path) -> bool:
    """看起来是压缩包吗。认不出来就返回 False（**不是错误**）。

    ★ 这必须是"认不出来就算了"，不能是"认不出来就拒绝"：

    CTF 直接给一个 ELF、一个 `.pcap`、一张图，都是常态。那些文件
    **本来就不该被解开**，原样放进目标目录即可 —— agent 要的就是那个文件本身。
    把"不是压缩包"当异常报错，等于把最常见的情况判成错误。
    """
    try:
        return zipfile.is_zipfile(path) or tarfile.is_tarfile(path)
    except OSError:
        return False


def unpack(archive: Path, dest: Path) -> list[str]:
    """把 `archive` 铺开到 `dest`，返回写出的**相对路径**清单。

    调用方负责先问 `is_archive`；这个函数假定格式已经被识别过了
    （真的传进来一个非压缩包，zip/tar 两边都会抛，由调用方兜）。

    任何守卫被触发都抛 `ApiError.validation` —— **整包拒绝，不做部分解压**。

    ★ 为什么是整包拒绝，而不是"跳过那个坏条目继续解"：

    用户交上来的是一道题的全部素材。悄悄地少解出几个文件，症状是
    "agent 说找不到某个文件"，而那是**从行为流里几乎查不出来**的一类问题。
    当场失败并说清是哪个条目、为什么，比留下一份不完整的素材好得多。
    """
    if zipfile.is_zipfile(archive):
        return _unpack_zip(archive, dest)
    return _unpack_tar(archive, dest)


# ══════════════════════════════════════════════════════════
# 公共判据
# ══════════════════════════════════════════════════════════


class _Budget:
    """跨条目的总量账本。

    做成对象而不是函数里的一个变量，是因为 zip 与 tar 两条路都要用 ——
    而漏掉其中一条的计数就是"某一种格式没有总量上限"。
    """

    def __init__(self) -> None:
        self.total = 0
        self.entries = 0

    def add_entry(self, name: str) -> None:
        self.entries += 1
        if self.entries > MAX_ENTRIES:
            raise ApiError.validation(
                f"压缩包里的文件太多（超过 {MAX_ENTRIES} 个）",
                {"file": name},
            )

    def add_bytes(self, count: int, name: str) -> None:
        if count > MAX_FILE_BYTES:
            raise ApiError.validation(
                f"压缩包里的单个文件解压后超过上限"
                f"（{MAX_FILE_BYTES // (1024 * 1024)} MB）",
                {"file": name},
            )
        self.total += count
        if self.total > MAX_TOTAL_BYTES:
            raise ApiError.validation(
                f"压缩包解压后总量超过上限"
                f"（{MAX_TOTAL_BYTES // (1024 * 1024)} MB）—— 不是正常的题目附件",
                {"file": name},
            )


def _safe_member(dest: Path, name: str) -> Path | None:
    """把条目名解析成一个**确定在 `dest` 之内**的落点（zip slip 的唯一防线）。

    返回 `None` 表示这个条目**应当被跳过**（名字是空的 / 只是 `.`），
    它不是错误。

    ## 判据

    1. 绝对路径（`/x`、`C:\\x`）→ 拒；
    2. 任何一段是 `..` → 拒；
    3. 最后再 `resolve()` 比一次 —— 这条**理论上永远不会命中**（前两条
       已经拦住了所有已知写法），留着是因为"理论上"：将来有人为了支持
       某种古怪的条目名放宽前两条时，这道会挡住穿越。
       纵深防御的代价是一次路径解析，漏掉的代价是任意文件写入。

    ⚠️ 反斜杠也要当分隔符看：Windows 上构造的包会用 `..\\..\\x`，
    而只看 `/` 的实现在 Linux 上会把它当成一个**普通文件名** ——
    于是在 Linux 上"没被攻击"，在 Windows 上被攻击。同一份代码在不同
    平台上的安全性不同，是最难被测出来的一种。
    """
    raw = (name or "").replace("\\", "/")

    if raw.startswith("/") or _WINDOWS_DRIVE.match(raw):
        raise ApiError.validation(
            "压缩包里有绝对路径的条目，已拒绝解压", {"entry": name}
        )

    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts:
        return None

    if any(part == ".." for part in parts):
        raise ApiError.validation(
            "压缩包里有指向上级目录的条目（zip slip），已拒绝解压", {"entry": name}
        )

    target = dest.joinpath(*parts)
    if not target.resolve().is_relative_to(dest.resolve()):
        logger.error("压缩包条目越界，已拒绝：%s", name)
        raise ApiError.validation("压缩包有条目不合法", {"entry": name})
    return target


def _write_stream(reader, target: Path, budget: _Budget, name: str) -> int:  # noqa: ANN001
    """把一条流边写边计数地落到 `target`。返回真实写出的字节数。

    ★ 用**真实字节数**判上限，而不是包头部声明的大小（见模块 docstring）。
    超限时**先关掉文件再抛** —— 已经写出去的那部分由调用方整目录清掉。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with target.open("wb") as out:
        while True:
            chunk = reader.read(_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            budget.add_bytes(len(chunk), name)
            out.write(chunk)
    return written


# ══════════════════════════════════════════════════════════
# zip
# ══════════════════════════════════════════════════════════


def _unpack_zip(archive: Path, dest: Path) -> list[str]:
    written: list[str] = []
    budget = _Budget()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            # 目录条目：不建空目录。空目录对"解题"没有价值，
            # 而建它需要额外一次路径判断 —— 少一个分支少一处可错。
            if info.is_dir():
                continue
            # 符号链接（unix 模式位 S_IFLNK）。**跳过并记一条警告**，
            # 不是整包拒绝：正常项目打包时带软链很常见，而拒绝它
            # 会让一个完全良性的题目附件解不开。
            # 跳过是安全的 —— 我们不会创建任何指向别处的入口。
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                logger.warning("压缩包里有符号链接条目，「%s」已跳过", info.filename)
                continue

            budget.add_entry(info.filename)
            target = _safe_member(dest, info.filename)
            if target is None:
                continue
            with zf.open(info) as src:
                _write_stream(src, target, budget, info.filename)
            written.append(str(target.relative_to(dest)))
    return written


# ══════════════════════════════════════════════════════════
# tar / tar.gz / tgz
# ══════════════════════════════════════════════════════════


def _unpack_tar(archive: Path, dest: Path) -> list[str]:
    written: list[str] = []
    budget = _Budget()
    # `r:*` 让 tarfile 自己嗅探压缩方式（gz / bz2 / xz / 纯 tar）
    with tarfile.open(archive, "r:*") as tf:
        for member in tf:
            if member.isdir():
                continue
            # 符号链接与硬链接、设备节点、FIFO —— 一律跳过。
            # 它们都能变成"指向解压目录之外的入口"，而我们没有任何理由
            # 为了一道 CTF 题去创建它们。
            if not member.isfile():
                logger.warning(
                    "压缩包里有非普通文件条目（%s），「%s」已跳过",
                    "链接" if member.issym() or member.islnk() else "特殊文件",
                    member.name,
                )
                continue

            budget.add_entry(member.name)
            target = _safe_member(dest, member.name)
            if target is None:
                continue
            src = tf.extractfile(member)
            if src is None:  # pragma: no cover - isfile() 为真时不该发生
                continue
            with src:
                _write_stream(src, target, budget, member.name)
            written.append(str(target.relative_to(dest)))
    return written


def stage(archive: Path, dest: Path) -> list[str]:
    """把一个**可能是压缩包的**文件放进 `dest`。

    * 是压缩包 → 解开（守卫见上）；
    * 不是 → **原样复制**（CTF 直接给一个 ELF 是常态）。

    返回写出的相对路径清单。

    ## 为什么解不开时**退回原样复制**，而不是报错

    一个损坏的 zip 仍然是一个**值得分析的样本** —— "为什么它坏了"
    有时就是考点（截断的文件头、故意破坏的结构）。

    但只有"认不出格式"和"包本身损坏"才退回。**守卫拒绝的（穿越路径、
    超限）绝不退回** —— 那类包不该被原样铺到目标目录里装作没事。

    ⚠️ 目标目录由这里保证存在：解压那两条路会顺带建出父目录
    （`_write_stream` 里），而"原样复制"这条路不会 —— 漏了它，
    传一个裸 ELF 会得到 `FileNotFoundError`，看起来像文件系统出了问题，
    其实只是少了一行 `mkdir`。
    """
    dest.mkdir(parents=True, exist_ok=True)
    try:
        return unpack(archive, dest)
    except ApiError:
        # ★ 守卫的拒绝必须往上抛，不能被下面的兜底吞掉
        raise
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as exc:
        logger.warning("附件「%s」无法作为压缩包解开（%s），按原样放入", archive.name, exc)
        shutil.copy2(archive, dest / archive.name)
        return [archive.name]


__all__ = [
    "is_archive",
    "MAX_ENTRIES",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "stage",
    "unpack",
]
