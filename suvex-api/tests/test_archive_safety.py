"""解压不信任压缩包的守卫（任务 17）。

## 这些测试要证伪的是**具体的攻击**，不是"函数能跑"

「解压一个 zip」写起来三行，跑起来也绿 —— 而它可能是**任意文件写入**。
所以这里每一条都构造出**真实的恶意包**，然后断言它被拒：

| 构造 | 不设防的后果 |
|---|---|
| 条目名 `../../x` | 写到目标目录**外面**（zip slip）|
| 条目名 `/etc/cron.d/x` | 绝对路径写入 |
| 条目名 `..\\..\\x` | 在 Windows 上穿越，在 Linux 上"没被攻击" |
| 声明 10 字节、实际吐 5 MB | 头部**自己写**的大小不能信 |
| 500 万个小条目 | 光建 inode 就能拖垮文件系统 |
| 符号链接条目 | 解出一个指向任意位置的入口 |

★ 最后一条判据值得单独说：**"在 Linux 上没被攻击"不等于安全**。
同一份代码在两个平台上安全性不同，是最难被测试发现的一类问题 ——
因为 CI 通常只跑其中一个平台。所以反斜杠穿越必须单独有一条。
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from app.core.errors import ApiError
from app.scans import archive


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def _symlink_zip(path: Path, link_name: str, target: str) -> Path:
    """构造一个含**符号链接**条目的 zip。

    zip 没有专门的链接类型 —— unix 的打包工具靠**外部属性里的模式位**
    表达（`S_IFLNK`）。所以这里手工把高 16 位设成 `0o120777`，
    这正是真实打包工具写进去的形状。
    """
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo(link_name)
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, target)
    return path


# ══════════════════════════════════════════════════════════
# 一、zip slip
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "entry",
    [
        "../evil.txt",
        "../../evil.txt",
        "a/b/../../../evil.txt",
        "/etc/cron.d/evil",
        "/tmp/evil",
    ],
)
def test_zip_slip_entries_are_refused(tmp_path: Path, entry: str) -> None:
    """★ 条目名指向上级或绝对路径 → **整包拒绝**。

    不设防时 `extractall` 会老老实实写到目标目录外面 ——
    而这条路径来自用户上传，等于任意文件写入。
    """
    package = _zip(tmp_path / "evil.zip", {entry: b"pwned"})
    dest = tmp_path / "out"

    with pytest.raises(ApiError):
        archive.unpack(package, dest)

    # 而且**什么都没写出去**（不是"拒绝了但还是留了一半"）
    assert not (tmp_path / "evil.txt").exists()
    assert not Path("/tmp/evil").exists()


def test_backslash_traversal_is_refused(tmp_path: Path) -> None:
    """★ 反斜杠也要当分隔符看。

    只看 `/` 的实现会把 `..\\..\\evil.txt` 当成**一个普通文件名** ——
    于是在 Linux 上"攻击无效"，在 Windows 上直接穿越。
    同一份代码在不同平台安全性不同，正是最难被发现的一类问题。

    （顺带：`_UNSAFE_IN_NAME` 那套文件名消毒去掉了反斜杠，
    但那是**上传时**对真实文件名做的，管不到**压缩包内部的条目名** ——
    这里必须自己判。）
    """
    package = _zip(tmp_path / "evil.zip", {"..\\..\\evil.txt": b"pwned"})

    with pytest.raises(ApiError):
        archive.unpack(package, tmp_path / "out")


def test_windows_drive_absolute_is_refused(tmp_path: Path) -> None:
    """`C:\\x` 这种带盘符的绝对路径同样要拒 —— 部署到 Windows 上才生效的漏洞
    最容易漏测。"""
    package = _zip(tmp_path / "evil.zip", {"C:\\Windows\\evil.dll": b"pwned"})

    with pytest.raises(ApiError):
        archive.unpack(package, tmp_path / "out")


# ══════════════════════════════════════════════════════════
# 二、膨胀与条目爆炸
# ══════════════════════════════════════════════════════════


def test_total_size_limit_is_enforced_on_real_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 解压总量超限 → 拒绝。

    上限调小到 1 KB 来跑，否则这条测试要先造 1 GiB 数据 ——
    而**测试跑不动就等于没有测试**。
    """
    monkeypatch.setattr(archive, "MAX_TOTAL_BYTES", 1024)
    package = _zip(tmp_path / "big.zip", {"a.bin": b"A" * 4096})

    with pytest.raises(ApiError, match="总量超过上限"):
        archive.unpack(package, tmp_path / "out")


