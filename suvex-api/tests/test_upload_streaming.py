"""附件**流式落盘**的行为（v0.84）。

## 为什么需要这组测试

v0.84 把单附件上限从 50 MB 提到 200 MB，起因是一个真实的
101,703,716 字节附件被 nginx 的 `client_max_body_size 64m` 挡掉
（用户只看到「服务端返回了非 JSON 响应（HTTP 413）」）。

只改上限是不够的：原实现是

    content = await item.read()          # ← 整个 body 进内存
    uploads.store_staged(..., content=content)

在单进程 uvicorn 上，一次 200 MB 的上传会让 RSS 涨 200 MB 以上，
**期间整站都卡**（不只是这个请求慢）。

所以落盘改成了分块拷贝。这组测试锁住三件事：

1. **内存不随文件大小线性涨** —— 这是改动的全部目的，最该被守住；
2. **分块写出来的字节与输入完全一致** —— 流式路径不能损坏数据；
3. **失败时不留下半截文件** —— 否则用户重试一次就多一个残骸，
   而残骸在 `list_for` 眼里和成功上传的附件长得一模一样。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.errors import ApiError
from app.scans import uploads


class _FakeUpload:
    """一个最小的 `UploadFile` 替身 —— 只实现 `read(n)`。

    ## 为什么不用真的 `UploadFile`

    这里要验的是"分块读 + 分块写"这个行为，不是 Starlette 的解析。
    真 `UploadFile` 的 `read(n)` 语义由 Starlette 自己的测试保证；
    而我们关心的是：**每次只问一小块、写出去之后就不再持有**。

    ⚠️ `max_chunk_seen` 是这组测试的关键 —— 它直接记录了
    "单次读了多少字节"。如果未来有人把 `item.read(_STREAM_CHUNK)`
    改回 `item.read()`，这个值会等于整个文件大小，测试会立刻红。
    """

    def __init__(self, data: bytes, *, name: str = "chall") -> None:
        self._data = data
        self._pos = 0
        self.filename = name
        #: 单次 `read(n)` 实际拿到的最大字节数 —— 内存占用的上界。
        self.max_chunk_seen = 0
        #: 调用了几次 `read`。
        self.read_calls = 0

    async def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        tail = size is None or size < 0
        chunk = self._data[self._pos :] if tail else self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        self.max_chunk_seen = max(self.max_chunk_seen, len(chunk))
        return chunk


@pytest.fixture
def work_root(tmp_path: Path, monkeypatch):
    """把扫描根目录指到 tmp，避免碰真实 `/tmp/suvex`。"""
    monkeypatch.setenv("SUPEX_SCAN_WORK_ROOT", str(tmp_path))
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_stream_writes_exact_bytes(work_root: Path) -> None:
    """★ 分块写出来的字节必须与输入**逐字节相同**。

    流式路径最容易出的错就是边界算错（少写/多写一块），
    而那种错误在"文件看起来存在、大小也对"的情况下极难发现。
    """
    payload = bytes(range(256)) * 40  # 10,240 字节，跨多个 1 MiB 之外
    fake = _FakeUpload(payload)
    name, size = asyncio.run(
        uploads.store_staged_stream(staging_id="stage_test", filename="chall", item=fake)
    )

    assert size == len(payload)
    assert name == "chall"
    on_disk = (work_root / "_uploads" / "_staging" / "stage_test" / name).read_bytes()
    assert on_disk == payload


def test_stream_never_reads_whole_file_at_once(work_root: Path) -> None:
    """★ 单次读的量不随文件变大 —— 这是把内存压住的那个机制本身。

    ⚠️ 这条是**反向验证过**的：把 `_write_stream` 里的
    `await item.read(_STREAM_CHUNK)` 改回 `await item.read()`，
    `max_chunk_seen` 会等于整个文件长度，断言立刻失败。
    """
    # 3 MiB，明显大于分块大小（1 MiB）
    payload = b"x" * (3 * 1024 * 1024)
    fake = _FakeUpload(payload)

    _, size = asyncio.run(
        uploads.store_staged_stream(staging_id="stage_big", filename="big.bin", item=fake)
    )

    assert size == len(payload)
    assert fake.max_chunk_seen <= uploads._STREAM_CHUNK, (
        f"单次读了 {fake.max_chunk_seen} 字节，超过分块大小 "
        f"{uploads._STREAM_CHUNK} —— 内存会随文件大小线性涨"
    )
    # 并且确实分了多块（否则说明上面那条是靠"一次读完但恰好小块"骗过的）
    assert fake.read_calls >= 3


def test_stream_rejects_oversize_and_removes_partial(work_root: Path, monkeypatch) -> None:
    """★ 超限时：报错 **且** 磁盘上不留半截文件。

    半截文件的危害不是占了几 MB 磁盘 —— 而是它看起来就像一个
    正常附件（`list_for` 只按名字列，不看完整性），用户会拿它
    去跑一次注定失败的扫描。
    """
    monkeypatch.setattr(uploads, "MAX_BYTES", 1024)
    fake = _FakeUpload(b"y" * 4096)

    with pytest.raises(ApiError) as excinfo:
        asyncio.run(
            uploads.store_staged_stream(staging_id="stage_over", filename="huge.bin", item=fake)
        )

    assert "上限" in excinfo.value.message
    # 目录可能建了，但**文件必须没了**
    leftovers = list((work_root / "_uploads" / "_staging" / "stage_over").glob("*"))
    assert leftovers == [], f"超限后留下了残骸：{leftovers}"


def test_stream_rejects_empty(work_root: Path) -> None:
    """空附件照旧拒收（与 `_write` 同口径）。"""
    fake = _FakeUpload(b"")

    with pytest.raises(ApiError) as excinfo:
        asyncio.run(
            uploads.store_staged_stream(staging_id="stage_empty", filename="empty", item=fake)
        )

    assert "空" in excinfo.value.message
    leftovers = list((work_root / "_uploads" / "_staging" / "stage_empty").glob("*"))
    assert leftovers == [], f"空文件不该留下：{leftovers}"


def test_stream_sanitizes_filename(work_root: Path) -> None:
    """流式路径的**文件名消毒**不能绕过 —— 与 `_write` 同一套。"""
    fake = _FakeUpload(b"data", name="../../etc/passwd")

    name, _ = asyncio.run(
        uploads.store_staged_stream(staging_id="stage_evil", filename=fake.filename, item=fake)
    )

    assert "/" not in name
    assert ".." not in name
    assert name == "passwd"


def test_stream_from_store_not_staged(work_root: Path) -> None:
    """`store_stream`（非暂存落点）也走同一套流式实现。"""
    payload = b"z" * 5000
    fake = _FakeUpload(payload, name="a.bin")

    name, size = asyncio.run(
        uploads.store_stream(scan_id="scan_stream", filename=fake.filename, item=fake)
    )

    assert size == len(payload)
    assert fake.max_chunk_seen <= uploads._STREAM_CHUNK
