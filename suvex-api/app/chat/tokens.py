"""DeepSeek 的离线精确 token 计数（移植自 OMP 的 utok）。

数据来自 OMP 的 `deepseek3.bin.zst`（UTOK1 容器），词表是 DeepSeek V3..V4
共用的 base BPE。预切分按 OMP `scan/deepseek.rs` 的三阶段 Split(Isolated)
链：数字 1..3 位、CJK 连续段、主模式。没有网络、没有运行时下载。

未安装 `regex` / `zstandard` 或读不到数据时回退到字符/4；调用方通过
`exact` 知道这次是不是精确值。
"""

from __future__ import annotations

import functools
import importlib.util
from pathlib import Path

_DATA = Path(__file__).with_name("data") / "deepseek3.bin.zst"
_STAGE_DIGITS = r"\p{N}{1,3}"
_STAGE_CJK = r"[一-龥぀-ゟ゠-ヿ]+"
_STAGE_MAIN = (
    r"[!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~][A-Za-z]+"
    r"|[^\r\n\p{L}\p{P}\p{S}]?[\p{L}\p{M}]+"
    r"| ?[\p{P}\p{S}]+[\r\n]*"
    r"|\s*[\r\n]+"
    r"|\s+(?!\S)"
    r"|\s+"
)


def _available() -> bool:
    return (
        _DATA.is_file()
        and importlib.util.find_spec("regex") is not None
        and importlib.util.find_spec("zstandard") is not None
    )


@functools.lru_cache(maxsize=1)
def _ranks() -> dict[bytes, int] | None:
    if not _available():
        return None
    import zstandard

    raw = zstandard.ZstdDecompressor().decompress(_DATA.read_bytes())
    if not raw.startswith(b"UTOK1\n"):
        return None
    pos = 6
    (count,) = int.from_bytes(raw[pos : pos + 4], "little"), 
    pos += 4
    table: dict[bytes, int] = {}
    for rank in range(count):
        length = 0
        shift = 0
        while True:
            byte = raw[pos]
            pos += 1
            length |= (byte & 0x7F) << shift
            if byte < 0x80:
                break
            shift += 7
        token = raw[pos : pos + length]
        pos += length
        if token:
            table[token] = rank
    return table


@functools.lru_cache(maxsize=1)
def _patterns():
    import regex

    return tuple(regex.compile(p) for p in (_STAGE_DIGITS, _STAGE_CJK, _STAGE_MAIN))


def _split_once(text: str, pattern) -> list[str]:
    out: list[str] = []
    last = 0
    for match in pattern.finditer(text):
        if match.start() > last:
            out.append(text[last : match.start()])
        if match.end() > match.start():
            out.append(match.group())
        last = match.end()
    if last < len(text):
        out.append(text[last:])
    return out


def _pieces(text: str) -> list[str]:
    pieces = [text]
    for pattern in _patterns():
        nxt: list[str] = []
        for piece in pieces:
            nxt.extend(_split_once(piece, pattern))
        pieces = nxt
    return pieces


_PIECE_CACHE: dict[bytes, int] = {}


def _piece_count(piece: str, ranks: dict[bytes, int]) -> int:
    data = piece.encode("utf-8")
    cached = _PIECE_CACHE.get(data)
    if cached is not None:
        return cached
    if not data:
        return 0
    if data in ranks:
        return 1
    symbols = [bytes((b,)) for b in data]
    while len(symbols) > 1:
        best_rank = None
        best = -1
        for i in range(len(symbols) - 1):
            rank = ranks.get(symbols[i] + symbols[i + 1])
            if rank is not None and (best_rank is None or rank < best_rank):
                best_rank = rank
                best = i
        if best < 0:
            break
        symbols[best : best + 2] = [symbols[best] + symbols[best + 1]]
    # 单进程长期运行时，工具输出会带来海量唯一片段；无界 dict 是内存泄漏。
    if len(_PIECE_CACHE) >= 65_536:
        _PIECE_CACHE.clear()
    _PIECE_CACHE[data] = len(symbols)
    return len(symbols)


def count_deepseek(text: str) -> int | None:
    """精确 DeepSeek token 数；依赖/数据不可用时返回 None。"""
    ranks = _ranks()
    if ranks is None:
        return None
    return sum(_piece_count(piece, ranks) for piece in _pieces(text or ""))


def count_tokens(text: str, model: str | None = None) -> tuple[int, bool]:
    """返回 `(tokens, exact)`；非 DeepSeek/不可用时字符/4。"""
    if model and "deepseek" in model.lower():
        exact = count_deepseek(text)
        if exact is not None:
            return exact, True
    return max(1, len(text or "") // 4), False


__all__ = ["count_deepseek", "count_tokens"]
