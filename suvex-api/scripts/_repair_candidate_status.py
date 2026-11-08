"""一次性修复：把「未开始 + 有候选 + 已取消。」的行还原成 `candidate_ready`。

## 为什么这些行是错的

`competition_racer._persist_single_result` 曾经把"跑完、有候选"写成
`status=solving`（代码已修）。界面因此显示「解题中」，用户点了取消 ——
而取消只改状态、不清候选，最后落成这行谁也不懂的数据。

现在的口径是：**有候选 = 等人确认**，所以把这些行还原成 `candidate_ready`
并清掉那句错误的原因。只动同时满足三个条件（未开始 / 有候选 /
原因正好是「已取消。」）的行 —— 其余「未开始 + 有候选」是合法状态
（提交时平台回"本队伍已解出"那条路径会刻意保留候选）。

用法：`python scripts/_repair_candidate_status.py [--apply]`
不给 `--apply` 只打印。
"""

from __future__ import annotations

import sys

from sqlalchemy import text

from app.core.db import session_scope

SELECT = text(
    """
    select id, remote_id, title, candidate_flag
    from suvex_challenges
    where status = 'unsolved'
      and candidate_flag is not null
      and last_error = '已取消。'
    order by updated_at desc
    """
)

UPDATE = text(
    """
    update suvex_challenges
       set status = 'candidate_ready',
           last_error = null
     where status = 'unsolved'
       and candidate_flag is not null
       and last_error = '已取消。'
    """
)


def main() -> int:
    apply = "--apply" in sys.argv
    with session_scope() as db:
        rows = db.execute(SELECT).fetchall()
        print(f"命中 {len(rows)} 道：")
        for row in rows:
            print(f"  {row.remote_id:>8}  {row.title[:28]:<30}  {row.candidate_flag}")
        if not apply:
            print("\n（只读。加 --apply 才会写库）")
            return 0
        if not rows:
            return 0
        result = db.execute(UPDATE)
        db.commit()
        print(f"\n已更新 {result.rowcount} 行 → candidate_ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
