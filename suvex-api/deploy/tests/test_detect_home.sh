#!/usr/bin/env bash
# 验证 update.sh 的 detect_home：没有显式 SUPEX_HOME 时能不能找对安装根。
#
# 为什么单独测：`--web` 那套测试全程显式传了 SUPEX_HOME，所以探测逻辑
# 一条分支都没走到。而探测正是 2026-10-25 那个"脚本在任何参数下都死在第一步"
# 缺口的修复本体（原代码默认 /opt/suvex，本机实际在 /root/suvex）。
#
# 四种情形：
#   1. 显式 SUPEX_HOME 指向合法根 → 用它
#   2. 显式 SUPEX_HOME 指向目录但没有 suvex-api/ → 明确报错（不猜）
#   3. systemd unit 的 WorkingDirectory → 用它（本机的真实路径来源）
#   4. 全都探测不到 → 明确失败并给出怎么显式指定
#
# 用法：sudo bash test_detect_home.sh <update.sh 路径>
set -uo pipefail

UPDATE_SH="${1:?用法：sudo bash test_detect_home.sh <update.sh 路径>}"
[ -f "$UPDATE_SH" ] || { echo "找不到 $UPDATE_SH"; exit 2; }

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf '  [PASS] %s\n' "$*"; }
fail() { FAIL=$((FAIL+1)); printf '  [FAIL] %s\n' "$*"; }

ROOT=/tmp/htest
restore() {
  # 如果测试期间造过 systemd unit，务必删掉，别污染真机
  if [ -f /etc/systemd/system/suvex-api.service.d/zz-htest.conf ]; then
    rm -f /etc/systemd/system/suvex-api.service.d/zz-htest.conf
    rmdir /etc/systemd/system/suvex-api.service.d 2>/dev/null || true
    systemctl daemon-reload 2>/dev/null || true
  fi
  rm -rf "$ROOT"
}
trap restore EXIT
rm -rf "$ROOT"

mk_root() {  # $1=路径  造一个"像"的安装根（能被 detect_home 接受）
  mkdir -p "$1/suvex-api/.venv/bin"
  printf '#!/bin/sh\nexit 0\n' > "$1/suvex-api/.venv/bin/python"
  chmod +x "$1/suvex-api/.venv/bin/python"
  : > "$1/suvex-api/alembic.ini"
}

# 空的后端包（只为让脚本走到前置检查之后）
mkdir -p "$ROOT/fakeback/app"; echo x > "$ROOT/fakeback/app/__init__.py"
( cd "$ROOT/fakeback" && tar czf "$ROOT/backend.tgz" app )

run_upd() {  # $1=log ; 其余=环境变量由调用处设置
  local log="$1"; shift
  bash "$UPDATE_SH" "$ROOT/backend.tgz" --dry-run --no-restart > "$log" 2>&1
}

# 在"没有 SUPEX_HOME"的环境里跑 —— 必须用子 shell 显式 unset。
# 不能写 `env -u SUPEX_HOME run_upd ...`：`env` 只能 exec 外部命令，
# 而 run_upd 是 shell 函数，会报 127 "No such file or directory"。
run_upd_nohome() {  # $1=log
  (
    unset SUPEX_HOME
    export SUPEX_DATABASE_URL="postgresql+psycopg://s:s@127.0.0.1:5432/s"
    run_upd "$1"
  )
}

# ── 1. 显式 SUPEX_HOME，合法 ──────────────────────────────
echo "=== 1. 显式 SUPEX_HOME 合法 → 采用 ==="
mk_root "$ROOT/rootA"
SUPEX_HOME="$ROOT/rootA" SUPEX_DATABASE_URL="postgresql+psycopg://s:s@127.0.0.1:5432/s" \
  run_upd "$ROOT/l1.txt"
RC=$?
[ "$RC" -eq 0 ] && pass "退出码 0" || { fail "退出码 $RC"; tail -8 "$ROOT/l1.txt" | sed 's/^/    /'; }
grep -qF "$ROOT/rootA" "$ROOT/l1.txt" && pass "输出里用了 rootA" || fail "没用 rootA"

# ── 2. 显式 SUPEX_HOME，但目录里没有 suvex-api ────────────
echo
echo "=== 2. 显式 SUPEX_HOME 非法 → 必须明确报错（不静默退回默认值） ==="
mkdir -p "$ROOT/rootBad"
SUPEX_HOME="$ROOT/rootBad" SUPEX_DATABASE_URL="postgresql+psycopg://s:s@127.0.0.1:5432/s" \
  run_upd "$ROOT/l2.txt"