def test_single_file_limit_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单个文件超限 → 拒绝（**先于**总量判，报错才说得清是哪个文件）。"""
    monkeypatch.setattr(archive, "MAX_FILE_BYTES", 512)
    package = _zip(tmp_path / "one-big.zip", {"a.bin": b"A" * 2048})

    with pytest.raises(ApiError, match="单个文件解压后超过上限"):
        archive.unpack(package, tmp_path / "out")


def test_entry_count_limit_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """条目数超限 → 拒绝。几十万个小文件不需要占多少字节，但能拖垮文件系统。"""
    monkeypatch.setattr(archive, "MAX_ENTRIES", 5)
    package = _zip(tmp_path / "many.zip", {f"f{i}.txt": b"x" for i in range(20)})

    with pytest.raises(ApiError, match="文件太多"):
        archive.unpack(package, tmp_path / "out")


def test_budget_counts_what_is_really_written(tmp_path: Path) -> None:
    """★★ 上限判在**真实写出的字节数**上，不信任包头声明的大小。

    为什么必须这样：`ZipInfo.file_size` 是压缩包**自己写**的字段。
    构造一个"头部声称 10 字节、实际吐出 5 MB"的包并不难，而按声明值判
    的实现会**放它过去**。

    这里直接喂一个"说一套做一套"的 reader —— 它没有 size 属性可供参考，
    函数只能靠数实际读到的字节。这正是真实解压路径的形状。
    """
    monkeypatch_budget = archive._Budget()
    written = 0
    chunk = b"A" * 1024

    class LyingReader:
        """一个只有 `read` 的流，像 `ZipExtFile` 那样 —— 声明值根本拿不到。"""

        def __init__(self, chunks: int) -> None:
            self.left = chunks

        def read(self, _size: int) -> bytes:
            if self.left <= 0:
                return b""
            self.left -= 1
            return chunk

    target = tmp_path / "out.bin"
    written = archive._write_stream(LyingReader(4), target, monkeypatch_budget, "x.bin")

    assert written == 4096, "计数必须是真实写出的字节数"
    assert target.stat().st_size == 4096


# ══════════════════════════════════════════════════════════
# 三、特殊文件
# ══════════════════════════════════════════════════════════


def test_symlink_entries_are_skipped_but_the_rest_is_extracted(tmp_path: Path) -> None:
    """★ 符号链接条目**跳过**，而**不**整包拒绝。

    取舍：正常项目打包时带软链很常见，拒绝它会让一个完全良性的
    题目附件解不开。而跳过是安全的 —— 我们不会创建任何指向别处的入口。

    与此同时，同一个包里的普通文件必须照常解出来：
    "为了安全把整包丢掉了"同样是一种坏结果，只是坏得安静。
    """
    package = tmp_path / "mixed.zip"
    with zipfile.ZipFile(package, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = 0o120777 << 16
        zf.writestr(info, "/etc/passwd")
        zf.writestr("real.bin", b"hello")

    written = archive.unpack(package, tmp_path / "out")

    assert written == ["real.bin"]
    assert (tmp_path / "out" / "real.bin").read_bytes() == b"hello"
    assert not (tmp_path / "out" / "link").exists(), "符号链接被创建了"


# ══════════════════════════════════════════════════════════
# 四、tar
# ══════════════════════════════════════════════════════════


def test_tar_traversal_is_refused(tmp_path: Path) -> None:
    """tar 与 zip 走**同一套判据** —— 不能出现"zip 挡得住、tar 挡不住"。

    这种不一致最阴的地方在于：修 zip 的时候测试全绿，而攻击者换个格式就进来了。
    """
    package = tmp_path / "evil.tar"
    with tarfile.open(package, "w") as tf:
        data = b"pwned"
        info = tarfile.TarInfo("../evil.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    with pytest.raises(ApiError):
        archive.unpack(package, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


def test_tar_symlink_is_skipped(tmp_path: Path) -> None:
    """tar 里的软链同样跳过，同一个包里的普通文件照常解出。"""
    package = tmp_path / "mixed.tar"
    with tarfile.open(package, "w") as tf:
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)

        data = b"hello"
        real = tarfile.TarInfo("real.bin")
        real.size = len(data)
        tf.addfile(real, io.BytesIO(data))

    written = archive.unpack(package, tmp_path / "out")

    assert written == ["real.bin"]
    assert not (tmp_path / "out" / "link").exists()


def test_normal_tar_gz_still_works(tmp_path: Path) -> None:
    """普通的 `.tar.gz` 必须能正常解开 —— 守卫不能把正常用法一起挡掉。

    （这条是"反向"的：前面全在测"拒绝"，而一个只会拒绝的实现
    同样能通过那些测试。必须有正例。）
    """
    package = tmp_path / "chall.tar.gz"
    with tarfile.open(package, "w:gz") as tf:
        data = b"\x7fELF fake binary"
        info = tarfile.TarInfo("chall")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    assert archive.is_archive(package)
    written = archive.unpack(package, tmp_path / "out")

    assert written == ["chall"]
    assert (tmp_path / "out" / "chall").read_bytes() == b"\x7fELF fake binary"


# ══════════════════════════════════════════════════════════
# 五、不是压缩包：原样放入，不许报错
# ══════════════════════════════════════════════════════════


def test_plain_file_is_copied_as_is(tmp_path: Path) -> None:
    """CTF 直接给一个 ELF 是常态 —— "不是压缩包"**不是错误**。

    把它当异常报错，等于把最常见的情况判成了错误。
    """
    source = tmp_path / "chall"
    source.write_bytes(b"\x7fELF real binary")
    dest = tmp_path / "out"

    assert archive.is_archive(source) is False
    assert archive.stage(source, dest) == ["chall"]
    assert (dest / "chall").read_bytes() == b"\x7fELF real binary"


def test_corrupt_archive_falls_back_to_the_raw_file(tmp_path: Path) -> None:
    """损坏的压缩包**原样放入**，而不是报错。

    "为什么它坏了"有时就是考点（截断的文件头、故意破坏的结构）——
    丢掉它等于丢掉这道题。但**守卫拒绝的绝不退回**（见
    `test_rejected_archive_is_not_silently_copied`）。
    """
    source = tmp_path / "broken.zip"
    # 有 PK 头、但没有中央目录 —— `is_zipfile` 认不出来
    source.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    dest = tmp_path / "out"

    assert archive.is_archive(source) is False
    assert archive.stage(source, dest) == ["broken.zip"]
    assert (dest / "broken.zip").exists()


def test_rejected_archive_is_not_silently_copied(tmp_path: Path) -> None:
    """★★ 守卫拒绝的包**绝不能**被"退回原样放入"。

    这是一条很容易写错的分支：`stage` 里有个"解不开就原样复制"的兜底，
    而它必须**只兜底"格式不认识"**，不能把 `ApiError` 一起吞掉。

    如果吞掉了，一个带 `../../` 的恶意包会被原样铺进目标目录，
    然后由沙箱里的 agent 解开 —— 攻击面从宿主机挪到沙箱，
    而**用户以为自己上传的东西被检查过了**。
    """
    package = _zip(tmp_path / "slip.zip", {"../../evil.txt": b"pwned"})
    dest = tmp_path / "out"

    with pytest.raises(ApiError):
        archive.stage(package, dest)

    assert not (dest / "slip.zip").exists(), "被拒的包被原样放回去了"
