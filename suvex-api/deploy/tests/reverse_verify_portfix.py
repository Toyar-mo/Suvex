#!/usr/bin/env python
"""反向验证 update.sh 本轮三处修复（端口分离 / 就绪轮询 / 状态码拼接）。

思路：把修复逐条「改回去」，确认对应的断言会变红。改不红的断言等于没有守卫，
下次谁把这行改回去都不会有人发现 —— 那两条假警报就会重新回来。

改坏只在**临时副本**上做；仓库文件全程只读，最后再核对一次没被动过。

用法：python reverse_verify_portfix.py            # 全跑
      python reverse_verify_portfix.py M1 M3      # 只跑指定几条
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_SH = HERE.parent / "update.sh"
TEST_SH = HERE / "test_update_web.sh"
REMOTE_DIR = "/tmp/rv_portfix"

CRLF = chr(13) + chr(10)
LF = chr(10)

# (名字, 期望变红的断言标记, 原文, 改坏后)
CASES: list[tuple[str, str, str, str]] = [
    (
        "M1 前端打回后端端口",
        "M1：",
        'WEB_CODE="$(curl -s -o /dev/null -w \'%{http_code}\' "http://127.0.0.1:$WEB_PORT/" 2>/dev/null)" || WEB_CODE=000',
        'WEB_CODE="$(curl -s -o /dev/null -w \'%{http_code}\' "http://127.0.0.1:$HEALTH_PORT/" 2>/dev/null)" || WEB_CODE=000',
    ),
    (
        "M3 就绪退化成单次检查",
        "M3：",
        '    while [ "$WAITED" -lt "$WAIT_READY_SEC" ]; do',
        '    while [ "$WAITED" -lt 1 ]; do',
    ),
    (
        "M4 状态码重复拼接",
        "M4：",
        # 坏写法必须是 `|| echo 000` 落在**命令替换内部**：
        #   $(curl -w '%{http_code}' URL || echo 000)   → "000000"
        # 落在外面（`"$(curl ...)" || echo 000`）时，echo 的输出进了终端
        # 而不是变量，CODE 反而变成空 —— 那是另一种形态，抓不到 000000。
        '"http://127.0.0.1:$HEALTH_PORT$HEALTH_PATH" 2>/dev/null)" || CODE=000',
        '"http://127.0.0.1:$HEALTH_PORT$HEALTH_PATH" || echo 000)"',
    ),
    (
        "N3 回滚的 rsync 去掉 --checksum",
        "N3：",
        'if rsync -a --checksum --delete "$WEB_BACKUP/" "$WEB_DIST/"; then',
        'if rsync -a --delete "$WEB_BACKUP/" "$WEB_DIST/"; then',
    ),
]


def read_text(path: Path) -> str:
    """读文件并统一成 LF —— Windows 工作区的 CRLF 会让远端 bash 报 $'\\r'。"""
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read().replace(CRLF, LF)


def write_text(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline=LF) as fh:
        fh.write(text)


def kali(cmd: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", "kali", cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def push(text: str, remote_name: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sh", delete=False, encoding="utf-8", newline=LF
    ) as fh:
        fh.write(text.replace(CRLF, LF))
        tmp = Path(fh.name)
    try:
        subprocess.run(
            ["scp", "-q", str(tmp), f"kali:{REMOTE_DIR}/{remote_name}"],
            check=True,
            capture_output=True,
        )
    finally:
        tmp.unlink()


def run_suite(update_name: str) -> str:
    # ★ 每轮都必须自己清现场。
    #
    # 测试脚本在失败时会**故意保留** /tmp/wtest 供排查（那是它的职责），
    # 但反向验证要跑很多轮，上一轮的残留（尤其没 umount 掉的 bind mount）
    # 会让下一轮从脏状态开始 —— 于是"还原后仍失败"这种结论根本不可信。
    # 第一版就是这样误判的：手动 rm -rf 后同一份代码立刻全绿。
    out = kali(
        f"echo kali | sudo -S bash -c "
        f"'umount {REMOTE_DIR}/wtest_mnt 2>/dev/null; "
        f"rm -rf /tmp/wtest; "
        f"bash {REMOTE_DIR}/t.sh {REMOTE_DIR}/{update_name}' 2>&1 | tail -40"
    )
    return out.stdout


def main() -> int:
    # Windows 控制台默认 GBK，打印 `✓` 会抛 UnicodeEncodeError 直接崩掉，
    # 而那正是「这条断言变红了」的时刻 —— 最不该崩的地方。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    only = {a.upper() for a in sys.argv[1:]}
    kali(f"mkdir -p {REMOTE_DIR}")

    print("=" * 62)
    print(" 反向验证：update.sh 端口/就绪/状态码三处修复")
    print("=" * 62)

    original = read_text(REPO_SH)
    push(original, "u.sh")
    push(read_text(TEST_SH), "t.sh")

    # 基线：未改动必须全绿。否则后面「变红」证明不了任何事
    # （可能本来就红的，改哪都红）。
    print("\n[基线] 未改动 → 应当全绿")
    base = run_suite("u.sh")
    print("\n".join(base.strip().splitlines()[-6:]))
    if "VERDICT: PASS" not in base:
        print("\n!! 基线不是 PASS，反向验证无意义，中止")
        return 2

    results: list[tuple[str, bool, str]] = []
    for name, marker, old, new in CASES:
        key = name.split()[0]
        if only and key not in only:
            continue
        print(f"\n[改坏] {name}")
        if old not in original:
            print("  !! 找不到改坏锚点（代码已变），跳过")
            results.append((name, False, "锚点缺失"))
            continue
        push(original.replace(old, new, 1), "u_bad.sh")
        text = run_suite("u_bad.sh")
        # 判据是「该条断言变成 FAIL」，不是「整体 FAIL」——
        # 整体 FAIL 可能由任何一条引起，那样证明不了正是这条守卫抓到的。
        red = any(
            line.strip().startswith("[FAIL]") and marker in line
            for line in text.splitlines()
        )
        print(f"  {'变红 ✓' if red else '★ 仍然全绿 —— 这条断言没有守住'}")
        if not red:
            for line in text.splitlines():
                if marker in line:
                    print(f"    相关行：{line.strip()}")
        results.append((name, red, ""))

    push(original, "u.sh")
    print("\n[还原] 原版 → 应当恢复全绿")
    back = run_suite("u.sh")
    print("\n".join(back.strip().splitlines()[-4:]))
    restored = "VERDICT: PASS" in back

    # 保险：确认仓库文件没被写过（改坏只发生在远端临时副本）
    if read_text(REPO_SH) != original:
        write_text(REPO_SH, original)
        print("!! 检测到仓库文件被改动，已还原")

    print("\n" + "=" * 62)
    for name, ok, note in results:
        print(f"  {'变红  ' if ok else 'FAIL  '} {name} {note}")
    print(f"  还原后全绿：{'是' if restored else '否'}")
    ok_final = bool(results) and all(ok for _, ok, _ in results) and restored
    print(f"  VERDICT: {'PASS' if ok_final else 'FAIL'}")
    print("=" * 62)
    return 0 if ok_final else 1


if __name__ == "__main__":
    sys.exit(main())
