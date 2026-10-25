#!/usr/bin/env python
"""update.sh 反向验证：逐条改坏 → 两个测试脚本必须变红 → 还原。

每个用例改一处「我以为在守」的约束，然后跑对应测试，断言退出码 != 0。
如果改坏了还是绿的，说明那条测试是假绿 —— 它守的不是我声称的东西。
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
UPD = ROOT / "deploy" / "update.sh"
HERE = pathlib.Path(__file__).resolve().parent

CASES = [
    # (名字, 原文, 改坏后, 期望测试脚本, 期望退出码非零的关键token)
    (
        "A. 默认安装根退回 /opt/suvex（原缺口原样复现）",
        'detect_home() {\n  # 1. 显式传入优先',
        'detect_home() {\n  printf "%s" "/opt/suvex";\n  return 0\n  # 1. 显式传入优先',
        "test_detect_home.sh",
        None,
    ),
    (
        "B. 探测不到时静默猜 /root/suvex（而不是明确失败）",
        '''  die 3 "探测不到安装根。请显式指定：''',
        '''  printf '%s' /root/suvex; return 0
  die 3 "探测不到安装根。请显式指定：''',
        "test_detect_home.sh",
        None,
    ),
    (
        "C. 前端切换改成 rm -rf + cp（2026-10-25 事故原样复现）",
        'if ! rsync -a --delete "$WEB_SRC/" "$WEB_DIST/"; then',
        'rm -rf "$WEB_DIST"/* ; if ! cp -a "$WEB_SRC/." "$WEB_DIST/"; then',
        "test_update_web.sh",
        None,
    ),
    (
        "D. 去掉结构校验（空 assets 也放行）",
        '  [ "$ASSET_N" -gt 0 ] || { rm -rf "$WEB_STAGE"; die 6 "前端产物里 assets/ 一个文件都没有 —— 拒绝发布"; }',
        '  : # 校验被删掉',
        "test_update_web.sh",
        None,
    ),
    (
        "E. 去掉「切换后校验」（rsync 假成功就发现不了）",
        '''    AFTER_N="$(find "$WEB_DIST/assets" -type f 2>/dev/null | wc -l || true)"
    if [ "$AFTER_N" -ne "$ASSET_N" ]; then''',
        '''    AFTER_N="$ASSET_N"
    if false; then''',
        "test_update_web.sh",
        None,
    ),
    (
        "E2. 去掉「发布后 index.html 存在」检查",
        '''    if [ ! -s "$WEB_DIST/index.html" ]; then
      web_rollback "发布后 $WEB_DIST/index.html 缺失"
    fi''',
        '''    : # 检查被删掉''',
        "test_update_web.sh",
        None,
    ),
    (
        "F. 不做旧产物备份（回滚点变空壳）",
        '    if ! rsync -a "$WEB_DIST/" "$WEB_BACKUP/"; then',
        '    if false; then',
        "test_update_web.sh",
        None,
    ),
    (
        "F2. 备份失败时继续往下走（不拦）",
        '      die 6 "旧产物备份失败，未做任何改动（现场完好）"',
        '      : # 不拦，继续',
        "test_update_web.sh",
        None,
    ),
    (
        "G. --web 后跟另一个选项时静默当成路径",
        '''      if [ $# -lt 2 ] || [ "${2#-}" != "$2" ]; then''',
        '''      if [ $# -lt 2 ]; then''',
        "test_update_web.sh",
        None,
    ),
]


def run(test_script: str) -> tuple[int, str]:
    """把当前 update.sh 与测试脚本送到 Kali 并跑，返回 (rc, 输出)。

    测试脚本也一起上传 —— 这样反向验证永远测的是**仓库里这一份**，
    不会因为 Kali 上 /tmp 里躺着旧版本而产生假结果。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        # 统一换行，否则 Kali 上 bash 会因 \r 报错
        upd_local = tmp / "update_under_test.sh"
        upd_local.write_bytes(UPD.read_bytes().replace(b"\r\n", b"\n"))
        tst_local = tmp / test_script
        tst_local.write_bytes((HERE / test_script).read_bytes().replace(b"\r\n", b"\n"))

        subprocess.run(
            ["scp", "-q", str(upd_local), "kali:/tmp/update_under_test.sh"], check=True
        )
        subprocess.run(
            ["scp", "-q", str(tst_local), f"kali:/tmp/{test_script}"], check=True
        )

    r = subprocess.run(
        ["ssh", "kali", f'echo kali | sudo -S -p "" bash /tmp/{test_script} /tmp/update_under_test.sh'],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    original = UPD.read_text(encoding="utf-8")

    print("=" * 70)
    print("步骤 1：基线 —— 未改动的 update.sh 必须全绿")
    print("=" * 70)
    for t in ("test_update_web.sh", "test_detect_home.sh"):
        rc, out = run(t)
        verdict = "PASS" if rc == 0 else "FAIL"
        print(f"  {t}: {verdict} (rc={rc})")
        if rc != 0:
            print(out)
            print("!! 基线就不绿，先修产品代码再谈反向验证")
            return 1

    results = []
    for name, old, new, test_script, _ in CASES:
        print()
        print("=" * 70)
        print(f"改坏：{name}")
        print(f"目标测试：{test_script}")
        print("=" * 70)

        if old not in original:
            print("!! 找不到改坏锚点 —— 用例本身过期了，需要更新脚本")
            results.append((name, "STALE-ANCHOR"))
            continue

        broken = original.replace(old, new, 1)
        UPD.write_text(broken, encoding="utf-8")
        try:
            rc, out = run(test_script)
        finally:
            UPD.write_text(original, encoding="utf-8")

        # 还原后再确认一次语法没坏
        if rc != 0:
            print(f"  ✅ 变红 (rc={rc}) —— 测试确实在守这条约束")
            tail = [ln for ln in out.splitlines() if "[FAIL]" in ln][:3]
            for ln in tail:
                print("     " + ln.strip())
            if not tail:
                # 没有 [FAIL] 行说明是"脚本直接崩了"而不是"断言失败"，
                # 这也算变红（守卫起作用了），但把原因打出来便于判断。
                for ln in out.splitlines()[-4:]:
                    print("     | " + ln.strip())
            results.append((name, "RED"))
        else:
            print("  ❌ 仍然全绿 —— 这条测试是假绿！")
            results.append((name, "FALSE-GREEN"))

    # 确认源码已完全还原
    assert UPD.read_text(encoding="utf-8") == original, "源码未还原！"

    print()
    print("=" * 70)
    red = sum(1 for _, s in results if s == "RED")
    bad = [n for n, s in results if s != "RED"]
    print(f"  反向验证：{red}/{len(results)} 变红")
    for n, s in results:
        if s != "RED":
            print(f"    {s}: {n}")
    print("  VERDICT:", "PASS" if not bad else "FAIL")
    print("=" * 70)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
