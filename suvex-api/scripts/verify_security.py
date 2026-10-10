"""""任务 8 的**真实请求**验证：高风险操作的鉴权与审计留痕。

这是 `docs/security-checklist.md` 里"验收"那一栏指向的脚本 ——
验收原话是「高风险操作的鉴权和**审计留痕**都能在**真实请求**中验证到」，
而"真实请求"意味着这个脚本不碰数据库、不 import app：
它只发 HTTP，然后从 `GET /audit` 把留痕读回来。

⚠️ 与 `verify_acceptance.py` 的分工：那个脚本**自起后端**、跑在独立的
验收库上、覆盖任务 1~7 的端到端行为；这个脚本要求后端**已经在跑**
（比如你刚 `docker compose up` 完），因为它要验的正是"你现在部署的
这一份"的鉴权与留痕 —— 用另一套环境自起一个后端就失去意义了。

验收原话：
> 高风险操作的鉴权和**审计留痕**都能在真实请求中验证到。

所以这个脚本不碰数据库、不 import app —— 它只发 HTTP，然后从
`GET /audit` 把留痕读回来。这正是"能不能验证到"的字面含义：
如果审计只能靠 `psql` 看，那它就不是一个可验证的交付物。

用法（后端已在 127.0.0.1:8000 跑着）：
    python scripts\verify_security.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

API = "http://127.0.0.1:8000/api/v1"
STATEMENT = "我有权对此目标进行安全测试"


def _password() -> str:
    """管理员口令。**绝不写死在源码里。**

    ⚠️ 这里原来写死的是真实口令。这是我在任务 8 里犯的一个**自相矛盾**的错误：
    那一轮的主题就是"凭据不许出现在不该出现的地方"，而我把自己测试用的
    管理员口令提交进了一个入库的脚本 —— 一次 `git push` 就把它带出去了。

    取值顺序：
      1. 环境变量 `SUVEX_PASSWORD`（CI / 无人值守用这个）；
      2. 交互式提示（本地手跑）；
      3. 都没有 → 报错退出，**不猜**。

    不设"兜底默认值"：一个能跑通的默认口令等于把上面那段话当没说。
    """
    from_env = os.environ.get("SUVEX_PASSWORD", "")
    if from_env:
        return from_env
    if sys.stdin.isatty():
        import getpass

        return getpass.getpass("管理员口令（不回显）：")
    print(
        "缺少口令。请用环境变量传入，例如：\n"
        "    $env:SUVEX_PASSWORD = '<你的口令>'; python scripts\\verify_security.py\n"
        "（不提供默认值 —— 写死口令正是这个脚本原先的问题。）"
    )
    raise SystemExit(2)

ok_count = 0
fail_count = 0


def record(passed: bool, label: str, detail: str = "") -> None:
    global ok_count, fail_count
    mark = "[PASS]" if passed else "[FAIL]"
    if passed:
        ok_count += 1
    else:
        fail_count += 1
    print(f"  {mark} {label}")
    if detail:
        print(f"         {detail}")


class Client:
    """一个带 Cookie 罐的最小 HTTP 客户端（只用标准库）。"""

    def __init__(self) -> None:
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.token: str | None = None

    def request(self, method: str, path: str, body: dict | None = None, *, auth: str | None = None):
        url = f"{API}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", auth)
        try:
            with self.opener.open(req) as response:
                raw = response.read().decode("utf-8", "replace")
                return response.status, raw, dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers)


def main() -> int:
    password = _password()
    cli = Client()

    print("── 0. 建立会话 ──")
    status, raw, _ = cli.request("POST", "/auth/login", {"password": password})
    if status != 200:
        print(f"  登录失败（{status}）：{raw[:200]}")
        print("  请确认后端在跑、且口令是脚本里写的那一个。")
        return 2
    record(True, "管理员登录成功")

    # ── 1. 鉴权：无凭据 / 错误凭据 ─────────────────────────
    print("\n── 1. 高风险操作的鉴权（真实请求）──")
    anon = Client()
    for method, path, body in [
        ("POST", "/scans", {}),
        ("POST", "/scans/scan_x/abort", None),
        ("DELETE", "/reports/rpt_x", None),
    ]:
        code, payload, _ = anon.request(method, path, body)
        body_code = ""
        try:
            body_code = json.loads(payload)["error"]["code"]
        except Exception:  # noqa: BLE001
            pass
        record(
            code == 401 and body_code == "UNAUTHENTICATED",
            f"无凭据 {method} {path} → 401 UNAUTHENTICATED",
            f"实际 {code} {body_code}",
        )

    bad = Client()
    for header in ("Bearer not-a-token", "Bearer svx_nonexistent1234567890", "Basic eA=="):
        code, payload, _ = bad.request("POST", "/scans", {}, auth=header)
        record(code == 401, f"错误凭据 {header[:24]}… → 401", f"实际 {code}")

    # ── 2. 审计留痕：真实操作后从 /audit 读回来 ────────────
    print("\n── 2. 审计留痕（真实请求 → GET /audit）──")
    providers = json.loads(cli.request("GET", "/providers")[1])
    provider_id = next((p["id"] for p in providers if p["isDefault"]), None) or providers[0]["id"]

    marker = f"task8-live-{abs(hash(STATEMENT)) % 100000}"
    status, raw, _ = cli.request(
        "POST",
        "/scans",
        {
            "name": f"任务8 验证 {marker}",
            "target": f"{marker}.example.com",
            "targetKind": "url",
            "skills": ["web"],
            "providerId": provider_id,
            "authorization": {"confirmed": True, "statement": STATEMENT},
        },
    )
    record(status == 201, "发起扫描（真实请求）", f"HTTP {status}")
    scan = json.loads(raw) if status == 201 else {}

    if scan:
        code, payload, _ = cli.request("POST", f"/scans/{scan['id']}/abort")
        record(code == 200, "终止扫描（真实请求）", f"HTTP {code}")

    # 漏洞状态流转：找一条真实存在的漏洞
    findings = json.loads(cli.request("GET", "/findings")[1])
    if findings:
        victim = findings[0]
        target_status = "confirmed" if victim["status"] == "new" else "new"
        code, payload, _ = cli.request(
            "POST", "/findings/status", {"ids": [victim["id"]], "status": target_status}
        )
        changed = json.loads(payload).get("changed") if code == 200 else None
        record(code == 200 and changed == 1, "变更漏洞状态（真实请求）", f"HTTP {code} changed={changed}")
        # 还原，别把数据改成一半
        if code == 200:
            cli.request(
                "POST", "/findings/status", {"ids": [victim["id"]], "status": victim["status"]}
            )
    else:
        record(False, "变更漏洞状态（真实请求）", "库里没有漏洞，跳过 —— 不是通过")

    reports = json.loads(cli.request("GET", "/reports")[1])
    if reports:
        code, payload, headers = cli.request("GET", f"/reports/{reports[0]['id']}/download")
        record(code == 200, "下载报告（真实请求）", f"HTTP {code}")
    else:
        record(False, "下载报告（真实请求）", "库里没有报告，跳过 —— 不是通过")

    # ── 2b. CTF2：改一次配置，验证审计留痕 ─────────────────
    #
    # ★ 为什么挑"改配置"来验证 CTF2 的留痕，而不是"绑定账号"或"提交 flag"：
    #   · 绑定账号需要一把**真的** PAT —— 验证脚本里不能有凭据；
    #   · 提交 flag 会消耗平台侧**不可再生**的错误提交额度。
    #   而 `safe → auto` 恰好是 CTF2 这一组动作里后果最重的一步：
    #   它决定机器能不能在无人确认的情况下对外提交 flag。
    print("\n── 2b. CTF2 审计留痕（真实请求 → GET /audit）──")
    code, raw, _ = cli.request("GET", "/ctf2/settings")
    if code == 200:
        before = json.loads(raw)
        flipped = "auto" if before["submitMode"] == "safe" else "safe"
        body = {
            "submitMode": flipped,
            "maxWrongSubmissions": before["maxWrongSubmissions"],
            "defaultSandboxProfile": before["defaultSandboxProfile"],
        }
        code, _, _ = cli.request("PUT", "/ctf2/settings", body)
        record(
            code == 200,
            "改 CTF2 提交策略（真实请求）",
            f"HTTP {code} {before['submitMode']} → {flipped}",
        )
        # ★ 改回去。验证脚本**不该留下副作用** —— 它会把用户的提交策略
        #   留在 auto 上，而那意味着"机器可以无人确认地对外提交 flag"。
        #   一次验证把安全策略放宽了，比不验证更糟。
        code_back, _, _ = cli.request(
            "PUT", "/ctf2/settings", {**body, "submitMode": before["submitMode"]}
        )
        record(code_back == 200, "把 CTF2 提交策略改回原值", f"HTTP {code_back}")
    else:
        record(False, "改 CTF2 提交策略（真实请求）", f"GET /ctf2/settings → HTTP {code}")

    # ── 3. 读回审计 ────────────────────────────────────────
    code, raw, _ = cli.request("GET", "/audit?limit=50")
    record(code == 200, "GET /audit（admin 会话）", f"HTTP {code}")
    page = json.loads(raw) if code == 200 else {"entries": []}
    entries = page.get("entries", [])
    actions = {entry["action"] for entry in entries}

    record(bool(entries), "审计里确实有记录", f"共 {page.get('total')} 条")
    record("scan.create" in actions, "留痕：scan.create（谁对什么目标发起扫描）")
    record("scan.abort" in actions, "留痕：scan.abort（谁终止了扫描）")
    record("finding.status_change" in actions, "留痕：finding.status_change（谁变更了漏洞状态）")
    record("report.download" in actions, "留痕：report.download（谁下载了报告）")
    # CTF2（v0.20）：改提交策略是"机器能不能无人确认地对外提交"这件事的
    # 开关，所以它必须留痕 —— 而且留痕里要能看出改成了什么。
    record(
        "ctf2.settings_update" in actions,
        "留痕：ctf2.settings_update（谁把提交策略改成了什么）",
    )
    record(
        page.get("retentionDays") == 365,
        "保留策略在 API 里可见（365 天，长于应用日志轮转）",
        f"retentionDays={page.get('retentionDays')}",
    )

    # 每条都必须能回答"谁 / 什么时候 / 对什么"
    incomplete = [
        entry["id"]
        for entry in entries
        if not (entry.get("actorLabel") and entry.get("at") and entry.get("summary"))
    ]
    record(not incomplete, "每条留痕都能回答「谁 / 何时 / 做了什么」", f"缺字段的：{incomplete}")

    # 凭据不得出现在审计里
    blob = json.dumps(entries, ensure_ascii=False)
    leaked = [needle for needle in (password, "sk-", "postgresql://") if needle in blob]
    record(not leaked, "审计内容里没有凭据", f"命中：{leaked}")

    # ── 4. 审计只读 ────────────────────────────────────────
    print("\n── 3. 审计只追加（真实请求）──")
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        code, _, _ = cli.request(method, "/audit", {} if method != "DELETE" else None)
        record(code == 405, f"{method} /audit → 405（只允许 GET）", f"实际 {code}")

    print("\n" + "=" * 56)
    print(f"合计 {ok_count + fail_count} 项：通过 {ok_count}，失败 {fail_count}")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
