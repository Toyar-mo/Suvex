"""探测一个**未知的** CTF 平台 API：认证方式、端点、题目形状、提交路径。

## 为什么需要这个脚本

`app/ctf2/` 这套东西在比赛时要接到另一个平台上 —— 而那个平台的 API
在拿到 key 之前**没人知道长什么样**。没有这个脚本，接入的第一步是
"对着文档猜"，或者在浏览器里手工试十几条路径。

有了它，接入的第一步变成：

    export PLATFORM_BASE_URL=https://比赛平台/api/v1
    export PLATFORM_KEY=赛事方给的key
    python scripts/probe_platform.py

三十秒后拿到一份报告：认证怎么带、哪些端点活着、题目列表的字段叫什么、
提交路径是哪一条。

## ★ 纪律：默认只读

`--allow-write` 之前**不发任何 POST/PUT/DELETE**。原因是这个脚本第一次
跑的时候，我们对平台的语义一无所知 —— 一条猜出来的 `POST /submit`
可能真的提交一个答案（并计一次错误次数），而错误次数在 CTF 里是
**不可再生资源**。

## ★ 纪律：凭据只从环境变量读，且只回显前 4 位

key 不会出现在命令行里（`ps`/history 会留下它），也不会被完整打印。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from typing import Any

import httpx

for stream in (sys.stdout, sys.stderr):
    # 中文与符号输出在 Windows 控制台里默认会是乱码（cp936 编不出来就抛），
    # 而这个脚本很可能**就是在比赛现场的 Windows 上第一次跑** ——
    # "探测器自己先炸了"比它探不到东西难查得多。
    with contextlib.suppress(AttributeError, ValueError):  # pragma: no cover
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

# ── 认证方式的候选（按常见程度排序）────────────────────────
#
# 为什么要把它们排成一个表：认证是**接入失败最常见的第一个原因**，
# 而它的症状极具迷惑性（401 看起来像"key 不对"，实际是"key 没带对地方"）。
# 一次把四种都试一遍，能把这一整类问题在三十秒内排掉。
AUTH_STYLES: dict[str, Any] = {
    # ★ 这一条排在最前，因为它是**已知的**比赛平台认证方式
    #   （`api_doc.md` 的「认证 Header」：`X-Agent-AccessKey`）。
    #   排前面只是省一点探测时间 —— 命中的判据没有放宽，仍然是
    #   "一个匿名被拒的端点，带上它之后通了"。
    "x-agent-accesskey": lambda key: {"X-Agent-AccessKey": key},
    "bearer": lambda key: {"Authorization": f"Bearer {key}"},
    "token": lambda key: {"Authorization": f"Token {key}"},
    "x-api-key": lambda key: {"X-API-Key": key},
    "api-key": lambda key: {"api-key": key},
    "plain-auth": lambda key: {"Authorization": key},
}

# ── 候选端点 ───────────────────────────────────────────────
#
# ★ 这张表里**短路径与长路径混着放**，是刻意的：不同平台给的 base_url
#   层级不同 —— CTF2 那种是 `https://ctf2.dasctf.com/api/open/v1/user`
#   （已经带了三段前缀），而别的平台可能只给域名。所以每个候选都会
#   按两种拼法各试一次（`base + path` 与 `origin + path`），哪个活取哪个。
#
#   `GET {base}/practice/` 在 CTF2 上是对的；`GET {origin}/api/v1/challenges/`
#   在 CTFd 上是对的。两种拼法兼顾，就不必让用户先去猜该给哪一级地址。
CANDIDATE_ENDPOINTS: tuple[str, ...] = (
    "",
    "/me",
    "/profile/",
    "/whoami",
    "/status/",
    "/challenges/",
    "/challenges",
    "/tasks/",
    "/problems/",
    "/questions/",
    "/practice/",
    "/writeups/",
    "/daily/",
    "/competitions/",
    "/events/",
    "/stages/",
    "/rounds/",
    "/scoreboard/",
    "/teams/",
    "/users/me",
    "/user/",
    "/api/",
    "/api/v1/",
    "/api/v1/challenges/",
    "/api/v1/competitions/",
    "/api/v1/events/",
    "/api/v1/tasks/",
    "/api/v1/scoreboard/",
    "/api/v1/teams/",
    "/api/v1/users/me",
    "/api/v1/status/",
    # ── ★ 比赛平台（`api_doc.md`）的路径 ────────────────────
    #
    # 带上它们，探测器在**那个**平台上能直接给出"题目列表在哪、
    # 能不能取到题"的结论 —— 而那正是比赛当天最先要回答的问题。
    #
    # 短的对应"base_url 已经含 agent 前缀"（文档给的形状），
    # 长的对应"只给了域名"（人更可能那么填）。
    "/ctf/exercise-list",
    "/answer-panel/overview",
    "/match/notice/match-info",
    "/slab-match/api/v1/agent/ctf/exercise-list",
)

# ── 认证自检用的候选端点 ───────────────────────────────────
#
# ★ 顺序有讲究：把"需要登录才能看"的端点排在前面（`/practice/`、
#   `/competitions/`、`/me`），因为认证探测必须建立在一个**匿名会被拒**
#   的端点上。
AUTH_PROBE_PATHS: tuple[str, ...] = (
    "/practice/",
    "/competitions/",
    "/challenges/",
    "/stages/",
    "/writeups/",
    "/me",
    "/api/v1/users/me",
    "/api/v1/me",
    "/api/v1/challenges/",
    "/user/",
    "/profile/",
)

# 从响应里找 id 时认的字段名（按优先级）。
ID_KEYS = ("id", "challenge_id", "challengeId", "uuid", "slug", "code")

# 题目标题可能叫的名字 —— 用来判断"这个列表是不是题目列表"。
TITLE_KEYS = ("title", "name", "challenge_name")

# 平台指纹：命中即说明"这是一个已知平台"，接入难度会低一个数量级。
FINGERPRINTS: tuple[tuple[str, str], ...] = (
    ("ctf_version", "CTFd"),
    ("ctfd", "CTFd"),
    ("rctf", "rCTF"),
    ("dasctf", "DASCTF/CTF2"),
    ("flag_format", "带 flag 格式声明的自研平台"),
)


def _clip(value: Any, limit: int = 220) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _shape(value: Any, depth: int = 0) -> Any:
    """把一个 JSON 响应压成"字段名骨架"，用来回答"字段叫什么"。

    为什么不直接打印原始响应：一个题目列表可能有几十道题、每道题几十个
    字段 —— 打印出来没人看，而接入时真正需要的只是**字段名**。
    """
    if depth > 3:
        return "…"
    if isinstance(value, dict):
        return {k: _shape(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [_shape(value[0], depth + 1)] if value else []
    return type(value).__name__


def _items(payload: Any) -> list[dict[str, Any]]:
    """从各种"包裹了一层"的响应里取出条目数组。

    平台把列表塞在 `items` / `data` / `results` / `list` 里都很常见，
    而"我以为它返回数组、其实外面包了一层"是接入时最常见的第一次失败。
    """
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("items", "data", "results", "list", "challenges", "records"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
        for value in payload.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return [x for x in value if isinstance(x, dict)]
    return []


def _pick_id(item: dict[str, Any]) -> str | None:
    for key in ID_KEYS:
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    return None


def _flatten_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把"分类 → corpus"的两级列表展平。

    ★ 比赛平台（`api_doc.md` §4）的题目列表是**两级**的：外层是分类
    （`{id, name, order, corpus: [...]}`），题目在 `corpus` 里。

    不展平的话，探测器会把**分类名**当成题目标题，然后报告
    "样例字段：id/name/order/corpus" —— 那个结论是错的，而且它会让人
    以为题目信息就在列表里（其实题面、分值、附件都在 §5 的详情端点）。

    展平后顺带把分类名挂成 `__group`，读报告时能看出这道题属于哪一类。
    """
    flat: list[dict[str, Any]] = []
    for item in items:
        corpus = item.get("corpus")
        if isinstance(corpus, list) and corpus:
            group_name = str(item.get("name") or "")
            for sub in corpus:
                if isinstance(sub, dict):
                    merged = dict(sub)
                    merged.setdefault("__group", group_name)
                    flat.append(merged)
            continue
        flat.append(item)
    return flat


