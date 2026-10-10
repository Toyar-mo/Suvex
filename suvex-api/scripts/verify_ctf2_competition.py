"""开赛自检：**这套 API 到底能不能用来打比赛？**

## 为什么需要它

比赛规则写的是「仅开放统一 API 接口，参赛队伍需通过 API 获取题目信息、
访问题目环境、提交 Flag」—— 整条链路都建立在"这套 API 做得到这三件事"
之上。而这件事**从来没有被验证过**。

已经确定的事实（2026-10-01 实测，见 `probe_ctf2_endpoints.py` 的
`REJECTED`，以及 `_tmp/probe_competition_api.mjs`）：

* CTF2 公开 Open API 的**写入端点只有 practice 路径**：
  `POST /practice/{id}/challenges/{challengeId}/submit/` 与
  `.../environment/start/`；
* **stage 下的提交/环境端点不存在**（试过 `/stages/{id}/challenges/{id}/submit/`
  与 `/competitions/...` 两种猜法，都是 404 route）；
* `/private-practice/` 在当前账号下**是空的**；
* 真正的比赛（`/competitions/`）列得出来，但 `/competitions/{id}/stages/`
  返回 403 `TEAM_MEMBERSHIP_REQUIRED`（战队没注册那场）。

于是"比赛题怎么提交"是一处**未经验证的关键假设**。这个脚本把它变成
一次可重复的执行结果：**开赛拿到 key 之后跑一遍，5 分钟内知道能不能用。**

## 它怎么分辨"路径不存在"与"路径存在但没权限"

平台对**不存在的路由**给 `404 + {"error": {"resource": "route"}}`；
对**存在但缺权限**的给 401/403；对**存在但参数不合法**的给 400/422。
这条差异在 `probe_ctf2_endpoints.py` 里已经用匿名请求验证过，
所以这里把它当成判据，而不是猜。

⚠️ 路径探测**故意用形状合法但不存在的 UUID**（见 `PROBE_ID`）。
用 `123456` 会先撞参数校验得到 400，于是"路径不存在"与"参数不对"
分不开 —— 那个坑实测踩过，注释留在原脚本里。

## ⚠️ 凭据怎么给

只从环境变量 `CTF2_PAT` 读（**也接受 `CTF2_BASE_URL` 覆盖平台地址**）。
**不接受命令行参数** —— 命令行会进 shell 历史，而它是一把能代表
用户去操作平台的钥匙。也不会打印它（只打前 4 位与长度）。

## ⚠️ 默认**不做**真实提交

提交会消耗**不可再生**的错误提交机会。所以真实提交探测要显式加
`--probe-submit`，**并且必须同时给出 `--practice-id` 与 `--challenge-id`**
—— 只有你知道哪道题可以随便试。（用具名参数而不是"自动挑一道"，
是因为自动挑会把一次真实提交打到一道**你并不打算牺牲**的题上。）

★★ **最省的探测法：挑一道你已经解出的题。**

实测（2026-10-01）拿一道已解出的题去探测，平台回
`400 {"error": {"code": "ALREADY_SOLVED"}}` —— 这个回答证明了三件事：
请求真的送达了、路径与请求体都对（否则走不到"这题已解出"这一层）、
平台确实会给判定；而它**不消耗**任何错误提交机会（根本没判对错）。
一次探测同时验完"通不通"与"有没有反馈"，代价是零。

用法：

    # 只做只读自检（默认，不消耗任何额度）
    CTF2_PAT=xxx python scripts/verify_ctf2_competition.py

    # 额外做一次真实提交探测（挑一道已解出的题，代价为零）
    CTF2_PAT=xxx python scripts/verify_ctf2_competition.py \
        --probe-submit --practice-id <容器id> --challenge-id <题目id>

退出码：0 = 三件事（列题/起环境/提交）都有着落；1 = 有缺口，
具体卡在哪一步看报告里的「结论」。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

for stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):  # pragma: no cover
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

import httpx  # noqa: E402

DEFAULT_BASE = "https://ctf2.dasctf.com"
API_PREFIX = "/api/open/v1/user"
TIMEOUT = 15.0

# 形状合法但**必定不存在**的 UUID。形状要紧 —— 见模块 docstring。
PROBE_ID = "11111111-1111-1111-1111-111111111111"

# ── 结论的三种取值（与 `probe_ctf2_endpoints.py` 同一套词汇）──
EXISTS = "存在"
MISSING = "不存在"
UNKNOWN = "看不懂"

# 每条：(方法, 路径, 说明)。说明写"这条回答什么问题"，
# 而不是"这是什么端点" —— 报告是给人做决策用的。
CANDIDATE_ROUTES: list[tuple[str, str, str]] = [
    # ① 比赛题的容器到底在哪
    ("GET", "/competitions/", "能看到哪些比赛"),
    ("GET", "/private-practice/", "私有练习场（比赛题最可能的容器）"),
    ("GET", "/practice/", "公开练习场"),
    # ② 比赛题的读路径
    ("GET", f"/competitions/{PROBE_ID}/stages/", "比赛 → 阶段"),
    ("GET", f"/stages/{PROBE_ID}/", "阶段详情"),
    ("GET", f"/stages/{PROBE_ID}/challenges/", "阶段 → 题目列表"),
    # ③ 提交与环境：三条候选路径，看哪条真的存在
    ("POST", f"/stages/{PROBE_ID}/challenges/{PROBE_ID}/submit/", "提交（stage 形状）"),
    ("POST", f"/competitions/{PROBE_ID}/challenges/{PROBE_ID}/submit/", "提交（competition 形状）"),
    ("POST", f"/practice/{PROBE_ID}/challenges/{PROBE_ID}/submit/", "提交（practice 形状）"),
    ("POST", f"/private-practice/{PROBE_ID}/challenges/{PROBE_ID}/submit/", "提交（private 形状）"),
    ("POST", f"/stages/{PROBE_ID}/challenges/{PROBE_ID}/environment/start/", "起环境（stage 形状）"),
    ("POST", f"/practice/{PROBE_ID}/challenges/{PROBE_ID}/environment/start/", "起环境（practice 形状）"),
    # ④ 对账用：提交记录能不能读
    ("GET", f"/stages/{PROBE_ID}/submissions/", "阶段提交记录"),
    ("GET", "/submissions/", "我的全部提交记录"),
]


def probe(client: httpx.Client, base: str, method: str, path: str) -> tuple[str, str]:
    """判定一条路由是否存在。返回 `(判定, 说明)`。

    ## ★★ 判据：404 里分两种，靠 `error.params.resource` 分开

    这是本脚本**最容易写错**的一处，实测（2026-10-01）把形状钉死了：

    | 情形 | HTTP | `error.params.resource` |
    | --- | --- | --- |
    | 路由存在，但那个 id 不存在 | 404 | `practice_ground` / `competition` / `stage` … |
    | **路由不存在** | 404 | 恒为 **`route`** |
    | 路由存在，参数不合法 | 400 | （无） |
    | 路由存在，缺权限 | 401 / 403 | — |

    所以判据是 **`resource == "route"` 才算不存在**，而不是"看到 404 就算"。
    最初这里写成"body 里没有 route 字样就当不存在"，于是把
    `POST /practice/{id}/challenges/{id}/environment/start/` 判成了
    「不存在」—— **而那条路我们真的用过**（起过几十次环境）。
    一个反了的判据会让报告谎称"平台没这个功能"，比赛当天足以让人
    放弃一条本来走得通的路。

    ⚠️ 另外把"存在但没权限"（401/403）判成「存在」是**刻意**的：
       那正是我们想知道的 —— 路由在，只是这个账号还够不着。
       混进「不存在」会让人误以为平台没做这个功能。
    """
    try:
        started = time.monotonic()
        response = client.request(
            method,
            f"{base}{API_PREFIX}{path}",
            json={} if method == "POST" else None,
        )
        elapsed = time.monotonic() - started
    except httpx.HTTPError as exc:
        return UNKNOWN, f"网络失败：{exc.__class__.__name__}"

    status = response.status_code
    if status in (400, 401, 403, 405, 422):
        return EXISTS, f"HTTP {status}（{elapsed * 1000:.0f}ms）"
    if status == 404:
        resource = None
        try:
            body = response.json()
            params = (body.get("error") or {}).get("params") or {}
            resource = params.get("resource")
        except (ValueError, AttributeError):
            body = None
        if resource == "route":
            return MISSING, "404 route（平台说这条路由不存在）"
        if isinstance(resource, str) and resource:
            return EXISTS, f"404 但 resource={resource}（路由在，id 不对）"
        return MISSING, "404（读不出 resource，保守判为不存在）"
    if status < 400:
        return EXISTS, f"HTTP {status}（{elapsed * 1000:.0f}ms）"
    if status == 429:
        return UNKNOWN, "429 限流（稍后重跑）"
    return UNKNOWN, f"HTTP {status} {response.text[:70]}"


def get_json(client: httpx.Client, base: str, path: str) -> tuple[int, Any, str]:
    """读一个端点，返回 `(状态码, 解析后的 data, 原文)`。不抛。"""
    try:
        response = client.get(f"{base}{API_PREFIX}{path}")
    except httpx.HTTPError as exc:
        return 0, None, f"网络失败：{exc.__class__.__name__}"
    text = response.text
    try:
        payload = response.json()
    except ValueError:
        return response.status_code, None, text[:200]
    return response.status_code, payload.get("data", payload), text[:400]


def items_of(data: Any) -> list[dict[str, Any]]:
    """把列表响应的三种包法抹平（与 `client._as_list` 同一套形状）。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("items", "results", "list", "records", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def pick(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) not in (None, ""):
            return row[key]
    return None


def section(title: str) -> None:
    print(f"\n{'─' * 66}\n{title}\n{'─' * 66}")


def main() -> int:
    parser = argparse.ArgumentParser(description="CTF2 比赛可用性自检")
    parser.add_argument(
        "--probe-submit",
        action="store_true",
        help="额外做一次**真实**提交探测（会消耗一次错误提交机会，默认关）",
    )
    parser.add_argument(
        "--practice-id",
        default="",
        help="真实提交探测的目标容器 id（配合 --probe-submit）",
    )
    parser.add_argument(
        "--challenge-id",
        default="",
        help="真实提交探测的目标题目 id（配合 --probe-submit）",
    )
    args = parser.parse_args()

    token = (os.environ.get("CTF2_PAT") or "").strip()
    if not token:
        print("缺少 CTF2_PAT。用法：CTF2_PAT=xxx python scripts/verify_ctf2_competition.py")
        return 1
    base = (os.environ.get("CTF2_BASE_URL") or DEFAULT_BASE).strip().rstrip("/")

    print(f"平台：{base}")
    # 只打前 4 位与长度 —— 用来确认"读到了哪一把"，不足以还原它
    print(f"凭据：{token[:4]}…（{len(token)} 字符）")
    print(f"真实提交探测：{'开' if args.probe_submit else '关（默认）'}")

    with httpx.Client(
        timeout=TIMEOUT,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        follow_redirects=False,
    ) as client:
        # ── ① 鉴权 ────────────────────────────────────────
        section("① 凭据与身份")
        status, data, raw = get_json(client, base, "/profile/")
        if status != 200:
            print(f"  ✗ /profile/ → HTTP {status}：{raw[:160]}")
            print("\n结论：凭据不可用，后面的检查没有意义。")
            return 1
        who = pick(data if isinstance(data, dict) else {}, "username", "friendly_id", "id")
        role = pick(data if isinstance(data, dict) else {}, "role")
        print(f"  ✓ 身份：{who}（role={role}）")

        # ── ② 路由存在性 ──────────────────────────────────
        section("② 路由存在性（这是最该先看的一张表）")
        routes: dict[str, str] = {}
        for method, path, label in CANDIDATE_ROUTES:
            verdict, note = probe(client, base, method, path)
            routes[f"{method} {path}"] = verdict
            mark = {"存在": "✓", "不存在": "✗", "看不懂": "?"}[verdict]
            print(f"  {mark} {label:<26} {verdict:<6} {note}")
            time.sleep(0.25)  # 主动限速：这一屏会打十几次请求

        submit_routes = [
            k for k in routes if k.endswith("/submit/") and routes[k] == EXISTS
        ]
        env_routes = [
            k
            for k in routes
            if k.endswith("/environment/start/") and routes[k] == EXISTS
        ]
        print()
        print(f"  可用的提交路径：{len(submit_routes)} 条")
        for r in submit_routes:
            print(f"    · {r}")
        print(f"  可用的起环境路径：{len(env_routes)} 条")
        for r in env_routes:
            print(f"    · {r}")

        # ── ③ 比赛与阶段 ──────────────────────────────────
        section("③ 比赛与阶段（比赛题的入口）")
        status, data, raw = get_json(client, base, "/competitions/")
        competitions = items_of(data)
        print(f"  /competitions/ → HTTP {status}，{len(competitions)} 场")
        for c in competitions[:10]:
            print(
                f"    · {str(pick(c, 'name', 'title') or '?')[:30]:<32}"
                f" id={pick(c, 'id')}"
            )

        stage_total = 0
        stage_blocked = 0
        for c in competitions[:8]:
            cid = pick(c, "id")
            if not cid:
                continue
            st_status, st_data, st_raw = get_json(client, base, f"/competitions/{cid}/stages/")
            stages = items_of(st_data)
            name = str(pick(c, "name", "title") or "?")[:24]
            if st_status == 403:
                stage_blocked += 1
                print(f"    {name:<26} 阶段 ✗ 403（战队没注册这场比赛）")
                continue
            print(f"    {name:<26} 阶段 {len(stages)} 个")
            stage_total += len(stages)
            time.sleep(0.3)

        # ── ④ 题目列表：这一条决定"能不能批量做题" ─────────
        section("④ 题目列表（能不能批量拿到题）")
        challenge_total = 0
        sampled: list[dict[str, Any]] = []
        for c in competitions[:8]:
            cid = pick(c, "id")
            if not cid:
                continue
            st_status, st_data, _ = get_json(client, base, f"/competitions/{cid}/stages/")
            if st_status != 200:
                continue
            for s in items_of(st_data)[:6]:
                sid = pick(s, "id")
                if not sid:
                    continue
                ch_status, ch_data, ch_raw = get_json(
                    client, base, f"/stages/{sid}/challenges/"
                )
                challenges = items_of(ch_data)
                sname = str(pick(s, "name", "title") or sid)[:22]
                if ch_status == 200:
                    challenge_total += len(challenges)
                    print(f"    {sname:<24} 题目 {len(challenges)} 道")
                    for ch in challenges[:3]:
                        sampled.append(ch)
                else:
                    print(f"    {sname:<24} 题目 ✗ HTTP {ch_status} {ch_raw[:60]}")
                time.sleep(0.3)

        # 练习场这边也报一下，好和比赛做对比
        p_status, p_data, _ = get_json(client, base, "/practice/")
        practices = items_of(p_data)
        print(f"    （对照）公开练习场 {len(practices)} 个，"
              f"平台侧题量合计 {sum(int(pick(x, 'challenge_count') or 0) for x in practices)}")

        pp_status, pp_data, _ = get_json(client, base, "/private-practice/")
        private = items_of(pp_data)
        print(f"    （关键）私有练习场 {len(private)} 个")
        for x in private[:6]:
            print(
                f"      · {str(pick(x, 'name', 'title') or '?')[:26]:<28}"
                f" id={pick(x, 'id')} 题量={pick(x, 'challenge_count')}"
            )

        # ── ⑤ 题目详情里有什么（决定起环境与提交怎么做）──
        section("⑤ 题目详情：起环境与提交需要的字段")
        if sampled:
            ch = sampled[0]
            cid = pick(ch, "id")
            print(f"  样本题：{str(pick(ch, 'name', 'title') or '?')[:40]}  id={cid}")
            for key in (
                "has_container",
                "requires_running_target_for_submit",
                "flag_completion_mode",
                "total_sub_flag_count",
                "sub_flags",
                "max_attempts",
                "allows_multiple_flag_submissions",
                "is_solved",
            ):
                if key in ch:
                    print(f"    {key} = {json.dumps(ch[key], ensure_ascii=False)[:80]}")
            missing = [
                k
                for k in ("has_container", "flag_completion_mode", "total_sub_flag_count")
                if k not in ch
            ]
            if missing:
                print(f"    ⚠ 列表里缺这些字段（要看详情才有）：{', '.join(missing)}")
        else:
            print("  （没拿到任何比赛题，这一步跳过）")

        # ── ⑥ 可选：真实提交探测 ──────────────────────────
        section("⑥ 真实提交探测")
        if not args.probe_submit:
            print("  已跳过（默认关）。要跑请加 --probe-submit 并指定题目。")
            print("  ⚠️ 它会真的发一次提交，消耗一次**不可再生**的错误提交机会。")
        elif not (args.practice_id and args.challenge_id):
            print("  要跑真实提交，必须同时给出 --practice-id 与 --challenge-id。")
            print("  为什么必须显式指定：只有你知道哪道题可以随便试。")
            print("  提示：若 `/private-practice/` 出现了条目，就用它的 id 填 --practice-id，")
            print("        再从那道题的列表里拿一个 challenge id。")
        else:
            # 答案故意是**一眼假**的值：不可能是任何题的正确答案，
            # 同时形状上也不冒充 flag —— 免得被误读成"我们以为这是 flag"。
            probe_answer = f"suvex-selfcheck-{int(time.time())}"
            path = (
                f"/practice/{args.practice_id}"
                f"/challenges/{args.challenge_id}/submit/"
            )
            print(f"  POST {path}")
            print(f"  answer = {probe_answer}（一眼假，不会撞上任何正确答案）")
            try:
                response = client.post(
                    f"{base}{API_PREFIX}{path}",
                    json={"flag": probe_answer, "confirmation": True},
                )
            except httpx.HTTPError as exc:
                print(f"  ✗ 网络失败：{exc.__class__.__name__}")
            else:
                print(f"  HTTP {response.status_code}")
                print(f"  响应：{response.text[:400]}")
                print()
                code = None
                resource = None
                with contextlib.suppress(ValueError, AttributeError):
                    err = response.json().get("error") or {}
                    code = err.get("code")
                    resource = (err.get("params") or {}).get("resource")

                if response.status_code < 400:
                    print("  ✅ 提交成功送达，平台给了判定 —— 这条路**可用**。")
                elif code == "ALREADY_SOLVED":
                    # ★★ 这是本脚本最想要的结果，而且它**零成本**。
                    #
                    # 实测（2026-10-01，拿一道已解出的题做探测）：平台回
                    # `400 {"error":{"code":"ALREADY_SOLVED"}}`。它证明了三件事：
                    #   ① 请求**真的送达了**平台（不是网络问题、不是路径 404）；
                    #   ② 路径与请求体都**正确**（否则不会走到"这题已解出"这一层）；
                    #   ③ 平台**给了明确判定**（我们拿得到反馈）。
                    # 而它**不消耗**任何错误提交机会 —— 因为根本没判对错。
                    #
                    # 所以：**优先挑一道已经解出的题来做这个探测。**
                    print("  ✅ 路径**可用**，而且这次探测**零成本**。")
                    print("     平台回 ALREADY_SOLVED —— 说明请求真的送达了、")
                    print("     路径与请求体都对（否则走不到这一步），而且没有判对错，")
                    print("     所以**没有消耗**任何错误提交机会。")
                    print("     → 这就是做提交探测的**最优选法**：挑一道已解出的题。")
                elif resource == "route":
                    print("  ✗ 404 route：这条提交路径在当前平台**不存在**。")
                    print("     → 说明比赛的 API 不是这一套，去要文档。")
                elif resource:
                    print(f"  ✗ 路径在，但 resource={resource} 找不到")
                    print("     → 多半是 --practice-id / --challenge-id 填错了。")
                elif code == "RATE_LIMIT_EXCEEDED":
                    print("  ? 被限流了。路径本身没问题，稍后重跑这一次探测。")
                else:
                    print(f"  ? 路径在、请求也到了，但平台拒绝了（code={code}）。")
                    print("     → 看上面的响应原文：这通常正是**答案格式**或请求字段")
                    print("       与平台预期不一致的地方，比赛时值得逐字对齐。")

    # ── 结论 ─────────────────────────────────────────────
    section("结论：三种容器，各自能做什么")

    can_list = challenge_total > 0
    can_env = len(env_routes) > 0
    can_submit = len(submit_routes) > 0
    stage_readable = stage_total > 0 or stage_blocked > 0
    # ⚠️ 只看 `pp_status`。这里最初写成 `pp_status == 200 and status == 200`，
    #    而 `status` 是**上一个** get_json 的返回值（/competitions/ 的）——
    #    拿一个过期变量当判据，正是这一类脚本最容易悄悄写错的地方。
    private_readable = pp_status == 200

    # ★ 这张表是整个脚本唯一要回答的东西：能力不是"API 有没有"，
    #   而是"哪一种容器 × 哪一个动作"。
    print("  · practice（公开练习场）")
    print(
        f"      读题目 ✓   起环境 {'✓' if can_env else '✗'}   "
        f"提交 {'✓' if can_submit else '✗'}"
        f"        共 {len(practices)} 个，平台侧题量合计 "
        f"{sum(int(pick(x, 'challenge_count') or 0) for x in practices)}"
    )

    print("  · competition / stage（比赛题）")
    print(
        f"      读题目 {'✓' if stage_readable else '✗'}   起环境 ✗   提交 ✗"
    )
    print("      ⚠️ **读得到、写不了**：比赛题的提交与环境端点在这套 API 里")
    print("         **不存在**（试过 stage 与 competition 两种形状，都是 404 route）。")
    if stage_blocked:
        print(f"      （另有 {stage_blocked} 场比赛的 stage 读不到：403 战队未注册）")

    print("  · private-practice（私有练习场）")
    print(f"      读题目 {'✓' if private_readable else '✗'}（当前 {len(private)} 个）")
    print("      起环境 ?   提交 ?")
    print("      **路径形状待验证**：`/private-practice/{id}/…` 不存在，")
    print("      但 `/practice/{id}/…` 存在 —— 它能不能接受私有练习场的 id，")
    print("      要等那里真的有数据才测得出来。")

    print()
    if can_list and can_env and can_submit:
        print("  ✅ 三件事都有着落 —— 这套 API 可以支撑比赛流程。")
        print("     下一步：把实际用到的路径填进 client.py，再跑一次端到端解题。")
        return 0

    print("  ❌ 还不能直接用。按要紧程度排：")
    print()
    print("  ① **比赛题没有写入路径**（最要紧）")
    print("     比赛规则要求「通过 API 提交 Flag」，而这套 API 的写入端点")
    print("     只有 practice 形状。比赛题挂在 stage 下，而 stage 的提交/环境")
    print("     端点都不存在。所以现在**即便拿到题目也交不了**。")
    print("     → 必须向赛事方确认比赛用哪套接口、索取文档与示例请求。")
    print("       在拿到确认之前**不要**去猜一条对称路径 —— 猜错的代价是把")
    print("       一次没送达的提交记成已提交（见 service._assert_submission_supported）。")
    print()
    if not can_list:
        print("  ② **比赛题也读不到**")
        if stage_blocked:
            print("     成因是 403（战队未注册该场比赛），这通常不是 API 的问题：")
            print("     → 先确认账号已加入战队、且战队已报名；开赛后重跑本脚本。")
        else:
            print("     可能是比赛还没开始（阶段未开放）—— 开赛后重跑本脚本。")
        print()
    print("  ③ 有一条**可能**的出路（先别当结论）")
    print("     `/private-practice/` 是比赛题容器的最可能候选：它有 /submissions/")
    print("     端点，说明那里确实发生过提交；而 `/practice/{id}/…` 这条路是通的。")
    print("     → 一旦 `/private-practice/` 有条目，把它的 id 塞进 practice 形状的")
    print("       submit 路径试一次（`--probe-submit`），用结果回答，别用推断。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
