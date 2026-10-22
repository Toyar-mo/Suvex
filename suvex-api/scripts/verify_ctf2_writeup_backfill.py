#!/usr/bin/env python
"""回填**前**的自检：证明重建出来的题解是完整的（不脱敏）。

## 为什么必须先跑它

`writeup.build_writeup` 内部走
`notes.key_steps_from_transcript(..., sanitize=False)` —— 题解**不脱敏**。
如果哪天有人把那个参数改回默认 `True`（或绕过 `build_writeup` 自己拼），
题解会**少了靶机地址与 flag**，而它**看起来仍然生成成功**
（有标题、有轮数、有过程纪要，只是关键步骤变成了空壳）。
那正是「看起来对了但实际残缺」那类问题。

所以这里对着**真实数据**断言两件事：

1. **flag 在题解正文里**（这批题都是 `solved`，`confirmed_flag` 库里都有）；
2. **靶机地址在题解正文里**（从 transcript 里抓一个真实出现的 host，
   断言它出现在题解里 —— 脱敏版会把它掩成 `<target>`）。

跑法（Kali 上）：

    .venv/bin/python scripts/verify_ctf2_writeup_backfill.py
"""

from __future__ import annotations

import contextlib
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):  # pragma: no cover
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from sqlalchemy import select  # noqa: E402

from app.core.db import session_scope  # noqa: E402
from app.core.vocab import KernelChallengeStatus  # noqa: E402
from app.kernel.models import KernelChallenge  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from backfill_ctf2_writeups import (  # noqa: E402
    _elapsed_sec,
    _parse_outcome,
    _transcript_for,
)

from app.kernel import writeup as writeup_mod  # noqa: E402

#: 从 transcript 里抓一个真实 host（脱敏版会把它掩掉）。
_HOST = re.compile(r"https?://([a-zA-Z0-9][a-zA-Z0-9.\-]{6,})")

#: 题解里如果出现这个，说明走了脱敏路径 —— 那是失败。
MASK_MARKERS = ("<target>", "<host>", "<url>")


def main() -> int:
    checked = 0
    bad_flag: list[str] = []
    bad_host: list[str] = []
    masked: list[str] = []

    with session_scope() as db:
        challenges = list(
            db.execute(
                select(KernelChallenge)
                .where(KernelChallenge.status == KernelChallengeStatus.SOLVED.value)
                .order_by(KernelChallenge.solved_at)
            )
            .scalars()
            .all()
        )

        for challenge in challenges:
            path = _transcript_for(challenge.id)
            if path is None:
                continue
            transcript = path.read_text(encoding="utf-8", errors="replace")
            outcome = _parse_outcome(transcript)
            md = writeup_mod.build_writeup(
                challenge=challenge,
                outcome=outcome,
                solved=True,
                elapsed_sec=_elapsed_sec(db, challenge),
            )
            checked += 1

            # ① flag 必须在（这批题都有 confirmed_flag）
            flag = challenge.confirmed_flag
            if flag and flag not in md:
                bad_flag.append(f"{challenge.title}（flag={flag[:24]}…）")

            # ② 靶机地址必须在
            host = None
            for m in _HOST.finditer(transcript):
                candidate = m.group(1)
                # 跳过明显不是靶机的（文档域名、示例域名）
                if candidate.endswith((".com", ".cn", ".org", ".net")) and "dasctf" not in candidate:
                    continue
                host = candidate
                break
            if host and host not in md:
                bad_host.append(f"{challenge.title}（host={host}）")

            # ③ 不能出现脱敏标记
            for marker in MASK_MARKERS:
                if marker in md:
                    masked.append(f"{challenge.title}（含 {marker}）")
                    break

    print(f"检查了 {checked} 道题的题解生成\n")
    print(f"① flag 缺失：{len(bad_flag)}")
    for item in bad_flag[:10]:
        print(f"     {item}")
    print(f"② 靶机地址缺失：{len(bad_host)}")
    for item in bad_host[:10]:
        print(f"     {item}")
    print(f"③ 出现脱敏标记：{len(masked)}")
    for item in masked[:10]:
        print(f"     {item}")

    ok = not bad_flag and not bad_host and not masked
    print(f"\n{'PASS：题解完整（flag 与靶机地址都在，无脱敏标记）' if ok else 'FAIL：见上面'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
