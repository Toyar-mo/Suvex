"""盯扫描进度：状态、日志里的工具调用计数、record_fact 出现次数。

## 这一轮要回答的问题

v0.80 那次实测 `record_fact` **0 次**。v0.83 加了记账欠费提醒，
这轮就是看它到底有没有用。所以这里**只数三样东西**：

  1. `record_fact` 被调几次
  2. 其它工具被调几次（判断总量，避免"只跑了 3 步就说没用"）
  3. 日志里有没有出现 `6 轮没有把结论写进` —— **这是判据的关键**：
     出现了说明提醒真的注入了；如果 record_fact 仍为 0 但提醒确实出现过，
     那才说明"催了也没用"，方向真的错。反之如果提醒一次都没出现，
     说明计数器根本没到阈值，测试就是无效的（不是模型不听话）。

## 用法

    python _kali_watch_scan.py <scan_id> [--tail N]

结果写文件再由人看（GBK 环境下 print 中文会乱码）。
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys

#: 日志里数工具调用的正则。格式见上一轮的实测输出：
#:     [tool_call] run_shell ...
TOOL_RE = re.compile(r"\[tool_call\]\s+(\S+)")
#: 欠费提醒里那句独一无二的文案（前面可能有 "⚠️ **你已经"）。
NUDGE_RE = re.compile(r"你已经\s*(\d+)\s*轮没有把结论写进项目黑板")
#: record_fact 的工具返回里那句成功提示。
FACT_OK_RE = re.compile(r"已写入黑板|已更新黑板")


def ssh(remote: str, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", "kali", remote],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def get_status(scan_id: str) -> str:
    # ⚠️ 这里有个踩过的坑：shlex.quote 加的是 **shell** 引号，不是 **SQL** 引号。
    # 写成 f"WHERE id = {shlex.quote(scan_id)}" 会被 psql 当成标识符
    # （scan_01M5... 不是合法 identifier → 静默返回空 / 报错），
    # 而函数用 `or "(no row)"` 兜底，看起来就像"表里没这行"。
    # 正确做法：SQL 字面量自己加单引号，再整体交给 shlex.quote 做 shell 转义。
    sql_scan_id = "'" + scan_id.replace("'", "''") + "'"
    sql = (
        "SELECT id, status, COALESCE(failure_reason,'') AS reason, "
        "to_char(started_at,'HH24:MI:SS') AS started, "
        "to_char(finished_at,'HH24:MI:SS') AS finished "
        f"FROM scans WHERE id = {sql_scan_id};"
    )
    out = ssh(
        "echo 'kali' | sudo -S -p '' docker exec suvex-db "
        "psql -U suvex -d suvex -A -F '|' -c " + shlex.quote(sql)
    )
    return out.stdout.strip() or "(no row)"


def get_fact_count(scan_id: str) -> str:
    # 同上：SQL 字面量要自带单引号。
    sql_scan_id = "'" + scan_id.replace("'", "''") + "'"
    sql = (
        "SELECT category, count(*) AS n FROM scan_facts "
        f"WHERE scan_id = {sql_scan_id} GROUP BY category ORDER BY 2 DESC;"
    )
    out = ssh(
        "echo 'kali' | sudo -S -p '' docker exec suvex-db "
        "psql -U suvex -d suvex -A -F '|' -c " + shlex.quote(sql)
    )
    return out.stdout.strip() or "(0 rows)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scan_id")
    ap.add_argument("--tail", type=int, default=300000)
    args = ap.parse_args()

    print(f"== scan {args.scan_id} ==")
    print("status|reason|started|finished:")
    print(get_status(args.scan_id))
    print()

    # 日志在 _scan_runs/<scan_id>/scan.log（实测确认，不在 /tmp）。
    #
    # ⚠️ 这里踩过一个坑：`sh -c "test -f X && stat -c '%s bytes' X"` 经过
    # `ssh kali` → 远端 shell → `sudo sh -c` **三层**引号后，`%s` 被远端
    # shell 当成自己的格式串吃掉，`&&` 也可能被拆到外层。结果是文件明明
    # 在（12KB），脚本却报「不存在」——**这类误判比报错更危险**，
    # 它会让人以为扫描没开始。
    #
    # 改法：整条命令只用 `ls -l`，不用 `stat` 的 `%` 格式串，也不放 `&&`。
    log_path = f"/root/suvex/suvex-api/_scan_runs/{args.scan_id}/scan.log"
    probe = ssh(
        "echo 'kali' | sudo -S -p '' ls -l " + shlex.quote(log_path) + " 2>/dev/null"
    )
    print("log:", log_path)
    size_line = probe.stdout.strip()
    if size_line:
        # `ls -l` 的第 5 个字段是字节数
        parts = size_line.split()
        nbytes = parts[4] if len(parts) > 4 else "?"
        print("size:", f"{nbytes} bytes")
    else:
        print("size:", "(不存在 —— 扫描可能还没真正开始跑)")
    print()
    if not size_line:
        return 0

    # 日志是 root 写的，读要 sudo cat。
    log = ssh(
        f"echo 'kali' | sudo -S -p '' tail -c {args.tail} {shlex.quote(log_path)}",
        timeout=120,
    )
    text = log.stdout
    print(f"log: {log_path}  （读到 {len(text)} 字符）")
    print()

    tools: dict[str, int] = {}
    for m in TOOL_RE.finditer(text):
        name = m.group(1)
        tools[name] = tools.get(name, 0) + 1
    print("== 工具调用计数 ==")
    if tools:
        for name, n in sorted(tools.items(), key=lambda kv: -kv[1]):
            print(f"{n:5d}  {name}")
        print(f"{sum(tools.values()):5d}  (total)")
    else:
        print("(日志里还没有 [tool_call])")
    print()

    nudges = NUDGE_RE.findall(text)
    print("== 欠费提醒 ==")
    # ⚠️ 重要：`scan.log` **只记 `[reasoning]` 和 `[tool_call]`**，
    # 从不写入 prompt / 状态段 / tool result（实测 96 行日志逐行确认）。
    # 所以这里数到 0 **不能**证明提醒没注入 —— 它只证明"日志里没有"。
    # 要判断提醒是否生效，必须走另一条路：
    #   1. 看代码层：`render_live_state(fact_debt=6)` 是否含提醒（单元测试覆盖）；
    #   2. 看行为层：模型是否在 debt 到阈值后调了 record_fact。
    # 这个计数留着，只作"如果出现就说明日志格式变了"的信号。
    print(f"日志中命中次数：{len(nudges)}")
    if nudges:
        print(f"报告的轮数：{sorted(set(int(x) for x in nudges))}")
        print("→ 日志里出现了提醒文本（比预期丰富的日志格式）")
    else:
        print("→ 0 次**不构成**'提醒没注入'的证据：scan.log 不记 prompt。")
        print("   判断依据请用 record_fact 调用次数 + scan_facts 行数。")
    print()

    print("== record_fact 成功写入提示 ==")
    print(f"{len(FACT_OK_RE.findall(text))} 次")
    print("（同上：scan.log 不记 tool result，0 次不代表没写成功）")
    print()

    print("== scan_facts 表（按 category）==")
    print(get_fact_count(args.scan_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
