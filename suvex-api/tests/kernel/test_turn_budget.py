"""按题型的最低回合/墙钟预算。

实测（2026-11-09）：RE5（二维码）/ RE6（Go 逆向）各跑满 500 轮仍未解出。
RE5 2623s / 500 轮 ≈ 5.2 秒/轮；RE6 4370s / 500 轮 ≈ 8.7 秒/轮 ——
500 轮对这两类题是先撞回合上限，而不是先撞墙钟（RE6 已到 81%）。

这组测试钉住三件事：

1. reverse/crypto 的下限是 800 轮，且**前缀匹配**（平台会写 `REVRESE`）；
2. 这两类题的墙钟下限是 2 小时（800 轮按 5–9 秒/轮需要 70–120 分钟）；
3. 其它题型不受影响，全局墙钟上限仍是 90 分钟。
"""

from __future__ import annotations

import inspect

from app.core import vocab
from app.kernel import runner, service


def test_reverse_and_crypto_get_800_turns() -> None:
    assert vocab.turn_budget_floor("reverse") == 800
    assert vocab.turn_budget_floor("REVRESE") == 800  # 平台拼写不规范
    assert vocab.turn_budget_floor("crypto") == 800
    assert vocab.turn_budget_floor("pwn") == 0


def test_wall_clock_floor_only_for_hard_categories() -> None:
    assert vocab.wall_clock_floor("rev") == 7200
    assert vocab.wall_clock_floor("crypto") == 7200
    assert vocab.wall_clock_floor("misc") == 0


def test_global_wall_clock_cap_is_unchanged() -> None:
    # 800 轮 × 45s 会被全局成本闸夹到 90 分钟；题型特例在 runner 里另加，
    # 不篡改全局默认值。
    assert vocab.default_ctf2_wall_clock_seconds(800) == vocab.MAX_CTF2_WALL_CLOCK_SECONDS


def test_start_solve_applies_the_turn_floor() -> None:
    """源码级断言：受理时写入 attempt 的回合数经过 floor。"""
    source = inspect.getsource(service.start_solve)
    assert "turn_budget_floor" in source


def test_build_spec_applies_the_wall_clock_floor() -> None:
    source = inspect.getsource(runner.build_spec)
    assert "wall_clock_floor" in source
    assert "turn_budget_floor" in source
