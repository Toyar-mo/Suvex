"""对着**真实的** CTF2 Open API 探测路由与响应字段名。

## 为什么需要它

`app/ctf2/client.py` 里的每个端点路径、以及每个响应的**字段名**
（`_first_str(payload, ("id", "practice_id"))` 那种），都是照着
别人的接口写的。照抄的东西会漂移，而且漂移的方式很安静：

* 端点被改路径 → 我们收到 404，而 `service._translate` 会把它报成
  "平台接口可能变更了"（对，但不知道**哪一个**变了）；
* 字段被改名 → `_pick` 返回 `None`，于是某道题的标题变成 `#1234`、
  分值是空的 —— 看起来像"平台没给这些数据"，而不是"我们读错了键"。

这个脚本把"到底长什么样"变成一次可重复的执行结果。

## 两种模式

**匿名（没有 PAT）**：靠状态码区分"路由存在"与"路由不存在" ——
平台对存在的路由会先做鉴权（401/400），对不存在的路由给
`404 + {"error":{"resource":"route"}}`。这一条已经实测过，
所以脚本里把它写成**结论**，同时留着重跑的能力。

**带 PAT**：额外把每个端点的**真实响应**顶层字段打出来，
用来核对 `client.py` 的解析假设。

## ⚠️ 凭据怎么给

只从环境变量 `CTF2_PAT` 读。**不接受命令行参数** —— 命令行会进
shell 历史，而它是一把能代表用户去操作平台的钥匙。

也不会打印它（只打前 4 位与长度，用来确认"读到了哪一把"）。

用法：

    python scripts/probe_ctf2_endpoints.py            # 匿名探测
    CTF2_PAT=pat_xxx python scripts/probe_ctf2_endpoints.py
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

for stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):  # pragma: no cover
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

import httpx  # noqa: E402

BASE = "https://ctf2.dasctf.com/api/open/v1/user"
TIMEOUT = 12.0

# 一个 UUID 形状的占位 id。**形状要紧**：平台对不合法的路径参数会先给
# 400（参数校验在鉴权之前），于是"存在但参数不对"与"存在且要鉴权"
# 分不开 —— 而这一条实测过：`123456` 得到 400，UUID 得到 401。
PROBE_ID = "11111111-1111-1111-1111-111111111111"

# client.py 里**声明**的端点（路径 + 方法）。这里手抄一遍是刻意的：
# 脚本要与代码对账，就不能从代码里读（那样两边一起错时它永远是绿的）。
DECLARED: list[tuple[str, str]] = [
    ("GET", "/practice/"),
    ("GET", f"/practice/{PROBE_ID}/challenges/{PROBE_ID}/"),
    ("POST", f"/practice/{PROBE_ID}/challenges/{PROBE_ID}/submit/"),
    ("POST", f"/practice/{PROBE_ID}/challenges/{PROBE_ID}/environment/start/"),
    ("GET", "/competitions/"),
    ("GET", f"/competitions/{PROBE_ID}/status/"),
    ("GET", f"/competitions/{PROBE_ID}/stages/"),
    ("GET", f"/stages/{PROBE_ID}/challenges/"),
    ("GET", f"/stages/{PROBE_ID}/submissions/"),
]

# 曾经猜过、实测**不存在**的路径。留在这里是为了防止将来有人
# "顺手补一条对称的路径"（见 `service._assert_submission_supported`
# 的说明：猜错的代价是把一次没送达的提交记成已提交）。
REJECTED: list[tuple[str, str]] = [
    ("POST", f"/competitions/{PROBE_ID}/challenges/{PROBE_ID}/submit/"),
    ("POST", f"/stages/{PROBE_ID}/challenges/{PROBE_ID}/submit/"),
    ("POST", f"/competitions/{PROBE_ID}/submit/"),
    ("POST", f"/stages/{PROBE_ID}/submit/"),
    ("GET", f"/competitions/{PROBE_ID}/challenges/"),
    ("GET", f"/practice/{PROBE_ID}/"),
    ("GET", f"/practice/{PROBE_ID}/challenges/"),
]


def probe(client: httpx.Client, method: str, path: str) -> tuple[str, str]:
    """返回 `(判定, 说明)`。

    判定的三种取值：

    * `exists` —— 路由存在（401/400/403 都说明平台走到了鉴权/参数校验）；
    * `missing` —— 路由不存在（404，且 body 里 resources 指的是 route）；
    * `unknown` —— 看不懂的响应（比如 5xx、非 JSON）。
    """
    try:
        response = client.request(method, f"{BASE}{path}", json={} if method == "POST" else None)
    except httpx.HTTPError as exc:
        return "unknown", f"网络失败：{exc.__class__.__name__}"

    status = response.status_code
    if status in (400, 401, 403, 405, 422):
        return "exists", f"{status}"
    if status == 404:
        try:
            body = response.json()
        except ValueError:
            return "missing", "404（非 JSON）"
        detail = json.dumps(body, ensure_ascii=False)
        if "route" in detail or "not_found" in detail.lower():
            return "missing", "404 route"
        return "missing", f"404 {detail[:80]}"
    if status < 400:
        return "exists", f"{status}（匿名就能读！）"
    return "unknown", f"{status} {response.text[:80]}"


def dump_fields(client: httpx.Client, path: str, *, limit: int = 3) -> None:
    """把一个列表端点的**真实字段名**打出来。

    只打**键集合**，不打值 —— 值里可能有别人的数据，而我们要的
    只是"解析时该读哪个键"。
    """
    try:
        response = client.get(f"{BASE}{path}")
    except httpx.HTTPError as exc:
        print(f"    {path} → 网络失败 {exc.__class__.__name__}")
        return
    if response.status_code >= 400:
        print(f"    {path} → HTTP {response.status_code}")
        return
    try:
        payload = response.json()
    except ValueError:
        print(f"    {path} → 非 JSON：{response.text[:80]}")
        return

    items = payload
    wrapped = ""
    if isinstance(payload, dict):
        for key in ("results", "items", "data", "list"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                wrapped = f"（外壳键 `{key}`，顶层键 {sorted(payload)[:8]}）"
                break
        else:
            print(f"    {path} → 顶层键：{sorted(payload)[:12]}")
            return

    print(f"    {path} → {len(items)} 条{wrapped}")
    for item in items[:limit]:
        if isinstance(item, dict):
            print(f"      · 字段：{sorted(item)}")


def main() -> int:
    token = os.environ.get("CTF2_PAT", "").strip()
    if token:
        print(f"凭据：环境变量 CTF2_PAT 已读到（{token[:4]}…，长度 {len(token)}）")
    else:
        print("凭据：没有设 CTF2_PAT —— 只做**匿名**路由探测。")

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    failures = 0

    with httpx.Client(timeout=TIMEOUT, headers=headers, follow_redirects=False) as client:
        print("\n── client.py 里声明的端点（应当全部存在）──")
        for method, path in DECLARED:
            verdict, detail = probe(client, method, path)
            mark = "OK " if verdict == "exists" else "!! "
            if verdict != "exists":
                failures += 1
            print(f"  [{mark}] {method:4} {path} → {verdict} {detail}")

        print("\n── 实测不存在的路径（**不要**照着它们写代码）──")
        for method, path in REJECTED:
            verdict, detail = probe(client, method, path)
            mark = "OK " if verdict == "missing" else "!! "
            if verdict != "missing":
                failures += 1
            print(f"  [{mark}] {method:4} {path} → {verdict} {detail}")

        if token:
            print("\n── 真实响应字段名（用来核对 client.py 的解析假设）──")
            dump_fields(client, "/practice/")
            dump_fields(client, "/competitions/")

    print()
    if failures:
        print(f"⚠️ 有 {failures} 项与预期不符 —— 平台的契约可能变了，先看上面带 !! 的行。")
    else:
        print("全部与预期一致。")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