RC=$?
[ "$RC" -eq 3 ] && pass "退出码 3（前置条件不满足）" || fail "退出码 $RC（期望 3）"
grep -q '下没有 suvex-api' "$ROOT/l2.txt" && pass "错误信息指明了原因" || fail "错误信息不明确"
grep -q '/opt/suvex' "$ROOT/l2.txt" && fail "竟然退回了 /opt/suvex 默认值" || pass "没有偷偷退回默认值"

# ── 3. 无 SUPEX_HOME，靠 systemd WorkingDirectory 探测 ────
echo
echo "=== 3. 无 SUPEX_HOME，用 systemd unit 的 WorkingDirectory ==="
mk_root "$ROOT/rootC"
# 用 drop-in 覆盖 WorkingDirectory，不改真 unit（可逆、不留痕）
mkdir -p /etc/systemd/system/suvex-api.service.d
cat > /etc/systemd/system/suvex-api.service.d/zz-htest.conf <<CONF
[Service]
WorkingDirectory=$ROOT/rootC/suvex-api
CONF
systemctl daemon-reload 2>/dev/null || true
WD_PROBE="$(systemctl show -p WorkingDirectory --value suvex-api 2>/dev/null || true)"
if [ "$WD_PROBE" = "$ROOT/rootC/suvex-api" ]; then
  pass "drop-in 生效（探测前提成立）"
else
  fail "drop-in 没生效（读出：$WD_PROBE）—— 本机可能没有 suvex-api.service"
fi
(
  unset SUPEX_HOME
  export SUPEX_DATABASE_URL="postgresql+psycopg://s:s@127.0.0.1:5432/s"
  run_upd "$ROOT/l3.txt"
)
RC=$?
[ "$RC" -eq 0 ] && pass "退出码 0" || { fail "退出码 $RC"; tail -10 "$ROOT/l3.txt" | sed 's/^/    /'; }
grep -qF "$ROOT/rootC" "$ROOT/l3.txt" && pass "探测到 rootC（来自 unit）" || fail "没探测到 rootC"
# 删掉 drop-in 再继续
rm -f /etc/systemd/system/suvex-api.service.d/zz-htest.conf
rmdir /etc/systemd/system/suvex-api.service.d 2>/dev/null || true
systemctl daemon-reload 2>/dev/null || true

# ── 4. 完全探测不到 → 明确失败 ────────────────────────────
echo
echo "=== 4. 完全探测不到 → 明确失败并告诉怎么指定 ==="
# 把 unit 的 WorkingDirectory 改成不存在的地方，同时清掉 /etc/suvex/api.env 影响
mkdir -p /etc/systemd/system/suvex-api.service.d
cat > /etc/systemd/system/suvex-api.service.d/zz-htest.conf <<CONF
[Service]
WorkingDirectory=/nonexistent/zz-htest
CONF
systemctl daemon-reload 2>/dev/null || true
(
  unset SUPEX_HOME
  export SUPEX_DATABASE_URL="postgresql+psycopg://s:s@127.0.0.1:5432/s"
  run_upd "$ROOT/l4.txt"
)
RC=$?
[ "$RC" -eq 3 ] && pass "退出码 3" || fail "退出码 $RC（期望 3）"
grep -q '探测不到安装根' "$ROOT/l4.txt" && pass "错误信息明确" || fail "错误信息不明确"
grep -q 'SUPEX_HOME=' "$ROOT/l4.txt" && pass "给出了显式指定的用法" || fail "没给出用法"
rm -f /etc/systemd/system/suvex-api.service.d/zz-htest.conf
rmdir /etc/systemd/system/suvex-api.service.d 2>/dev/null || true
systemctl daemon-reload 2>/dev/null || true

# ── 5. 真机实际路径（本机 /root/suvex）能被探测到 ─────────
echo
echo "=== 5. 真机：不传 SUPEX_HOME 时能不能找到 /root/suvex ==="
if [ -d /root/suvex/suvex-api ]; then
  (
    unset SUPEX_HOME
    export SUPEX_DATABASE_URL="postgresql+psycopg://s:s@127.0.0.1:5432/s"
    run_upd "$ROOT/l5.txt"
  )
  if grep -q '/root/suvex' "$ROOT/l5.txt"; then
    pass "探测到真机安装根 /root/suvex"
  else
    fail "没探测到 /root/suvex"
    head -20 "$ROOT/l5.txt" | sed 's/^/    /'
  fi
  grep -q '/opt/suvex' "$ROOT/l5.txt" && fail "还在用 /opt/suvex" || pass "不再依赖 /opt/suvex 假设"
else
  echo "  [SKIP] 本机没有 /root/suvex/suvex-api，跳过"
fi

echo
echo "=============================================="
echo "  PASS: $PASS   FAIL: $FAIL"
if [ "$FAIL" -eq 0 ]; then echo "  VERDICT: PASS"; RC=0; else echo "  VERDICT: FAIL"; RC=1; fi
echo "=============================================="
exit $RC