def _looks_like_challenges(items: list[dict[str, Any]]) -> bool:
    if not items:
        return False
    return any(any(k in item for k in TITLE_KEYS) for item in items[:5])


class Probe:
    def __init__(
        self, base_url: str, key: str, *, timeout: float = 12.0, min_interval: float = 0.25
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # `origin` = 协议 + 主机（丢掉路径）。见候选端点表那段说明：
        # 每个候选都要按 `base + path` 与 `origin + path` 各试一次。
        parsed = httpx.URL(self.base_url)
        self.origin = f"{parsed.scheme}://{parsed.host}" + (
            f":{parsed.port}" if parsed.port else ""
        )
        self.key = key
        # 最近一次成功的 URL（多拼法探测时用来回答"到底哪条地址是对的"）。
        # ⚠️ 用实例属性而不是返回值传递，是为了不动五六个调用点的签名 ——
        #    这是个一次性脚本，隐式状态在这里的代价小于改五处的风险。
        self._last_url = self.base_url
        self.client = httpx.Client(timeout=timeout, follow_redirects=True)
        # ★ 限速不是可选项：一次探测要打 30+ 个候选端点，而真实平台几乎
        #   都有速率限制。不限速的后果不是"探测失败"，而是**把用户这个
        #   账号短暂打进 429** —— 而比赛期间那是要用来提交答案的额度。
        #   0.25 秒 = 4 req/s，比 CTF2 的礼貌线（5 req/s）再保守一点。
        self._min_interval = min_interval
        self._last_request = 0.0
        self.auth_headers: dict[str, str] = {}
        self.auth_style = ""
        self.report: dict[str, Any] = {
            "baseUrl": self.base_url,
            "authStyle": None,
            "fingerprint": None,
            "endpoints": {},
            "challengeSource": None,
            "challengeSample": None,
            "submitCandidates": [],
            "notes": [],
        }

    # ── 基础请求 ────────────────────────────────────────────

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.monotonic()

    @staticmethod
    def _is_json(response: httpx.Response) -> bool:
        """这个响应是 API 的 JSON，还是前端页面？"""
        return "json" in response.headers.get("content-type", "").lower()

    def _rank(self, response: httpx.Response) -> int:
        """给响应打分，用来在"两种拼法"之间选优。

        ★ 200 **只在带 JSON 时**才算命中 —— 这是实测撞出来的：
          首次用 CTF2 冒烟时，脚本报了 20 个"活着的端点"（`/challenges/`、
          `/tasks/`、`/scoreboard/`……），而 CTF2 一个都没有。根因是
          **SPA 的 catch-all 路由**：前端单页应用对任意未知路径都返回
          200 + `text/html`。只看状态码的话，所有猜出来的路径都会"命中"，
          而照着它写出来的驱动会全军覆没 —— 而且失败得很晚（等到提交
          那一步才发现）。
        """
        if response.status_code in (401, 403):
            # 路由存在、只是没权限。对"这个端点叫什么"同样是有效信息。
            return 1
        if response.status_code == 200 and self._is_json(response):
            return 2
        return 0

    def _urls_for(self, path: str) -> list[str]:
        """一个候选路径的两种拼法（`base + path` 与 `origin + path`）。"""
        primary = f"{self.base_url}{path}"
        secondary = f"{self.origin}{path}"
        return [primary] if secondary == primary else [primary, secondary]

    def _get(self, path: str, headers: dict[str, str] | None = None) -> httpx.Response | None:
        """打一个候选路径。**两种拼法都试，取状态码最好的那个。**

        判优顺序是 200 > 401/403 > 其他：
        `401/403` 说明**路由存在、只是没权限**（那对"这个端点叫什么"
        同样是有效信息），而 404 什么都不说明。
        """
        best: httpx.Response | None = None
        best_url = ""
        best_rank = -1
        for url in self._urls_for(path):
            self._throttle()
            try:
                response = self.client.get(
                    url,
                    headers=headers if headers is not None else self.auth_headers,
                )
            except httpx.HTTPError as exc:
                self.report["notes"].append(f"{url} 请求失败：{type(exc).__name__}")
                continue
            rank = self._rank(response)
            if rank > best_rank:
                best, best_url, best_rank = response, url, rank
            if rank == 2:
                break
        if best is not None:
            self._last_url = best_url
        return best

    def _describe(self, response: httpx.Response) -> dict[str, Any]:
        """把一次响应压成可读的形状（状态码 + 内容类型 + 字段骨架）。"""
        entry: dict[str, Any] = {"url": self._last_url, "status": response.status_code}
        content_type = response.headers.get("content-type", "")
        entry["contentType"] = content_type.split(";")[0]
        if "json" not in content_type.lower():
            entry["body"] = _clip(response.text)
            return entry
        try:
            payload = response.json()
        except ValueError:
            entry["body"] = _clip(response.text)
            return entry
        entry["shape"] = _shape(payload)
        # ★ "路由不存在"与"资源不存在"的区分，是判断端点是否活着的关键。
        #   CTF2 用 `params.resource == "route"` 表达前者，别的平台可能用
        #   `detail: Not Found` 或 `error.code`。这里把可疑字段原样留下，
        #   由人（或下一个驱动）判断。
        if isinstance(payload, dict):
            for key in ("detail", "error", "message", "msg", "code", "params"):
                if key in payload:
                    entry.setdefault("hints", {})[key] = _clip(payload[key], 120)
        return entry

    # ── ① 认证 ──────────────────────────────────────────────

    def discover_auth(self) -> None:
        """找出这个平台认哪种认证方式。

        ★ 判据不能是"某个端点返回 200"，有两个理由：

          ① 公开端点**不带 key 也返回 200**（scoreboard、题目列表都可能公开）；
          ② SPA 的 catch-all 对任意路径都返回 200 + HTML。

        所以流程是：先找一个**匿名访问会被拒（401/403）**的端点 ——
        那才证明"这个端点确实要求认证" —— 再逐个认证方式试它。
        找不到这样的端点时，结论只能是"没能确认"，而不是随便挑一个
        200 就宣布成功。

        ⚠️ 首次冒烟时这里选错了自检端点（`/me` 匿名返回 200），于是
           "认证命中"这条结论其实是空的 —— 这类假阳性比探测失败更危险，
           因为它会让人以为接入已经成功了一半。
        """
        print("── ① 认证方式 ──", flush=True)

        baseline: tuple[str, int] | None = None
        fallback: tuple[str, int] | None = None
        for path in AUTH_PROBE_PATHS:
            response = self._get(path, headers={})
            if response is None:
                continue
            self.report["endpoints"][path] = self._describe(response)
            if response.status_code in (401, 403):
                baseline = (path, response.status_code)
                break
            if fallback is None and response.status_code == 200 and self._is_json(response):
                fallback = (path, response.status_code)

        if baseline is None:
            if fallback is None:
                self.report["notes"].append(
                    "既没有需要认证的端点，也没有公开的 JSON 端点 —— "
                    "base_url 可能不对（或者这个平台根本不是一个 HTTP API）"
                )
                print("  ✗ 没找到可用于自检的端点")
                return
            baseline = fallback
            self.report["notes"].append(
                f"没有任何候选端点匿名返回 401/403，因此**无法确认认证是否真的生效** —— "
                f"下面的判定只说明「带上 key 能访问 {fallback[0]}」"
            )
            print(f"  ⚠️ 没有需要认证的端点，退而用 {fallback[0]}（匿名也是 200）")

        path, anon_status = baseline
        print(f"  自检端点：{path}（不带 key → {anon_status}）")

        for style, builder in AUTH_STYLES.items():
            response = self._get(path, headers=builder(self.key))
            if response is None:
                continue
            status = response.status_code
            if status == 200 and self._is_json(response):
                self.auth_headers = builder(self.key)
                self.auth_style = style
                # ★ 认证头**绝不进报告**（它是凭据），只记风格名。
                self.report["authStyle"] = style
                print(f"  ✔ 命中：{style}（{path} → 200）")
                return
            print(f"    · {style} → {status}")

        self.report["notes"].append(
            "所有候选认证方式都没能让自检端点返回 200 —— "
            "请确认 key 是否有效，或平台是否用别的头（比如签名、HMAC）"
        )
        print("  ✗ 没有命中任何一种 —— 见报告 notes")

    # ── ② 端点扫描 ──────────────────────────────────────────

    def scan_endpoints(self) -> None:
        print("\n── ② 端点扫描 ──", flush=True)

        # ★ 认证阶段探过的端点记的是**匿名**结果（401/403），而"活着"
        #   应该反映**带上认证之后**的可达性。
        #
        #   不重探的后果实测过两次：`/practice/`（CTF2 上最重要的端点，
        #   匿名 401 / 带 key 200）一直不出现在列表里，而列表里只有
        #   `/profile/`、`/teams/` 这几个无关紧要的 —— 看报告的人会得出
        #   "这个平台没有题目接口"的结论，而那正好是**反的**。
        for path, described in list(self.report["endpoints"].items()):
            if described.get("status") not in (401, 403):
                continue
            response = self._get(path)
            if response is None:
                continue
            self.report["endpoints"][path] = self._describe(response)

        alive: list[str] = []
        for path in CANDIDATE_ENDPOINTS:
            described = self.report["endpoints"].get(path)
            if described is not None:
                # 已经探过（认证阶段打过它）—— **别把它丢下**。
                #
                # ⚠️ 这一条原来漏了，后果是 `/practice/`（CTF2 最重要的
                #    那个端点）没有出现在"活着的端点"列表里 —— 因为它
                #    在认证阶段就被探过了。列表少一个"最重要的"而多几个
                #    无关的，看报告的人会以为这个平台没有题目接口。
                if described.get("status") == 200 and "json" in described.get(
                    "contentType", ""
                ).lower():
                    alive.append(path)
                continue
            response = self._get(path)
            if response is None:
                continue
            if response.status_code >= 400 and response.status_code not in (401, 403):
                # 400 段里只有 401/403 值得记（它们说明端点在、只是没权限）。
                # 404 太多，只在明确"路由不存在"的语义下才有信息量 ——
                # 那一条已经在下面按指纹判过了。
                continue
            described = self._describe(response)
            self.report["endpoints"][path] = described
            # ★ 只有 **200 + JSON** 才算"这个端点活着"（见 `_rank` 的说明）。
            if response.status_code == 200 and self._is_json(response):
                alive.append(path)

        print(f"  活着的端点 {len(alive)} 个：")
        for path in alive:
            print(f"    ✔ {path}")
        skipped = [
            path
            for path, described in self.report["endpoints"].items()
            if described.get("status") == 200 and "json" not in described.get("contentType", "").lower()
        ]
        if skipped:
            print(f"  （{len(skipped)} 条 200 但不是 JSON —— 那是前端页面，不是 API）")

        self._fingerprint()

    def _fingerprint(self) -> None:
        """认平台指纹 —— 命中"这是 CTFd"能省掉一整轮猜测。"""
        blob = json.dumps(self.report["endpoints"], ensure_ascii=False).lower()
        for needle, name in FINGERPRINTS:
            if needle.lower() in blob:
                self.report["fingerprint"] = name
                print(f"  ★ 指纹命中：{name}")
                return

    # ── ③ 题目形状 ──────────────────────────────────────────

    def discover_challenges(self) -> None:
        """找出"题目列表"在哪，以及它的字段叫什么。

        判据是**形状**而不是路径名：返回的条目里有 `title`/`name` 且有
        `id`，就当它是题目列表。这比维护一张"哪些路径是题目列表"的表
        可靠 —— 平台的名字可以叫 challenges / tasks / problems / questions。
        """
        print("\n── ③ 题目来源 ──", flush=True)
        best: tuple[str, list[dict[str, Any]], Any] | None = None

        for path, described in self.report["endpoints"].items():
            if described.get("status") != 200 or "shape" not in described:
                continue
            response = self._get(path)
            if response is None:
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            items = _flatten_groups(_items(payload))
            if not _looks_like_challenges(items):
                continue
            if best is None or len(items) > len(best[1]):
                best = (path, items, payload)

        if best is None:
            hint = ""
            if any("writeup" in path for path in self.report["endpoints"]):
                # ★ 这条提示是**用真实平台换来的经验**，值得写进工具里：
                #   CTF2 没有"列出题目"的端点（带真 PAT 试过 11 条候选路径，
                #   全是 `404 resource=route`），但 `/writeups/` 是活的 ——
                #   每条公开解法都带 `challenge_id`。拿到 id 再逐个"容器"
                #   （练习集/赛事/阶段）试详情端点，实测 98 个 id 全中。
                #
                #   所以"没找到题目列表"在带 writeups 的平台上**不等于**
                #   "拉不到题" —— 这条提示就是把那个区别讲清楚。
                hint = (
                    "。但 `/writeups/` 是活的 —— 试试「解法侧门」："
                    "公开解法每条带着 challenge_id，拿 id 逐个容器试详情端点"
                )
            self.report["notes"].append(
                "没有找到「看起来像题目列表」的端点 —— "
                "这个平台可能要求先选赛事/阶段，或者题目藏在别的路径下" + hint
            )
            print("  ✗ 没找到题目列表（见报告 notes）")
            return

        path, items, _ = best
        self.report["challengeSource"] = path
        self.report["challengeSample"] = _shape(items[0]) if items else None
        print(f"  ✔ {path}（{len(items)} 条）")
        print(f"    样例字段：{_clip(list(items[0].keys()), 200) if items else '（空）'}")

        self._probe_detail_and_submit(path, items)

    def _probe_detail_and_submit(self, path: str, items: list[dict[str, Any]]) -> None:
        """拿一个**真实 id** 去探详情与提交端点。

        ★ 这是整个脚本最有价值的一步：没有真 id 时，`/challenges/{id}/`
        这种路径只能靠猜，而猜出来的 404 分不清"路由不存在"和"id 不存在"。
        有了真 id，200 就是"这条路径是对的"。
        """
        sample_id = next((_pick_id(item) for item in items if _pick_id(item)), None)
        if sample_id is None:
            self.report["notes"].append("题目条目里没有可识别的 id 字段，无法继续探详情/提交")
            return

        base = path.rstrip("/")
        candidates = [
            f"{base}/{sample_id}/",
            f"{base}/{sample_id}",
            f"/api/v1/challenges/{sample_id}/",
            f"/challenges/{sample_id}/",
        ]
        print(f"\n  用真实 id {sample_id} 探详情：")
        for candidate in candidates:
            if candidate in self.report["endpoints"]:
                continue
            response = self._get(candidate)
            if response is None:
                continue
            if response.status_code in (200, 401, 403):
                self.report["endpoints"][candidate] = self._describe(response)
                print(f"    ✔ {candidate} → {response.status_code}")

        # ★ 提交端点只**列出候选**，不主动打 —— 见模块 docstring 的纪律。
        #   即使带了 `--allow-write`，这里也只打印"下一步该试哪几条"，
        #   真正的提交探测应该由人来决定（它会计一次错误次数）。
        submit_candidates = [
            f"{base}/{sample_id}/submit/",
            f"{base}/{sample_id}/submit",
            f"/api/v1/challenges/{sample_id}/submit/",
            "/api/v1/submit/",
            "/submit/",
            "/api/v1/flags/",
        ]
        self.report["submitCandidates"] = submit_candidates
        print("\n  提交端点候选（**未探测**，需要真提交才能确认）：")
        for candidate in submit_candidates:
            print(f"    ? {candidate}")

    # ── 输出 ────────────────────────────────────────────────

    def run(self, *, report_path: str | None = None) -> int:
        print(f"平台：{self.base_url}")
        print(f"Key：{self.key[:4]}…（长度 {len(self.key)}）\n")
        self.discover_auth()
        self.scan_endpoints()
        self.discover_challenges()

        print("\n── 报告 ──", flush=True)
        if report_path:
            with open(report_path, "w", encoding="utf-8") as fh:
                json.dump(self.report, fh, ensure_ascii=False, indent=2)
            print(f"  已写入 {report_path}")

        notes = self.report["notes"]
        if notes:
            print("  需要人工判断的地方：")
            for note in notes:
                print(f"    · {note}")

        # 退出码表达"探测成功到什么程度"，便于串进别的脚本：
        #   0 = 认证 + 题目来源都找到了（可以开始写驱动）
        #   2 = 只摸到一部分（还要人工看）
        if self.report["authStyle"] and self.report["challengeSource"]:
            print("\n结论：认证与题目来源都摸到了 —— 可以开始写驱动了。")
            return 0
        print("\n结论：只摸到一部分，请把报告发给 AI 一起看。")
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="探测未知 CTF 平台 API 的形状")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("PLATFORM_BASE_URL", ""),
        help="平台根地址（默认读环境变量 PLATFORM_BASE_URL）",
    )
    parser.add_argument(
        "--key",
        default=os.environ.get("PLATFORM_KEY", ""),
        help="API key（默认读环境变量 PLATFORM_KEY；**不建议写在命令行里**）",
    )
    parser.add_argument("--report", default="_tmp/platform-probe.json", help="报告写到哪里")
    args = parser.parse_args()

    if not args.base_url or not args.key:
        print(
            "缺参数。用法：\n"
            "  export PLATFORM_BASE_URL=https://平台/api/v1\n"
            "  export PLATFORM_KEY=赛事方给的key\n"
            "  python scripts/probe_platform.py",
            file=sys.stderr,
        )
        return 1

    print(
        "⚠️ 这个脚本从设计上**不发任何写请求** —— 提交端点只列候选，\n"
        "   由人手工试一次（它会计一次错误提交，那是不可再生资源）。",
        flush=True,
    )

    return Probe(args.base_url, args.key).run(report_path=args.report)


if __name__ == "__main__":
    sys.exit(main())
