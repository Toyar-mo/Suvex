"""附件内容指纹（v0.30）：识别"改了内容但大小一样"的更新。

## 这组用例守什么

`_already_there` 原本**只比文件大小**，代码自己写了那条代价：

    ⚠️ 平台改了附件但大小恰好一样时我们会漏掉这次更新。

漏掉的后果不是"少个优化"：容器里挂的是**旧内容**，而题面写的是新的
—— 症状是"模型分析的附件与题面对不上"，排查方向会跑到模型身上。

## 为什么不是"下载前校验哈希"

**因为下载前没有基准可比**。平台只给 `content-length`，不给 checksum。
所以指纹是**下载后记录、下次比对**：它让"跳过"这个决定多一层依据。

## 三个口径要分清（这组用例的核心）

  · 大小一致 + 指纹一致  → 跳过（正常路径）
  · 大小一致 + 指纹**不一致** → **重下**（本地那份被改过或没写完）
  · 大小一致 + **没有指纹记录** → 跳过（不能因为缺 sidecar 就全量重下）
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.ctf2 import attachments as att


def _write(path: Path, content: bytes) -> None:
    path.write_bytes(content)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ══════════════════════════════════════════════════════════
# 一、指纹读写
# ══════════════════════════════════════════════════════════


def test_fingerprint_path_uses_the_shared_suffix(tmp_path: Path) -> None:
    """★ 指纹文件与附件同目录、名字是 `<name> + 后缀`。

    后缀必须来自 `FINGERPRINT_SUFFIX` —— 因为 `runner._attachment_paths`
    也用同一个常量排除它。两处各写一份就会漂移。
    """
    target = tmp_path / "chall.zip"
    assert att._fingerprint_path(target).name == "chall.zip" + att.FINGERPRINT_SUFFIX


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    """★ 写进去能读回来。"""
    target = tmp_path / "a.bin"
    _write(target, b"hello")
    att._write_fingerprint(target, _sha(b"hello"))

    assert att._read_fingerprint(target) == _sha(b"hello")


def test_read_returns_none_when_missing(tmp_path: Path) -> None:
    """★ 没有指纹文件 → `None`（而不是抛异常）。"""
    assert att._read_fingerprint(tmp_path / "nope.bin") is None


def test_read_returns_none_for_a_malformed_file(tmp_path: Path) -> None:
    """★ 格式不对 → `None`（当作没有，而不是拿一段垃圾去比对）。"""
    target = tmp_path / "b.bin"
    _write(target, b"x")
    att._fingerprint_path(target).write_text("这不是指纹\n", encoding="utf-8")
    assert att._read_fingerprint(target) is None


def test_sha256_of_matches_hashlib(tmp_path: Path) -> None:
    """★ 算出来的确实是与 hashlib 一致的 SHA256。"""
    target = tmp_path / "c.bin"
    _write(target, b"some content for hashing")
    assert att._sha256_of(target) == _sha(b"some content for hashing")


def test_sha256_handles_a_file_larger_than_the_read_chunk(tmp_path: Path) -> None:
    """★★ 分块读的边界：文件比 chunk 大时结果仍然正确。

    这条挡的是"分块实现漏了最后一块"或"offset 算错"—— 那类 bug
    在小文件上完全看不出来（测试常用几十字节的 fixture）。
    这里造 3 MB（> 1 MB 的 chunk）。
    """
    target = tmp_path / "big.bin"
    payload = bytes(range(256)) * (3 * 4096)  # 3 MiB
    _write(target, payload)
    assert att._sha256_of(target) == _sha(payload)


# ══════════════════════════════════════════════════════════
# 二、★ 核心：`_already_there` 的三个口径
# ══════════════════════════════════════════════════════════


def test_skips_when_size_and_fingerprint_both_match(tmp_path: Path) -> None:
    """★ 正常路径：大小一致 + 指纹一致 → 跳过（不重下）。"""
    target = tmp_path / "same.zip"
    _write(target, b"content")
    att._write_fingerprint(target, _sha(b"content"))

    assert att._already_there(target, expected_size=7) is True


def test_redownloads_when_content_changed_but_size_is_identical(
    tmp_path: Path,
) -> None:
    """★★ 这条是整个改动存在的理由。

    平台改了附件、而**大小恰好一样** —— 只比大小的实现会跳过，
    于是容器里挂着旧内容。现在指纹不一致 → **重下**。
    """
    target = tmp_path / "tricky.zip"
    # 本地那份是"旧内容"，指纹记的也是旧内容
    _write(target, b"AAAAAAAA")
    att._write_fingerprint(target, _sha(b"AAAAAAAA"))
    # 现在本地文件被替换成了"新内容"，但大小一样（8 字节）
    _write(target, b"BBBBBBBB")

    assert att._already_there(target, expected_size=8) is False, (
        "★ 大小一样但内容变了，必须重下 —— 这正是只比大小会漏掉的情况"
    )


def test_skips_when_fingerprint_record_is_absent(tmp_path: Path) -> None:
    """★★ 没有指纹记录时**跳过**，不重下。

    为什么这条重要：升级到本版本时，所有**已有附件都没有 sidecar**。
    若"缺记录"被当成"内容可疑"，第一次同步会把全部附件重下一遍 ——
    那可能是几十 MB × 上百道题。

    缺记录是"不知道"，而"不知道"不该导致大批量重下。
    """
    target = tmp_path / "legacy.zip"
    _write(target, b"old attachment")

    assert att._already_there(target, expected_size=14) is True


def test_redownloads_when_size_differs(tmp_path: Path) -> None:
    """★ 大小不同 → 重下（原有行为，不能退化）。"""
    target = tmp_path / "grew.zip"
    _write(target, b"short")
    assert att._already_there(target, expected_size=999) is False


def test_missing_file_is_never_already_there(tmp_path: Path) -> None:
    """★ 文件不存在 → 一定要下。"""
    assert att._already_there(tmp_path / "absent.zip", expected_size=10) is False


def test_unknown_size_only_checks_existence(tmp_path: Path) -> None:
    """★ 平台没给 `content-length` 时只判"存在"（最弱的一档）。

    那时无法比大小，而"重下"要付带宽代价 —— 所以按"已存在"处理。
    这是刻意的取舍，不是遗漏。
    """
    target = tmp_path / "nosize.zip"
    _write(target, b"whatever")
    assert att._already_there(target, expected_size=None) is True


# ══════════════════════════════════════════════════════════
# 三、★ 指纹文件不能混进附件清单
# ══════════════════════════════════════════════════════════


def test_fingerprint_files_are_excluded_from_attachment_paths() -> None:
    """★★ 指纹 sidecar **不能**出现在给模型的附件清单里。

    不过滤的后果：模型会去分析一个只有哈希值的文件
    （`- foo.zip.sha256（65 字节）`），而它看起来像题目给的。
    """
    from app.ctf2 import runner

    directory = runner._attachment_dir("ctf2chal_FINGERPRINTFILTER")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "chall.zip").write_bytes(b"PK\x03\x04 real")
    (directory / "chall.zip.sha256").write_text("sha256:deadbeef\n", encoding="utf-8")

    class _FakeChallenge:
        id = "ctf2chal_FINGERPRINTFILTER"

    names = [p.name for p in runner._attachment_paths(_FakeChallenge())]

    assert "chall.zip" in names, "真附件必须还在"
    assert "chall.zip.sha256" not in names, "★ 指纹文件混进了附件清单"


def test_a_real_file_named_like_a_hash_is_kept() -> None:
    """★★ 题目真给的 `hashes.sha256.txt` **不能**被误伤。

    这条钉住"用精确后缀而不是 `'.sha256' in name`"：
    后者会把题目的正常文件也排除掉，而症状是"附件少了一个"。
    """
    from app.ctf2 import runner

    directory = runner._attachment_dir("ctf2chal_REALHASHFILE")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "hashes.sha256.txt").write_bytes(b"real challenge file")

    class _FakeChallenge:
        id = "ctf2chal_REALHASHFILE"

    names = [p.name for p in runner._attachment_paths(_FakeChallenge())]
    assert "hashes.sha256.txt" in names, "★ 题目给的真文件被误当成指纹排除了"
